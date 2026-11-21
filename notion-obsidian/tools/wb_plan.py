#!/usr/bin/env python3
"""wb_plan: the Obsidian -> Notion write-back PLANNER (WRITEBACK_SPEC v1.1 §4-§8, §10.2; IMPL_CONTRACT §5.13 I1).

Read-only with respect to Notion: every Notion access goes through the injected read client (a ReadOnlyClient,
which refuses any write before I/O). The write-back token is never loaded here.

    ctx = PlanContext(paths, cfg, db, read_client=rc)          # db = writeback.db connection (wb_state.connect)
    plan, preview = build_plan(ctx, scope=None)                  # pure computation (no file / db writes)
    info = create_plan(ctx, plan, preview)                       # plan.json + preview.md + vault copy + db rows

Three-way classification per note (WB §4.1/§4.2): L = vault file, B = latest base_history row (text from
state/base/<sha>, block geometry from writeback.db block_lines), R = live Notion (page object with completed
properties + block tree) compared by wb_norm (page_json_sha / block_json_sha).

Body changes become UPDATE / APPEND (one M item per page) and TRASH_BLOCK (one B item per block, bound to the M
item of the page). THE ORACLE (contract §5.13): the planned ops are applied to an overlay of R and the page is
re-rendered with Exporter.render_page(..., sandbox=True, record=True); the body must equal L's body (numbered-list
renumbering normalised; normalized items compared after L was normalised through parse -> render). Pages that fail
are REFUSED; no op of a refused page is ever emitted.

Interface for the executor (I2, contract §5.13.1): load_plan, Selection, selection_from_args, selected_items,
challenge_phrase. plan.json is written once (atomic) and never edited; status lives in writeback.db.

Op shapes in plan.json (`ops` of an item):
  UPDATE        {op, target_id, parent_id, block_type, before_norm_sha, request:{<type>:{...}}, expected_after, normalized}
  APPEND        {op, parent_id, position:{type:after_block|start}|null, anchor_id, anchor_next_id, children:[...],
                 new_blocks, expected_after:[canon_writable...], normalized}
  TRASH_BLOCK   {op, target_id, parent_id, block_type, before_norm_sha}
  UPDATE_PROPERTIES {op, target_id, page_id, properties:{name: value}, request:{properties:{...}},
                 before:{name: json_norm(prop)}, expected_after:{...}}                       (row properties, M item)
  RENAME        {op, target_id, page_id, title_property, old_title:[rich_text], new_title, request, expected_after}
  TRASH_PAGE    {op, target_id, page_id, request:{in_trash:true}, page_json_sha, last_edited_time, subtree_ids}
  CREATE_PAGE   {op, parent:{type, page_id|data_source_id}, title, title_property, properties:{...}, children:[...],
                 new_blocks, local_rel}
(op names and fields follow the executor's vocabulary, wb_apply.py docstring)
  MOVE_PAGE     {op, target_id, old_parent_id, new_parent:{type:page_id, page_id}}
"""
import collections
import copy
import dataclasses
import datetime
import difflib
import json
import math
import os
import posixpath
import re
import secrets
import sqlite3
import sys
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402
from wb_common import cli_command
import wb_config  # noqa: E402
import wb_norm  # noqa: E402
import wb_scan  # noqa: E402
import wb_state  # noqa: E402

PLAN_FORMAT = 1
CATEGORIES = ("N", "M", "B", "D", "T", "MV", "C")
CAT_ORDER = {c: i for i, c in enumerate(CATEGORIES)}
PLAN_ID_RE = re.compile(r"wb-\d{8}T\d{4,6}-[0-9a-f]{4}")
ITEM_ID_RE = re.compile(r"(N|M|B|D|T|MV|C)-\d{3,}")
UNKNOWN = "未知"
RESIDUAL_RISK = "API 没有条件写，单个请求的预检与写入之间仍有约 1 秒窗口；执行期间请不要在 Notion 中编辑这些页面"
NORMALIZED_NOTE = "写回后本地文件会被规范化为 Notion 渲染格式"   # == md2blocks.HINT_NORMALIZED (asserted in tests)
OPEN_STATUSES = ("draft", "confirmed")
NOTION_URL = "https://app.notion.com/p/{}"
TRASH_HINT = "未开启块删除，请在 Notion 中手工删除后再写回其余改动"
QUOTA_HINT = "额度受限，需在 Notion 中手工添加或升级套餐"
# L-17: a local rename / move that no refresh has registered yet blocks EVERY item of the plan (blocked_by code)
UNREGISTERED_RENAME = "UNREGISTERED_RENAME"
UNREGISTERED_RENAME_HINT = "检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划"
UNREGISTERED_KIND_TEXT = {"renamed_same_folder": "同文件夹改名", "moved_other_folder": "跨文件夹移动",
                          "unsupported_rename": "不支持的改名（folder note / 数据库 / 模板）"}
# fix loop 2: a refresh round opened the name override but did not publish it (paused for review / failed)
OVERRIDE_UNPUBLISHED_TEXT = ("刷新已记录命名差异但尚未发布：若最近一轮刷新已暂停待评审，"
                             "请查看最近一轮暂停说明中的完整放行命令，或等下一轮成功发布后再写回")
# a same-folder rename the exporter rejected as a name override (refresh hold override_rejected:<why>): waiting for
# the next sync does not help, the note has to be renamed again
OVERRIDE_REJECTED_TEXT = {"unsafe_name": "新文件名含导出时会被替换的写法（% # ? : [ ] ^ | 等符号、...、连续空格或结尾的点）",
                          "name_clash": "新文件名与同一文件夹里的其他笔记重名",
                          "folder_note": "folder note 不能改名", "different_folder": "不在原文件夹内"}
OVERRIDE_REJECTED_HINT = ("本地新文件名未被导出器接受，等待同步不会解除：请把这篇笔记再改一次名（或改回原名、或在 Notion 中改标题），"
                          "下一次自动同步后暂停自动解除")
# L-23 (planner / executor hold parity): the texts of wb_apply._hold_check / _hold_release_hint for an object's OWN
# hold (the planner refuses the note with them, the executor skips such an item with the same words)
SCAN_HOLD_SOURCES = ("scan", "export")      # refresh.py: holds it recomputes every round (auto release when fixed)
HOLD_SCAN_RELEASE_HINT = ("这类暂停由 refresh 每轮按扫描结果重新计算：问题消失后（例如 Sync 冲突副本已删除）下一轮 refresh "
                          "自动解除，也可人工运行 `{command}`；解除后重新生成计划")
HOLD_MANUAL_RELEASE_HINT = "人工处理并解除暂停后重新生成计划"
PENDING_HOLD_TEXT = "该对象处于删除保留（pending_deletes=hold，本地笔记已删除）：不写入"
PENDING_HOLD_HINT = ("这篇笔记仍登记为本地删除（pending_deletes=hold）：文件已放回时，下一轮 refresh 把这条删除保留标为 "
                     "restored 后即可写回（重新生成计划）；要删除它，请删掉本地文件后按 D 项确认")
WB_BOT_NOTE = "写回集成"
# exporter character substitutions (sanitize) that a Notion title must not contain (WB 7.3)
EXPORT_SUBST = set("／：＊？＂＜＞｜＃＾［］…％＼")
DEDUP_SUFFIX_RE = re.compile(r" \([0-9a-f]{8,32}\)$")
TRUNC_SUFFIX_RE = re.compile(r"… \([0-9a-f]{8}\)$")
RENUM_RE = re.compile(r"^((?:[ \t]|>)*)\d{1,9}\. ")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
READONLY_PROP_TYPES = ("relation", "people", "files", "formula", "rollup", "created_time", "created_by",
                       "last_edited_time", "last_edited_by", "unique_id", "verification", "button")
HINTS = {
    "STALE_REVERT": "本地改动把 Notion 先前发布的内容改回了旧版本（可能是从 Sync 版本历史恢复）；请先核对 Notion 当前版本，再决定是否恢复本地基线或在 Notion 中手工修改",
    "SYNC_SETTLING": "Obsidian Sync 可能仍在同步；请等待同步完成（并保持 Obsidian 运行）后重新生成计划",
    "UNSUPPORTED_REGION": "改动落在只读（X 类）块内；请在 Notion 中手工修改",
    "LINK_REWRITE_NOOP": "只有 wikilink 写法变化，指向的页面不变；不写 Notion，由同步流程处理",
    "LOCAL_KEYS_ONLY": "只改了不在 Notion 中的 frontmatter 键；保留在库内，不写 Notion",
    "READONLY_KEY_CHANGED": "notion_* 键是只读的；请还原这些键并重新生成计划",
    "LOCKED": "Notion 页面已锁定；请在 Notion 中解锁后重新生成计划，或手工修改",
    "OUT_OF_WHITELIST": "目标不在声明的写回范围（whitelist_page_ids / whitelist_database_ids）",
    "BLOCKED_PAGE": "页面处于 BLOCKED / PARTIAL_CREATE；查看状态中的页面编号及解除命令，核对远端后再解除",
    "RENDERER_MISMATCH": "base 的渲染器版本与当前不同；等待下一轮 refresh 重新发布该笔记后再写回",
    "INITIAL_BACKLOG": "启用写回之前就存在的本地差异；请先确认本次需要写回该笔记，再按预览中的积压确认命令处理",
    "POSSIBLE_DUPLICATE": "与已有页面内容相同或高度相似；请确认不是重复后改写内容，或在 Notion 中手工新建",
    "DELETE_UNSUPPORTED": "此对象不能经写回删除；请在 Notion 中手工处理（下一轮会按 Notion 版本恢复本地文件）",
    "UNSUPPORTED_RENAME": "改文件夹名、跨文件夹移动或 folder note 改名不支持写回；请在 Notion 中手工调整",
    "SCHEMA_OPTION_MISSING": "选项不存在于数据库 schema；请先在 Notion 中添加选项（写回不新建选项）",
    "PROPERTY_LOSSY": "该属性值不能无损写回；请在 Notion 中手工修改",
    "AUTOMATION_RISK": "可能触发数据库自动化，未执行；把数据库列入 automation_free_database_ids 后才可写回",
    "CONFLICT_DEL_EDIT": "Notion 侧在本地删除后又有改动；不删除，下一轮按 Notion 版本恢复本地文件",
    "ORACLE_MISMATCH": "拟写入内容重新渲染后与本地文件不一致；请简化改动或在 Notion 中手工修改",
    "NO_BASE": "该笔记还没有 base 记录；等待下一轮 refresh 发布后再写回",
    "FRONTMATTER_ERROR": "frontmatter 无法解析；请修复后重新生成计划",
    "MAPPING_ANOMALY": "映射异常（同一 notion_id 多个文件、路径与 id 不符等）；请人工处理",
    "SYNC_CONFLICT_COPY": "存在 Obsidian Sync 冲突副本；请先合并或删除副本",
    "R_UNREACHABLE": "读连接无法读取该页面（未共享或已删除）",
    "LOCAL_DELETED": ("本地删除尚未登记：下一轮自动同步（或手动运行 {command}）会把它登记为"
                      "待决删除（hold），之后重新生成计划，它才会作为 D 项出现、可以确认删除"),
    "READONLY_NOTE_KIND": "数据库 / 模板 / 生成笔记只读，不写回",
    "NOT_EXPORTED": "对象不在已发布快照的导出树中；等待下一轮 refresh",
    "BLANK_LINES": "只有空行变化无法在 Notion 中表示；请撤销多余空行",
    "N_LOCATION": "新文件只能放在白名单页面（或白名单数据库）的 folder note 文件夹中",
    "N_NEEDS_RENAME": "文件名含导出时会被替换的字符或后缀；请改名后再新增",
    "NEEDS_REFRESH": "依据的快照早于 24 小时（或之后有 Notion 变化）；先运行 {command} 刷新快照，再重新规划删除",
    "ALREADY_TRASHED": "Notion 页面已在回收站",
    "AUTO_SYNC_HOLD": "该对象的 Notion → Obsidian 自动同步处于暂停（state/auto_sync_hold.json）；人工处理并解除后再写回",
    "LINE_ENDINGS": "文件被改成了 Windows 换行（CRLF）；请在编辑器中改回 LF 换行后重新生成计划（内容本身不必改）",
    "UTF8_BOM": "文件开头有 UTF-8 BOM（不可见字符）：写回后库内采纳会把 frontmatter 当成正文、丢失本地属性（tags 等）；"
                "请在编辑器中另存为“UTF-8（无 BOM）”后重新生成计划（内容本身不必改）",
    "NOT_LEAF": "阶段 2（mode=whitelist / plan）只写叶子页面：该页面含子页面或子数据库；请在 Notion 中手工修改，或在阶段 3 处理",
    "BLOCK_TRASH_DISABLED": TRASH_HINT,
    "QUOTA": QUOTA_HINT,
    "BREAKER": "本地改动规模超过熔断阈值；核对全部本地异常，确认是本人所为后按预览的熔断确认步骤重新生成全库计划",
    "RENAME_NOT_WRITABLE": ("本地新名不满足改名写回条件（新名必须能原样作为 Notion 标题、导出时不被替换字符或加后缀）；"
                            "Notion 标题保持不变，本地文件名记为命名差异"),
    "MV_NOT_SUPPORTED": "页面移动（阶段 3）尚未由执行器实现；请在 Notion 中手工移动",
    "CONFLICT": "冲突项需按预览逐项选择以 Notion 或本地为准",
    "N_ALREADY_CREATED": "该本地文件之前已经通过写回新建过页面（或新建结果未知）；请先运行 {command} 查看未知请求和停用页面的完整处理命令，不会重复新建",
    UNREGISTERED_RENAME: UNREGISTERED_RENAME_HINT,
}
# r1-user-journey-02: plan-level blockers (WB 2.6): executability is fixed in plan.json, so the way out is a NEW plan
PLAN_LEVEL_HINTS = {
    "SYNC_SETTLING": "这份计划之后也不会变成可执行：请保持 Obsidian 在本机运行，等同步完成（启动满 sync_settle_minutes "
                     "分钟，默认 15）后重新运行 {command}",
}
# short, plain-language description of each refusal class (the class code itself is shown separately)
CLASS_TEXT = {
    "ORACLE_MISMATCH": "拟写入内容重新渲染后与本地文件不一致",
    "TYPE_CHANGED": "块类型变化（例如标题级别、列表类型）不能写回",
    "STRUCTURE_CHANGED": "缩进 / 层级 / 同页移动不能写回",
    "UNSUPPORTED_REGION": "改动落在只读块内",
    "UNSUPPORTED_INLINE": "含写回不支持的行内写法",
    "UNSUPPORTED_BLOCK": "含写回不支持的块",
    "LIMIT": "超出 Notion 请求限制",
    "BLANK_LINES": "只有空行变化",
    "REPARSE_MISMATCH": "改动不能无歧义地解析成块操作",
}


class _WbError(Exception):
    """Write-back planning/executing error (message in Chinese, safe to print)."""


WbError = getattr(wb_common, "WbError", None) or _WbError


class _NoMatch(Exception):
    """An interpretation of a diff hunk does not reproduce L (try the next one)."""


_REFUSAL_RANK = ("UNSUPPORTED_BLOCK:ambiguous_nesting", "UNSUPPORTED_INLINE", "UNSUPPORTED_REGION", "TYPE_CHANGED",
                 "STRUCTURE_CHANGED", "UNSUPPORTED_BLOCK", "LIMIT", "BLANK_LINES", "REPARSE_MISMATCH")


def _most_specific(refusals, MB):
    """The refusal the user can act on: a to-do nesting refusal (L-03, also reported by rebuild_block as
    TYPE_CHANGED with the nesting hint) first, then inline / region / structure reasons."""
    def norm(e):
        if getattr(e, "hint", "") == getattr(MB, "HINT_NESTING", None) and not e.reason.startswith("UNSUPPORTED_BLOCK"):
            return MB.Refused("UNSUPPORTED_BLOCK:ambiguous_nesting", e.hint)
        return e

    def rank(e):
        for i, p in enumerate(_REFUSAL_RANK):
            if e.reason == p or e.reason.startswith(p):
                return i
        return len(_REFUSAL_RANK)
    return min((norm(e) for e in refusals), key=rank)


def _refusal_text(cls, detail=None, where=None):
    """Refusal reason a person can act on: what (plain words), where (file lines + the hunk text), detail."""
    head = cls.split(":")[0]
    txt = CLASS_TEXT.get(cls) or CLASS_TEXT.get(head) or cls
    if ":" in cls and cls not in CLASS_TEXT:
        txt += f"（{cls.split(':', 1)[1]}）"
    if detail:
        txt += f"：{detail}"
    if where:
        if where.get("l_lines"):
            a, z = where["l_lines"]
            loc = f"本地文件第 {a} 行" if a == z else f"本地文件第 {a}–{z} 行"
        else:
            a, z = where["b_lines"]
            loc = f"base 第 {a} 行" if a == z else f"base 第 {a}–{z} 行"
        lsn = " ⏎ ".join(x.strip() for x in where.get("l_text") or [] if x.strip())
        bsn = " ⏎ ".join(x.strip() for x in where.get("b_text") or [] if x.strip())
        if lsn:
            txt += f"；位置：{loc}「{_trunc(lsn, 120)}」" + (f"（原为「{_trunc(bsn, 80)}」）" if bsn else "")
        elif bsn:
            txt += f"；位置：{loc}附近（删除了「{_trunc(bsn, 120)}」）"
        else:
            txt += f"；位置：{loc}（只有空行）"
    return txt


X_REASON_TEXT = {"color": "含颜色文字", "mention": "含提及（@页面 / 人员 / 日期）", "own_line_lost": "块与文件行的对应关系已丢失",
                 "synced": "同步块的内容", "owner": "来自其他页面的内容", "type:child_page": "指向子页面的链接行",
                 "type:child_database": "子数据库", "type:link_to_page": "指向其他页面的链接块",
                 "notion_link": "含指向 Notion 页面的链接", "anchor_link": "含 #锚点链接", "caption": "含图注（caption）",
                 "toggleable_heading": "可折叠标题", "code_fence_paragraph": "像代码块的段落（行内代码含换行）"}


def x_reason_text(xr):
    """r1-user-journey-08: an exporter x_reason code in plain words (the code itself is kept next to it)."""
    xr = str(xr or "")
    if xr in X_REASON_TEXT:
        return X_REASON_TEXT[xr]
    if xr.startswith("mention_"):                  # the exporter names the mention type: mention_page / _user / _date
        return X_REASON_TEXT["mention"]
    if xr.startswith("type:"):
        return f"写回不支持的块类型 {xr[5:]}"
    return f"只读内容（{xr}）" if xr else "只读内容"


def reason_hint(code, default=""):
    hint = HINTS.get(code, default)
    if code in ("LOCAL_DELETED", "NEEDS_REFRESH"):
        return hint.format(command=cli_command("refresh", "--mode", "incremental"))
    if code == "N_ALREADY_CREATED":
        return hint.format(command=cli_command("writeback", "status"))
    return hint


def plan_level_hint(code):
    hint = PLAN_LEVEL_HINTS.get(code, "")
    return hint.format(command=cli_command("writeback", "plan"))


def blocked_text(code):
    """A blocked_by code with its plain-language explanation (codes that already carry text are kept; a
    "CODE：text" code also gets the code's hint -- r1-user-journey-02: SYNC_SETTLING then says to re-run plan)."""
    code = str(code)
    if "：" in code:
        head = code.split("：", 1)[0]
        h = plan_level_hint(head) or reason_hint(head)
        return f"{code}（{h}）" if h and h not in code else code
    head = code.split(":")[0].split("（")[0]
    h = reason_hint(head)
    return f"{code}（{h}）" if h and "（" not in code else code


def unregistered_renames(scan, holds=None, parents=None):
    """L-17: the local renames / moves of a read-only wb_scan result that no refresh has registered yet, as
    [{kind, notion_id, old_rel, new_rel[, reason][, override_open]}] (sorted). While this list is non-empty EVERY
    item of a plan is blocked (UNREGISTERED_RENAME): the adoption after an apply exports and verifies with what a
    refresh registered only, so it would end partially_applied (W6) and a renamed child's parent note could not be
    written (W3 F1).

    What a refresh registers (and what therefore does NOT count here):
    * any kind: a refresh round that PUBLISHED it. The publisher then writes the note at its local name (name
      override) and moves the object's published_files row there, so the scan no longer lists it at all. An open
      name_overrides row alone (the scan's override "existing") is NOT enough (fix loop 2): a round that opens it
      and then pauses (paused_for_review, e.g. conflict_on_page_with_children after Obsidian rewrote the link in
      the parent folder note) or fails publishes nothing — published_files and the sortspec keep the old name and
      the apply's adoption ended partially_applied (W6 C05). Such an entry carries override_open True (display);
    * any kind (L-20a (i)): ANY entry of the object's OWN hold — subtree or note-only, whatever its `paths`. A
      held object is left alone by the publisher (SKIP_HELD), so its rename stays listed for as long as the hold
      lasts, and the planner / executor refuse its own items (AUTO_SYNC_HOLD). Example: a local rename plus an
      Obsidian Sync conflicted copy named after the OLD name -> the refresh holds the note note-only
      (sync_conflict_copy) with paths = the copy's original (old) name; before L-20a that blocked every plan,
      round after round;
    * any kind: an ANCESTOR (export parent chain `parents`: objects_map as {notion_id: parent_id} or
      load_export_state's {notion_id: {parent_id, ...}}) held WITH its subtree (subtree not False) — any source:
      the refresh's own holds (a move / unsupported rename: unsupported_rename) and the manual-release holds
      (post_verify / plan / race). The publisher leaves a held object's whole subtree alone (so the scan keeps
      listing a rename below a held ancestor after every published round), and the adoption publishes and
      verifies with every hold BY NOTION ID: verify_vault excuses the held subtree wherever its notes are, and the
      planner refuses their items (AUTO_SYNC_HOLD, ancestors included). A note-only ancestor hold does not count.
    `holds` (L-20a (ii)) must be the SAME hold set the publisher and the adoption use: adoption_holds(), i.e.
    state/auto_sync_hold.json + pending_deletes on hold + page_state BLOCKED / PARTIAL_CREATE (wb_apply._adopt_holds).
    Obsidian Sync conflicted copies are never renames (the scan lists them separately; WB 4.0 handles them)."""
    scan = scan or {}
    holds = holds if isinstance(holds, dict) else {}

    def nfc(s):
        return unicodedata.normalize("NFC", str(s or ""))

    def k32(x):
        return str(x or "").replace("-", "").lower()

    by32 = {k32(k): v for k, v in holds.items() if isinstance(v, dict)}
    up = {}
    for k, v in (parents or {}).items():
        p = v.get("parent_id") if isinstance(v, dict) else v
        if k and p and isinstance(p, str):
            up[k32(k)] = k32(p)

    def held(nid):
        if by32.get(k32(nid)) is not None:          # L-20a (i): any own hold registers it
            return True
        seen, a = {k32(nid)}, up.get(k32(nid))
        while a and a not in seen and len(seen) < 5000:
            seen.add(a)
            h = by32.get(a)
            if h is not None and h.get("subtree", True) is not False:
                return True
            a = up.get(a)
        return False
    open_at = {(k32(o.get("notion_id")), nfc(o.get("local_rel"))) for o in scan.get("overrides_open") or []
               if isinstance(o, dict) and o.get("local_rel")}
    out = []
    for r in scan.get("renamed_same_folder") or []:
        nid, new = r.get("notion_id"), r.get("new_rel")
        if held(nid):
            continue
        e = {"kind": "renamed_same_folder", "notion_id": nid, "old_rel": r.get("old_rel"), "new_rel": new}
        if r.get("override") == "existing" or (k32(nid), nfc(new)) in open_at:
            e["override_open"] = True
        out.append(e)
    for key in ("moved_other_folder", "unsupported_rename"):
        for r in scan.get(key) or []:
            nid, new = r.get("notion_id"), r.get("new_rel")
            if held(nid):
                continue
            e = {"kind": key, "notion_id": nid, "old_rel": r.get("old_rel"), "new_rel": new}
            if r.get("reason"):
                e["reason"] = r["reason"]
            out.append(e)
    return sorted(out, key=lambda e: (str(e.get("new_rel") or ""), e["kind"], str(e.get("notion_id") or "")))


def export_parents(export_db):
    """{notion_id: parent_id} of export.db objects_map (the export parent chain unregistered_renames walks for
    ancestor holds); {} when the database cannot be read (then only the object's own hold counts: stricter)."""
    try:
        _pub, objs = wb_scan.load_export_state(export_db)
    except (sqlite3.Error, OSError, ValueError):
        return {}
    return {k: v.get("parent_id") for k, v in objs.items() if isinstance(v, dict) and v.get("parent_id")}


def adoption_holds(holds, db=None):
    """L-20a (ii): the hold set a publish runs with — the adoption publish (wb_apply._adopt_holds) and a refresh
    round's publish (refresh.py _write_holds_file) — as {notion_id (32 hex, lower): hold dict}: every dict entry of
    state/auto_sync_hold.json (as given, `subtree` / `paths` kept), then pending_deletes rows with status hold and
    page_state BLOCKED / PARTIAL_CREATE, both WITH their subtree. Same precedence as _adopt_holds (setdefault: an
    auto_sync_hold entry wins for the same id). unregistered_renames() must get exactly this set, so a rename of an
    object the publisher leaves alone (SKIP_HELD) is not reported as unregistered forever. `db` None or unreadable:
    only the auto_sync_hold entries (fewer registrations = stricter, never looser)."""
    out = {}
    for k, v in (holds.items() if isinstance(holds, dict) else ()):
        if k and isinstance(k, str) and isinstance(v, dict):
            out.setdefault(nid32(k).lower(), v)
    if db is None:
        return out
    try:
        for (nid,) in db.execute("SELECT DISTINCT notion_id FROM pending_deletes WHERE status='hold'").fetchall():
            if nid:
                out.setdefault(nid32(nid).lower(), {"reason": "pending_delete", "source": "pending_delete",
                                                    "subtree": True})
        for nid, state in db.execute("SELECT notion_id, state FROM page_state WHERE state IN "
                                     "('BLOCKED','PARTIAL_CREATE')").fetchall():
            if nid:
                out.setdefault(nid32(nid).lower(), {"reason": f"page_state_{state}", "source": "page_state",
                                                    "subtree": True})
    except sqlite3.Error:
        pass
    return out


