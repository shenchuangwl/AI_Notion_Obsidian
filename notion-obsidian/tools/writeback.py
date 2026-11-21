#!/usr/bin/env python3
"""writeback.py — Obsidian → Notion 手动写回：先预览，只有用户本人确认后才写入 Notion。
Manual, approval-gated write-back (Obsidian -> Notion). Nothing is written to Notion without the user's own
confirmation at an interactive terminal.

查看 VIEW (read-only; never loads the write-back token):
  status                          模式、计划、待确认的本地改动（按类）、hold、命名差异、BLOCKED、已拒绝、额度 / mode, plans, pending changes
  plan [--scope 白名单|<路径前缀>]  生成计划与预览（只读 Notion，取 refresh.lock） / build a plan + preview (read-only)
  show --plan P [--item X]        显示预览或单项差异与操作 / show the preview or one item (unified diff + ops)
  whitelist-check                 白名单页面的实际可写范围 / descendants, inline databases, rows of whitelisted pages

确认 APPROVE (human at a real terminal + fresh challenge phrase):
  apply --plan P --sha S12 <选择>   执行已确认的项 / execute exactly the selected items of plan P
        选择 selection: --items M-001,N-002 | --all-safe (M+N only) | --delete D-001 | --delete-all-in-plan |
                        --delete-blocks B-001 | --rename T-001 | --move MV-001   (conflicts only via resolve)
  resolve --plan P --item C-001 --take obsidian   以本地为准：只生成一个新计划（仍须 apply） / creates a NEW plan only

拒绝 REJECT (local decisions, 0 Notion requests; human terminal + y/N):
  reject --plan P --items M-001,T-002 [--reason R] [--restore-notion]
  unreject --id <notion_id> | --path <rel>        discard --plan P
  restore-deleted --plan P --item D-001 | --id <notion_id>
  resolve --plan P --item C-001 --take notion     以 Notion 为准：备份后恢复本地文件，下一轮 refresh 覆盖

再次执行 RE-RUN:
  plan (again) · apply --resume P · reconcile --journal N --decision committed|not_committed · unblock --page ID ·
  rollback --plan P [--items …] · ack-anomaly --run P · ack-backlog --path REL · hold --run RUN ·
  rebase --from-notion · audit-scope

命令写法 HOW TO RUN：下列为完整命令示例，上方为各子命令的参数总览。先查看当前状态：python3 -I tools/writeback.py status。
手动刷新一次 MANUAL REFRESH（登记本地改名 / 删除、更新库内笔记）：python3 -I tools/refresh.py --mode incremental
暂停轮次审阅 PAUSED REFRESH：先阅读库内 Notion_Obsidian/运行记录/<run_id>-已暂停.md，再由本人运行
python3 -I tools/refresh.py --approve-run "${RUN_ID:?请先设置暂停轮次编号}" 放行，或 python3 -I tools/refresh.py --discard-run "${RUN_ID:?请先设置暂停轮次编号}" 放弃。
对象级暂停已修复后，由本人运行 python3 -I tools/refresh.py --release-hold "${NOTION_ID:?请先设置暂停对象编号}" 解除该对象的自动暂停。

There is no --yes flag, no environment variable and no file that confirms anything. apply / rollback /
resolve --take obsidian refuse unless stdin and stdout are terminals, /dev/tty opens and no automation marker
(CLAUDECODE, AI_AGENT, CI, GITHUB_ACTIONS, CLAUDE_CODE_*) is set; the gate runs before any state file is opened.
A plan is applied (and resumed) only in the mode it was built in; the items of one note are confirmed together
or not at all.
With mode off every subcommand only prints the explanation. Exit codes: 0 ok, 1 failed/cancelled,
2 refused by mode/state, 3 human gate refused, 64 usage error (unknown option / missing argument).
"""
import argparse
import collections
import datetime
import getpass
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402
from wb_common import cli_command
import wb_config  # noqa: E402
import wb_gate  # noqa: E402
import wb_locks  # noqa: E402
import wb_plan  # noqa: E402
import wb_state  # noqa: E402
from wb_plan import WbError  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_GATE = 0, 1, 2, 3
EXIT_USAGE = 64
PROD_WORK = "/data120/Obsidian/notion-obsidian"
MODE_OFF_TEXT = ("写回模式为 off（config/writeback.json）：writeback.py 只打印说明，不生成计划、不读写 Notion。\n"
                 "Write-back mode is off: nothing is planned or written. 启用只读预览请由用户把 mode 改为 \"plan\"；"
                 "写入还需要 whitelist/full、read_integration_readonly_confirmed_at 与白名单。")


class _Fail(Exception):
    def __init__(self, msg, code=EXIT_FAIL):
        super().__init__(msg)
        self.msg, self.code = msg, code


def _out(s=""):
    print(s, flush=True)


def _err(s):
    print(s, file=sys.stderr, flush=True)


def _safe(s):
    return wb_gate.display_safe(s)


# ============================================================================================ environment
def _paths(args):
    work = os.path.abspath(getattr(args, "work", None) or wb_common.WORK)
    vault = getattr(args, "vault", None)
    if not vault and work == PROD_WORK:
        vault = wb_common.PROD_VAULT
    return wb_plan.Paths(work, vault)


def _need_vault(paths):
    if not paths.vault:
        raise _Fail("需要 --vault <库路径>（开发/演练环境不会默认使用正式库）", EXIT_REFUSED)
    if not os.path.isdir(paths.vault) or os.path.islink(paths.vault):
        raise _Fail(f"库路径不存在或是符号链接：{paths.vault}", EXIT_REFUSED)


def _check_plan_vault(paths, plan):
    """r1-never-unapproved-05: apply / resume / rollback run only against the vault the plan was built from (and
    never against the production vault by default outside the production WORK). Before the challenge."""
    _need_vault(paths)
    pv = plan.get("vault")
    if pv and os.path.realpath(pv) != os.path.realpath(paths.vault):
        raise _Fail(f"计划 {plan.get('plan_id')} 是按库 {pv} 生成的，本次库路径为 {paths.vault}：只能对生成计划时的库执行"
                    "（请用相同的 --vault）；未显示挑战短语、未写入任何内容", EXIT_REFUSED)


def _precheck_token_file(args):
    """r1-operability-01: stat-only check of the write-back token file BEFORE the challenge (lstat of the file and its
    directory; the file is never opened or read here -- wb_apply.make_clients loads it later and re-checks
    everything). A wrong mode is refused now, so no phrase is asked, nothing is confirmed, the plan keeps its status
    and running the same command again after chmod works."""
    import stat as _stat
    import notion_tokens
    path = os.path.abspath(os.path.expanduser(getattr(args, "token_file", None) or notion_tokens.WRITEBACK_ENV_FILE))
    d = os.path.dirname(path)

    def fail(msg):
        raise _Fail(f"写回凭据检查失败：{msg}；未显示挑战短语、未记录任何确认、计划状态不变（修正后直接重试同一命令）",
                    EXIT_REFUSED)
    try:
        dst = os.lstat(d)
    except FileNotFoundError:
        fail(f"凭据目录不存在：{d}")
    except OSError as exc:
        fail(f"无法检查凭据目录 {d}（{type(exc).__name__}）")
    if _stat.S_ISLNK(dst.st_mode) or not _stat.S_ISDIR(dst.st_mode):
        fail(f"凭据目录不是普通目录（或为符号链接）：{d}")
    if _stat.S_IMODE(dst.st_mode) != 0o700:
        fail(f"凭据目录权限为 {oct(_stat.S_IMODE(dst.st_mode))}，必须为 0o700：{d}（chmod 700 {d}）")
    if dst.st_uid != os.geteuid():
        fail(f"凭据目录不属于当前用户：{d}")
    try:
        fst = os.lstat(path)
    except FileNotFoundError:
        fail(f"凭据文件不存在：{path}")
    except OSError as exc:
        fail(f"无法检查凭据文件 {path}（{type(exc).__name__}）")
    if _stat.S_ISLNK(fst.st_mode) or not _stat.S_ISREG(fst.st_mode):
        fail(f"凭据文件不是普通文件（不允许符号链接）：{path}")
    if _stat.S_IMODE(fst.st_mode) != 0o600:
        fail(f"凭据文件权限为 {oct(_stat.S_IMODE(fst.st_mode))}，必须为 0o600：{path}（chmod 600 {path}）")
    if fst.st_uid != os.geteuid():
        fail(f"凭据文件不属于当前用户：{path}")


def _cfg(paths):
    try:
        return wb_config.load(paths.config)
    except wb_config.ConfigError as exc:
        raise _Fail(f"配置无效（config/writeback.json）：{exc}") from None


def _db_rw(paths):
    return wb_state.connect(paths.wb_db)


def _db_rw_existing(paths):
    """Read-write connection to an EXISTING writeback.db (never creates state/writeback.db)."""
    if not os.path.isfile(paths.wb_db):
        raise _Fail(f"还没有任何计划或写回状态（state/writeback.db 不存在）；先运行 {cli_command('writeback', 'plan')}", EXIT_REFUSED)
    return wb_state.connect(paths.wb_db)