def unregistered_lines(entries, limit=20):
    """One display line per unregistered rename / move (plain text, for preview / status / CLI messages)."""
    out = []
    for e in (entries or [])[:limit]:
        out.append(f"{UNREGISTERED_KIND_TEXT.get(e.get('kind'), e.get('kind'))}：{e.get('old_rel')} → {e.get('new_rel')}"
                   + (f"（{OVERRIDE_UNPUBLISHED_TEXT}）" if e.get("override_open") else ""))
    if len(entries or []) > limit:
        out.append(f"……另有 {len(entries) - limit} 处")
    return out


# ============================================================================================ paths / small utils
class Paths:
    """Every location the write-back CLI touches, derived from one WORK root (tests use temp roots)."""

    def __init__(self, work=None, vault=None):
        self.work = os.path.abspath(work or wb_common.WORK)
        self.state = os.path.join(self.work, "state")
        self.reports = os.path.join(self.work, "reports")
        self.wb_reports = os.path.join(self.reports, "writeback")
        self.approvals = os.path.join(self.work, "approvals", "writeback")
        self.config = os.path.join(self.work, "config", "writeback.json")
        self.export_db = os.path.join(self.state, "export.db")
        self.wb_db = os.path.join(self.state, "writeback.db")
        self.staging = os.path.join(self.work, "staging")
        self.snapshots = os.path.join(self.work, "snapshots")
        self.backup = os.path.join(self.work, "backup")
        self.stop_file = os.path.join(self.state, "WRITEBACK_STOP")
        self.last_published_txt = os.path.join(self.state, "last_published_snapshot.txt")
        self.last_published_json = os.path.join(self.state, "last_published.json")
        self.holds = os.path.join(self.state, "auto_sync_hold.json")
        self.paused_runs = os.path.join(self.state, "paused_runs.json")
        self.vault = os.path.abspath(vault) if vault else None

    def plan_dir(self, plan_id):
        _check_plan_id(plan_id)
        return os.path.join(self.wb_reports, plan_id)

    def plan_json(self, plan_id):
        return os.path.join(self.plan_dir(plan_id), "plan.json")


def _check_plan_id(plan_id):
    if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
        raise WbError(f"无效的计划编号：{plan_id!r}")


def _now(ctx=None):
    n = getattr(ctx, "now", None) if ctx is not None else None
    return n if n is not None else datetime.datetime.now().astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse_iso(s):
    try:
        d = datetime.datetime.fromisoformat(s) if isinstance(s, str) and s else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.astimezone()
    return d


def new_plan_id(now=None):
    now = now or datetime.datetime.now().astimezone()
    return f"wb-{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"


def nid32(nid):
    return (nid or "").replace("-", "")


def notion_url(nid):
    return NOTION_URL.format(nid32(nid))


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _lines(text):
    t = text.split("\n")
    if t and t[-1] == "":
        t.pop()
    return t


def _blankish(line):
    return line.replace(">", "").strip() == ""


def _groups(lines):
    """Number of blank-line separated groups (paragraph count of a Markdown snippet)."""
    n, inside = 0, False
    for ln in lines:
        if _blankish(ln):
            inside = False
        elif not inside:
            n, inside = n + 1, True
    return n


def _renum1(line):
    return RENUM_RE.sub(lambda mm: mm.group(1) + "1. ", line, count=1)


def _renum(lines, mask=None):
    """Numbered-list renumbering normalised to '1. ' (Obsidian may renumber lists, M6).

    With `mask` (body indices of numbered-list MARKER lines, known exactly from block_lines rows) only those lines
    are normalised. Without it (text without rows, i.e. L): a heuristic used only to ALIGN the diff — every
    '<digits>. ' line outside fenced code and outside $$ equation blocks. Safety never rests on the heuristic:
    every comparison that decides whether a page is planned uses _same_body() with an exact mask."""
    if mask is not None:
        return [_renum1(ln) if i in mask else ln for i, ln in enumerate(lines)]
    out, fence, eq = [], None, False
    for ln in lines:
        s = ln.lstrip(" \t>")
        m = FENCE_RE.match(s)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not s[len(m.group(1)):].strip():
                fence = None
            out.append(ln)
            continue
        if eq:
            if s.strip() == "$$":
                eq = False
            out.append(ln)
            continue
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(ln)
            continue
        if s.strip() == "$$":
            eq = True
            out.append(ln)
            continue
        out.append(_renum1(ln))
    return out


def _marker_mask(rows, start):
    """Body indices of numbered_list_item marker lines (a row's first own line) from block_lines rows of a text
    whose first body line is physical line `start` (body_of)."""
    out = set()
    for r in rows or []:
        if r.get("type") == "numbered_list_item" and not r.get("zero_line") and r.get("own_start") is not None:
            out.add(int(r["own_start"]) - int(start))
    return out


def _same_body(rendered, mask, expected):
    """THE ORACLE comparison: `rendered` (with the exact marker mask of the rendering) equals `expected` line by
    line; a line may differ only in its list number, and only where the rendering has a numbered-list marker."""
    if len(rendered) != len(expected):
        return False
    for i, (a, e) in enumerate(zip(rendered, expected)):
        if a != e and not (i in mask and _renum1(a) == _renum1(e)):
            return False
    return True


def _first_difference(rendered, mask, expected):
    """(body index, rendered line | None, expected line | None) of the first line _same_body rejects."""
    for i in range(max(len(rendered), len(expected))):
        a = rendered[i] if i < len(rendered) else None
        e = expected[i] if i < len(expected) else None
        if a is None or e is None or (a != e and not (i in mask and _renum1(a) == _renum1(e))):
            return i, a, e
    return None


def body_of(text):
    """(frontmatter_or_None, body_lines, first_body_physical_line). One blank line after the frontmatter is
    skipped (the exporter writes exactly one); trailing blank lines are dropped (the exporter never ends a body
    with one, Obsidian may add one). Raises wb_common.FrontmatterError."""
    fm, _body, first = wb_common.split_note(text)
    lines = _lines(text)
    start = first  # 1-based physical line after the closing '---' (or 1)
    while start <= len(lines) and lines[start - 1].strip() == "":
        start += 1
    body = lines[start - 1:]
    while body and body[-1].strip() == "":
        body.pop()
    return fm, body, start


def _icode(s):
    """Inline code span for any text (paths, titles): never a wikilink outside code (WB 5.1a)."""
    s = wb_common_safe(str(s))
    run = max((len(m) for m in re.findall(r"`+", s)), default=0)
    ticks = "`" * (run + 1)
    pad = " " if (s.startswith("`") or s.endswith("`") or run) else ""
    return f"{ticks}{pad}{s}{pad}{ticks}"


def wb_common_safe(s):
    """No control characters in generated files (terminal and vault safety)."""
    return "".join(ch if (ch in "\t" or unicodedata.category(ch) not in ("Cc", "Zl", "Zp")) else " " for ch in s)


def _txt(s):
    """Free text in generated Markdown: no wikilink / embed syntax can appear outside code."""
    return wb_common_safe(str(s)).replace("[[", "[ [").replace("]]", "] ]")


def _fence(lines):
    body = "\n".join(wb_common_safe(x) for x in lines)
    run = max((len(m) for m in re.findall(r"`+", body)), default=0)
    f = "`" * max(3, run + 1)
    return [f + "text"] + body.split("\n") + [f] if body != "" else [f + "text", f]


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


# ============================================================================================ live reads (R)
class LiveReader:
    """Read-only access to live Notion through the injected ReadOnlyClient, with caching and a request count."""

    def __init__(self, client):
        self.c = client
        self.requests = 0
        self._pages, self._trees, self._ds, self._db = {}, {}, {}, {}
        self._user_types = {}

    def user_type(self, uid):
        """'person' / 'bot' / None of a user id (r1-user-journey-09: page last_edited_by is a partial user without
        'type'); from the members() listing, else one GET users/{id}; None when it cannot be read (403 ...)."""
        uid = str(uid or "")
        if not uid:
            return None
        if uid not in self._user_types:
            try:
                u = self.c.request("GET", f"users/{uid}")
                self._req()
                self._user_types[uid] = (u or {}).get("type") if isinstance(u, dict) else None
            except Exception:  # noqa: BLE001 - no user-info capability, network ...
                self._user_types[uid] = None
        return self._user_types[uid]

    def _req(self, n=1):
        self.requests += n

    def page(self, pid):
        """Live page object (properties completed). Cached for the planning run."""
        if pid not in self._pages:
            self._req()
            p = self.c.retrieve_page(pid)
            self._complete_properties(pid, p)
            self._pages[pid] = p
        return self._pages[pid]

    def _complete_properties(self, pid, data):
        """Same completion as nmig.rest_inventory._complete_properties (snapshot JSON == live JSON, D-10)."""
        for _name, prop in (data.get("properties") or {}).items():
            if isinstance(prop, dict) and prop.get("has_more") and prop.get("id"):
                self._req()
                full = self.c.retrieve_page_property(pid, prop["id"])
                items = full.get("results", [])
                t = prop.get("type")
                if t == "relation":
                    prop["relation"] = [{"id": it["relation"]["id"]} for it in items if it.get("relation")]
                elif t == "people":
                    prop["people"] = [it["people"] for it in items if it.get("people")]
                elif t in ("rich_text", "title"):
                    prop[t] = [it[t] for it in items if it.get(t)]
                prop["has_more"] = False
                prop["_completed_via_property_item"] = len(items)

    def children(self, bid):
        kids = self.c.list_children(bid)
        self._req(max(1, math.ceil(len(kids) / 100)))
        return kids

    def tree(self, pid):
        """{parent_id: [block json]} for the page's own block tree (children of child_page / child_database /
        synced_block are not part of it, D-10)."""
        if pid not in self._trees:
            out = {}

            def walk(parent):
                kids = self.children(parent)
                out[parent] = kids
                for b in kids:
                    if b.get("has_children") and b.get("type") not in ("child_page", "child_database", "synced_block"):
                        walk(b["id"])
            walk(pid)
            self._trees[pid] = out
        return self._trees[pid]

    def data_source(self, dsid):
        if dsid not in self._ds:
            self._req()
            self._ds[dsid] = self.c.retrieve_data_source(dsid)
        return self._ds[dsid]

    def database(self, did):
        if did not in self._db:
            self._req()
            self._db[did] = self.c.retrieve_database(did)
        return self._db[did]

    def members(self):
        """(count of type==person users | None, error). WB 9.2: names are never kept."""
        n = 0
        try:
            for u in self.c.paginate("GET", "users"):
                if isinstance(u, dict) and u.get("id"):
                    self._user_types[str(u["id"])] = u.get("type")     # types only, never names (WB 9.2)
                if isinstance(u, dict) and u.get("type") == "person":
                    n += 1
            self._req()
        except Exception as exc:  # noqa: BLE001 - 403 without the user-info capability, network ...
            return None, f"{type(exc).__name__}: {getattr(exc, 'status', '')}".strip()
        return n, None


def _api_status(exc):
    return getattr(exc, "status", None)


def page_sha_live(page, tree, page_id):
    return wb_norm.page_json_sha(page, wb_norm.page_blocks_dfs(lambda p: tree.get(p), page_id))


def _parent_map(tree):
    out = {}
    for parent, kids in tree.items():
        for b in kids:
            out[b["id"]] = parent
    return out


def _title_prop(page):
    for name, p in (page.get("properties") or {}).items():
        if isinstance(p, dict) and p.get("type") == "title":
            return name, p.get("title") or []
    return None, []


def _plain(rich):
    return "".join((r.get("plain_text") if r.get("plain_text") is not None else
                    ((r.get("text") or {}).get("content") or "")) for r in rich or [] if isinstance(r, dict))


def _single_plain(rich):
    """True when a rich_text array is at most one plain text segment (no annotations, link, mention)."""
    if not rich:
        return True
    if len(rich) != 1:
        return False
    r = rich[0]
    if r.get("type", "text") != "text":
        return False
    ann = r.get("annotations") or {}
    if any(ann.get(k) for k in ("bold", "italic", "strikethrough", "underline", "code")):
        return False
    if ann.get("color") not in (None, "default"):
        return False
    if r.get("href") or ((r.get("text") or {}).get("link")):
        return False
    return True


def _canon_title(rich):
    import md2blocks as MB
    return MB.canon_rich_text(rich or [])


# ============================================================================================ obsidian / sync settling
def obsidian_status():
    """{"running": bool, "started_at": iso|None}: a local Obsidian process (WB 4.2 SYNC_SETTLING)."""
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            btime = next(int(x.split()[1]) for x in fh if x.startswith("btime"))
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError):
        btime, hz = None, 100
    best = None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/comm", encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip().lower()
            if comm != "obsidian":
                continue
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                cmd = fh.read().split(b"\0")
            if any(a.startswith(b"--type=") for a in cmd):
                continue  # electron helper processes
            with open(f"/proc/{d}/stat", encoding="ascii", errors="replace") as fh:
                st = fh.read().rsplit(")", 1)[1].split()
            start = int(st[19])
        except (OSError, IndexError, ValueError):
            continue
        if best is None or start < best:
            best = start
    if best is None:
        return {"running": False, "started_at": None}
    started = None
    if btime is not None:
        started = _iso(datetime.datetime.fromtimestamp(btime + best / hz).astimezone())
    return {"running": True, "started_at": started}


def burst_files(mtimes, window=120, threshold=20):
    """Set of rel paths whose mtime falls in a `window`-second window holding >= threshold files."""
    items = sorted(mtimes.items(), key=lambda kv: kv[1])
    out = set()
    if threshold <= 0:
        return out
    j = 0
    for i in range(len(items)):
        while items[i][1] - items[j][1] > window:
            j += 1
        if i - j + 1 >= threshold:
            out.update(k for k, _ in items[j:i + 1])
    return out


# ============================================================================================ context
def strict_stage(ctx):
    """Stage-2 rules (WB 2.3 leaf pages only, 5.3.3 new-page limits) apply in every mode below `full`. A plan
    built in mode `plan` therefore already carries them, and apply additionally requires plan.mode == the current
    effective mode (writeback.py), so a plan can never be executed under rules weaker than the ones it was
    checked with."""
    return getattr(ctx, "mode", None) != "full"


class PlanContext:
    """Everything one planning run needs. `read_client`: a ReadOnlyClient (tests: the fake server)."""

    def __init__(self, paths, cfg, db, *, read_client=None, mode=None, now=None, obsidian=None, snapshot=None,
                 attachments_json=None, overrides=None, exporter=None, scan=None):
        self.paths = paths
        self.cfg = cfg
        self.db = db
        self.reader = LiveReader(read_client) if read_client is not None else None
        self.mode = mode or wb_config.effective_mode(cfg, db)
        self.now = now
        self._obsidian = obsidian
        self._snapshot = snapshot
        self._attachments = attachments_json
        self._overrides = overrides
        self._ex = exporter
        self._scan = scan
        self._chosen = None
        self.warnings = []

    # ------------------------------------------------------------ inputs
    def last_published(self):
        return _read_json(self.paths.last_published_json, {}) or {}

    def snapshot(self):
        """The snapshot the vault was LAST PUBLISHED from (r1-contract-consistency-03, L-13): the refresh's
        state/last_published_snapshot.txt, or -- when a write-back adoption published later -- the adopted
        snapshots/wb/ snapshot recorded by wb_refetch.record_adopted (wb_refetch.choose_base decides; a missing
        chosen snapshot fails closed). An explicit --snapshot wins."""
        if self._snapshot:
            return os.path.abspath(self._snapshot)
        if self._chosen is None:
            self._chosen = self._adopted_snapshot() or self.refresh_snapshot()
        return self._chosen

    def _adopted_snapshot(self):
        import wb_refetch
        try:
            ad = wb_refetch.read_adopted(state_dir=self.paths.state)
        except wb_refetch.RefetchError as exc:
            raise WbError(f"state/last_adopted_wb_snapshot.json 无法读取（{exc}）；请先运行 {cli_command('refresh', '--mode', 'incremental')}") from None
        if not ad:
            return None
        try:
            chosen = wb_refetch.choose_base(state_dir=self.paths.state)
        except wb_refetch.RefetchError as exc:
            raise WbError(f"上次发布所依据的快照不可用（{exc}）；请先运行 {cli_command('refresh', '--mode', 'incremental')}") from None
        return chosen if os.path.abspath(chosen) == os.path.abspath(ad["snapshot"]) else None

    def refresh_snapshot(self):
        """The last REFRESH-published snapshot (state/last_published_snapshot.txt)."""
        try:
            with open(self.paths.last_published_txt, encoding="utf-8") as fh:
                s = fh.read().strip()
        except OSError:
            s = ""
        if s and not os.path.isabs(s):
            s = os.path.join(self.paths.work, s)
        if not s or not os.path.exists(s):
            raise WbError(f"没有可用的已发布快照（state/last_published_snapshot.txt）；请先运行 {cli_command('refresh', '--mode', 'incremental')}")
        return s

    def snapshot_time(self, refresh_only=False):
        """finished_at of the planning snapshot; `refresh_only`: of the last refresh-published one (deletion
        freshness, WB 6.4: an adopted write-back snapshot refetched only the plan's pages)."""
        try:
            snap = self.snapshot() if not refresh_only or self._snapshot else self.refresh_snapshot()
            con, _ = wb_common.open_snapshot_reader(snap)
            try:
                row = con.execute("SELECT value FROM meta WHERE key='finished_at'").fetchone()
            finally:
                con.close()
            return row[0] if row else None
        except (sqlite3.Error, WbError):
            return None

    def attachments_json(self):
        if self._attachments:
            return self._attachments
        run = self.last_published().get("run_id")
        if run:
            p = os.path.join(self.paths.staging, run, "attachments.json")
            if os.path.isfile(p):
                return p
        return None

    def overrides(self):
        if self._overrides is not None:
            return self._overrides
        run = self.last_published().get("run_id")
        if run:
            d = _read_json(os.path.join(self.paths.reports, run, "overrides.json"))
            if isinstance(d, dict):
                return d
        out = {}
        try:
            for nid, rel in self.db.execute("SELECT notion_id, local_rel FROM name_overrides WHERE status='open'"):
                out[nid] = rel
        except sqlite3.Error:
            pass
        return out

    @property
    def ex(self):
        if self._ex is None:
            import export_obsidian as EO
            self._ex = EO.Exporter.for_planning(self.snapshot(), self.paths.export_db, overrides=self.overrides() or None,
                                                attachments_json=self.attachments_json())
        return self._ex

    def scan(self):
        if self._scan is None:
            self._scan = wb_scan.scan(self.paths.vault, self.paths.export_db,
                                      self.paths.wb_db if os.path.exists(self.paths.wb_db) else None, self.cfg)
        return self._scan

    def obsidian(self):
        if self._obsidian is None:
            self._obsidian = obsidian_status()
        return self._obsidian

    def need_reader(self):
        if self.reader is None:
            raise WbError("没有读客户端（需要读取 Notion 当前内容）")
        return self.reader

    # ------------------------------------------------------------ scope
    def scope_pages(self):
        s = set(self.cfg.get("whitelist_page_ids") or [])
        if self.cfg.get("test_page_id"):
            s.add(self.cfg["test_page_id"])
        return s

    def scope_dbs(self):
        return set(self.cfg.get("whitelist_database_ids") or [])


# ============================================================================================ block diff engine
class _Op(dict):
    """A planned op; internal keys start with '_' and are dropped when the plan is serialised."""


def _strip_internal(op):
    return {k: v for k, v in op.items() if not k.startswith("_")}


class PagePlanner:
    """Body diff (B text + B block_lines rows vs L) -> ops, validated by the render oracle on an overlay of R."""

    def __init__(self, ctx, node, b_text, rows, l_body, tree, page_id, *, base_label="B"):
        import md2blocks as MB
        self.MB = MB
        self.ctx = ctx
        self.ex = ctx.ex
        self.node = node
        self.page_id = page_id
        self.tree = tree                         # live R {parent: [blocks]}
        self.parent_of = _parent_map(tree)
        self.r_by_id = {b["id"]: b for kids in tree.values() for b in kids}
        _fm, self.Bb, self.bstart = body_of(b_text)
        self.Lb = list(l_body)
        self.rows = [r for r in rows]
        self.blocks = [r for r in self.rows if r.get("type") != "gap"]
        self.row_by_bid = {}
        for r in self.blocks:
            if r.get("block_id") and r["block_id"] not in self.row_by_bid:
                self.row_by_bid[r["block_id"]] = r
        self.owner = [None] * len(self.Bb)
        for r in self.rows:
            if r.get("zero_line"):
                continue
            s, e = r.get("own_start"), r.get("own_end")
            if s is None or e is None:
                continue
            for ln in range(s, e + 1):
                i = ln - self.bstart
                if 0 <= i < len(self.Bb):
                    self.owner[i] = r
        self.bmask = _marker_mask(self.rows, self.bstart)
        self.base_label = base_label
        self._n = 0
        self.last_refusal = None
        self.cur_group = None

    # ------------------------------------------------------------ helpers
    def _bi(self, phys):
        return phys - self.bstart

    def _own_range(self, r):
        """(s, e) body indices of a block row's own lines (inclusive), or None."""
        if r.get("zero_line") or r.get("own_start") is None:
            return None
        return self._bi(r["own_start"]), self._bi(r["own_end"])

    def _subtree_range(self, r):
        return self._bi(r["line_start"]), self._bi(r["line_end"])

    def _render(self, overlay, record=False):
        text, _rows = self.ex.render_page(self.node, blocks=overlay, record=record, sandbox=True)
        _fm, body, _s = body_of(text)
        return body

    def _render_m(self, overlay):
        """(body lines, exact numbered-marker mask) of a recorded sandbox rendering."""
        text, rows = self.ex.render_page(self.node, blocks=overlay, record=True, sandbox=True)
        _fm, body, s = body_of(text)
        return body, _marker_mask(rows, s)

    def _base_overlay(self):
        return {k: list(v) for k, v in self.tree.items()}

    def _apply(self, ops):
        MB = self.MB
        ov = self._base_overlay()

        def set_has_children(bid):
            par = self.parent_of.get(bid)
            if par is None or par not in ov:
                return
            lst = ov[par]
            for i, b in enumerate(lst):
                if b.get("id") == bid and not b.get("has_children"):
                    nb = dict(b)
                    nb["has_children"] = True
                    lst[i] = nb
        for op in ops:
            kind = op["op"]
            if kind == "UPDATE":
                lst = ov[op["parent_id"]]
                for i, b in enumerate(lst):
                    if b.get("id") == op["target_id"]:
                        lst[i] = MB.apply_update(b, op["request"])
                        break
                else:
                    raise _NoMatch("update target not in R")
            elif kind == "TRASH_BLOCK":
                lst = ov[op["parent_id"]]
                ov[op["parent_id"]] = [b for b in lst if b.get("id") != op["target_id"]]
            elif kind == "APPEND":
                self._n += 1
                parent = op["parent_id"]
                sub, _ids = MB.snapshot_blocks(op["children"], parent, f"wbplan-new{self._n}")
                new = sub.pop(parent)
                lst = list(ov.get(parent) if parent in ov else (self.ex.blocks.get(parent) or []))
                pos = op.get("position")
                if pos and pos.get("type") == "after_block":
                    aid = pos["after_block"]["id"]
                    idx = next((i for i, b in enumerate(lst) if b.get("id") == aid), None)
                    if idx is None:
                        raise _NoMatch("anchor not in R")
                    lst[idx + 1:idx + 1] = new
                elif pos and pos.get("type") == "start":
                    lst[0:0] = new
                else:
                    lst.extend(new)
                ov[parent] = lst
                ov.update(sub)
                if parent != self.page_id:
                    set_has_children(parent)
        return ov

    def _check(self, ops, expected):
        body, mask = self._render_m(self._apply(ops))
        return _same_body(body, mask, expected)

    # ------------------------------------------------------------ ops
    def _r_block(self, row):
        b = self.r_by_id.get(row.get("block_id"))
        if b is None:
            raise self.MB.Refused("UNSUPPORTED_REGION", "块在 Notion 当前内容中不存在")
        return b

    def _check_writable(self, row):
        """X rows are final (B's structural klass, incl. own_line_lost); nothing inside a column or a synced
        subtree (or rendered from another page) is writable (WB 4.3, D-11)."""
        if row.get("klass") == "X":
            raise self.MB.Refused("UNSUPPORTED_REGION", f"{x_reason_text(row.get('x_reason'))}（X:{row.get('x_reason')}；"
                                  "只读块：含提及、颜色或 Notion 链接等，不能从 Obsidian 修改或删除，请在 Notion 中手工编辑）")
        par = row.get("parent_block_id")
        seen = 0
        while par and seen < 64:
            pr = self.row_by_bid.get(par)
            if pr is None:
                break
            if pr.get("x_reason") in ("synced", "owner") or pr.get("type") in ("column", "column_list", "synced_block"):
                raise self.MB.Refused("UNSUPPORTED_REGION", f"inside:{pr.get('type')}")
            par = pr.get("parent_block_id")
            seen += 1

    def _list_num(self, line, prefix):
        m = re.match(r"(\d{1,9})\. ", line[len(prefix):] if line.startswith(prefix) else line.lstrip())
        return int(m.group(1)) if m else 1

    def op_update(self, row, new_own, l_span):
        MB = self.MB
        self._check_writable(row)
        rb = self._r_block(row)
        body, normalized = MB.rebuild_block(self.ex, rb, new_own, row.get("prefix") or "")
        after = MB.apply_update(rb, body)
        if MB.canon_writable(after) == MB.canon_writable(rb):
            return []  # only a renumbering / no effective change
        t = rb["type"]
        op = _Op(op="UPDATE", target_id=rb["id"], parent_id=self.parent_of.get(rb["id"], self.page_id), block_type=t,
                 before_norm_sha=wb_norm.block_json_sha(rb), request=body, expected_after=MB.canon_writable(after),
                 normalized=bool(normalized), old_md=self._own_lines(row), new_md=list(new_own))
        op["_lspan"] = l_span
        if normalized:
            num = self._list_num(new_own[0], row.get("prefix") or "") if t == "numbered_list_item" and new_own else 1
            op["_canon"] = MB.render_own_lines(self.ex, after, row.get("prefix") or "", num)
        return [op]

    def _own_lines(self, row):
        rng = self._own_range(row)
        return list(self.Bb[rng[0]:rng[1] + 1]) if rng else []

    def op_append(self, target, lines, l_span):
        MB = self.MB
        lo, hi = 0, len(lines)
        while lo < hi and _blankish(lines[lo]):
            lo += 1
        while hi > lo and _blankish(lines[hi - 1]):
            hi -= 1
        content = lines[lo:hi]
        if not content:
            raise MB.Refused("BLANK_LINES", HINTS["BLANK_LINES"])
        prefix = target["prefix"]
        if any(not _blankish(ln) and not ln.startswith(prefix) for ln in content):
            raise _NoMatch("prefix")
        req, normalized = MB.parse_new_blocks(self.ex, content, prefix)
        if not req:
            raise MB.Refused("BLANK_LINES", HINTS["BLANK_LINES"])
        overlay, _ = MB.snapshot_blocks(req, "wbplan-canon-root", "wbplan-canon")
        exp_canon = [MB.canon_writable(b) for b in overlay["wbplan-canon-root"]]
        op = _Op(op="APPEND", parent_id=target["container"], position=target["position"], anchor_id=target["anchor"],
                 anchor_next_id=target["next"], children=req, new_blocks=MB.block_count(req),
                 expected_after=exp_canon, normalized=bool(normalized), new_md=list(content))
        op["_lspan"] = (l_span[0] + lo, l_span[0] + hi)
        if normalized:
            op["_canon"] = self._render_new(overlay, prefix)
        return [op]

    def _render_new(self, overlay, prefix):
        from collections import ChainMap, Counter, defaultdict
        ex = self.ex
        saved = {k: getattr(ex, k) for k in ("blocks", "report", "lists", "_rec", "_sandbox", "_list_num", "_blank_count")}
        try:
            ex.blocks = ChainMap(overlay, saved["blocks"])
            ex.report, ex.lists, ex._rec, ex._sandbox = defaultdict(Counter), defaultdict(list), None, True
            return [str(x) for x in ex.render_blocks("wbplan-canon-root", "General", prefix)]
        finally:
            for k, v in saved.items():
                setattr(ex, k, v)

    def op_trash(self, rows):
        """Post-order TRASH ops for fully deleted block rows (and their descendants)."""
        MB = self.MB
        sel = {id(r) for r in rows}
        out = []
        for r in sorted(rows, key=lambda r: (-(r.get("depth") or 0), r["line_start"])):
            if r.get("zero_line"):
                continue
            t = r.get("type")
            if t not in MB.R_TYPES or t in ("child_page", "child_database", "link_to_page", "synced_block"):
                raise MB.Refused("UNSUPPORTED_REGION", f"不能删除 {t} 块；请在 Notion 中手工删除")
            if r.get("klass") == "X":
                raise MB.Refused("UNSUPPORTED_REGION", f"X:{r.get('x_reason')}（只读块：含提及、颜色或 Notion 链接等，不能从 Obsidian 修改或删除，请在 Notion 中手工编辑）")
            rb = self._r_block(r)
            kids = [k for k in self.tree.get(rb["id"], [])]
            for k in kids:
                kr = self.row_by_bid.get(k["id"])
                if kr is None or id(kr) not in sel:
                    raise MB.Refused("UNSUPPORTED_REGION", "被删除的块还有未删除的子块")
            self._check_writable(r)
            out.append(_Op(op="TRASH_BLOCK", target_id=rb["id"], parent_id=self.parent_of.get(rb["id"], self.page_id),
                           block_type=t, before_norm_sha=wb_norm.block_json_sha(rb),
                           old_md=self._own_lines(r), has_children=bool(rb.get("has_children"))))
        _ = sel
        return out

    # ------------------------------------------------------------ append targets
    def _child_prefix(self, row):
        kids = [r for r in self.blocks if r.get("parent_block_id") == row.get("block_id") and r.get("prefix") is not None]
        if kids:
            return kids[0]["prefix"]
        t = row.get("type")
        p = row.get("prefix") or ""
        if t == "bulleted_list_item":
            return p + "    "
        if t == "numbered_list_item":
            rng = self._own_range(row)
            num = self._list_num(self.Bb[rng[0]], p) if rng else 1
            return p + " " * max(4, len(f"{num}. "))
        if t in ("quote", "callout"):
            return p + "> "
        return None

    def append_targets(self, p):
        """Placement candidates for new lines inserted before body index p (longest prefix first)."""
        phys = self.bstart + p
        out = []

        def kids_of(container):
            return self.tree.get(container, [])

        def anchor_in(container):
            kids = kids_of(container)
            anchor, nxt = None, None
            for i, k in enumerate(kids):
                r = self.row_by_bid.get(k["id"])
                if r is None:
                    continue
                end = r["line_end"] if r.get("line_end") is not None else r.get("line_start")
                start = r.get("line_start")
                if r.get("zero_line"):
                    if end is not None and end < phys:
                        anchor, nxt = k["id"], (kids[i + 1]["id"] if i + 1 < len(kids) else None)
                    continue
                if end is not None and end < phys:
                    anchor, nxt = k["id"], (kids[i + 1]["id"] if i + 1 < len(kids) else None)
                elif start is not None and start >= phys:
                    if anchor is None:
                        nxt = k["id"]
                    break
            if anchor is None:
                nxt = kids[0]["id"] if kids else None
            if anchor is not None:
                pos = {"type": "after_block", "after_block": {"id": anchor}}
            elif kids:
                pos = {"type": "start"}
            else:
                pos = None
            return anchor, nxt, pos

        a, n, pos = anchor_in(self.page_id)
        out.append({"container": self.page_id, "prefix": "", "anchor": a, "next": n, "position": pos, "depth": 0})
        for r in self.blocks:
            if r.get("zero_line") or not r.get("block_id") or r.get("type") not in (
                    "bulleted_list_item", "numbered_list_item", "quote", "callout"):
                continue
            oe = r["own_end"] if r.get("own_end") is not None else r["line_start"] - 1
            if not (oe < phys <= r["line_end"] + 1):
                continue
            try:
                self._check_writable_container(r)
            except self.MB.Refused:
                continue
            cp = self._child_prefix(r)
            if cp is None:
                continue
            a, n, pos = anchor_in(r["block_id"])
            out.append({"container": r["block_id"], "prefix": cp, "anchor": a, "next": n, "position": pos,
                        "depth": (r.get("depth") or 0) + 1})
        out.sort(key=lambda t: (-len(t["prefix"]), -t["depth"]))
        return out

    def _check_writable_container(self, r):
        if r.get("x_reason") in ("synced", "owner"):
            raise self.MB.Refused("UNSUPPORTED_REGION", "synced")
        self._check_writable({"klass": "R", "parent_block_id": r.get("parent_block_id")})

    # ------------------------------------------------------------ hunks
    def plan(self):
        """-> (ops, info). Raises md2blocks.Refused (reason/hint) when a hunk cannot be expressed."""
        MB = self.MB
        Bn, Ln = _renum(self.Bb, self.bmask), _renum(self.Lb)
        sm = difflib.SequenceMatcher(None, Bn, Ln, autojunk=False)
        regions = [op for op in sm.get_opcodes() if op[0] != "equal"]
        if not regions:
            # the caller only plans pages whose B and L differ beyond numbered-list renumbering (exact B mask);
            # a difference the alignment heuristic hides must never become "no change"
            if _same_body(self.Bb, self.bmask, self.Lb):
                return [], {"groups": 0, "renumber_only": True}
            raise MB.Refused("ORACLE_MISMATCH", HINTS["ORACLE_MISMATCH"])
        # rows touched per region (blocks only; interior inserts touch their block)
        groups = []
        for tag, i1, i2, j1, j2 in regions:
            touched = []
            for i in range(i1, i2):
                r = self.owner[i]
                if r is not None and r.get("type") != "gap" and all(r is not x for x in touched):
                    touched.append(r)
            if tag == "insert" and 0 < i1 < len(self.Bb):
                a, b = self.owner[i1 - 1], self.owner[i1]
                if a is not None and a is b and a.get("type") != "gap":
                    touched.append(a)
            g = {"regions": [(tag, i1, i2, j1, j2)], "blocks": touched}
            if groups and any(any(x is y for y in groups[-1]["blocks"]) for x in touched):
                groups[-1]["regions"].append((tag, i1, i2, j1, j2))
                for x in touched:
                    if all(x is not y for y in groups[-1]["blocks"]):
                        groups[-1]["blocks"].append(x)
            else:
                groups.append(g)
        all_ops = []
        for g in groups:
            I1 = min(r[1] for r in g["regions"])
            I2 = max(r[2] for r in g["regions"])
            J1 = min(r[3] for r in g["regions"])
            J2 = max(r[4] for r in g["regions"])
            self.cur_group = (I1, I2, J1, J2)
            if all(_blankish(x) for x in self.Bb[I1:I2]) and all(_blankish(x) for x in self.Lb[J1:J2]):
                raise MB.Refused("BLANK_LINES", HINTS["BLANK_LINES"])
            ops = self._interpret(g, I1, I2, J1, J2)
            all_ops.extend(ops)
        self.cur_group = None
        self._check_conflicts(all_ops)
        ordered = ([o for o in all_ops if o["op"] == "UPDATE"] + [o for o in all_ops if o["op"] == "APPEND"]
                   + [o for o in all_ops if o["op"] == "TRASH_BLOCK"])
        expected = self._expected(ordered)
        body, mask = self._render_m(self._apply(ordered))
        if not _same_body(body, mask, expected):
            d = _first_difference(body, mask, expected)
            if d is not None:
                self.cur_group = (d[0], d[0] + 1, d[0], d[0] + 1)
            raise MB.Refused("ORACLE_MISMATCH", HINTS["ORACLE_MISMATCH"])
        return ordered, {"groups": len(groups)}

    def where(self):
        """Location of the hunk being interpreted when a refusal was raised: (B lines, L lines) as 1-based file
        line ranges plus the hunk text, for the refusal reason a person can act on."""
        if self.cur_group is None:
            return None
        I1, I2, J1, J2 = self.cur_group
        lstart = getattr(self, "lstart", None)
        out = {"b_lines": [self.bstart + I1, self.bstart + max(I1, I2 - 1)],
               "b_text": [x for x in self.Bb[I1:I2]][:6], "l_text": [x for x in self.Lb[J1:J2]][:6]}
        if lstart is not None:
            out["l_lines"] = [lstart + J1, lstart + max(J1, J2 - 1)]
        return out

    def _check_conflicts(self, ops):
        seen = set()
        for o in ops:
            if o["op"] in ("UPDATE", "TRASH_BLOCK"):
                if o["target_id"] in seen:
                    raise self.MB.Refused("REPARSE_MISMATCH", "同一块有多处互相冲突的改动")
                seen.add(o["target_id"])
        anchors = collections.Counter((o["parent_id"], json.dumps(o.get("position"), sort_keys=True))
                                      for o in ops if o["op"] == "APPEND")
        if any(v > 1 for v in anchors.values()):
            raise self.MB.Refused("REPARSE_MISMATCH", "同一位置有多处新增内容；请合并为一处")

    def _expected(self, ops, base=None, span=None):
        """L lines (or the [J1,J2) span) with normalized ops' L spans replaced by their canonical rendering."""
        lines = list(self.Lb)
        subs = sorted([o for o in ops if o.get("_canon") is not None], key=lambda o: -o["_lspan"][0])
        for o in subs:
            a, b = o["_lspan"]
            lines[a:b] = o["_canon"]
        return lines

    def _expected_group(self, ops, bx1, bx2, lx1, lx2):
        seg = list(self.Lb[lx1:lx2])
        subs = sorted([o for o in ops if o.get("_canon") is not None], key=lambda o: -o["_lspan"][0])
        for o in subs:
            a, b = o["_lspan"]
            if a < lx1 or b > lx2:
                raise _NoMatch("normalized span outside the group")
            seg[a - lx1:b - lx1] = o["_canon"]
        return list(self.Bb[:bx1]) + seg + list(self.Bb[bx2:])

    def _try(self, make, bx1, bx2, lx1, lx2):
        """Run one interpretation and the per-group oracle; return ops or raise _NoMatch / Refused."""
        ops = make()
        exp = self._expected_group(ops, bx1, bx2, lx1, lx2)
        if not self._check(ops, exp):
            raise _NoMatch("oracle")
        return ops

    def _interpret(self, g, I1, I2, J1, J2):
        MB = self.MB
        blocks = g["blocks"]
        cands = []
        refusal = None
        if len(blocks) == 1:
            r = blocks[0]
            rng = self._own_range(r)
            if rng and rng[0] <= I1 and I2 <= rng[1] + 1:
                s, e = rng
                new_own = list(self.Bb[s:I1]) + list(self.Lb[J1:J2]) + list(self.Bb[I2:e + 1])
                lspan = (J1 - (I1 - s), J2 + (e + 1 - I2))
                bx = (s, e + 1)
                edit = ("edit", lambda r=r, new_own=new_own, lspan=lspan: self.op_update(r, new_own, lspan), bx, lspan)
                splits = []
                extra = (J2 - J1) - (I2 - I1)
                if extra > 0 and len(new_own) > 1:
                    blank_k = [k for k in range(1, len(new_own)) if _blankish(new_own[k]) or _blankish(new_own[k - 1])]
                    other_k = [k for k in range(1, len(new_own)) if k not in blank_k]
                    for ks in (blank_k, other_k):
                        for k in sorted(ks, reverse=True)[:12]:
                            own, rest = new_own[:k], new_own[k:]
                            splits.append(("edit+append", lambda r=r, own=own, rest=rest, ls=lspan, k=k:
                                           self.op_update(r, own, (ls[0], ls[0] + k))
                                           + self.op_append(self._sibling_target(r), rest, (ls[0] + k, ls[1])), bx,
                                           lspan))
                        for k in sorted(ks)[:12]:
                            rest, own = new_own[:k], new_own[k:]
                            splits.append(("append+edit", lambda r=r, own=own, rest=rest, ls=lspan, k=k:
                                           self.op_append(self._before_target(r), rest, (ls[0], ls[0] + k))
                                           + self.op_update(r, own, (ls[0] + k, ls[1])), bx, lspan))
                # more blank-separated paragraphs than the block had: the user added blocks -> try splits first
                if _groups(new_own) > _groups(self.Bb[s:e + 1]):
                    cands += splits + [edit]
                else:
                    cands += [edit] + splits
        if not blocks:
            # pure insertion at a boundary, or a change of separator (gap) lines only
            for t in self.append_targets(I1):
                cands.append(("append", lambda t=t: self.op_append(t, list(self.Lb[J1:J2]), (J1, J2)), (I1, I2),
                              (J1, J2)))
            if I1 == I2 and I1 > 0:
                prev = self.owner[I1 - 1]
                rng = self._own_range(prev) if prev is not None and prev.get("type") != "gap" else None
                if rng and rng[1] == I1 - 1:
                    s, e = rng
                    new_own = list(self.Bb[s:I1]) + list(self.Lb[J1:J2])
                    lspan = (J1 - (I1 - s), J2)
                    cands.append(("extend-prev", lambda prev=prev, new_own=new_own, lspan=lspan:
                                  self.op_update(prev, new_own, lspan), (s, I1), lspan))
            if I1 == I2 and I1 < len(self.Bb):
                nxt = self.owner[I1]
                rng = self._own_range(nxt) if nxt is not None and nxt.get("type") != "gap" else None
                if rng and rng[0] == I1:
                    s, e = rng
                    new_own = list(self.Lb[J1:J2]) + list(self.Bb[I1:e + 1])
                    lspan = (J1, J2 + (e + 1 - I1))
                    cands.append(("extend-next", lambda nxt=nxt, new_own=new_own, lspan=lspan:
                                  self.op_update(nxt, new_own, lspan), (I1, e + 1), lspan))
        else:
            full = all(self._covered(r, I1, I2) for r in blocks)
            if full and J1 == J2:
                tops = self._with_descendants(blocks)
                if tops is not None:
                    cands.append(("delete", lambda tops=tops: self.op_trash(tops), (I1, I2), (J1, J2)))
            if len(blocks) > 1 or (full and J1 != J2):
                for name, make, bx, lx in self._multi(blocks, I1, I2, J1, J2):
                    cands.append((name, make, bx, lx))
        refusals = []
        for name, make, bx, lx in cands:
            bx1, bx2 = min(bx[0], I1), max(bx[1], I2)
            lx1, lx2 = J1 - (I1 - bx1), J2 + (bx2 - I2)
            try:
                return self._try(make, bx1, bx2, lx1, lx2)
            except MB.Refused as e:
                refusals.append(e)
            except _NoMatch:
                continue
            except Exception as e:  # noqa: BLE001 - renderer refusal (RenderRefused) etc.
                refusals.append(MB.Refused("REPARSE_MISMATCH", f"{type(e).__name__}: {str(e)[:200]}"))
        if refusals:
            raise _most_specific(refusals, MB)
        raise MB.Refused("ORACLE_MISMATCH", HINTS["ORACLE_MISMATCH"])

    def _covered(self, r, I1, I2):
        a, b = self._subtree_range(r)
        rng = self._own_range(r)
        if rng is None:
            return False
        return I1 <= a and b < I2

    def _with_descendants(self, blocks):
        ids = {r.get("block_id") for r in blocks}
        out = list(blocks)
        changed = True
        while changed:
            changed = False
            for r in self.blocks:
                if r.get("parent_block_id") in ids and r.get("block_id") not in ids:
                    ids.add(r.get("block_id"))
                    out.append(r)
                    changed = True
        return out

    def _sibling_target(self, r):
        par = r.get("parent_block_id") or self.page_id
        kids = self.tree.get(par, [])
        idx = next((i for i, k in enumerate(kids) if k["id"] == r.get("block_id")), None)
        if idx is None:
            raise _NoMatch("sibling")
        nxt = kids[idx + 1]["id"] if idx + 1 < len(kids) else None
        return {"container": par, "prefix": r.get("prefix") or "", "anchor": r["block_id"], "next": nxt,
                "position": {"type": "after_block", "after_block": {"id": r["block_id"]}}, "depth": r.get("depth") or 0}

    def _before_target(self, r):
        par = r.get("parent_block_id") or self.page_id
        kids = self.tree.get(par, [])
        idx = next((i for i, k in enumerate(kids) if k["id"] == r.get("block_id")), None)
        if idx is None:
            raise _NoMatch("sibling")
        if idx == 0:
            pos, anchor = {"type": "start"}, None
        else:
            anchor = kids[idx - 1]["id"]
            pos = {"type": "after_block", "after_block": {"id": anchor}}
        return {"container": par, "prefix": r.get("prefix") or "", "anchor": anchor, "next": r["block_id"],
                "position": pos, "depth": r.get("depth") or 0}

    def _multi(self, blocks, I1, I2, J1, J2):
        """Interpretations for a hunk touching several blocks: positional pairing, then blank-line groups."""
        rows = []
        for i in range(I1, I2):
            r = self.owner[i]
            if r is not None and all(r is not x for x in rows):
                rows.append(r)
        rngs = [(r, self._own_range(r)) for r in rows]
        if any(rg is None for _r, rg in rngs):
            return []
        ex1 = min(min(rg[0] for _r, rg in rngs), I1)
        ex2 = max(max(rg[1] for _r, rg in rngs) + 1, I2)
        lx1, lx2 = J1 - (I1 - ex1), J2 + (ex2 - I2)
        if lx1 < 0 or lx2 > len(self.Lb):
            return []
        out = []
        for r in rows:
            if r.get("type") != "gap":
                a, b = self._subtree_range(r)
                rg = self._own_range(r)
                if (a, b) != rg:  # blocks with children: not paired here
                    return out
        if (ex2 - ex1) == (lx2 - lx1):
            def positional():
                ops = []
                for r, (s, e) in rngs:
                    new = list(self.Lb[lx1 + (s - ex1):lx1 + (e + 1 - ex1)])
                    if r.get("type") == "gap":
                        if [x for x in new] != list(self.Bb[s:e + 1]):
                            raise _NoMatch("gap changed")
                        continue
                    if new != list(self.Bb[s:e + 1]):
                        ops += self.op_update(r, new, (lx1 + (s - ex1), lx1 + (e + 1 - ex1)))
                return ops
            out.append(("positional", positional, (ex1, ex2), (lx1, lx2)))

        def grouped():
            bgroups, cur = [], []
            for r, (s, e) in rngs:
                if r.get("type") == "gap":
                    if cur:
                        bgroups.append(cur)
                    cur = []
                else:
                    cur.append((r, s, e))
            if cur:
                bgroups.append(cur)
            lgroups, curl = [], None
            for k in range(lx1, lx2):
                if _blankish(self.Lb[k]):
                    if curl is not None:
                        lgroups.append(curl)
                    curl = None
                else:
                    curl = [k, k + 1] if curl is None else [curl[0], k + 1]
            if curl is not None:
                lgroups.append(curl)
            ops = []
            n = min(len(bgroups), len(lgroups))
            for gi in range(n):
                bg, (la, lb) = bgroups[gi], lgroups[gi]
                if len(bg) == 1:
                    r, s, e = bg[0]
                    new = list(self.Lb[la:lb])
                    if new != list(self.Bb[s:e + 1]):
                        ops += self.op_update(r, new, (la, lb))
                elif sum(e - s + 1 for _r, s, e in bg) == (lb - la):
                    k = la
                    for r, s, e in bg:
                        new = list(self.Lb[k:k + (e - s + 1)])
                        if new != list(self.Bb[s:e + 1]):
                            ops += self.op_update(r, new, (k, k + (e - s + 1)))
                        k += e - s + 1
                else:
                    raise _NoMatch("group shapes differ")
            if len(lgroups) > len(bgroups):
                last = bgroups[-1][-1][0] if bgroups else None
                if last is None:
                    raise _NoMatch("no anchor")
                la = lgroups[len(bgroups)][0]
                ops += self.op_append(self._sibling_target(last), list(self.Lb[la:lx2]), (la, lx2))
            elif len(bgroups) > len(lgroups):
                rest = [r for bg in bgroups[len(lgroups):] for r, _s, _e in bg]
                ops += self.op_trash(self._with_descendants(rest))
            return ops
        out.append(("grouped", grouped, (ex1, ex2), (lx1, lx2)))
        return out


# ============================================================================================ planner
@dataclasses.dataclass
class Selection:
    """apply selection (WB 10.4, R-163). Ids only; selected_items() validates them against a plan."""
    items: list = dataclasses.field(default_factory=list)          # M / N
    delete: list = dataclasses.field(default_factory=list)         # D
    delete_blocks: list = dataclasses.field(default_factory=list)  # B
    rename: list = dataclasses.field(default_factory=list)         # T
    move: list = dataclasses.field(default_factory=list)           # MV
    resolve: list = dataclasses.field(default_factory=list)        # C (resolve only; never via apply)
    all_safe: bool = False                                         # --all-safe was given (messages only)

    def all_ids(self):
        out = []
        for x in (self.items, self.delete, self.delete_blocks, self.rename, self.move, self.resolve):
            for i in x:
                if i not in out:
                    out.append(i)
        return out

    def as_dict(self):
        return {"items": list(self.items), "delete": list(self.delete), "delete_blocks": list(self.delete_blocks),
                "rename": list(self.rename), "move": list(self.move)}


def _split_ids(vals):
    out = []
    for v in vals or []:
        for x in re.split(r"[,\s，]+", str(v)):
            x = x.strip().upper()
            if x and x not in out:
                out.append(x)
    return out


_FLAG_CATS = {"items": ("M", "N"), "delete": ("D",), "delete_blocks": ("B",), "rename": ("T",), "move": ("MV",),
              "resolve": ("C",)}
_FLAG_NAMES = {"items": "--items", "delete": "--delete", "delete_blocks": "--delete-blocks", "rename": "--rename",
               "move": "--move", "resolve": "resolve"}


def _cat(item_id):
    return item_id.split("-", 1)[0]


def conflict_steps(plan_id, item_id):
    notion = cli_command("writeback", "resolve", "--plan", plan_id, "--item", item_id, "--take", "notion")
    local = cli_command("writeback", "resolve", "--plan", plan_id, "--item", item_id, "--take", "obsidian")
    return f"以 Notion 为准用 `{notion}`，以本地为准生成新计划用 `{local}`"


def selection_from_args(plan, args):
    """--items / --all-safe / --delete / --delete-all-in-plan / --delete-blocks / --rename / --move -> Selection."""
    sel = Selection()
    for field in ("items", "delete", "delete_blocks", "rename", "move"):
        ids = _split_ids(getattr(args, field, None))
        for i in ids:
            if not ITEM_ID_RE.fullmatch(i):
                raise WbError(f"无效的条目编号：{i}")
            c = _cat(i)
            if c == "C":
                raise WbError(f"{i}：冲突项不能经 apply 确认。{conflict_steps(plan.get('plan_id'), i)}")
            if c not in _FLAG_CATS[field]:
                right = next((f for f, cats in _FLAG_CATS.items() if c in cats), None)
                raise WbError(f"{i} 不能用 {_FLAG_NAMES[field]} 选择（请用 {_FLAG_NAMES.get(right, '?')}）")
        getattr(sel, field).extend(x for x in ids if x not in getattr(sel, field))
    items = plan.get("items") or []
    if getattr(args, "all_safe", False):
        sel.all_safe = True
        excluded = {e["item_id"] for e in all_safe_exclusions(plan)}
        for it in items:
            if it["category"] in ("M", "N") and it.get("executable") and it["item_id"] not in sel.items \
                    and it["item_id"] not in excluded:
                sel.items.append(it["item_id"])
    if getattr(args, "delete_all_in_plan", False):
        ds = [it for it in items if it["category"] == "D" and it.get("executable")]
        batch = [it["item_id"] for it in ds if it.get("risk") != "high"]
        limit = int((plan.get("limits") or {}).get("max_batch_deletes", 5))
        if len(batch) > limit:
            raise WbError(f"--delete-all-in-plan 一次最多 {limit} 个（本计划可成批的 D 项 {len(batch)} 个）；请逐项 --delete")
        for i in batch:
            if i not in sel.delete:
                sel.delete.append(i)
    return sel


def note_key(it):
    """Items of the same note (same Notion page, or the same new file) form one unit."""
    return it.get("notion_id") or ("path:" + (it.get("rel_path") or ""))


def _flag_for(cat):
    return next((_FLAG_NAMES[f] for f, cats in _FLAG_CATS.items() if cat in cats), "?")