def _config_mode_info(cfg):
    """Effective mode from the CONFIG ALONE (mode, read-integration confirmation, subtree scope caps), computed
    without opening any state file: used before the human gate so a refused command touches nothing under state/.
    Automatic downgrades recorded in writeback.db (forced_mode) are applied after the gate (_require_write_mode)."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute("CREATE TABLE wb_meta(key TEXT PRIMARY KEY, value TEXT)")
        return wb_config.effective_mode_info(cfg, mem)
    finally:
        mem.close()


def _refuse_mode_off(cfg, what):
    """WB §15: with mode off every write-back subcommand only prints the explanation (decision commands too)."""
    if cfg.get("mode") == "off":
        raise _Fail(MODE_OFF_TEXT + f"\n（{what} 未执行）", EXIT_REFUSED)


def _plan_mode_guidance(info):
    check = cli_command("writeback", "whitelist-check")
    status = cli_command("writeback", "status")
    plan = cli_command("writeback", "plan", "--scope", "白名单")
    if info is None:
        return f"先运行 `{status}` 查看当前配置和有效模式，再决定是否重新规划。"
    if info["configured"] == "plan":
        return (f"mode=plan 只允许生成预览。请用户本人核对 config/writeback.json 中的 whitelist_page_ids 和读连接"
                f"只读确认记录，并运行 `{check}` 确认范围。确需写入时，由本人在独立终端把 mode 改为 whitelist，"
                f"再运行 `{plan}`，审阅新计划。旧计划不能在新模式下执行。")
    reasons = "；".join(info["reasons"])
    return (f"配置 mode 已为 {info['configured']}，当前有效模式为 {info['mode']}。{reasons}。"
            f"用 `{status}` 检查有效模式；恢复后重新运行 `{plan}` 并审阅新计划。")


def _precheck_write_mode(cfg, what):
    """Mode check before the gate (config only, no state access)."""
    _refuse_mode_off(cfg, what)
    info = _config_mode_info(cfg)
    if info["mode"] not in ("whitelist", "full"):
        raise _Fail(f"拒绝 {what}：当前有效模式为 {info['mode']}；" + _plan_mode_guidance(info),
                    EXIT_REFUSED)
    return info


def _db_ro(paths):
    return wb_state.connect_readonly(paths.wb_db)


def _read_client(args, paths, cfg):
    """ReadOnlyClient behind the shared rate gate. Loads only the READ token (never writeback.env)."""
    import rate_gate
    from readonly_client import make_read_client
    gate = rate_gate.RateGate(os.path.join(paths.state, "rate_gate.json"),
                              total_rps=float(cfg.get("rate_limit_total_rps") or 2.5))
    kw = {"base_url": getattr(args, "base_url", None), "gate": gate, "rate": None}
    tf = getattr(args, "read_token_file", None)
    if tf:
        kw["token_path"] = tf
    return make_read_client(**kw)


def _apply_module():
    import wb_apply
    return wb_apply


def _make_clients(args, paths, cfg, purpose):
    """(WritebackClient, ReadOnlyClient) via the executor helper: the ONLY place writeback.env is loaded."""
    wa = _apply_module()
    return wa.make_clients(cfg, token_path=getattr(args, "token_file", None), base_url=getattr(args, "base_url", None),
                           read_token_path=getattr(args, "read_token_file", None), state_dir=paths.state,
                           purpose=purpose)


def _mode(cfg, db):
    return wb_config.effective_mode_info(cfg, db)


def _require_write_mode(cfg, db, what):
    info = _mode(cfg, db)
    if info["mode"] == "off":
        raise _Fail(MODE_OFF_TEXT, EXIT_REFUSED)
    if info["mode"] not in ("whitelist", "full"):
        raise _Fail(f"拒绝 {what}：当前有效模式为 {info['mode']}；" + _plan_mode_guidance(info),
                    EXIT_REFUSED)
    return info


def _human(action):
    try:
        return wb_gate.require_human(action)
    except wb_gate.GateRefused as exc:
        raise _Fail(exc.reason, EXIT_GATE) from None


def _yes(question):
    try:
        return wb_gate.confirm_yes_no(question)
    except wb_gate.GateRefused as exc:
        raise _Fail(exc.reason, EXIT_GATE) from None


def _phrase(phrase, lines):
    try:
        return wb_gate.confirm_phrase(phrase, lines)
    except wb_gate.GateRefused as exc:
        raise _Fail(exc.reason, EXIT_GATE) from None


def _os_user():
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return str(os.getuid())


def _load(paths, db, plan_id):
    try:
        return wb_plan.load_plan(plan_id, work=paths, db=db)
    except WbError as exc:
        raise _Fail(str(exc), EXIT_REFUSED) from None


def _status_now(db, plan_id, now=None):
    row = wb_plan.plan_row(db, plan_id)
    if row is None:
        raise _Fail(f"计划 {plan_id} 不存在", EXIT_REFUSED)
    st = row["status"]
    exp = wb_plan._parse_iso(row.get("expires_at"))
    if st in ("draft", "confirmed") and exp is not None and exp < (now or datetime.datetime.now().astimezone()):
        st = "expired"
    return st, row


def _item_line(it):
    ops = ",".join(o.get("op", "?") for o in it.get("ops") or [])
    return f"{it['item_id']}  {it['category']}  {it.get('rel_path') or ''}  [{ops}]"


def _item_details(it):
    """Extra lines of the exact list shown before the challenge (what each selected item will really do)."""
    c = it["category"]
    out = []
    if c == "D":
        inb = it.get("inbound")
        refs = inb.get("count") if isinstance(inb, dict) else wb_plan.UNKNOWN
        rels = (", ".join(f"{r.get('property')}×{r.get('rows')}" for r in inb.get("relations") or [])
                if isinstance(inb, dict) else wb_plan.UNKNOWN) or "无"
        out.append(f"      移入回收站：页面 {it.get('title') or ''}（风险 {it.get('risk')}）；连带子页面 "
                   f"{it.get('children_count', wb_plan.UNKNOWN)} 个；入站引用 {refs}；入站关系 {rels}")
        for ch in (it.get("children") or [])[:20]:
            out.append(f"        子页面：{ch.get('title') or wb_plan.UNKNOWN}  {ch.get('id')}")
    elif c == "B":
        out.append(f"      删除块 {str(it.get('block_id') or '')[:8]}（{it.get('block_type')}"
                   + ("，有子块" if it.get("has_children") else "") + f"）：{(it.get('text') or '')[:80]}")
    elif c == "T":
        out.append(f"      Notion 标题：{it.get('notion_title')} → {it.get('local_name')}")
    elif c == "M":
        n = collections.Counter(o.get("op") for o in it.get("ops") or [])
        out.append("      " + "，".join(f"{k}×{v}" for k, v in sorted(n.items()))
                   + (f"；新建块 {it.get('new_blocks')}" if it.get("new_blocks") else "")
                   + (f"；绑定块删除 {','.join(it['binds'])}" if it.get("binds") else ""))
    elif c == "N":
        out.append(f"      新建页面「{it.get('title')}」于 {it.get('parent_title') or it.get('parent_id')}；"
                   f"块 {it.get('new_blocks')}")
    return out



# ============================================================================================ confirmation core
def _write_approval(paths, name, data):
    """approvals/writeback/<name>.json, never overwritten (O_EXCL; a numbered sibling if it exists)."""
    os.makedirs(paths.approvals, exist_ok=True)
    base = os.path.join(paths.approvals, name)
    cand, n = base + ".json", 0
    payload = (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    while True:
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError:
            n += 1
            cand = f"{base}.{n}.json"
            continue
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return cand


def record_confirmation(paths, db, plan_id, plan_sha, item_ids, *, scope, tty, os_user, selection=None,
                        approval_name=None, extra=None):
    """INSERT one confirmations row per item (phrase_scope = scope, full plan sha, challenge_ok=1) and write the
    approvals JSON (no token, no challenge code). Returns the confirmation record handed to the executor."""
    at = wb_common.now_iso()
    with wb_state.transaction(db):
        for iid in item_ids:
            db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                       "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,?,?,?)",
                       (plan_id, iid, at, os_user, tty, 1, scope, plan_sha))
    record = {"plan_id": plan_id, "plan_sha256": plan_sha, "os_user": os_user, "tty": tty, "confirmed_at": at,
              "selection": selection if selection is not None else None, "items": list(item_ids),
              "challenge_ok": True, "phrase_scope": scope}
    if extra:
        record.update(extra)
    rec_file = dict(record)
    if selection is None:
        rec_file.pop("selection")
    record["approval_file"] = _write_approval(paths, approval_name or plan_id, rec_file)
    return record


def _plan_mode_matches(cfg, db, plan):
    """A plan is applied only in the mode it was checked in: the stage-dependent rules (leaf pages, new-page
    limits) were evaluated with plan['mode'], so a different effective mode now (e.g. a draft built in mode plan,
    config switched since) requires a new plan."""
    cur = _mode(cfg, db)["mode"]
    if plan.get("mode") != cur:
        raise _Fail(f"计划 {plan.get('plan_id')} 生成时的模式为 {plan.get('mode')}，当前有效模式为 {cur}：规则按生成时的"
                    f"模式检查，请重新运行 {cli_command('writeback', 'plan')} 生成计划", EXIT_REFUSED)


def _registration_holds(paths, db):
    """L-20a (ii): the hold set that registers a rename / move — the one the publisher and the adoption use
    (state/auto_sync_hold.json + pending_deletes on hold + page_state BLOCKED / PARTIAL_CREATE: wb_plan.adoption_holds,
    the planner's own set). `db` None (no writeback.db yet): auto_sync_hold.json alone."""
    return wb_plan.adoption_holds(wb_plan._read_json(paths.holds, {}) or {}, db)



def resume_mode_mismatch(plan, current_mode, mode_info=None):
    """L-22: None when an interrupted (`applying`) plan may be resumed in the current effective mode, else the
    next step for the person ("把模式改回 <plan mode> 后再 --resume"). The plan's stage rules (leaf pages, new-page
    limits) were evaluated for plan["mode"]; the executor refuses a resume in any other mode (wb_apply._start_checks,
    also whitelist -> full), so cmd_resume refuses it before the challenge and `status` shows the hint."""
    pm = plan.get("mode")
    if pm == current_mode:
        return None
    if pm in ("whitelist", "full"):
        resume = cli_command("writeback", "apply", "--resume", plan["plan_id"])
        if mode_info and mode_info["reasons"]:
            reason = "；".join(mode_info["reasons"])
            status = cli_command("writeback", "status")
            return (f"{reason}。运行 `{status}`，先确认有效模式已恢复为此计划原来的 {pm}，"
                    f"再由用户本人运行 `{resume}`。")
        return f"用户本人核对后把模式改回 {pm}，再运行 `{resume}`"
    return "计划没有记录可写入的生成模式，不能续执行（计划保持 applying；需要时联系维护者）"


def _refuse_resume_mode(cfg, db, plan):
    """L-22: apply --resume of a plan whose mode differs from the current effective mode is refused BEFORE the
    challenge: no phrase shown, no confirmations row, no approvals file, no client, nothing under state/ written."""
    info = _mode(cfg, db)
    nxt = resume_mode_mismatch(plan, info["mode"], info)
    if nxt is None:
        return
    raise _Fail(f"计划 {plan.get('plan_id')} 生成时的模式为 {plan.get('mode') or '（未记录）'}，当前有效模式为 "
                f"{info['mode']}：续执行只能在生成计划时的模式下进行，"
                f"未显示挑战短语、未记录确认、未发送任何请求；{nxt}", EXIT_REFUSED)


def _scan_unregistered(paths, cfg, db=None):
    """L-17 at apply time: renames / moves of a FRESH read-only vault scan that no refresh has registered yet
    (wb_plan.unregistered_renames, with the publisher's hold set: L-20a). A scan that cannot run refuses (fail
    closed): without it nobody can tell whether the adoption after the writes would verify."""
    import wb_scan
    try:
        res = wb_scan.scan(paths.vault, paths.export_db, paths.wb_db if os.path.exists(paths.wb_db) else None, cfg)
    except Exception as exc:  # noqa: BLE001
        raise _Fail(f"库扫描失败（{type(exc).__name__}）：无法确认没有尚未登记的本地改名/移动，未写入任何内容",
                    EXIT_REFUSED) from None
    return wb_plan.unregistered_renames(res, _registration_holds(paths, db), wb_plan.export_parents(paths.export_db))


def _refuse_unregistered_renames(paths, cfg, plan, items=(), *, scan=True, db=None, resume=False):
    """L-17: no apply while a local rename / move is unregistered — neither of a plan built in that state (its
    items are all non-executable) nor of an older plan applied after a rename happened (fresh scan; scan=False
    checks the plan only: cmd_apply before the challenge, the one full-vault scan runs in confirm_and_execute)."""
    found = list(plan.get("unregistered_renames") or [])
    flagged = any(wb_plan.UNREGISTERED_RENAME in (it.get("blocked_by") or []) for it in items)
    if not found and not flagged and scan:
        found = _scan_unregistered(paths, cfg, db)
    if found or flagged:
        detail = "；".join(_safe(x) for x in wb_plan.unregistered_lines(found, 5))
        tail = (f"；计划 {plan.get('plan_id')} 保持 applying：登记后运行 {cli_command('writeback', 'apply', '--resume', plan.get('plan_id'))} "
                "续执行（不必重新生成计划）" if resume else "")
        raise _Fail(f"{wb_plan.UNREGISTERED_RENAME}：{wb_plan.UNREGISTERED_RENAME_HINT}；未写入任何内容" +
                    (f"（{detail}）" if detail else "") + tail, EXIT_REFUSED)


def confirm_and_execute(paths, cfg, db, plan, plan_sha, items, selection, *, tty, os_user, client_factory,
                        executor=None, blobs_dir=None, sidebar=None):
    """After a successful challenge phrase: confirmations + approvals JSON, CAS draft->confirmed, decisions
    'confirmed', then the executor (wb_apply.execute_confirmed_plan). `client_factory()` -> (write, read)."""
    pid = plan["plan_id"]
    ids = [it["item_id"] for it in items]
    _plan_mode_matches(cfg, db, plan)
    # L-17: the fresh read-only vault scan (after the phrase, so a rename made while the challenge was on screen
    # counts too), before any confirmation row, client or executor
    _refuse_unregistered_renames(paths, cfg, plan, items, db=db)
    record = record_confirmation(paths, db, pid, plan_sha, ids, scope="apply", tty=tty, os_user=os_user,
                                 selection=selection.as_dict())
    if not wb_state.cas_plan_status(db, pid, "draft", "confirmed"):
        raise _Fail("计划状态已不是 draft（并发操作？），未执行任何写入", EXIT_REFUSED)
    with wb_state.transaction(db):
        for iid in ids:
            db.execute("UPDATE plan_items SET decision='confirmed' WHERE plan_id=? AND item_id=? AND decision='pending'",
                       (pid, iid))
    try:
        wc, rc = client_factory()
    except Exception as exc:  # noqa: BLE001 - TokenError, OSError ...: r1-operability-01
        # the plan is already confirmed (and confirmed plans cannot go back to draft): close it with the reason so
        # nothing stays half-confirmed; nothing was sent
        why = f"写回客户端无法建立（{type(exc).__name__}：{exc}）"
        wb_state.cas_plan_status(db, pid, "confirmed", "aborted", reason=why[:300])
        raise _Fail(why + f"；没有向 Notion 发送任何请求，计划已关闭（aborted）：修正后重新运行 {cli_command('writeback', 'plan')}",
                    EXIT_REFUSED) from None
    ex = executor or _apply_module().execute_confirmed_plan
    extra = {k: v for k, v in (("blobs_dir", blobs_dir), ("sidebar", sidebar)) if v}
    return ex(pid, selection, record, client=wc, read_client=rc, cfg=cfg, db=db, work=paths.work, vault=paths.vault,
              cfg_path=paths.config, **extra)


# ============================================================================================ VIEW helpers


def paused_rounds(paths):
    """r1-user-journey-04: unreleased paused_for_review refresh rounds (state/paused_runs.json, status paused),
    newest first: [{run_id, paused_at, reasons}]. Read-only."""
    d = wb_plan._read_json(paths.paused_runs, {}) or {}
    runs = d.get("runs") if isinstance(d, dict) else None
    out = []
    for rid, e in (runs or {}).items():
        if isinstance(e, dict) and e.get("status") == "paused":
            reasons = [str(x) for x in (e.get("reasons") or [])]
            partial = bool(e.get("partially_applied")) or any(r.partition(":")[0] == "apply_race" for r in reasons)
            out.append({"run_id": rid, "paused_at": e.get("paused_at"),
                        "partially_applied": partial, "reasons": reasons[:5]})
    return sorted(out, key=lambda x: str(x.get("paused_at") or ""), reverse=True)


def paused_round_lines(rounds):
    L = []
    for r in rounds[:3]:
        L.append(f"注意：refresh 轮次 {r['run_id']} 已暂停等待人工审阅（paused_for_review，自 {r.get('paused_at')}）："
                 + "；".join(_safe(x) for x in r["reasons"]))
        if r.get("partially_applied") or any(str(reason).partition(":")[0] == "apply_race" for reason in r["reasons"]):
            L.append(f"  本轮发布已中止。核对 reports/{r['run_id']}/publish_journal.jsonl 中实际写入的文件，"
                     "保留当前编辑，等待下一轮刷新。")
            continue
        L.append(f"  审阅 Notion_Obsidian/运行记录/{r['run_id']}-已暂停.md 后：放行 {cli_command('refresh', '--approve-run', r['run_id'])}，或丢弃 {cli_command('refresh', '--discard-run', r['run_id'])}")
        L.append("  暂停期间执行的写回只采纳计划内的笔记（--only-ids）；写回会改变已发布状态，之后放行该轮次通常会被拒绝"
                 f"（需要重新运行 {cli_command('refresh', '--mode', 'incremental')}）。")
    return L


def _block_head(code):
    return str(code).split("：", 1)[0].split(":", 1)[0].split("（", 1)[0]


def plan_next_steps(plan, plan_id, sha12, mode_info=None):
    """r1-user-journey-02 / r1-operability-06: what `plan` prints after the counts -- why items are not executable
    (grouped), plan-level blockers, and a next step that fits the plan (no apply hint when nothing can be applied,
    the right selector per category, resolve for conflicts, how to enable writing in mode plan)."""
    L = []
    items = plan.get("items") or []
    blocked = collections.OrderedDict()
    for it in items:
        if it.get("executable") or it["category"] == "C":
            continue
        for code in it.get("blocked_by") or ["不可执行"]:
            blocked.setdefault(_block_head(code), {"n": 0, "text": wb_plan.blocked_text(code)})["n"] += 1
    sync = (plan.get("sync") or {}).get("plan_level")
    if sync:
        L.append(f"注意：{_safe(sync)}——{wb_plan.plan_level_hint('SYNC_SETTLING')}")
    for head, e in blocked.items():
        if head == "SYNC_SETTLING" and sync:
            continue
        L.append(f"不可执行 {e['n']} 项：{_safe(e['text'])}")
    refused = collections.Counter(r.get("class") for r in plan.get("refused") or [])
    if refused:
        L.append("拒绝写回（只报告）：" + "，".join(f"{k} {v}" for k, v in refused.most_common()) +
                 f"（原因与建议见 {cli_command('writeback', 'show', '--plan', plan_id)}）")
    for entry in plan.get("refused") or []:
        if entry.get("class") == "AUTO_SYNC_HOLD":
            L.append(f"因自动同步暂停而拒绝 {_safe(entry.get('rel_path') or '')}：{_safe(entry.get('reason') or '')}。"
                     f"下一步：{_safe(entry.get('hint') or '')}")
    conflicts = [it["item_id"] for it in items if it["category"] == "C"]
    if conflicts:
        L.append(f"冲突 {len(conflicts)} 项只能用 resolve 处理：{wb_plan.conflict_steps(plan_id, conflicts[0])}")
    exe = [it for it in items if it.get("executable")]
    if plan.get("mode") not in ("whitelist", "full"):
        L.append(f"本计划只是预览（mode={plan.get('mode')}）。" + _plan_mode_guidance(mode_info)
                 + f"查看预览：{cli_command('writeback', 'show', '--plan', plan_id)}")
        return L
    if not exe:
        L.append(f"下一步：本计划没有可执行项，不需要 apply。处理上面的原因后重新运行 {cli_command('writeback', 'plan')}；"
                 f"详情：{cli_command('writeback', 'show', '--plan', plan_id)}")
        return L
    # Renaming another note changes rendered link labels during adoption (W6). Keep the items of
    # each note together and let a fresh plan account for the new rendering before the next group.
    selected = exe
    rename_notes = {wb_plan.note_key(it) for it in exe if it["category"] == "T"}
    body_notes = {wb_plan.note_key(it) for it in exe if it["category"] == "M"}
    if rename_notes and body_notes and len(rename_notes | body_notes) > 1:
        if body_notes - rename_notes:
            selected = [it for it in exe if wb_plan.note_key(it) not in rename_notes]
        else:
            first_note = next(wb_plan.note_key(it) for it in exe if it["category"] == "M")
            selected = [it for it in exe if wb_plan.note_key(it) == first_note]
        scope = plan.get("scope")
        scope_args = ("--scope", scope) if scope and scope != "全库" else ()
        next_plan = cli_command("writeback", "plan", *scope_args)
        L.append(f"本批含正文修改和其他笔记的改名。改名会改变笔记里的链接显示，本次先确认以下 {len(selected)} 项。"
                 f"完成后运行 {next_plan}，核对新计划的编号、校验值及剩余项目，再按新提示确认。")
    sel = collections.OrderedDict()
    for it in selected:
        sel.setdefault(wb_plan._flag_for(it["category"]), []).append(it["item_id"])
    selection = [arg for flag, ids in sel.items() for arg in (flag, ",".join(ids))]
    apply = cli_command("writeback", "apply", "--plan", plan_id, "--sha", sha12, *selection)
    L.append(f"下一步：{cli_command('writeback', 'show', '--plan', plan_id)}；确认写入（可执行 {len(exe)} 项，可只选其中一部分；同一笔记的项须一起选）："
             f"{apply}（需本人在终端输入挑战短语）")
    return L


# ============================================================================================ VIEW commands
def cmd_status(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    db = _db_ro(paths)
    try:
        st = status_data(paths, cfg, db, scan=not args.no_scan)
    finally:
        if db is not None:
            db.close()
    if args.json:
        _out(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        return EXIT_OK
    for line in status_text(st):
        _out(line)
    return EXIT_OK


STATUS_DECIDED_PLANS = 5


def status_data(paths, cfg, db, scan=True, now=None):
    """Everything `status` prints (R-152): mode, members/creation_state, plans, pending local changes by class,
    holds, name differences, BLOCKED pages, rejected items, backlog, breaker acknowledgement."""
    now = now or datetime.datetime.now().astimezone()
    info = _mode(cfg, db)
    out = {"mode": info, "work": paths.work, "vault": paths.vault}

    def q(sql, args=()):
        if db is None:
            return []
        try:
            return [dict(r) for r in db.execute(sql, args)]
        except sqlite3.Error:
            return []
    cs = q("SELECT * FROM creation_state ORDER BY rowid DESC LIMIT 1")
    out["members"] = {"configured": cfg.get("workspace_members"), "last": cs[0] if cs else None,
                      "creation_state": (cs[0]["state"] if cs else None) or ("unlimited" if cfg.get(
                          "workspace_members") == 1 else "blocked_assumed")}
    allp = q("SELECT plan_id, created_at, expires_at, status, status_reason, scope, counts_json FROM plans "
             "ORDER BY created_at DESC LIMIT 500")
    # r1-user-journey-15: open plans (draft / confirmed / applying) + the last few decided ones; older ones counted
    open_st = ("draft", "confirmed", "applying")
    plans = [p for p in allp if p["status"] in open_st]
    plans += [p for p in allp if p["status"] not in open_st][:STATUS_DECIDED_PLANS]
    plans.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    out["plans_hidden"] = len(allp) - len(plans)
    for p in plans:
        exp = wb_plan._parse_iso(p.get("expires_at"))
        if p["status"] in ("draft", "confirmed") and exp is not None and exp < now:
            p["status"] = "expired（待标记）"
        try:
            p["counts"] = json.loads(p.pop("counts_json") or "{}")
        except ValueError:
            p["counts"] = {}
    out["plans"] = plans
    # L-22: interrupted plans whose `apply --resume` is refused because the effective mode changed since planning
    out["resume_refused"] = []
    for r in q("SELECT plan_id FROM plans WHERE status='applying' ORDER BY created_at"):
        try:
            pl, _sha = wb_plan.load_plan(r["plan_id"], work=paths, db=db)
        except (WbError, sqlite3.Error, OSError, ValueError) as exc:
            out["resume_refused"].append({"plan_id": r["plan_id"], "plan_mode": None, "mode": info["mode"],
                                          "hint": f"plan.json 无法读取（{exc}）：不能续执行"})
            continue
        nxt = resume_mode_mismatch(pl, info["mode"], info)
        if nxt:
            out["resume_refused"].append({"plan_id": r["plan_id"], "plan_mode": pl.get("mode"), "mode": info["mode"],
                                          "hint": nxt})
    for p in plans:
        rr = next((x for x in out["resume_refused"] if x["plan_id"] == p["plan_id"]), None)
        if rr:
            p["resume_refused"] = rr["hint"]
    last_plan = None
    if db is not None:
        try:
            last_plan = wb_state.get_meta_json(db, "last_plan", None)
        except (sqlite3.Error, ValueError):
            last_plan = None
    out["last_plan"] = last_plan
    pending = {}
    if scan and paths.vault and os.path.isdir(paths.vault) and info["mode"] != "off":
        try:
            import wb_scan
            res = wb_scan.scan(paths.vault, paths.export_db, paths.wb_db if os.path.exists(paths.wb_db) else None, cfg)
            pending = {"source": "scan", "at": res.get("scanned_at"),
                       "待写回的本地修改 local_modified": wb_plan.pending_modified(paths, db, res),
                       "本地新增 local_new": [x for x in res.get("local_new", []) if x.endswith(".md")],
                       "本地删除 missing": [m["rel"] for m in res.get("missing", []) if m["rel"].endswith(".md")],
                       "本地改名 renamed": [f"{r['old_rel']} → {r['new_rel']}" for r in res.get("renamed_same_folder", [])],
                       "跨文件夹移动 moved": [f"{r['old_rel']} → {r['new_rel']}" for r in res.get("moved_other_folder", [])],
                       "Sync 冲突副本": [s["rel"] for s in res.get("sync_conflict_copies", [])],
                       "frontmatter 错误": [e["rel"] for e in res.get("frontmatter_errors", [])]}
            # L-17: the same notice `plan` gives (every item of a plan built now would be non-executable)
            # (L-20a (ii): registered by the publisher's / adoption's hold set, like plan and apply)
            out["unregistered_renames"] = wb_plan.unregistered_renames(
                res, _registration_holds(paths, db), wb_plan.export_parents(paths.export_db))
        except Exception as exc:  # noqa: BLE001
            pending = {"source": "scan", "error": f"{type(exc).__name__}: {exc}"}
    else:
        latest = _latest_refresh_scan(paths)
        if latest:
            res = wb_plan._read_json(latest, {}) or {}
            pending = {"source": latest, "counts": res.get("counts")}
    out["pending_local_changes"] = pending
    out["holds"] = {"pending_deletes": q("SELECT notion_id, rel_path, detected_run, ttl_until FROM pending_deletes "
                                         "WHERE status='hold' ORDER BY rowid"),
                    "auto_sync_hold": wb_plan._read_json(paths.holds, {}) or {}}
    out["name_overrides"] = q("SELECT notion_id, old_rel, local_rel, since FROM name_overrides WHERE status='open'")
    out["blocked_pages"] = q("SELECT notion_id, state, reason, since FROM page_state WHERE state IN "
                             "('BLOCKED','PARTIAL_CREATE')")
    out["rejected"] = q("SELECT id, notion_id, rel_path, item_category, reason, created_at, plan_id, restore_notion, "
                        "l_sha FROM rejections WHERE active=1 ORDER BY id")
    for r in out["rejected"]:            # r1-user-journey-15: a rejection applies only while the file is unchanged
        r["in_effect"] = None
        if r.get("l_sha") and r.get("rel_path") and paths.vault:
            try:
                r["in_effect"] = wb_common.sha256_file(wb_common.safe_join(paths.vault, r["rel_path"])) == r["l_sha"]
            except (OSError, ValueError):
                r["in_effect"] = False
    # r1-user-journey-04: refresh rounds waiting for --approve-run / --discard-run
    out["paused_rounds"] = paused_rounds(paths)
    # r1-user-journey-06 / R-183: page deletions (D) and their next-round verification (WB 6.6)
    out["deletions"] = q("SELECT plan_id, item_id, notion_id, rel_path, verify_status, reason FROM plan_items WHERE "
                         "category='D' AND decision='executed' AND verify_status IN ('pending_next_round','needs_manual')"
                         " ORDER BY plan_id DESC LIMIT 50")
    out["deletions_verified"] = q("SELECT plan_id, item_id, rel_path FROM plan_items WHERE category='D' AND "
                                  "verify_status='verified' ORDER BY plan_id DESC LIMIT 5")
    if out["members"]["last"] is None or out["members"]["last"].get("members") is None:
        for p in plans:                    # r1-user-journey-15: the latest plan's live read
            try:
                pl, _sha = wb_plan.load_plan(p["plan_id"], work=paths, db=db)
            except (WbError, sqlite3.Error, OSError, ValueError):
                continue
            if pl.get("members") is not None:
                out["members"]["from_plan"] = {"plan_id": p["plan_id"], "members": pl.get("members")}
            break
    bl = {}
    if db is not None:
        try:
            bl = wb_state.get_meta_json(db, "initial_backlog", {}) or {}
        except (sqlite3.Error, ValueError):
            bl = {}
    out["initial_backlog"] = {k: v for k, v in bl.items() if not v.get("acked_at")}
    if db is not None:
        try:
            out["anomaly_ack"] = wb_state.get_meta_json(db, "anomaly_ack", None)
            out["forced_mode"] = wb_state.get_meta(db, "forced_mode")
            out["forced_mode_reason"] = wb_state.get_meta(db, "forced_mode_reason")
        except (sqlite3.Error, ValueError):
            pass
    out["stop_file"] = os.path.exists(paths.stop_file)
    return out


def _latest_refresh_scan(paths):
    best = None
    try:
        for run in os.listdir(paths.reports):
            p = os.path.join(paths.reports, run, "wb_scan.json")
            if os.path.isfile(p) and (best is None or os.path.getmtime(p) > os.path.getmtime(best)):
                best = p
    except OSError:
        return None
    return best


def status_text(st):
    m = st["mode"]
    L = ["== 写回状态 / write-back status ==",
         f"模式 mode：{m['mode']}（配置 {m['configured']}）" + ("；" + "；".join(m["reasons"]) if m["reasons"] else "")]
    if st.get("forced_mode"):
        L.append(f"自动降级 forced_mode：{st['forced_mode']}")
        if str(st.get("forced_mode_reason") or "").startswith("audit-scope"):       # r1-user-journey-01
            L.append("  " + _apply_module().audit_recovery_hint())
    if st.get("stop_file"):
        L.append("急停文件 state/WRITEBACK_STOP 存在：任何写入都会停止")
    if st.get("unregistered_renames"):
        L.append(f"注意 {wb_plan.UNREGISTERED_RENAME}：{wb_plan.UNREGISTERED_RENAME_HINT}"
                 "（现在生成的计划所有条目都不可执行；登记前任何 apply 都会被拒绝）")
        L += ["  - " + _safe(x) for x in wb_plan.unregistered_lines(st["unregistered_renames"])]
    for rr in st.get("resume_refused") or []:           # L-22
        L.append(f"注意：中断的计划 {rr['plan_id']}（applying）生成时的模式为 {rr.get('plan_mode') or '（未记录）'}，"
                 f"当前有效模式为 {rr.get('mode')}：续执行会被拒绝；{_safe(rr['hint'])}")
    L += paused_round_lines(st.get("paused_rounds") or [])
    mem = st["members"]
    last = mem.get("last") or {}
    fp = mem.get("from_plan") or {}
    if last.get("members") is not None:
        mtxt = str(last["members"])
    elif fp.get("members") is not None:
        mtxt = f"{fp['members']}（计划 {fp['plan_id']} 生成时的实时读取）"
    else:
        mtxt = wb_plan.UNKNOWN
    L.append(f"成员数 members：{mtxt}（配置 {mem['configured']}）；块额度 creation_state：{mem['creation_state']}")
    L.append("")
    L.append("-- 计划 plans --（未结束的计划 + 最近 " + str(STATUS_DECIDED_PLANS) + " 个已结束的）")
    if not st["plans"]:
        L.append("（无）")
    for p in st["plans"]:
        c = p.get("counts") or {}
        L.append(f"{p['plan_id']}  {p['status']}  过期 {p['expires_at']}  范围 {_safe(p.get('scope'))}  " +
                 " ".join(f"{k}{c.get(k, 0)}" for k in wb_plan.CATEGORIES) + f" 拒绝{c.get('refused', 0)}" +
                 (f"  ({_safe(p['status_reason'])})" if p.get("status_reason") else "") +
                 (f"  续执行被拒绝：{_safe(p['resume_refused'])}" if p.get("resume_refused") else ""))
    if st.get("plans_hidden"):
        L.append(f"（另有 {st['plans_hidden']} 个更早的已结束计划未列出）")
    if st.get("deletions") or st.get("deletions_verified"):
        L.append("-- 页面删除 D 的下一轮核验 --")
        for d in st.get("deletions") or []:
            if d.get("verify_status") == "needs_manual":
                L.append(f"  {d['plan_id']} {d['item_id']} {_safe(d.get('rel_path') or '')}：需人工处理（needs_manual）"
                         f"{'：' + _safe(d['reason']) if d.get('reason') else ''}；需要恢复时运行 {cli_command('writeback', 'rollback', '--plan', d['plan_id'], '--items', d['item_id'])}")
            else:
                L.append(f"  {d['plan_id']} {d['item_id']} {_safe(d.get('rel_path') or '')}：等待下一轮 refresh 发布后、"
                         "下一次 plan 时核验（pending_next_round）")
        for d in st.get("deletions_verified") or []:
            L.append(f"  {d['plan_id']} {d['item_id']} {_safe(d.get('rel_path') or '')}：已核验（verified）")
    L.append("")
    L.append("-- 待确认的本地改动（按类） pending local changes by class --")
    pend = st.get("pending_local_changes") or {}
    if pend.get("error"):
        L.append(f"扫描失败：{_safe(pend['error'])}")
    elif pend.get("counts") is not None:
        L.append(f"（上次 refresh 扫描 {pend['source']}）" + json.dumps(pend["counts"], ensure_ascii=False))
    else:
        any_ = False
        for k, v in pend.items():
            if isinstance(v, list):
                L.append(f"{k}：{len(v)}")
                for x in v[:20]:
                    L.append(f"  - {_safe(x)}")
                any_ = any_ or bool(v)
        if not pend:
            L.append("（模式 off 或没有库路径：未扫描）")
    lp = st.get("last_plan")
    if lp:                                  # r1-user-journey-15: counts in words, not raw JSON
        c = lp.get("counts") or {}
        rb = lp.get("refused_by_class") or {}
        L.append(f"上次计划 {lp.get('plan_id')}：" + " ".join(f"{k} {c.get(k, 0)}" for k in wb_plan.CATEGORIES) +
                 f"；拒绝 {c.get('refused', sum(rb.values()) if rb else 0)}" +
                 (f"（{'，'.join(f'{k} {v}' for k, v in sorted(rb.items()))}）" if rb else "") +
                 f"；已拒绝 {c.get('rejected', 0)}")
    L.append("")
    L.append("-- hold（待决删除 / 自动同步暂停） --")
    for h in st["holds"]["pending_deletes"]:
        L.append(f"  删除 hold：{_safe(h['rel_path'])}  到期 {h['ttl_until']}  （{h['notion_id']}）")
    for nid, h in sorted((st["holds"]["auto_sync_hold"] or {}).items()):
        L.append(f"  自动同步暂停：{nid}  {_safe(h.get('reason'))}  自 {h.get('since')}")
        if isinstance(h, dict) and str(h.get("reason") or "").startswith("override_rejected"):
            L.append(f"    （{wb_plan.OVERRIDE_REJECTED_HINT}）")       # waiting for the sync does not clear it
    if not st["holds"]["pending_deletes"] and not st["holds"]["auto_sync_hold"]:
        L.append("（无）")
    L.append("-- 命名差异 name differences（本地改名未写回，Notion 标题保持不变） --")
    for o in st["name_overrides"] or []:
        L.append(f"  {_safe(o['old_rel'])} → {_safe(o['local_rel'])}  自 {o.get('since')}")
    if not st["name_overrides"]:
        L.append("（无）")
    L.append("-- BLOCKED / PARTIAL_CREATE 页面 --")
    for b in st["blocked_pages"] or []:
        L.append(f"  {b['notion_id']}  {b['state']}  {_safe(b.get('reason') or '')}  自 {b.get('since')}"
                 f"  → {cli_command('writeback', 'unblock', '--page', b['notion_id'])}")
    if not st["blocked_pages"]:
        L.append("（无）")
    L.append("-- 已拒绝 rejected --")
    for r in st["rejected"] or []:
        L.append(f"  #{r['id']} {r['item_category']} {_safe(r.get('rel_path') or r.get('notion_id') or '')}  "
                 f"{_safe(r.get('reason') or '')}  ({r.get('plan_id')}, {r.get('created_at')})" +
                 ("  （文件已再次改动：当前不生效，新的改动会重新列入计划）" if r.get("in_effect") is False else ""))
    if not st["rejected"]:
        L.append("（无）")
    if st.get("initial_backlog"):
        L.append("-- INITIAL_BACKLOG（启用前已存在的本地差异，需 ack-backlog） --")
        for nid, e in st["initial_backlog"].items():
            L.append(f"  {nid}  {e.get('detected_at')}")
    if st.get("anomaly_ack"):
        L.append(f"熔断确认 ack-anomaly：{json.dumps(st['anomaly_ack'], ensure_ascii=False)}")
    return L


def cmd_plan(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    if cfg.get("mode") == "off":
        _out(MODE_OFF_TEXT)          # WB §15: only the explanation; no state file is created or opened
        return EXIT_OK
    _need_vault(paths)
    db = _db_rw(paths)
    try:
        info = _mode(cfg, db)
        try:
            lk = wb_locks.lock("refresh", state_dir=paths.state)
            lk.__enter__()
        except wb_locks.LockBusy:
            raise _Fail("refresh 正在运行，稍后重试（refresh is running; retry later）", EXIT_REFUSED) from None
        try:
            wb_plan.expire_plans(db)
            rc = _read_client(args, paths, cfg)
            ctx = wb_plan.PlanContext(paths, cfg, db, read_client=rc, mode=info["mode"],
                                      snapshot=getattr(args, "snapshot", None))
            plan, preview = wb_plan.build_plan(ctx, scope=args.scope)
            res = wb_plan.create_plan(ctx, plan, preview)
            # WB 6.6 / R-183: deletions executed before the last refresh publish are verified now (read-only)
            try:
                verified = _apply_module().verify_pending_deletes(
                    db, read_client=rc, work=paths.work, vault=paths.vault, nid_index=ctx.scan().get("nid_index"))
            except Exception as exc:  # noqa: BLE001 - never fails the plan; checked again next time
                verified = [{"item_id": "?", "plan_id": "?", "status": "pending_next_round",
                             "reasons": [f"核验出错（{type(exc).__name__}）：下一次 plan 再核验"]}]
        finally:
            lk.__exit__(None, None, None)
    finally:
        db.close()
    c = plan["counts"]
    _out(f"计划 plan_id：{res['plan_id']}")
    _out(f"sha（apply --sha 用前 12 位）：{res['plan_sha256'][:12]}")
    _out("数量：" + " ".join(f"{k} {c.get(k, 0)}" for k in wb_plan.CATEGORIES) +
         f"  拒绝 {c.get('refused', 0)}  已拒绝 {c.get('rejected', 0)}  可执行 {plan['summary']['executable']}")
    _out(f"过期时间：{plan['expires_at']}；模式：{plan['mode']}")
    _out(f"预览：{os.path.join(res['dir'], 'preview.md')}")
    if res.get("vault_copy"):
        _out(f"库内只读副本（不能作为确认依据）：{res['vault_copy']}")
    if res["superseded"]:
        _out("被取代的旧计划：" + ", ".join(res["superseded"]))
    if plan.get("unregistered_renames"):
        _out(f"注意 {wb_plan.UNREGISTERED_RENAME}：{wb_plan.UNREGISTERED_RENAME_HINT}（本计划所有条目都不可执行）")
        for x in wb_plan.unregistered_lines(plan["unregistered_renames"]):
            _out("  - " + _safe(x))
    for w in ctx.warnings:
        _out(f"警告：{_safe(w)}")
    if plan.get("members") is None:                   # r1-operability-10: say WHY the count is unknown
        why = (plan.get("summary") or {}).get("members_error")
        _out("警告：工作区成员数读取失败" + (f"（{_safe(why)}）" if why else "") + "：按多成员处理；如果是网络或 Notion "
             f"连接问题，检查网络 / 读连接后重新运行 {cli_command('writeback', 'plan')}")
    if plan["creation_state"] != "unlimited":
        _out(f"警告：块额度状态 {plan['creation_state']}：{wb_plan.QUOTA_HINT}")
    for line in paused_round_lines(paused_rounds(paths)):         # r1-user-journey-04
        _out(line)
    for v in verified:                                            # r1-user-journey-06
        if v["status"] == "verified":
            _out(f"页面删除核验：{v['plan_id']} {v['item_id']} {_safe(v.get('rel_path') or '')} 已核验（verified）")
        elif v["status"] == "needs_manual":
            _out(f"页面删除核验：{v['plan_id']} {v['item_id']} {_safe(v.get('rel_path') or '')} 需人工处理（needs_manual）："
                 + "；".join(_safe(x) for x in v["reasons"]) + f"；需要恢复时运行 {cli_command('writeback', 'rollback', '--plan', v['plan_id'], '--items', v['item_id'])}")
        else:
            _out(f"页面删除核验：{v['item_id']} 暂未完成：" + "；".join(_safe(x) for x in v["reasons"]))
    for line in plan_next_steps(plan, res["plan_id"], res["plan_sha256"][:12], info):
        _out(line)
    return EXIT_OK


def cmd_show(args):
    paths = _paths(args)
    db = _db_ro(paths)
    if db is None:
        raise _Fail(f"还没有任何计划（state/writeback.db 不存在）；先运行 {cli_command('writeback', 'plan')}", EXIT_REFUSED)
    try:
        plan, sha = _load(paths, db, args.plan)
        st, row = _status_now(db, args.plan)
        dec = wb_plan.item_decisions(db, args.plan)
    finally:
        db.close()
    _out(f"计划 {plan['plan_id']}  状态 {st}  过期 {plan['expires_at']}  sha12 {sha[:12]}")
    if args.item:
        it = next((x for x in plan["items"] if x["item_id"] == args.item.upper()), None)
        if it is None:
            raise _Fail(f"{args.item} 不在计划中", EXIT_REFUSED)
        _out(f"决定 decision：{dec.get(it['item_id'], 'pending')}")
        if paths.vault and it.get("rel_path") and it.get("l_sha"):
            try:
                cur = wb_common.sha256_file(wb_common.safe_join(paths.vault, it["rel_path"]))
                if cur != it["l_sha"]:
                    _out("注意：库内文件在计划后又被修改（与计划时的 L sha 不同），apply 时该项会被跳过")
            except (OSError, ValueError):
                pass
        _out(_safe_block(wb_plan.show_item(plan, it["item_id"])))
        return EXIT_OK
    # r1-never-unapproved-07: the preview is rendered from the sha-verified plan.json (what apply would send), never
    # printed from reports/.../preview.md, which no hash covers; a preview.md that differs is pointed out
    text = wb_plan.render_preview(plan, sha12=sha[:12])
    _out(_safe_block(text))
    p = os.path.join(paths.plan_dir(args.plan), "preview.md")
    try:
        with open(p, encoding="utf-8") as fh:
            disk = fh.read()
    except OSError:
        disk = None
    if disk is not None and disk != text:
        _out(f"注意：{p} 与已校验的计划不一致（文件被改动过？）；以上内容按 plan.json（sha {sha[:12]}）生成，以此为准")
    return EXIT_OK


def _safe_block(text):
    return "\n".join(_safe(x) for x in text.split("\n"))


def cmd_whitelist_check(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    db = _db_ro(paths)
    try:
        ctx = wb_plan.PlanContext(paths, cfg, db, read_client=None, mode="plan",
                                  snapshot=getattr(args, "snapshot", None))
        rc = None
        try:
            rc = _read_client(args, paths, cfg)
        except Exception as exc:  # noqa: BLE001
            _out(f"（读客户端不可用，跳过实时检查：{type(exc).__name__}）")
        rows = whitelist_report(ctx, rc)
    finally:
        if db is not None:
            db.close()
    if args.json:
        _out(json.dumps(rows, ensure_ascii=False, indent=1))
        return EXIT_OK
    _out("== 白名单页面的实际可写范围（共享给写回集成后，集成可读写该页面及其全部后代） ==")
    if not rows:
        _out("（白名单为空）")
    for r in rows:
        _out(f"{r['id']}  {_safe(r.get('title') or wb_plan.UNKNOWN)}  类型 {r['kind']}  后代页面 {r['descendant_pages']}  "
             f"内联数据库 {r['inline_databases']}  行 {r['rows']}  叶子 {'是' if r['leaf'] else '否'}  "
             f"读连接可读 {r['live']}")
    return EXIT_OK


def whitelist_report(ctx, rc=None):
    cfg = ctx.cfg
    ids = list(dict.fromkeys(list(ctx.scope_pages()) + list(cfg.get("whitelist_database_ids") or [])))
    out = []
    try:
        ex = ctx.ex
    except WbError as exc:
        raise _Fail(str(exc), EXIT_REFUSED) from None
    for nid in ids:
        n = ex.nodes.get(nid)
        pages = dbs = rows = 0
        stack = list((n or {}).get("children") or [])
        while stack:
            c = ex.nodes.get(stack.pop())
            if c is None:
                continue
            if c["kind"] == "database":
                dbs += 1
            elif c["kind"] == "row":
                rows += 1
            else:
                pages += 1
            stack.extend(c.get("children") or [])
        live = wb_plan.UNKNOWN
        if rc is not None:
            try:
                if n is not None and n["kind"] == "database":
                    rc.retrieve_database(nid)
                else:
                    rc.retrieve_page(nid)
                live = "是"
            except Exception as exc:  # noqa: BLE001
                live = f"否（{getattr(exc, 'status', type(exc).__name__)}）"
        out.append({"id": nid, "title": (n or {}).get("title"), "kind": (n or {}).get("kind") or wb_plan.UNKNOWN,
                    "descendant_pages": pages if n else wb_plan.UNKNOWN, "inline_databases": dbs if n else wb_plan.UNKNOWN,
                    "rows": rows if n else wb_plan.UNKNOWN, "leaf": bool(n) and not (n.get("children")), "live": live})
    return out


# ============================================================================================ APPROVE
def cmd_apply(args):
    if args.resume:
        return cmd_resume(args)
    if not args.plan or not args.sha:
        raise _Fail(f"写入需要计划编号、SHA 前 12 位和所选项；请查看 {cli_command('writeback', 'apply', '--help')}，"
                    f"或用 {cli_command('writeback', 'plan')} 生成待审阅的计划及其完整命令", EXIT_REFUSED)
    paths = _paths(args)
    cfg = _cfg(paths)
    # nothing under state/ is opened before the human gate (a refused command leaves state/ byte-identical)
    _precheck_write_mode(cfg, "apply")
    if os.path.exists(paths.stop_file):
        raise _Fail("急停文件 state/WRITEBACK_STOP 存在：拒绝执行", EXIT_REFUSED)
    tty = _human("写回操作 apply")
    _precheck_token_file(args)
    db = _db_rw_existing(paths)
    try:
        _require_write_mode(cfg, db, "apply")
        plan, sha = _load(paths, db, args.plan)
        s12 = (args.sha or "").strip().lower()
        if len(s12) != 12 or not sha.startswith(s12):
            raise _Fail("--sha 与计划的 plan_sha256 前 12 位不符（计划已变化或输错），未写入任何内容", EXIT_REFUSED)
        wb_plan.expire_plans(db)
        st, _row = _status_now(db, args.plan)
        if st != "draft":
            raise _Fail(f"计划状态为 {st}（只有 draft 且未过期的计划可以确认；请重新运行 {cli_command('writeback', 'plan')}）", EXIT_REFUSED)
        _plan_mode_matches(cfg, db, plan)
        _check_plan_vault(paths, plan)
        try:
            sel = wb_plan.selection_from_args(plan, args)
            items = wb_plan.selected_items(plan, sel, wb_plan.item_decisions(db, args.plan))
        except WbError as exc:
            raise _Fail(str(exc), EXIT_REFUSED) from None
        # L-17: a plan built while a rename was unregistered is refused above (non-executable items) and here
        # (plan only, no scan); one built earlier is refused by the fresh vault scan in confirm_and_execute
        # (after the phrase, before any confirmation row): one full-vault scan per apply, not two
        _refuse_unregistered_renames(paths, cfg, plan, items, scan=False)
        lines = [f"计划 {plan['plan_id']}（sha {sha[:12]}）将执行以下 {len(items)} 项，其余项不会写入："]
        for it in items:
            lines += ["  " + _item_line(it)] + _item_details(it)   # confirm_phrase escapes each line
        if getattr(args, "all_safe", False):
            for e in wb_plan.all_safe_exclusions(plan):
                lines.append(f"  （--all-safe 未包含 {e['item_id']} {e.get('rel_path') or ''}：同一笔记还有 "
                             f"{','.join(e['with'])}，须一起显式选择）")
        lines += [f"剩余风险：{wb_plan.RESIDUAL_RISK}"]
        code = wb_gate.challenge_code()
        phrase = wb_plan.challenge_phrase(plan["plan_id"], items, code)
        if not _phrase(phrase, lines):
            raise _Fail("短语不符：已取消，未写入任何内容", EXIT_FAIL)
        res = confirm_and_execute(paths, cfg, db, plan, sha, items, sel, tty=tty, os_user=_os_user(),
                                  client_factory=lambda: _make_clients(args, paths, cfg, "apply"),
                                  blobs_dir=getattr(args, "blobs", None), sidebar=getattr(args, "sidebar", None))
    finally:
        db.close()
    return _print_result(res)


def _print_result(res, kind="apply"):
    """The executor's result: the JSON summary, the W1-W10 line, then (r1-user-journey-03) a Chinese verdict for
    EVERY status, the reason of each item that was not done, the next step and where the full result is."""
    _out(json.dumps({k: res.get(k) for k in ("plan_id", "status", "reason", "executed", "skipped", "unknown",
                                              "blocked_pages", "result_md", "vault_record")},
                    ensure_ascii=False, indent=1, default=str)[:20000])
    w = res.get("w_checks") or {}
    if w:
        _out("W1–W10：" + " ".join(f"{k}={(v or {}).get('status', v) if isinstance(v, dict) else v}" for k, v in w.items()))
    st = res.get("status")
    pid = res.get("plan_id") or "<计划>"
    sent = res.get("requests")
    sent_txt = f"本次发送写请求 {sent} 个" if isinstance(sent, int) else ""
    for sk in (res.get("skipped") or [])[:10]:
        if isinstance(sk, dict) and sk.get("reason"):
            _out(f"  - {sk.get('item_id')}：{_safe(str(sk.get('status') or ''))} {_safe(str(sk.get('reason')))}")
    if kind == "rollback":
        nm = len(res.get("needs_manual") or [])
        if st == "applied":
            _out("回退完成（rollback applied）" + (f"；{nm} 项需人工检查（例如恢复的块位置可能变化，见回退记录）" if nm else "")
                 + "：库内笔记在下一轮 refresh 时按 Notion 当前版本更新")
            code = EXIT_OK
        elif st == "partially_applied":
            _out(f"回退部分完成，待人工处理（partially_applied）：{nm} 项需人工处理；{sent_txt}")
            code = EXIT_FAIL
        elif st == "aborted":
            _out(f"回退已中止（aborted）：{_safe(str(res.get('reason') or ''))}；{sent_txt}；处理后重新运行 {cli_command('writeback', 'rollback', '--plan', pid)}（会先显示预览）")
            code = EXIT_FAIL
        else:
            _out(f"回退被拒绝（refused），没有发送任何写请求：{_safe(str(res.get('reason') or ''))}")
            code = EXIT_REFUSED
    elif st == "applied":
        _out("完成，可验收（applied）")
        code = EXIT_OK
    elif st == "partially_applied":
        _out(f"部分完成，待人工处理（partially_applied）：见 result.md；需要时 {cli_command('writeback', 'rollback', '--plan', pid)}")
        code = EXIT_FAIL
    elif st == "aborted":
        _out(f"已中止（aborted）：{_safe(str(res.get('reason') or ''))}" + (f"；{sent_txt}" if sent_txt else "") +
             f"。下一步：检查上面的原因后重新运行 {cli_command('writeback', 'plan')}，再按新计划确认（冲突项按新计划列出的两种处理方式选择）")
        code = EXIT_FAIL
    else:
        _out(f"已拒绝（{st or 'refused'}），没有向 Notion 发送任何写请求：{_safe(str(res.get('reason') or ''))}。"
             f"下一步：按上面的原因处理（{cli_command('writeback', 'status')} 查看当前状态），然后重新运行 {cli_command('writeback', 'plan')}")
        code = EXIT_REFUSED if st == "refused" else EXIT_FAIL
    if res.get("result_md"):
        _out(f"完整结果：{res['result_md']}")
    return code


def _intent_items(db, plan_id):
    rows = db.execute("SELECT item_id, op, state FROM journal WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
    out = []
    for r in rows:
        if str(r["op"]).startswith("RB_") or r["op"] == "VAULT_FRONTMATTER":
            continue
        if r["state"] == "intent" and r["item_id"] not in out:
            out.append(r["item_id"])
    return out


def _journal_states(db, pid):
    """{state: count} of the plan's forward journal rows (rollback / frontmatter rows excluded)."""
    out = {}
    for r in db.execute("SELECT op, state FROM journal WHERE plan_id=?", (pid,)).fetchall():
        if str(r["op"]).startswith("RB_") or r["op"] == "VAULT_FRONTMATTER":
            continue
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out


def _close_unstarted(paths, db, pid):
    """An `applying` plan whose run stopped before any journal row was written (crash between the CAS
    confirmed->applying and the first intent): nothing was sent, nothing can be resumed. After the human gate and a
    y/N it is closed as aborted, under the writeback lock (never while an executor still runs it)."""
    sent = {k: v for k, v in _journal_states(db, pid).items() if k not in ("cancelled", "failed")}
    if sent:
        raise _Fail(f"journal 已有其他状态的记录 {sent}：不能直接关闭（用 {cli_command('writeback', 'apply', '--resume', pid)} 续执行并结束）", EXIT_REFUSED)
    _out(f"计划 {pid} 处于 applying，但 journal 中没有任何已发出或待发出的操作（运行在写入前中断）。")
    if not _yes(f"把它关闭为 aborted（不发送任何请求；之后需重新运行 {cli_command('writeback', 'plan')}）？"):
        _out("已取消。")
        return EXIT_FAIL
    try:
        lk = wb_locks.lock("writeback", state_dir=paths.state)
        lk.__enter__()
    except wb_locks.LockBusy:
        raise _Fail("另一个写回进程正在运行（writeback.lock 被占用）：未关闭", EXIT_REFUSED) from None
    try:
        if {k: v for k, v in _journal_states(db, pid).items() if k not in ("cancelled", "failed")}:
            raise _Fail("journal 已有新记录：未关闭", EXIT_REFUSED)
        if not wb_state.cas_plan_status(db, pid, "applying", "aborted", reason="closed_by_user:no_journal_writes"):
            raise _Fail("计划状态已变化：未关闭", EXIT_REFUSED)
    finally:
        lk.__exit__(None, None, None)
    _out(f"计划 {pid} 已关闭为 aborted（没有任何写入）。")
    return EXIT_OK


def cmd_resume(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    pid = args.resume
    _precheck_write_mode(cfg, "apply --resume")
    if os.path.exists(paths.stop_file):
        raise _Fail("急停文件 state/WRITEBACK_STOP 存在：拒绝执行", EXIT_REFUSED)
    tty = _human("写回操作 apply --resume")
    _precheck_token_file(args)
    db = _db_rw_existing(paths)
    try:
        plan, sha = _load(paths, db, pid)
        st, _row = _status_now(db, pid)
        if st != "applying":
            raise _Fail(f"只有 applying（中断）的计划可以 --resume；当前状态 {st}（aborted 等须重新运行 {cli_command('writeback', 'plan')}）", EXIT_REFUSED)
        # L-22: a plan-mode mismatch is refused here, before any prompt (challenge or the y/N of _close_unstarted)
        # and before any confirmation row / client: state/ stays byte-identical
        _refuse_resume_mode(cfg, db, plan)
        _require_write_mode(cfg, db, "apply --resume")
        _check_plan_vault(paths, plan)
        # r1-contract-consistency-02 (L-17): no resume while a local rename / move is unregistered -- the adoption
        # after the remaining writes would not verify (partially_applied). Before the challenge (state/ untouched;
        # the plan stays applying until a refresh registers the rename) and again after the phrase (fresh scan).
        _refuse_unregistered_renames(paths, cfg, plan, db=db, resume=True)
        todo = _intent_items(db, pid)
        states = _journal_states(db, pid)
        if not todo and not {k: v for k, v in states.items() if k not in ("cancelled", "failed")}:
            return _close_unstarted(paths, db, pid)
        by_id = {it["item_id"]: it for it in plan["items"]}
        if todo:
            lines = [f"计划 {pid} 续执行：只发送 journal 中未发出的操作（{len(todo)} 项）："]
            lines += ["  " + _item_line(by_id[i]) if i in by_id else f"  {i}" for i in todo]
        else:
            # I2 request: no intent rows but sent / unknown / done rows -> resume_plan closes the plan WITHOUT
            # sending any write (sent -> unknown, read-back + adoption of done rows, final status, result)
            lines = [f"计划 {pid} 续执行：journal 中没有未发出的操作（{states}）。",
                     "本次不会发送任何写入请求（0 个）：只核对已发出的操作（结果未知的记为 unknown，需 reconcile）、"
                     "回读与采纳已完成的写入，并结束该计划。"]
        code = wb_gate.challenge_code()
        phrase = f"确认 {pid[-4:]} 续执行{len(todo)}[{','.join(todo)}] {code}"
        if not _phrase(phrase, lines):
            raise _Fail("短语不符：已取消，未写入任何内容", EXIT_FAIL)
        _refuse_unregistered_renames(paths, cfg, plan, db=db, resume=True)
        record = record_confirmation(paths, db, pid, sha, todo, scope="resume", tty=tty, os_user=_os_user(),
                                     approval_name=f"{pid}.resume-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}")
        wc, rc = _make_clients(args, paths, cfg, "apply")
        extra = {k: v for k, v in (("blobs_dir", getattr(args, "blobs", None)),
                                   ("sidebar", getattr(args, "sidebar", None))) if v}
        res = _apply_module().resume_plan(pid, record, client=wc, read_client=rc, cfg=cfg, db=db, work=paths.work,
                                          vault=paths.vault, cfg_path=paths.config, **extra)
    finally:
        db.close()
    return _print_result(res)


def cmd_resolve(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    _refuse_mode_off(cfg, f"resolve --take {args.take}")
    # the human gate runs before any state file is opened (resolve --take obsidian is a confirmation command)
    tty = _human(f"写回操作 resolve --take {args.take}")
    db = _db_rw_existing(paths)
    try:
        plan, sha = _load(paths, db, args.plan)
        iid = args.item.upper()
        it = next((x for x in plan["items"] if x["item_id"] == iid), None)
        if it is None or it["category"] != "C":
            raise _Fail(f"{iid} 不是计划 {args.plan} 中的冲突项（C）", EXIT_REFUSED)
        if args.take == "notion":
            return _resolve_notion(paths, cfg, db, plan, it)
        # --take obsidian: human + challenge phrase; creates a NEW plan only (0 Notion requests)
        info = _mode(cfg, db)
        st, _row = _status_now(db, args.plan)
        if st not in ("draft", "confirmed", "superseded", "expired"):
            raise _Fail(f"计划状态 {st}：不能据此解决冲突", EXIT_REFUSED)
        _need_vault(paths)
        lines = [f"以本地版本为准解决 {iid}（{it.get('rel_path')}）：只生成一个新计划，不写 Notion；新计划仍须 apply。",
                 f"本项将覆盖 Notion 自 {it.get('notion_last_edited_time') or wb_plan.UNKNOWN} 以来的改动：{it.get('notion_change')}"]
        code = wb_gate.challenge_code()
        phrase = f"确认 {args.plan[-4:]} 以本地为准 {iid} {code}"
        if not _phrase(phrase, lines):
            raise _Fail("短语不符：已取消", EXIT_FAIL)
        record_confirmation(paths, db, args.plan, sha, [iid], scope="resolve", tty=tty, os_user=_os_user(),
                            approval_name=f"{args.plan}.resolve-{iid}")
        try:
            lk = wb_locks.lock("refresh", state_dir=paths.state)
            lk.__enter__()
        except wb_locks.LockBusy:
            raise _Fail("refresh 正在运行，稍后重试", EXIT_REFUSED) from None
        try:
            ctx = wb_plan.PlanContext(paths, cfg, db, read_client=None, mode=info["mode"])
            src = {"plan_id": args.plan, "item": it, "members": plan.get("members"),
                   "creation_state": plan.get("creation_state"), "breaker": plan.get("breaker"), "sync": plan.get("sync")}
            new, preview = wb_plan.build_plan(ctx, scope=f"resolve:{args.plan}:{iid}", resolve_from=src)
            res = wb_plan.create_plan(ctx, new, preview)
        finally:
            lk.__exit__(None, None, None)
        with wb_state.transaction(db):
            db.execute("UPDATE plan_items SET decision='resolved_obsidian', reason=? WHERE plan_id=? AND item_id=?",
                       (f"new_plan:{res['plan_id']}", args.plan, iid))
    finally:
        db.close()
    _out(f"新计划 {res['plan_id']}（sha12 {res['plan_sha256'][:12]}）：" +
         " ".join(f"{k}{new['counts'].get(k, 0)}" for k in wb_plan.CATEGORIES) + f" 拒绝{new['counts'].get('refused', 0)}")
    if new.get("unregistered_renames"):
        _out(f"注意 {wb_plan.UNREGISTERED_RENAME}：{wb_plan.UNREGISTERED_RENAME_HINT}（新计划所有条目都不可执行）")
    for r in new.get("refused") or []:          # e.g. L-23 (c): the note is held (AUTO_SYNC_HOLD) like in any plan
        _out(f"拒绝 {_safe(r.get('rel_path') or '')}：{r['class']} — {_safe(r.get('reason') or '')}；"
             f"建议：{_safe(r.get('hint') or '')}")
    _out(f"预览：{os.path.join(res['dir'], 'preview.md')}")
    for line in plan_next_steps(new, res['plan_id'], res['plan_sha256'][:12], info):
        _out(line)
    return EXIT_OK


# ============================================================================================ vault restore helper
def restore_published(paths, db, rel, *, expected_sha=None, nid=None, reason="restore"):
    """Back the vault file up (outside the vault) and write the bytes it had when last published (published_files
    sha -> state/base content, else the latest base_history text). The next refresh then publishes the Notion
    version through its normal REPLACE path. Guarded: the file must still hash to `expected_sha` (if given).
    Returns a short status string."""
    import wb_scan
    target = wb_common.safe_join(paths.vault, rel)
    if not os.path.isfile(target):
        return "本地文件不存在，未恢复"
    cur = wb_common.sha256_file(target)
    if expected_sha and cur != expected_sha:
        return "本地文件在计划后又被修改，未恢复（保留当前文件）"
    pub, _objs = wb_scan.load_export_state(paths.export_db)
    row = next((r for r in pub if r["rel"] == rel and (r["status"] or "active") == "active"), None)
    content = None
    if row and row.get("sha"):
        try:
            content = wb_state.base_content(row["sha"], db=db)
        except (ValueError, wb_state.BaseCorrupt):
            content = None
    if content is None and nid:
        base = wb_state.latest_base(db, nid)
        if base:
            try:
                content = wb_state.base_content(base["sha256"], db=db)
            except (ValueError, wb_state.BaseCorrupt):
                content = None
    if content is None:
        return "没有可恢复的已发布版本（state/base 中无内容），未恢复"
    if wb_common.sha256_bytes(content) == cur:
        return "本地文件已经是已发布版本"
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    bk = wb_common.backup_copy(target, os.path.join(paths.backup, "writeback-restore", ts), rel)
    wb_common.atomic_write(target, content)
    return f"已恢复为上次发布的版本（备份：{bk}）；下一轮 refresh 会按 Notion 当前版本更新"


def _resolve_notion(paths, cfg, db, plan, it):
    _need_vault(paths)
    dec = wb_plan.item_decisions(db, plan["plan_id"]).get(it["item_id"])
    if dec not in (None, "pending"):
        raise _Fail(f"{it['item_id']} 已处理（{dec}），未改动", EXIT_REFUSED)
    _out(f"以 Notion 为准解决 {it['item_id']}：{_safe(it.get('rel_path'))}")
    _out("本地文件会先备份到库外，然后恢复为上次发布的版本；下一轮 refresh 按 Notion 当前版本覆盖。不发送任何 Notion 请求。")
    if not _yes("以 Notion 为准（本地改动备份后放弃）？"):
        _out("已取消。")
        return EXIT_FAIL
    try:
        lk = wb_locks.lock("refresh", state_dir=paths.state)
        lk.__enter__()
    except wb_locks.LockBusy:
        raise _Fail("refresh 正在运行，稍后重试", EXIT_REFUSED) from None
    try:
        msg = restore_published(paths, db, it["rel_path"], expected_sha=it.get("l_sha"), nid=it.get("notion_id"))
    finally:
        lk.__exit__(None, None, None)
    with wb_state.transaction(db):
        db.execute("UPDATE plan_items SET decision='resolved_notion', reason=? WHERE plan_id=? AND item_id=?",
                   (msg[:200], plan["plan_id"], it["item_id"]))
    _out(msg)
    return EXIT_OK


# ============================================================================================ REJECT
def cmd_reject(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    _refuse_mode_off(cfg, "reject")
    tty = _human("写回操作 reject")
    db = _db_rw_existing(paths)
    try:
        plan, _sha = _load(paths, db, args.plan)
        ids = wb_plan._split_ids(args.items)
        by_id = {x["item_id"]: x for x in plan["items"]}
        items = []
        for i in ids:
            it = by_id.get(i)
            if it is None:
                raise _Fail(f"{i} 不在计划 {args.plan} 中", EXIT_REFUSED)
            if it["category"] == "C":
                raise _Fail(f"{i} 是冲突项：{wb_plan.conflict_steps(args.plan, i)}", EXIT_REFUSED)
            if it["category"] == "D":
                raise _Fail(f"{i} 是删除项：用 {cli_command('writeback', 'restore-deleted', '--plan', args.plan, '--item', i)}", EXIT_REFUSED)
            items.append(it)
        if not items:
            raise _Fail("没有指定要拒绝的项（--items M-001,…）", EXIT_REFUSED)
        try:
            check_rejectable(db, plan, items)
        except WbError as exc:
            raise _Fail(str(exc), EXIT_REFUSED) from None
        if args.restore_notion:
            _need_vault(paths)
        _out(f"将拒绝计划 {args.plan} 的以下项（不发送任何 Notion 请求）：")
        for it in items:
            _out("  " + _safe(_item_line(it)))
        if args.restore_notion:
            _out("--restore-notion：M/B 项的本地文件备份后恢复为上次发布的版本（下一轮按 Notion 更新）；"
                 "T 项把文件改回原名（命名差异解除）；N 项不改动本地文件。")
        if not _yes("拒绝这些项？"):
            _out("已取消。")
            return EXIT_FAIL
        msgs = reject_items(paths, db, plan, items, reason=args.reason, restore_notion=args.restore_notion,
                            os_user=_os_user(), tty=tty)
    finally:
        db.close()
    for m in msgs:
        _out(_safe(m))
    return EXIT_OK


REJECTABLE_DECISIONS = ("pending", "skipped_precheck")
REJECT_REFUSED_STATUSES = ("confirmed", "applying")


def check_rejectable(db, plan, items):
    """Only items nobody has acted on may be rejected: decision pending (or skipped by the executor's precheck),
    in a plan that is not being confirmed / executed. An executed / confirmed / failed / unknown item keeps its
    decision (audit trail, W8)."""
    pid = plan["plan_id"]
    row = wb_plan.plan_row(db, pid)
    if row is None:
        raise WbError(f"计划 {pid} 不存在")
    if row["status"] in REJECT_REFUSED_STATUSES:
        raise WbError(f"计划 {pid} 状态为 {row['status']}（正在确认或执行）：不能拒绝其中的项")
    dec = wb_plan.item_decisions(db, pid)
    for it in items:
        d = dec.get(it["item_id"], "pending")
        if d not in REJECTABLE_DECISIONS:
            raise WbError(f"{it['item_id']} 的决定已是 {d}（只有尚未执行的项可以拒绝）；"
                          + (f"已执行的改动用 {cli_command('writeback', 'rollback', '--plan', pid)} 回退" if d in ("executed", "confirmed")
                             else "请先核对其结果"))


def reject_items(paths, db, plan, items, *, reason=None, restore_notion=False, os_user=None, tty=None):
    """plan_items.decision='rejected' + one active rejections row per item (keyed by notion_id/path + L sha).
    Optional --restore-notion vault restore (M/B: published bytes, T: rename back). Never a Notion request.
    Guarded: only items with a pending (or precheck-skipped) decision, never in a confirmed / applying plan."""
    pid = plan["plan_id"]
    check_rejectable(db, plan, items)
    at = wb_common.now_iso()
    out = []
    with wb_state.transaction(db):
        for it in items:
            cur = db.execute("UPDATE plan_items SET decision='rejected', reason=? WHERE plan_id=? AND item_id=? AND "
                             "decision IN (?, ?)", (reason or "rejected_by_user", pid, it["item_id"],
                                                    *REJECTABLE_DECISIONS))
            if cur.rowcount != 1:
                raise WbError(f"{it['item_id']} 的决定已变化（并发操作？），未拒绝任何项")
            db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id,"
                       " active, os_user, restore_notion) VALUES(?,?,?,?,?,?,?,1,?,?)",
                       (it.get("notion_id"), it.get("rel_path"), it.get("l_sha"), it["category"], reason, at, pid,
                        os_user, 1 if restore_notion else 0))
            out.append(f"{it['item_id']} 已拒绝")
    if restore_notion:
        try:
            lk = wb_locks.lock("refresh", state_dir=paths.state)
            lk.__enter__()
        except wb_locks.LockBusy:
            out.append("refresh 正在运行：已记录拒绝，本地文件尚未恢复。请从库外备份核对需恢复的内容。")
            return out
        try:
            for it in items:
                c = it["category"]
                if c in ("M", "B"):
                    out.append(f"{it['item_id']}：" + restore_published(paths, db, it["rel_path"],
                                                                      expected_sha=it.get("l_sha"), nid=it.get("notion_id")))
                elif c == "T":
                    out.append(f"{it['item_id']}：" + _rename_back(paths, db, it))
                elif c == "N":
                    out.append(f"{it['item_id']}：新文件保留在库内（不改动）")
        finally:
            lk.__exit__(None, None, None)
    return out


def _rename_back(paths, db, it):
    old, cur = it.get("old_rel"), it.get("rel_path")
    if not old or not cur:
        return "缺少原路径，未改回"
    src = wb_common.safe_join(paths.vault, cur)
    dst = wb_common.safe_join(paths.vault, old)
    if not os.path.isfile(src):
        return "本地文件不存在，未改回"
    if os.path.lexists(dst):
        return f"原路径 {old} 已存在文件，未改回"
    if it.get("l_sha") and wb_common.sha256_file(src) != it["l_sha"]:
        return "本地文件在计划后又被修改，未改回"
    if os.path.dirname(src) != os.path.dirname(dst):
        return "不在同一文件夹，未改回"
    os.rename(src, dst)
    with wb_state.transaction(db):
        db.execute("UPDATE name_overrides SET status='closed' WHERE notion_id=? AND status='open'", (it["notion_id"],))
    return f"已把文件改回 {old}（命名差异解除）"


def cmd_unreject(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "unreject")
    _human("写回操作 unreject")
    db = _db_rw_existing(paths)
    try:
        if args.id:
            nid = wb_common.norm_notion_id(args.id)
            rows = db.execute("SELECT id, item_category, rel_path FROM rejections WHERE active=1 AND notion_id=?",
                              (nid,)).fetchall()
        else:
            rows = db.execute("SELECT id, item_category, rel_path FROM rejections WHERE active=1 AND rel_path=?",
                              (args.path,)).fetchall()
        if not rows:
            raise _Fail("没有匹配的有效拒绝记录", EXIT_REFUSED)
        for r in rows:
            _out(f"  #{r['id']} {r['item_category']} {_safe(r['rel_path'] or '')}")
        if not _yes(f"撤销这 {len(rows)} 条拒绝（下次 plan 重新提出）？"):
            _out("已取消。")
            return EXIT_FAIL
        with wb_state.transaction(db):
            for r in rows:
                db.execute("UPDATE rejections SET active=0 WHERE id=?", (r["id"],))
    except ValueError as exc:
        raise _Fail(str(exc), EXIT_REFUSED) from None
    finally:
        db.close()
    _out(f"已撤销 {len(rows)} 条拒绝；运行 {cli_command('writeback', 'plan')} 重新生成计划。")
    return EXIT_OK


def cmd_discard(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "discard")
    _human("写回操作 discard")
    db = _db_rw_existing(paths)
    try:
        st, row = _status_now(db, args.plan)
        if st not in ("draft", "confirmed"):
            raise _Fail(f"计划状态为 {st}：只能放弃 draft/confirmed 的计划", EXIT_REFUSED)
        if not _yes(f"放弃整个计划 {args.plan}（之后不能再执行）？"):
            _out("已取消。")
            return EXIT_FAIL
        if not wb_state.cas_plan_status(db, args.plan, row["status"], "superseded", reason="discarded_by_user"):
            raise _Fail("计划状态已变化，未放弃", EXIT_REFUSED)
    finally:
        db.close()
    _out(f"计划 {args.plan} 已放弃（superseded：discarded_by_user），不能再 apply。")
    return EXIT_OK


def cmd_restore_deleted(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "restore-deleted")
    _human("写回操作 restore-deleted")
    db = _db_rw_existing(paths)
    try:
        nid, item = None, None
        if args.plan:
            plan, _sha = _load(paths, db, args.plan)
            item = next((x for x in plan["items"] if x["item_id"] == (args.item or "").upper()), None)
            if item is None or item["category"] != "D":
                raise _Fail(f"{args.item} 不是计划 {args.plan} 中的删除项（D）", EXIT_REFUSED)
            nid = item["notion_id"]
        elif args.id:
            nid = wb_common.norm_notion_id(args.id)
        else:
            raise _Fail(f"需要删除计划的编号和 D 项，或待恢复的页面 ID；用 {cli_command('writeback', 'restore-deleted', '--help')} 核对参数", EXIT_REFUSED)
        rows = db.execute("SELECT rowid, rel_path, status FROM pending_deletes WHERE notion_id=? AND status='hold'",
                          (nid,)).fetchall()
        if not rows:
            raise _Fail(f"{nid} 没有处于 hold 的待决删除", EXIT_REFUSED)
        for r in rows:
            _out(f"  {_safe(r['rel_path'])}  ({nid})")
        if not _yes("放弃本地删除，下一轮 refresh 按 Notion 版本恢复该文件？"):
            _out("已取消。")
            return EXIT_FAIL
        now = wb_common.now_iso()
        with wb_state.transaction(db):
            db.execute("UPDATE pending_deletes SET status='expired', ttl_until=? WHERE notion_id=? AND status='hold'",
                       (now, nid))
            if item is not None:
                db.execute("UPDATE plan_items SET decision='rejected', reason='restore_deleted' WHERE plan_id=? AND "
                           "item_id=?", (args.plan, item["item_id"]))
    except ValueError as exc:
        raise _Fail(str(exc), EXIT_REFUSED) from None
    finally:
        db.close()
    _out("已放弃本地删除（pending_deletes → expired）：下一轮 refresh 按 Notion 版本恢复文件。")
    return EXIT_OK


# ============================================================================================ RE-RUN
def cmd_reconcile(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    _refuse_mode_off(cfg, "reconcile")
    _human("写回操作 reconcile")
    db = _db_rw_existing(paths)
    try:
        row = db.execute("SELECT * FROM journal WHERE seq=?", (args.journal,)).fetchone()
        if row is None:
            raise _Fail(f"journal {args.journal} 不存在", EXIT_REFUSED)
        _out(f"journal #{row['seq']}  {row['plan_id']} {row['item_id']} {row['op']} {row['target_id']}  状态 {row['state']}")
        if not _yes(f"按你在 Notion 中核对的结果把它记为 {args.decision}？"):
            _out("已取消。")
            return EXIT_FAIL
        rc = _read_client(args, paths, cfg)
        # vault: a committed CREATE_PAGE also gets its sha-guarded notion_id frontmatter (I2 request)
        res = _apply_module().reconcile_journal(args.journal, args.decision, read_client=rc, cfg=cfg, db=db,
                                                work=paths.work, vault=paths.vault)
    finally:
        db.close()
    _out(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return EXIT_OK if res.get("ok") else EXIT_FAIL


def cmd_unblock(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "unblock")
    _human("写回操作 unblock")
    db = _db_rw_existing(paths)
    try:
        nid = wb_common.norm_notion_id(args.page)
        row = db.execute("SELECT * FROM page_state WHERE notion_id=?", (nid,)).fetchone()
        if row is None or row["state"] not in ("BLOCKED", "PARTIAL_CREATE"):
            raise _Fail(f"{nid} 不处于 BLOCKED / PARTIAL_CREATE", EXIT_REFUSED)
        _out(f"{nid}  {row['state']}  {_safe(row['reason'] or '')}  自 {row['since']}")
        if not _yes("你已在 Notion 中检查过该页面，解除 BLOCKED？"):
            _out("已取消。")
            return EXIT_FAIL
        with wb_state.transaction(db):
            db.execute("UPDATE page_state SET state='normal', reason=?, since=? WHERE notion_id=?",
                       (f"unblocked by user (was {row['state']})", wb_common.now_iso(), nid))
    except ValueError as exc:
        raise _Fail(str(exc), EXIT_REFUSED) from None
    finally:
        db.close()
    _out(f"已解除；运行 {cli_command('writeback', 'plan')} 重新生成计划。")
    return EXIT_OK


def cmd_rollback(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    _precheck_write_mode(cfg, "rollback")
    if os.path.exists(paths.stop_file):
        raise _Fail("急停文件 state/WRITEBACK_STOP 存在：拒绝执行", EXIT_REFUSED)
    tty = _human("写回操作 rollback")
    _precheck_token_file(args)
    db = _db_rw_existing(paths)
    try:
        _require_write_mode(cfg, db, "rollback")
        plan, sha = _load(paths, db, args.plan)
        _check_plan_vault(paths, plan)
        ids = wb_plan._split_ids(args.items) if args.items else []
        rc = _read_client(args, paths, cfg)
        wa = _apply_module()
        prev = wa.rollback_preview(args.plan, ids, read_client=rc, db=db, work=paths.work)
        steps = prev.get("steps") or []
        if not steps:
            raise _Fail("没有可回退的操作：" + str(prev.get("reason") or "journal 中没有已执行的操作"), EXIT_REFUSED)
        items = []
        lines = [f"回退预览（计划 {args.plan}；只有自写入后未被改动的块才会改回）："]
        for s in steps:
            lines.append(f"  #{s.get('seq')} {s.get('item_id')} {s.get('op')} {str(s.get('target') or '')[:8]}  "
                         f"→ {s.get('action')}  {s.get('reason') or ''}")
            if s.get("item_id") and s["item_id"] not in items:
                items.append(s["item_id"])
        code = wb_gate.challenge_code()
        phrase = f"确认 {args.plan[-4:]} 回退{len(items)}[{','.join(items)}] {code}"
        if not _phrase(phrase, lines):
            raise _Fail("短语不符：已取消，未写入任何内容", EXIT_FAIL)
        record = record_confirmation(paths, db, args.plan, sha, items, scope="rollback", tty=tty, os_user=_os_user(),
                                     approval_name=f"{args.plan}.rollback-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}")
        wc, rc2 = _make_clients(args, paths, cfg, "rollback")
        res = wa.rollback_plan(args.plan, ids or items, record, client=wc, read_client=rc2, cfg=cfg, db=db,
                               work=paths.work, vault=paths.vault, cfg_path=paths.config)
    finally:
        db.close()
    return _print_result(res, kind="rollback")


def cmd_audit_scope(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    _refuse_mode_off(cfg, "audit-scope")
    _human("写回操作 audit-scope")
    _precheck_token_file(args)
    db = _db_rw(paths)
    try:
        forced_before = wb_state.get_meta(db, "forced_mode")
        wc, _rc = _make_clients(args, paths, cfg, "audit")
        wa = _apply_module()
        res = wa.audit_scope(client=wc, cfg=cfg, db=db)
        _out(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        for t in res.get("trashed_via_writeback") or []:
            _out(f"注意：白名单页面 {t['id']} 已由写回删除（计划 {t.get('plan_id')}，在 Notion 回收站中），不算不可达；"
                 "确认不再需要写回它时，可以把它从 config/writeback.json 的 whitelist_page_ids 中删除。")
        if not res.get("ok"):
            _out("audit-scope 不通过：写回集成可达范围与声明不符，模式已强制降为 plan。")
            _out(_safe(wa.audit_scope_detail(res)))
            return EXIT_FAIL
        if forced_before:
            why = wb_state.get_meta(db, "forced_mode_reason") or ""
            _out(f"之前的自动降级：forced_mode={forced_before}（{_safe(why)}）")
            if _yes("本次 audit-scope 已通过：解除自动降级（之后按配置的 mode 运行）？"):
                with wb_state.transaction(db):
                    for k in ("forced_mode", "forced_mode_reason", "forced_mode_at"):
                        wb_state.set_meta(db, k, None)
                _out("已解除自动降级。")
    finally:
        db.close()
    return EXIT_OK


def cmd_ack_anomaly(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "ack-anomaly")
    _human("写回操作 ack-anomaly")
    db = _db_rw_existing(paths)
    try:
        plan, _sha = _load(paths, db, args.run)
        br = plan.get("breaker") or {}
        if not br.get("over_threshold", br.get("tripped")):
            raise _Fail(f"计划 {args.run} 没有触发熔断", EXIT_REFUSED)
        _out("熔断原因：" + "；".join(_safe(x) for x in br.get("reasons") or []))
        if not _yes("确认这些本地改动规模是你本人有意为之（之后需重新生成全库计划）？"):
            _out("已取消。")
            return EXIT_FAIL
        wb_state.set_meta(db, "anomaly_ack", wb_plan.anomaly_ack_record(plan, os_user=_os_user()))
    finally:
        db.close()
    _out(f"已确认；运行 {cli_command('writeback', 'plan')}（不带 --scope）重新生成全库计划。")
    return EXIT_OK


def cmd_ack_backlog(args):
    paths = _paths(args)
    _refuse_mode_off(_cfg(paths), "ack-backlog")
    _human("写回操作 ack-backlog")
    db = _db_rw_existing(paths)
    try:
        _need_vault(paths)
        nid = None
        try:
            with open(wb_common.safe_join(paths.vault, args.path), "rb") as fh:
                import wb_scan
                nid, _k, _e = wb_scan.read_note_identity(fh.read())
        except (OSError, ValueError):
            nid = None
        bl = wb_state.get_meta_json(db, "initial_backlog", {}) or {}
        if nid is None:
            # a new file (no notion_id) recorded at the first plan is keyed "new:<rel>"
            if ("new:" + args.path) in bl:
                nid = "new:" + args.path
            else:
                raise _Fail(f"无法从 {args.path} 读出 notion_id（也不是已记录的启用前新文件）", EXIT_REFUSED)
        _out(f"{_safe(args.path)}  ({nid})" + ("" if nid in bl else "（不在已记录的 INITIAL_BACKLOG 中）"))
        if not _yes("确认这篇笔记的现有本地差异可以进入写回计划？"):
            _out("已取消。")
            return EXIT_FAIL
        at = wb_common.now_iso()
        with wb_state.transaction(db):
            if nid in bl:
                bl[nid]["acked_at"] = at
                wb_state.set_meta(db, "initial_backlog", bl)
            acked = wb_state.get_meta_json(db, "backlog_acked", {}) or {}
            acked[nid] = {"at": at, "path": args.path}
            wb_state.set_meta(db, "backlog_acked", acked)
    finally:
        db.close()
    _out(f"已确认；下次 {cli_command('writeback', 'plan')} 会为它生成写回项。")
    return EXIT_OK


def cmd_hold(args):
    """Convert the missing notes of a paused round (local_mass_delete, WB 6.0 (c)) into pending_deletes holds."""
    import refresh
    import wb_scan
    paths = _paths(args)
    cfg = _cfg(paths)
    _refuse_mode_off(cfg, "hold")
    _human("写回操作 hold")
    db = _db_rw(paths)
    try:
        P = refresh.Paths(paths.work, paths.vault or paths.work)   # hold reads state/reports only, never a vault
        pr = refresh.load_paused(P)
        e = pr["runs"].get(args.run)
        if not e or e.get("status") != "paused":
            raise _Fail(f"{args.run} 不是处于暂停状态的轮次", EXIT_REFUSED)
        scan = wb_plan._read_json(P.report(args.run, "wb_scan.json"), None)
        if not isinstance(scan, dict):
            raise _Fail(f"缺少 reports/{args.run}/wb_scan.json", EXIT_REFUSED)
        mode = wb_config.effective_mode(cfg, db)
        if wb_config.mode_rank(mode) < wb_config.mode_rank("plan"):
            raise _Fail("mode=off：本地删除按 Notion 版本恢复，不能转为 hold", EXIT_REFUSED)
        big = dict(cfg, max_local_deletes=10 ** 6)
        _pub, objects = wb_scan.load_export_state(paths.export_db)
        page_json = refresh.load_page_json(e.get("staging") or os.path.join(paths.staging, args.run))
        roots = (refresh.read_json(e.get("sidebar"), {}) or {}).get("roots") or [] if e.get("sidebar") else []
        root_ids = {r.get("id") for r in roots}
        ro = wb_state.connect_readonly(paths.wb_db)
        try:
            dec = refresh.deletion_decisions(scan, big, mode, objects, page_json, e.get("snapshot"), root_ids, ro,
                                             args.run)
        finally:
            if ro is not None:
                ro.close()
        # W3 glue fix: a round paused for another reason (e.g. source_mass_change) has already put its new local
        # deletions on hold (refresh update_pending runs before the pause decision); never register them twice
        held = {r[0] for r in db.execute("SELECT notion_id FROM pending_deletes WHERE status='hold'")}
        already = [r for r in dec["pending"] if r["notion_id"] in held]
        dec["pending"] = [r for r in dec["pending"] if r["notion_id"] not in held]
        for r in already:
            _out(f"  已在 hold 中（不重复登记）：{_safe(r['rel_path'])}  ({r['notion_id']})")
        if not dec["pending"]:
            raise _Fail("暂停轮次中没有可转为 hold 的本地删除" + ("（这些删除已在 hold 中，未改动任何状态）" if already
                                                         else "（其余的本地删除在下一轮 refresh 时按 Notion 版本恢复）"), EXIT_REFUSED)
        for r in dec["pending"]:
            _out(f"  hold：{_safe(r['rel_path'])}  ({r['notion_id']})")
        for r in dec["restore"][:50]:
            _out(f"  恢复（不能 hold）：{_safe(r.get('rel') or '')}  {r.get('rule')}")
        if not _yes(f"把这 {len(dec['pending'])} 个本地删除转为 hold（之后可在计划中逐项确认删除）？"):
            _out("已取消。")
            return EXIT_FAIL
        try:
            lk = wb_locks.lock("refresh", state_dir=paths.state)
            lk.__enter__()
        except wb_locks.LockBusy:
            raise _Fail("refresh 正在运行，稍后重试", EXIT_REFUSED) from None
        try:
            with wb_state.transaction(db):
                for r in dec["pending"]:
                    if db.execute("SELECT 1 FROM pending_deletes WHERE notion_id=? AND status='hold'",
                                  (r["notion_id"],)).fetchone():
                        continue            # held meanwhile (re-checked under refresh.lock)
                    db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at,"
                               " status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                               (r["notion_id"], r["rel_path"], r["subtree_json"], r["detected_run"], r["detected_at"],
                                "hold", r["ttl_until"]))
            pr = refresh.load_paused(P)
            pr["runs"][args.run].update(status="discarded", decided_at=wb_common.now_iso(),
                                        decided_by="writeback.py hold")
            if pr.get("latest") == args.run:
                pr["latest"] = None
            refresh.write_json(P.paused_runs, pr)
        finally:
            lk.__exit__(None, None, None)
    finally:
        db.close()
    _out(f"已转为 hold：{len(dec['pending'])} 个；暂停轮次 {args.run} 已结束（discarded）。下一轮 refresh 不再恢复这些文件。")
    return EXIT_OK


def cmd_rebase(args):
    paths = _paths(args)
    cfg = _cfg(paths)
    if not args.from_notion:
        raise _Fail(f"恢复本地笔记需要从已发布版本重建；请先查看 {cli_command('writeback', 'rebase', '--help')}", EXIT_REFUSED)
    _refuse_mode_off(cfg, "rebase --from-notion")
    if cfg.get("mode") in ("whitelist", "full"):
        raise _Fail(f"请用户本人先把 config/writeback.json 的 mode 设为 plan（恢复操作期间不写回 Notion），再运行 {cli_command('writeback', 'rebase', '--from-notion')}", EXIT_REFUSED)
    _human("写回操作 rebase --from-notion")
    db = _db_rw_existing(paths)
    try:
        _need_vault(paths)
        import wb_scan
        res = wb_scan.scan(paths.vault, paths.export_db, paths.wb_db, cfg)
        mods = [m for m in res.get("local_modified", []) if m.get("kind") in ("page", "row")]
        _out(f"以 Notion 为准重建 base：{len(mods)} 篇本地有差异的笔记会备份到库外，然后恢复为上次发布的版本；"
             "下一轮 refresh 按 Notion 当前版本更新。不发送任何 Notion 请求。")
        for m in mods[:50]:
            _out(f"  {_safe(m['rel'])}")
        if not _yes("继续？"):
            _out("已取消。")
            return EXIT_FAIL
        try:
            lk = wb_locks.lock("refresh", state_dir=paths.state)
            lk.__enter__()
        except wb_locks.LockBusy:
            raise _Fail("refresh 正在运行，稍后重试", EXIT_REFUSED) from None
        try:
            msgs = [f"{m['rel']}：" + restore_published(paths, db, m["rel"], expected_sha=m.get("vault_sha"),
                                                       nid=m.get("notion_id")) for m in mods]
            wb_state.set_meta(db, "rebased_at", wb_common.now_iso())
        finally:
            lk.__exit__(None, None, None)
    finally:
        db.close()
    for x in msgs:
        _out(_safe(x))
    return EXIT_OK


# ============================================================================================ parser
class _ArgParser(argparse.ArgumentParser):
    """r1-operability-10: a usage error says so in Chinese and exits 64 (not 2, which means "refused by mode/state")."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"参数错误 / usage error：{message}\n（运行 {self.prog} --help 查看用法；未执行任何操作）\n")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("通用选项 common options")
    g.add_argument("--work", default=argparse.SUPPRESS, help="工作目录（默认 tools/ 的上级）/ WORK root (rehearsals)")
    g.add_argument("--vault", default=argparse.SUPPRESS, help="库路径（正式部署默认 Godcreate）/ vault path")
    g.add_argument("--base-url", default=argparse.SUPPRESS, help="Notion API 地址（测试用本地假服务）/ API base URL")
    g.add_argument("--read-token-file", default=argparse.SUPPRESS, help="读令牌文件 / read token file")
    g.add_argument("--token-file", default=argparse.SUPPRESS,
                   help="写回令牌文件（仅 apply/rollback/audit-scope；须 0600 于 0700 目录）/ write-back token file")
    help_text = "\n\n".join(__doc__.split("\n\n")[1:])
    for tool in ("writeback", "refresh"):
        help_text = help_text.replace(f"python3 -I tools/{tool}.py", cli_command(tool))
    ap = _ArgParser(prog=cli_command("writeback"), parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__.split("\n\n")[0],
                                 epilog=help_text)
    sub = ap.add_subparsers(dest="cmd", metavar="命令 command")
    sub.required = True

    def add(name, help_, fn):
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    P = "计划编号 wb-…（plan 的输出）/ plan id"
    p = add("status", "查看 VIEW：模式、计划、待确认改动（按类）、hold、命名差异、BLOCKED、已拒绝 / status", cmd_status)
    p.add_argument("--json", action="store_true", help="输出 JSON / machine-readable output")
    p.add_argument("--no-scan", action="store_true", help="不扫描库（只读状态库）/ do not scan the vault")
    p = add("plan", "查看 VIEW：生成计划与预览（只读 Notion）/ build a plan + preview (read-only)", cmd_plan)
    p.add_argument("--scope", default=None, help="白名单 | <路径前缀>（熔断统计始终全库）/ whitelist | path prefix")
    p.add_argument("--snapshot", default=None, help="规划用的已发布快照（默认 state/last_published_snapshot.txt）"
                                                    " / planning snapshot")
    p = add("show", "查看 VIEW：显示预览或单项（统一差异 + 操作列表）/ show a plan or one item", cmd_show)
    p.add_argument("--plan", required=True, help=P)
    p.add_argument("--item", default=None, help="只看一项，例如 M-001（差异 + 操作列表）/ one item, e.g. M-001")
    p = add("whitelist-check", "查看 VIEW：白名单页面的后代、内联数据库与行数 / whitelist descendants", cmd_whitelist_check)
    p.add_argument("--json", action="store_true", help="输出 JSON / machine-readable output")
    p.add_argument("--snapshot", default=None, help="统计用的快照（默认上次发布的快照）/ snapshot to count from")
    p = add("apply", "确认 APPROVE：执行计划中被选择的项（本人终端 + 挑战短语）/ execute selected items", cmd_apply)
    p.add_argument("--plan", default=None, help=P)
    p.add_argument("--sha", default=None, help="plan_sha256 的前 12 位 / 12-hex prefix of plan_sha256")
    p.add_argument("--items", action="append", default=[], help="M-…,N-…（修改与新增）/ modify + new items")
    p.add_argument("--all-safe", action="store_true",
                   help="全部可执行的 M 与 N（永不含 B/D/T/MV/C；同一笔记还有其他项的不含）/ all independent M+N")
    p.add_argument("--delete", action="append", default=[], help="D-…（页面移入回收站，逐项）/ page trash items")
    p.add_argument("--delete-all-in-plan", action="store_true",
                   help="本计划全部可成批的 D 项（≤ max_batch_deletes，不含高风险）/ batchable D items")
    p.add_argument("--delete-blocks", action="append", default=[], help="B-…（块删除）/ block delete items")
    p.add_argument("--rename", action="append", default=[], help="T-…（改 Notion 标题）/ rename items")
    p.add_argument("--move", action="append", default=[], help="MV-…（移动页面，阶段 3）/ move items")
    p.add_argument("--resume", default=None, metavar="P", help="续执行中断的计划（只发 journal 中未发出的操作）/ resume")
    p.add_argument("--blobs", default=None, metavar="DIR",
                   help="写回后采纳用的附件目录（默认 WORK/blobs；演练环境用）/ blobs dir for the adoption export")
    p.add_argument("--sidebar", default=None, metavar="FILE",
                   help="写回后采纳用的侧栏文件（默认取快照的 roots）/ sidebar file for the adoption export")
    p = add("resolve", "确认/拒绝：解决冲突项 C（notion = 以 Notion 为准；obsidian = 只生成新计划）/ resolve a conflict",
            cmd_resolve)
    p.add_argument("--plan", required=True, help=P)
    p.add_argument("--item", required=True, help="冲突项 C-… / conflict item")
    p.add_argument("--take", required=True, choices=["notion", "obsidian"],
                   help="notion：备份后恢复本地文件；obsidian：生成新计划（仍须 apply）/ which side wins")
    p = add("reject", "拒绝 REJECT：拒绝 M/N/T/B 项（不发 Notion 请求）/ reject items", cmd_reject)
    p.add_argument("--plan", required=True, help=P)
    p.add_argument("--items", action="append", default=[], required=True, help="要拒绝的项，例如 M-001,T-002 / items")
    p.add_argument("--reason", default=None, help="拒绝原因（记录在状态库）/ reason, recorded")
    p.add_argument("--restore-notion", action="store_true",
                   help="同时把本地恢复为上次发布（Notion）的版本，先备份 / also restore the local file")
    p = add("unreject", "拒绝 REJECT：撤销拒绝 / undo a rejection", cmd_unreject)
    gx = p.add_mutually_exclusive_group(required=True)
    gx.add_argument("--id", default=None, help="按 Notion 页面 ID 撤销 / by notion_id")
    gx.add_argument("--path", default=None, help="按库内路径撤销 / by vault path")
    p = add("discard", "拒绝 REJECT：放弃整个计划（superseded）/ discard a plan", cmd_discard)
    p.add_argument("--plan", required=True, help=P)
    p = add("restore-deleted", "拒绝 REJECT：放弃本地删除，下一轮按 Notion 恢复 / undo a local deletion", cmd_restore_deleted)
    p.add_argument("--plan", default=None, help=P)
    p.add_argument("--item", default=None, help="删除项 D-… / delete item")
    p.add_argument("--id", default=None, help="或直接给 Notion 页面 ID / or the notion_id")
    p = add("reconcile", "再次执行 RE-RUN：人工核对结果未知的操作 / reconcile an unknown journal row", cmd_reconcile)
    p.add_argument("--journal", type=int, required=True, help="journal 序号（result.md 中列出）/ journal seq")
    p.add_argument("--decision", required=True, choices=["committed", "not_committed"],
                   help="你在 Notion 中核对的结果 / what you found in Notion")
    p = add("unblock", "再次执行 RE-RUN：解除 BLOCKED / PARTIAL_CREATE / unblock a page", cmd_unblock)
    p.add_argument("--page", required=True, help="Notion 页面 ID / page id")
    p = add("rollback", "再次执行 RE-RUN：回退已执行的写入（本人终端 + 挑战短语）/ roll back", cmd_rollback)
    p.add_argument("--plan", required=True, help=P)
    p.add_argument("--items", action="append", default=[], help="只回退这些项（默认全部）/ only these items")
    p = add("ack-anomaly", "再次执行 RE-RUN：确认熔断异常 / acknowledge a tripped circuit breaker", cmd_ack_anomaly)
    p.add_argument("--run", required=True, metavar="PLAN_ID", help="触发熔断的计划编号 / the plan that tripped")
    p = add("ack-backlog", "再次执行 RE-RUN：确认启用前已有的本地差异 / acknowledge an INITIAL_BACKLOG note", cmd_ack_backlog)
    p.add_argument("--path", required=True, help="库内路径 / vault path of the note")
    p = add("hold", "再次执行 RE-RUN：把暂停轮次的本地删除转为 hold / convert a paused round's deletions", cmd_hold)
    p.add_argument("--run", required=True, metavar="RUN_ID", help="暂停的 refresh 轮次 / paused refresh run")
    p = add("rebase", "再次执行 RE-RUN：恢复操作后以 Notion 为准重建 base / rebase from Notion", cmd_rebase)
    p.add_argument("--from-notion", action="store_true", required=True,
                   help="本地有差异的笔记备份后恢复为上次发布的版本 / restore modified notes from the last publish")
    add("audit-scope", "再次执行 RE-RUN：核验写回集成的实际可达范围（加载写回令牌）/ audit write scope", cmd_audit_scope)
    return ap


def main(argv=None):
    import logging
    import notion_tokens
    ap = build_parser()
    args = ap.parse_args(argv)
    # r1-operability-10: the planner reports unreadable pages itself (Chinese, per item); the library's English
    # per-request warnings ("… GET pages/… -> 404 …") are not shown to the person
    logging.getLogger("nmig.client").setLevel(logging.ERROR)
    try:
        with wb_common.command_context(args):
            return args.fn(args)
    except _Fail as exc:
        _err(exc.msg)
        return exc.code
    except WbError as exc:
        _err(str(exc))
        return EXIT_REFUSED
    except wb_common.UnsafePath as exc:
        _err(f"路径不安全：{exc}")
        return EXIT_REFUSED
    except notion_tokens.TokenError as exc:                       # r1-operability-01 / -10
        _err(f"凭据问题：{exc}；未向 Notion 发送任何写请求（修正凭据文件后重试）")
        return EXIT_REFUSED
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")     # the reader (e.g. `| head`) went away: stop quietly
        except OSError:
            pass
        return EXIT_FAIL
    except sqlite3.Error as exc:
        _err(f"状态库错误（{type(exc).__name__}：{exc}）：state/writeback.db 或 state/export.db 无法读写（损坏或被占用）；"
             "未向 Notion 发送任何写请求。请先备份 state/ 目录，再联系维护者")
        return EXIT_FAIL
    except OSError as exc:
        _err(f"文件读写失败（{type(exc).__name__}：{exc}）：未向 Notion 发送任何写请求；检查路径与权限后重试")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