def all_safe_empty_text(plan):
    """Why --all-safe selected nothing (r1-user-journey-13): it covers only independent executable M/N items."""
    parts = ["--all-safe 只包含可执行、且同一笔记没有其他项的 M/N 项"]
    others = [it for it in plan.get("items") or [] if it.get("executable") and it["category"] not in ("M", "N")]
    if others:
        parts.append("本计划的 " + "、".join(f"{it['item_id']}（用 {_flag_for(it['category'])}）" for it in others[:10]) +
                     " 须单独显式确认")
    ex = all_safe_exclusions(plan)
    if ex:
        parts.append("、".join(f"{e['item_id']}（同一笔记还有 {','.join(e['with'])}）" for e in ex[:10]) +
                     " 须与同一笔记的其他项一起显式选择")
    if not others and not ex:
        parts.append(f"本计划没有可执行的 M/N 项（原因见 {cli_command('writeback', 'show', '--plan', plan.get('plan_id'))}）")
    return "；".join(parts)


def all_safe_exclusions(plan):
    """M/N items --all-safe leaves out because their note also has other items (block deletes, a rename, a
    conflict ...): a note is executed as a whole or not at all; such items must be selected explicitly together."""
    by_note = collections.defaultdict(list)
    for it in plan.get("items") or []:
        by_note[note_key(it)].append(it)
    out = []
    for it in plan.get("items") or []:
        if it["category"] in ("M", "N") and it.get("executable"):
            others = [o["item_id"] for o in by_note[note_key(it)] if o is not it]
            if others:
                out.append({"item_id": it["item_id"], "rel_path": it.get("rel_path"), "with": others})
    return out


def selected_items(plan, selection, decisions=None):
    """Exactly the plan items selected, in plan order. Refuses unknown, non-executable, rejected and C ids, an
    empty selection, and any selection that takes only PART of a note: every item of a note (M with its bound B
    items, B items, T, MV — same notion_id or same new file) is one unit. Executing part of a note would let the
    adoption that follows apply replace the vault file with the Notion version and so drop the note's other,
    unconfirmed local changes (WB 4.4.2: an M bound to an unconfirmed B is not executed at all)."""
    by_id = {it["item_id"]: it for it in plan.get("items") or []}
    decisions = decisions or {}
    wanted = []
    for field in ("items", "delete", "delete_blocks", "rename", "move", "resolve"):
        for i in getattr(selection, field) or []:
            if i not in by_id:
                raise WbError(f"{i} 不在计划 {plan.get('plan_id')} 中")
            it = by_id[i]
            if it["category"] == "C" or field == "resolve":
                raise WbError(f"{i}：{conflict_steps(plan.get('plan_id'), i)}")
            if it["category"] not in _FLAG_CATS[field]:
                raise WbError(f"{i} 不能用 {_FLAG_NAMES[field]} 选择")
            if not it.get("executable"):
                why = "；".join(blocked_text(x) for x in it.get("blocked_by") or []) or "不可执行"
                raise WbError(f"{i} 不可执行：{why}")
            if decisions.get(i) == "rejected":
                raise WbError(f"{i} 已被拒绝（撤销拒绝：{cli_command('writeback', 'unreject', '--path', it['rel_path'])}，然后重新生成计划）")
            if i not in wanted:
                wanted.append(i)
    if not wanted:
        why = (f"；本计划的所有条目都不可执行：{blocked_text(UNREGISTERED_RENAME)}"
               if plan.get("unregistered_renames") else "")
        if getattr(selection, "all_safe", False) and not why:
            why = "；" + all_safe_empty_text(plan)          # r1-user-journey-13
        raise WbError("没有选择任何条目（--items / --all-safe / --delete / --delete-blocks / --rename / --move）" + why)
    if plan.get("unregistered_renames"):     # L-17, defence in depth (every item is already non-executable)
        raise WbError(f"本计划生成时存在尚未登记的本地改名/移动，所有条目都不可执行：{blocked_text(UNREGISTERED_RENAME)}")
    for i in wanted:
        for b in by_id[i].get("binds") or []:
            if b not in wanted:
                raise WbError(f"{i} 依赖块删除 {b}：须同时用 --delete-blocks {b} 确认（否则该项整体不执行）")
    by_note = collections.defaultdict(list)
    for it in plan.get("items") or []:
        by_note[note_key(it)].append(it)
    # a local change of the same note that this plan does NOT write (refused, or rejected by the user) would be
    # overwritten by the adoption that follows an executed item of that note
    unwritten = {}
    for e in list(plan.get("refused") or []) + list(plan.get("rejected") or []):
        what = e.get("class") or ("已拒绝 " + str(e.get("item_category") or ""))
        if e.get("notion_id"):
            unwritten.setdefault(("id", e["notion_id"]), what)
        if e.get("rel_path"):
            unwritten.setdefault(("rel", e["rel_path"]), what)
    for i in wanted:
        it = by_id[i]
        hit = unwritten.get(("id", it.get("notion_id"))) if it.get("notion_id") else None
        hit = hit or unwritten.get(("rel", it.get("rel_path")))
        if hit and it["category"] != "D":
            raise WbError(f"{i}：同一笔记 {it.get('rel_path') or ''} 还有本计划不会写回的本地改动（{hit}）；本笔记的其他项"
                          "不能单独执行（写回后的采纳会用 Notion 版本覆盖它）。请先处理该改动（或撤销它）后重新生成计划")
    for i in wanted:
        for o in by_note[note_key(by_id[i])]:
            oid = o["item_id"]
            if oid in wanted:
                continue
            rel = o.get("rel_path") or ""
            if o["category"] == "C":
                raise WbError(f"{i}：同一笔记 {rel} 还有冲突项 {oid}，{conflict_steps(plan.get('plan_id'), oid)}；"
                              "本笔记的其他项不能单独执行")
            if not o.get("executable") or decisions.get(oid) == "rejected":
                why = "已被拒绝" if decisions.get(oid) == "rejected" else "不可执行"
                raise WbError(f"{i}：同一笔记 {rel} 的 {oid} {why}；同一笔记的各项只能一起执行"
                              "（否则写回后的采纳会用 Notion 版本覆盖库内未写入的本地改动）")
            raise WbError(f"{i}：同一笔记 {rel} 的 {oid} 也须同时选择（{_flag_for(o['category'])} {oid}）；"
                          "同一笔记的各项只能一起执行")
    return [it for it in plan["items"] if it["item_id"] in wanted]


def challenge_phrase(plan_id, items, code):
    """WB §10.2: `确认 <P后4位> 修改N 新增N 删除N[D-…] 改名N[T-…] <挑战码>` (+ 删块N[B-…] / 移动N[MV-…] when selected)."""
    by = collections.defaultdict(list)
    for it in items:
        by[it["category"]].append(it["item_id"])

    def lst(c):
        return f"[{','.join(by[c])}]" if by[c] else ""
    dele = f"删除{len(by['D'])}{lst('D')}"
    if by["D"]:
        # WB 6.3: the phrase states how many pages go to the trash, child pages included
        kids = sum(int(it.get("children_count") or 0) for it in items if it["category"] == "D")
        dele += f"（含子页面{kids}个）"
    parts = ["确认", plan_id[-4:], f"修改{len(by['M'])}", f"新增{len(by['N'])}", dele]
    if by["B"]:
        parts.append(f"删块{len(by['B'])}{lst('B')}")
    parts.append(f"改名{len(by['T'])}{lst('T')}")
    if by["MV"]:
        parts.append(f"移动{len(by['MV'])}{lst('MV')}")
    parts.append(code)
    return " ".join(parts)


# ============================================================================================ build_plan
class _Builder:
    def __init__(self, ctx, scope, resolve_from=None):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.db = ctx.db
        self.scope = scope
        self.resolve_from = resolve_from
        self.items = []
        self.refused = []
        self.rejected = []
        self.conflicts = []
        self.notes = []
        self.counter = collections.Counter()
        self.plan_reads = 0
        self.unregistered = []          # L-17 (unregistered_renames of the whole-vault scan)

    # ------------------------------------------------------------ bookkeeping
    def next_id(self, cat):
        self.counter[cat] += 1
        return f"{cat}-{self.counter[cat]:03d}"

    def refuse(self, rel, cls, reason="", hint=None, nid=None, **extra):
        e = {"rel_path": rel, "notion_id": nid, "class": cls, "reason": reason or cls,
             "hint": hint if hint is not None else reason_hint(cls.split(":")[0])}
        if cls == "INITIAL_BACKLOG" and rel:
            e["hint"] += "：" + cli_command("writeback", "ack-backlog", "--path", rel)
        elif cls == "BLOCKED_PAGE" and nid:
            e["hint"] += "：" + cli_command("writeback", "unblock", "--page", nid)
        e.update(extra)
        self.refused.append(e)

    def in_scope_path(self, rel, nid=None):
        sc = self.scope
        if not sc or sc in ("全库", "all"):
            return True
        if sc in ("白名单", "whitelist"):
            return bool(nid and (nid in self.ctx.scope_pages() or self._row_db(nid) in self.ctx.scope_dbs()))
        if sc.startswith("resolve:"):
            return True
        return rel == sc or rel.startswith(sc.rstrip("/") + "/") or rel.startswith(sc)

    def _row_db(self, nid):
        try:
            n = self.ctx.ex.nodes.get(nid)
        except WbError:
            return None
        if n and n.get("kind") in ("row", "template"):
            return n.get("parent")
        return None

    # ------------------------------------------------------------ per-note data
    def rejection_for(self, nid, rel, l_sha, categories=None):
        """Active rejection of this note version (same L sha) for one of `categories` (None = any)."""
        try:
            rows = self.db.execute("SELECT * FROM rejections WHERE active=1 AND l_sha=? AND (notion_id=? OR rel_path=?) "
                                   "ORDER BY id DESC", (l_sha, nid, rel)).fetchall()
        except sqlite3.Error:
            return None
        for r in rows:
            if categories is None or r["item_category"] in categories:
                return dict(r)
        return None

    def blocked_state(self, nid):
        try:
            row = self.db.execute("SELECT state, reason FROM page_state WHERE notion_id=? AND state IN "
                                  "('BLOCKED','PARTIAL_CREATE')", (nid,)).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row else None


def build_plan(ctx, *, scope=None, resolve_from=None, plan_id=None):
    """Compute a plan (dict) and its preview Markdown. Writes nothing (files, db rows, Notion)."""
    b = _Builder(ctx, scope, resolve_from)
    now = _now(ctx)
    cfg = ctx.cfg
    plan_id = plan_id or new_plan_id(now)
    if resolve_from is not None:
        _plan_resolve(b, resolve_from)
        scan = None
    else:
        scan = ctx.scan()
        _plan_all(b, scan)
    plan = _assemble(b, plan_id, now, scan)
    preview = render_preview(plan)
    return plan, preview


# -------------------------------------------------------------------------------------------- state lookups
def _published_rows(ctx):
    pub, objs = wb_scan.load_export_state(ctx.paths.export_db)
    return pub, objs


def _backlog(ctx):
    try:
        v = wb_state.get_meta_json(ctx.db, "initial_backlog", {})
    except (sqlite3.Error, ValueError):
        v = {}
    return v if isinstance(v, dict) else {}


def _creation(ctx):
    """(members | None, creation_state, detail) — WB 9.2."""
    cfg = ctx.cfg
    last = None
    try:
        last = ctx.db.execute("SELECT members, state FROM creation_state ORDER BY rowid DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        last = None
    api, err = (None, "no read client")
    if ctx.reader is not None:
        api, err = ctx.reader.members()
    members = None if api is None else max(api, int(cfg.get("workspace_members") or 1))
    if last is not None and last[1] == "blocked_confirmed":
        state = "blocked_confirmed"
    elif str(cfg.get("notion_plan") or "free").lower() != "free":
        state = "unlimited"
    elif members == 1:
        state = "unlimited"
    else:
        state = "blocked_assumed"
    return members, state, err


# -------------------------------------------------------------------------------------------- full plan
def _plan_all(b, scan):
    ctx = b.ctx
    cfg = ctx.cfg
    b.scan = scan
    b.members, b.creation_state, b.members_error = _creation(ctx)
    b.pub_rows, b.objects = _published_rows(ctx)
    b.pub_by_rel = {r["rel"]: r for r in b.pub_rows if (r["status"] or "active") == "active"}
    b.pub_by_nid = collections.defaultdict(list)
    for r in b.pub_rows:
        if (r["status"] or "active") == "active" and r["notion_id"]:
            b.pub_by_nid[r["notion_id"]].append(r)
    b.rel_by_nid = {}
    for nid, rels in (scan.get("nid_index") or {}).items():
        if len(rels) == 1:
            b.rel_by_nid[nid] = rels[0]
    b.nid_by_rel = {rel: nid for nid, rels in (scan.get("nid_index") or {}).items() for rel in rels}
    # INITIAL_BACKLOG (D-20 / L-13): enabled_at + backlog at the first plan with mode >= plan. The backlog is
    # recorded from the WHOLE scan (never limited by --scope, never skipped because a note is refused for another
    # reason in this plan): every local difference that exists when write-back is enabled.
    b.backlog = _backlog(ctx)
    b.first_plan = wb_state.get_meta(ctx.db, "enabled_at") is None
    b.new_backlog = {}
    if b.first_plan:
        _record_first_backlog(b, scan)
    # SYNC_SETTLING (WB 4.2)
    obs = ctx.obsidian()
    b.sync = {"obsidian_running": bool(obs.get("running")), "obsidian_started_at": obs.get("started_at"),
              "plan_level": None}
    if not obs.get("running"):
        b.sync["plan_level"] = "本机未检测到 Obsidian 进程（客户端未运行），本轮不生成可执行项"
    else:
        st = _parse_iso(obs.get("started_at"))
        if st is not None and (_now(ctx) - st).total_seconds() < 60 * int(cfg["sync_settle_minutes"]):
            b.sync["plan_level"] = f"Obsidian 启动不足 {cfg['sync_settle_minutes']} 分钟，同步可能未完成"
    cand_rels = [m["rel"] for m in scan.get("local_modified", [])] + \
        [r["new_rel"] for r in scan.get("renamed_same_folder", [])] + \
        [x for x in scan.get("local_new", []) if x.endswith(".md")]
    # WB 4.2 burst rule over the files that differ from what was published (local edits / new files / renames):
    # an Obsidian Sync burst from another device changes many of them at once; files the publisher itself wrote
    # (unchanged since) are not local changes and do not count
    mt = {}
    for rel in cand_rels:
        try:
            mt[rel] = os.stat(os.path.join(ctx.paths.vault, *rel.split("/"))).st_mtime
        except OSError:
            pass
    b.burst = burst_files(mt, 120, int(cfg["sync_burst_files"]))
    # mapping anomalies / sync copies / frontmatter errors: listed, never items
    anomalies = wb_scan.anomaly_ids(scan)
    b.anomaly_ids = anomalies
    for s in scan.get("sync_conflict_copies", []):
        b.refuse(s["original_rel"], "SYNC_CONFLICT_COPY", f"Sync 冲突副本 {s['rel']}", nid=s.get("original_notion_id"))
    for e in scan.get("frontmatter_errors", []):
        b.refuse(e["rel"], "FRONTMATTER_ERROR", e.get("error") or "", nid=e.get("published_notion_id"))
    for d in scan.get("duplicates", []):
        b.refuse(", ".join(d["rels"]), "MAPPING_ANOMALY", "同一 notion_id 出现在多个文件", nid=d["notion_id"])
    for m in scan.get("path_id_mismatch", []):
        b.refuse(m["rel"], "MAPPING_ANOMALY", m.get("reason") or "", nid=m.get("published_notion_id"))
    for m in scan.get("unsupported_rename", []):
        b.refuse(m["new_rel"], "UNSUPPORTED_RENAME", f"{m['old_rel']} → {m['new_rel']}（{m.get('reason')}）",
                 nid=m["notion_id"])
    for m in scan.get("normalization_mismatch", []):
        b.refuse(m["rel"], "MAPPING_ANOMALY", f"路径只在 Unicode 规范化后与已发布路径 {m['published_rel']} 相同",
                 nid=m.get("notion_id"))
    # objects whose automatic N->O sync is paused (WB 14.3/14.4): no write-back until a person released them
    holds = _read_json(ctx.paths.holds, {}) or {}
    # every entry counts for the object itself (L-23: also sync_conflict_copy after the copy is gone and, but for
    # D items, pending_delete*: _own_hold) and with its subtree for its descendants (_ancestor_hold)
    b.raw_holds = {k: v for k, v in holds.items() if isinstance(v, dict)} if isinstance(holds, dict) else {}
    # L-17: renames / moves no refresh has registered yet (whole scan, never limited by --scope) block every item;
    # L-20a (ii): registered by the same hold set the publisher / adoption use; objects_map: ancestor holds
    b.unregistered = unregistered_renames(scan, adoption_holds(holds, b.db), b.objects)
    for m in scan.get("missing", []):
        if m.get("notion_id") and m["rel"].endswith(".md") and not m.get("pending") and not m.get("expired") \
                and m.get("kind") in ("page", "row") and b.in_scope_path(m["rel"], m["notion_id"]):
            b.refuse(m["rel"], "LOCAL_DELETED", "本地文件缺失（尚未进入 hold）", nid=m["notion_id"])
    # M (+B, C) candidates
    renamed = {r["notion_id"]: r for r in scan.get("renamed_same_folder", [])}
    seen = set()
    b.shadow_ops = {}
    for m in scan.get("local_modified", []):
        nid = m["notion_id"]
        if nid in anomalies or nid in seen:
            continue
        seen.add(nid)
        if m.get("kind") not in ("page", "row"):
            if b.in_scope_path(m["rel"], nid):
                b.refuse(m["rel"], "READONLY_NOTE_KIND", f"notion_kind={m.get('kind')}", nid=nid)
            continue
        if not b.in_scope_path(m["rel"], nid):
            _shadow_ops(b, nid, m["rel"], m.get("kind"))
            continue
        _plan_modified(b, nid, m["rel"], m.get("kind"))
    for nid, r in renamed.items():
        if nid in anomalies or nid in seen:
            continue
        seen.add(nid)
        if not r.get("content_changed"):
            continue
        if not b.in_scope_path(r["new_rel"], nid):
            _shadow_ops(b, nid, r["new_rel"], r.get("kind"))
            continue
        _plan_modified(b, nid, r["new_rel"], r.get("kind"))
    # T
    for nid, cand in _rename_candidates(b, scan, renamed).items():
        if nid in anomalies or not b.in_scope_path(cand["local_rel"], nid):
            continue
        _plan_rename(b, nid, cand)
    # MV
    for m in scan.get("moved_other_folder", []):
        if not b.in_scope_path(m["new_rel"], m["notion_id"]):
            continue
        if cfg.get("allow_page_move") and ctx.mode == "full" and m.get("kind") == "page":
            _plan_move(b, m)
        else:
            b.refuse(m["new_rel"], "UNSUPPORTED_RENAME", f"跨文件夹移动 {m['old_rel']} → {m['new_rel']}",
                     nid=m["notion_id"])
    # N
    for rel in scan.get("local_new", []):
        if not rel.endswith(".md") or rel in scan.get("local_new_folders", []):
            continue
        if not b.in_scope_path(rel, None) and not (b.scope in ("白名单", "whitelist")):
            continue
        _plan_new(b, rel)
    # D
    for p in scan.get("pending_deletes", []):
        nid = p["notion_id"]
        if nid in (scan.get("nid_index") or {}):
            continue
        if not b.in_scope_path(p.get("rel_path") or "", nid):
            continue
        _plan_delete(b, p)
    if b.new_backlog:
        b.notes.append(f"{len(b.new_backlog)} 篇启用写回前已存在的本地差异记为 INITIAL_BACKLOG，"
                       "逐篇 ack-backlog 后才可进入计划")


def _record_first_backlog(b, scan):
    """D-20 / L-13: at the first plan with mode >= plan, EVERY local difference of the scan is INITIAL_BACKLOG
    (modified notes, renamed / moved notes, open name overrides, new files), independent of --scope and of any
    other refusal in this plan. New files are keyed "new:<rel>" (they have no notion_id)."""
    now = _iso(_now(b.ctx))

    def l_sha_of(rel):
        try:
            return wb_common.sha256_bytes(_read_l(b.ctx, rel))
        except (OSError, ValueError):
            return None

    def add(key, rel, what, **extra):
        if key and key not in b.backlog and key not in b.new_backlog:
            e = {"l_sha": extra.pop("l_sha", None) or l_sha_of(rel), "rel": rel, "detected_at": now,
                 "rule": "first_plan", "what": what}
            e.update(extra)
            b.new_backlog[key] = e
    for m in scan.get("local_modified", []):
        if m.get("notion_id") and m.get("kind") in ("page", "row"):
            add(m["notion_id"], m["rel"], "modified", l_sha=m.get("vault_sha"))
    for r in list(scan.get("renamed_same_folder", [])) + list(scan.get("moved_other_folder", [])):
        if r.get("notion_id") and r.get("kind") in ("page", "row"):
            add(r["notion_id"], r["new_rel"], "renamed", old_rel=r.get("old_rel"))
    for o in scan.get("overrides_open", []):
        if o.get("notion_id") and o.get("local_rel"):
            add(o["notion_id"], o["local_rel"], "name_override", old_rel=o.get("old_rel"))
    folders = set(scan.get("local_new_folders", []))
    for rel in scan.get("local_new", []):
        if rel.endswith(".md") and rel not in folders:
            add("new:" + rel, rel, "new")


def _backlog_open(b, key):
    """True while `key` (notion_id or "new:<rel>") is INITIAL_BACKLOG and not acknowledged."""
    acked = wb_state.get_meta_json(b.db, "backlog_acked", {}) or {}
    for src in (b.backlog, b.new_backlog):
        if key in src:
            return not src[key].get("acked_at") and key not in acked
    return False


def _shadow_ops(b, nid, rel, kind):
    """--scope never limits the circuit breaker (WB 4.4.4): an out-of-scope LOCAL_MODIFIED note is analysed exactly
    like in a full plan, only its op count is kept (no item, no refusal, no backlog entry is emitted)."""
    saved = (len(b.items), len(b.refused), len(b.rejected), collections.Counter(b.counter), dict(b.new_backlog))
    try:
        _plan_modified(b, nid, rel, kind)
    except Exception:  # noqa: BLE001 - a note that cannot be analysed has no items in a full plan either
        pass
    n = sum(len(it.get("ops") or []) for it in b.items[saved[0]:] if it["category"] in ("M", "B"))
    del b.items[saved[0]:]
    del b.refused[saved[1]:]
    del b.rejected[saved[2]:]
    b.counter = saved[3]
    b.new_backlog = saved[4]
    if n:
        b.shadow_ops[nid] = b.shadow_ops.get(nid, 0) + n


# -------------------------------------------------------------------------------------------- M analysis
def _read_l(ctx, rel):
    p = wb_common.safe_join(ctx.paths.vault, rel)
    with open(p, "rb") as fh:
        data = fh.read()
    return data


def _is_backlog(b, nid, base, l_sha, rel=None):
    """D-20 / L-13 INITIAL_BACKLOG: (1) recorded at the first plan with mode >= plan (every L != B note then);
    (2) a note whose only base is its seed row (seq 1) and whose local edit is OLDER than that seed (the seed was
    written while the note was LOCAL_ONLY, e.g. a hold released later): its diff predates any base. Both stay
    INITIAL_BACKLOG until `writeback.py ack-backlog --path` records the acknowledgement."""
    acked = wb_state.get_meta_json(b.db, "backlog_acked", {}) or {}
    if nid in b.backlog or nid in b.new_backlog:
        return _backlog_open(b, nid)
    if b.first_plan:
        b.new_backlog[nid] = {"l_sha": l_sha, "rel": rel, "detected_at": _iso(_now(b.ctx)), "rule": "first_plan"}
        return True
    if base.get("seed") and base.get("seq") == 1 and rel and nid not in acked:
        try:
            seed_t = os.stat(os.path.join(wb_state.base_dir_for(b.db), base["sha256"])).st_mtime
            l_t = os.stat(os.path.join(b.ctx.paths.vault, *rel.split("/"))).st_mtime
        except (OSError, ValueError):
            return False
        if l_t + 1 < seed_t:
            b.new_backlog[nid] = {"l_sha": l_sha, "rel": rel, "detected_at": _iso(_now(b.ctx)),
                                  "rule": "seeded_while_local_only"}
            return True
    return False


def _local_diff_stat(b_body, l_body):
    plus = minus = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b_body, l_body, autojunk=False).get_opcodes():
        if tag != "equal":
            minus += i2 - i1
            plus += j2 - j1
    return plus, minus


def _unified(b_lines, l_lines, rel, b_label):
    return list(difflib.unified_diff(b_lines, l_lines, fromfile=f"{b_label}:{rel}", tofile=f"L:{rel}", lineterm="",
                                     n=2))


def _stale_revert(b, nid, b_body, l_body):
    """WB 4.2 STALE_REVERT: a B->L hunk that undoes a REMOTE publish (seq >= 2, not a seed)."""
    try:
        hist = wb_state.base_history_for(b.db, nid)
    except sqlite3.Error:
        return None
    hunks = [(b_body[i1:i2], l_body[j1:j2]) for tag, i1, i2, j1, j2 in
             difflib.SequenceMatcher(None, b_body, l_body, autojunk=False).get_opcodes() if tag != "equal"]
    if not hunks:
        return None
    for k in range(1, len(hist)):
        cur = hist[k]
        if cur.get("origin") != "REMOTE" or cur.get("seed") or cur.get("seq", 0) < 2:
            continue
        prev = hist[k - 1]
        try:
            pc = wb_state.base_content(prev["sha256"], db=b.db)
            cc = wb_state.base_content(cur["sha256"], db=b.db)
        except (ValueError, wb_state.BaseCorrupt):
            continue
        if pc is None or cc is None:
            continue
        try:
            _f1, pb, _s1 = body_of(pc.decode("utf-8"))
            _f2, cb, _s2 = body_of(cc.decode("utf-8"))
        except (UnicodeDecodeError, wb_common.FrontmatterError):
            continue
        added, removed = set(), set()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, pb, cb, autojunk=False).get_opcodes():
            if tag != "equal":
                removed.update(x for x in pb[i1:i2] if x.strip())
                added.update(x for x in cb[j1:j2] if x.strip())
        prev_set = set(x for x in pb if x.strip())
        for bside, lside in hunks:
            bnb = [x for x in bside if x.strip()]
            lnb = [x for x in lside if x.strip()]
            if not any(x in added for x in bnb):
                continue
            if lnb and not all(x in prev_set for x in lnb):
                continue
            if lnb and not any(x in removed for x in lnb):
                continue
            return f"撤销了 {cur.get('run_id')} 发布的 Notion 改动（base seq {cur.get('seq')}）"
    return None


class LinkSpellings:
    """Compare published and current wikilinks by identity, retaining alias, fragment and code syntax."""

    def __init__(self, scan, pub_rows, objects):
        from publish_vault import Resolver, _fold
        old = {r["rel"]: r["notion_id"] for r in pub_rows if (r["rel"] or "").endswith(".md")}
        for nid, obj in objects.items():
            if obj.get("rel_path"):
                old.setdefault(obj["rel_path"], nid)
        current = {}
        local_ids = {nid for nid, paths in scan.get("nid_index", {}).items() if len(paths) == 1}
        for nid, paths in scan.get("nid_index", {}).items():
            for path in paths:
                current[path] = nid if len(paths) == 1 else None
        for path in scan.get("local_new", []) + scan.get("symlinks", []):
            current.setdefault(path, None)
        for row in (scan.get("frontmatter_errors", []) + scan.get("unreadable", [])
                    + scan.get("sync_conflict_copies", []) + scan.get("path_id_mismatch", [])):
            current.setdefault(row["rel"], None)

        def resolver(paths):
            # Retain unidentified candidates as blockers when choosing among same-name files.
            variants = collections.Counter(_fold(path) for path in paths)

            def key(path):
                # A missing sibling keeps its literal spelling on both sides. It cannot be used to
                # prove a different spelling denotes the same object when that object has no local note.
                nid = paths[path]
                return ("id", nid) if nid in local_ids and variants[_fold(path)] == 1 else None

            return Resolver(paths, key)

        self.base = resolver(old)
        self.local = resolver(current)

    def pairs(self, rel, base, local):
        """Align whole lines that differ solely by resolved wikilink spelling. Unmatched lines stay genuine edits."""
        from publish_vault import norm_body
        if not any("[[" in line for line in base) or any("\x00" in line for line in base + local):
            return []
        old = norm_body("\n".join(base), rel, self.base).split("\n")
        new = norm_body("\n".join(local), rel, self.local).split("\n")
        pairs = []
        for i, j, size in difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks():
            for offset in range(size):
                a, b = i + offset, j + offset
                if base[a] != local[b] and "\x00" in old[a]:
                    pairs.append((a, b))
        return pairs


def _only_spelling(base, local, pairs):
    if not pairs or len(base) != len(local):
        return False
    proven = {i for i, j in pairs if i == j}
    return all(b == l or i in proven for i, (b, l) in enumerate(zip(base, local)))


def pending_modified(paths, db, scan):
    """A fresh status uses only local state. If equivalence cannot be proven, keep the note visible."""
    candidates = [m for m in scan.get("local_modified", []) if m.get("kind") in ("page", "row")]
    if db is None or not candidates:
        return [m["rel"] for m in candidates]
    links = LinkSpellings(scan, *wb_scan.load_export_state(paths.export_db))
    pending = []
    for m in candidates:
        rel = m["rel"]
        try:
            base = wb_state.latest_base(db, m["notion_id"])
            prev = wb_state.base_content(base["sha256"], db=db) if base else None
            path = wb_common.safe_join(paths.vault, rel, allow_final_symlink=False)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as fh:
                data = fh.read()
            if prev and data and b"\r" not in data and wb_common.sha256_bytes(data) == m["vault_sha"]:
                fm, old, _ = body_of(prev.decode("utf-8"))
                new_fm, local, _ = body_of(data.decode("utf-8"))
                if fm == new_fm and _only_spelling(old, local, links.pairs(rel, old, local)):
                    continue
        except (OSError, ValueError, sqlite3.Error, wb_state.BaseCorrupt):
            pass
        pending.append(rel)
    return pending


def _retained_link_preview(b, planner):
    """Use the published renderer only for a blocked preview. Adoption still uses canonical paths."""
    published = b.ctx.last_published()
    run = published.get("run_id")
    snap = published.get("snapshot")
    if not isinstance(run, str) or not run or run in (".", "..") or os.path.basename(run) != run:
        return None
    if not isinstance(snap, str) or os.path.abspath(snap) != b.ctx.snapshot():
        return None
    report = _read_json(os.path.join(b.ctx.paths.reports, run, "link_targets.json"))
    if not isinstance(report, dict):
        return None
    base_text = "\n".join(planner.Bb)
    kept = {}
    for nid, rel in report.items():
        if not isinstance(rel, str) or not rel.endswith(".md") or nid not in planner.ex.nodes:
            continue
        if b.scan.get("nid_index", {}).get(nid) != [rel] or planner.ex.link_note(nid) == rel:
            continue
        if any("[[" + rel[:-3] + end in base_text for end in ("|", "\\|", "#", "]]")):
            kept[nid] = rel
    if not kept:
        return None
    canonical = planner._render(planner._base_overlay())
    if canonical == planner.Bb:
        return None
    # Page render uses per-instance link targets. Keep the ordinary exporter intact for all other pages.
    original = planner.ex
    planner.ex = copy.copy(original)
    planner.ex.link_targets = {**original.link_targets, **kept}
    if planner._render(planner._base_overlay()) != planner.Bb:
        planner.ex = original
        return None
    paths = "、".join(f"「{p}」（Notion 当前导出路径「{original.link_note(nid)}」）"
                     for nid, p in sorted(kept.items()))
    return (f"本页引用的改名冲突笔记仍保留在 {paths}。正文可预览，写回后的核对仍按 Notion 当前路径渲染。"
            "请保留正文和现有链接，处理所列笔记的改名冲突，等下一轮刷新后重新生成计划，运行 "
            + wb_common.cli_command("writeback", "plan", work=b.ctx.paths.work, vault=b.ctx.paths.vault))


def _fm_changes(fm_b, fm_l):
    keys = set(fm_b) | set(fm_l)
    return sorted(k for k in keys if fm_b.get(k, _MISSING) != fm_l.get(k, _MISSING))


_MISSING = object()


def _hold_index(b):
    """({notion_id32: hold} of state/auto_sync_hold.json, {notion_id32 on deletion hold}), cached on the builder."""
    if getattr(b, "_holds32", None) is None:
        b._holds32 = {nid32(k).lower(): v for k, v in (getattr(b, "raw_holds", None) or {}).items()
                      if isinstance(v, dict)}
    if getattr(b, "_pend_ids", None) is None:
        try:
            b._pend_ids = {nid32(r[0]).lower() for r in b.db.execute(
                "SELECT DISTINCT notion_id FROM pending_deletes WHERE status='hold'") if r[0]}
        except sqlite3.Error:
            b._pend_ids = set()
    return b._holds32, b._pend_ids


def hold_release_hint(hold, nid):
    """Next step for an object's own hold (the words of wb_apply._hold_release_hint): a scan / export hold (e.g.
    sync_conflict_copy) stays in state/auto_sync_hold.json until the next refresh even when its cause is gone."""
    if (hold or {}).get("source") in SCAN_HOLD_SOURCES:
        return HOLD_SCAN_RELEASE_HINT.format(command=cli_command("refresh", "--release-hold", nid))
    return HOLD_MANUAL_RELEASE_HINT


def _own_hold(b, nid, cat):
    """L-23 (planner / executor hold parity, the own-object half of wb_apply._hold_check): (detail, hint) when the
    object ITSELF is held, else None.
    * any entry of state/auto_sync_hold.json — also (a) the note's own sync_conflict_copy hold after the copy is
      gone (the refresh releases it only at its next round) and (b) a pending_delete* auto hold for every category
      but D (a D item keeps its own deletion hold: it is that item's precondition);
    * (b) every category but D: a pending_deletes row with status hold.
    The detail + hint are the executor's skip reason ("AUTO_SYNC_HOLD：<detail>；<hint>"), so plan and apply say the
    same thing; a same-folder rename the exporter rejected (override_rejected:<why>) keeps its specific
    explanation and hint (only another rename clears it, waiting does not). Used by every category with an own id
    (M / B / C via _plan_modified, T, MV, D), also for resolve --take obsidian plans (L-23 (c))."""
    holds, pend = _hold_index(b)
    k = nid32(nid).lower()
    h = holds.get(k)
    if h is not None:
        reason = str(h.get("reason") or "auto_sync_hold")
        source = h.get("source") or "auto_sync_hold"
        if not (cat == "D" and reason.startswith("pending_delete")):
            detail = f"该对象的 Notion → Obsidian 自动同步已暂停（{reason}，{source}）：不写入"
            if reason.startswith("override_rejected"):
                why = reason.split(":", 1)[1] if ":" in reason else ""
                txt = OVERRIDE_REJECTED_TEXT.get(why.split("+")[0])
                return detail + (f"：{txt}" if txt else ""), OVERRIDE_REJECTED_HINT
            return detail, hold_release_hint(h, nid)
    if cat != "D" and k in pend:
        return PENDING_HOLD_TEXT, PENDING_HOLD_HINT
    return None


def _refuse_own_hold(b, rel, nid, cat):
    """AUTO_SYNC_HOLD refusal of an object held itself (_own_hold); True when refused."""
    got = _own_hold(b, nid, cat)
    if got is None:
        return False
    detail, hint = got
    b.refuse(rel, "AUTO_SYNC_HOLD", detail, hint=hint, nid=nid)
    return True


def _ancestor_hold(b, nid, cat, direct=()):
    """L-18 parity at plan time (the executor's wb_apply._hold_check refuses the same items at apply): the refusal
    detail when an ANCESTOR of the object (export parent chain: snapshot nodes + objects_map) is held with its
    subtree in state/auto_sync_hold.json (for a D item a pending_delete hold does not count), or — every category
    but D — is on deletion hold (pending_deletes status hold); for an N item `direct` are the parents the page is
    created under, where any hold (also note-only), deletion hold or BLOCKED / PARTIAL_CREATE state counts.
    None when nothing above the object is held. The object's own hold is checked by the callers (_own_hold)."""
    holds, pend_all = _hold_index(b)
    pend = pend_all if cat != "D" else set()

    def counts(h):
        return h.get("subtree", True) is not False and not (cat == "D" and str(h.get("reason") or "")
                                                             .startswith("pending_delete"))
    for d in direct:
        k = nid32(d).lower()
        h = holds.get(k)
        if h:
            return f"新页面的父级 {nid32(d)[:8]} 的自动同步已暂停（{h.get('reason')}）：不新建"
        if k in pend:
            return f"新页面的父级 {nid32(d)[:8]} 处于删除保留（pending_deletes=hold）：不新建"
        bs = b.blocked_state(d)
        if bs:
            return f"新页面的父级 {nid32(d)[:8]} 处于 {bs['state']}：不新建"
    if not holds and not pend:
        return None
    if getattr(b, "_parent_index", None) is None:
        try:
            ex = b.ctx.ex
            nodes, prev = ex.nodes, getattr(ex, "prev", None) or {}
        except Exception:  # noqa: BLE001 - no export state: the executor re-checks with its own chain
            return None
        n32 = {}
        for src in (nodes, prev):
            for k in src:
                n32.setdefault(nid32(k).lower(), k)
        b._parent_index = (nodes, prev, n32)
    nodes, prev, n32 = b._parent_index

    def parents(x):
        key = n32.get(nid32(x).lower(), x)
        out = []
        for src in (nodes, prev):
            p = (src.get(key) or {}).get("parent") if isinstance(src.get(key), dict) else None
            if p and p not in out:
                out.append(p)
        return out
    seen = {nid32(nid).lower()} if nid else set()
    todo = list(direct) + (parents(nid) if nid else [])
    depth = 0
    while todo and depth < 2000:
        depth += 1
        a = todo.pop(0)
        k = nid32(a).lower()
        if k in seen:
            continue
        seen.add(k)
        h = holds.get(k)
        if h and counts(h):
            return f"上级对象 {nid32(a)[:8]} 的自动同步已暂停（含子树：{h.get('reason')}）：不写入"
        if k in pend:
            return f"上级对象 {nid32(a)[:8]} 处于删除保留（pending_deletes=hold，含子树）：不写入"
        todo.extend(parents(a))
    return None


def _plan_modified(b, nid, rel, kind, *, r_capture=None, base_override=None, banner=None, resolve_of=None):
    """One LOCAL_MODIFIED note -> M (+B) items, a C item, or a refusal."""
    ctx = b.ctx
    import md2blocks as MB
    import export_obsidian as EO
    try:
        data = _read_l(ctx, rel)
    except (OSError, ValueError) as exc:
        b.refuse(rel, "R_UNREACHABLE", f"无法读取本地文件：{exc}", nid=nid)
        return
    l_sha = wb_common.sha256_bytes(data)
    base = wb_state.latest_base(b.db, nid)
    if base is None:
        b.refuse(rel, "NO_BASE", "base_history 无记录", nid=nid)
        return
    if base_override is None and l_sha == base["sha256"]:
        return
    rej = b.rejection_for(nid, rel, l_sha, ("M", "B"))
    if rej and resolve_of is None:
        b.rejected.append({"notion_id": nid, "rel_path": rel, "l_sha": l_sha, "since": rej.get("created_at"),
                           "item_category": rej.get("item_category"), "reason": rej.get("reason"),
                           "plan_id": rej.get("plan_id")})
        return
    bs = b.blocked_state(nid)
    if bs:
        b.refuse(rel, "BLOCKED_PAGE", f"{bs['state']}：{bs.get('reason') or ''}", nid=nid)
        return
    # L-23: the object's own hold (any auto_sync_hold entry, a deletion hold) and an ancestor's subtree hold refuse
    # the note exactly as the executor's _hold_check skips it — resolve --take obsidian plans too (L-23 (c))
    if _refuse_own_hold(b, rel, nid, "M"):
        return
    anc = _ancestor_hold(b, nid, "M")
    if anc:
        b.refuse(rel, "AUTO_SYNC_HOLD", anc, nid=nid)
        return
    if base_override is None and base.get("renderer_version") != EO.RENDERER_VERSION:
        b.refuse(rel, "RENDERER_MISMATCH", f"base {base.get('renderer_version')} ≠ 当前 {EO.RENDERER_VERSION}", nid=nid)
        return
    if resolve_of is None and _is_backlog(b, nid, base, l_sha, rel):
        b.refuse(rel, "INITIAL_BACKLOG", "启用写回前已存在的本地差异", nid=nid, l_sha=l_sha)
        return
    if rel in getattr(b, "burst", set()):
        b.refuse(rel, "SYNC_SETTLING", "120 秒内大量文件同时变化（Sync 批量写入）", nid=nid)
        return
    if b"\r" in data:
        b.refuse(rel, "LINE_ENDINGS", "文件含 CR 换行符（Windows CRLF）；导出器只写 LF，逐行比较无法进行", nid=nid)
        return
    if data.startswith(b"\xef\xbb\xbf"):
        # r1-data-safety-02: the adoption publish (publish_vault.Note) does not strip a BOM: it would read the
        # frontmatter as body and REPLACE the note without its local keys -- refused until every stage handles it
        b.refuse(rel, "UTF8_BOM", "文件以 UTF-8 BOM 开头", nid=nid)
        return
    try:
        text = data.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        fm_l_txt, l_body, _ls = body_of(text)
        fm_l = wb_common.parse_frontmatter(fm_l_txt)
    except (UnicodeDecodeError, wb_common.FrontmatterError) as exc:
        b.refuse(rel, "FRONTMATTER_ERROR", str(exc)[:200], nid=nid)
        return
    if base_override is not None:
        b_text = base_override["text"]
        rows = base_override["rows"]
    else:
        try:
            bc = wb_state.base_content(base["sha256"], db=b.db)
        except (ValueError, wb_state.BaseCorrupt) as exc:
            b.refuse(rel, "NO_BASE", f"state/base 内容不可用：{exc}", nid=nid)
            return
        if bc is None:
            b.refuse(rel, "NO_BASE", "state/base 中没有 base 内容", nid=nid)
            return
        b_text = bc.decode("utf-8")
        rows = wb_state.block_lines_for(b.db, nid, base["sha256"])
    fm_b_txt, b_body, _bs = body_of(b_text)
    fm_b = wb_common.parse_frontmatter(fm_b_txt)
    b_mask = _marker_mask(rows, _bs)
    if fm_l_txt is None:
        b.refuse(rel, "READONLY_KEY_CHANGED", "frontmatter 被删除", nid=nid)
        return
    changed_keys = _fm_changes(fm_b, fm_l)
    ro_keys = [k for k in changed_keys if str(k).startswith("notion_") and k not in ("notion_last_edited_time",)]
    if ro_keys:
        _conflict(b, nid, rel, "READONLY_KEY_CHANGED", l_sha=l_sha,
                  local=f"改了只读键 {', '.join(map(str, ro_keys))}", notion="—",
                  suggestion="还原这些 notion_* 键，或先在 Obsidian 恢复上次发布的该项元数据")
        return
    try:
        node = ctx.ex.nodes.get(nid)
    except WbError as exc:
        b.refuse(rel, "NOT_EXPORTED", str(exc), nid=nid)
        return
    if node is None:
        b.refuse(rel, "NOT_EXPORTED", "对象不在已发布快照中", nid=nid)
        return
    is_row = node.get("kind") == "row"
    prop_keys = []
    if is_row:
        prop_keys = [k for k in changed_keys if _prop_for_key(node["data"], k) is not None]
    # exact: only lines that B's block_lines rows mark as numbered-list markers may differ, and only in their number
    body_changed = not _same_body(b_body, b_mask, l_body)
    if not body_changed and not prop_keys:
        if changed_keys:
            b.refuse(rel, "LOCAL_KEYS_ONLY", f"只改了本地键 {', '.join(map(str, changed_keys))}", nid=nid)
        return
    spelling_pairs = []
    if base_override is None and body_changed and any("[[" in line for line in b_body):
        if not hasattr(b, "link_spellings"):
            b.link_spellings = LinkSpellings(b.scan, b.pub_rows, b.objects)
        spelling_pairs = b.link_spellings.pairs(rel, b_body, l_body)
    if not prop_keys and _only_spelling(b_body, l_body, spelling_pairs):
        b.refuse(rel, "LINK_REWRITE_NOOP", "只有链接写法变化", nid=nid)
        return
    # ---------------------------------------------------------------- R (live) or captured R
    if r_capture is not None:
        page, tree = r_capture["page"], {k: v for k, v in r_capture["tree"].items()}
    else:
        reader = ctx.need_reader()
        try:
            page = reader.page(nid)
            tree = reader.tree(nid) if not page.get("in_trash") else {}
        except Exception as exc:  # noqa: BLE001
            b.refuse(rel, "R_UNREACHABLE", f"读取 Notion 失败：{type(exc).__name__} {_api_status(exc) or ''}", nid=nid)
            return
    if page.get("in_trash") or page.get("archived"):
        _conflict(b, nid, rel, "REMOTE_TRASHED_LOCAL_PRESENT", l_sha=l_sha, local="本地仍有该笔记且有改动",
                  notion="Notion 页面已在回收站", suggestion="在 Notion 中恢复页面，或删除本地文件；不会自动处理")
        return
    if page.get("is_locked"):
        b.refuse(rel, "LOCKED", "Notion 页面已锁定", nid=nid)
        return
    r_sha = page_sha_live(page, tree, nid)
    if base_override is None and r_sha != base.get("page_json_sha"):
        _conflict_both(b, nid, rel, l_sha, base, rows, page, tree, r_sha, b_body, l_body, node)
        return
    if base_override is None:
        sr = _stale_revert(b, nid, b_body, l_body)
        if sr:
            b.refuse(rel, "STALE_REVERT", sr, nid=nid)
            return
    # ---------------------------------------------------------------- ops
    spelling_block = None
    ops = []
    normalized = False
    renumber_only = False
    if body_changed:
        pp = None
        diff_r = _unified(b_body, l_body, rel, "B" if base_override is None else "R")
        try:
            pp = PagePlanner(ctx, node, b_text, rows, l_body, tree, nid)
            pp.lstart = _ls
            if base_override is None and any("[[" in line for line in b_body):
                spelling_block = _retained_link_preview(b, pp)
            if spelling_pairs:
                # Keep block_lines positions stable. Use the actual overlay renderer's spelling so that
                # both the per-group and the final full-page oracles still check exact rendered lines.
                clean = pp._render(pp._base_overlay())
                if len(clean) != len(b_body):
                    raise MB.Refused("ORACLE_MISMATCH", HINTS["ORACLE_MISMATCH"])
                for i, j in spelling_pairs:
                    if clean[i] not in (b_body[i], l_body[j]):
                        raise MB.Refused("ORACLE_MISMATCH", HINTS["ORACLE_MISMATCH"])
                    if clean[i] != l_body[j] and spelling_block is None:
                        spelling_block = (f"本地第 {_ls + j} 行的链接指向同一对象，当前导出写法为"
                                          f"「{_trunc(clean[i], 180)}」。请等待刷新登记改名。若仍有差异，"
                                          "请保留正文修改，并把该行恢复为导出写法后重新生成计划，运行 "
                                          + wb_common.cli_command("writeback", "plan",
                                                                  work=ctx.paths.work, vault=ctx.paths.vault))
                    pp.Bb[i] = pp.Lb[j] = clean[i]
            ops, _info = pp.plan()
            renumber_only = bool(_info.get("renumber_only"))
        except MB.Refused as e:
            cls = e.reason
            head = cls.split(":")[0]
            hint = e.hint or reason_hint(cls) or reason_hint(head) or MB.HINTS.get(cls) or MB.HINTS.get(head, "")
            if head == "UNSUPPORTED_REGION" and not e.hint:
                hint = HINTS["UNSUPPORTED_REGION"]
            if head == "UNSUPPORTED_REGION" and pp and pp.cur_group:
                start, end, _, _ = pp.cur_group
                touched_types = {(r or {}).get("type") for r in pp.owner[start:end]} - {None, "gap"}
                if touched_types and touched_types <= {"child_page", "child_database"}:
                    hint = ("该行是根据 Notion 子页面或子数据库生成的目录链接，编辑父笔记不能改变其层级。"
                            "如需更改层级，请在 Notion 中处理子对象。请还原提示行中的子对象链接，"
                            "保留独立正文改动，重新生成计划，运行 "
                            + wb_common.cli_command("writeback", "plan", work=ctx.paths.work, vault=ctx.paths.vault))
            where = pp.where() if pp is not None else None
            reason = _refusal_text(cls, hint if head == "UNSUPPORTED_REGION" else None, where)
            b.refuse(rel, cls, reason, hint=hint, nid=nid, where=where, diff=diff_r[:80])
            return
        except Exception as exc:  # noqa: BLE001 - render refusal etc.: never an executable item
            where = pp.where() if pp is not None else None
            b.refuse(rel, "ORACLE_MISMATCH", _refusal_text("ORACLE_MISMATCH", f"{type(exc).__name__}: {str(exc)[:200]}",
                                                          where), nid=nid, where=where, diff=diff_r[:80])
            return
        normalized = any(o.get("normalized") for o in ops)
    prop_ops, prop_block = [], []
    if prop_keys:
        try:
            prop_ops, prop_block = _row_property_ops(b, node, page, fm_b, fm_l, prop_keys)
        except _PropRefused as e:
            b.refuse(rel, e.cls, e.reason, nid=nid)
            return
    # r1-user-journey-14: notion_* keys are read-only; one that differs here (notion_last_edited_time after a Notion
    # edit, resolve --take obsidian) is Notion's timestamp, not a local key the person changed or "keeps"
    if spelling_block:
        prop_block.append(spelling_block)   # adoption must match the local body (W6)
    local_kept = [k for k in changed_keys if k not in prop_keys and not str(k).startswith("notion_")]
    if not ops and not prop_ops:
        local_only = local_kept
        if renumber_only and not local_only:
            return          # only Obsidian's list renumbering: nothing to write, nothing to report (M6)
        b.refuse(rel, "LOCAL_KEYS_ONLY", "改动不产生任何 Notion 写入（只有编号/本地键变化）", nid=nid)
        return
    planned_base, planned_local = (pp.Bb, pp.Lb) if spelling_pairs else (b_body, l_body)
    diff = _unified(planned_base, planned_local, rel, "B" if base_override is None else "R")
    _emit_m(b, nid, rel, node, l_sha, base, r_sha, ops, prop_ops, prop_block, diff, normalized,
            local_keys=local_kept, banner=banner, resolve_of=resolve_of,
            page=page)


class _PropRefused(Exception):
    def __init__(self, cls, reason):
        super().__init__(reason)
        self.cls, self.reason = cls, reason


def _prop_for_key(page, key):
    """(property name, part) for a frontmatter key of a row note ('start'|'end'), or None (local key)."""
    props = page.get("properties") or {}
    k = str(key)
    part = "start"
    if k.endswith("（结束）"):
        k, part = k[:-len("（结束）")], "end"
    cands = [k]
    if k.startswith("prop_"):
        cands.insert(0, k[5:])
    for name in cands:
        p = props.get(name)
        if not isinstance(p, dict):
            continue
        exported_key = ("prop_" + name) if (name.startswith("notion_") or name.lower() in
                                            {"aliases", "tags", "cssclasses", "cssclass", "alias", "tag"}) else name
        if exported_key != k:
            continue
        if p.get("type") == "title":
            return None
        if part == "end" and p.get("type") != "date":
            return None
        return name, part
    return None


def _opts(schema_prop):
    t = schema_prop.get("type")
    return {o.get("name") for o in ((schema_prop.get(t) or {}).get("options") or []) if isinstance(o, dict)}


def _row_property_ops(b, node, page, fm_b, fm_l, keys):
    """WB 2.4.1 -> ([UPDATE_PROPERTIES op], blockers). Raises _PropRefused."""
    ctx = b.ctx
    props = page.get("properties") or {}
    by_name = collections.defaultdict(dict)
    for k in keys:
        hit = _prop_for_key(page, k)
        if hit is None:
            continue
        name, part = hit
        by_name[name][part] = fm_l.get(k)
        by_name[name]["_key"] = by_name[name].get("_key") or (k if part == "start" else str(k)[:-len("（结束）")])
    parent = page.get("parent") or {}
    dsid = parent.get("data_source_id")
    schema = {}
    if dsid and ctx.reader is not None:
        try:
            schema = (ctx.reader.data_source(dsid) or {}).get("properties") or {}
        except Exception:  # noqa: BLE001
            schema = {}
    req, before, expected = {}, {}, {}
    for name, ch in by_name.items():
        p = props[name]
        t = p.get("type")
        key = ch["_key"]
        new = fm_l.get(key)
        if t in READONLY_PROP_TYPES or t == "title":
            raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」类型 {t} 只读")
        if t == "rich_text":
            if not _single_plain(p.get("rich_text") or []):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」原值含格式/链接/多段，不能无损写回")
            if new is None:
                new = ""
            if not isinstance(new, str):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」应为文本")
            if _u16(new) > 2000:
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」超过 2000 字符")
            val = {"rich_text": [{"type": "text", "text": {"content": new}}] if new else []}
        elif t in ("select", "status"):
            if new is None:
                if t == "status":
                    raise _PropRefused("PROPERTY_LOSSY", f"状态属性「{name}」不能清空")
                val = {"select": None}
            else:
                if not isinstance(new, str) or "," in new:
                    raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」的值无效")
                if new not in _opts(schema.get(name) or {}):
                    raise _PropRefused("SCHEMA_OPTION_MISSING", f"属性「{name}」的选项「{new}」不在数据库 schema 中")
                val = {t: {"name": new}}
        elif t == "multi_select":
            vals = [] if new is None else new
            if not isinstance(vals, list) or not all(isinstance(x, str) and "," not in x for x in vals):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」应为不含逗号的文本列表")
            missing = [x for x in vals if x not in _opts(schema.get(name) or {})]
            if missing:
                raise _PropRefused("SCHEMA_OPTION_MISSING", f"属性「{name}」的选项 {missing} 不在数据库 schema 中")
            val = {"multi_select": [{"name": x} for x in vals]}
        elif t == "date":
            cur = p.get("date") or {}
            if cur.get("time_zone"):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」原值带时区，库内的值不能无损表达")
            start = _date_str(fm_l.get(key))
            end = _date_str(fm_l.get(f"{key}（结束）"))
            if start is None and end is not None:
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」只有结束日期")
            if start is False or end is False:
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」不是有效日期")
            val = {"date": None if start is None else {"start": start, "end": end, "time_zone": None}}
        elif t == "number":
            if new is not None and (isinstance(new, bool) or not isinstance(new, (int, float)) or not math.isfinite(new)):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」应为有限数值")
            val = {"number": new}
        elif t == "checkbox":
            if not isinstance(new, bool):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」应为 true/false")
            val = {"checkbox": new}
        elif t in ("url", "email", "phone_number"):
            if new is not None and not isinstance(new, str):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」应为文本")
            if t == "url" and new and not re.match(r"^(https?|mailto):", new):
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」不是有效链接")
            if t == "email" and new and "@" not in new:
                raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」不是有效邮箱")
            val = {t: new or None}
        else:
            raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」类型 {t} 不支持写回")
        # oracle: the exporter renders the new value exactly like L's frontmatter value
        newp = dict(copy.deepcopy(p))
        if t == "rich_text":
            newp["rich_text"] = [dict(x, plain_text=x["text"]["content"], annotations={}, href=None)
                                 for x in val["rich_text"]]
        else:
            newp.update(copy.deepcopy(val))
        rendered = ctx.ex.prop_value(newp, node["ts"])
        want = fm_l.get(key)
        if t == "date":
            want = _date_str(want)
        if _norm_val(rendered) != _norm_val(want):
            raise _PropRefused("PROPERTY_LOSSY", f"属性「{name}」写回后渲染为 {rendered!r}，与本地值 {want!r} 不一致")
        req[name] = val
        before[name] = wb_norm.json_norm(p)
        expected[name] = wb_norm.json_norm(newp)
    if not req:
        return [], []
    blockers = []
    db_id = parent.get("database_id") or node.get("parent")
    if db_id not in set(ctx.cfg.get("automation_free_database_ids") or []):
        blockers.append("AUTOMATION_RISK")
    op = _Op(op="UPDATE_PROPERTIES", target_id=node["id"], page_id=node["id"], properties=req,
             request={"properties": req}, before=before, expected_after=expected)
    return [op], blockers


def _date_str(v):
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, str):
        try:
            datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            try:
                datetime.date.fromisoformat(v)
            except ValueError:
                return False
        return v
    return False


def _norm_val(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, list):
        return [_norm_val(x) for x in v]
    if v == "":
        return None
    return v


def _emit_m(b, nid, rel, node, l_sha, base, r_sha, ops, prop_ops, prop_block, diff, normalized, *, local_keys,
            banner=None, resolve_of=None, page=None):
    blocked = list(prop_block)
    trash = [o for o in ops if o["op"] == "TRASH_BLOCK"]
    body_ops = [o for o in ops if o["op"] in ("UPDATE", "APPEND")] + prop_ops
    b_ids = []
    title = node.get("title")
    for o in trash:
        bid = b.next_id("B")
        b_ids.append(bid)
        b.items.append({"item_id": bid, "category": "B", "notion_id": nid, "rel_path": rel, "risk": "high",
                        "l_sha": l_sha, "b_seq": base.get("seq"), "r_json_sha": r_sha, "binds": [],
                        "ops": [_strip_internal(o)], "title": title, "notion_url": notion_url(nid),
                        "block_id": o["target_id"], "block_type": o["block_type"],
                        "text": _trunc("\n".join(o.get("old_md") or []), 500),
                        "has_children": bool(o.get("has_children")), "page_kind": node.get("kind"),
                        "_page_trash": True, "_blocked": list(blocked)})
    if body_ops:
        mid = b.next_id("M")
        new_blocks = sum(o.get("new_blocks", 0) for o in body_ops if o["op"] == "APPEND")
        item = {"item_id": mid, "category": "M", "notion_id": nid, "rel_path": rel, "risk": "normal",
                "l_sha": l_sha, "b_seq": base.get("seq"), "b_sha": base.get("sha256"), "r_json_sha": r_sha,
                "binds": list(b_ids), "ops": [_strip_internal(o) for o in body_ops], "title": title,
                "notion_url": notion_url(nid), "page_kind": node.get("kind"), "normalize": bool(normalized),
                "new_blocks": new_blocks, "diff": diff, "local_keys_kept": [str(k) for k in local_keys],
                "_blocked": list(blocked), "_appends": any(o["op"] == "APPEND" for o in body_ops),
                "_page_trash": bool(trash)}
        if banner:
            item["banner"] = banner
        if resolve_of:
            item["resolve_of"] = resolve_of
            item["risk"] = "high"
        b.items.append(item)
        for bi in b.items:
            if bi["item_id"] in b_ids:
                bi["bound_to"] = mid
    elif trash:
        for bi in b.items:
            if bi["item_id"] in b_ids:
                bi["diff"] = diff


def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"…（已截断，原文 {len(s)} 字）"


EDITOR_TYPE_TEXT = {"person": "人（person）", "bot": "集成 / 机器人（bot）"}


def _conflict(b, nid, rel, kind, *, l_sha=None, local="", notion="", suggestion="", page=None, extra=None):
    cid = b.next_id("C")
    it = {"item_id": cid, "category": "C", "notion_id": nid, "rel_path": rel, "risk": "high", "l_sha": l_sha,
          "kind": kind, "ops": [], "binds": [], "local_change": local, "notion_change": notion,
          "suggestion": suggestion, "notion_url": notion_url(nid) if nid else None, "executable": False,
          "blocked_by": ["CONFLICT"]}
    if page is not None:
        lb = page.get("last_edited_by") or {}
        it["notion_last_edited_time"] = page.get("last_edited_time")
        it["notion_last_edited_by"] = (lb.get("id") or "")[:8] or None
        et = lb.get("type") or (lb.get("object") if lb.get("object") != "user" else None)
        if not et and lb.get("id") and b.ctx.reader is not None:
            et = b.ctx.reader.user_type(lb["id"])          # r1-user-journey-09
        it["notion_editor_type"] = EDITOR_TYPE_TEXT.get(et, et) or UNKNOWN
    if extra:
        it.update(extra)
    b.items.append(it)
    return it


def _conflict_both(b, nid, rel, l_sha, base, rows, page, tree, r_sha, b_body, l_body, node):
    r_by = {x["id"]: x for kids in tree.values() for x in kids}
    b_ids = {r["block_id"]: r for r in rows if r.get("block_id") and r.get("type") != "gap"}
    changed = sum(1 for i, r in b_ids.items() if i in r_by and wb_norm.block_json_sha(r_by[i]) != r.get("json_sha"))
    added = sum(1 for i in r_by if i not in b_ids)
    removed = sum(1 for i in b_ids if i not in r_by)
    plus, minus = _local_diff_stat(b_body, l_body)
    props_changed = wb_norm.page_json_sha(page, []) != wb_norm.page_json_sha(node["data"], [])
    notion = f"块 改动 {changed} / 新增 {added} / 删除 {removed}" + ("；页面属性/标题有变化" if props_changed else "")
    # r1-user-journey-09: what Notion now holds (the blocks that differ from B), so a choice is not made blind
    side = []
    for i, x in r_by.items():
        kind_ = "改动" if i in b_ids and wb_norm.block_json_sha(x) != b_ids[i].get("json_sha") else \
            ("新增" if i not in b_ids else None)
        if kind_:
            t = x.get("type")
            txt = _plain(((x.get(t) or {}) if isinstance(x.get(t), dict) else {}).get("rich_text"))
            side.append({"kind": kind_, "type": t, "text": _trunc(txt, 120)})
        if len(side) >= 8:
            break
    _conflict(b, nid, rel, "CONFLICT_BOTH", l_sha=l_sha, local=f"本地 +{plus}/-{minus} 行",
              notion=notion, page=page,
              suggestion="按预览中的两条处理命令选择以 Notion 为准（先备份再恢复本地），或以本地为准（生成新计划）",
              extra={"b_seq": base.get("seq"), "r_json_sha": r_sha, "title": node.get("title"), "notion_side": side,
                     "diff": _unified(b_body, l_body, rel, "B"),
                     "r_capture": {"page": page, "tree": tree, "captured_at": _iso(_now(b.ctx))}})


# -------------------------------------------------------------------------------------------- T / MV
def _rename_candidates(b, scan, renamed):
    out = {}
    for nid, r in renamed.items():
        if r.get("kind") in ("page", "row"):
            out[nid] = {"old_rel": r["old_rel"], "local_rel": r["new_rel"], "source": "scan"}
    for o in scan.get("overrides_open", []):
        nid = o["notion_id"]
        if nid in out:
            continue
        found = (scan.get("nid_index") or {}).get(nid) or []
        if o.get("local_rel") in found:
            out[nid] = {"old_rel": o.get("old_rel"), "local_rel": o["local_rel"], "source": "name_override"}
    return out


def _rename_writable(new_title, nid, r_title):
    import export_obsidian as EO
    if not new_title.strip():
        return False, "新标题为空"
    if _u16(new_title) > 2000:
        return False, "新标题超过 2000 字符"
    if any(ch in EXPORT_SUBST for ch in new_title):
        return False, "含导出时的字符替换（全角符号 ／：＊？＂＜＞｜＃＾［］ … ％）"
    if TRUNC_SUFFIX_RE.search(new_title):
        return False, "含截断后缀 … (<id8>)"
    if DEDUP_SUFFIX_RE.search(new_title):
        return False, "含去重后缀 (<id8>)"
    if EO.sanitize(new_title, nid) != new_title:
        return False, "文件名不是导出器的规范名称（会被再次改名）"
    if not _single_plain(r_title):
        return False, "Notion 原标题含格式、链接或提及，不能整体替换"
    return True, None


def _plan_rename(b, nid, cand):
    ctx = b.ctx
    rel = cand["local_rel"]
    new_title = posixpath.basename(rel)[:-3]
    if _refuse_own_hold(b, rel, nid, "T"):
        return
    anc = _ancestor_hold(b, nid, "T")
    if anc:
        b.refuse(rel, "AUTO_SYNC_HOLD", anc, nid=nid)
        return
    if _backlog_open(b, nid):
        b.refuse(rel, "INITIAL_BACKLOG", "启用写回前已存在的本地差异（改名 / 命名差异）", nid=nid)
        return
    if b.blocked_state(nid):
        bs = b.blocked_state(nid)
        b.refuse(rel, "BLOCKED_PAGE", f"{bs['state']}：{bs.get('reason') or ''}", nid=nid)
        return
    try:
        node = ctx.ex.nodes.get(nid)
    except WbError as exc:
        b.refuse(rel, "NOT_EXPORTED", str(exc), nid=nid)
        return
    if node is None:
        b.refuse(rel, "NOT_EXPORTED", "对象不在已发布快照中", nid=nid)
        return
    try:
        l_sha = wb_common.sha256_bytes(_read_l(ctx, rel))
    except (OSError, ValueError):
        l_sha = None
    rej = b.rejection_for(nid, rel, l_sha, ("T",)) if l_sha else None
    if rej:
        b.rejected.append({"notion_id": nid, "rel_path": rel, "l_sha": l_sha, "since": rej.get("created_at"),
                           "item_category": "T", "reason": rej.get("reason"), "plan_id": rej.get("plan_id")})
        return
    base_prop, base_title = _title_prop(node["data"])
    try:
        page = ctx.need_reader().page(nid)
    except Exception as exc:  # noqa: BLE001
        b.refuse(rel, "R_UNREACHABLE", f"读取 Notion 失败：{type(exc).__name__}", nid=nid)
        return
    prop, r_title = _title_prop(page)
    if prop is None:
        b.refuse(rel, "PROPERTY_LOSSY", "页面没有标题属性", nid=nid)
        return
    if _canon_title(r_title) != _canon_title(base_title):
        _conflict(b, nid, rel, "CONFLICT_RENAME", l_sha=l_sha, local=f"本地改名为 {new_title}",
                  notion=f"Notion 标题已改为 {_plain(r_title)}", page=page,
                  suggestion="双方都改了名：在 Notion 中决定最终标题；本地文件名保持不变（命名差异）",
                  extra={"title": node.get("title")})
        return
    if new_title == _plain(r_title):
        return
    if page.get("is_locked"):
        b.refuse(rel, "LOCKED", "Notion 页面已锁定", nid=nid)
        return
    ok, why = _rename_writable(new_title, nid, r_title)
    req_title = [{"type": "text", "text": {"content": new_title}}]
    import md2blocks as MB
    op = _Op(op="RENAME", target_id=nid, page_id=nid, title_property=prop, old_title=copy.deepcopy(r_title),
             new_title=new_title, request={"properties": {prop: {"title": req_title}}},
             expected_after=MB.canon_rich_text([dict(req_title[0], plain_text=new_title, annotations={}, href=None)]))
    refs = _inbound(b, nid)
    tid = b.next_id("T")
    b.items.append({"item_id": tid, "category": "T", "notion_id": nid, "rel_path": rel, "risk": "high",
                    "l_sha": l_sha, "binds": [], "ops": [_strip_internal(op)], "title": node.get("title"),
                    "notion_url": notion_url(nid), "notion_title": _plain(r_title), "base_title": _plain(base_title),
                    "local_name": new_title, "old_rel": cand.get("old_rel"), "writable": ok, "why_not": why,
                    "inbound_refs": refs.get("count") if refs else UNKNOWN,
                    "rewritten_link_notes": _rewritten_links(b, rel),
                    "files_moved_next_round": 1, "page_kind": node.get("kind"),
                    "title_property": prop if node.get("kind") == "row" else None,
                    "source": cand.get("source"), "_blocked": [] if ok else ["RENAME_NOT_WRITABLE"]})


def _rewritten_links(b, rel):
    stem = posixpath.basename(rel)[:-3]
    target = rel[:-3]
    n = 0
    try:
        for m in b.scan.get("local_modified", []):
            try:
                t = _read_l(b.ctx, m["rel"]).decode("utf-8", "replace")
            except (OSError, ValueError):
                continue
            if f"[[{target}" in t or f"[[{stem}|" in t or f"[[{stem}]]" in t:
                n += 1
    except Exception:  # noqa: BLE001
        return UNKNOWN
    return n


def _plan_move(b, m):
    ctx = b.ctx
    nid = m["notion_id"]
    new_dir = posixpath.dirname(m["new_rel"])
    fn = f"{new_dir}/{posixpath.basename(new_dir)}.md"
    parent = b.nid_by_rel.get(fn)
    try:
        pnode = ctx.ex.nodes.get(parent) if parent else None
    except WbError:
        pnode = None
    if not parent or not pnode or pnode.get("kind") != "page":
        b.refuse(m["new_rel"], "UNSUPPORTED_RENAME", "新文件夹没有页面 folder note", nid=nid)
        return
    if _refuse_own_hold(b, m["new_rel"], nid, "MV"):
        return
    anc = _ancestor_hold(b, nid, "MV")
    if anc:
        b.refuse(m["new_rel"], "AUTO_SYNC_HOLD", anc, nid=nid)
        return
    if _backlog_open(b, nid):
        b.refuse(m["new_rel"], "INITIAL_BACKLOG", "启用写回前已存在的本地移动", nid=nid)
        return
    bs = b.blocked_state(nid)
    if bs:
        b.refuse(m["new_rel"], "BLOCKED_PAGE", f"{bs['state']}：{bs.get('reason') or ''}", nid=nid)
        return
    try:
        reader = ctx.need_reader()
        page = reader.page(nid)
        tree = reader.tree(nid) if not page.get("in_trash") else {}
    except Exception as exc:  # noqa: BLE001
        b.refuse(m["new_rel"], "R_UNREACHABLE", f"读取 Notion 失败：{type(exc).__name__}", nid=nid)
        return
    if page.get("in_trash") or page.get("archived"):
        b.refuse(m["new_rel"], "ALREADY_TRASHED", "Notion 页面已在回收站", nid=nid)
        return
    if page.get("is_locked"):
        b.refuse(m["new_rel"], "LOCKED", "Notion 页面已锁定", nid=nid)
        return
    base = wb_state.latest_base(b.db, nid)
    r_sha = page_sha_live(page, tree, nid)
    if base is None or base.get("page_json_sha") != r_sha:
        _conflict(b, nid, m["new_rel"], "CONFLICT_MOVE", local=f"本地移动 {m['old_rel']} → {m['new_rel']}",
                  notion="Notion 侧自 base 以来有改动（或无 base）", page=page,
                  suggestion="在 Notion 中决定页面位置；本地文件保持不动")
        return
    old_parent = (page.get("parent") or {}).get("page_id")
    blocked = [] if parent in ctx.scope_pages() else ["OUT_OF_WHITELIST:new_parent"]
    if nid not in ctx.scope_pages():
        blocked.append("OUT_OF_WHITELIST")          # the moved page itself must be declared (exact id, WB 2.3)
    if old_parent and old_parent not in ctx.scope_pages():
        blocked.append("OUT_OF_WHITELIST:old_parent")
    blocked.append("MV_NOT_SUPPORTED")              # the executor refuses every MV (I2 deviation 9): report only
    mvid = b.next_id("MV")
    b.items.append({"item_id": mvid, "category": "MV", "notion_id": nid, "rel_path": m["new_rel"], "risk": "high",
                    "binds": [], "old_rel": m["old_rel"], "old_parent_id": old_parent, "new_parent_id": parent,
                    "notion_url": notion_url(nid), "r_json_sha": r_sha, "b_seq": base.get("seq"),
                    "page_kind": "page",
                    "ops": [{"op": "MOVE_PAGE", "target_id": nid, "old_parent_id": old_parent,
                             "new_parent": {"type": "page_id", "page_id": parent}}],
                    "_blocked": blocked, "_scope_checked": True})


# -------------------------------------------------------------------------------------------- N
def _plan_new(b, rel):
    ctx = b.ctx
    import md2blocks as MB
    import export_obsidian as EO
    parts = rel.split("/")
    folder = "/".join(parts[:-1])
    stem = parts[-1][:-3]
    if "_attachments" in parts or "_templates" in parts:
        return
    try:
        data = _read_l(ctx, rel)
    except (OSError, ValueError):
        return
    l_sha = wb_common.sha256_bytes(data)
    rej = b.rejection_for(None, rel, l_sha, ("N",))
    if rej:
        b.rejected.append({"notion_id": None, "rel_path": rel, "l_sha": l_sha, "since": rej.get("created_at"),
                           "item_category": "N", "reason": rej.get("reason"), "plan_id": rej.get("plan_id")})
        return
    # the executor's mapping of pages it created (wb_meta created_page:<rel>, any state incl. unknown / partial):
    # this file was already created (or may have been) -> never propose the same N again (I2 request)
    try:
        mp = wb_state.get_meta_json(b.db, "created_page:" + rel, None)
    except (sqlite3.Error, ValueError):
        mp = {"state": "unreadable"}
    if mp:
        pid = (mp or {}).get("page_id") if isinstance(mp, dict) else None
        st = (mp or {}).get("state") if isinstance(mp, dict) else None
        ps = b.blocked_state(pid) if pid else None
        seq = (mp or {}).get("journal_seq") if isinstance(mp, dict) else None
        nxt = (f"；{wb_common.reconcile_hint(seq)}" if seq
               else (f"；检查后 {cli_command('writeback', 'unblock', '--page', pid)}" if ps else ""))
        b.refuse(rel, "N_ALREADY_CREATED", f"映射 created_page 状态 {st or UNKNOWN}，页面 {pid or UNKNOWN}"
                 + (f"（{ps['state']}）" if ps else "") + nxt, nid=pid)
        return
    if _backlog_open(b, "new:" + rel):
        b.refuse(rel, "INITIAL_BACKLOG", "启用写回前已存在的新文件", l_sha=l_sha)
        return
    if b"\r" in data:
        b.refuse(rel, "LINE_ENDINGS", "文件含 CR 换行符（Windows CRLF）")
        return
    if len(parts) < 2 or folder in wb_scan.TEAMSPACES:
        b.refuse(rel, "N_LOCATION", "团队协作区根目录不能新建页面")
        return
    fn_rel = f"{folder}/{posixpath.basename(folder)}.md"
    parent_nid = b.nid_by_rel.get(fn_rel)
    if parent_nid is None:
        sib = f"{folder}.md"
        why = "叶子笔记旁新建的文件夹" if sib in b.nid_by_rel else "所在文件夹没有 folder note"
        b.refuse(rel, "N_LOCATION", why)
        return
    try:
        pnode = ctx.ex.nodes.get(parent_nid)
    except WbError as exc:
        b.refuse(rel, "NOT_EXPORTED", str(exc))
        return
    if pnode is None or pnode.get("kind") not in ("page", "database"):
        b.refuse(rel, "N_LOCATION", "folder note 不是页面或数据库")
        return
    anc = _ancestor_hold(b, None, "N", direct=[parent_nid])
    if anc:
        b.refuse(rel, "AUTO_SYNC_HOLD", anc)
        return
    if rel in getattr(b, "burst", set()):
        b.refuse(rel, "SYNC_SETTLING", "120 秒内大量文件同时变化")
        return
    if EO.sanitize(stem, "0" * 32) != stem or DEDUP_SUFFIX_RE.search(stem) or TRUNC_SUFFIX_RE.search(stem) \
            or any(ch in EXPORT_SUBST for ch in stem):
        b.refuse(rel, "N_NEEDS_RENAME", "需改名后再新增（文件名不是规范名称）")
        return
    try:
        text = data.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        fm_txt, body, _s = body_of(text)
        fm = wb_common.parse_frontmatter(fm_txt)
    except (UnicodeDecodeError, wb_common.FrontmatterError) as exc:
        b.refuse(rel, "FRONTMATTER_ERROR", str(exc)[:200])
        return
    if any(str(k).startswith("notion_") for k in fm):
        b.refuse(rel, "READONLY_KEY_CHANGED", "新文件含 notion_* 键（可能是复制的笔记）")
        return
    dup = _possible_duplicate(b, rel, data, body, folder)
    if dup:
        b.refuse(rel, "POSSIBLE_DUPLICATE", dup)
        return
    stage2 = strict_stage(ctx)
    if stage2 and any("![[" in ln for ln in body):
        b.refuse(rel, "UNSUPPORTED_INLINE:embed", "阶段 2 不支持引用本地附件的新笔记")
        return
    try:
        children, normalized = MB.parse_new_blocks(ctx.ex, body, "") if body else ([], False)
    except MB.Refused as e:
        b.refuse(rel, e.reason, _refusal_text(e.reason), hint=e.hint or MB.HINTS.get(e.reason.split(":")[0], ""))
        return
    n_blocks = MB.block_count(children) + 1
    if stage2 and n_blocks - 1 > 100:
        b.refuse(rel, "LIMIT:new_page_blocks", f"阶段 2 拒绝正文超过 100 块的新页面（{n_blocks - 1}）")
        return
    # oracle: the request renders exactly like L's body (normalised through parse -> render); list numbers may
    # differ only on lines the rendering marks as numbered-list markers
    pp_render, pp_mask = _render_request(ctx, children, with_mask=True)
    if not normalized and not _same_body(pp_render, pp_mask, body):
        d = _first_difference(pp_render, pp_mask, body)
        where = f"；位置：正文第 {d[0] + 1} 行「{_trunc((d[2] or '').strip(), 120)}」" if d else ""
        b.refuse(rel, "ORACLE_MISMATCH", "新页面正文重新渲染后与本地文件不一致" + where)
        return
    is_row = pnode.get("kind") == "database"
    blocked = []
    props = {}
    if is_row:
        db_obj = pnode["data"]
        dss = db_obj.get("data_sources") or []
        if not dss:
            b.refuse(rel, "N_LOCATION", "数据库没有数据源")
            return
        dsid = dss[0]["id"]
        try:
            schema = (ctx.need_reader().data_source(dsid) or {}).get("properties") or {}
        except Exception as exc:  # noqa: BLE001
            b.refuse(rel, "R_UNREACHABLE", f"读取数据源失败：{type(exc).__name__}")
            return
        tname = next((n for n, p in schema.items() if isinstance(p, dict) and p.get("type") == "title"), "Name")
        title_prop = tname
        props[tname] = {"title": [{"type": "text", "text": {"content": stem}}]}
        for k, v in fm.items():
            if k in ("aliases", "tags", "cssclasses"):
                continue
            sp = schema.get(k)
            if not isinstance(sp, dict):
                continue
            t = sp.get("type")
            if t == "select" and isinstance(v, str):
                if v not in _opts(sp):
                    b.refuse(rel, "SCHEMA_OPTION_MISSING", f"属性「{k}」的选项「{v}」不在 schema 中")
                    return
                props[k] = {"select": {"name": v}}
            elif t == "multi_select" and isinstance(v, list):
                if any(x not in _opts(sp) for x in v):
                    b.refuse(rel, "SCHEMA_OPTION_MISSING", f"属性「{k}」含不存在的选项")
                    return
                props[k] = {"multi_select": [{"name": x} for x in v]}
            elif t == "rich_text" and isinstance(v, str):
                props[k] = {"rich_text": [{"type": "text", "text": {"content": v}}]}
            elif t == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
                props[k] = {"number": v}
            elif t == "checkbox" and isinstance(v, bool):
                props[k] = {"checkbox": v}
            elif t in ("url", "email", "phone_number") and isinstance(v, str):
                props[k] = {t: v}
            elif t == "date":
                s = _date_str(v)
                if s is False:
                    b.refuse(rel, "PROPERTY_LOSSY", f"属性「{k}」不是有效日期")
                    return
                props[k] = {"date": {"start": s}} if s else {"date": None}
            else:
                b.refuse(rel, "PROPERTY_LOSSY", f"属性「{k}」（{t}）不能在新建行时写入")
                return
        parent = {"type": "data_source_id", "data_source_id": dsid}
        target_scope = pnode["id"] in ctx.scope_dbs()
        if pnode["id"] not in set(ctx.cfg.get("automation_free_database_ids") or []):
            blocked.append("AUTOMATION_RISK")
    else:
        title_prop = "title"
        props["title"] = {"title": [{"type": "text", "text": {"content": stem}}]}
        parent = {"type": "page_id", "page_id": parent_nid}
        target_scope = parent_nid in ctx.scope_pages()
    if not target_scope:
        if b.scope in ("白名单", "whitelist"):
            return
        blocked.append("OUT_OF_WHITELIST")
    if b.creation_state != "unlimited":
        blocked.append("QUOTA:" + b.creation_state)
    nid_item = b.next_id("N")
    b.items.append({"item_id": nid_item, "category": "N", "notion_id": None, "rel_path": rel, "risk": "normal",
                    "l_sha": l_sha, "binds": [], "title": stem, "parent_id": pnode["id"],
                    "parent_title": pnode.get("title"), "parent_url": notion_url(pnode["id"]), "is_row": is_row,
                    "properties": props, "new_blocks": n_blocks, "normalize": bool(normalized),
                    "ops": [{"op": "CREATE_PAGE", "parent": parent, "title": stem, "title_property": title_prop,
                             "properties": {k: v for k, v in props.items() if k != title_prop},
                             "children": children, "new_blocks": n_blocks, "local_rel": rel}],
                    "body_md": list(body), "_blocked": blocked, "_scope_checked": True})


def _render_request(ctx, children, with_mask=False):
    """Body lines the exporter renders for a CREATE_PAGE request (sandbox). with_mask: also the exact body
    indices of numbered-list marker lines, from the exporter's own line recorder (each rendered line knows the
    block it belongs to)."""
    import md2blocks as MB
    import export_obsidian as EO
    from collections import ChainMap, Counter, defaultdict
    if not children:
        return ([], set()) if with_mask else []
    overlay, _ = MB.snapshot_blocks(children, "wbplan-n-root", "wbplan-n")
    ex = ctx.ex
    saved = {k: getattr(ex, k) for k in ("blocks", "report", "lists", "_rec", "_sandbox", "_list_num", "_blank_count")}
    rec = EO._Recorder(ex, "wbplan-n-root")
    try:
        ex.blocks = ChainMap(overlay, saved["blocks"])
        ex.report, ex.lists, ex._rec, ex._sandbox = defaultdict(Counter), defaultdict(list), rec, True
        elems = list(ex.render_blocks("wbplan-n-root", "General", ""))
    finally:
        for k, v in saved.items():
            setattr(ex, k, v)
    lines, mask, seen = [], set(), set()
    for x in elems:
        key = getattr(x, "key", None) if type(x) is EO._Ln else None
        if key is not None and getattr(x, "role", None) == "own" and key not in seen:
            seen.add(key)                       # a block's first own line (a list item's marker line)
            if (rec.meta.get(key) or {}).get("type") == "numbered_list_item":
                mask.add(len(lines))
        lines.extend(str(x).split("\n"))
    lead = 0
    while lead < len(lines) and not lines[lead].strip():
        lead += 1
    lines = lines[lead:]
    while lines and not lines[-1].strip():
        lines.pop()
    mask = {i - lead for i in mask if 0 <= i - lead < len(lines)}
    return (lines, mask) if with_mask else lines


def _possible_duplicate(b, rel, data, body, folder):
    sha = wb_common.sha256_bytes(data)
    for r in b.pub_rows:
        if r["sha"] == sha:
            return f"内容与已发布文件 {r['rel']} 完全相同"
    try:
        for (old_rel,) in b.db.execute("SELECT rel_path FROM plan_items WHERE category='N' AND decision='executed' "
                                       "AND rel_path=?", (rel,)):
            return f"该本地路径已经通过写回新建过页面（{old_rel}）"
    except sqlite3.Error:
        pass
    text = "\n".join(body)
    if len(text) < 40:
        return None
    near = {folder, posixpath.dirname(folder)}
    for other_rel, nid in b.nid_by_rel.items():
        if posixpath.dirname(other_rel) not in near or other_rel == rel:
            continue
        try:
            o = _read_l(b.ctx, other_rel).decode("utf-8", "replace")
            _f, ob, _s = body_of(o)
        except (OSError, ValueError, wb_common.FrontmatterError):
            continue
        sm = difflib.SequenceMatcher(None, text, "\n".join(ob), autojunk=False)
        if sm.real_quick_ratio() >= 0.9 and sm.quick_ratio() >= 0.9 and sm.ratio() >= 0.9:
            return f"与 {other_rel} 相似度 {sm.ratio():.0%}（≥ 90%）"
    return None


# -------------------------------------------------------------------------------------------- D
def _inbound(b, nid):
    """Inbound references from the planning snapshot: {count, by_source:{page_id: n}, relations:[...]}."""
    try:
        ex = b.ctx.ex
    except WbError:
        return None
    key = "_inbound_cache"
    cache = getattr(b, key, None)
    if cache is None:
        cache = {}
        setattr(b, key, cache)
    if nid in cache:
        return cache[nid]
    n32 = nid32(nid)
    by_src = collections.Counter()
    rel_refs = collections.Counter()
    for parent, kids in ex.blocks.items():
        for blk in kids:
            t = blk.get("type")
            v = blk.get(t) if isinstance(blk.get(t), dict) else {}
            hit = False
            if t == "link_to_page" and (v.get("page_id") == nid):
                hit = True
            else:
                for arr in [v.get("rich_text") or []] + list(v.get("cells") or []):
                    for r in arr:
                        if not isinstance(r, dict):
                            continue
                        if r.get("type") == "mention" and ((r.get("mention") or {}).get("page") or {}).get("id") == nid:
                            hit = True
                        url = ((r.get("text") or {}).get("link") or {}).get("url") or r.get("href") or ""
                        if n32 in url.replace("-", ""):
                            hit = True
            if hit:
                owner = (ex.block_row.get(blk.get("id")) or (None,))[0] or parent
                if owner != nid:
                    by_src[owner] += 1
    for (oid, kind), (data, _par) in ex.obj.items():
        if kind != "page" or oid == nid:
            continue
        for pname, p in (data.get("properties") or {}).items():
            if isinstance(p, dict) and p.get("type") == "relation":
                if any(x.get("id") == nid for x in p.get("relation") or []):
                    db = ((data.get("parent") or {}).get("database_id")) or "?"
                    rel_refs[(db, pname)] += 1
    out = {"count": sum(by_src.values()), "by_source": dict(by_src),
           "relations": [{"database_id": d, "property": p, "rows": n} for (d, p), n in sorted(rel_refs.items())]}
    cache[nid] = out
    return out


def _plan_delete(b, p):
    ctx = b.ctx
    nid = p["notion_id"]
    rel = p.get("rel_path") or ""
    try:
        sub = json.loads(p.get("subtree_json") or "{}") if isinstance(p.get("subtree_json"), str) else {}
    except ValueError:
        sub = {}
    ids = [e.get("notion_id") for e in (sub.get("ids") or []) if isinstance(e, dict)] or [nid]
    high = bool(sub.get("high_risk")) or len(ids) > 1
    obj = (b.objects or {}).get(nid) or {}
    kind = obj.get("kind")
    try:
        node = ctx.ex.nodes.get(nid)
    except WbError:
        node = None
    if kind not in ("page", "row") or (node is not None and node.get("kind") not in ("page", "row")):
        b.refuse(rel, "DELETE_UNSUPPORTED", f"kind={kind}", nid=nid)
        return
    if node is not None and node.get("parent") is None:
        b.refuse(rel, "DELETE_UNSUPPORTED", "侧栏根对象", nid=nid)
        return
    # own hold (a pending_delete* reason / the pending_deletes row are this item's precondition), then ancestors
    if _refuse_own_hold(b, rel, nid, "D"):
        return
    anc = _ancestor_hold(b, nid, "D")
    if anc:
        b.refuse(rel, "AUTO_SYNC_HOLD", anc, nid=nid)
        return
    try:
        reader = ctx.need_reader()
        page = reader.page(nid)
        tree = reader.tree(nid) if not page.get("in_trash") else {}
    except Exception as exc:  # noqa: BLE001
        b.refuse(rel, "R_UNREACHABLE", f"读取 Notion 失败：{type(exc).__name__} {_api_status(exc) or ''}", nid=nid)
        return
    if page.get("in_trash"):
        b.refuse(rel, "ALREADY_TRASHED", "Notion 页面已在回收站", nid=nid)
        return
    base = wb_state.latest_base(b.db, nid)
    r_sha = page_sha_live(page, tree, nid)
    if base is None or base.get("page_json_sha") != r_sha:
        b.refuse(rel, "CONFLICT_DEL_EDIT", "Notion 侧自 base 以来有改动（或无 base）", nid=nid)
        return
    blocks = [x for kids in tree.values() for x in kids]
    if any(x.get("type") == "child_database" for x in blocks):
        b.refuse(rel, "DELETE_UNSUPPORTED", "页面含子数据库", nid=nid)
        return
    syn = [x["id"] for x in blocks if x.get("type") == "synced_block"
           and (x.get("synced_block") or {}).get("synced_from") is None]
    if syn:
        b.refuse(rel, "DELETE_UNSUPPORTED", "页面含被引用的同步块原件", nid=nid)
        return
    # live subtree (child pages, recursively)
    children, stack, seen = [], [x["id"] for x in blocks if x.get("type") == "child_page"], set()
    while stack:
        cid = stack.pop()
        if cid in seen or len(seen) > 500:
            continue
        seen.add(cid)
        try:
            cp = reader.page(cid)
            ct = reader.tree(cid)
        except Exception:  # noqa: BLE001
            children.append({"id": cid, "title": UNKNOWN})
            continue
        children.append({"id": cid, "title": _plain(_title_prop(cp)[1])})
        stack.extend(x["id"] for kids in ct.values() for x in kids if x.get("type") == "child_page")
    attachments = sum(1 for x in blocks if x.get("type") in ("image", "file", "pdf", "video", "audio"))
    refs = _inbound(b, nid)
    locked = bool(page.get("is_locked"))
    if locked:
        b.refuse(rel, "LOCKED", "Notion 页面已锁定", nid=nid)
        return
    if children or (refs and refs.get("relations")):
        high = True
    blocked = []
    snap_t = _parse_iso(ctx.snapshot_time(refresh_only=True))
    if snap_t is None or (_now(ctx) - snap_t).total_seconds() > 24 * 3600:
        blocked.append("NEEDS_REFRESH")
    ttl = _parse_iso(p.get("ttl_until"))
    if ttl is not None and ttl < _now(ctx):
        b.refuse(rel, "DELETE_UNSUPPORTED", "hold 已过期（下一轮按 Notion 恢复）", nid=nid)
        return
    is_row = (page.get("parent") or {}).get("type") in ("data_source_id", "database_id")
    db_id = (page.get("parent") or {}).get("database_id")
    in_scope = (nid in ctx.scope_pages()) if not is_row else (db_id in ctx.scope_dbs())
    if not in_scope:
        blocked.append("OUT_OF_WHITELIST")
    # trashing a page trashes its whole subtree: every descendant must itself be in the declared scope (exact ids)
    outside = [c for c in children if c.get("id") not in ctx.scope_pages()]
    if outside:
        blocked.append(f"OUT_OF_WHITELIST:子页面 {len(outside)} 个不在白名单")
    did = b.next_id("D")
    b.items.append({"item_id": did, "category": "D", "notion_id": nid, "rel_path": rel,
                    "risk": "high" if high else "normal", "binds": [], "title": _plain(_title_prop(page)[1]),
                    "notion_url": notion_url(nid), "kind": "row" if is_row else "page", "database_id": db_id,
                    "children": children, "children_count": len(children), "body_blocks": len(blocks),
                    "attachments": attachments, "synced_originals": [], "inline_databases": [],
                    "inbound": refs if refs is not None else UNKNOWN, "locked": locked,
                    "notion_last_edited_time": page.get("last_edited_time"),
                    "base_run": base.get("run_id"), "base_seq": base.get("seq"),
                    "pending": {"detected_run": p.get("detected_run"), "ttl_until": p.get("ttl_until")},
                    "r_json_sha": r_sha,
                    "subtree_rels": [e.get("rel") for e in (sub.get("ids") or []) if isinstance(e, dict)
                                     and e.get("rel") and e.get("rel") != rel],
                    "ops": [{"op": "TRASH_PAGE", "target_id": nid, "page_id": nid, "request": {"in_trash": True},
                             "page_json_sha": r_sha, "last_edited_time": page.get("last_edited_time"),
                             "subtree_ids": sorted(x["id"] for x in blocks
                                                   if x.get("type") in ("child_page", "child_database"))}],
                    "_blocked": blocked, "_scope_checked": True})


# -------------------------------------------------------------------------------------------- resolve --take obsidian
def _plan_resolve(b, src):
    """New plan from a C item of an older plan: L vs R (captured at that plan, 0 Notion requests)."""
    ctx = b.ctx
    item = src["item"]
    b.members, b.creation_state, b.members_error = src.get("members"), src.get("creation_state") or "unlimited", None
    b.pub_rows, b.objects = _published_rows(ctx)
    b.pub_by_nid = collections.defaultdict(list)
    b.nid_by_rel, b.rel_by_nid, b.backlog, b.first_plan, b.new_backlog = {}, {}, {}, False, {}
    b.sync = src.get("sync") or {"obsidian_running": None, "plan_level": None}
    b.scan = {}
    # L-23 (c): the held objects are refused exactly like in a normal plan (own hold, ancestors with subtree)
    holds = _read_json(ctx.paths.holds, {}) or {}
    b.raw_holds = {k: v for k, v in holds.items() if isinstance(v, dict)} if isinstance(holds, dict) else {}
    # L-17 holds for this new plan too: a read-only vault scan (no Notion request) for unregistered renames / moves
    # (L-20a (ii): the publisher's / adoption's hold set)
    b.unregistered = unregistered_renames(ctx.scan(), adoption_holds(holds, b.db), b.objects)
    cap = item.get("r_capture") or {}
    if item.get("kind") != "CONFLICT_BOTH" or not cap:
        raise WbError(f"{item['item_id']} 不是可用本地版本解决的冲突（{item.get('kind')}）")
    nid, rel = item["notion_id"], item["rel_path"]
    try:
        node = ctx.ex.nodes.get(nid)
    except WbError:
        node = None
    if node is None:
        raise WbError("对象不在已发布快照中，无法生成计划")
    page, tree = cap["page"], cap["tree"]
    # R rendering + geometry = the base of the new plan
    overlay = {k: list(v) for k, v in tree.items()}
    text, rows = ctx.ex.render_page(node, blocks=overlay, record=True, sandbox=True)
    banner = (f"本项将覆盖 Notion 自 {item.get('notion_last_edited_time') or UNKNOWN} 以来的改动（{item.get('notion_change')}）；"
              f"来源：计划 {src['plan_id']} 的 {item['item_id']}")
    before = len(b.items)
    _plan_modified(b, nid, rel, node.get("kind"), r_capture=cap, base_override={"text": text, "rows": rows},
                   banner=banner, resolve_of={"plan_id": src["plan_id"], "item_id": item["item_id"]})
    if len(b.items) == before and not b.refused:
        b.refuse(rel, "LOCAL_KEYS_ONLY", "本地版本与 Notion 当前内容一致，无需写回", nid=nid)
    # STALE_REVERT is judged on the original base chain
    base = wb_state.latest_base(b.db, nid)
    if base is not None and b.items:
        try:
            bc = wb_state.base_content(base["sha256"], db=b.db)
            _f, b_body, _s = body_of(bc.decode("utf-8")) if bc else (None, [], 1)
            _f2, l_body, _s2 = body_of(_read_l(ctx, rel).decode("utf-8"))
            sr = _stale_revert(b, nid, b_body, l_body)
        except Exception:  # noqa: BLE001
            sr = None
        if sr:
            b.items = [it for it in b.items if it.get("notion_id") != nid]
            b.refuse(rel, "STALE_REVERT", sr, nid=nid)


# -------------------------------------------------------------------------------------------- breaker acknowledgement
def anomaly_ack_record(plan, *, os_user=None, at=None):
    """wb_meta anomaly_ack written by `writeback.py ack-anomaly --run <P>`: bound to the anomalous SET of that plan
    (modified notes as notion_id@L-sha, new local deletes, pages over max_ops_per_page with their op counts)."""
    br = plan.get("breaker") or {}
    return {"plan_id": plan["plan_id"], "at": at or wb_common.now_iso(), "os_user": os_user,
            "modified_notes": br.get("modified_notes"), "local_deletes": br.get("local_deletes"),
            "max_ops_page": br.get("max_ops_page"), "reasons": br.get("reasons") or [],
            "keys": br.get("keys") or {}}


def ack_covers(ack, keys, cfg, now):
    """An acknowledgement covers the current anomaly only if it is recent (plan_ttl_hours) and every anomalous key
    of now was in the acknowledged set (a different burst of edits / deletes trips the breaker again). The ack is
    consumed by the next full plan that uses it (create_plan)."""
    if not isinstance(ack, dict) or not isinstance(ack.get("keys"), dict):
        return False
    at = _parse_iso(ack.get("at"))
    if at is None or (now - at).total_seconds() > float(cfg["plan_ttl_hours"]) * 3600 or at > now + \
            datetime.timedelta(minutes=5):
        return False
    k = ack["keys"]
    if not set(keys.get("modified") or []) <= set(k.get("modified") or []):
        return False
    if not set(keys.get("deletes") or []) <= set(k.get("deletes") or []):
        return False
    acked_pages = k.get("ops_pages") or {}
    for nid, n in (keys.get("ops_pages") or {}).items():
        if nid not in acked_pages or int(n) > int(acked_pages[nid]):
            return False
    return True


# -------------------------------------------------------------------------------------------- assembly
def _assemble(b, plan_id, now, scan):
    ctx = b.ctx
    cfg = ctx.cfg
    # breaker (whole vault, not limited by --scope: out-of-scope notes were analysed for their op count only)
    per_page = collections.Counter()
    for it in b.items:
        if it["category"] in ("M", "B"):
            per_page[it["notion_id"]] += len(it.get("ops") or [])
    for nid, n in (getattr(b, "shadow_ops", None) or {}).items():
        per_page[nid] += n
    max_ops = max(per_page.values(), default=0)
    modified_keys, delete_keys = [], []
    if scan is not None:
        for m in scan.get("local_modified", []):
            if m.get("kind") in ("page", "row"):
                modified_keys.append(f"{m.get('notion_id')}@{m.get('vault_sha')}")
        for r in scan.get("renamed_same_folder", []):
            if r.get("content_changed"):
                try:
                    sha = wb_common.sha256_bytes(_read_l(ctx, r["new_rel"]))
                except (OSError, ValueError):
                    sha = None
                modified_keys.append(f"{r.get('notion_id')}@{sha}")
        delete_keys = [m["notion_id"] for m in scan.get("missing", [])
                       if m.get("notion_id") and m["rel"].endswith(".md") and m.get("kind") in ("page", "row")
                       and not m.get("pending") and not m.get("expired")]
    modified = len(modified_keys)
    deletes = len(delete_keys)
    limit_ops = int(cfg["max_ops_per_page"])
    ops_pages = {nid: n for nid, n in sorted(per_page.items()) if n > limit_ops}
    reasons = []
    if max_ops > limit_ops:
        reasons.append(f"单页拟操作块数 {max_ops} > max_ops_per_page {cfg['max_ops_per_page']}")
    if modified > int(cfg["max_modified_notes"]):
        reasons.append(f"本地修改笔记 {modified} > max_modified_notes {cfg['max_modified_notes']}")
    if deletes > int(cfg["max_local_deletes"]):
        reasons.append(f"新的本地删除 {deletes} > max_local_deletes {cfg['max_local_deletes']}")
    keys = {"modified": sorted(set(modified_keys)), "deletes": sorted(set(delete_keys)), "ops_pages": ops_pages}
    ack = None
    try:
        ack = wb_state.get_meta_json(ctx.db, "anomaly_ack", None)
    except (sqlite3.Error, ValueError):
        ack = None
    full_scope = b.scope in (None, "", "全库", "all")
    acknowledged = bool(reasons and full_scope and ack_covers(ack, keys, cfg, _now(ctx)))
    breaker = {"tripped": bool(reasons) and not acknowledged, "over_threshold": bool(reasons),
               "modified_notes": modified, "local_deletes": deletes, "max_ops_page": max_ops,
               "reasons": reasons, "acknowledged": acknowledged, "keys": keys,
               "ack_plan": (ack or {}).get("plan_id") if acknowledged else None}
    if b.resolve_from is not None:
        breaker = dict(b.resolve_from.get("breaker") or breaker)
        breaker["acknowledged_in_source"] = breaker.pop("acknowledged", False)
        breaker["acknowledged"] = False
    # executability
    sp, sdb = ctx.scope_pages(), ctx.scope_dbs()
    leaf_cache = {}

    def page_scope(nid):
        try:
            node = ctx.ex.nodes.get(nid)
        except WbError:
            node = None
        if node is not None and node.get("kind") in ("row", "template"):
            return node.get("parent") in sdb
        return nid in sp

    cap_trees = {}
    if b.resolve_from is not None:
        src_it = b.resolve_from.get("item") or {}
        if (src_it.get("r_capture") or {}).get("tree") is not None:
            cap_trees[src_it.get("notion_id")] = src_it["r_capture"]["tree"]

    def is_leaf(nid):
        """WB 2.3: no child_page / child_database / inline database at any depth — checked in the planning
        snapshot AND in a live read (fail closed: anything that cannot be read is not a leaf)."""
        if nid not in leaf_cache:
            try:
                snap_kids = ctx.ex.page_children(nid)
                if ctx.reader is not None:
                    tree = ctx.reader.tree(nid)
                elif nid in cap_trees:
                    tree = cap_trees[nid]
                else:
                    tree = None
                live_kids = None if tree is None else [x for kids in tree.values() for x in kids
                                                       if x.get("type") in ("child_page", "child_database")]
                leaf_cache[nid] = not snap_kids and live_kids is not None and not live_kids
            except Exception:  # noqa: BLE001
                leaf_cache[nid] = False
        return leaf_cache[nid]
    stage2 = strict_stage(ctx)
    not_leaf = "NOT_LEAF（阶段 2 只写叶子页面）"
    trash_pages = {it["notion_id"] for it in b.items if it["category"] == "B"}
    for it in b.items:
        if it["category"] == "C":
            continue
        blocked = list(it.pop("_blocked", []) or [])
        cat = it["category"]
        if cat in ("M", "B", "T") and not page_scope(it["notion_id"]):
            blocked.append("OUT_OF_WHITELIST")
        if stage2 and cat in ("M", "B", "T") and it.get("page_kind") == "page" and not is_leaf(it["notion_id"]):
            blocked.append(not_leaf)
        if stage2 and cat == "N" and not it.get("is_row") and not is_leaf(it.get("parent_id")):
            blocked.append(not_leaf.replace("叶子页面", "叶子页面：新页面的父页面已有子页面"))
        if stage2 and cat == "D" and (it.get("children_count") or not is_leaf(it["notion_id"])):
            blocked.append(not_leaf.replace("叶子页面", "叶子页面：删除会连带子页面"))
        if cat in ("M", "B") and it["notion_id"] in trash_pages and not cfg.get("allow_block_trash"):
            blocked.append("BLOCK_TRASH_DISABLED：" + TRASH_HINT)
        if cat == "B" and b.creation_state != "unlimited":
            blocked.append("QUOTA:block_trash_forced_off")
        if cat == "M" and it.pop("_appends", False) and b.creation_state != "unlimited":
            blocked.append(f"QUOTA:{b.creation_state}：{QUOTA_HINT}")
        it.pop("_appends", None)
        it.pop("_page_trash", None)
        it.pop("_scope_checked", None)
        if breaker["tripped"]:
            blocked.append("BREAKER：" + "；".join(breaker["reasons"]) + "（核对全部异常后按预览的确认步骤重新生成全库计划）")
        if b.sync.get("plan_level"):
            blocked.append("SYNC_SETTLING：" + b.sync["plan_level"])
        if b.unregistered:
            blocked.append(UNREGISTERED_RENAME)
        if cat == "M" and it.get("binds") and not cfg.get("allow_block_trash"):
            pass
        it["blocked_by"] = list(dict.fromkeys(blocked))
        it["executable"] = not it["blocked_by"]
        if cat in ("M", "N"):
            it["preview"] = _item_preview(it)
        else:
            it["preview"] = _item_preview(it)
    for it in b.items:
        if it["category"] == "C":
            if b.unregistered and UNREGISTERED_RENAME not in (it.get("blocked_by") or []):
                it["blocked_by"] = list(it.get("blocked_by") or []) + [UNREGISTERED_RENAME]
            it["executable"] = False
            it["preview"] = _item_preview(it, plan_id)
            it.pop("_blocked", None)
    b.items.sort(key=lambda it: (CAT_ORDER[it["category"]], it["item_id"]))
    counts = collections.Counter(it["category"] for it in b.items)
    new_blocks = sum(int(it.get("new_blocks") or 0) for it in b.items if it["category"] in ("M", "N"))
    writes = 0
    for it in b.items:
        for op in it.get("ops") or []:
            if op["op"] == "APPEND":
                writes += max(1, math.ceil(len(op.get("children") or []) / 100))
            elif op["op"] == "CREATE_PAGE":
                writes += 1 + max(0, math.ceil((len(op.get("children") or []) - 100) / 100))
            else:
                writes += 1
    reads = ctx.reader.requests if ctx.reader is not None else 0
    rps = float(cfg.get("rate_limit_total_rps") or 2.5)
    apply_requests = 2 * reads + writes
    summary = {"counts": {c: counts.get(c, 0) for c in CATEGORIES},
               "executable": sum(1 for it in b.items if it.get("executable")),
               "plan_requests": reads, "apply_requests_estimate": apply_requests,
               "apply_seconds_estimate": round(apply_requests / rps) if rps else None,
               "adopt_note": "另需全库导出与两次校验约 4 分钟",
               "new_blocks": new_blocks, "creation_state": b.creation_state, "members": b.members,
               "members_error": b.members_error, "residual_risk": RESIDUAL_RISK}
    snap_name = UNKNOWN
    snap_time = UNKNOWN
    try:
        snap_name = os.path.basename(ctx.snapshot())
        snap_time = ctx.snapshot_time() or UNKNOWN
    except WbError:
        pass
    try:
        snap_rel = os.path.relpath(ctx.snapshot(), ctx.paths.work)
    except (WbError, ValueError):
        snap_rel = None
    import export_obsidian as EO
    plan = {
        "format": PLAN_FORMAT,
        "plan_id": plan_id,
        "created_at": _iso(now),
        "expires_at": _iso(now + datetime.timedelta(hours=float(cfg["plan_ttl_hours"]))),
        "mode": ctx.mode,
        "scope": b.scope or "全库",
        "renderer_version": EO.RENDERER_VERSION,
        "base_snapshot": snap_rel,
        # r1-never-unapproved-05: the vault the L shas were read from; apply / resume / rollback refuse another one
        "vault": os.path.abspath(ctx.paths.vault) if ctx.paths.vault else None,
        "config_sha": wb_config.config_sha(cfg),
        "members": b.members,
        "creation_state": b.creation_state,
        "breaker": breaker,
        "sync": b.sync,
        "limits": {"max_batch_deletes": int(cfg["max_batch_deletes"]), "max_ops_per_page": int(cfg["max_ops_per_page"]),
                   "allow_block_trash": bool(cfg.get("allow_block_trash"))},
        "inbound_source": f"依据快照 {snap_name}（{snap_time}）及计划时的实时读取；范围外与快照之后的引用不在此列",
        "items": b.items,
        "refused": b.refused,
        "rejected": b.rejected,
        "counts": dict({c: counts.get(c, 0) for c in CATEGORIES}, refused=len(b.refused), rejected=len(b.rejected)),
        "summary": summary,
        "notes": b.notes,
        "new_backlog": getattr(b, "new_backlog", {}),
        # L-17: non-empty -> every item carries blocked_by UNREGISTERED_RENAME (hint UNREGISTERED_RENAME_HINT)
        "unregistered_renames": list(b.unregistered or []),
    }
    if b.resolve_from is not None:
        plan["resolve_of"] = {"plan_id": b.resolve_from["plan_id"], "item_id": b.resolve_from["item"]["item_id"]}
    return plan


# ============================================================================================ preview
CAT_TITLE = {"N": "新增内容（N）", "M": "修改内容（M）", "D": "待删除 / 移入回收站（高风险）：页面删除 D",
             "B": "待删除 / 移入回收站（高风险）：块删除 B", "T": "标题或路径变化（高风险，T / MV）：改名 T",
             "MV": "标题或路径变化（高风险，T / MV）：移动 MV", "C": "冲突（C）"}


def _v(x):
    return UNKNOWN if x is None or x == "" else x


def _op_lines(op):
    t = op["op"]
    if t == "UPDATE":
        out = [f"- UPDATE 块 {_icode(op['target_id'][:8])}（{op.get('block_type')}）" +
               ("，写回后规范化" if op.get("normalized") else "")]
        out += _fence([f"- {x}" for x in op.get("old_md") or []] + [f"+ {x}" for x in op.get("new_md") or []])
        return out
    if t == "APPEND":
        where = (f"在块 {_icode(op['anchor_id'][:8])} 之后" if op.get("anchor_id") else
                 ("插到父块首位" if (op.get("position") or {}).get("type") == "start" else "追加到末尾"))
        out = [f"- APPEND 到父级 {_icode((op.get('parent_id') or '')[:8])}，{where}，新建块 {op.get('new_blocks')}" +
               ("，写回后规范化" if op.get("normalized") else "")]
        out += _fence([f"+ {x}" for x in op.get("new_md") or []])
        return out
    if t == "TRASH_BLOCK":
        return [f"- TRASH_BLOCK 块 {_icode(op['target_id'][:8])}（{op.get('block_type')}）"]
    if t == "UPDATE_PROPERTIES":
        out = ["- UPDATE_PROPERTIES 行属性（原始 JSON → 拟写入 JSON）："]
        for name, v in (op.get("request") or {}).get("properties", {}).items():
            out += _fence([f"{name}:", "  原始 " + json.dumps((op.get("before") or {}).get(name), ensure_ascii=False,
                                                              sort_keys=True),
                           "  拟写入 " + json.dumps(v, ensure_ascii=False, sort_keys=True)])
        return out
    if t == "RENAME":
        return [f"- RENAME 标题属性 {_icode(op.get('title_property'))}：{_icode(_plain(op.get('old_title')))} → "
                f"{_icode(op.get('new_title'))}"]
    if t == "TRASH_PAGE":
        return ["- TRASH_PAGE（PATCH in_trash: true）"]
    if t == "CREATE_PAGE":
        return [f"- CREATE_PAGE 新建块 {op.get('new_blocks')}"]
    if t == "MOVE_PAGE":
        return [f"- MOVE_PAGE 到 {_icode((op.get('new_parent') or {}).get('page_id'))}"]
    return [f"- {t}"]


def _item_preview(it, plan_id=None):
    c = it["category"]
    L = [f"### {it['item_id']} {_icode(it.get('rel_path') or '')}"]
    ex = "可执行" if it.get("executable") else ("不可执行：" + "；".join(_txt(blocked_text(x))
                                                                     for x in it.get("blocked_by") or []))
    if it.get("banner"):
        L.append(f"> 注意：{_txt(it['banner'])}")
    if c == "M":
        L += [f"- 本地路径：{_icode(it['rel_path'])}",
              f"- Notion 页面：{_icode(it.get('title') or '')} {it.get('notion_url')}",
              f"- 状态：{ex}", f"- 风险：{it.get('risk')}"]
        if it.get("binds"):
            L.append(f"- 绑定块删除：{', '.join(it['binds'])}（未确认则本项整体不执行）")
        if it.get("normalize"):
            L.append(f"- {NORMALIZED_NOTE}")
        if it.get("local_keys_kept"):
            L.append("- 本地 frontmatter 键保留在库内、不写 Notion：" + ", ".join(_icode(k) for k in it["local_keys_kept"]))
        L.append("- 逐块差异（旧 → 新，块 ID 前 8 位）与操作：")
        for op in it.get("ops") or []:
            L += _op_lines(op)
    elif c == "N":
        L += [f"- 本地路径：{_icode(it['rel_path'])}", f"- 拟建页面标题：{_icode(it.get('title'))}",
              f"- Notion 父页面：{_icode(it.get('parent_title') or '')} {it.get('parent_url')}",
              f"- 拟新建块数：{_v(it.get('new_blocks'))}", f"- 新建行：{'是' if it.get('is_row') else '否'}",
              f"- 状态：{ex}"]
        if it.get("is_row"):
            L += ["- 属性 JSON："] + _fence([json.dumps(it.get("properties"), ensure_ascii=False, sort_keys=True)])
        if it.get("normalize"):
            L.append(f"- {NORMALIZED_NOTE}")
    elif c == "B":
        L += [f"- 页面：{_icode(it.get('title') or '')} {it.get('notion_url')}",
              f"- 块 ID：{_icode(it['block_id'][:8])}  类型：{it.get('block_type')}",
              f"- 是否有子块：{'是' if it.get('has_children') else '否'}", f"- 状态：{ex}",
              f"- 绑定的修改项：{it.get('bound_to') or '无'}", "- 原文："] + _fence((it.get("text") or "").split("\n"))
    elif c == "D":
        inb = it.get("inbound")
        L += [f"- 页面：{_icode(it.get('title') or '')} {it.get('notion_url')}",
              f"- 类型：{it.get('kind')}" + (f"（数据库 {_icode(it.get('database_id'))}）" if it.get("database_id") else ""),
              f"- 子页面与子数据库（实时读取，递归）：{_v(it.get('children_count'))}"]
        for ch in it.get("children") or []:
            L.append(f"  - {_icode(ch.get('title'))} {notion_url(ch.get('id'))}")
        L += [f"- 正文块数：{_v(it.get('body_blocks'))}；附件数：{_v(it.get('attachments'))}",
              f"- 同步块原件及引用页面：{len(it.get('synced_originals') or [])}",
              f"- 内联数据库及行数：{len(it.get('inline_databases') or [])}"]
        if isinstance(inb, dict):
            L.append(f"- 入站引用：{inb.get('count')}（按来源页面：" +
                     (", ".join(f"{_icode(k[:8])}×{v}" for k, v in sorted(inb.get('by_source', {}).items())) or "无") + "）")
            L.append("- 入站关系：" + ("; ".join(f"数据库 {_icode((r['database_id'] or '')[:8])} 属性 {_icode(r['property'])} "
                                            f"{r['rows']} 行" for r in inb.get("relations") or []) or "无"))
        else:
            L += [f"- 入站引用：{UNKNOWN}", f"- 入站关系：{UNKNOWN}"]
        L += [f"- 是否锁定：{'是' if it.get('locked') else '否'}",
              f"- Notion 最后编辑时间：{_v(it.get('notion_last_edited_time'))}；base：{_v(it.get('base_run'))}（seq {_v(it.get('base_seq'))}）",
              f"- 风险：{it.get('risk')}" + ("（不能成批确认，只能逐项 --delete）" if it.get("risk") == "high" else ""),
              f"- 状态：{ex}"]
    elif c == "T":
        L += [f"- Notion 当前标题：{_icode(it.get('notion_title'))}", f"- base 中标题：{_icode(it.get('base_title'))}",
              f"- 本地新名：{_icode(it.get('local_name'))}",
              f"- 新文件名可直接用作 Notion 标题：{'满足' if it.get('writable') else '不满足：' + _txt(it.get('why_not'))}",
              f"- 入站引用数：{_v(it.get('inbound_refs'))}", f"- 库内被 Obsidian 改写链接的笔记数：{_v(it.get('rewritten_link_notes'))}",
              f"- 改名后下一轮库内将移动的文件数：{_v(it.get('files_moved_next_round'))}",
              f"- 数据库行：{'是，标题属性 ' + _icode(it.get('title_property')) if it.get('page_kind') == 'row' else '否'}",
              f"- 状态：{ex}", "- 未确认时 Notion 标题保持不变，本地文件名记为命名差异"]
    elif c == "MV":
        L += [f"- 旧路径：{_icode(it.get('old_rel'))}", f"- 旧父页面：{_icode(it.get('old_parent_id'))}",
              f"- 新父页面：{_icode(it.get('new_parent_id'))}", "- 共享与权限差异：" + UNKNOWN, f"- 状态：{ex}"]
    elif c == "C":
        L += [f"- 冲突类型：{it.get('kind')}", f"- Notion 页面：{it.get('notion_url') or UNKNOWN}",
              f"- 本地改动：{_txt(it.get('local_change'))}", f"- Notion 改动：{_txt(it.get('notion_change'))}"]
        for x in it.get("notion_side") or []:
            L.append(f"  - Notion 中{x.get('kind')}的块（{_txt(x.get('type'))}）：「{_txt(x.get('text'))}」")
        L += [f"- Notion 最后编辑时间：{_v(_local_time(it.get('notion_last_edited_time')))}；"
              f"编辑者类型：{_v(it.get('notion_editor_type'))}",
              f"- 建议：{_txt(it.get('suggestion'))}"]
        if plan_id:
            L.append(f"- 命令：{conflict_steps(plan_id, it['item_id'])}")
        L += ["- 下一轮 refresh 会把 Notion 的版本写到 `Notion_Obsidian/conflicts/` 下供对照，本地文件保持不动",
              "- 冲突项不能经 apply 确认，须按上列完整命令单独处理"]
    return "\n".join(L)


def _local_time(s):
    """An API timestamp (UTC 'Z') in local time, like every other time of the preview (r1-user-journey-09)."""
    d = _parse_iso(s) if isinstance(s, str) else None
    return _iso(d.astimezone()) if d is not None else s


def render_preview(plan, *, sha12=None):
    """preview.md (WB 5.2). All values unknown to the planner are shown as 未知, never 0."""
    s = plan["summary"]
    L = [f"# 写回预览 {plan['plan_id']}", "",
         f"- 生成时间：{plan['created_at']}；过期时间：{plan['expires_at']}",
         f"- 模式：{plan['mode']}；范围：{_txt(plan['scope'])}；渲染器：{plan['renderer_version']}",
         f"- 依据快照：{_icode(plan.get('base_snapshot') or UNKNOWN)}"]
    if sha12:
        L.append(f"- plan_sha256 前 12 位：{_icode(sha12)}（apply 时用 --sha 提供）")
    L += ["- 本文件只是预览，不能作为确认依据；写入 Notion 只能由用户本人在终端核对库外计划的编号、SHA 和选择项，运行生成该计划时终端显示的审批命令并输入现场挑战短语。", ""]
    if plan.get("resolve_of"):
        ro = plan["resolve_of"]
        L += [f"> 本计划由 resolve --take obsidian 生成（{ro['plan_id']} {ro['item_id']}）：本项将覆盖 Notion 侧的改动，"
              "仍须 apply 确认。", ""]
    if plan.get("unregistered_renames"):
        L += [f"> 注意（{UNREGISTERED_RENAME}）：{UNREGISTERED_RENAME_HINT}。本计划的所有条目都不可执行。", ">"]
        for e in plan["unregistered_renames"][:20]:
            L.append(f"> - {UNREGISTERED_KIND_TEXT.get(e.get('kind'), _txt(e.get('kind')))}："
                     f"{_icode(e.get('old_rel') or '')} → {_icode(e.get('new_rel') or '')}" +
                     (f"（{OVERRIDE_UNPUBLISHED_TEXT}）" if e.get("override_open") else ""))
        if len(plan["unregistered_renames"]) > 20:
            L.append(f"> - ……另有 {len(plan['unregistered_renames']) - 20} 处")
        L.append("")
    by = collections.defaultdict(list)
    for it in plan["items"]:
        by[it["category"]].append(it)
    for c in ("N", "M", "D", "B", "T", "MV", "C"):
        L += [f"## {CAT_TITLE[c]}", ""]
        if not by[c]:
            L += ["（无）", ""]
            continue
        if c == "D":
            L += [_txt(plan.get("inbound_source") or ""), ""]
        for it in by[c]:
            L += [it.get("preview") or _item_preview(it, plan["plan_id"]), ""]
            if c in ("M", "B", "C") and it.get("diff"):
                L += ["统一差异（B ↔ L）："] + _fence(it["diff"]) + [""]
    L += ["## 拒绝写回 / 只报告", ""]
    if not plan["refused"]:
        L += ["（无）", ""]
    for r in plan["refused"]:
        label = "因自动同步暂停而拒绝（AUTO_SYNC_HOLD）" if r["class"] == "AUTO_SYNC_HOLD" else r["class"]
        L.append(f"- {_icode(r.get('rel_path') or '')}：{_txt(label)} — {_txt(r.get('reason') or '')}；"
                 f"建议：{_txt(r.get('hint') or '')}")
        if r.get("diff"):
            L += ["", "该笔记的统一差异（B ↔ L，本项不会写入）："] + _fence(r["diff"]) + [""]
    L += ["", "## 已拒绝（用户拒绝，文件未再改动前不再提出）", ""]
    if not plan["rejected"]:
        L += ["（无）", ""]
    for r in plan["rejected"]:
        L.append(f"- {_icode(r.get('rel_path') or '')}：{_txt(r.get('item_category') or '')} 自 {_v(r.get('since'))}"
                 f"（{_txt(r.get('reason') or '')}）")
    br = plan["breaker"]
    cnt = plan["counts"]
    L += ["", "## 汇总", "",
          "- 各类数量：" + "，".join(f"{c} {cnt.get(c, 0)}" for c in CATEGORIES) +
          f"，拒绝 {cnt.get('refused', 0)}，已拒绝 {cnt.get('rejected', 0)}；可执行 {s['executable']}",
          f"- 拟发请求数：约 {s['apply_requests_estimate']}（计划阶段读取 {s['plan_requests']} 次）；预计耗时：约 "
          f"{_v(s.get('apply_seconds_estimate'))} 秒，{s['adopt_note']}",
          f"- 拟新建块数：{s['new_blocks']}；块额度状态：{plan['creation_state']}；成员数：{_v(plan.get('members'))}",
          f"- 熔断：{'已触发' if br.get('over_threshold', br['tripped']) else '未触发'}"
          + (f"（{'；'.join(br['reasons'])}）" if br.get("reasons") else "")
          + ("，已由 ack-anomaly 确认" if br.get("acknowledged") else "")
          + f"；本地修改笔记 {br['modified_notes']}，新的本地删除 {br['local_deletes']}，单页最大操作数 {br['max_ops_page']}",
          f"- Obsidian：{'运行中' if (plan.get('sync') or {}).get('obsidian_running') else ('未运行' if (plan.get('sync') or {}).get('obsidian_running') is False else UNKNOWN)}"
          + (f"；{_txt(plan['sync']['plan_level'])}" if (plan.get("sync") or {}).get("plan_level") else ""),
          f"- 剩余风险：{RESIDUAL_RISK}"]
    if br.get("tripped"):
        ack = cli_command("writeback", "ack-anomaly", "--run", plan["plan_id"])
        fresh = cli_command("writeback", "plan")
        L.append(f"- 请核对全库改动。只有确认均为本人有意操作后，才由本人运行 {_icode(ack)}，再运行 {_icode(fresh)}。")
    if plan["creation_state"] != "unlimited":
        L.append(f"- 警告：工作区成员数 {_v(plan.get('members'))}，块额度状态 {plan['creation_state']}：{QUOTA_HINT}")
    for n in plan.get("notes") or []:
        L.append(f"- {_txt(n)}")
    return "\n".join(L).rstrip("\n") + "\n"


def vault_copy_text(plan, preview):
    """Notion_Obsidian/写回预览/<P>.md: generated-file rules (WB 5.1a)."""
    fm = f"---\nnotion_kind: generated\ngenerated_by: writeback.py\nplan_id: {plan['plan_id']}\n---\n\n"
    return fm + preview.replace("\r\n", "\n").rstrip("\n") + "\n"


# ============================================================================================ persistence
def plan_bytes(plan):
    return (json.dumps(plan, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def create_plan(ctx, plan, preview):
    """Write plan.json (once), preview.md, the vault copy (ADD only) and the plans / plan_items rows; supersede
    older open plans touching the same notion_id or path. Returns {plan_id, plan_sha256, dir, superseded}."""
    pid = plan["plan_id"]
    d = ctx.paths.plan_dir(pid)
    if os.path.exists(d):
        raise WbError(f"计划目录已存在：{d}")
    data = plan_bytes(plan)
    sha = wb_common.sha256_bytes(data)
    os.makedirs(d, exist_ok=False)
    pj = os.path.join(d, "plan.json")
    wb_common.atomic_write(pj, data)
    os.chmod(pj, 0o440)
    prev_text = preview if "plan_sha256 前 12 位" in preview else render_preview(plan, sha12=sha[:12])
    wb_common.atomic_write(os.path.join(d, "preview.md"), prev_text.encode("utf-8"))
    vc = None
    if ctx.paths.vault:
        rel = f"Notion_Obsidian/写回预览/{pid}.md"
        try:
            p = wb_common.safe_join(ctx.paths.vault, rel)
            if not os.path.lexists(p):
                wb_common.atomic_write(p, vault_copy_text(plan, prev_text).encode("utf-8"))
                vc = rel
        except (OSError, ValueError) as exc:
            ctx.warnings.append(f"库内预览副本未写入：{exc}")
    db = ctx.db
    superseded = []
    # WB 5.1: older plans involving the same notion_id or path are superseded. "Involving" = items AND refusals
    # AND rejections of this plan (a note this plan refuses, e.g. now on AUTO_SYNC_HOLD, must not stay executable
    # in an older draft); a whole-vault plan evaluated every note, so it supersedes every older open plan.
    entries = list(plan["items"]) + list(plan.get("refused") or []) + list(plan.get("rejected") or [])
    keys_n = {e.get("notion_id") for e in entries if e.get("notion_id")}
    keys_r = set()
    for e in entries:
        for r in str(e.get("rel_path") or "").split(", "):
            if r:
                keys_r.add(r)
        if e.get("old_rel"):
            keys_r.add(e["old_rel"])
    supersede_all = plan.get("scope") in (None, "", "全库", "all")
    with wb_state.transaction(db):
        db.execute("INSERT INTO plans(plan_id, created_at, expires_at, plan_sha256, status, scope, counts_json) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, plan["created_at"], plan["expires_at"], sha, "draft", plan["scope"],
                                             wb_common.canon_json(plan["counts"])))
        for it in plan["items"]:
            db.execute("INSERT INTO plan_items(plan_id, item_id, notion_id, rel_path, category, ops_json, risk, decision,"
                       " reason, l_sha, r_json_sha, verify_status, binds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, it["item_id"], it.get("notion_id"), it.get("rel_path"), it["category"],
                        wb_common.canon_json(it.get("ops") or []), it.get("risk"), "pending",
                        "；".join(it.get("blocked_by") or []) or None, it.get("l_sha"), it.get("r_json_sha"),
                        "pending_next_round" if it["category"] == "D" else None,
                        wb_common.canon_json(it.get("binds") or [])))
        for old_pid, st in db.execute("SELECT plan_id, status FROM plans WHERE status IN ('draft','confirmed') AND "
                                      "plan_id != ?", (pid,)).fetchall():
            rows = db.execute("SELECT notion_id, rel_path FROM plan_items WHERE plan_id=?", (old_pid,)).fetchall()
            if supersede_all or any((n and n in keys_n) or (r and r in keys_r) for n, r in rows):
                if wb_state.cas_plan_status(db, old_pid, st, "superseded", reason=f"superseded_by:{pid}"):
                    superseded.append(old_pid)
        if (plan.get("breaker") or {}).get("acknowledged"):
            # the acknowledgement is consumed by the one full plan that used it (WB 4.4.4)
            used = wb_state.get_meta_json(db, "anomaly_ack", None)
            wb_state.set_meta(db, "anomaly_ack", None)
            wb_state.set_meta(db, "anomaly_ack_used", {"ack": used, "used_by": pid, "at": plan["created_at"]})
        if plan.get("new_backlog"):
            bl = _backlog(ctx)
            for nid, e in plan["new_backlog"].items():
                bl.setdefault(nid, e)
            wb_state.set_meta(db, "initial_backlog", bl)
        if wb_state.get_meta(db, "enabled_at") is None and plan["mode"] in ("plan", "whitelist", "full"):
            wb_state.set_meta(db, "enabled_at", plan["created_at"])
        wb_state.set_meta(db, "last_plan", {"plan_id": pid, "at": plan["created_at"], "counts": plan["counts"],
                                            "refused_by_class": dict(collections.Counter(r["class"] for r in plan["refused"]))})
    return {"plan_id": pid, "plan_sha256": sha, "dir": d, "superseded": superseded, "vault_copy": vc}


def load_plan(plan_id, *, work=None, db):
    """(plan, plan_sha256) of reports/writeback/<P>/plan.json; the bytes must hash to plans.plan_sha256."""
    _check_plan_id(plan_id)
    paths = work if isinstance(work, Paths) else Paths(work)
    row = db.execute("SELECT plan_sha256 FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise WbError(f"计划 {plan_id} 未登记在 writeback.db 中")
    try:
        with open(paths.plan_json(plan_id), "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise WbError(f"无法读取 plan.json：{exc}") from None
    sha = wb_common.sha256_bytes(data)
    if sha != row[0]:
        raise WbError(f"plan.json 的 sha256 与登记值不符（文件被改动）：{plan_id}")
    try:
        plan = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise WbError(f"plan.json 无法解析：{exc}") from None
    if plan.get("plan_id") != plan_id:
        raise WbError("plan.json 的 plan_id 与目录不符")
    return plan, sha


def expire_plans(db, now=None):
    """draft/confirmed plans past expires_at -> expired (CAS). Returns the expired plan ids."""
    now = now or datetime.datetime.now().astimezone()
    out = []
    for pid, st, exp in db.execute("SELECT plan_id, status, expires_at FROM plans WHERE status IN ('draft','confirmed')").fetchall():
        e = _parse_iso(exp)
        if e is not None and e < now and wb_state.cas_plan_status(db, pid, st, "expired", reason="ttl"):
            out.append(pid)
    return out


def plan_row(db, plan_id):
    r = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    return dict(r) if r else None


def item_decisions(db, plan_id):
    return {r[0]: r[1] for r in db.execute("SELECT item_id, decision FROM plan_items WHERE plan_id=?", (plan_id,))}


def show_item(plan, item_id):
    """Terminal text of one item: preview block, unified diff L↔B (with @@ hunks) and the op list."""
    it = next((x for x in plan["items"] if x["item_id"] == item_id), None)
    if it is None:
        raise WbError(f"{item_id} 不在计划 {plan['plan_id']} 中")
    L = [it.get("preview") or _item_preview(it, plan["plan_id"]), ""]
    if it.get("diff"):
        L += ["统一差异（B ↔ L）："] + list(it["diff"]) + [""]
    L.append("操作列表：")
    for op in it.get("ops") or []:
        L.append("  " + json.dumps({k: v for k, v in op.items() if k not in ("children", "old_md", "new_md")},
                                   ensure_ascii=False, sort_keys=True)[:2000])
        if op.get("children"):
            L.append(f"    children: {len(op['children'])} 个顶层块")
    return "\n".join(L)
