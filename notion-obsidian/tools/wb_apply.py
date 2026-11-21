#!/usr/bin/env python3
"""Write-back executor (I2): runs a CONFIRMED write-back plan against Notion (WRITEBACK_SPEC §11–§13,
IMPL_CONTRACT §5.13 / §5.13.1).

Only the human-gated CLI (``writeback.py apply|rollback``, owned by I1) calls the write entry points, and
only after ``wb_gate.require_human`` + ``confirm_phrase`` and after it inserted the ``confirmations`` rows.
This module RE-VERIFIES everything itself (defence in depth) and sends a write only for an op of an item
that is (a) in the plan.json whose bytes hash to the recorded ``plan_sha256``, (b) in the selection, (c)
in a matching ``confirmations`` row (plan_id, item_id, full plan sha, challenge_ok, phrase scope), (d) not
rejected, and (e) passed the live precheck. Unselected / unconfirmed items are never sent.

Public API (contract §5.13.1)::

    make_clients(cfg, *, token_path=None, base_url=None, ...) -> (WritebackClient, ReadOnlyClient)
    execute_confirmed_plan(plan_id, selection, confirmation_record, *, client, read_client, cfg, db, now=None, ...)
    resume_plan(plan_id, confirmation_record, *, client, read_client, cfg, db, now=None, ...)
    rollback_preview(plan_id, item_ids, *, read_client, db) -> dict
    rollback_plan(plan_id, item_ids, confirmation_record, *, client, read_client, cfg, db, now=None, ...)
    reconcile_journal(seq, decision, *, read_client, cfg, db) -> dict      # decision: committed | not_committed
    audit_scope(*, client, cfg, db) -> {"ok", "extra", "unreachable", ...}

Apply flow (WRITEBACK_SPEC §11):
 0. ``state/writeback.lock`` + ``state/refresh.lock`` (non-blocking), plan ``confirmed`` -> ``applying`` (CAS).
 1. start checks in order: token permissions -> plan validity + full sha -> confirmations cover the
    selection -> audit-scope -> members / creation_state vs plan -> mode -> breaker -> rate gate.
 2. live precheck of every selected page (read-only client); more than 20 % inconsistent pages -> aborted
    (a page counts only for a live drift / refusal, class CONFLICT_BOTH; an AUTO_SYNC_HOLD / NOT_LEAF skip
    skips only its own page).
 3. per page (mode + ``state/WRITEBACK_STOP`` re-checked before each page and each request): intent rows
    for the page's ops are already in the journal; UPDATE -> APPEND -> TRASH_BLOCK -> properties -> title ->
    page trash; single-block re-GET before each UPDATE/TRASH; every request is preceded by an ``intent``
    row (fsync'ed), marked ``sent`` by the client's ``on_send`` hook just before the HTTP send, then
    ``done`` / ``unknown`` / ``failed``. Non-idempotent requests (APPEND, POST pages) are NEVER resent; an
    unknown outcome is reconciled by set difference and the page is BLOCKED. Any APPEND failure stops the
    page (no TRASH sent) -> BLOCKED. A block-limit 403 -> creation_state blocked_confirmed (persistent).
    New pages: created -> filling -> complete; a failure while filling -> PARTIAL_CREATE; the mapping is
    written right after the POST, the note's frontmatter only while the vault sha still equals the plan's
    L sha (backup first).
 4. read-back of every touched page (read-only client) against ``md2blocks.canon_writable`` of the
    expected block (Notion merges rich text: D-12), untouched blocks byte-equal (json_norm), bot authorship.
 5. targeted refetch (wb_refetch.choose_base + refetch) -> exporter -> staging verify -> publish_vault
    ``--adopt-writeback`` (normal publish with the refresh holds; ``--only-ids`` = the plan's notes while an
    unreleased paused_for_review round exists; a normal publish that would write anything but the adopted notes,
    their refetched parents and the generated index falls back to ``--only-ids`` = adopted + parents) -> verify
    ``--ids`` (``--snapshot-immutable``, L-05) -> W1–W10 -> ``reports/writeback/<P>/result.md`` + vault
    ``Notion_Obsidian/运行记录/写回-<P>.md``.

Defence in depth (review fix round): page-level ops must target the item's own page; M items never change a title
or a read-only property; blocks holding a mention / coloured text / Notion link (X) are never updated or trashed;
a 429/529 resend re-checks STOP + mode; a create whose outcome cannot be identified is recorded as an `unknown`
mapping and never POSTed again; any unexpected exception closes the plan with a result (never left `applying`).

Lead decision L-18 (apply / resume): plan["mode"] must equal the current effective mode (start check: 0 writes;
re-checked before every write); in stage 2 (mode below full) the target page of M/B/T, every D target and the parent
page of an N are re-checked live to be leaf pages (no child_page / child_database at any depth); before each page
the object must not be held (state/auto_sync_hold.json, pending_deletes=hold; subtree holds via the parent chain;
an N parent also not BLOCKED); a note with any unexecuted item of the plan (or a local change the plan does not write) is never
adopted: it is held from the adoption publish (stays local, LOCAL_ONLY) and listed as 待人工处理 with the reason.

Journal states: intent -> sent -> done | unknown | failed;  unknown -> reconciled;  intent -> cancelled
(never sent: the page/run stopped first). ``apply --resume`` only sends ``intent`` rows; ``sent`` rows of a
crashed run are unknown (never resent).

Op vocabulary accepted in ``plan.json`` items (speccheck §6 shape; documented in the I2 report):
  UPDATE        {target_id, before_norm_sha, request:{<type>:{...}}, expected_after?}
  APPEND        {parent_id, position?, children:[...], anchor_next_id? | parent_child_ids?, new_blocks?}
  TRASH_BLOCK   {target_id, before_norm_sha}                      (B items only, allow_block_trash)
  UPDATE_PROPERTIES {page_id?, properties:{name: value}, before:{name: value}}
  RENAME        {page_id?, title_property?, old_title:[rich_text], new_title:"..."}   (T items)
  TRASH_PAGE    {page_id?, last_edited_time?, page_json_sha?, subtree_ids?}           (D items)
  CREATE_PAGE   {parent:{page_id|data_source_id}, title, title_property?, properties?, children:[...]} (N)
``before_norm_sha`` = ``wb_norm.block_json_sha(block)``; ``page_json_sha`` = ``wb_norm.page_json_sha``.

There is no flag, environment variable or file that skips any of the checks above.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import md2blocks  # noqa: E402
import publish_vault  # noqa: E402  (shared race-aware vault swap)
import wb_common  # noqa: E402
from wb_common import cli_command
import wb_config  # noqa: E402
import wb_locks  # noqa: E402
import wb_norm  # noqa: E402
import wb_state  # noqa: E402
from readonly_client import ReadOnlyClient  # noqa: E402  (puts nmig on sys.path)
from nmig.client import NotionAPIError  # noqa: E402
from writeback_client import WriteForbidden, WritebackClient, norm_id  # noqa: E402

STOP_FILE = "WRITEBACK_STOP"
EXEC_CATEGORIES = ("M", "N", "B", "D", "T")
BUCKETS = {"items": ("M", "N"), "delete": ("D",), "delete_blocks": ("B",), "rename": ("T",), "move": ("MV",),
           "resolve": ("C",)}
WRITE_MODES = ("whitelist", "full")
PHASE = {"UPDATE": 0, "APPEND": 1, "TRASH_BLOCK": 2, "UPDATE_PROPERTIES": 3, "RENAME": 4, "TRASH_PAGE": 5}
OP_ALIASES = {"PROPS": "UPDATE_PROPERTIES", "UPDATE_PROPS": "UPDATE_PROPERTIES", "PROPERTIES": "UPDATE_PROPERTIES",
              "TITLE": "RENAME", "UPDATE_TITLE": "RENAME", "TRASH": "TRASH_PAGE", "CREATE": "CREATE_PAGE",
              "DELETE_BLOCK": "TRASH_BLOCK"}
NEVER_TRASH_TYPES = frozenset({"child_page", "child_database", "link_to_page", "synced_block"})
INCONSISTENT_ABORT_RATIO = 0.20
PAUSE_RECHECK_S = 30.0
W_KEYS = tuple(f"W{i}" for i in range(1, 11))
PLAN_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,80}")
VERIFY_SCOPED = ("C03", "C04", "C06", "C08", "C10")
VERIFY_FULL = ("C01", "C02", "C05")
PROP_SIMPLE = ("number", "checkbox", "url", "email", "phone_number")
LIST_PROPS = ("title", "rich_text", "relation", "people")
MAPPING_PREFIX = "created_page:"
BOT_META = "writeback_bot_id"
# row properties a M item may write (WB 2.4.1); the title only through a T item, read-only types never
WRITABLE_PROP_TYPES = frozenset({"rich_text", "number", "select", "multi_select", "status", "date", "checkbox",
                                 "url", "email", "phone_number", "relation", "people"})
PAGE_OPS = ("UPDATE_PROPERTIES", "RENAME", "TRASH_PAGE")
NOTION_LINK_RE = re.compile(r"^(/|#)|notion\.(so|site)/|app\.notion\.com/", re.I)


class ApplyRefused(Exception):
    """A start check failed: nothing was sent to Notion."""

    def __init__(self, reason, abort=False):
        super().__init__(reason)
        self.reason, self.abort = reason, abort


class _StopRun(Exception):
    """Stop the whole run now (STOP file, mode drop, WriteForbidden = plan bug)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _PageStop(Exception):
    """Stop the current page. blocked=True -> page_state BLOCKED (something may have been written)."""

    def __init__(self, reason, blocked=False, partial_create=False):
        super().__init__(reason)
        self.reason, self.blocked, self.partial_create = reason, blocked, partial_create


class _GuardAbort(Exception):
    """Raised from the client's on_send hook before a 429/529 RESEND when the STOP file appeared or the
    effective mode dropped during the Retry-After pause: the resend is never made."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# =============================================================================================== paths
PROD_WORK = "/data120/Obsidian/notion-obsidian"


def vault_problem(paths, plan):
    """None, or why this run must not use `paths.vault` (r1-never-unapproved-05): no vault outside the production
    WORK, a vault that is not a real directory, or a vault other than the one the plan was built from."""
    if not paths.vault:
        return "需要库路径（--vault）：开发/演练环境不会默认使用正式库；未发送任何请求"
    if not os.path.isdir(paths.vault) or os.path.islink(paths.vault):
        return f"库路径不存在或是符号链接：{paths.vault}；未发送任何请求"
    pv = (plan or {}).get("vault")
    if pv and os.path.realpath(pv) != os.path.realpath(paths.vault):
        return (f"计划是按库 {pv} 生成的，本次库路径为 {paths.vault}：只能对生成计划时的库执行（请用相同的 --vault）；"
                "未发送任何请求")
    return None


class Paths:
    def __init__(self, work=None, vault=None):
        self.work = os.path.abspath(work or wb_common.WORK)
        self.state = os.path.join(self.work, "state")
        self.reports = os.path.join(self.work, "reports", "writeback")
        self.stop = os.path.join(self.state, STOP_FILE)
        self.export_db = os.path.join(self.state, "export.db")
        self.snapshots_wb = os.path.join(self.work, "snapshots", "wb")
        self.staging = os.path.join(self.work, "staging")
        self.backup = os.path.join(self.work, "backup")
        self.approvals = os.path.join(self.work, "approvals", "writeback")
        self.config = os.path.join(self.work, "config", "writeback.json")
        self.auto_hold = os.path.join(self.state, "auto_sync_hold.json")
        # r1-never-unapproved-05: the production vault is the default ONLY for the production WORK; any other WORK
        # (the sandbox's own default, a --work rehearsal) has no vault unless one is given: apply / resume / rollback
        # then refuse (vault_problem) instead of reading or writing /data120/Obsidian/Godcreate
        if vault:
            self.vault = os.path.abspath(vault)
        elif os.path.realpath(self.work) == os.path.realpath(PROD_WORK):
            self.vault = os.path.abspath(wb_common.PROD_VAULT)
        else:
            self.vault = None

    def plan_dir(self, plan_id):
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise ApplyRefused(f"非法 plan_id：{plan_id!r}")
        return os.path.join(self.reports, plan_id)


# =============================================================================================== small helpers
def _now(now=None):
    if now is None:
        return datetime.datetime.now().astimezone()
    if isinstance(now, datetime.datetime):
        return now if now.tzinfo else now.astimezone()
    if callable(now):
        return _now(now())
    raise TypeError("now must be a datetime")


def _dt(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _minute(d):
    return d.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0) if d else None


def _jdump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _jload(s, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _nid(x):
    return norm_id(x) if isinstance(x, str) else None


def _sha_json(obj):
    return wb_common.sha256_bytes(wb_common.canon_json(obj).encode("utf-8"))


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    # Scanner paths may contain surrogate-escaped undecodable filenames. Preserve them as printable JSON escapes
    # instead of allowing adoption verification to fail while writing expected deviations.
    wb_common.atomic_write(path, (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode(
        "utf-8", "backslashreplace"))


def _rich_plain(rich):
    return "".join((s.get("plain_text") if s.get("plain_text") is not None else (s.get("text") or {}).get("content", ""))
                   for s in rich or [] if isinstance(s, dict))


def _op_name(op):
    n = str((op or {}).get("op") or "").upper()
    return OP_ALIASES.get(n, n)


def _block_type(b):
    return (b or {}).get("type")


def _norm_selection(selection):
    """Selection (I1 dataclass) | dict of buckets | list of item ids -> {bucket: [ids]}."""
    if selection is None:
        return {k: [] for k in BUCKETS}
    if dataclasses.is_dataclass(selection):
        selection = dataclasses.asdict(selection)
    if isinstance(selection, (list, tuple, set)):
        out = {k: [] for k in BUCKETS}
        for i in selection:
            pre = str(i).split("-", 1)[0]
            bucket = next((b for b, cats in BUCKETS.items() if pre in cats), None)
            if bucket is None:
                raise ApplyRefused(f"选择项 {i!r} 不是可执行的项目编号")
            out[bucket].append(str(i))
        return out
    if not isinstance(selection, dict):
        raise ApplyRefused("selection 格式无效")
    out = {}
    for k in BUCKETS:
        v = selection.get(k) or []
        if not isinstance(v, (list, tuple)):
            raise ApplyRefused(f"selection.{k} 必须是列表")
        out[k] = [str(x) for x in v]
    unknown = set(selection) - set(BUCKETS)
    if unknown - {"all_safe", "delete_all_in_plan"}:
        raise ApplyRefused(f"selection 含未知键：{sorted(unknown)}")
    return out


def _sel_ids(sel):
    out = []
    for k in BUCKETS:
        for i in sel.get(k) or []:
            if i not in out:
                out.append(i)
    return out


# =============================================================================================== plan loading
def load_plan_verified(plan_id, *, work=None, db):
    """(plan, plan_sha256) — plan.json bytes must hash to the sha recorded in writeback.db `plans`."""
    paths = Paths(work)
    p = os.path.join(paths.plan_dir(plan_id), "plan.json")
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ApplyRefused(f"无法读取计划文件 {p}：{exc.__class__.__name__}") from None
    sha = wb_common.sha256_bytes(raw)
    row = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise ApplyRefused(f"writeback.db 中没有计划 {plan_id}")
    if (row["plan_sha256"] or "") != sha:
        raise ApplyRefused("plan.json 的 sha256 与 writeback.db 记录不一致（计划文件被改动？）")
    try:
        plan = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApplyRefused("plan.json 不是有效的 JSON") from None
    if not isinstance(plan, dict) or plan.get("plan_id") not in (None, plan_id):
        raise ApplyRefused("plan.json 的 plan_id 不匹配")
    if not isinstance(plan.get("items"), list):
        raise ApplyRefused("plan.json 缺少 items")
    ids = [it.get("item_id") for it in plan["items"] if isinstance(it, dict)]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        raise ApplyRefused("plan.json 的项目编号重复或缺失（计划文件异常）")
    return plan, sha, dict(row)


# =============================================================================================== context
class Ctx:
    """State of one apply / resume / rollback run."""

    def __init__(self, *, plan_id, plan, plan_sha, plan_row, client, read_client, cfg, cfg_path, db, paths, now,
                 label):
        self.plan_id, self.plan, self.plan_sha, self.plan_row = plan_id, plan, plan_sha, plan_row
        self.client, self.rc, self.cfg, self.cfg_path, self.db = client, read_client, cfg, cfg_path, db
        self.paths, self._now, self.label = paths, now, label
        self.items = {it.get("item_id"): it for it in plan.get("items", []) if isinstance(it, dict)}
        self.order = [it.get("item_id") for it in plan.get("items", []) if isinstance(it, dict)]
        self.plan_created = _minute(_dt(plan.get("created_at")) or _dt(plan_row.get("created_at")))
        self.bot = None
        self.sends = []            # (method, rel, seq) from on_send
        self.cur_seq = None
        self.item_res = {}         # item_id -> {"status", "reason", ...}
        self.page_res = {}         # page key -> {"status", "reason"}
        self.blocked_pages = []
        self.readback = {"W1": [], "W2": []}
        self.block_limit_hit = False
        self.creation_state = None
        self.members = None
        self.audit = None
        self.abort_reason = None
        self.created = {}          # item_id -> new page id
        self.l_bytes = {}          # item_id -> vault bytes at precheck (W6 comparison)
        self.ours = set()          # block / page ids created by this run (not "unplanned")
        self.paused_s = 0.0
        self.notes = []
        self.selected = set()
        self.tree_ids = {}         # page id -> block ids of its prechecked tree (W3 target binding)
        self.trashed_here = set()  # block ids trashed (done) by this run (nested B deletes, post-order)
        self.fm_skipped = set()    # N item ids whose frontmatter write-back was skipped (vault changed)
        self.adopt_holdback = {}   # L-18 (d): note key -> executed note NOT adopted (other items unwritten)
        self._objmap = None        # export.db objects_map {notion_id: parent_id} (hold ancestors), lazily read

    def now(self):
        return _now(self._now)

    # -------------------------------------------------- guards
    def stop_requested(self):
        return os.path.exists(self.paths.stop)

    def check_stop(self):
        if self.stop_requested():
            raise _StopRun(f"急停文件 {self.paths.stop} 存在：立即停止")

    def effective_mode(self):
        cfg = self.cfg
        if self.cfg_path:
            try:
                cfg = wb_config.load(self.cfg_path)
            except Exception as exc:  # noqa: BLE001 - unreadable config = no write mode
                return "off", [f"配置文件不可读：{exc}"]
            self.cfg = cfg
        info = wb_config.effective_mode_info(cfg, self.db)
        return info["mode"], info["reasons"]

    def guard_write(self):
        """Before every write request (and every page): STOP file and effective mode (re-read)."""
        self.check_stop()
        mode, why = self.effective_mode()
        if mode not in WRITE_MODES:
            raise _StopRun(f"有效模式已降为 {mode}（{'；'.join(why) or '配置'}）：停止写入")
        if self.label in ("apply", "resume") and mode != self.plan.get("mode"):
            # L-18 (b): a plan is executed only under the mode it was built (and its stage rules checked) in
            raise _StopRun(f"有效模式已变为 {mode}，与计划生成时的模式 {self.plan.get('mode') or '（未记录）'} 不同："
                           "停止写入（计划只能在生成它的模式下执行）")

    # -------------------------------------------------- reads (read-only client; STOP checked before each)
    def get_page(self, pid):
        self.check_stop()
        return self.rc.retrieve_page(pid)

    def get_block(self, bid):
        self.check_stop()
        return self.rc.retrieve_block(bid)

    def children(self, bid):
        self.check_stop()
        return self.rc.list_children(bid)

    def prop_items(self, pid, prop_id):
        self.check_stop()
        return self.rc.retrieve_page_property(pid, prop_id)

    def query(self, dsid, body=None):
        self.check_stop()
        return self.rc.query_data_source(dsid, body or {})

    def bot_id(self):
        if self.bot is None:
            self.bot = _bot_id(self.client, self)
            try:      # remembered for reconcile (read client only): created_by filter of set differences
                if wb_state.get_meta(self.db, BOT_META) != self.bot:
                    wb_state.set_meta(self.db, BOT_META, self.bot)
            except sqlite3.Error:
                pass
        return self.bot


def _bot_id(client, ctx=None):
    bid = getattr(client, "bot_id", None)
    if bid:
        return _nid(bid)
    if ctx is not None:
        ctx.check_stop()
    reader = getattr(client, "_reader", None)
    me = reader.me() if reader is not None else None
    if not isinstance(me, dict) or not me.get("id"):
        raise ApplyRefused("无法确定写回集成的 bot 用户（users/me）")
    try:
        client.bot_id = _nid(me["id"])
    except AttributeError:
        pass
    return _nid(me["id"])


# =============================================================================================== journal
def _j_insert(ctx, item_id, op, target, before, request, expected, state="intent"):
    with wb_state.transaction(ctx.db):
        cur = ctx.db.execute(
            "INSERT INTO journal(plan_id, item_id, op, target_id, state, before_json, request_json, "
            "expected_after_json, ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (ctx.plan_id, item_id, op, target, state, _jdump(before), _jdump(request), _jdump(expected),
             wb_common.now_iso()))
        return cur.lastrowid


def _j_update(ctx, seq, **cols):
    if not cols:
        return
    keys = sorted(cols)
    vals = [_jdump(cols[k]) if k in ("before_json", "request_json", "expected_after_json", "after_json",
                                      "committed_ids") else cols[k] for k in keys]
    with wb_state.transaction(ctx.db):
        ctx.db.execute(f"UPDATE journal SET {', '.join(k + '=?' for k in keys)}, ts=? WHERE seq=?",
                       vals + [wb_common.now_iso(), seq])


def _j_row(db, seq):
    r = db.execute("SELECT * FROM journal WHERE seq=?", (seq,)).fetchone()
    return dict(r) if r else None


def _j_rows(db, plan_id, states=None):
    q = "SELECT * FROM journal WHERE plan_id=?"
    args = [plan_id]
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"
        args += list(states)
    return [dict(r) for r in db.execute(q + " ORDER BY seq", args)]


def _result_info(res):
    return {"outcome": res.outcome, "code": res.code, "status": res.status, "ids": list(res.committed_ids or []),
            "verify_required": bool(getattr(res, "verify_required", False)), "message": (res.message or "")[:300],
            "paused_s": getattr(res, "paused_s", 0.0)}


def _send(ctx, seq, fn, *args):
    """Send one journaled write. The row becomes 'sent' in the client's on_send hook (just before the HTTP
    send); the outcome is recorded right after. WriteForbidden (policy) -> the row is 'failed', run stops."""
    ctx.guard_write()
    ctx.cur_seq = seq
    try:
        try:
            res = fn(*args)
        except WriteForbidden as exc:
            _j_update(ctx, seq, state="failed", committed_ids={"outcome": "forbidden", "message": str(exc)[:300]})
            raise _StopRun(f"写入策略拒绝（计划缺陷）：{exc}") from None
        except _GuardAbort as exc:
            # the previous attempt got 429/529 (not processed by Notion); the resend was never made
            _j_update(ctx, seq, state="failed", committed_ids={"outcome": "not_sent", "code": "stopped_before_retry",
                                                               "message": exc.reason[:300]})
            raise _StopRun(exc.reason) from None
    finally:
        ctx.cur_seq = None
    info = _result_info(res)
    ctx.paused_s += info["paused_s"] or 0.0
    if res.outcome == "ok":
        state = "done"
    elif res.outcome == "unknown":
        state = "unknown"
    else:
        state = "failed"
    _j_update(ctx, seq, state=state, response_status=res.status, committed_ids=info)
    return res


def _install_hook(ctx):
    prev = getattr(ctx.client, "on_send", None)

    def on_send(method, rel, attempt):
        seq = ctx.cur_seq
        if attempt > 1:
            # a 429/529 resend after the Retry-After pause: STOP file + effective mode again (WB §11.3: before
            # EVERY request); raising here aborts the resend before any I/O
            try:
                ctx.guard_write()
            except _StopRun as exc:
                raise _GuardAbort(exc.reason) from None
        ctx.sends.append((method, rel, seq))
        if seq is not None and attempt == 1:
            _j_update(ctx, seq, state="sent")
        if prev is not None:
            prev(method, rel, attempt)

    ctx.client.on_send = on_send
    return prev


# =============================================================================================== state helpers
def _set_page_state(ctx, nid, state, reason):
    with wb_state.transaction(ctx.db):
        ctx.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?) "
                       "ON CONFLICT(notion_id) DO UPDATE SET state=excluded.state, reason=excluded.reason, "
                       "since=excluded.since", (nid, state, str(reason)[:500], wb_common.now_iso()))


def _page_state(db, nid):
    r = db.execute("SELECT state, reason FROM page_state WHERE notion_id=?", (nid,)).fetchone()
    return (r["state"], r["reason"]) if r else (None, None)


def _set_item(ctx, item_id, status, reason=None, **extra):
    r = ctx.item_res.setdefault(item_id, {"item_id": item_id})
    r.update(status=status, reason=reason, **extra)
    decision = {"executed": "executed", "skipped": "skipped_precheck", "failed": "failed", "unknown": "unknown",
                "blocked": "failed", "not_executed": "confirmed", "cancelled": "confirmed",
                "refused": "confirmed"}.get(status, status)
    try:
        with wb_state.transaction(ctx.db):
            ctx.db.execute("UPDATE plan_items SET decision=?, reason=COALESCE(?, reason) WHERE plan_id=? AND item_id=?",
                           (decision, reason, ctx.plan_id, item_id))
    except sqlite3.Error:
        pass


def _latest_creation_state(db):
    r = db.execute("SELECT * FROM creation_state ORDER BY rowid DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _record_block_limit(ctx):
    ctx.block_limit_hit = True
    ctx.creation_state = "blocked_confirmed"
    with wb_state.transaction(ctx.db):
        ctx.db.execute("INSERT INTO creation_state(ts, members, plan, state, source) VALUES(?,?,?,?,?)",
                       (wb_common.now_iso(), ctx.members, ctx.cfg.get("notion_plan"), "blocked_confirmed",
                        f"apply:{ctx.plan_id}"))


def _mapping(db, rel):
    v = wb_state.get_meta(db, MAPPING_PREFIX + rel)
    return _jload(v) if v else None


def _set_mapping(ctx, rel, data):
    wb_state.set_meta(ctx.db, MAPPING_PREFIX + rel, data)


# =============================================================================================== members / creation state
def count_members(read_client):
    """{"count": persons | None, "status": ok | unknown, "error"} (names are never kept, WB §9.2)."""
    try:
        n = sum(1 for u in read_client.paginate("GET", "users") if isinstance(u, dict) and u.get("type") == "person")
        return {"count": n, "status": "ok", "error": None}
    except NotionAPIError as exc:
        return {"count": None, "status": "unknown", "error": f"{exc.status} {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - network etc.: unknown, never a failure
        return {"count": None, "status": "unknown", "error": exc.__class__.__name__}


def creation_state_now(cfg, db, member_info):
    """(state, effective_members|None). blocked_confirmed is persistent (WB §9.2)."""
    last = _latest_creation_state(db)
    if last and last.get("state") == "blocked_confirmed":
        return "blocked_confirmed", last.get("members")
    cnt = member_info.get("count")
    eff = None if cnt is None else max(int(cfg.get("workspace_members") or 1), cnt)
    if str(cfg.get("notion_plan") or "free").lower() != "free":
        return "unlimited", eff
    if eff == 1:
        return "unlimited", eff
    return "blocked_assumed", eff


# =============================================================================================== scope
def _in_scope_page(ctx, page):
    """Exact-id whitelist (WB §2.3): whitelisted page, the test page, a row of a whitelisted database, or a page
    this tool created (mapping) under one of those."""
    pid = _nid(page.get("id"))
    cfg = ctx.cfg
    allowed = set(cfg.get("whitelist_page_ids") or [])
    if cfg.get("test_page_id"):
        allowed.add(cfg["test_page_id"])
    if pid in allowed:
        return True
    par = page.get("parent") or {}
    db_id = _nid(par.get("database_id"))
    if db_id and db_id in set(cfg.get("whitelist_database_ids") or []):
        return True
    created = _created_ids(ctx.db)
    if pid not in created:
        return False
    # r1-never-unapproved-06: a created page counts only while its LIVE parent is itself in scope (a whitelisted /
    # test page, a whitelisted database, or another page this tool created under one of those)
    seen = {pid}
    for _ in range(16):
        ppid = _nid(par.get("page_id"))
        if not ppid:
            return False
        if ppid in allowed:
            return True
        if ppid not in created or ppid in seen:
            return False
        seen.add(ppid)
        try:
            par = (ctx.get_page(ppid) or {}).get("parent") or {}
        except NotionAPIError:
            return False
        db_id = _nid(par.get("database_id"))
        if db_id and db_id in set(cfg.get("whitelist_database_ids") or []):
            return True
    return False


def _created_ids(db):
    """Pages this tool created (r1-never-unapproved-06): a wb_meta created_page mapping counts only when a journal
    CREATE_PAGE row of an apply-confirmed item of the same plan committed exactly that page id (done, or reconciled
    as committed). A forged or stale mapping row alone never widens the write scope or hides an object from
    audit-scope."""
    mapped = {}
    for r in db.execute("SELECT value FROM wb_meta WHERE key LIKE ?", (MAPPING_PREFIX + "%",)):
        v = _jload(r["value"], {}) or {}
        if _nid(v.get("page_id")):
            mapped[_nid(v.get("page_id"))] = v
    if not mapped:
        return set()
    committed = set()
    for r in db.execute("SELECT plan_id, item_id, state, committed_ids FROM journal WHERE op='CREATE_PAGE' AND "
                        "state IN ('done','reconciled')"):
        info = _jload(r["committed_ids"], {}) or {}
        rec = str(info.get("reconciled", ""))
        if r["state"] == "reconciled" and (rec.endswith("not_committed") or rec == "not_applied_by_get"):
            continue
        if db.execute("SELECT 1 FROM confirmations WHERE plan_id=? AND item_id=? AND challenge_ok=1 AND "
                      "phrase_scope='apply'", (r["plan_id"], r["item_id"])).fetchone() is None:
            continue
        for i in info.get("ids") or []:
            m = mapped.get(_nid(i))
            if m is not None and m.get("plan_id") in (None, r["plan_id"]):
                committed.add(_nid(i))
    return committed


def _tool_trashed_pages(db):
    """{page_id: plan_id} of pages whose LATEST write by this tool is a committed TRASH_PAGE of an apply-confirmed D
    item (journal done, or reconciled as committed) that no later rollback (RB_RESTORE_PAGE done) undid
    (r1-user-journey-01: audit-scope does not count such a whitelisted page as unreachable)."""
    last = {}
    for r in db.execute("SELECT seq, plan_id, item_id, op, target_id, state, committed_ids FROM journal WHERE op IN "
                        "('TRASH_PAGE','RB_RESTORE_PAGE') AND state IN ('done','reconciled') ORDER BY seq"):
        info = _jload(r["committed_ids"], {}) or {}
        rec = str(info.get("reconciled", ""))
        if r["state"] == "reconciled" and (rec.endswith("not_committed") or rec == "not_applied_by_get"):
            continue
        if r["op"] == "TRASH_PAGE" and db.execute(
                "SELECT 1 FROM confirmations WHERE plan_id=? AND item_id=? AND challenge_ok=1 AND phrase_scope='apply'",
                (r["plan_id"], r["item_id"])).fetchone() is None:
            continue
        last[_nid(r["target_id"])] = (r["op"], r["plan_id"])
    return {pid: plan for pid, (op, plan) in last.items() if pid and op == "TRASH_PAGE"}


def _trashed_by_tool(client, page, trashed):
    """plan_id when `page` (in the trash) or an ancestor page of it was trashed by this tool (see above), else None."""
    cur = page
    for _ in range(32):
        pid = _nid((cur or {}).get("id"))
        if pid in trashed:
            return trashed[pid]
        ppid = _nid(((cur or {}).get("parent") or {}).get("page_id"))
        if not ppid:
            return None
        try:
            cur = client.get_page(ppid)
        except NotionAPIError:
            return None
    return None


def _parent_in_scope(ctx, parent):
    cfg = ctx.cfg
    if parent.get("page_id"):
        pid = _nid(parent["page_id"])
        allowed = set(cfg.get("whitelist_page_ids") or [])
        if cfg.get("test_page_id"):
            allowed.add(cfg["test_page_id"])
        return pid in allowed
    if parent.get("data_source_id"):
        try:
            ds = ctx.rc.retrieve_data_source(_nid(parent["data_source_id"]))
        except NotionAPIError:
            return False
        dbid = _nid((ds.get("parent") or {}).get("database_id"))
        return dbid in set(cfg.get("whitelist_database_ids") or [])
    return False


# =============================================================================================== audit-scope
def audit_scope(*, client, cfg, db, record=True):
    """WB §2.3: everything the write-back integration can reach (POST search, paginated) vs the declared
    scope. Extra objects or unreachable whitelisted ids -> not ok -> wb_meta forced_mode=plan (record)."""
    declared_pages = set(cfg.get("whitelist_page_ids") or [])
    if cfg.get("test_page_id"):
        declared_pages.add(cfg["test_page_id"])
    declared_dbs = set(cfg.get("whitelist_database_ids") or [])
    created = _created_ids(db)
    try:
        found = client.search_all("")
    except NotionAPIError as exc:
        out = {"ok": False, "extra": [], "unreachable": [], "error": f"search failed: {exc.status} {exc.code}",
               "checked": 0}
        if record:
            wb_config.record_downgrade(db, "plan", "audit-scope 无法完成（search 失败）")
        return out
    extra, seen_pages, seen_dbs = [], set(), set()
    parent_of = {_nid(o.get("id")): (o.get("parent") or {}) for o in found if isinstance(o, dict)}

    def created_in_scope(oid):
        # r1-never-unapproved-06: a page this tool created is excused only under a currently declared parent
        seen = set()
        while oid in created and oid not in seen:
            seen.add(oid)
            par = parent_of.get(oid)
            if par is None:
                try:
                    par = (client.get_page(oid) or {}).get("parent") or {}
                except NotionAPIError:
                    return False
            if _nid(par.get("database_id")) in declared_dbs:
                return True
            ppid = _nid(par.get("page_id"))
            if ppid in declared_pages:
                return True
            oid = ppid
        return False
    for o in found:
        if not isinstance(o, dict):
            continue
        oid = _nid(o.get("id"))
        obj = o.get("object")
        par = o.get("parent") or {}
        if obj == "page":
            seen_pages.add(oid)
            dbid = _nid(par.get("database_id"))
            if oid in declared_pages or (dbid and dbid in declared_dbs) or created_in_scope(oid):
                continue
            extra.append({"id": oid, "object": "page", "title": _page_title(o)[:80]})
        elif obj in ("data_source", "database"):
            dbid = _nid(par.get("database_id")) if obj == "data_source" else oid
            seen_dbs.add(dbid)
            if dbid in declared_dbs:
                continue
            extra.append({"id": oid, "object": obj})
        else:
            extra.append({"id": oid, "object": obj})
    unreachable, trashed = [], []
    ours = _tool_trashed_pages(db)
    for pid in sorted(declared_pages - seen_pages):
        try:
            p = client.get_page(pid)
            if p.get("in_trash"):
                # r1-user-journey-01: a whitelisted page that THIS tool moved to the trash (confirmed D item, or a
                # descendant of one) is not a scope drift: still reachable, reported separately, mode unchanged
                by = _trashed_by_tool(client, p, ours) if ours else None
                if by:
                    trashed.append({"id": pid, "reason": "trashed_via_writeback", "plan_id": by})
                else:
                    unreachable.append({"id": pid, "reason": "in_trash"})
        except NotionAPIError as exc:
            unreachable.append({"id": pid, "reason": f"{exc.status} {exc.code}"})
    for did in sorted(declared_dbs - seen_dbs):
        unreachable.append({"id": did, "reason": "not_found_by_search"})
    ok = not extra and not unreachable
    out = {"ok": ok, "extra": extra, "unreachable": unreachable, "trashed_via_writeback": trashed,
           "checked": len(found), "at": wb_common.now_iso()}
    if not ok and record:
        wb_config.record_downgrade(db, "plan", f"audit-scope：多出 {len(extra)} 个对象，不可达 {len(unreachable)} 个")
    try:
        wb_state.set_meta(db, "last_audit_scope", {k: out[k] for k in ("ok", "at", "checked")} |
                          {"extra": len(extra), "unreachable": len(unreachable)})
    except sqlite3.Error:
        pass
    return out


def audit_recovery_hint():
    audit = cli_command("writeback", "audit-scope")
    plan = cli_command("writeback", "plan")
    return ("处理方法：多出的对象 → 在 Notion 中取消写回集成对它的共享（或由用户本人把它的 id 加入 "
            "config/writeback.json 的白名单）；不可达的白名单页面 → 重新共享给写回集成，或（已删除、不再需要写回时）"
            f"从 whitelist_page_ids 中删除；然后运行 `{audit}`，通过后回答 y 解除自动降级，再运行 `{plan}`。")



def audit_scope_detail(res, limit=5):
    """One Chinese line naming what audit-scope found (ids + reasons) and the recovery steps (r1-user-journey-01)."""
    parts = []
    ex = res.get("extra") or []
    un = res.get("unreachable") or []
    if ex:
        parts.append("多出：" + "、".join(f"{x.get('object')} {x.get('id')}" + (f"「{x['title']}」" if x.get("title") else "")
                                         for x in ex[:limit]) + ("…" if len(ex) > limit else ""))
    if un:
        reasons = {"in_trash": "在回收站（不是写回删除的）", "not_found_by_search": "search 找不到"}
        parts.append("不可达：" + "、".join(f"{x.get('id')}（{reasons.get(x.get('reason'), x.get('reason'))}）"
                                           for x in un[:limit]) + ("…" if len(un) > limit else ""))
    if res.get("error"):
        parts.append(f"错误：{res['error']}")
    return "；".join(parts + [audit_recovery_hint()])


def _page_title(page):
    for v in (page.get("properties") or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return _rich_plain(v.get("title"))
    return ""


# =============================================================================================== clients
def make_clients(cfg, *, token_path=None, base_url=None, read_token=None, read_token_path=None, gate_path=None,
                 state_dir=None, purpose="apply", timeout=75):
    """(WritebackClient, ReadOnlyClient) behind the shared RateGate. The ONLY place that loads
    writeback.env (notion_tokens.load_writeback_token: 0600 file in a 0700 dir). Tests pass a temp token
    file and the fake base_url. purpose="rollback" enables trash_block on the client, used only to trash
    blocks THIS tool appended (verified against the journal before each request, WB §13)."""
    import notion_tokens
    import rate_gate
    from readonly_client import make_read_client
    if purpose not in ("apply", "rollback", "audit"):
        raise ValueError(f"unknown purpose {purpose!r}")
    token = notion_tokens.load_writeback_token(token_path)
    sd = state_dir or wb_common.STATE
    gate = rate_gate.RateGate(gate_path or os.path.join(sd, "rate_gate.json"),
                              total_rps=float(cfg.get("rate_limit_total_rps") or 2.5))
    allow_trash = bool(cfg.get("allow_block_trash")) or purpose == "rollback"
    kw = {}
    if base_url:
        kw["base_url"] = base_url
    wb = WritebackClient(token, gate=gate, timeout=timeout, silent=bool(cfg.get("send_silent_notifications", True)),
                         allow_block_trash=allow_trash, **kw)
    wb.token_path = token_path
    wb.purpose = purpose
    if read_token is None and read_token_path is None:
        rc = make_read_client(base_url=base_url, gate=gate, rate=None)
    else:
        rc = make_read_client(base_url=base_url, token=read_token, token_path=read_token_path, gate=gate, rate=None)
    return wb, rc


def _check_token(client):
    if not isinstance(client, WritebackClient):
        raise ApplyRefused("写回客户端无效（不是 WritebackClient）")
    tp = getattr(client, "token_path", None)
    if tp is not None or getattr(client, "purpose", None) is not None:
        import notion_tokens
        try:
            notion_tokens.load_writeback_token(tp)
        except Exception as exc:  # noqa: BLE001 - TokenError and OS errors: refuse
            raise ApplyRefused(f"写回凭据检查失败：{exc}") from None


# =============================================================================================== confirmations
def _check_record(record, plan_id, sha, sel, scope):
    if not isinstance(record, dict):
        raise ApplyRefused("缺少确认记录")
    if record.get("plan_id") != plan_id:
        raise ApplyRefused("确认记录的 plan_id 不符")
    if record.get("plan_sha256") != sha:
        raise ApplyRefused("确认记录绑定的 plan_sha256 与计划文件不符")
    if record.get("challenge_ok") is not True:
        raise ApplyRefused("确认记录未通过挑战码校验")
    rs = record.get("phrase_scope")
    if rs is not None and rs != scope:
        raise ApplyRefused(f"确认记录的范围是 {rs}，不是 {scope}")
    if sel is not None and record.get("selection") is not None:
        try:
            rsel = _norm_selection(record.get("selection"))
        except ApplyRefused:
            raise ApplyRefused("确认记录中的选择项无效") from None
        if {k: sorted(v) for k, v in rsel.items()} != {k: sorted(v) for k, v in sel.items()}:
            raise ApplyRefused("本次选择项与确认记录中的选择项不一致")


def _check_confirm_rows(db, plan_id, sha, item_ids, scope):
    missing = []
    for iid in item_ids:
        r = db.execute("SELECT 1 FROM confirmations WHERE plan_id=? AND item_id=? AND plan_sha256=? AND "
                       "challenge_ok=1 AND phrase_scope=?", (plan_id, iid, sha, scope)).fetchone()
        if r is None:
            missing.append(iid)
    if missing:
        raise ApplyRefused(f"以下项目没有匹配的确认记录（plan_id + 完整 sha + {scope}）：{', '.join(missing)}")


def _is_rejected(db, plan_id, item):
    r = db.execute("SELECT decision FROM plan_items WHERE plan_id=? AND item_id=?", (plan_id, item.get("item_id"))).fetchone()
    if r is not None and (r["decision"] or "") == "rejected":
        return True
    nid, rel, l_sha = item.get("notion_id"), item.get("rel_path"), item.get("l_sha")
    rows = db.execute("SELECT notion_id, rel_path, l_sha FROM rejections WHERE active=1").fetchall()
    for x in rows:
        same_obj = (nid and x["notion_id"] == nid) or (rel and x["rel_path"] == rel)
        if same_obj and (x["l_sha"] is None or l_sha is None or x["l_sha"] == l_sha):
            return True
    return False


# =============================================================================================== start checks
def _start_checks(ctx, sel_ids, record, scope, sel=None):
    """Order (task / WB §11.1–11.2): token -> plan validity + sha -> confirmations -> audit-scope ->
    members/creation_state -> mode -> breaker -> rate gate. Raises ApplyRefused (abort=True once the
    confirmation itself is valid: the plan is then marked aborted)."""
    _check_token(ctx.client)
    if not isinstance(ctx.rc, ReadOnlyClient):
        raise ApplyRefused("读取客户端必须是 ReadOnlyClient")
    # plan validity
    exp = _dt(ctx.plan_row.get("expires_at")) or _dt(ctx.plan.get("expires_at"))
    if exp is None or exp <= ctx.now():
        raise ApplyRefused("计划已过期", abort=True)
    _check_record(record, ctx.plan_id, ctx.plan_sha, sel, scope)
    for iid in sel_ids:
        it = ctx.items.get(iid)
        if it is None:
            raise ApplyRefused(f"选择项 {iid} 不在计划中")
        if _is_rejected(ctx.db, ctx.plan_id, it):
            raise ApplyRefused(f"{iid} 已被拒绝（reject），不能执行")
    _check_confirm_rows(ctx.db, ctx.plan_id, ctx.plan_sha, sel_ids, scope)
    if scope != "apply":   # resume / rollback: the original apply confirmation must exist too
        _check_confirm_rows(ctx.db, ctx.plan_id, ctx.plan_sha, sel_ids, "apply")
    ctx.check_stop()
    # audit-scope
    ctx.audit = audit_scope(client=ctx.client, cfg=ctx.cfg, db=ctx.db)
    if not ctx.audit["ok"]:
        raise ApplyRefused(f"audit-scope 不通过（多出 {len(ctx.audit['extra'])} 个对象，不可达 "
                           f"{len(ctx.audit['unreachable'])} 个）：模式已降为 plan；" + audit_scope_detail(ctx.audit),
                           abort=True)
    # members / creation_state
    ctx.check_stop()
    mi = count_members(ctx.rc)
    state, eff = creation_state_now(ctx.cfg, ctx.db, mi)
    ctx.creation_state, ctx.members = state, eff
    pm, ps = ctx.plan.get("members"), ctx.plan.get("creation_state")
    if ps is not None and ps != state:
        raise ApplyRefused(f"块额度状态已变化（计划 {ps} → 现在 {state}）：计划失效", abort=True)
    if pm is not None and eff is not None and int(pm) != int(eff):
        raise ApplyRefused(f"成员数已变化（计划 {pm} → 现在 {eff}）：计划失效", abort=True)
    if pm is not None and eff is None and int(pm) == 1 and state != "unlimited":
        raise ApplyRefused("成员数未知（按多成员处理）：计划失效", abort=True)
    # mode
    mode, why = ctx.effective_mode()
    if mode not in WRITE_MODES:
        raise ApplyRefused(f"有效模式为 {mode}，不允许写入（{'；'.join(why) or '配置 mode'}）", abort=True)
    # L-18 (b): the plan's stage rules (WB 2.3 leaf pages, new-page limits) were evaluated for plan["mode"]; any
    # other current mode (also whitelist -> full) refuses the whole run before a single request
    pmode = ctx.plan.get("mode")
    if pmode != mode:
        nxt = (f"用户本人核对后把模式改回 {pmode}，再运行 {cli_command('writeback', 'apply', '--resume', ctx.plan_id)}" if scope == "resume" and pmode in WRITE_MODES
               else f"请在当前模式下重新运行 {cli_command('writeback', 'plan')}，再按新计划确认")
        raise ApplyRefused(f"计划生成时的模式是 {pmode or '（未记录）'}，当前有效模式是 {mode}：计划只能在生成它的模式下"
                           f"执行，本次没有发送任何写请求；{nxt}", abort=True)
    # breaker
    br = ctx.plan.get("breaker") or {}
    if br.get("tripped"):
        raise ApplyRefused("计划触发了熔断（breaker.tripped），没有可执行项", abort=True)
    # rate gate
    if getattr(ctx.client, "gate", None) is None:
        raise ApplyRefused("写回客户端没有共享限流（RateGate）", abort=True)
    ctx.check_stop()


# =============================================================================================== page tree
def _page_tree(ctx, page_id):
    blocks = wb_norm.page_blocks_dfs(lambda b: ctx.children(b), page_id)
    by_id = {}
    kids = {}
    for b in blocks:
        bid = _nid(b.get("id"))
        by_id[bid] = b
        par = b.get("parent") or {}
        pid = _nid(par.get("block_id") or par.get("page_id")) or page_id
        kids.setdefault(pid, []).append(bid)
    return {"blocks": blocks, "by_id": by_id, "kids": kids}


def _child_ids(ctx, parent_id):
    return [_nid(b.get("id")) for b in ctx.children(parent_id)]


# =============================================================================================== property canon
def _prop_canon(v):
    if not isinstance(v, dict):
        return v
    t = v.get("type")
    if t is None:
        t = next((k for k in v if k not in ("id", "object", "has_more", "next_cursor")), None)
    val = v.get(t)
    if t in ("title", "rich_text"):
        return {"type": t, "value": md2blocks.canon_rich_text(val or [])}
    if t in ("select", "status"):
        return {"type": t, "value": (val or {}).get("name") if isinstance(val, dict) else None}
    if t == "multi_select":
        return {"type": t, "value": [x.get("name") for x in val or [] if isinstance(x, dict)]}
    if t == "date":
        return {"type": t, "value": None if not val else {k: val.get(k) for k in ("start", "end", "time_zone")}}
    if t in PROP_SIMPLE:
        return {"type": t, "value": val}
    if t in ("relation", "people"):
        return {"type": t, "value": sorted(_nid(x.get("id")) or "" for x in val or [] if isinstance(x, dict))}
    return {"type": t, "value": wb_norm.json_norm(val)}


def _full_prop(ctx, page, name):
    """A page property with list values completed through the property-item endpoint when truncated."""
    v = copy.deepcopy((page.get("properties") or {}).get(name))
    if not isinstance(v, dict):
        return None
    t = v.get("type")
    if t in LIST_PROPS and v.get("has_more"):
        res = ctx.prop_items(page["id"], v.get("id") or name)
        items = res.get("results") if isinstance(res, dict) else None
        if isinstance(items, list):
            if t in ("title", "rich_text"):
                v[t] = [i.get(t) for i in items if isinstance(i, dict) and i.get(t)]
            else:
                v[t] = [i.get(t) for i in items if isinstance(i, dict) and i.get(t)]
        v["has_more"] = False
    return v


def _title_prop(page, hint=None):
    props = page.get("properties") or {}
    if hint and hint in props:
        return hint
    return next((k for k, v in props.items() if isinstance(v, dict) and v.get("type") == "title"), "title")


# =============================================================================================== expected values
def _canon_block(b):
    return {"type": _block_type(b), "w": md2blocks.canon_writable(b)}


def _canon_tree(children):
    """Request blocks -> [{"type", "w", "children"}] (D-12 merge; the fake / Notion merge rich text)."""
    out = []
    for c in children or []:
        t = _block_type(c) or next((k for k in c if k not in ("object", "children")), None)
        c2 = dict(c, type=t)
        v = c2.get(t) if isinstance(c2.get(t), dict) else {}
        kids = v.get("children") or c.get("children") or []
        out.append({"type": t, "w": md2blocks.canon_writable(c2), "children": _canon_tree(kids)})
    return out


def _live_tree(ctx, ids):
    out, raw = [], []
    for bid in ids:
        b = ctx.get_block(bid)
        kids_raw = ctx.children(bid) if b.get("has_children") else []
        sub, sub_raw = _live_tree(ctx, [_nid(k.get("id")) for k in kids_raw]) if kids_raw else ([], [])
        out.append({"type": _block_type(b), "w": md2blocks.canon_writable(b), "children": sub})
        raw.append(dict(b, _children=sub_raw))
    return out, raw


def _count_tree(t):
    return sum(1 + _count_tree(x.get("children") or []) for x in t or [])


def _strip_writable(b):
    """json_norm of a block without its writable fields / volatile keys (non-writable part must not change)."""
    n = wb_norm.json_norm(b)
    t = n.get("type")
    v = n.get(t)
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k not in ("rich_text", "checked", "language", "expression", "cells")}
        n[t] = v
    n.pop("has_children", None)
    return n


def _expected_update(before, request):
    return md2blocks.canon_writable(md2blocks.apply_update(before, request))


def _plan_expected_ok(op, mine):
    exp = op.get("expected_after")
    if not isinstance(exp, dict) or not exp:
        return True
    if "type" in exp:
        return md2blocks.canon_writable(exp) == mine
    if set(exp) <= {"rich_text", "checked", "expression", "cells", "code_text", "language"}:
        return exp == mine
    return True


# =============================================================================================== groups
class Group:
    def __init__(self, key, page_id, kind):
        self.key, self.page_id, self.kind = key, page_id, kind     # kind: page | new
        self.items = []
        self.ok_items = []
        self.snap = None
        self.reason = None
        self.steps = []
        self.written = False
        self.new_page = None
        self.trash_deps = {}       # item id of a nested block delete -> item ids deleting its children


def _groups(ctx, item_ids):
    groups, order = {}, []
    for iid in item_ids:
        it = ctx.items[iid]
        if it.get("category") == "N":
            key = "new:" + iid
            g = groups.get(key) or Group(key, None, "new")
        else:
            pid = _nid(it.get("notion_id"))
            if pid is None:
                raise ApplyRefused(f"{iid} 缺少 notion_id")
            key = pid
            g = groups.get(key) or Group(key, pid, "page")
        if key not in groups:
            groups[key] = g
            order.append(key)
        g.items.append(it)
    return [groups[k] for k in order]


def _bind_closure(ctx, items):
    """item_id -> set of item ids bound together (binds, both directions)."""
    adj = {}
    for it in items:
        iid = it.get("item_id")
        adj.setdefault(iid, set())
        for b in it.get("binds") or []:
            adj[iid].add(b)
            adj.setdefault(b, set()).add(iid)
    return adj


# =============================================================================================== vault checks
def _vault_file(ctx, rel):
    try:
        return wb_common.safe_join(ctx.paths.vault, rel)
    except ValueError:
        return None


def _vault_sha(ctx, rel):
    p = _vault_file(ctx, rel) if rel else None
    if not p or not os.path.isfile(p) or os.path.islink(p):
        return None
    return wb_common.sha256_file(p)


def _vault_has_id(ctx, nid):
    """Any .md file in the vault (outside .obsidian / Notion_Obsidian) whose frontmatter carries notion_id."""
    needle = (nid or "").encode()
    root = ctx.paths.vault
    for d, dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, root)
        dirs[:] = [x for x in dirs if not x.startswith(".") and not (rel_d == "." and x == "Notion_Obsidian")]
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(d, f)
            try:
                with open(p, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            if needle and needle in head:
                return os.path.relpath(p, root)
    return None


# =============================================================================================== precheck
def _precheck_group(ctx, g):
    """WB §11.3.1 (read-only). Sets g.ok_items / per-item skip reasons. Never writes."""
    g.ok_items, fails = [], {}
    try:
        if g.kind == "page":
            st, why = _page_state(ctx.db, g.page_id)
            if st in ("BLOCKED", "PARTIAL_CREATE"):
                raise _PageStop(f"BLOCKED_PAGE：页面处于 {st}（{why}）")
            try:
                page = ctx.get_page(g.page_id)
            except NotionAPIError as exc:
                raise _PageStop(f"页面不可读（{exc.status} {exc.code}）") from None
            if page.get("in_trash"):
                raise _PageStop("页面已在回收站")
            if page.get("is_locked"):
                raise _PageStop("LOCKED：页面已锁定")
            if not _in_scope_page(ctx, page):
                raise _PageStop("OUT_OF_WHITELIST：页面不在声明的有效范围")
            try:
                tree = _page_tree(ctx, g.page_id)
            except NotionAPIError as exc:
                raise _PageStop(f"页面内容不可读（{exc.status} {exc.code}）") from None
            g.snap = {"page": page, "tree": tree}
            ctx.tree_ids[g.page_id] = set(tree["by_id"])
        else:
            g.snap = {"page": None, "tree": None}
    except _PageStop as exc:
        for it in g.items:
            fails[it["item_id"]] = exc.reason
        g.reason = exc.reason
        return fails
    g.trash_deps = {}
    for it in g.items:
        try:
            _precheck_item(ctx, g, it)
        except _PageStop as exc:
            fails[it["item_id"]] = exc.reason
        except NotionAPIError as exc:
            fails[it["item_id"]] = f"预检读取失败（{exc.status} {exc.code}）"
    # bound items fail together; a nested block delete fails with the deletes of its children
    adj = _bind_closure(ctx, g.items)
    for parent_item, child_items in (getattr(g, "trash_deps", None) or {}).items():
        for c in child_items:
            if c != parent_item:
                adj.setdefault(c, set()).add(parent_item)
    changed = True
    while changed:
        changed = False
        for iid in list(adj):
            if iid in fails:
                for other in adj[iid]:
                    if other not in fails and any(x["item_id"] == other for x in g.items):
                        fails[other] = f"绑定项 {iid} 未通过预检"
                        changed = True
    g.ok_items = [it for it in g.items if it["item_id"] not in fails]
    return fails


def _precheck_item(ctx, g, it):
    cat = it.get("category")
    ops = [dict(o, op=_op_name(o)) for o in it.get("ops") or []]
    if not ops:
        raise _PageStop("计划项没有操作")
    for b in it.get("binds") or []:
        if b not in ctx.selected:
            raise _PageStop(f"绑定的 {b} 未被选择/确认，本项整体不执行")
    _hold_check(ctx, it, (g.snap or {}).get("page"))       # L-18 (c): held since the plan -> not written
    # vault side (WB §11.3.1: the note's sha equals the plan's L sha, for every item with a note)
    rel = it.get("rel_path")
    if cat in ("M", "T", "N", "B"):
        p = _vault_file(ctx, rel) if rel else None
        if not p or not os.path.isfile(p):
            raise _PageStop("库内文件不存在")
        with open(p, "rb") as fh:
            data = fh.read()
        if not it.get("l_sha") or wb_common.sha256_bytes(data) != it["l_sha"]:
            raise _PageStop("库内文件在计划后又被修改（sha 不等于计划时 L sha）")
        ctx.l_bytes[it["item_id"]] = data
    if cat == "D":
        _precheck_delete_vault(ctx, it)
    if cat == "N":
        _precheck_new(ctx, g, it, ops)
        return
    page = g.snap["page"]
    tree = g.snap["tree"]
    _leaf_check(ctx, g, it)                                 # L-18 (a): stage 2 writes leaf pages only (live)
    allowed = {"M": {"UPDATE", "APPEND", "UPDATE_PROPERTIES"}, "B": {"TRASH_BLOCK"}, "T": {"RENAME"},
               "D": {"TRASH_PAGE"}}.get(cat, set())
    for op in ops:
        name = op["op"]
        if name not in allowed:
            raise _PageStop(f"操作 {name} 不属于 {cat} 项（不执行）")
        if name in PAGE_OPS:
            # the request goes to the page that was prechecked: the item's own page, never another one
            for k in ("page_id", "target_id"):
                if op.get(k) is not None and _nid(op.get(k)) != g.page_id:
                    raise _PageStop(f"计划缺陷：{name} 的目标 {str(op.get(k))[:8]} 不是本项的页面（不执行）")
        if name in ("UPDATE", "TRASH_BLOCK"):
            tid = _nid(op.get("target_id") or op.get("block_id"))
            b = tree["by_id"].get(tid)
            if b is None:
                raise _PageStop(f"块 {str(tid)[:8]} 不在该页面中（或已删除）")
            want = op.get("before_norm_sha") or op.get("before_json_sha") or op.get("json_sha")
            if not want and isinstance(op.get("before"), dict):
                want = wb_norm.block_json_sha(op["before"])
            if not want or wb_norm.block_json_sha(b) != want:
                raise _PageStop(f"块 {str(tid)[:8]} 自计划后已改变")
            xr = _x_reason(b)
            if xr:
                raise _PageStop(f"块 {str(tid)[:8]} 含{xr}，属于只读（X）块：不写入（请在 Notion 中修改）")
            if name == "UPDATE":
                req = op.get("request") or {}
                t = _block_type(b)
                if set(req) != {t}:
                    raise _PageStop(f"块 {str(tid)[:8]} 的类型已不是计划中的类型")
                if not _plan_expected_ok(op, _expected_update(b, req)):
                    raise _PageStop("计划中的 expected_after 与请求不一致（计划缺陷）")
            else:
                if not ctx.cfg.get("allow_block_trash") or ctx.creation_state != "unlimited":
                    raise _PageStop("未开启块删除（allow_block_trash=false）或额度受限：B 项不执行")
                if _block_type(b) in NEVER_TRASH_TYPES:
                    raise _PageStop("块删除只允许普通块（不含子页面/子数据库/同步块/链接）")
                if b.get("has_children"):
                    _nested_trash_ok(ctx, g, it, tid)
        elif name == "APPEND":
            if ctx.creation_state != "unlimited" or ctx.block_limit_hit:
                raise _PageStop("块额度受限：不执行 APPEND（需在 Notion 中手工添加或升级套餐）")
            _precheck_append(ctx, g, op)
        elif name == "UPDATE_PROPERTIES":
            dbid = _nid((page.get("parent") or {}).get("database_id"))
            if dbid not in set(ctx.cfg.get("automation_free_database_ids") or []):
                raise _PageStop("AUTOMATION_RISK：所在数据库未声明“无自动化”，行属性不写回")
            before = op.get("before") or {}
            props = op.get("properties") or {}
            if not props:
                raise _PageStop("计划缺陷：UPDATE_PROPERTIES 没有属性")
            for pname, newv in props.items():
                live = _full_prop(ctx, page, pname)
                if live is None:
                    raise _PageStop(f"属性 {pname} 不存在")
                if live.get("type") not in WRITABLE_PROP_TYPES:
                    raise _PageStop(f"属性 {pname} 的类型 {live.get('type')} 不能经 M 项写回"
                                    + ("（标题只能经改名 T 项修改）" if live.get("type") == "title" else "（只读类型）"))
                if not isinstance(newv, dict) or set(newv) - {"type"} != {live.get("type")}:
                    raise _PageStop(f"计划缺陷：属性 {pname} 的新值类型与 Notion 中的类型不符")
                if pname not in before or _prop_canon(live) != _prop_canon(before[pname]):
                    raise _PageStop(f"属性 {pname} 自计划后已改变")
        elif name == "RENAME":
            tp = _title_prop(page, op.get("title_property"))
            live = _full_prop(ctx, page, tp) or {}
            old = op.get("old_title")
            if old is None or md2blocks.canon_rich_text(live.get("title") or []) != md2blocks.canon_rich_text(old):
                raise _PageStop("Notion 当前标题不等于计划中的旧标题（CONFLICT_RENAME）")
            nt = op.get("new_title")
            if not isinstance(nt, str) or not nt.strip() or len(nt) > 2000:
                raise _PageStop("新标题无效")
        elif name == "TRASH_PAGE":
            _precheck_trash_page(ctx, g, op)


def _x_reason(b):
    """Contract §2 write-back inline policy: a block holding a mention, a coloured segment or a text link to a
    Notion page / #anchor is read-only (X). Defence in depth against a planner defect: never UPDATE / TRASH it."""
    t = _block_type(b)
    v = b.get(t) if isinstance(b.get(t), dict) else {}
    segs = list(v.get("rich_text") or [])
    for row in v.get("cells") or []:
        segs += list(row or [])
    for s in segs:
        if not isinstance(s, dict):
            continue
        if s.get("type") == "mention":
            return "提及（mention）"
        if ((s.get("annotations") or {}).get("color") or "default") != "default":
            return "带颜色的文字"
        link = ((s.get("text") or {}).get("link") or {}).get("url") if isinstance(s.get("text"), dict) else None
        if link and NOTION_LINK_RE.search(str(link)):
            return "指向 Notion 页面或锚点的链接"
    return None


# ----------------------------------------------------------------------------------------- L-18 apply-time re-checks
SUBPAGE_TYPES = ("child_page", "child_database")


def _stage2(ctx):
    """WB 2.3 stage-2 rules (leaf pages only) apply below mode full -- like the planner's strict_stage; the plan's
    mode and the current effective mode are both considered (equal anyway after the L-18 (b) start check)."""
    mode, _why = ctx.effective_mode()
    return ctx.plan.get("mode") != "full" or mode != "full"


def _subpages(tree, ignore=()):
    """child_page / child_database blocks anywhere in a page's own block tree (_page_tree: any depth, D-10)."""
    return [_nid(b.get("id")) for b in (tree or {}).get("blocks") or []
            if _block_type(b) in SUBPAGE_TYPES and _nid(b.get("id")) not in ignore]


def _is_row_page(page):
    par = (page or {}).get("parent") or {}
    return bool(par.get("database_id") or par.get("data_source_id"))


def _not_leaf(what, kids):
    return _PageStop(f"NOT_LEAF：阶段 2（mode=whitelist）只写叶子页面；{what}现在含 {len(kids)} 个子页面或子数据库"
                     "（执行前实时读取）：本页不写入。请在 Notion 中手工修改，或在 mode=full 下重新生成计划")


def _leaf_check(ctx, g, it):
    """L-18 (a), WB 2.3 "plan 与 apply 时各检查一次": in stage 2 the target page of an M / B / T item and every D
    target has no child_page / child_database at any depth, read live right before its writes (the planner's
    rule: database rows are exempt for M / B / T). Pages created by this run itself are not counted."""
    cat = it.get("category")
    if cat not in ("M", "B", "T", "D") or not _stage2(ctx):
        return
    if cat != "D" and it.get("page_kind") != "page" and _is_row_page(g.snap.get("page")):
        return
    kids = _subpages(g.snap.get("tree"), ctx.ours)
    if kids:
        raise _not_leaf("该页面", kids)


def _auto_holds(paths, strict=False):
    """state/auto_sync_hold.json (written by refresh.py) -> {nid: {reason, source, subtree}}. strict: a file that
    exists but cannot be read / parsed raises ValueError (the executor then writes nothing for the object)."""
    if strict and os.path.lexists(paths.auto_hold):
        try:
            with open(paths.auto_hold, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError(f"state/auto_sync_hold.json 不可读（{exc.__class__.__name__}）") from None
        if not isinstance(raw, (dict, list)):
            raise ValueError("state/auto_sync_hold.json 格式无效")
    else:
        raw = _read_json(paths.auto_hold, {}) or {}
    if isinstance(raw, dict) and isinstance(raw.get("holds"), (dict, list)) and len(raw) <= 3:
        raw = raw["holds"]
    items = [(x, {}) for x in raw] if isinstance(raw, list) else list(raw.items()) if isinstance(raw, dict) else []
    out = {}
    for k, v in items:
        nid = _nid(k)
        if not nid:
            continue
        v = v if isinstance(v, dict) else {"reason": str(v)}
        out[nid] = {"reason": str(v.get("reason") or "auto_sync_hold"), "source": v.get("source") or "auto_sync_hold",
                    "subtree": v.get("subtree", True) is not False}
    return out


def _objects_parents(ctx):
    """{notion_id: parent_id} from export.db objects_map (read-only; empty when absent): the parent chain that the
    publisher's subtree holds follow."""
    if ctx._objmap is None:
        out = {}
        p = ctx.paths.export_db
        if os.path.isfile(p):
            try:
                con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=ro", uri=True, timeout=30)
                try:
                    for nid, par in con.execute("SELECT notion_id, parent_id FROM objects_map"):
                        if _nid(nid) and _nid(par):
                            out[_nid(nid)] = _nid(par)
                finally:
                    con.close()
            except sqlite3.Error:
                out = {}
        ctx._objmap = out
    return ctx._objmap


def _hold_ancestors(ctx, nid, live_parents):
    """Ancestor ids of `nid`: the live parent(s) first, then the objects_map parent chains (cycle-safe)."""
    par = _objects_parents(ctx)
    out, seen = [], {nid} if nid else set()
    for start in [p for p in list(live_parents) + [par.get(nid) if nid else None] if p]:
        cur, depth = _nid(start), 0
        while cur and cur not in seen and depth < 200:
            seen.add(cur)
            out.append(cur)
            cur, depth = par.get(cur), depth + 1
    return out


def _hold_check(ctx, it, page=None):
    """L-18 (c): right before the page's writes, the object must not be held: state/auto_sync_hold.json (the
    refresh's object-level pause) and the open deletion holds in writeback.db (pending_deletes=hold), the object
    itself or an ancestor with a subtree hold (live parent + objects_map chain). A hold that appeared after planning
    skips the item (the planner refuses objects held at plan time). D items keep their own deletion hold
    (pending_deletes / a pending_delete reason), which is their precondition. The page's own BLOCKED /
    PARTIAL_CREATE state is refused by _precheck_group; a BLOCKED ancestor does not stop an independent child page
    (apply --resume of a child after its parent crashed), but the parent an N item is created under must not be
    BLOCKED / PARTIAL_CREATE (the create changes that page's child list)."""
    cat = it.get("category")
    try:
        holds = _auto_holds(ctx.paths, strict=True)
    except ValueError as exc:
        raise _PageStop(f"AUTO_SYNC_HOLD：{exc}：无法确认对象是否已暂停同步，不写入") from None
    pend = {_nid(r["notion_id"]) for r in ctx.db.execute("SELECT DISTINCT notion_id FROM pending_deletes "
                                                          "WHERE status='hold'") if _nid(r["notion_id"])}
    blocked = {_nid(r["notion_id"]): r["state"] for r in ctx.db.execute(
        "SELECT notion_id, state FROM page_state WHERE state IN ('BLOCKED','PARTIAL_CREATE')") if _nid(r["notion_id"])}

    def deletion_hold(h):
        return cat == "D" and str(h.get("reason") or "").startswith("pending_delete")
    if cat == "N":
        op = next((o for o in it.get("ops") or [] if _op_name(o) == "CREATE_PAGE"), {})
        par = op.get("parent") or {}
        direct = [_nid(x) for x in (par.get("page_id"), par.get("data_source_id"), it.get("parent_id")) if _nid(x)]
        own, live = None, direct
    else:
        own = _nid(it.get("notion_id"))
        direct = []
        lp = (page or {}).get("parent") or {}
        live = [lp.get(k) for k in ("page_id", "database_id", "data_source_id", "block_id")]
    if own:
        h = holds.get(own)
        if h and not deletion_hold(h):
            raise _PageStop(f"AUTO_SYNC_HOLD：该对象的 Notion → Obsidian 自动同步已暂停（{h['reason']}，{h['source']}）："
                            "不写入；" + _hold_release_hint(h, own))
        if cat != "D" and own in pend:
            raise _PageStop("AUTO_SYNC_HOLD：该对象处于删除保留（pending_deletes=hold，本地笔记已删除）：不写入")
    for d in direct:        # the parent a new page is created under: any hold of it counts (it changes in Notion)
        h = holds.get(d)
        if h:
            raise _PageStop(f"AUTO_SYNC_HOLD：新页面的父级 {d[:8]} 的自动同步已暂停（{h['reason']}）：不新建")
        if d in pend or d in blocked:
            raise _PageStop(f"AUTO_SYNC_HOLD：新页面的父级 {d[:8]} 处于"
                            f"{'删除保留（pending_deletes=hold）' if d in pend else blocked[d]}：不新建")
    for a in _hold_ancestors(ctx, own, live):
        h = holds.get(a)
        if h and h.get("subtree", True) and not deletion_hold(h):
            raise _PageStop(f"AUTO_SYNC_HOLD：上级对象 {a[:8]} 的自动同步已暂停（含子树：{h['reason']}）：不写入")
        if cat != "D" and a in pend:
            raise _PageStop(f"AUTO_SYNC_HOLD：上级对象 {a[:8]} 处于删除保留（pending_deletes=hold，含子树）：不写入")


SCAN_HOLD_SOURCES = ("scan", "export")      # refresh.py: holds it recomputes every round (auto release when fixed)


def _hold_release_hint(h, nid):
    """Next step for an object's own hold. A scan / export hold (e.g. sync_conflict_copy) stays in
    auto_sync_hold.json until the next refresh even when its cause is gone (the copy deleted): the planner may
    still list the note, the executor and the adoption publish do not write / adopt it until then."""
    if str(h.get("reason") or "").startswith("override_rejected"):
        # r1-contract-consistency-06: the exporter rebuilds this hold every round from the rejected rename; waiting
        # or --release-hold does not clear it (same words as the planner's OVERRIDE_REJECTED_HINT)
        return ("本地新文件名未被导出器接受，等待同步不会解除：请把这篇笔记再改一次名（或改回原名、或在 Notion 中改标题），"
                "下一次自动同步后暂停自动解除")
    if h.get("source") in SCAN_HOLD_SOURCES:
        return ("这类暂停由 refresh 每轮按扫描结果重新计算：问题消失后（例如 Sync 冲突副本已删除）下一轮 refresh 自动解除，"
                f"也可人工运行 `{cli_command('refresh', '--release-hold', nid)}`；解除后重新生成计划")
    return "人工处理并解除暂停后重新生成计划"


def _skip_class(why):
    """Result label of a precheck skip: the L-18 classes by name, everything else is a live drift (CONFLICT_BOTH)."""
    w = str(why or "")
    return next((c for c in ("AUTO_SYNC_HOLD", "NOT_LEAF") if w.startswith(c)), "CONFLICT_BOTH")


def _nested_trash_ok(ctx, g, it, tid):
    """A block with children may be trashed only when every child is itself trashed earlier in this same
    confirmed selection (post-order; the planner's op_trash). The parent item then fails with any child item."""
    kids = (g.snap["tree"]["kids"] or {}).get(tid) or []
    targets = {}
    for other in g.items:
        if other.get("item_id") not in ctx.selected:
            continue
        for o in other.get("ops") or []:
            if _op_name(o) == "TRASH_BLOCK":
                targets[_nid(o.get("target_id") or o.get("block_id"))] = other["item_id"]
    missing = [k for k in kids if k not in targets]
    if not kids or missing:
        raise _PageStop("块删除：该块有子块，只有其全部子块也在本次确认的删除中时才删除（请在 Notion 中手工处理）")
    g.trash_deps.setdefault(it["item_id"], set()).update(targets[k] for k in kids)


def _precheck_append(ctx, g, op):
    tree = g.snap["tree"]
    parent = _nid(op.get("parent_id"))
    if parent != g.page_id and parent not in tree["by_id"]:
        raise _PageStop(f"APPEND 的父块 {str(parent)[:8]} 不在该页面中")
    if parent != g.page_id:
        pb = tree["by_id"][parent]
        if _block_type(pb) in NEVER_TRASH_TYPES or _block_type(pb) in ("column_list", "column"):
            raise _PageStop("APPEND 的父块类型不可写")
    # blocks / child pages this run created itself (e.g. an N item's new child page, appended at the end of its
    # parent) are not a remote change of the anchor's neighbourhood
    cur = [c for c in tree["kids"].get(parent, []) if c not in ctx.ours]
    if not op.get("children"):
        raise _PageStop("APPEND 没有子块")
    try:
        for chunk in md2blocks.chunk_children(op["children"]):
            md2blocks.check_children_request(chunk)
    except md2blocks.Refused as exc:
        raise _PageStop(f"APPEND 超出请求限制：{exc.reason}") from None
    pos = op.get("position")
    anchor = None
    if isinstance(pos, dict) and pos.get("type") == "after_block":
        anchor = _nid((pos.get("after_block") or {}).get("id"))
        if anchor not in cur:
            raise _PageStop(f"APPEND 锚点 {str(anchor)[:8]} 已不在父块下")
    elif pos is None:
        # only into a container that had no children at plan time and still has none (the new blocks become
        # its only children); everything else needs after_block / start (WB §2.4)
        if not ("anchor_next_id" in op and op.get("anchor_next_id") is None and not cur):
            raise _PageStop("APPEND 没有 position：只允许追加到空的父块")
    elif not (isinstance(pos, dict) and pos.get("type") == "start"):
        raise _PageStop("APPEND position 无效（只允许在指定块之后 after_block 或列表开头 start 插入）")
    if "parent_child_ids" in op:
        want = [_nid(x) for x in op.get("parent_child_ids") or []]
        if cur != want:
            raise _PageStop("APPEND 父块的子块列表自计划后已改变")
    elif "anchor_next_id" in op:
        nxt = _nid(op.get("anchor_next_id")) if op.get("anchor_next_id") else None
        if anchor is not None:
            i = cur.index(anchor)
            live_next = cur[i + 1] if i + 1 < len(cur) else None
        elif isinstance(pos, dict) and pos.get("type") == "start":
            live_next = cur[0] if cur else None
        else:
            live_next = None if not cur else "__end__"
            nxt = "__end__" if nxt is None and cur else nxt
        if live_next != nxt:
            raise _PageStop("APPEND 锚点之后的同级块自计划后已改变")
    else:
        raise _PageStop("PLAN_INCOMPLETE：计划里的追加操作缺少锚点信息（anchor_next_id / parent_child_ids），"
                        f"无法确认插入位置是否仍正确；请重新运行 {cli_command('writeback', 'plan')}")
    bot = ctx.bot_id()
    since = ctx.plan_created
    for c in cur:
        b = tree["by_id"].get(c) or {}
        if _nid((b.get("created_by") or {}).get("id")) == bot and since is not None:
            ct = _minute(_dt(b.get("created_time")))
            if ct is not None and ct >= since and c not in ctx.ours:
                raise _PageStop("计划后该父块下已有写回集成新建的块（可能是上次执行的结果）")


def _precheck_trash_page(ctx, g, op):
    page = g.snap["page"]
    tree = g.snap["tree"]
    par = page.get("parent") or {}
    if par.get("type") == "workspace" or par.get("workspace"):
        raise _PageStop("DELETE_UNSUPPORTED：根对象不能经写回删除")
    if op.get("last_edited_time") and page.get("last_edited_time") != op["last_edited_time"]:
        raise _PageStop("CONFLICT_DEL_EDIT：页面自计划后被编辑")
    if op.get("page_json_sha"):
        # properties truncated at 25 values (has_more) are completed exactly as the planner / snapshot do (D-10)
        if wb_norm.page_json_sha(_completed_page(ctx, page), tree["blocks"]) != op["page_json_sha"]:
            raise _PageStop("CONFLICT_DEL_EDIT：页面内容自计划后已改变")
    subtree = sorted(_nid(b.get("id")) for b in tree["blocks"] if _block_type(b) in ("child_page", "child_database"))
    if any(_block_type(b) == "child_database" for b in tree["blocks"]):
        raise _PageStop("DELETE_UNSUPPORTED：含子数据库的页面不能删除")
    if "subtree_ids" in op and sorted(_nid(x) for x in op.get("subtree_ids") or []) != subtree:
        raise _PageStop("CONFLICT_DEL_EDIT：子页面清单自计划后已改变")


def _completed_page(ctx, page):
    """Copy of `page` whose has_more properties are completed through the property-item endpoint -- the same
    completion as wb_plan.LiveReader._complete_properties / nmig.rest_inventory (D-10 page_json_sha)."""
    out = copy.deepcopy(page)
    for _name, prop in (out.get("properties") or {}).items():
        if isinstance(prop, dict) and prop.get("has_more") and prop.get("id"):
            full = ctx.prop_items(out["id"], prop["id"])
            items = full.get("results", []) if isinstance(full, dict) else []
            t = prop.get("type")
            if t == "relation":
                prop["relation"] = [{"id": i["relation"]["id"]} for i in items if i.get("relation")]
            elif t == "people":
                prop["people"] = [i["people"] for i in items if i.get("people")]
            elif t in ("rich_text", "title"):
                prop[t] = [i[t] for i in items if i.get(t)]
            prop["has_more"] = False
            prop["_completed_via_property_item"] = len(items)
    return out


def _precheck_delete_vault(ctx, it):
    nid = _nid(it.get("notion_id"))
    for rel in [it.get("rel_path")] + list(it.get("subtree_rels") or []):
        if rel and _vault_file(ctx, rel) and os.path.lexists(_vault_file(ctx, rel)):
            raise _PageStop(f"库内文件 {rel} 又出现了：不删除")
    other = _vault_has_id(ctx, nid)
    if other:
        raise _PageStop(f"库内存在同一 notion_id 的文件 {other}：不删除")
    r = ctx.db.execute("SELECT status FROM pending_deletes WHERE notion_id=? ORDER BY rowid DESC LIMIT 1",
                       (nid,)).fetchone()
    if r is None or r["status"] != "hold":
        raise _PageStop("pending_deletes 状态不是 hold：不删除")


def _precheck_new(ctx, g, it, ops):
    if len(ops) != 1 or ops[0]["op"] != "CREATE_PAGE":
        raise _PageStop("N 项必须恰好有一个 CREATE_PAGE 操作")
    if ctx.creation_state != "unlimited" or ctx.block_limit_hit:
        raise _PageStop("块额度受限：不新建页面")
    op = ops[0]
    parent = op.get("parent") or {}
    if not (parent.get("page_id") or parent.get("data_source_id")):
        raise _PageStop("新页面的父级无效")
    if not _parent_in_scope(ctx, parent):
        raise _PageStop("OUT_OF_WHITELIST：新页面的父级不在白名单")
    rel = it.get("rel_path")
    if _mapping(ctx.db, rel):
        raise _PageStop("映射中已有该本地路径（之前已新建过）")
    prior = _prior_create(ctx.db, rel)
    if prior is not None:
        # an earlier create of this note may have reached Notion (sent / unknown / committed): never POST it again,
        # even when the live listing does not show the page yet (search / listings can lag behind)
        raise _PageStop(f"该本地路径之前已发出过新建请求（journal {prior['seq']}，状态 {prior['state']}）："
                        f"{wb_common.reconcile_hint(prior['seq'])}")
    if _published_has_rel(ctx, rel):
        raise _PageStop("published_files 已登记该路径")
    title = op.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _PageStop("新页面没有标题")
    try:
        for chunk in md2blocks.chunk_children(op.get("children") or []):
            md2blocks.check_children_request(chunk)
    except md2blocks.Refused as exc:
        raise _PageStop(f"新页面内容超出请求限制：{exc.reason}") from None
    cands = _bot_pages_since(ctx, parent, ctx.plan_created, title)
    if cands:
        raise _PageStop("父级下已有写回集成在计划后新建的同名页面")
    if parent.get("page_id"):
        try:
            pp = ctx.get_page(_nid(parent["page_id"]))
        except NotionAPIError as exc:
            raise _PageStop(f"父页面不可读（{exc.status}）") from None
        if pp.get("in_trash") or pp.get("is_locked"):
            raise _PageStop("父页面在回收站或已锁定")
        g.snap["parent_kids"] = [_nid(b.get("id")) for b in ctx.children(_nid(parent["page_id"]))]
    if parent.get("page_id") and _stage2(ctx):
        # L-18 (a): in stage 2 a new page goes only under a leaf parent page (new rows have no such rule)
        try:
            kids = _subpages(_page_tree(ctx, _nid(parent["page_id"])), ctx.ours)
        except NotionAPIError as exc:
            raise _PageStop(f"父页面内容不可读（{exc.status}）：无法确认它是叶子页面") from None
        if kids:
            raise _not_leaf("新页面的父页面", kids)


def _prior_create(db, rel):
    """The latest CREATE_PAGE journal row (any plan) for the note `rel` that may have created a page."""
    if not rel:
        return None
    rows = db.execute("SELECT j.seq, j.plan_id, j.state, j.committed_ids FROM journal j JOIN plan_items p "
                      "ON p.plan_id=j.plan_id AND p.item_id=j.item_id WHERE j.op='CREATE_PAGE' AND p.rel_path=? "
                      "AND j.state IN ('sent','unknown','done','reconciled') ORDER BY j.seq DESC", (rel,)).fetchall()
    for r in rows:
        info = _jload(r["committed_ids"], {}) or {}
        if r["state"] == "reconciled" and str(info.get("reconciled", "")) in ("manual:not_committed",
                                                                            "not_applied_by_get"):
            continue
        return dict(r)
    return None


def _published_has_rel(ctx, rel):
    p = ctx.paths.export_db
    if not rel or not os.path.isfile(p):
        return False
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=ro", uri=True, timeout=30)
        try:
            r = con.execute("SELECT 1 FROM published_files WHERE rel_path=? AND status='active'", (rel,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return r is not None


def _bot_pages_since(ctx, parent, since, title):
    """Pages under `parent` created by the write-back bot at/after `since` (minute precision) with `title`."""
    bot = ctx.bot_id()
    out = []
    if parent.get("page_id"):
        for b in ctx.children(_nid(parent["page_id"])):
            if _block_type(b) != "child_page":
                continue
            if _nid((b.get("created_by") or {}).get("id")) != bot:
                continue
            ct = _minute(_dt(b.get("created_time")))
            if since is not None and ct is not None and ct < since:
                continue
            if title is None or (b.get("child_page") or {}).get("title") == title:
                out.append(_nid(b.get("id")))
    elif parent.get("data_source_id"):
        body = {}
        if since is not None:
            body = {"filter": {"timestamp": "created_time",
                               "created_time": {"on_or_after": since.strftime("%Y-%m-%dT%H:%M:00.000Z")}}}
        for r in ctx.query(_nid(parent["data_source_id"]), body):
            if _nid((r.get("created_by") or {}).get("id")) != bot:
                continue
            if title is None or _page_title(r) == title:
                out.append(_nid(r.get("id")))
    return out


# =============================================================================================== intents
def _build_steps(ctx, g):
    """Journal intent rows (state=intent) for every op of the page's prechecked items. Returns step dicts."""
    steps = []
    tree = (g.snap or {}).get("tree")
    entries = []
    depth = _tree_depths(tree) if tree else {}
    for ii, it in enumerate(g.ok_items):
        for oi, op in enumerate(it.get("ops") or []):
            name = _op_name(op)
            # block deletes deepest first (children before their parent: nested deletes are post-order)
            d = -depth.get(_nid(op.get("target_id") or op.get("block_id")), 0) if name == "TRASH_BLOCK" else 0
            entries.append((PHASE.get(name, 9), d, ii, oi, it, dict(op, op=name)))
    entries.sort(key=lambda e: e[:4])
    for _ph, _d, _ii, oi, it, op in entries:
        name = op["op"]
        iid = it["item_id"]
        if name == "UPDATE":
            tid = _nid(op.get("target_id") or op.get("block_id"))
            before = tree["by_id"][tid]
            req = op.get("request")
            seq = _j_insert(ctx, iid, "UPDATE", tid, before, {"body": req, "op_index": oi},
                            _expected_update(before, req))
            steps.append({"seq": seq})
        elif name == "APPEND":
            parent = _nid(op.get("parent_id"))
            chunks = md2blocks.chunk_children(op["children"])
            prev = None
            for k, chunk in enumerate(chunks):
                pos = op.get("position") if k == 0 else {"after_seq": prev}
                req = {"parent_id": parent, "children": chunk, "position": pos, "op_index": oi, "chunk": k}
                before = {"child_ids": list(tree["kids"].get(parent, [])) if tree else []}
                seq = _j_insert(ctx, iid, "APPEND", parent, before, req, _canon_tree(chunk))
                steps.append({"seq": seq})
                prev = seq
        elif name == "TRASH_BLOCK":
            tid = _nid(op.get("target_id") or op.get("block_id"))
            seq = _j_insert(ctx, iid, "TRASH_BLOCK", tid, tree["by_id"][tid], {"op_index": oi}, {"in_trash": True})
            steps.append({"seq": seq})
        elif name == "UPDATE_PROPERTIES":
            pid = g.page_id            # bound to the prechecked page (op page_id / target_id must equal it)
            page = g.snap["page"]
            before = {n: _full_prop(ctx, page, n) for n in (op.get("properties") or {})}
            exp = {n: _prop_canon(v) for n, v in (op.get("properties") or {}).items()}
            seq = _j_insert(ctx, iid, "UPDATE_PROPERTIES", pid, before,
                            {"body": {"properties": op.get("properties")}, "op_index": oi}, exp)
            steps.append({"seq": seq})
        elif name == "RENAME":
            pid = g.page_id
            page = g.snap["page"]
            tp = _title_prop(page, op.get("title_property"))
            new = op["new_title"]
            body = {"properties": {tp: {"title": md2blocks.request_rich(md2blocks.split_for_api(
                [{"type": "text", "text": {"content": new}}], render_safe=False))}}}
            seq = _j_insert(ctx, iid, "RENAME", pid, {"title_property": tp, "title": (_full_prop(ctx, page, tp) or {}).get("title")},
                            {"body": body, "op_index": oi, "new_title": new},
                            {"title": md2blocks.canon_rich_text([{"type": "text", "text": {"content": new}}])})
            steps.append({"seq": seq})
        elif name == "TRASH_PAGE":
            pid = g.page_id
            seq = _j_insert(ctx, iid, "TRASH_PAGE", pid, {"page": g.snap["page"], "parent": g.snap["page"].get("parent")},
                            {"body": {"in_trash": True}, "op_index": oi}, {"in_trash": True})
            steps.append({"seq": seq})
        elif name == "CREATE_PAGE":
            parent = op.get("parent") or {}
            ptype = "page_id" if parent.get("page_id") else "data_source_id"
            pval = _nid(parent.get(ptype))
            chunks = md2blocks.chunk_children(op.get("children") or []) if op.get("children") else []
            tp = op.get("title_property") or "title"
            props = copy.deepcopy(op.get("properties") or {})
            props[tp] = {"title": md2blocks.request_rich(md2blocks.split_for_api(
                [{"type": "text", "text": {"content": op["title"]}}], render_safe=False))}
            body = {"parent": {ptype: pval}, "properties": props}
            if chunks:
                body["children"] = chunks[0]
            before = {"parent": {ptype: pval}, "since": ctx.now().isoformat(timespec="seconds"),
                      "known": g.snap.get("parent_kids")}
            seq = _j_insert(ctx, iid, "CREATE_PAGE", pval, before, {"body": body, "op_index": oi,
                                                                    "title": op["title"], "title_property": tp},
                            {"title": op["title"], "children": _canon_tree(chunks[0]) if chunks else []})
            steps.append({"seq": seq})
            prev = seq
            for k, chunk in enumerate(chunks[1:], start=1):
                req = {"parent_id": {"new_page_of_seq": seq}, "children": chunk, "position": {"after_seq": prev},
                       "op_index": oi, "chunk": k}
                s2 = _j_insert(ctx, iid, "APPEND", None, {"child_ids": []}, req, _canon_tree(chunk))
                steps.append({"seq": s2})
                prev = s2
    g.steps = steps
    return steps


def _tree_depths(tree):
    """block id -> depth (1 = direct child of the page) from a _page_tree result."""
    parent_of = {}
    for par, kids in (tree.get("kids") or {}).items():
        for k in kids:
            parent_of[k] = par
    out = {}
    for bid in tree.get("by_id") or {}:
        d, cur, seen = 0, bid, set()
        while cur in parent_of and cur not in seen:
            seen.add(cur)
            cur = parent_of[cur]
            d += 1
        out[bid] = d
    return out


def _parent_in_tree(tree, bid):
    for par, kids in ((tree or {}).get("kids") or {}).items():
        if bid in kids:
            return par
    return None


# =============================================================================================== execution
def _resolve_parent(ctx, ref):
    if isinstance(ref, dict) and "new_page_of_seq" in ref:
        row = _j_row(ctx.db, ref["new_page_of_seq"])
        ids = (_jload(row.get("committed_ids"), {}) or {}).get("ids") if row else None
        if not row or row["state"] not in ("done", "reconciled") or not ids:
            raise _PageStop("新页面尚未成功创建，不能填充内容", blocked=True, partial_create=True)
        return _nid(ids[0])
    return _nid(ref)


def _resolve_position(ctx, pos):
    if isinstance(pos, dict) and "after_seq" in pos:
        row = _j_row(ctx.db, pos["after_seq"])
        info = _jload(row.get("committed_ids"), {}) if row else {}
        ids = (info or {}).get("ids") or []
        if not row or row["state"] not in ("done", "reconciled") or not ids:
            raise _PageStop("前一个分块未成功，停止本页", blocked=True)
        if row["op"] == "CREATE_PAGE":
            req = _jload(row["request_json"], {})
            first = (req.get("body") or {}).get("children") or []
            # the first chunk went into the POST: its last top-level block is the anchor
            tree = ctx.children(_nid(ids[0]))
            if len(tree) < len(first) or not first:
                return None
            return {"type": "after_block", "after_block": {"id": _nid(tree[len(first) - 1].get("id"))}}
        return {"type": "after_block", "after_block": {"id": _nid(ids[-1])}}
    return pos


def _exec_step(ctx, g, seq):
    row = _j_row(ctx.db, seq)
    if row is None or row["state"] != "intent":
        return
    op = row["op"]
    req = _jload(row["request_json"], {}) or {}
    if op in ("UPDATE", "TRASH_BLOCK"):
        before = _jload(row["before_json"], {})
        # single-block re-GET right before the write (WB §11.3.3)
        try:
            live = ctx.get_block(row["target_id"])
        except NotionAPIError as exc:
            raise _PageStop(f"写前复核读取失败（{exc.status}）", blocked=g.written) from None
        cmp_live = live
        if op == "TRASH_BLOCK" and (before or {}).get("has_children") and not live.get("has_children"):
            kids = ((g.snap or {}).get("tree") or {}).get("kids", {}).get(row["target_id"]) or []
            if kids and all(k in ctx.trashed_here for k in kids):
                cmp_live = dict(live, has_children=True)      # its children were trashed by this run just now
        if wb_norm.block_json_sha(cmp_live) != wb_norm.block_json_sha(before) or live.get("in_trash"):
            _j_update(ctx, seq, state="cancelled")
            raise _PageStop(f"写前复核：块 {row['target_id'][:8]} 自预检后已改变", blocked=g.written)
        if op == "TRASH_BLOCK" and (_block_type(live) in NEVER_TRASH_TYPES or live.get("has_children")):
            _j_update(ctx, seq, state="cancelled")
            raise _PageStop("写前复核：块类型或子块不允许删除", blocked=g.written)
    if op == "UPDATE":
        res = _send(ctx, seq, ctx.client.update_block, row["target_id"], req["body"])
        return _after_simple(ctx, g, seq, row, res)
    if op == "TRASH_BLOCK":
        res = _send(ctx, seq, ctx.client.trash_block, row["target_id"])
        return _after_simple(ctx, g, seq, row, res)
    if op in ("UPDATE_PROPERTIES", "RENAME", "TRASH_PAGE"):
        if op == "TRASH_PAGE":
            _archive_before_trash(ctx, g, row)
        res = _send(ctx, seq, ctx.client.update_page, row["target_id"], req["body"])
        return _after_simple(ctx, g, seq, row, res)
    if op == "APPEND":
        parent = _resolve_parent(ctx, req.get("parent_id"))
        pos = _resolve_position(ctx, req.get("position"))
        if row["target_id"] != parent:
            _j_update(ctx, seq, target_id=parent)
        # the parent's child set right before the send (not the precheck tree): an earlier APPEND of this run to
        # the same parent must not count as "new" when this request's outcome has to be reconciled
        before_ids = _child_ids(ctx, parent)
        _j_update(ctx, seq, before_json={"child_ids": before_ids, "at": wb_common.now_iso()})
        res = _send(ctx, seq, ctx.client.append_children, parent, req["children"], pos)
        return _after_append(ctx, g, seq, parent, before_ids, req, res)
    if op == "CREATE_PAGE":
        return _exec_create(ctx, g, seq, row, req)
    raise _StopRun(f"未知的日志操作 {op}")


def _after_simple(ctx, g, seq, row, res):
    g.written = g.written or res.outcome in ("ok", "unknown")
    if res.outcome == "ok":
        if res.verify_required:
            _verify_simple_committed(ctx, g, seq, row)
        if row["op"] == "TRASH_BLOCK":
            ctx.trashed_here.add(row["target_id"])
        return
    if res.outcome == "unknown":
        # idempotent op: GET-verify; never resent automatically (WB §11.3.5 allows a resend after checking;
        # this tool prefers to stop the page and let the user re-plan)
        try:
            ok = _simple_applied(ctx, row)
        except NotionAPIError:
            ok = None
        if ok:
            _j_update(ctx, seq, state="done",
                      committed_ids=dict(_jload(_j_row(ctx.db, seq)["committed_ids"], {}), reconciled="applied_by_get"))
            if row["op"] == "TRASH_BLOCK":
                ctx.trashed_here.add(row["target_id"])
            return
        if ok is False:
            _j_update(ctx, seq, state="reconciled",
                      committed_ids=dict(_jload(_j_row(ctx.db, seq)["committed_ids"], {}), reconciled="not_applied_by_get"))
            raise _PageStop("写入结果未知，GET 核对为未生效（不自动重发）", blocked=_page_has_writes(ctx, g))
        raise _PageStop("写入结果未知，无法核对（需 reconcile）", blocked=True)
    # rejected / not_sent
    code = res.code or res.outcome
    if code == "block_limit":
        _record_block_limit(ctx)
        raise _PageStop("Notion 返回块额度 403（block_limit）：creation_state=blocked_confirmed", blocked=True)
    g.written = _page_has_writes(ctx, g)
    raise _PageStop(f"写入被拒绝（{res.status} {code}）：{(res.message or '')[:120]}", blocked=g.written)


def _page_has_writes(ctx, g):
    for s in g.steps:
        r = _j_row(ctx.db, s["seq"])
        if r and r["state"] in ("done", "unknown", "reconciled", "sent"):
            info = _jload(r["committed_ids"], {}) or {}
            if r["state"] == "reconciled" and info.get("reconciled") == "not_applied_by_get":
                continue
            return True
    return False


def _simple_applied(ctx, row):
    op = row["op"]
    exp = _jload(row["expected_after_json"])
    tid = row["target_id"]
    if op == "UPDATE":
        live = ctx.get_block(tid)
        if md2blocks.canon_writable(live) == exp:
            return True
        before = _jload(row["before_json"], {})
        return False if wb_norm.block_json_sha(live) == wb_norm.block_json_sha(before) else None
    if op == "TRASH_BLOCK":
        return bool(ctx.get_block(tid).get("in_trash"))
    if op == "TRASH_PAGE":
        return bool(ctx.get_page(tid).get("in_trash"))
    if op in ("UPDATE_PROPERTIES", "RENAME"):
        page = ctx.get_page(tid)
        if op == "RENAME":
            tp = (_jload(row["before_json"], {}) or {}).get("title_property") or _title_prop(page)
            cur = md2blocks.canon_rich_text((_full_prop(ctx, page, tp) or {}).get("title") or [])
            if cur == exp.get("title"):
                return True
            old = md2blocks.canon_rich_text((_jload(row["before_json"], {}) or {}).get("title") or [])
            return False if cur == old else None
        cur = {n: _prop_canon(_full_prop(ctx, page, n)) for n in exp}
        if cur == exp:
            return True
        before = _jload(row["before_json"], {}) or {}
        return False if cur == {n: _prop_canon(before.get(n)) for n in exp} else None
    return None


def _verify_simple_committed(ctx, g, seq, row):
    ok = _simple_applied(ctx, row)
    if not ok:
        _j_update(ctx, seq, state="unknown")
        raise _PageStop("503 committed：GET 核对未通过", blocked=True)


def _after_append(ctx, g, seq, parent, before_ids, req, res):
    n = len(req["children"])
    if res.outcome == "ok":
        ids = [_nid(i) for i in res.committed_ids]
        if res.verify_required or len(ids) != n:
            if len(ids) != n:
                _j_update(ctx, seq, state="unknown")
                g.written = True
                raise _PageStop("APPEND 返回的新块数量不符", blocked=True)
            for i in ids:
                b = ctx.get_block(i)
                bp = _nid((b.get("parent") or {}).get("block_id") or (b.get("parent") or {}).get("page_id"))
                if bp != parent or i in before_ids:
                    _j_update(ctx, seq, state="unknown")
                    g.written = True
                    raise _PageStop("APPEND 503 committed：GET 核对未通过（不重发）", blocked=True)
        g.written = True
        ctx.ours.update(ids)
        _ledger(ctx, seq, req["children"])
        return
    if res.outcome == "unknown":
        g.written = True
        cand = _append_set_diff(ctx, parent, before_ids, req)
        info = dict(_jload(_j_row(ctx.db, seq)["committed_ids"], {}) or {})
        if cand is not None:
            info.update(ids=cand, reconciled="set_difference_unique")
            _j_update(ctx, seq, state="reconciled", committed_ids=info)
            ctx.ours.update(cand)
            _ledger(ctx, seq, req["children"])
            raise _PageStop("APPEND 结果未知：按子块集合求差唯一确定为已生效，但本页停止（BLOCKED）", blocked=True)
        raise _PageStop("APPEND 结果未知且无法唯一核对：页面 BLOCKED，需 reconcile", blocked=True)
    code = res.code or res.outcome
    if code == "block_limit":
        _record_block_limit(ctx)
        raise _PageStop("Notion 返回块额度 403（block_limit）：creation_state=blocked_confirmed", blocked=True)
    raise _PageStop(f"APPEND 失败（{res.status} {code}）：不再发送本页其余请求", blocked=True)


def _append_set_diff(ctx, parent, before_ids, req):
    """Unique set difference (WB §11.3.5): the new children are exactly n bot-created blocks of the requested
    types that were not there before (and are not ours from earlier requests)."""
    try:
        cur = ctx.children(parent)
    except NotionAPIError:
        return None
    bot = ctx.bot_id()
    new = [b for b in cur if _nid(b.get("id")) not in set(before_ids) and _nid(b.get("id")) not in ctx.ours]
    want = [(_block_type(c) or next(k for k in c if k not in ("object", "children"))) for c in req["children"]]
    if len(new) != len(want):
        return None
    if any(_nid((b.get("created_by") or {}).get("id")) != bot for b in new):
        return None
    if [_block_type(b) for b in new] != want:
        return None
    return [_nid(b.get("id")) for b in new]


def _ledger(ctx, seq, children, extra=0):
    row = _j_row(ctx.db, seq)
    with wb_state.transaction(ctx.db):
        ctx.db.execute("INSERT INTO block_budget(ts, plan_id, item_id, created_blocks) VALUES(?,?,?,?)",
                       (wb_common.now_iso(), ctx.plan_id, row["item_id"] if row else None,
                        md2blocks.block_count(children) + extra))


def _exec_create(ctx, g, seq, row, req):
    body = req["body"]
    before = _jload(row["before_json"], {}) or {}
    parent = before.get("parent") or body["parent"]
    title = req.get("title")
    known = set(before.get("known") or [])
    since = _minute(_dt(before.get("since")))
    res = _send(ctx, seq, ctx.client.create_page, body)
    iid = row["item_id"]
    it = ctx.items[iid]
    pid = None
    if res.outcome == "ok":
        pid = _nid(res.committed_ids[0]) if res.committed_ids else None
        if res.verify_required:
            try:
                pg = ctx.get_page(pid)
            except NotionAPIError:
                pg = None
            pp = (pg or {}).get("parent") or {}
            want = _nid(parent.get("page_id") or parent.get("data_source_id"))
            if not pg or _nid(pp.get("page_id") or pp.get("data_source_id")) != want:
                _j_update(ctx, seq, state="unknown")
                raise _PageStop("新建页面 503 committed：GET 核对未通过", blocked=True, partial_create=True)
    elif res.outcome == "unknown":
        try:
            cands = [c for c in _bot_pages_since(ctx, parent, since, title) if c not in known and c not in ctx.ours]
        except NotionAPIError:
            cands = None                      # cannot read the parent now: not decidable, never resent
        info = dict(_jload(_j_row(ctx.db, seq)["committed_ids"], {}) or {})
        if cands is not None and len(cands) == 1:
            pid = cands[0]
            info.update(ids=[pid], reconciled="set_difference_unique")
            _j_update(ctx, seq, state="reconciled", committed_ids=info)
            g.written = True
            g.new_page = pid
            ctx.ours.add(pid)
            ctx.created[iid] = pid
            _ledger(ctx, seq, body.get("children") or [], extra=1)
            _write_mapping(ctx, it, pid, "partial")
            _set_page_state(ctx, pid, "PARTIAL_CREATE", "新建页面请求结果未知（已按集合求差确定页面，未继续填充）")
            raise _PageStop("新建页面结果未知：已唯一确定新页面，但不继续填充（PARTIAL_CREATE）", blocked=True,
                            partial_create=True)
        g.written = True
        _write_mapping(ctx, it, None, "unknown", journal_seq=seq)
        raise _PageStop("新建页面结果未知且无法唯一核对：需 reconcile（不重发）", blocked=True)
    else:
        code = res.code or res.outcome
        if code == "block_limit":
            _record_block_limit(ctx)
            raise _PageStop("Notion 返回块额度 403（block_limit）：creation_state=blocked_confirmed", blocked=False)
        raise _PageStop(f"新建页面被拒绝（{res.status} {code}）", blocked=False)
    # created: mapping first, then (sha-guarded) frontmatter
    g.written = True
    g.new_page = pid
    ctx.ours.add(pid)
    ctx.created[iid] = pid
    _ledger(ctx, seq, body.get("children") or [], extra=1)
    rest = [s for s in g.steps if s["seq"] > seq]
    _write_mapping(ctx, it, pid, "filling" if rest else "complete")
    _frontmatter_writeback(ctx, it, pid)


def _write_mapping(ctx, it, pid, state, journal_seq=None):
    """wb_meta["created_page:<rel>"] (WB §5.3.4: written right after the create). state unknown = the POST may
    have created a page that could not be identified (page_id None, journal_seq for reconcile): the planner and
    _precheck_new treat ANY mapping as "already created" so the note is never POSTed again."""
    rel = it.get("rel_path")
    cur = _mapping(ctx.db, rel) or {}
    cur.update({"page_id": pid, "plan_id": ctx.plan_id, "item_id": it["item_id"], "l_sha": it.get("l_sha"),
                "rel_path": rel, "state": state, "at": wb_common.now_iso()})
    if journal_seq is not None:
        cur["journal_seq"] = journal_seq
    _set_mapping(ctx, rel, cur)
    if pid:
        with wb_state.transaction(ctx.db):
            ctx.db.execute("UPDATE plan_items SET notion_id=? WHERE plan_id=? AND item_id=?",
                           (pid, ctx.plan_id, it["item_id"]))


def _frontmatter_writeback(ctx, it, pid):
    """WB §5.3.4: only while the vault file still has the plan's L sha; backup first; atomic; journaled."""
    rel = it.get("rel_path")
    p = _vault_file(ctx, rel)
    seq = _j_insert(ctx, it["item_id"], "VAULT_FRONTMATTER", pid, {"rel": rel}, {"notion_id": pid}, None)
    if not p or not os.path.isfile(p):
        _j_update(ctx, seq, state="failed", committed_ids={"outcome": "file_missing"})
        ctx.fm_skipped.add(it["item_id"])
        ctx.notes.append(f"{it['item_id']}：库内文件不存在，未回写 frontmatter（待人工处理）")
        return None
    with open(p, "rb") as fh:
        data = fh.read()
    if wb_common.sha256_bytes(data) != it.get("l_sha"):
        _j_update(ctx, seq, state="failed", committed_ids={"outcome": "sha_changed"})
        ctx.fm_skipped.add(it["item_id"])
        ctx.notes.append(f"{it['item_id']}：库内文件在执行期间被修改，未回写 frontmatter（待人工处理：Notion 版本会写入 "
                         "conflicts/，你的本地修改保持不动）")
        return None
    new = _with_notion_id(data, pid)
    if new is None:
        _j_update(ctx, seq, state="failed", committed_ids={"outcome": "frontmatter_unparseable"})
        ctx.fm_skipped.add(it["item_id"])
        ctx.notes.append(f"{it['item_id']}：frontmatter 无法解析，未回写（待人工处理）")
        return None
    bk = wb_common.backup_copy(p, os.path.join(ctx.paths.backup, "writeback", ctx.plan_id), rel)
    # r1-data-safety-07: the intent row names the exact content about to be written BEFORE the replace, so a crash
    # between the replace and the `done` update is settled from the live file (_settle_frontmatter_intents)
    _j_update(ctx, seq, before_json={"rel": rel, "sha": it.get("l_sha"), "backup": bk},
              expected_after_json={"sha": wb_common.sha256_bytes(new)})
    with open(p, "rb") as fh:          # re-check right before the replace
        if wb_common.sha256_bytes(fh.read()) != it.get("l_sha"):
            _j_update(ctx, seq, state="failed", committed_ids={"outcome": "sha_changed"})
            ctx.fm_skipped.add(it["item_id"])
            ctx.notes.append(f"{it['item_id']}：库内文件在执行期间被修改，未回写 frontmatter（待人工处理）")
            return None
    race_dir = os.path.join(ctx.paths.backup, "writeback", ctx.plan_id, "race")
    race = publish_vault.guarded_replace(p, new, it.get("l_sha"), race_dir)
    if race is not None:
        _j_update(ctx, seq, state="failed", committed_ids={"outcome": "sha_changed", "race": race})
        ctx.fm_skipped.add(it["item_id"])
        note = f"{it['item_id']}：库内文件在执行期间被修改，未回写 frontmatter（待人工处理）"
        if race.get("kept"):
            note += (f"。两次保存的内容都已保留。请先将库内的 {_code(rel)} 另存到库外，再打开保留的更新版本 "
                     f"{_code(race['kept'])}，对照两份内容，把需要的文字合并回原笔记。")
        ctx.notes.append(note)
        return None
    _j_update(ctx, seq, state="done", before_json={"rel": rel, "sha": it.get("l_sha"), "backup": bk},
              after_json={"sha": wb_common.sha256_bytes(new)}, committed_ids={"outcome": "ok", "ids": [pid]})
    cur = _mapping(ctx.db, rel) or {}
    cur["vault_sha"] = wb_common.sha256_bytes(new)
    _set_mapping(ctx, rel, cur)
    return wb_common.sha256_bytes(new)


def _settle_frontmatter_intents(ctx):
    """r1-data-safety-07: a VAULT_FRONTMATTER row still `intent` (a crash after the sha-guarded replace, before its
    `done` update). The live file decides: it hashes to the content the row announced -> the write happened (row
    done, mapping vault_sha, so the adoption passes the real current sha as l_sha); anything else -> not written
    (the row is cancelled with the other intent rows, as before)."""
    for r in _j_rows(ctx.db, ctx.plan_id, ("intent",)):
        if r["op"] != "VAULT_FRONTMATTER":
            continue
        exp = (_jload(r["expected_after_json"], {}) or {}).get("sha")
        rel = (_jload(r["before_json"], {}) or {}).get("rel")
        if not exp or not rel:
            continue
        try:
            with open(_vault_file(ctx, rel), "rb") as fh:
                cur = wb_common.sha256_bytes(fh.read())
        except (OSError, ValueError, TypeError):
            continue
        if cur != exp:
            continue
        pid = _nid(r["target_id"])
        _j_update(ctx, r["seq"], state="done", after_json={"sha": cur},
                  committed_ids={"outcome": "ok", "ids": [pid], "settled": "live file equals the announced content"})
        m = _mapping(ctx.db, rel) or {}
        if _nid(m.get("page_id")) in (None, pid):
            m["vault_sha"] = cur
            _set_mapping(ctx, rel, m)


def _with_notion_id(data, pid):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # r1-data-safety-02: the scan and the planner read a note with a leading UTF-8 BOM without it; so does this
    # rewrite: notion_id goes into the REAL frontmatter and the BOM is dropped (the publisher's adoption and the
    # verifier then parse the note's frontmatter, so local keys such as tags / aliases are kept)
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        fm, body, _ = wb_common.split_note(text)
        meta = wb_common.parse_frontmatter(fm)
    except wb_common.FrontmatterError:
        return None
    if meta.get("notion_id"):
        return None
    line = f"notion_id: {pid}"
    if fm is None:
        return (f"---\n{line}\n---\n" + text).encode("utf-8")
    first_nl = text.index("\n")
    return (text[:first_nl + 1] + line + "\n" + text[first_nl + 1:]).encode("utf-8")


def _archive_before_trash(ctx, g, row):
    """WB §6.5: page object, all block JSON, subtree list and inbound refs (from the plan) -> trash_archive."""
    nid = row["target_id"]
    page = (g.snap or {}).get("page") or {}
    blocks = ((g.snap or {}).get("tree") or {}).get("blocks") or []
    it = ctx.items.get(row["item_id"]) or {}
    with wb_state.transaction(ctx.db):
        ctx.db.execute("INSERT INTO trash_archive(notion_id, archived_at, reason, last_snapshot, object_json, "
                       "blocks_json, inbound_refs_json) VALUES(?,?,?,?,?,?,?)",
                       (nid, wb_common.now_iso(), f"writeback_trash plan={ctx.plan_id} item={row['item_id']}",
                        "live:" + ctx.plan_id, json.dumps(page, ensure_ascii=False),
                        json.dumps(blocks, ensure_ascii=False),
                        json.dumps({"mode": "plan", "refs": it.get("inbound_refs") or [],
                                    "subtree": [b.get("id") for b in blocks if _block_type(b) == "child_page"]},
                                   ensure_ascii=False)))


# =============================================================================================== read-back
def _readback_group(ctx, g):
    """WB §11.3.7: read-only client; returns (w1_ok, w2_ok, problems). Stores after_json in the journal."""
    problems, w1, w2 = [], True, True
    bot = ctx.bot_id()
    rows = [_j_row(ctx.db, s["seq"]) for s in g.steps]
    rows = [r for r in rows if r and r["state"] in ("done", "reconciled")]
    rows = [r for r in rows if not ((_jload(r["committed_ids"], {}) or {}).get("reconciled") == "not_applied_by_get")]
    touched, trashed, new_ids, parents = set(), set(), set(), set()
    for r in rows:
        op = r["op"]
        try:
            if op == "UPDATE":
                live = ctx.get_block(r["target_id"])
                before = _jload(r["before_json"], {})
                exp = _jload(r["expected_after_json"])
                _j_update(ctx, r["seq"], after_json=live)
                touched.add(r["target_id"])
                if md2blocks.canon_writable(live) != exp:
                    w1 = False
                    problems.append(f"UPDATE {r['target_id'][:8]}：回读与预期不一致")
                elif _strip_writable(live) != _strip_writable(before):
                    w1 = False
                    problems.append(f"UPDATE {r['target_id'][:8]}：不可写字段（类型/颜色等）发生变化")
                elif _nid((live.get("last_edited_by") or {}).get("id")) != bot:
                    w1 = False
                    problems.append(f"UPDATE {r['target_id'][:8]}：最后编辑者不是写回集成")
            elif op == "APPEND":
                ids = [_nid(i) for i in (_jload(r["committed_ids"], {}) or {}).get("ids") or []]
                req = _jload(r["request_json"], {})
                got, raw = _live_tree(ctx, ids)
                _j_update(ctx, r["seq"], after_json=raw)
                new_ids.update(ids)
                for x in raw:
                    _collect_ids(x, new_ids)
                parents.add(r["target_id"])
                if got != _jload(r["expected_after_json"]):
                    w1 = False
                    problems.append(f"APPEND 到 {str(r['target_id'])[:8]}：新块回读与预期不一致")
                elif any(_nid((b.get("created_by") or {}).get("id")) != bot for b in raw):
                    w1 = False
                    problems.append("APPEND：新块的创建者不是写回集成")
                elif not _position_ok(ctx, r, ids, req):
                    w1 = False
                    problems.append(f"APPEND 到 {str(r['target_id'])[:8]}：新块位置不符")
            elif op == "TRASH_BLOCK":
                live = ctx.get_block(r["target_id"])
                _j_update(ctx, r["seq"], after_json=live)
                trashed.add(r["target_id"])
                if not live.get("in_trash"):
                    w1 = False
                    problems.append(f"TRASH_BLOCK {r['target_id'][:8]}：块未在回收站")
            elif op in ("UPDATE_PROPERTIES", "RENAME", "TRASH_PAGE"):
                page = ctx.get_page(r["target_id"])
                _j_update(ctx, r["seq"], after_json=page)
                exp = _jload(r["expected_after_json"], {})
                if op == "TRASH_PAGE":
                    if not page.get("in_trash"):
                        w1 = False
                        problems.append("TRASH_PAGE：页面未在回收站")
                elif op == "RENAME":
                    tp = (_jload(r["before_json"], {}) or {}).get("title_property") or _title_prop(page)
                    if md2blocks.canon_rich_text((_full_prop(ctx, page, tp) or {}).get("title") or []) != exp.get("title"):
                        w1 = False
                        problems.append("RENAME：标题回读与预期不一致")
                else:
                    cur = {n: _prop_canon(_full_prop(ctx, page, n)) for n in exp}
                    if cur != exp:
                        w1 = False
                        problems.append("UPDATE_PROPERTIES：属性回读与预期不一致")
                if op != "TRASH_PAGE" and _nid((page.get("last_edited_by") or {}).get("id")) != bot:
                    w1 = False
                    problems.append(f"{op}：最后编辑者不是写回集成")
            elif op == "CREATE_PAGE":
                ids = (_jload(r["committed_ids"], {}) or {}).get("ids") or []
                pid = _nid(ids[0]) if ids else None
                page = ctx.get_page(pid)
                exp = _jload(r["expected_after_json"], {})
                req = _jload(r["request_json"], {})
                tp = req.get("title_property") or "title"
                kids = [_nid(b.get("id")) for b in ctx.children(pid)]
                first = (req.get("body") or {}).get("children") or []
                got, raw = _live_tree(ctx, kids[:len(first)])
                _j_update(ctx, r["seq"], after_json={"page": page, "blocks": raw})
                for x in raw:
                    _collect_ids(x, new_ids)
                if _rich_plain((_full_prop(ctx, page, tp) or {}).get("title")) != exp.get("title"):
                    w1 = False
                    problems.append("CREATE_PAGE：新页面标题不符")
                if got != exp.get("children"):
                    w1 = False
                    problems.append("CREATE_PAGE：新页面内容回读与预期不一致")
                if _nid((page.get("created_by") or {}).get("id")) != bot:
                    w1 = False
                    problems.append("CREATE_PAGE：创建者不是写回集成")
        except NotionAPIError as exc:
            w1 = False
            problems.append(f"{op} 回读失败（{exc.status} {exc.code}）")
    # W2: untouched blocks of the page unchanged
    if g.kind == "page" and g.snap and g.snap.get("tree") and rows and not any(r["op"] == "TRASH_PAGE" for r in rows):
        try:
            after = _page_tree(ctx, g.page_id)
        except NotionAPIError as exc:
            after = None
            w2 = False
            problems.append(f"W2 回读失败（{exc.status}）")
        if after is not None:
            before = g.snap["tree"]["by_id"]
            for t in trashed:                  # trashing a last child flips its parent's has_children
                par = _parent_in_tree(g.snap["tree"], t)
                if par:
                    parents.add(par)
            for bid, b in before.items():
                if bid in touched or bid in trashed:
                    continue
                a = after["by_id"].get(bid)
                if a is None:
                    w2 = False
                    problems.append(f"未触碰的块 {bid[:8]} 消失了（计划外变化）")
                    continue
                na, nb = wb_norm.json_norm(a), wb_norm.json_norm(b)
                if bid in parents:
                    na.pop("has_children", None)
                    nb.pop("has_children", None)
                if na != nb:
                    w2 = False
                    problems.append(f"未触碰的块 {bid[:8]} 前后不一致（计划外变化）")
            for bid in after["by_id"]:
                if bid not in before and bid not in new_ids and bid not in ctx.ours:
                    w2 = False
                    problems.append(f"出现计划外的新块 {bid[:8]}")
    ctx.readback["W1"].append(w1)
    ctx.readback["W2"].append(w2)
    return w1, w2, problems


def _collect_ids(raw, acc):
    acc.add(_nid(raw.get("id")))
    for c in raw.get("_children") or []:
        _collect_ids(c, acc)


def _position_ok(ctx, row, ids, req):
    parent = row["target_id"]
    cur = _child_ids(ctx, parent)
    if not all(i in cur for i in ids):
        return False
    i0 = cur.index(ids[0])
    if cur[i0:i0 + len(ids)] != ids:
        return False
    pos = req.get("position")
    if isinstance(pos, dict) and pos.get("type") == "start":
        return i0 == 0
    if isinstance(pos, dict) and pos.get("type") == "after_block":
        a = _nid((pos.get("after_block") or {}).get("id"))
        return i0 > 0 and cur[i0 - 1] == a
    return True


# =============================================================================================== page runner
def _run_group(ctx, g):
    """Execute the intent rows of one page. Returns page status."""
    ctx.guard_write()
    for s in g.steps:
        row = _j_row(ctx.db, s["seq"])
        if row is None or row["state"] != "intent":
            continue
        ctx.guard_write()
        if ctx.paused_s > PAUSE_RECHECK_S:
            ctx.paused_s = 0.0
            _recheck_remaining(ctx, g, s["seq"])
        _exec_step(ctx, g, s["seq"])
    return "done"


def _recheck_remaining(ctx, g, from_seq):
    """After a rate-limit pause > 30 s: re-run the page precheck for the remaining UPDATE/TRASH targets."""
    for s in g.steps:
        if s["seq"] < from_seq:
            continue
        row = _j_row(ctx.db, s["seq"])
        if row and row["state"] == "intent" and row["op"] in ("UPDATE", "TRASH_BLOCK"):
            live = ctx.get_block(row["target_id"])
            if wb_norm.block_json_sha(live) != wb_norm.block_json_sha(_jload(row["before_json"], {})):
                raise _PageStop("限流暂停后复核：块已改变", blocked=g.written)
    if g.page_id:
        page = ctx.get_page(g.page_id)
        if page.get("in_trash") or page.get("is_locked"):
            raise _PageStop("限流暂停后复核：页面已锁定或在回收站", blocked=g.written)


def _cancel_rows(ctx, g, reason):
    for s in g.steps:
        row = _j_row(ctx.db, s["seq"])
        if row and row["state"] == "intent":
            _j_update(ctx, s["seq"], state="cancelled", committed_ids={"outcome": "cancelled", "message": reason[:300]})


def _block_group(ctx, g, reason):
    """page_state of a page that was (or may have been) written and stopped: PARTIAL_CREATE for a page this run
    created, BLOCKED for an existing page; a new page whose create could not be identified has no page id --
    it is recorded as an 'unknown' mapping (+ journal seq for reconcile), never as page_state(None)."""
    if g.new_page:
        _set_page_state(ctx, g.new_page, "PARTIAL_CREATE", reason)
        ctx.blocked_pages.append(g.new_page)
        if g.ok_items:
            _write_mapping(ctx, g.ok_items[0], g.new_page, "partial")
    elif g.page_id:
        _set_page_state(ctx, g.page_id, "BLOCKED", reason)
        ctx.blocked_pages.append(g.page_id)
    else:
        for it in g.ok_items:
            row = next((r for r in reversed(_item_rows(ctx, g, it)) if r["op"] == "CREATE_PAGE"), None)
            if row and row["state"] in ("sent", "unknown") and not _mapping(ctx.db, it.get("rel_path")):
                _write_mapping(ctx, it, None, "unknown", journal_seq=row["seq"])


def _finish_group(ctx, g, stop=None):
    """Item statuses + page_state after a page ran (or stopped)."""
    key = g.new_page or g.page_id or g.key
    if stop is not None:
        _cancel_rows(ctx, g, stop.reason)
        if g.kind == "new":
            unknown = any(_item_state(ctx, g, it) == "unknown" for it in g.ok_items)
            if g.new_page or unknown:
                _block_group(ctx, g, stop.reason)
            for it in g.ok_items:
                if g.new_page:
                    st = "blocked"
                elif _item_state(ctx, g, it) == "unknown":
                    st = "unknown"
                else:
                    st = "failed" if _item_sent(ctx, g, it) else "skipped"
                _set_item(ctx, it["item_id"], st, stop.reason)
        elif stop.blocked:
            _block_group(ctx, g, stop.reason)
            for it in g.ok_items:
                st = _item_state(ctx, g, it)
                _set_item(ctx, it["item_id"], "unknown" if st == "unknown" else "blocked", stop.reason)
        else:
            for it in g.ok_items:
                _set_item(ctx, it["item_id"], "failed" if _item_sent(ctx, g, it) else "skipped", stop.reason)
        ctx.page_res[key] = {"status": "blocked" if (stop.blocked or stop.partial_create) else "stopped",
                             "reason": stop.reason}
        if g.written and (g.kind == "page" or g.new_page):
            w1, w2, problems = _readback_group(ctx, g)
            if problems:
                ctx.page_res[key]["readback"] = problems
        return
    w1, w2, problems = _readback_group(ctx, g)
    if not (w1 and w2):
        reason = "回读校验不一致：" + "；".join(problems[:5])
        _block_group(ctx, g, reason)
        for it in g.ok_items:
            _set_item(ctx, it["item_id"], "blocked", reason)
        ctx.page_res[key] = {"status": "blocked", "reason": reason, "readback": problems}
        return
    if g.kind == "new" and g.new_page:
        _write_mapping(ctx, g.ok_items[0], g.new_page, "complete")
    for it in g.ok_items:
        _set_item(ctx, it["item_id"], "executed", None)
        if it.get("category") == "D":
            _after_delete(ctx, it)
        if it.get("category") == "T":
            _after_rename(ctx, it)
    ctx.page_res[key] = {"status": "done"}


def _item_rows(ctx, g, it):
    return [r for r in (_j_row(ctx.db, s["seq"]) for s in g.steps) if r and r["item_id"] == it["item_id"]]


def _item_state(ctx, g, it):
    rows = _item_rows(ctx, g, it)
    if any(r["state"] in ("unknown", "sent") for r in rows):
        return "unknown"
    return "other"


def _item_sent(ctx, g, it):
    return any(r["state"] not in ("intent", "cancelled") for r in _item_rows(ctx, g, it))


def _after_delete(ctx, it):
    nid = _nid(it.get("notion_id"))
    with wb_state.transaction(ctx.db):
        ctx.db.execute("UPDATE pending_deletes SET status='confirmed' WHERE notion_id=? AND status='hold'", (nid,))
        ctx.db.execute("UPDATE plan_items SET verify_status='pending_next_round' WHERE plan_id=? AND item_id=?",
                       (ctx.plan_id, it["item_id"]))


def _after_rename(ctx, it):
    nid = _nid(it.get("notion_id"))
    with wb_state.transaction(ctx.db):
        ctx.db.execute("UPDATE name_overrides SET status='closed' WHERE notion_id=? AND status='open'", (nid,))


# =============================================================================================== execute
def execute_confirmed_plan(plan_id, selection, confirmation_record, *, client, read_client, cfg, db, now=None,
                           work=None, vault=None, cfg_path=None, adopter=None, runner=None, blobs_dir=None,
                           sidebar=None):
    """Internal entry point of `writeback.py apply` (called only after the human gate). See module docstring.
    Returns the result dict of contract §5.13.1."""
    paths = Paths(work, vault)
    if cfg_path is None and os.path.isfile(paths.config):
        cfg_path = paths.config
    res = _empty_result(plan_id)
    try:
        sel = _norm_selection(selection)
    except ApplyRefused as exc:
        return dict(res, status="refused", reason=exc.reason)
    sel_ids = _sel_ids(sel)
    try:
        with wb_locks.lock_all(["writeback", "refresh"], state_dir=paths.state):
            try:
                return _execute_locked(plan_id, sel, sel_ids, confirmation_record, client=client,
                                       read_client=read_client, cfg=cfg, db=db, now=now, paths=paths, cfg_path=cfg_path,
                                       adopter=adopter, runner=runner, blobs_dir=blobs_dir, sidebar=sidebar)
            except Exception as exc:  # noqa: BLE001 - a defect in the start checks: nothing was sent
                return _start_defect(db, plan_id, res, exc, "confirmed")
    except wb_locks.LockBusy as exc:
        return dict(res, status="refused", reason=str(exc))


def _start_defect(db, plan_id, res, exc, from_status):
    """An unexpected exception BEFORE the plan went to `applying` (start checks): no request was sent; the plan
    is closed (aborted) so it cannot be left half-confirmed, and a plain reason is returned instead of a traceback."""
    reason = (f"执行前检查出现程序错误（{exc.__class__.__name__}）：没有向 Notion 发送任何写请求；计划已中止，"
              f"请保留输出并联系维护者，然后重新运行 {cli_command('writeback', 'plan')}")
    try:
        wb_state.cas_plan_status(db, plan_id, from_status, "aborted", reason)
    except Exception:  # noqa: BLE001
        pass
    return dict(res, status="refused", reason=reason, error=f"{exc.__class__.__name__}: {exc}"[:300])


def _empty_result(plan_id):
    return {"plan_id": plan_id, "status": "refused", "reason": None, "executed": [], "skipped": [], "unknown": [],
            "blocked_pages": [], "w_checks": {}, "result_md": None, "vault_record": None, "requests": 0}


def _execute_locked(plan_id, sel, sel_ids, record, *, client, read_client, cfg, db, now, paths, cfg_path, adopter,
                    runner, blobs_dir, sidebar):
    res = _empty_result(plan_id)
    try:
        _check_token(client)                    # start check 1: token permissions
        if not isinstance(read_client, ReadOnlyClient):
            raise ApplyRefused("读取客户端必须是 ReadOnlyClient")
        plan, sha, row = load_plan_verified(plan_id, work=paths.work, db=db)    # 2: plan validity + full sha
        vp = vault_problem(paths, plan)
        if vp:
            raise ApplyRefused(vp)
    except ApplyRefused as exc:
        return dict(res, reason=exc.reason)
    if row.get("status") != "confirmed":
        return dict(res, reason=f"计划状态是 {row.get('status')}，不是 confirmed（不能执行）")
    if not sel_ids:
        return dict(res, reason="没有选择任何项目")
    if sel.get("resolve"):
        return dict(res, reason="冲突项（C）只能经 resolve 处理，不能 apply")
    for iid in sel_ids:
        it = next((x for x in plan["items"] if isinstance(x, dict) and x.get("item_id") == iid), None)
        if it is None:
            return dict(res, reason=f"选择项 {iid} 不在计划中")
        cat = it.get("category")
        bucket = next((b for b, ids in sel.items() if iid in ids), None)
        if cat not in BUCKETS.get(bucket, ()):
            return dict(res, reason=f"{iid}（{cat}）不能通过 {bucket} 选择")
        if cat not in EXEC_CATEGORIES:
            return dict(res, reason=f"{iid}（{cat}）不是可执行项（MV/C 不支持）")
        if it.get("executable") is False or it.get("refused"):
            return dict(res, reason=f"{iid} 在计划中不可执行")
    ctx = Ctx(plan_id=plan_id, plan=plan, plan_sha=sha, plan_row=row, client=client, read_client=read_client, cfg=cfg,
              cfg_path=cfg_path, db=db, paths=paths, now=now, label="apply")
    ctx.selected = set(sel_ids)
    try:
        _start_checks(ctx, sel_ids, record, "apply", sel)
    except ApplyRefused as exc:
        if exc.abort:
            to = "expired" if "过期" in exc.reason else "aborted"
            wb_state.cas_plan_status(db, plan_id, "confirmed", to, exc.reason)
        return dict(res, reason=exc.reason)
    except _StopRun as exc:
        wb_state.cas_plan_status(db, plan_id, "confirmed", "aborted", exc.reason)
        return dict(res, reason=exc.reason)
    if not wb_state.cas_plan_status(db, plan_id, "confirmed", "applying", None):
        return dict(res, reason=f"计划状态刚被另一个进程改变（不再是 confirmed），未执行任何写入；请运行 {cli_command('writeback', 'status')} 查看")
    for iid in sel_ids:
        _set_item(ctx, iid, "not_executed", None)
    prev_hook = _install_hook(ctx)
    try:
        return _run(ctx, sel_ids, adopter=adopter, runner=runner, blobs_dir=blobs_dir, sidebar=sidebar)
    except Exception as exc:  # noqa: BLE001 - never leave the plan in `applying` without a result
        return _crash_finalize(ctx, exc)
    finally:
        ctx.client.on_send = prev_hook


def _any_forward_send(ctx):
    return any(r["state"] not in ("intent", "cancelled") for r in _j_rows(ctx.db, ctx.plan_id)
               if not r["op"].startswith("RB_") and r["op"] != "VAULT_FRONTMATTER")


def _crash_finalize(ctx, exc):
    """Any unexpected exception after the plan went to `applying`: the plan is closed (never left `applying`),
    remaining intents are cancelled, a result is written. Nothing is resent."""
    if isinstance(exc, _StopRun):          # STOP file / mode drop noticed while a stopped page was being closed
        reason, status = exc.reason, "aborted"
    else:
        reason = (f"执行器内部错误（{exc.__class__.__name__}）：已停止，没有重发任何请求；请保留 result.json 并联系维护者")
        ctx.notes.append(f"内部错误详情（给维护者）：{exc.__class__.__name__}: {str(exc)[:200]}")
        status = "partially_applied" if _any_forward_send(ctx) else "aborted"
    ctx.abort_reason = reason
    wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", status, reason)
    try:
        return _finalize(ctx, status, adopt=False)
    except Exception as exc2:  # noqa: BLE001 - last resort: still return a result and cancel the intents
        for r in _j_rows(ctx.db, ctx.plan_id, ("intent",)):
            _j_update(ctx, r["seq"], state="cancelled", committed_ids={"outcome": "cancelled", "message": reason[:300]})
        out = _empty_result(ctx.plan_id)
        out.update(status=status, reason=reason, notes=list(ctx.notes) + [f"结果文件写入失败：{exc2.__class__.__name__}"])
        return out


def _run(ctx, sel_ids, *, adopter, runner, blobs_dir, sidebar, resume_groups=None):
    stopped = None
    internal = False
    groups = resume_groups
    if groups is None:
        groups = _groups(ctx, sel_ids)
        try:
            inconsistent = 0
            for g in groups:
                fails = _precheck_group(ctx, g)
                for iid, why in fails.items():
                    _set_item(ctx, iid, "skipped", why, conflict=_skip_class(why))
                # Lead decision (L-18 fix loop 2): only a live drift / refusal (CONFLICT_BOTH) makes a page count
                # toward the abort. A hold or a new child page (AUTO_SYNC_HOLD / NOT_LEAF) skips only its own page:
                # the planner drops some plan-time holds (an own sync_conflict_copy / pending_delete* hold), so one
                # such note must not stop the writes of unrelated clean notes in the same round.
                if any(_skip_class(why) == "CONFLICT_BOTH" for why in fails.values()):
                    inconsistent += 1
            if groups and inconsistent > INCONSISTENT_ABORT_RATIO * len(groups):
                ctx.abort_reason = (f"预检不一致的页面 {inconsistent}/{len(groups)} 超过所选页面的 20%：整轮中止，"
                                    "未发送任何写请求")
                wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", "aborted", ctx.abort_reason)
                return _finalize(ctx, "aborted", adopt=False)
            if groups and not any(g.ok_items for g in groups):
                # every selected page skipped (only holds / leaf skips, else the ratio above already aborted):
                # nothing ran, so the round is aborted -- not "partially applied" with a rollback hint
                ctx.abort_reason = (f"所选的 {len(groups)} 个页面都未通过预检（各项原因见 result.md）：整轮中止，"
                                    "未发送任何写请求")
                wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", "aborted", ctx.abort_reason)
                return _finalize(ctx, "aborted", adopt=False)
            for g in groups:
                if g.ok_items:
                    _build_steps(ctx, g)
        except _StopRun as exc:
            ctx.abort_reason = exc.reason
            wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", "aborted", exc.reason)
            return _finalize(ctx, "aborted", adopt=False)
    for gi, g in enumerate(groups):
        if not g.steps:
            continue
        if stopped is not None:
            _cancel_rows(ctx, g, stopped)
            for it in g.ok_items:
                _set_item(ctx, it["item_id"], "not_executed", stopped)
            continue
        try:
            if ctx.label == "apply" and gi > 0:
                fails = _recheck_group(ctx, g)           # fresh page precheck right before the page (WB §11.3.1)
                if fails:
                    for iid, why in fails.items():
                        _set_item(ctx, iid, "skipped", why, conflict=_skip_class(why))
                    keep = {it["item_id"] for it in g.ok_items}
                    for s in g.steps:
                        r = _j_row(ctx.db, s["seq"])
                        if r and r["item_id"] not in keep and r["state"] == "intent":
                            _j_update(ctx, s["seq"], state="cancelled", committed_ids={"outcome": "cancelled",
                                                                                         "message": "precheck"})
                    g.steps = [s for s in g.steps if (_j_row(ctx.db, s["seq"]) or {}).get("item_id") in keep]
                    if not g.steps:
                        continue
                    _refresh_befores(ctx, g)
            elif ctx.label == "resume" and gi > 0:
                _resume_recheck(ctx, g)                  # the same per-page re-check in apply --resume (L-18 a/c)
            _run_group(ctx, g)
            _finish_group(ctx, g)
        except _PageStop as exc:
            _finish_group(ctx, g, exc)
        except _StopRun as exc:
            stopped = exc.reason
            ctx.abort_reason = exc.reason
            if ctx.stop_requested():
                _stop_group_no_reads(ctx, g, exc.reason)      # STOP: no further request at all, not even reads
            else:
                _finish_group(ctx, g, _PageStop(exc.reason, blocked=_page_has_writes(ctx, g)))
        except NotionAPIError as exc:
            _finish_group(ctx, g, _PageStop(f"读取失败（{exc.status} {exc.code}）", blocked=g.written or
                                            _page_has_writes(ctx, g)))
        except Exception as exc:  # noqa: BLE001 - a program defect stops the RUN (no further page), never resends
            internal = True
            stopped = (f"执行器内部错误（{exc.__class__.__name__}）：已停止本页及其后所有页面，没有重发任何请求；"
                       "请保留 result.json 并联系维护者")
            ctx.abort_reason = stopped
            ctx.notes.append(f"内部错误详情（给维护者）：{exc.__class__.__name__}: {str(exc)[:200]}")
            try:
                _stop_group_no_reads(ctx, g, stopped)
            except Exception as exc2:  # noqa: BLE001
                ctx.notes.append(f"停止本页时又出错（给维护者）：{exc2.__class__.__name__}")
    if stopped is not None:
        status = "aborted"
        if internal and _any_forward_send(ctx):
            status = "partially_applied"          # something reached Notion before the defect: not a clean abort
        wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", status, stopped)
        return _finalize(ctx, status, adopt=False)
    return _finalize(ctx, None, adopt=True, adopter=adopter, runner=runner, blobs_dir=blobs_dir, sidebar=sidebar)


def _recheck_group(ctx, g):
    """Per-page precheck again right before the page's writes, restricted to the items that passed the first
    pass (items skipped there stay skipped, they have no journal rows)."""
    all_items = g.items
    g.items = list(g.ok_items)
    try:
        return _precheck_group(ctx, g)
    finally:
        g.items = all_items


def _resume_recheck(ctx, g):
    """apply --resume, right before each page after the first: a fresh page + tree and the resume precheck of its
    remaining intent rows again (WB §11.3.1; L-18 (a)/(c) "before writing each page" -- a child page / database
    or a hold can appear while the earlier pages are written). A failure raises _PageStop: _run's handler
    (_finish_group) cancels the page's intent rows and records the items, as the resume's first precheck does.
    The fresh tree is the page's W2 baseline and the journal before-images (still equal by sha)."""
    g.snap = _resume_snap(ctx, g)
    _resume_precheck(ctx, g)
    _refresh_befores(ctx, g)


def _stop_group_no_reads(ctx, g, reason):
    _cancel_rows(ctx, g, reason)
    written = g.written or _page_has_writes(ctx, g)
    if written:
        _block_group(ctx, g, reason)
    for it in g.ok_items:
        st = _item_state(ctx, g, it)
        _set_item(ctx, it["item_id"], ("unknown" if st == "unknown" and not g.new_page else "blocked") if written
                  else "not_executed", reason)
    ctx.page_res[g.new_page or g.page_id or g.key] = {"status": "stopped", "reason": reason}


def _refresh_befores(ctx, g):
    """After the per-page precheck: journal before-images = the fresh tree (still equal by sha)."""
    tree = (g.snap or {}).get("tree")
    if not tree:
        return
    for s in g.steps:
        r = _j_row(ctx.db, s["seq"])
        if r and r["state"] == "intent" and r["op"] in ("UPDATE", "TRASH_BLOCK") and r["target_id"] in tree["by_id"]:
            _j_update(ctx, s["seq"], before_json=tree["by_id"][r["target_id"]])
        elif r and r["state"] == "intent" and r["op"] == "APPEND" and r["target_id"]:
            _j_update(ctx, s["seq"], before_json={"child_ids": list(tree["kids"].get(r["target_id"], []))})


# =============================================================================================== adoption hold-back (L-18 d)
def _note_key(it):
    """Items of the same note: same Notion page, or the same new file (I1's note_key)."""
    return _nid(it.get("notion_id")) or ("path:" + str(it.get("rel_path") or ""))


def _adoption_holdback(ctx, written):
    """{note key: {notion_id, rel_path, items, reason}} for executed notes that must NOT be adopted: the same note
    has a plan item that was not executed now (unselected, rejected, skipped, failed, blocked, unknown, a conflict
    or move item), or -- except for a page delete -- a local change this plan does not write (a `refused[]` /
    `rejected[]` entry of the plan, or an active rejection of the same note and L sha). I1's selection rule makes
    this impossible through the CLI; this is the executor's own guard (defence in depth)."""
    done = set(written)
    decisions = {r["item_id"]: r["decision"] for r in ctx.db.execute(
        "SELECT item_id, decision FROM plan_items WHERE plan_id=?", (ctx.plan_id,))}
    by_note = {}
    for iid in ctx.order:
        by_note.setdefault(_note_key(ctx.items[iid]), []).append(ctx.items[iid])
    unwritten = []
    for e in list(ctx.plan.get("refused") or []) + list(ctx.plan.get("rejected") or []):
        if not isinstance(e, dict):
            continue
        what = e.get("class") or ("已拒绝" + (f" {e['item_category']}" if e.get("item_category") else ""))
        rels = {r for r in str(e.get("rel_path") or "").split(", ") if r} | ({e["old_rel"]} if e.get("old_rel") else set())
        unwritten.append(({_nid(e.get("notion_id"))} - {None}, rels, str(what)))
    rejections = ctx.db.execute("SELECT notion_id, rel_path, l_sha, item_category FROM rejections WHERE active=1").fetchall()
    out, checked = {}, set()
    for iid in written:
        it = ctx.items[iid]
        key = _note_key(it)
        if key in checked:
            if key in out:
                out[key]["items"].append(iid)
            continue
        checked.add(key)
        reasons = []
        for o in by_note.get(key, []):
            oid = o.get("item_id")
            if oid in done:
                continue
            r = ctx.item_res.get(oid) or {}
            if decisions.get(oid) == "rejected":
                why = "已被拒绝"
            elif oid not in ctx.selected:
                why = "未选择/未确认"
            else:
                why = STATUS_ZH.get(r.get("status"), r.get("status") or "未执行")
            reasons.append(f"{oid}（{o.get('category')}）{why}")
        nid = _nid(it.get("notion_id"))
        if it.get("category") != "D":
            mine = {it.get(k) for k in ("rel_path", "old_rel") if it.get(k)}
            for o in by_note.get(key, []):
                mine |= {o.get(k) for k in ("rel_path", "old_rel") if o.get(k)}
            for ids, rels, what in unwritten:
                if (nid and nid in ids) or (mine & rels):
                    reasons.append(f"本计划不写回的本地改动（{what}）")
            l_shas = {o.get("l_sha") for o in by_note.get(key, []) if o.get("item_id") in done}
            for x in rejections:
                same = (nid and _nid(x["notion_id"]) == nid) or (x["rel_path"] and x["rel_path"] in mine)
                if same and (x["l_sha"] is None or x["l_sha"] in l_shas):
                    reasons.append(f"已拒绝的本地改动（{x['item_category'] or '?'}）")
        if reasons:
            out[key] = {"notion_id": nid or ctx.created.get(iid), "rel_path": it.get("rel_path"), "items": [iid],
                        "reason": "；".join(dict.fromkeys(reasons))}
    return out


def _mark_holdback(ctx):
    for h in ctx.adopt_holdback.values():
        for iid in h["items"]:
            ctx.item_res.setdefault(iid, {"item_id": iid})["adoption"] = "held_back"
            try:
                with wb_state.transaction(ctx.db):
                    ctx.db.execute("UPDATE plan_items SET verify_status='needs_manual' WHERE plan_id=? AND item_id=?",
                                   (ctx.plan_id, iid))
            except sqlite3.Error:
                pass


def _merge_holdback(ctx, adoption):
    """W6–W8 fail and 待人工处理 entries for the notes that were not adopted (L-18 d)."""
    hb = ctx.adopt_holdback
    if not hb:
        return adoption
    adoption = dict(adoption or {})
    rows = [{"notion_id": h["notion_id"], "rel_path": h["rel_path"], "items": list(h["items"]), "reason": h["reason"]}
            for h in hb.values()]
    adoption["held_back"] = rows
    adoption["ok"] = False
    nm = list(adoption.get("needs_manual") or [])
    for h in rows:
        for iid in h["items"]:
            it = ctx.items.get(iid) or {}
            k = _nid(it.get("notion_id")) or ctx.created.get(iid) or ("path:" + str(it.get("rel_path") or ""))
            if k not in nm:
                nm.append(k)
    adoption["needs_manual"] = nm
    detail = "待人工处理（未采纳，库内保留本地版本 LOCAL_ONLY）：" + "；".join(
        f"{h['rel_path'] or h['notion_id'] or '本次写入的笔记'}（{', '.join(h['items'])}）：{h['reason']}" for h in rows)
    for k in ("W6", "W7", "W8"):
        prev = adoption.get(k) or {}
        keep = prev.get("detail") if prev.get("status") == "fail" and prev.get("detail") else ""
        adoption[k] = _w("fail", "；".join(x for x in (keep, detail) if x))
    if not adoption.get("reason"):
        adoption["reason"] = "部分笔记未采纳（同一笔记还有未写回的项目或本地改动），待人工处理"
    return adoption


# =============================================================================================== finalize
def _finalize(ctx, forced_status, *, adopt, adopter=None, runner=None, blobs_dir=None, sidebar=None):
    _settle_frontmatter_intents(ctx)
    rows = _j_rows(ctx.db, ctx.plan_id)
    for r in rows:
        if r["state"] == "intent":
            _j_update(ctx, r["seq"], state="cancelled", committed_ids={"outcome": "cancelled",
                                                                        "message": ctx.abort_reason or "not executed"})
    adoption = None
    written = [iid for iid, r in ctx.item_res.items() if r.get("status") == "executed"]
    if adopt and written and not ctx.stop_requested():
        # L-18 (d): a note that still has an unselected / rejected / skipped / failed item (or a local change this
        # plan does not write) is never adopted -- the adoption would replace the vault file by the Notion version
        # and drop that change. It stays local (held from the publish, LOCAL_ONLY) and is listed as 待人工处理.
        try:
            ctx.adopt_holdback = _adoption_holdback(ctx, written)
        except Exception as exc:  # noqa: BLE001 - the check itself failed: adopt nothing (fail closed)
            ctx.adopt_holdback = {"check_failed": {
                "notion_id": None, "rel_path": None, "items": list(written),
                "reason": f"无法确认同一笔记是否还有未写回的项目（{exc.__class__.__name__}）：本次不采纳任何笔记"}}
        held = {i for h in ctx.adopt_holdback.values() for i in h["items"]}
        to_adopt = [i for i in written if i not in held]
        if ctx.adopt_holdback:
            _mark_holdback(ctx)
        try:
            fn = adopter or _adopt_pipeline
            adoption = fn(ctx, to_adopt, runner=runner, blobs_dir=blobs_dir, sidebar=sidebar) if to_adopt else \
                {"ok": False, "needs_manual": [], "reason": "本次写入的笔记都还有未写回的项目或本地改动：不采纳（见待人工处理）"}
        except Exception as exc:  # noqa: BLE001 - adoption failure never undoes Notion writes; reported
            adoption = {"ok": False, "needs_manual": to_adopt,
                        "reason": (f"库内采纳步骤出现程序错误（{exc.__class__.__name__}）：Notion 中的写入不受影响，库内笔记"
                                   "暂未更新；下一次 refresh 会按 Notion 版本更新（先备份）。请保留 result.json 并联系维护者"),
                        "error": f"{exc.__class__.__name__}: {exc}"[:300]}
        adoption = _merge_holdback(ctx, adoption)
    elif written and not adopt:
        adoption = {"ok": False, "reason": ctx.abort_reason or "未运行采纳", "needs_manual": written}
    w = _w_checks(ctx, adoption)
    executed = [i for i in ctx.order if ctx.item_res.get(i, {}).get("status") == "executed"]
    skipped = [{"item_id": i, "status": ctx.item_res[i]["status"], "reason": ctx.item_res[i].get("reason")}
               for i in ctx.order if i in ctx.item_res and ctx.item_res[i]["status"] not in ("executed",)]
    unknown = [i for i in ctx.order if ctx.item_res.get(i, {}).get("status") == "unknown"]
    if forced_status:
        status = forced_status
    else:
        all_ok = all(v["status"] in ("pass", "n/a") for v in w.values())
        status = "applied" if (all_ok and not skipped and not ctx.blocked_pages) else "partially_applied"
        if not wb_state.cas_plan_status(ctx.db, ctx.plan_id, "applying", status, None if status == "applied" else
                                        "部分完成，待人工处理"):
            ctx.notes.append(f"计划状态未能从 applying 更新为最终状态（可能另一个进程同时改了它）：请运行 {cli_command('writeback', 'status')} 查看")
    out = _empty_result(ctx.plan_id)
    out.update(status=status, reason=ctx.abort_reason, executed=executed, skipped=skipped, unknown=unknown,
               blocked_pages=sorted(set(ctx.blocked_pages)), w_checks=w,
               requests=sum(1 for s in ctx.sends), adoption=adoption, notes=list(ctx.notes),
               items={i: ctx.item_res[i] for i in ctx.order if i in ctx.item_res}, created=dict(ctx.created),
               audit_scope=ctx.audit)
    try:
        out["result_md"], out["vault_record"] = _write_outputs(ctx, out)
    except OSError as exc:
        out["notes"].append(f"输出文件写入失败：{exc}")
    return out


# =============================================================================================== W1–W10
def _w(status, detail=""):
    return {"status": status, "detail": detail}


def _w_checks(ctx, adoption):
    rows = [r for r in _j_rows(ctx.db, ctx.plan_id) if r["op"] not in ("VAULT_FRONTMATTER",)
            and not r["op"].startswith("RB_")]
    w = {}
    sent_rows = [r for r in rows if r["state"] in ("done", "reconciled", "unknown", "failed", "sent")]
    # W1
    if not ctx.readback["W1"]:
        w["W1"] = _w("n/a", "无已执行操作")
    else:
        w["W1"] = _w("pass" if all(ctx.readback["W1"]) else "fail",
                     f"{sum(ctx.readback['W1'])}/{len(ctx.readback['W1'])} 个页面回读一致")
    w["W2"] = _w("n/a", "无") if not ctx.readback["W2"] else \
        _w("pass" if all(ctx.readback["W2"]) else "fail", "被触碰页面中未触碰块前后一致" if all(ctx.readback["W2"])
           else "存在计划外变化")
    # W3: every request maps to a journal row of a confirmed + selected item; audit clean
    bad = []
    seqs = {r["seq"]: r for r in rows}
    for method, rel, seq in ctx.sends:
        r = seqs.get(seq) or (_j_row(ctx.db, seq) if seq else None)
        if r is None or r["item_id"] not in ctx.selected:
            bad.append(f"{method} {rel}")
            continue
        why = _send_target_problem(ctx, method, rel, r)
        if why:
            bad.append(f"{method} {rel}（{why}）")
    audit_ok = bool(ctx.audit and ctx.audit.get("ok"))
    w["W3"] = _w("pass" if (not bad and audit_ok) else ("n/a" if not ctx.sends and audit_ok else "fail"),
                 f"{len(ctx.sends)} 个写请求均对应已确认项且目标属于该项的页面；audit-scope 无多余对象"
                 if not bad and audit_ok else f"不符合范围的请求：{bad[:5]}；audit ok={audit_ok}")
    # W4 deletions
    d_items = [i for i in ctx.selected if (ctx.items.get(i) or {}).get("category") == "D"]
    if not d_items:
        w["W4"] = _w("n/a", "无删除项")
    else:
        ok = all(ctx.item_res.get(i, {}).get("status") == "executed" for i in d_items)
        w["W4"] = _w("pass" if ok else "fail", "已移入回收站并回读确认；verify_status=pending_next_round（下一轮置 verified）"
                     if ok else "删除项未全部执行")
    # W5 renames
    t_items = [i for i in ctx.selected if (ctx.items.get(i) or {}).get("category") == "T"]
    if not t_items:
        w["W5"] = _w("n/a", "无改名项")
    else:
        probs = []
        for i in t_items:
            it = ctx.items[i]
            if ctx.item_res.get(i, {}).get("status") != "executed":
                probs.append(f"{i} 未执行")
                continue
            if not _vault_sha(ctx, it.get("rel_path")):
                probs.append(f"{i} 库内文件不在 {it.get('rel_path')}")
            r = ctx.db.execute("SELECT 1 FROM name_overrides WHERE notion_id=? AND status='open'",
                               (_nid(it.get("notion_id")),)).fetchone()
            if r:
                probs.append(f"{i} name_overrides 未清除")
        w["W5"] = _w("fail" if probs else "pass", "；".join(probs) or "标题、文件名与命名差异记录一致")
    # W6 / W7 / W8 from adoption
    if adoption is None:
        na = not any(ctx.item_res.get(i, {}).get("status") == "executed" for i in ctx.selected)
        w["W6"] = _w("n/a" if na else "fail", "无已写回笔记" if na else "未运行采纳")
        w["W7"] = _w("n/a" if na else "fail", "")
        w["W8"] = _w("n/a" if na else "fail", "")
    else:
        w["W6"] = adoption.get("W6") or _w("fail", adoption.get("reason") or "采纳失败")
        w["W7"] = adoption.get("W7") or _w("fail", adoption.get("reason") or "采纳失败")
        w["W8"] = adoption.get("W8") or _w("fail", adoption.get("reason") or "采纳失败")
    fm_missing = sorted(i for i in ctx.fm_skipped if ctx.item_res.get(i, {}).get("status") == "executed")
    if fm_missing:
        w["W8"] = _w("fail", "；".join(x for x in [(w.get("W8") or {}).get("detail") if
                                                   (w.get("W8") or {}).get("status") == "fail" else "",
                                                   f"新页面已建，但库内笔记未写入 notion_id（{', '.join(fm_missing)}，"
                                                   "待人工处理）"] if x))
    # W9 block budget
    ledger = ctx.db.execute("SELECT COALESCE(SUM(created_blocks),0) FROM block_budget WHERE plan_id=?",
                            (ctx.plan_id,)).fetchone()[0]
    actual = 0
    for r in rows:
        if r["op"] == "APPEND" and r["state"] in ("done", "reconciled"):
            actual += _count_raw(_jload(r["after_json"], []) or [])
        elif r["op"] == "CREATE_PAGE" and r["state"] in ("done", "reconciled"):
            actual += 1 + _count_raw((_jload(r["after_json"], {}) or {}).get("blocks") or [])
    probs = []
    if ledger != actual:
        probs.append(f"台账 {ledger} ≠ 回读新块 {actual}")
    if ctx.block_limit_hit:
        probs.append("出现额度 403（creation_state=blocked_confirmed）")
    if ctx.creation_state not in (None, "unlimited") and ledger:
        probs.append(f"creation_state={ctx.creation_state} 但新建了块")
    w["W9"] = _w("fail" if probs else ("pass" if ledger or actual else "n/a"),
                 "；".join(probs) or f"新建块 {actual}，台账 {ledger}，creation_state={ctx.creation_state}")
    # W10 coverage
    not_done = [i for i in ctx.selected if ctx.item_res.get(i, {}).get("status") != "executed"]
    w1_ok = w["W1"]["status"] in ("pass", "n/a")
    w["W10"] = _w("pass" if (not not_done and w1_ok) else "fail",
                  "每个已确认项都已执行且回读一致" if not not_done and w1_ok else
                  f"未完成：{', '.join(sorted(not_done)[:20])}")
    return {k: w[k] for k in W_KEYS}


def _send_target_problem(ctx, method, rel, row):
    """W3 (WB §2.3 target binding): the request went to the journal row's target, and that target belongs to the
    item: the item's own page for page ops, a block of the item page's prechecked tree for block ops, the page /
    a block of it / a page or block this run created for APPEND. None = fine."""
    op = row["op"]
    it = ctx.items.get(row["item_id"]) or {}
    page = _nid(it.get("notion_id"))
    parts = str(rel or "").split("/")
    if op == "CREATE_PAGE":
        return None if (method == "POST" and parts == ["pages"]) else "新建请求的路径不符"
    tid = _nid(parts[1]) if len(parts) >= 2 else None
    if not tid or tid != _nid(row["target_id"]):
        return "请求目标与 journal 记录不符"
    tree = ctx.tree_ids.get(page, set())
    if op == "APPEND":
        ok = parts[0] == "blocks" and parts[2:] == ["children"] and (tid == page or tid in tree or tid in ctx.ours)
    elif op in ("UPDATE", "TRASH_BLOCK"):
        ok = parts[0] == "blocks" and len(parts) == 2 and tid in tree
    elif op in PAGE_OPS:
        ok = parts[0] == "pages" and len(parts) == 2 and tid == page
    else:
        ok = False
    return None if ok else "目标不属于该项的页面"


def _count_raw(raw):
    n = 0
    for b in raw or []:
        n += 1 + _count_raw(b.get("_children") or [])
    return n


# =============================================================================================== adoption (WB §11.4)
def _default_runner(paths, log_path):
    py = [sys.executable, "-I", "-B"]

    def run(script, args, timeout=None):
        argv = py + [os.path.join(TOOLS, script)] + [str(a) for a in args]
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\n$ " + " ".join(argv) + "\n")
            log.flush()
            try:
                r = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, cwd=TOOLS)
            except subprocess.TimeoutExpired:
                log.write(f"TIMEOUT after {timeout}s\n")
                return 124
        return r.returncode
    return run


def _snapshot_ids(path):
    con, _ = wb_common.open_snapshot_reader(path)
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, kind, parent_id FROM objects")}
    finally:
        con.close()


def _refetch_sets(ctx, written):
    """(page_ids, ds_ids, adopt entries, verify ids, parent page ids) for §11.4. Refetch = written pages + the
    parents of new, renamed and trashed pages + data sources of new rows."""
    pages, dss, adopt, ids, parents = set(), set(), {}, set(), set()

    def parent_of(par):
        if par.get("page_id"):
            pages.add(_nid(par["page_id"]))
            parents.add(_nid(par["page_id"]))
        if par.get("data_source_id"):
            dss.add(_nid(par["data_source_id"]))

    for iid in written:
        it = ctx.items[iid]
        cat = it.get("category")
        if cat == "N":
            pid = ctx.created.get(iid) or (_mapping(ctx.db, it.get("rel_path")) or {}).get("page_id")
            op = next((o for o in it.get("ops") or [] if _op_name(o) == "CREATE_PAGE"), {})
            parent_of(op.get("parent") or {})
            if pid:
                # the sha this executor itself wrote (frontmatter write-back), never the live file: a user edit
                # made after that write must make the publisher keep the file and write a conflict copy. When the
                # frontmatter write was skipped (file changed during the apply) the plan's L sha is used, which no
                # longer matches -> conflict copy + 待人工处理, the user's edit stays.
                m = _mapping(ctx.db, it.get("rel_path")) or {}
                sha = m.get("vault_sha") if iid not in ctx.fm_skipped and m.get("page_id") == pid else None
                adopt[pid] = {"rel": it.get("rel_path"), "l_sha": sha or it.get("l_sha")}
                ids.add(pid)
            continue
        nid = _nid(it.get("notion_id"))
        pages.add(nid)
        if cat == "D":
            row = ctx.db.execute("SELECT before_json FROM journal WHERE plan_id=? AND item_id=? AND op='TRASH_PAGE' "
                                 "ORDER BY seq DESC LIMIT 1", (ctx.plan_id, iid)).fetchone()
            parent_of(((_jload(row["before_json"], {}) or {}).get("parent") or {}) if row else {})
            adopt[nid] = {"rel": it.get("rel_path"), "l_sha": None, "trashed": True}
        else:
            if cat == "T":
                row = ctx.db.execute("SELECT after_json FROM journal WHERE plan_id=? AND item_id=? AND op='RENAME' "
                                     "AND state IN ('done','reconciled') ORDER BY seq DESC LIMIT 1",
                                     (ctx.plan_id, iid)).fetchone()
                parent_of(((_jload(row["after_json"], {}) or {}).get("parent") or {}) if row else {})
            adopt[nid] = {"rel": it.get("rel_path"), "l_sha": it.get("l_sha")}
        ids.add(nid)
    return pages, dss, adopt, ids, parents - set(adopt)


ADOPT_HARMLESS_OPS = ("UNCHANGED", "UNCHANGED_SEMANTIC", "SKIP_HELD", "LOCAL_ONLY")
# a parent note is republished like in any refresh round: replaced while unchanged locally (backup first), moved
# when it gained its first / lost its last child page (folder note <-> plain note), conflict copy otherwise
ADOPT_PARENT_OPS = ADOPT_HARMLESS_OPS + ("REPLACE", "LINK_REWRITE_ONLY", "LOCAL_KEYS_ONLY", "MOVE", "CONFLICT_BOTH",
                                         "CONFLICT_MOVED_EDITED", "CONFLICT_NOT_FILE", "CONFLICT_UNTRACKED")
ADOPT_GENERATED_OPS = ("UNCHANGED", "UNCHANGED_SEMANTIC", "GENERATED_OVERWRITE", "REPLACE", "ADD")


def _paused_rounds(paths):
    """Run ids of unreleased paused_for_review refresh rounds (state/paused_runs.json, status paused)."""
    pr = _read_json(os.path.join(paths.state, "paused_runs.json"), {}) or {}
    runs = pr.get("runs") if isinstance(pr, dict) else None
    return sorted(r for r, e in (runs or {}).items() if isinstance(e, dict) and e.get("status") == "paused")


def _unregistered_deletions(ctx, missing, exclude=()):
    """r1-contract-consistency-01: published notes deleted locally that no refresh has registered yet (scan
    'missing' without a pending_deletes row): {notion_id: [rel]}. A refresh round in mode >= plan would hold them
    (WB §6.0 b) instead of restoring them; the adoption publish and its verify treat them the same way."""
    out = {}
    ex = {_nid(x) for x in exclude}
    for m in missing or []:
        nid = _nid(m.get("notion_id"))
        rel = str(m.get("rel") or "")
        if not nid or not rel.endswith(".md") or nid in ex or m.get("pending") or m.get("expired"):
            continue
        if ctx.db.execute("SELECT 1 FROM pending_deletes WHERE notion_id=? AND COALESCE(status,'')!='restored'",
                          (nid,)).fetchone():
            continue
        out.setdefault(nid, []).append(rel)
    return out


def _adopt_holds(ctx, rep, adopt=()):
    """--holds for the adoption publish, from the same sources a refresh round uses: state/auto_sync_hold.json,
    pending_deletes on hold (a locally deleted note is never re-added) and BLOCKED / PARTIAL_CREATE pages; plus
    (L-18 d) the executed notes that are not adopted, note only, so the publish leaves their local file alone;
    plus (r1-contract-consistency-01) notes deleted locally that no refresh has registered yet, note only."""
    out = dict(_auto_holds(ctx.paths))
    try:
        import wb_scan
        scan = wb_scan.scan(ctx.paths.vault, ctx.paths.export_db, wb_state.db_file(ctx.db), ctx.cfg)
        for nid, rels in sorted(_unregistered_deletions(ctx, scan.get("missing"), exclude=adopt).items()):
            out.setdefault(nid, {"reason": "local_delete_unregistered", "source": "writeback", "subtree": False,
                                 "paths": sorted(rels)})
    except Exception as exc:  # noqa: BLE001 - without the scan the publish may re-add such a note (old behaviour)
        if isinstance(getattr(ctx, "notes", None), list):
            ctx.notes.append(f"库扫描失败（{exc.__class__.__name__}）：尚未登记的本地删除未加入采纳的 hold")
    for r in ctx.db.execute("SELECT DISTINCT notion_id FROM pending_deletes WHERE status='hold'"):
        if _nid(r["notion_id"]):
            out.setdefault(_nid(r["notion_id"]), {"reason": "pending_delete", "source": "pending_delete", "subtree": True})
    for r in ctx.db.execute("SELECT notion_id, state FROM page_state WHERE state IN ('BLOCKED','PARTIAL_CREATE')"):
        if _nid(r["notion_id"]):
            out.setdefault(_nid(r["notion_id"]), {"reason": f"page_state_{r['state']}", "source": "page_state",
                                                  "subtree": True})
    for h in (getattr(ctx, "adopt_holdback", None) or {}).values():
        if _nid(h.get("notion_id")):
            out.setdefault(_nid(h["notion_id"]), {"reason": "writeback_not_adopted", "source": "writeback",
                                                  "subtree": False})
    p = os.path.join(rep, "holds.json")
    _write_json(p, dict(sorted(out.items())))
    return p


def _foreign_publish_actions(pplan, adopt, parents):
    """Actions of an adoption publish plan that would write anything other than the adopted objects, the refetched
    parent notes (child links) and the tool-generated Notion_Obsidian/* index files; tombstones count too."""
    bad = []
    for a in pplan.get("actions", []) or []:
        nid = a.get("nid") or a.get("notion_id")
        op = a.get("op")
        if nid and nid in adopt:
            continue
        if op in ADOPT_HARMLESS_OPS:
            continue
        if nid and nid in parents and op in ADOPT_PARENT_OPS:
            continue
        if not nid and a.get("kind") == "generated" and str(a.get("rel") or "").startswith("Notion_Obsidian/") \
                and op in ADOPT_GENERATED_OPS:
            continue
        bad.append(a)
    bad += [{"op": "TOMBSTONE", "rel": r} for r in pplan.get("tombstones") or []]
    return bad


def _open_overrides(ctx, exclude=()):
    out = {}
    for r in ctx.db.execute("SELECT notion_id, old_rel, local_rel FROM name_overrides WHERE status='open' ORDER BY rowid"):
        if r["notion_id"] in exclude:
            continue
        out[r["notion_id"]] = {"old_rel": r["old_rel"], "local_rel": r["local_rel"]}
    return out


def _adopt_pipeline(ctx, written, *, runner=None, blobs_dir=None, sidebar=None, refetch=None):
    """WB §11.4: targeted refetch -> export -> staging verify -> publish --adopt-writeback --only-ids ->
    verify --ids (W6) -> W7/W8. Holds refresh.lock (the caller does). Never touches Notion beyond reads."""
    import wb_refetch
    paths = ctx.paths
    rep = os.path.join(paths.plan_dir(ctx.plan_id), "adopt" if ctx.label == "apply" else f"adopt-{ctx.label}")
    os.makedirs(rep, exist_ok=True)
    run = runner or _default_runner(paths, os.path.join(rep, "adopt.log"))
    out = {"ok": False, "steps": {}, "needs_manual": []}
    pages, dss, adopt, ids, parents = _refetch_sets(ctx, written)
    for h in (getattr(ctx, "adopt_holdback", None) or {}).values():
        if _nid(h.get("notion_id")):          # not adopted (L-18 d), but refetched: the snapshot shows Notion as it is
            pages.add(_nid(h["notion_id"]))
    out["adopt"] = adopt

    def fail(reason, w6=None):
        out["reason"] = reason
        out["needs_manual"] = sorted(adopt)
        for nid in adopt:
            ctx.db.execute("UPDATE plan_items SET verify_status='needs_manual' WHERE plan_id=? AND notion_id=?",
                           (ctx.plan_id, nid))
        out["W6"] = w6 or _w("fail", f"待人工处理：{reason}")
        out["W7"] = _w("fail", "未采纳：下一次 plan 仍可能把这些笔记列为修改")
        out["W8"] = _w("fail", "未采纳：映射未更新")
        return out

    try:
        base = wb_refetch.choose_base(state_dir=paths.state)
    except wb_refetch.RefetchError as exc:
        return fail(f"无可用基准快照（{exc}）：请运行 {cli_command('refresh', '--mode', 'incremental')}")
    known = _snapshot_ids(base)
    page_ids = sorted(p for p in pages if p in known)
    ds_ids = sorted(d for d in dss if d in known)
    os.makedirs(paths.snapshots_wb, exist_ok=True)
    out_path = os.path.join(paths.snapshots_wb, "snap_%s_%s.db" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"),
                                                                   secrets.token_hex(4)))
    try:
        ctx.check_stop()
        st = (refetch or wb_refetch.refetch)(base, out_path, page_ids, ds_ids, ctx.rc, blobs_dir=blobs_dir, workers=4)
    except wb_refetch.RefetchError as exc:
        return fail(f"定向重抓失败（{exc.__class__.__name__}: {exc}）：请运行 {cli_command('refresh', '--mode', 'incremental')}")
    out["steps"]["refetch"] = {"base": base, "out": st.get("out", out_path), "pages": page_ids, "data_sources": ds_ids}
    snap = st.get("out", out_path)
    roots_json = st.get("roots_json") or snap + ".roots.json"
    _archive_refetched(ctx, base, st)
    run_id = ctx.plan_id if ctx.label == "apply" else f"{ctx.plan_id}-{ctx.label}"
    stg = os.path.join(paths.staging, run_id)
    if os.path.exists(stg):
        run_id = f"{run_id}-{secrets.token_hex(2)}"
        stg = os.path.join(paths.staging, run_id)
    if sidebar is None:
        import refresh
        sidebar = refresh.sidebar_from_snapshot(snap, os.path.join(rep, "sidebar.json"))
    excl = {_nid(ctx.items[i].get("notion_id")) for i in written if ctx.items[i].get("category") == "T"}
    ovr = _open_overrides(ctx, exclude=excl)
    ovr_out = {k: v["local_rel"] for k, v in ovr.items()}
    t_dev = []
    for iid in written:            # a renamed note: its local path (ADOPT_RENAME when published under the old one)
        it = ctx.items[iid]
        if it.get("category") == "T" and it.get("rel_path"):
            ovr_out[_nid(it.get("notion_id"))] = it["rel_path"]
            t_dev.append({"notion_id": _nid(it.get("notion_id")), "old_rel": it.get("old_rel"),
                          "local_rel": it["rel_path"]})
    ovr_file = os.path.join(rep, "overrides.json")
    _write_json(ovr_file, dict(sorted(ovr_out.items())))
    rc = run("export_obsidian.py", ["--snapshot", snap, "--sidebar", sidebar, "--run-id", run_id, "--immutable",
                                    "--roots-kinds", roots_json, "--overrides", ovr_file, "--state", paths.export_db,
                                    "--out", stg], timeout=3600)
    out["steps"]["export"] = rc
    if rc != 0:
        return fail(f"导出失败（exit {rc}）")
    # r1-contract-consistency-08: the exporter's block_lines self-check gates the adoption exactly like a refresh
    # round (refresh pauses on it): its block_lines become the WRITEBACK base of the next three-way plan
    er = _read_json(os.path.join(stg, "export_report.json"), {}) or {}
    bl = (er.get("block_lines") or {}) if isinstance(er, dict) else {}
    bad_bl = {k: bl.get(k) for k in ("untagged_lines", "contiguity_violations", "own_noncontiguous")
              if isinstance(bl, dict) and bl.get(k)}
    if bad_bl:
        out["steps"]["export_block_lines_selfcheck"] = bad_bl
        return fail(f"导出器行映射自检失败（{bad_bl}）：不发布（refresh 遇到同样的问题也会暂停本轮）")
    stg_dev = os.path.join(rep, "staging_deviations.json")
    _write_json(stg_dev, {"schema": 1, "generated_by": "wb_apply", "name_overrides": [
        {"notion_id": k, "old_rel": v["old_rel"], "local_rel": v["local_rel"]} for k, v in sorted(ovr.items())] + t_dev})
    vout = os.path.join(rep, "staging_verify")
    rc = run("verify_vault.py", ["--snapshot", snap, "--sidebar", sidebar, "--vault", os.path.join(stg, "vault"),
                                 "--out", vout, "--roots-kinds", roots_json, "--snapshot-immutable",
                                 "--expected-deviations", stg_dev], timeout=3600)
    sv = _read_verify(vout)
    out["steps"]["staging_verify"] = {"rc": rc, "status": (sv or {}).get("_sbc")}
    if sv is None or any((sv["_sbc"].get(c) != "PASS") for c in ("C01", "C02", "C03", "C04", "C05", "C06", "C07",
                                                                     "C08", "C09", "C10")):
        return fail(f"暂存校验未通过（{(sv or {}).get('_sbc')}）：不发布")
    adopt_file = os.path.join(rep, "adopt.json")
    _write_json(adopt_file, adopt)
    holds_file = _adopt_holds(ctx, rep, adopt)
    # WB §11.4: publish normally (--adopt-writeback) unless an unreleased paused_for_review round exists; then only
    # the plan's notes (--only-ids). The normal publish is accepted only when it writes nothing but the adopted
    # objects, the refetched parents (child links) and the tool-generated index files; otherwise it falls back
    # to --only-ids (adopted objects + refetched parents) -- the index is then updated by the next refresh.
    paused = _paused_rounds(paths)
    modes = (["only_ids"] if paused else ["full", "only_ids_parents"])
    pplan = pp = pargs = None
    reasons = []
    for mode in modes:
        prep = os.path.join(rep, "publish" if mode == modes[0] else f"publish-{mode}")
        os.makedirs(prep, exist_ok=True)
        pargs = ["--run-id", run_id, "--vault", paths.vault, "--state", paths.export_db, "--report-dir", prep,
                 "--staging-dir", stg, "--backup-dir", os.path.join(paths.backup, "vault-before", run_id),
                 "--wb-db", wb_state.db_file(ctx.db), "--overrides", ovr_file, "--adopt-writeback", adopt_file,
                 "--holds", holds_file]
        only = None
        if mode != "full":
            only = set(adopt) | (set(parents) if mode == "only_ids_parents" else set())
            ids_file = os.path.join(rep, f"only_ids-{mode}.txt")
            wb_common.atomic_write(ids_file, ("".join(f"{i}\n" for i in sorted(only))).encode())
            pargs += ["--only-ids", ids_file]
        if os.path.realpath(paths.vault) != wb_common.PROD_VAULT:
            pargs.append("--allow-other-vault")
        rc = run("publish_vault.py", pargs, timeout=3600)
        out["steps"].setdefault("publish_plan", rc)
        pp = os.path.join(prep, "publish_plan.json")
        pplan = _read_json(pp)
        if rc != 0 or not isinstance(pplan, dict):
            return fail(f"发布计划失败（exit {rc}）")
        foreign = _foreign_publish_actions(pplan, adopt, parents)
        if not foreign:
            out["steps"]["publish_mode"] = mode
            break
        reasons.append(f"{mode}: {len(foreign)} 项计划外写入（如 {foreign[0].get('op')} {foreign[0].get('rel')}）")
        pplan = None
    if paused:
        ctx.notes.append(f"存在未放行的暂停轮次（{', '.join(paused[:3])}）：只采纳计划内笔记（--only-ids）")
    if pplan is None:
        return fail("发布计划含计划外对象的写入：不发布（" + "；".join(reasons) + "）")
    if out["steps"].get("publish_mode") == "only_ids_parents":
        ctx.notes.append("正常发布会改动计划外的对象，已改为只发布本次写回的笔记及其父笔记；目录索引等生成文件在下一次 "
                         "refresh 时更新（" + "；".join(reasons) + "）")
    approval = os.path.join(paths.approvals, f"{ctx.plan_id}.json")
    if not os.path.isfile(approval):
        approval = os.path.join(rep, "adoption.approval.json")
        _write_json(approval, {"plan_id": ctx.plan_id, "plan_sha256": ctx.plan_sha, "purpose": "adopt-writeback",
                               "at": wb_common.now_iso()})
    rc = run("publish_vault.py", pargs + ["--apply", "--approval", approval, "--plan-file", pp,
                                          "--plan-sha", pplan.get("plan_sha256") or ""], timeout=3600)
    out["steps"]["publish_apply"] = rc
    if rc in (0, 3, 4, 5):
        try:
            wb_refetch.record_adopted(snap, plan_id=ctx.plan_id, state_dir=paths.state)
        except wb_refetch.RefetchError as exc:
            ctx.notes.append(f"record_adopted 失败：{exc}")
    if rc not in (0, 3, 4, 5):
        return fail(f"发布被拒绝（exit {rc}）")
    nm = _read_json(os.path.join(prep, "adopt_needs_manual.json"), []) or []
    nm_ids = sorted({_nid(e.get("notion_id")) for e in nm if isinstance(e, dict) and _nid(e.get("notion_id"))})
    for nid in nm_ids:
        ctx.db.execute("UPDATE plan_items SET verify_status='needs_manual' WHERE plan_id=? AND notion_id=?",
                       (ctx.plan_id, nid))
    out["needs_manual"] = nm_ids
    # W6: post-publish verify (--ids scoped + full-vault C01/C02/C05) with deviations
    dev = _vault_deviations(ctx, rep, run_id, exclude=set(adopt))
    vids = os.path.join(rep, "verify_ids.txt")
    wb_common.atomic_write(vids, ("".join(f"{i}\n" for i in sorted(ids))).encode())
    pout = os.path.join(rep, "post_verify")
    prc = run("verify_vault.py", ["--snapshot", snap, "--sidebar", sidebar, "--vault", paths.vault, "--out", pout,
                                  "--roots-kinds", roots_json, "--snapshot-immutable", "--ids", vids,
                                  "--expected-deviations", dev], timeout=3600)
    pv = _read_verify(pout)
    out["steps"]["post_verify"] = {"rc": prc, "status": (pv or {}).get("_sbc")}
    w6_probs = []
    if rc != 0:
        w6_probs.append(f"publish exit {rc}")
    if nm_ids:
        w6_probs.append(f"待人工处理 {len(nm_ids)} 项（adopt_needs_manual）")
    if pv is None:
        w6_probs.append("post verify.json 缺失")
    else:
        bad = [c for c in VERIFY_SCOPED + VERIFY_FULL if pv["_sbc"].get(c) != "PASS"]
        if bad:
            w6_probs.append("校验未通过：" + ",".join(bad))
    sha_probs = _adopted_sha_check(ctx, stg, pplan, adopt, written)
    w6_probs += sha_probs
    out["W6"] = _w("fail" if w6_probs else "pass", "；".join(w6_probs) or "采纳后文件 sha = 暂存 sha；--ids 与全库校验通过")
    out["W7"], out["W8"] = _w7_w8(ctx, adopt, written)
    out["ok"] = out["W6"]["status"] == "pass"
    for nid in adopt:
        if nid not in nm_ids:
            ctx.db.execute("UPDATE plan_items SET verify_status=COALESCE(verify_status, 'verified') WHERE plan_id=? "
                           "AND notion_id=? AND category!='D'", (ctx.plan_id, nid))
    try:
        wb_refetch.prune_wb_snapshots(paths.snapshots_wb, state_dir=paths.state, keep=(snap,))
    except (OSError, ValueError):
        pass
    return out


def _archive_refetched(ctx, base, st):
    """H's request: archive trashed/pruned objects that still have content in the BASE (was_marker false), unless
    archived before the trash (§6.5)."""
    import trash_archive
    ids = [e.get("id") for e in (st.get("trashed") or []) + (st.get("pruned") or [])
           if isinstance(e, dict) and not e.get("was_marker")]
    ids = [i for i in ids if i and not ctx.db.execute("SELECT 1 FROM trash_archive WHERE notion_id=?", (i,)).fetchone()]
    if not ids:
        return
    try:
        con = trash_archive.open_snapshot(base)
    except Exception:  # noqa: BLE001
        return
    try:
        objs = trash_archive._objects(con)
        for i in ids:
            o = objs.get(i)
            if not o:
                continue
            blocks = trash_archive._blocks_of(con, i) if o["kind"] == "page" else []
            ctx.db.execute("INSERT INTO trash_archive(notion_id, archived_at, reason, last_snapshot, object_json, "
                           "blocks_json, inbound_refs_json) VALUES(?,?,?,?,?,?,?)",
                           (i, wb_common.now_iso(), f"targeted_refetch plan={ctx.plan_id}", os.path.abspath(base),
                            o["raw"], json.dumps(blocks, ensure_ascii=False), json.dumps({"mode": "skipped", "refs": []})))
    finally:
        con.close()


def _read_verify(out_dir):
    v = _read_json(os.path.join(out_dir, "verify.json"))
    if not isinstance(v, dict):
        return None
    sbc = v.get("status_by_check")
    if not sbc:
        raw = v.get("checks", {})
        sbc = {k: c.get("status") for k, c in raw.items()} if isinstance(raw, dict) else \
            {c["id"]: c["status"] for c in raw if isinstance(c, dict)}
    v["_sbc"] = dict(sbc or {})
    return v


def _vault_deviations(ctx, rep, run_id, exclude=()):
    """Expected deviations for the post-adoption verify: the last refresh round's file (if any) + a fresh read-only
    vault scan (refresh.build_expected_deviations), with the written objects removed (they must verify clean)."""
    import refresh
    import wb_scan
    paths = ctx.paths
    holds = _read_json(paths.auto_hold, {}) or {}
    ovr = _open_overrides(ctx)
    try:
        scan = wb_scan.scan(paths.vault, paths.export_db, wb_state.db_file(ctx.db), ctx.cfg)
        holds = dict(holds)
        for nid, rels in _unregistered_deletions(ctx, scan.get("missing"), exclude=exclude).items():
            holds.setdefault(nid, {"subtree": False, "paths": sorted(rels)})     # r1-contract-consistency-01
        dev = refresh.build_expected_deviations(run_id, scan, {}, holds, ovr)
        for entry in scan.get("undecodable_names", []) or []:
            rel = entry.get("rel") if isinstance(entry, dict) else None
            if isinstance(rel, str) and rel not in dev.get("local_new", []):
                dev.setdefault("local_new", []).append(rel)
    except Exception as exc:  # noqa: BLE001 - fall back to the refresh round's file only
        ctx.notes.append(f"库扫描失败（{exc.__class__.__name__}），只用上一轮 refresh 的预期偏差")
        dev = {"schema": 1, "run_id": run_id, "local_new": [], "hold_ids": [], "name_overrides": [],
               "sync_conflict_copies": [], "local_modified": [], "paused": []}
    lp = _read_json(os.path.join(paths.state, "last_published.json"), {}) or {}
    prev = _read_json(os.path.join(paths.work, "reports", lp.get("run_id") or "-", "expected_deviations.json"))
    if isinstance(prev, dict):
        for k in ("hold_ids", "paused", "local_new", "sync_conflict_copies", "local_modified", "name_overrides"):
            have = {wb_common.canon_json(x) for x in dev.get(k, [])}
            for x in prev.get(k) or []:
                if wb_common.canon_json(x) not in have:
                    dev.setdefault(k, []).append(x)
    ex = set(exclude)

    def keep(x):          # entries are dicts ({notion_id, path} / {notion_id, subtree, paths}) or bare ids
        if isinstance(x, dict):
            return x.get("notion_id") not in ex
        return not (isinstance(x, str) and x in ex)
    for k in ("hold_ids", "paused", "local_modified"):
        dev[k] = [x for x in dev.get(k, []) if keep(x)]
    dev["generated_by"] = "wb_apply"
    p = os.path.join(rep, "expected_deviations.json")
    _write_json(p, dev)
    return p


def _adopted_sha_check(ctx, stg, pplan, adopt, written):
    probs = []
    manifest = _read_json(os.path.join(stg, "manifest.json"), {}) or {}
    staged = {}
    if isinstance(manifest, dict):          # exporter manifest: {rel: {kind, notion_id, sha256, size}}
        for rel, e in manifest.items():
            if isinstance(e, dict) and e.get("sha256"):
                staged[rel] = e["sha256"]
    for a in pplan.get("actions", []):
        nid = a.get("nid") or a.get("notion_id")
        if nid in adopt and a.get("rel") and a.get("sha"):
            staged.setdefault(a["rel"], a["sha"])
        if nid in adopt and a.get("rel") and a.get("keys") and a.get("expect"):
            # the staged version with the note's local keys kept (LOCAL_KEYS_ONLY): the file the publisher wrote
            staged[a["rel"]] = a["expect"]
    # A rename and a body edit of the same note applied together (T + M): the adopted file carries the new title
    # (notion_title / aliases) in every item's diff, not only in the T item's own one.
    renamed = {_note_key(ctx.items[i]) for i in written if (ctx.items.get(i) or {}).get("category") == "T"}
    for iid in written:
        it = ctx.items[iid]
        if it.get("category") == "D":
            continue
        rel = it.get("rel_path")
        cur = _vault_sha(ctx, rel)
        s = staged.get(rel)
        if not s or cur != s:
            probs.append(f"{iid}：库内 sha ≠ 暂存 sha（{rel}）")
            continue
        lb = ctx.l_bytes.get(iid)
        normalized = it.get("normalize") or any((o or {}).get("normalized") for o in it.get("ops") or [])
        if lb is not None and not normalized:
            d = _diff_outside_ok_keys(lb, _read_bytes(_vault_file(ctx, rel)), body_only=it.get("category") == "N",
                                      rename=it.get("category") == "T" or _note_key(it) in renamed)
            if d:
                probs.append(f"{iid}：采纳后的笔记与写回前的本地版本有额外差异（{d[:2]}）")
    return probs


def _read_bytes(p):
    try:
        with open(p, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


NUMBERED_RE = re.compile(r"^(\s*)\d+\.(?=\s|$)")


def _diff_outside_ok_keys(a, b, body_only=False, rename=False):
    """Lines in which the adopted (staged) note differs from L beyond what W6 allows: notion_last_edited_time /
    notion_id, numbered-list renumbering (WB §4.4.1, the planner's oracle normalises it too), and -- for a rename
    (T) -- notion_title / aliases, which follow the new title."""
    def norm(x):
        try:
            text = x.decode("utf-8")
        except UnicodeDecodeError:
            return [repr(x)]
        if text.startswith("\ufeff"):      # r1-data-safety-02: L may carry a BOM the adoption drops
            text = text[1:]
        try:
            fm, body, _ = wb_common.split_note(text)
        except wb_common.FrontmatterError:
            return text.split("\n")
        lines, skip_block = [], False
        for ln in ([] if body_only else (fm or "").split("\n")):
            if skip_block and (ln.startswith((" ", "\t", "-")) or not ln.strip()):
                continue
            skip_block = False
            if ln.startswith(("notion_last_edited_time:", "notion_id:")):
                continue
            if rename and ln.startswith("notion_title:"):
                continue
            if rename and ln.startswith("aliases:"):
                skip_block = True
                continue
            lines.append(ln)
        return lines + ["---"] + [NUMBERED_RE.sub(r"\1#.", ln) for ln in body.strip("\n").split("\n")]
    la, lb = norm(a), norm(b)
    if la == lb:
        return []
    return [x for x in lb if x not in la][:5] or [x for x in la if x not in lb][:5]


def _w7_w8(ctx, adopt, written):
    """W7 (proxy: L == B for every written note, so the next plan yields no executable item) and W8."""
    p7, p8 = [], []
    for iid in written:
        it = ctx.items[iid]
        nid = _nid(it.get("notion_id")) or ctx.created.get(iid)
        if it.get("category") == "D":
            r = ctx.db.execute("SELECT status FROM pending_deletes WHERE notion_id=? ORDER BY rowid DESC LIMIT 1",
                               (nid,)).fetchone()
            if not r or r["status"] != "confirmed":
                p8.append(f"{iid}：pending_deletes 不是 confirmed")
            continue
        cur = _vault_sha(ctx, it.get("rel_path"))
        lb = wb_state.latest_base(ctx.db, nid) if nid else None
        if not lb or lb.get("origin") != "WRITEBACK":
            p8.append(f"{iid}：base_history 无 WRITEBACK 记录")
        pub = _published_row(ctx, it.get("rel_path"))
        if not pub or pub.get("status") != "active" or (pub.get("notion_id") and pub.get("notion_id") != nid):
            p8.append(f"{iid}：published_files 未登记")
        if not lb or cur is None or (pub and pub.get("sha256") != cur):
            p7.append(f"{iid}：库内文件与发布记录不一致（下一次 plan 可能再列出）")
        if it.get("category") == "T":
            if ctx.db.execute("SELECT 1 FROM name_overrides WHERE notion_id=? AND status='open'", (nid,)).fetchone():
                p8.append(f"{iid}：name_overrides 仍为 open")
        if it.get("category") == "N" and not (_mapping(ctx.db, it.get("rel_path")) or {}).get("page_id"):
            p8.append(f"{iid}：新页面映射缺失")
    p8 += _w8_objects_and_items(ctx, written)
    return (_w("fail" if p7 else "pass", "；".join(p7) or "已写回笔记 L = B（下一次 plan 不会再列出）"),
            _w("fail" if p8 else "pass", "；".join(p8) or "映射一致"))


def _w8_objects_and_items(ctx, written):
    """W8 (WB §12, r1-spec-coverage-11): objects_map and plan_items agree with what was written -- objects_map: the
    written object is active at the note's path (a deleted page: trashed_via_writeback); plan_items: every written
    item is `executed` (an N item carries its new page id) and the plan has no row outside plan.json."""
    probs = []
    p = ctx.paths.export_db
    rows = {}
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='objects_map'").fetchone():
                for iid in written:
                    it = ctx.items.get(iid) or {}
                    nid = _nid(it.get("notion_id")) or ctx.created.get(iid)
                    if nid:
                        r = con.execute("SELECT status, rel_path, published_path FROM objects_map WHERE notion_id=?",
                                        (nid,)).fetchone()
                        rows[iid] = dict(r) if r else None
            else:
                probs.append("export.db 没有 objects_map 表")
        finally:
            con.close()
    except sqlite3.Error as exc:
        return [f"objects_map 无法读取（{exc.__class__.__name__}）"]
    for iid, r in rows.items():
        it = ctx.items.get(iid) or {}
        if r is None:
            probs.append(f"{iid}：objects_map 无记录")
            continue
        if it.get("category") == "D":
            if r["status"] != "trashed_via_writeback":
                probs.append(f"{iid}：objects_map.status = {r['status']}，不是 trashed_via_writeback")
            continue
        if r["status"] in ("trashed_via_writeback", "source_missing"):
            probs.append(f"{iid}：objects_map.status = {r['status']}")
        where = r.get("published_path") or r.get("rel_path")
        if it.get("rel_path") and where and where != it.get("rel_path"):
            probs.append(f"{iid}：objects_map 路径 {where} ≠ {it.get('rel_path')}")
    planned = set(ctx.items)
    for r in ctx.db.execute("SELECT item_id, decision, notion_id, category FROM plan_items WHERE plan_id=?",
                            (ctx.plan_id,)):
        if r["item_id"] not in planned:
            probs.append(f"plan_items 有计划外的行 {r['item_id']}")
        elif r["item_id"] in written:
            if r["decision"] != "executed":
                probs.append(f"{r['item_id']}：plan_items.decision = {r['decision']}，不是 executed")
            if r["category"] == "N" and not _nid(r["notion_id"]):
                probs.append(f"{r['item_id']}：plan_items 没有新页面 id")
    seen = {r["item_id"] for r in ctx.db.execute("SELECT item_id FROM plan_items WHERE plan_id=?", (ctx.plan_id,))}
    for iid in written:
        if iid not in seen:
            probs.append(f"{iid}：plan_items 缺少该项")
    return probs


def _published_row(ctx, rel):
    p = ctx.paths.export_db
    if not rel or not os.path.isfile(p):
        return None
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM published_files WHERE rel_path=?", (rel,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return dict(r) if r else None


# =============================================================================================== outputs (WB §11.5, §5.1a)
def _code(s):
    s = str(s if s is not None else "")
    s = s.replace("\n", " ").replace("\r", " ")
    fence = "``" if "`" in s else "`"
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


STATUS_ZH = {"executed": "已写入", "skipped": "已跳过", "failed": "未生效", "blocked": "页面已停用（BLOCKED）",
             "unknown": "结果未知", "not_executed": "未执行", "cancelled": "未执行", "refused": "已拒绝"}


def _next_step(ctx, iid, r):
    """W10: every item that is not done gets a concrete next step (plain words + the command)."""
    st = r.get("status")
    it = ctx.items.get(iid) or {}
    if st == "executed":
        if iid in ctx.fm_skipped:
            return "新页面已建好，但本地笔记在执行中被改过、未写入 notion_id：对照 conflicts/ 中的 Notion 版本手工合并"
        if r.get("adoption") == "held_back":
            return ("已写入 Notion，但同一笔记还有未写回的改动，库内笔记保留本地版本、未采纳：下一次 refresh 会把 Notion "
                    f"版本放到 conflicts/ 供对照；处理后运行 `{cli_command('writeback', 'plan')}`（冲突项按新计划中的两条处理路径逐项核对）")
        return "无（下一轮 refresh 后可在 Notion 与库内验收）"
    if st in ("skipped", "not_executed", "cancelled", "refused"):
        return f"检查原因后运行 `{cli_command('writeback', 'plan')}` 重新生成计划，再按新计划确认"
    if st == "failed":
        return f"这项没有写入 Notion；处理原因后运行 `{cli_command('writeback', 'plan')}` 重新生成计划"
    if st == "unknown":
        seqs = [str(x["seq"]) for x in _j_rows(ctx.db, ctx.plan_id, ("unknown", "sent")) if x["item_id"] == iid]
        return ("；".join(wb_common.reconcile_hint(s) for s in seqs[:3]) if seqs else
                "先在 Notion 核对；在下方“结果未知的请求”找到对应编号及完整命令。")
    if st == "blocked":
        pid = _nid(it.get("notion_id")) or ctx.created.get(iid)
        if not pid:
            return f"请在 Notion 中核查页面，通过 `{cli_command('writeback', 'status')}` 查看已停用页面的编号和处理命令。"
        return (f"在 Notion 中检查该页面（必要时用 `{cli_command('writeback', 'rollback', '--plan', ctx.plan_id)}` 撤销），"
                f"确认无误后运行 `{cli_command('writeback', 'unblock', '--page', pid)}`")
    return f"运行 `{cli_command('writeback', 'status')}` 查看"


def _summary_lines(ctx, out):
    status_zh = {"applied": "完成，可验收", "partially_applied": "部分完成，待人工处理", "aborted": "已中止",
                 "refused": "已拒绝"}.get(out["status"], out["status"])
    L = [f"- 计划：{_code(ctx.plan_id)}（sha256 前 12 位 {_code(ctx.plan_sha[:12])}）",
         f"- 类型：{ctx.label}；结果：**{status_zh}**（{out['status']}）",
         f"- 时间：{wb_common.now_iso()}；写请求数：{out.get('requests', 0)}"]
    if out.get("reason"):
        L.append(f"- 原因：{out['reason']}")
    L += ["", "## 逐项结果", "", "| 项目 | 类型 | 本地路径 | Notion | 结果 | 原因 | 下一步 |", "|---|---|---|---|---|---|---|"]
    for iid in ctx.order:
        r = ctx.item_res.get(iid)
        if not r:
            continue
        it = ctx.items[iid]
        why = (r.get("reason") or "").replace("|", "／").replace("\n", " ")
        if r.get("conflict"):
            why = f"{r['conflict']}：{why}"
        nid = _nid(it.get("notion_id")) or ctx.created.get(iid)
        link = f"[打开](https://app.notion.com/p/{nid.replace('-', '')})" if nid else "—"
        label = STATUS_ZH.get(r["status"], r["status"])
        nxt = _next_step(ctx, iid, r).replace("|", "／")
        L.append(f"| {iid} | {it.get('category')} | {_code(it.get('rel_path'))} | {link} | {label}（{r['status']}） | "
                 f"{why[:300]} | {nxt} |")
    L += ["", "## 写后一致性检查（W1–W10）", "", "| 检查 | 结果 | 说明 |", "|---|---|---|"]
    for k, v in (out.get("w_checks") or {}).items():
        L.append(f"| {k} | {v['status']} | {str(v.get('detail') or '').replace('|', '／')[:300]} |")
    if out.get("blocked_pages"):
        L += ["", "## BLOCKED 页面（不自动重试、不补偿）", ""]
        L += [f"- {_code(p)}：检查 Notion 后运行 {_code(cli_command('writeback', 'unblock', '--page', p))}" for p in out["blocked_pages"]]
    unk = [r for r in _j_rows(ctx.db, ctx.plan_id, ("unknown", "sent"))]
    if unk:
        L += ["", "## 结果未知的请求（需人工核对）", ""]
        L += [f"- journal {r['seq']}：{r['op']} {_code(r['target_id'])} → {wb_common.reconcile_hint(r['seq'])}"
              for r in unk]
    ad = out.get("adoption") or {}
    if ad:
        L += ["", "## 库内采纳", "", f"- 结果：{'成功' if ad.get('ok') else '未完成'}" +
              (f"；原因：{ad.get('reason')}" if ad.get("reason") else "")]
        for nid in ad.get("needs_manual") or []:
            L.append(f"- 待人工处理：{_code(nid)}")
        for h in ad.get("held_back") or []:
            L.append(f"- 待人工处理（未采纳，保留本地版本 LOCAL_ONLY）：{_code(h.get('rel_path'))}"
                     f"（{', '.join(h.get('items') or [])}）原因：{str(h.get('reason') or '').replace(chr(10), ' ')}")
    if out["status"] != "refused" and out.get("executed"):
        L += ["", "## 回退", "", f"- 需要撤销时运行：{_code(cli_command('writeback', 'rollback', '--plan', ctx.plan_id))}"
                                    "（交互终端 + 挑战码；自写入后又被编辑的内容会跳过）"]
    for n in out.get("notes") or []:
        L.append(f"- 备注：{n}")
    return L


def _write_outputs(ctx, out):
    pdir = ctx.paths.plan_dir(ctx.plan_id)
    os.makedirs(pdir, exist_ok=True)
    name = "result.md" if ctx.label == "apply" else f"result-{ctx.label}.md"
    body = [f"# 写回执行结果 {ctx.plan_id}", ""] + _summary_lines(ctx, out)
    rp = os.path.join(pdir, name)
    wb_common.atomic_write(rp, ("\n".join(body).rstrip("\n") + "\n").encode("utf-8"))
    _write_json(os.path.join(pdir, name.replace(".md", ".json")),
                {k: v for k, v in out.items() if k not in ("result_md", "vault_record")})
    vault_rel = None
    if os.path.isdir(ctx.paths.vault):
        d = os.path.join(ctx.paths.vault, "Notion_Obsidian", "运行记录")
        os.makedirs(d, exist_ok=True)
        stem = f"写回-{ctx.plan_id}" + ("" if ctx.label == "apply" else f"-{ctx.label}")
        cand, n = f"{stem}.md", 1
        while os.path.lexists(os.path.join(d, cand)):   # ADD-only, never overwrite (5.1a)
            n += 1
            cand = f"{stem}-{n}.md"
        fm = ["---", "notion_kind: generated", "generated_by: writeback.py", f"plan_id: {ctx.plan_id}", "---"]
        text = "\n".join(fm + [f"# 写回 {ctx.plan_id}", ""] + _summary_lines(ctx, out))
        gen = sum(len(os.listdir(os.path.join(ctx.paths.vault, "Notion_Obsidian", x)))
                  for x in ("运行记录", "写回预览") if os.path.isdir(os.path.join(ctx.paths.vault, "Notion_Obsidian", x)))
        if gen > 100:
            text += f"\n\n- 提示：运行记录与写回预览文件已有 {gen} 个，可以手工清理旧文件。"
        text = text.replace("[[", "［［").replace("]]", "］］")
        p = os.path.join(d, cand)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as fh:
            fh.write((text.rstrip("\n") + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        vault_rel = os.path.relpath(p, ctx.paths.vault).replace(os.sep, "/")
    return rp, vault_rel


# =============================================================================================== resume
def resume_plan(plan_id, confirmation_record, *, client, read_client, cfg, db, now=None, work=None, vault=None,
                cfg_path=None, adopter=None, runner=None, blobs_dir=None, sidebar=None):
    """`apply --resume`: only for a plan in `applying` (a crash); only journal rows in state `intent` are sent
    (after a fresh precheck); `sent` rows of the crashed run are unknown (BLOCKED, never resent)."""
    paths = Paths(work, vault)
    if cfg_path is None and os.path.isfile(paths.config):
        cfg_path = paths.config
    res = _empty_result(plan_id)
    try:
        with wb_locks.lock_all(["writeback", "refresh"], state_dir=paths.state):
            try:
                return _resume_locked(plan_id, confirmation_record, client=client, read_client=read_client, cfg=cfg,
                                      db=db, now=now, paths=paths, cfg_path=cfg_path, adopter=adopter, runner=runner,
                                      blobs_dir=blobs_dir, sidebar=sidebar)
            except Exception as exc:  # noqa: BLE001 - a defect before the resumed run started: nothing was sent
                out = dict(res, status="refused", error=f"{exc.__class__.__name__}: {exc}"[:300])
                out["reason"] = (f"续执行前检查出现程序错误（{exc.__class__.__name__}）：没有发送任何写请求；计划仍为 "
                                 "applying，请保留输出并联系维护者")
                return out
    except wb_locks.LockBusy as exc:
        return dict(res, reason=str(exc))


def _resume_locked(plan_id, confirmation_record, *, client, read_client, cfg, db, now, paths, cfg_path, adopter, runner,
                   blobs_dir, sidebar):
    res = _empty_result(plan_id)
    try:
        _check_token(client)
        plan, sha, row = load_plan_verified(plan_id, work=paths.work, db=db)
        vp = vault_problem(paths, plan)
        if vp:
            raise ApplyRefused(vp)
    except ApplyRefused as exc:
        return dict(res, reason=exc.reason)
    if row.get("status") != "applying":
        return dict(res, reason=f"只有 applying（中断）的计划可以 --resume；当前状态 {row.get('status')}")
    rows = _j_rows(db, plan_id)
    fwd = [r for r in rows if not r["op"].startswith("RB_") and r["op"] != "VAULT_FRONTMATTER"]
    item_ids = []
    for r in fwd:
        if r["item_id"] not in item_ids:
            item_ids.append(r["item_id"])
    todo = sorted({r["item_id"] for r in fwd if r["state"] == "intent"})
    ctx = Ctx(plan_id=plan_id, plan=plan, plan_sha=sha, plan_row=row, client=client, read_client=read_client,
              cfg=cfg, cfg_path=cfg_path, db=db, paths=paths, now=now, label="resume")
    # every item the human confirmed for this plan sha -- also those the crashed apply skipped at precheck
    # (they have no journal rows): W10 and the result must list them, the plan is never "applied" without them
    confirmed = [r["item_id"] for r in db.execute(
        "SELECT DISTINCT item_id FROM confirmations WHERE plan_id=? AND plan_sha256=? AND phrase_scope='apply' "
        "AND challenge_ok=1", (plan_id, sha))]
    ctx.selected = set(item_ids) | {i for i in confirmed if i in ctx.items}
    _seed_unjournaled(ctx, [i for i in ctx.order if i in ctx.selected and i not in item_ids])
    try:
        _start_checks(ctx, todo, confirmation_record, "resume", None)
    except (ApplyRefused, _StopRun) as exc:
        reason = getattr(exc, "reason", str(exc))
        if isinstance(exc, ApplyRefused) and exc.abort and "过期" in reason:
            # an expired plan can never be resumed: close it (its unsent intents are cancelled)
            for r in fwd:
                if r["state"] == "intent":
                    _j_update(ctx, r["seq"], state="cancelled", committed_ids={"outcome": "cancelled",
                                                                                 "message": reason})
            wb_state.cas_plan_status(db, plan_id, "applying", "aborted", reason)
        return dict(res, reason=reason)
    prev_hook = _install_hook(ctx)
    try:
        return _resume_run(ctx, fwd, item_ids, adopter=adopter, runner=runner, blobs_dir=blobs_dir,
                           sidebar=sidebar)
    except Exception as exc:  # noqa: BLE001 - never leave the plan in `applying` without a result
        return _crash_finalize(ctx, exc)
    finally:
        ctx.client.on_send = prev_hook


def _seed_unjournaled(ctx, iids):
    """Confirmed items without journal rows (skipped / failed at the crashed apply's precheck): their recorded
    decision becomes their result in this resume (never executed now, never silently dropped)."""
    for iid in iids:
        r = ctx.db.execute("SELECT decision, reason FROM plan_items WHERE plan_id=? AND item_id=?",
                           (ctx.plan_id, iid)).fetchone()
        dec = (r["decision"] if r else None) or "confirmed"
        st = {"skipped_precheck": "skipped", "failed": "failed", "unknown": "unknown", "executed": "failed"}.get(
            dec, "not_executed")
        reason = (r["reason"] if r else None) or "中断前的执行没有为此项写入 journal（预检未通过或未开始）"
        if dec == "executed":
            reason = "plan_items 记为已执行但 journal 中没有记录（需人工核对）"
        ctx.item_res[iid] = {"item_id": iid, "status": st, "reason": reason,
                             **({"conflict": "CONFLICT_BOTH"} if st == "skipped" else {})}


def _resume_run(ctx, rows, item_ids, *, adopter, runner, blobs_dir, sidebar):
    # crashed 'sent' rows: outcome unknown, never resent
    by_page = {}
    groups = []
    for iid in item_ids:
        it = ctx.items.get(iid)
        if it is None:
            continue
        key = ("new:" + iid) if it.get("category") == "N" else _nid(it.get("notion_id"))
        g = by_page.get(key)
        if g is None:
            g = Group(key, None if it.get("category") == "N" else key, "new" if it.get("category") == "N" else "page")
            by_page[key] = g
            groups.append(g)
        g.items.append(it)
        g.ok_items.append(it)
    for r in rows:
        it = ctx.items.get(r["item_id"]) or {}
        key = ("new:" + r["item_id"]) if it.get("category") == "N" else _nid(it.get("notion_id"))
        g = by_page.get(key)
        if g is None:
            continue
        g.steps.append({"seq": r["seq"]})
        if r["state"] in ("done", "reconciled", "sent", "unknown"):
            g.written = True
        if r["op"] == "CREATE_PAGE" and r["state"] in ("done", "reconciled"):
            ids = (_jload(r["committed_ids"], {}) or {}).get("ids") or []
            if ids:
                g.new_page = _nid(ids[0])
                ctx.created[r["item_id"]] = g.new_page
                ctx.ours.add(g.new_page)
        if r["op"] == "APPEND" and r["state"] in ("done", "reconciled"):
            ctx.ours.update(_nid(i) for i in (_jload(r["committed_ids"], {}) or {}).get("ids") or [])
    out_groups = []
    for g in groups:
        rs = [_j_row(ctx.db, s["seq"]) for s in g.steps]
        if any(r["state"] == "sent" for r in rs):
            for r in rs:
                if r["state"] == "sent":
                    _j_update(ctx, r["seq"], state="unknown",
                              committed_ids={"outcome": "unknown", "code": "crash_after_send",
                                             "message": "进程在发送后中断：结果未知，不重发"})
            _finish_group(ctx, g, _PageStop("中断前已发出的请求结果未知（不重发，需 reconcile）", blocked=True,
                                            partial_create=g.kind == "new" and bool(g.new_page)))
            continue
        if any(r["state"] in ("unknown", "failed", "cancelled") for r in rs) or \
                (g.page_id and _page_state(ctx.db, g.page_id)[0] in ("BLOCKED", "PARTIAL_CREATE")):
            _finish_group(ctx, g, _PageStop("中断前该页已停止（失败、取消或结果未知的请求）", blocked=g.written))
            continue
        if not any(r["state"] == "intent" for r in rs):
            g.snap = _resume_snap(ctx, g)
            _finish_group(ctx, g)
            continue
        try:
            g.snap = _resume_snap(ctx, g)
            _resume_precheck(ctx, g)
        except _PageStop as exc:
            _finish_group(ctx, g, exc)
            continue
        except NotionAPIError as exc:
            _finish_group(ctx, g, _PageStop(f"预检读取失败（{exc.status}）", blocked=g.written))
            continue
        out_groups.append(g)
    return _run(ctx, None, adopter=adopter, runner=runner, blobs_dir=blobs_dir, sidebar=sidebar,
                resume_groups=out_groups)


def _resume_snap(ctx, g):
    if g.kind == "page":
        page = ctx.get_page(g.page_id)
        # the W2 baseline is the tree before the remaining writes, minus nothing: blocks we already changed are
        # excluded through the journal (touched targets / ours)
        tree = _page_tree(ctx, g.page_id)
        ctx.tree_ids[g.page_id] = set(tree["by_id"])
        return {"page": page, "tree": tree}
    return {"page": None, "tree": None}


def _resume_precheck(ctx, g):
    """Fresh precheck of the remaining intent rows (time passed since the crash). An item none of whose requests
    was sent gets the FULL item precheck of a fresh apply (WB §11.3.1, and §6.4 for page deletes: vault file and
    subtree still absent, no file with that notion_id, pending_deletes still hold, page JSON / last_edited /
    subtree unchanged); a partially executed item gets op-level checks of its remaining rows."""
    if g.kind == "page":
        page = g.snap["page"]
        if page.get("in_trash") or page.get("is_locked") or not _in_scope_page(ctx, page):
            raise _PageStop("页面已锁定、在回收站或不在范围内", blocked=g.written)
    by_item = {}
    for s in g.steps:
        r = _j_row(ctx.db, s["seq"])
        if r:
            by_item.setdefault(r["item_id"], []).append(r)
    for iid, rs in by_item.items():
        if not any(r["state"] == "intent" for r in rs):
            continue
        it = ctx.items[iid]
        if all(r["state"] == "intent" for r in rs):
            try:
                _precheck_item(ctx, g, it)
            except _PageStop as exc:
                raise _PageStop(exc.reason, blocked=g.written) from None
            continue
        _resume_precheck_rows(ctx, g, it, [r for r in rs if r["state"] == "intent"])


def _resume_precheck_rows(ctx, g, it, rows):
    try:                    # L-18 (a)/(c) for a partially executed item too: held / no longer a leaf -> stop
        _hold_check(ctx, it, (g.snap or {}).get("page"))
        if g.kind == "page":
            _leaf_check(ctx, g, it)
    except _PageStop as exc:
        raise _PageStop(exc.reason, blocked=g.written) from None
    if it.get("category") in ("M", "T", "N", "B") and not (it.get("category") == "N" and g.new_page):
        sha = _vault_sha(ctx, it.get("rel_path"))
        if not it.get("l_sha") or sha != it["l_sha"]:
            raise _PageStop("库内文件在计划后又被修改", blocked=g.written)
        ctx.l_bytes[it["item_id"]] = _read_bytes(_vault_file(ctx, it.get("rel_path")))
    if it.get("category") == "D":
        _precheck_delete_vault(ctx, it)
    for r in rows:
        if r["op"] in ("UPDATE", "TRASH_BLOCK"):
            live = g.snap["tree"]["by_id"].get(r["target_id"])
            if live is None or wb_norm.block_json_sha(live) != wb_norm.block_json_sha(_jload(r["before_json"], {})):
                raise _PageStop(f"块 {r['target_id'][:8]} 自中断后已改变", blocked=g.written)
            if _x_reason(live):
                raise _PageStop(f"块 {r['target_id'][:8]} 是只读（X）块", blocked=g.written)
        elif r["op"] == "APPEND" and r["target_id"] and not isinstance((_jload(r["request_json"], {}) or {}).get("parent_id"), dict):
            req = _jload(r["request_json"], {})
            if isinstance(req.get("position"), dict) and "after_seq" in req["position"]:
                continue
            want = [i for i in (_jload(r["before_json"], {}) or {}).get("child_ids") or []]
            cur = [c for c in g.snap["tree"]["kids"].get(r["target_id"], []) if c not in ctx.ours]
            if cur != want:
                raise _PageStop("APPEND 父块的子块自中断后已改变", blocked=g.written)
        elif r["op"] in PAGE_OPS:
            if r["target_id"] != g.page_id:
                raise _PageStop("计划缺陷：页面操作的目标不是本项的页面", blocked=g.written)
            if r["op"] == "TRASH_PAGE":
                _precheck_trash_page(ctx, g, {})
            if _simple_applied(ctx, r) is not False:
                raise _PageStop(f"{r['op']}：目标状态自中断后已改变", blocked=g.written)
        elif r["op"] == "CREATE_PAGE":
            req = _jload(r["request_json"], {})
            before = _jload(r["before_json"], {}) or {}
            if _mapping(ctx.db, it.get("rel_path")):
                raise _PageStop("映射中已有该本地路径", blocked=False)
            if [c for c in _bot_pages_since(ctx, before.get("parent") or {}, ctx.plan_created, req.get("title"))
                    if c not in set(before.get("known") or [])]:
                raise _PageStop("父级下已有写回集成新建的同名页面", blocked=False)


# =============================================================================================== reconcile
def reconcile_journal(seq, decision, *, read_client, cfg, db, work=None, vault=None, bot_id=None):
    """`writeback.py reconcile --journal <seq>` (decision after the user looked at Notion). A live GET must
    agree with the decision, otherwise nothing is recorded. The page stays BLOCKED (unblock is separate).
    APPEND: only blocks created by the write-back bot after the request, not present before it and not committed
    by another row of the plan count (after_json = exactly those blocks: rollback trashes nothing else).
    CREATE_PAGE committed: the created_page mapping, PARTIAL_CREATE and plan_items.notion_id are written (and the
    note's notion_id frontmatter, sha-guarded, when `vault` is given); not_committed clears an 'unknown' mapping."""
    if decision not in ("committed", "not_committed"):
        return {"ok": False, "reason": "decision 必须是 committed 或 not_committed"}
    paths = Paths(work, vault)
    if os.path.exists(paths.stop):
        return {"ok": False, "reason": "急停文件存在"}
    if not isinstance(read_client, ReadOnlyClient):
        return {"ok": False, "reason": "读取客户端必须是 ReadOnlyClient"}
    try:
        # r1-data-safety-03: a committed CREATE_PAGE rewrites the note's frontmatter and bookkeeping in writeback.db:
        # mutually exclusive with a refresh round (state/refresh.lock), like apply / resume / rollback
        with wb_locks.lock_all(["writeback", "refresh"], state_dir=paths.state):
            row = _j_row(db, seq)
            if row is None:
                return {"ok": False, "reason": f"journal {seq} 不存在"}
            if row["state"] not in ("unknown", "sent"):
                return {"ok": False, "reason": f"journal {seq} 的状态是 {row['state']}，不需要核对"}
            bot = _nid(bot_id) if bot_id else _nid(wb_state.get_meta(db, BOT_META) or "")

            class _R:
                rc = read_client

                def check_stop(self):
                    if os.path.exists(paths.stop):
                        raise _StopRun("急停文件存在")

                get_page = Ctx.get_page
                get_block = Ctx.get_block
                children = Ctx.children
                prop_items = Ctx.prop_items
                query = Ctx.query

            r = _R()
            try:
                verdict, ids, live = _reconcile_probe(r, row, db=db, bot=bot)
            except NotionAPIError as exc:
                return {"ok": False, "reason": f"读取失败（{exc.status} {exc.code}）"}
            except _StopRun as exc:
                return {"ok": False, "reason": exc.reason}
            if verdict == "ambiguous" or (decision == "committed") != (verdict == "committed"):
                return {"ok": False, "reason": f"实时读取结果为 {verdict}，与人工判定 {decision} 不一致：未记录",
                        "live": verdict}
            info = dict(_jload(row["committed_ids"], {}) or {})
            info.update(reconciled=f"manual:{decision}", ids=ids if decision == "committed" else [],
                        at=wb_common.now_iso())
            with wb_state.transaction(db):
                db.execute("UPDATE journal SET state='reconciled', committed_ids=?, after_json=?, ts=? WHERE seq=? "
                           "AND state=?", (_jdump(info), _jdump(live), wb_common.now_iso(), seq, row["state"]))
            if decision == "committed" and row["op"] in ("APPEND", "CREATE_PAGE") and ids:
                with wb_state.transaction(db):
                    db.execute("INSERT INTO block_budget(ts, plan_id, item_id, created_blocks) VALUES(?,?,?,?)",
                               (wb_common.now_iso(), row["plan_id"], row["item_id"],
                                md2blocks.block_count((_jload(row["request_json"], {}).get("children")
                                                       or (_jload(row["request_json"], {}).get("body") or {}).get("children")
                                                       or [])) + (1 if row["op"] == "CREATE_PAGE" else 0)))
            out = {"ok": True, "seq": seq, "decision": decision, "ids": ids, "op": row["op"],
                   "note": "页面仍为 BLOCKED；请在状态输出中查该页的编号和解除命令，核对无误后再解除"}
            if row["op"] == "CREATE_PAGE":
                out["note"] = _reconcile_create_effects(db, paths, row, decision, ids, vault is not None)
            return out
    except wb_locks.LockBusy as exc:
        return {"ok": False, "reason": str(exc)}


def _reconcile_create_effects(db, paths, row, decision, ids, has_vault):
    """WB §5.3.4 for a create settled by reconcile: same bookkeeping as a create identified during the apply."""
    pi = db.execute("SELECT * FROM plan_items WHERE plan_id=? AND item_id=?", (row["plan_id"], row["item_id"])).fetchone()
    it = {"item_id": row["item_id"], "rel_path": pi["rel_path"] if pi else None, "l_sha": pi["l_sha"] if pi else None}
    lite = _LiteCtx(db, paths, row["plan_id"])
    rel = it["rel_path"]
    m = _mapping(db, rel) if rel else None
    if decision == "not_committed":
        if m and not m.get("page_id") and m.get("state") == "unknown":
            wb_state.set_meta(db, MAPPING_PREFIX + rel, None)      # the note may be planned (created) again
        return f"已记为未生效：该笔记可以在下一次 {cli_command('writeback', 'plan')} 中重新列为新增"
    pid = _nid(ids[0])
    if not rel:
        return f"新页面 {pid} 已登记为已生效，但计划项没有本地路径：请人工处理"
    _write_mapping(lite, it, pid, "partial", journal_seq=row["seq"])
    _set_page_state(lite, pid, "PARTIAL_CREATE", "reconcile：新建请求已确认生效（未继续填充内容）")
    fm = None
    if has_vault and it.get("l_sha"):
        fm = _frontmatter_writeback(lite, it, pid)
    tail = ("库内笔记已写入 notion_id" if fm else
            "库内笔记未写入 notion_id（" + ("；".join(lite.notes) if lite.notes else "未提供库路径") +
            "）：请在笔记 frontmatter 中加入 notion_id: " + pid + "，或等下一次 refresh 处理冲突副本")
    return (f"新页面 {pid} 已登记（PARTIAL_CREATE，映射已写入）；{tail}。在 Notion 中检查后运行 "
            f"{cli_command('writeback', 'unblock', '--page', pid)}")


class _LiteCtx:
    """The parts of Ctx the mapping / page_state / frontmatter helpers use (reconcile has no apply run)."""

    def __init__(self, db, paths, plan_id):
        self.db, self.paths, self.plan_id = db, paths, plan_id
        self.notes, self.fm_skipped = [], set()


def _plan_committed_ids(db, plan_id, exclude_seq=None):
    out = set()
    for x in _j_rows(db, plan_id, ("done", "reconciled")):
        if x["seq"] == exclude_seq or x["op"] not in ("APPEND", "CREATE_PAGE"):
            continue
        info = _jload(x["committed_ids"], {}) or {}
        if str(info.get("reconciled", "")).endswith("not_committed"):
            continue
        out.update(_nid(i) for i in info.get("ids") or [] if _nid(i))
    return out


def _reconcile_probe(r, row, db=None, bot=None):
    """('committed'|'not_committed'|'ambiguous', committed ids, live json)."""
    op = row["op"]
    req = _jload(row["request_json"], {}) or {}
    exp = _jload(row["expected_after_json"])
    tid = row["target_id"]
    if op == "UPDATE":
        live = r.get_block(tid)
        if md2blocks.canon_writable(live) == exp:
            return "committed", [tid], live
        if wb_norm.block_json_sha(live) == wb_norm.block_json_sha(_jload(row["before_json"], {})):
            return "not_committed", [], live
        return "ambiguous", [], live
    if op in ("TRASH_BLOCK",):
        live = r.get_block(tid)
        return ("committed" if live.get("in_trash") else "not_committed"), [tid], live
    if op in ("TRASH_PAGE",):
        live = r.get_page(tid)
        return ("committed" if live.get("in_trash") else "not_committed"), [tid], live
    if op in ("UPDATE_PROPERTIES", "RENAME"):
        live = r.get_page(tid)
        if op == "RENAME":
            tp = (_jload(row["before_json"], {}) or {}).get("title_property") or _title_prop(live)
            cur = md2blocks.canon_rich_text(((live.get("properties") or {}).get(tp) or {}).get("title") or [])
            old = md2blocks.canon_rich_text((_jload(row["before_json"], {}) or {}).get("title") or [])
            v = "committed" if cur == exp.get("title") else ("not_committed" if cur == old else "ambiguous")
            return v, [tid], live
        cur = {n: _prop_canon((live.get("properties") or {}).get(n)) for n in exp}
        before = _jload(row["before_json"], {}) or {}
        if cur == exp:
            return "committed", [tid], live
        if cur == {n: _prop_canon(before.get(n)) for n in exp}:
            return "not_committed", [], live
        return "ambiguous", [], live
    if op == "APPEND":
        bj = _jload(row["before_json"], {}) or {}
        before_ids = set(bj.get("child_ids") or [])
        earlier = _plan_committed_ids(db, row["plan_id"], exclude_seq=row["seq"]) if db is not None else set()
        at = _minute(_dt(bj.get("at")))
        cur = r.children(tid)
        new = [b for b in cur if _nid(b.get("id")) not in before_ids and _nid(b.get("id")) not in earlier]
        want = [(_block_type(c) or next(k for k in c if k not in ("object", "children"))) for c in req.get("children") or []]
        if not new:
            return "not_committed", [], None
        if not bot:
            return "ambiguous", [], None            # who created them cannot be proven
        for b in new:
            ct = _minute(_dt(b.get("created_time")))
            if _nid((b.get("created_by") or {}).get("id")) != bot or (at is not None and ct is not None and ct < at):
                return "ambiguous", [], None
        if [_block_type(b) for b in new] == want and \
                [md2blocks.canon_writable(b) for b in new] == [x["w"] for x in (exp or [])]:
            ids = [_nid(b.get("id")) for b in new]
            _got, raw = _live_tree(r, ids)
            return "committed", ids, raw
        return "ambiguous", [], None
    if op == "CREATE_PAGE":
        before = _jload(row["before_json"], {}) or {}
        parent = before.get("parent") or {}
        known = set(before.get("known") or [])
        title = req.get("title")
        cands = []
        if parent.get("page_id"):
            for b in r.children(_nid(parent["page_id"])):
                if _block_type(b) == "child_page" and (b.get("child_page") or {}).get("title") == title \
                        and _nid(b.get("id")) not in known:
                    cands.append(b)
        else:
            for p in r.query(_nid(parent.get("data_source_id"))):
                if _page_title(p) == title:
                    cands.append(p)
        if not cands:
            return "not_committed", [], None
        if not bot or any(_nid((c.get("created_by") or {}).get("id")) != bot for c in cands):
            return "ambiguous", [], None
        if len(cands) == 1:
            pid = _nid(cands[0].get("id"))
            page = r.get_page(pid)
            _got, raw = _live_tree(r, [_nid(b.get("id")) for b in r.children(pid)])
            return "committed", [pid], {"page": page, "blocks": raw}      # the read-back shape rollback compares
        return "ambiguous", [], None
    return "ambiguous", [], None


# =============================================================================================== W4 next round (WB §6.6)
def verify_pending_deletes(db, *, read_client, work=None, vault=None, nid_index=None, now=None):
    """WB §6.6 / R-183, the next-round half of W4 (r1-spec-coverage-01, r1-user-journey-06). For every executed D item
    still `pending_next_round` whose TRASH_PAGE write is older than the last REFRESH publish (state/last_published.json
    "at"; an adoption is not a refresh round), check: Notion GET in_trash (the page; each descendant itself or via its
    ancestor chain), no vault file with any of those ids (`nid_index`: notion_id -> [rel] of a fresh read-only scan),
    published_files + objects_map trashed_via_writeback, pending_deletes confirmed, the parent note republished without
    a link to the page, a trash_archive row. All hold -> verified; a definite failure -> needs_manual (reason + the
    rollback command in `status`); a transient read error -> stays pending (checked again at the next plan). Read-only
    towards Notion (ReadOnlyClient). Returns [{plan_id, item_id, rel_path, status, reasons}]."""
    if not isinstance(read_client, ReadOnlyClient):
        raise ApplyRefused("读取客户端必须是 ReadOnlyClient")
    paths = Paths(work, vault)
    lp = _read_json(os.path.join(paths.state, "last_published.json"), {}) or {}
    lp_at = _dt(lp.get("at")) if isinstance(lp, dict) else None
    rows = db.execute("SELECT * FROM plan_items WHERE category='D' AND decision='executed' AND "
                      "verify_status='pending_next_round' ORDER BY plan_id, item_id").fetchall()
    if not rows or lp_at is None:
        return []
    out = []
    for pi in rows:
        pi = dict(pi)
        root = _nid(pi["notion_id"])
        jr = db.execute("SELECT * FROM journal WHERE plan_id=? AND item_id=? AND op='TRASH_PAGE' AND state IN "
                        "('done','reconciled') ORDER BY seq DESC LIMIT 1", (pi["plan_id"], pi["item_id"])).fetchone()
        if not root or jr is None:
            continue
        done_at = _dt(jr["ts"])
        if done_at is None or lp_at <= done_at:
            continue                                   # no refresh has published since the delete: not yet
        ops = _jload(pi["ops_json"], []) or []
        sub = [_nid(x) for op in ops if isinstance(op, dict) for x in (op.get("subtree_ids") or []) if _nid(x)]
        ids = [root] + [x for x in sub if x != root]
        reasons, transient = [], False
        # Notion
        try:
            for i in ids:
                pg = read_client.retrieve_page(i)
                if pg.get("in_trash") or pg.get("archived"):
                    continue
                chain, cur = False, pg
                for _ in range(32):
                    ppid = _nid(((cur or {}).get("parent") or {}).get("page_id"))
                    if not ppid:
                        break
                    if ppid == root:
                        chain = True
                        break
                    cur = read_client.retrieve_page(ppid)
                    if cur.get("in_trash"):
                        chain = True
                        break
                if not chain:
                    reasons.append(f"Notion 中 {i[:8]} 不在回收站（in_trash=false）")
        except NotionAPIError as exc:
            if exc.status in (None, 429) or (exc.status or 0) >= 500:
                transient = True
            else:
                reasons.append(f"Notion 读取 {exc.status} {exc.code}：无法证明已在回收站")
        if transient:
            out.append({"plan_id": pi["plan_id"], "item_id": pi["item_id"], "rel_path": pi["rel_path"],
                        "status": "pending_next_round", "reasons": ["Notion 暂时无法读取：下一次 plan 再核验"]})
            continue
        # vault
        if nid_index is not None:
            left = sorted({r for i in ids for r in (nid_index.get(i) or nid_index.get(i.replace("-", "")) or [])})
            if left:
                reasons.append("库内仍有该对象的文件：" + "、".join(left[:3]))
        # mappings (export.db)
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(paths.export_db)}?mode=ro", uri=True, timeout=30)
            try:
                con.row_factory = sqlite3.Row
                pub = con.execute(f"SELECT rel_path, status, notion_id FROM published_files WHERE notion_id IN "
                                  f"({','.join('?' * len(ids))})", ids).fetchall()
                om = con.execute("SELECT status FROM objects_map WHERE notion_id=?", (root,)).fetchone() \
                    if con.execute("SELECT 1 FROM sqlite_master WHERE name='objects_map'").fetchone() else None
                parent_rel = None
                par = (_jload(jr["before_json"], {}) or {}).get("parent") or {}
                if _nid(par.get("page_id")):
                    prow = con.execute("SELECT rel_path FROM published_files WHERE notion_id=? AND status='active' "
                                       "ORDER BY rowid DESC LIMIT 1", (_nid(par["page_id"]),)).fetchone()
                    parent_rel = prow["rel_path"] if prow else None
            finally:
                con.close()
        except sqlite3.Error as exc:
            out.append({"plan_id": pi["plan_id"], "item_id": pi["item_id"], "rel_path": pi["rel_path"],
                        "status": "pending_next_round", "reasons": [f"export.db 无法读取（{type(exc).__name__}）"]})
            continue
        if not any(_nid(r["notion_id"]) == root for r in pub):
            reasons.append("published_files 中没有该页面的记录")
        bad = [r["rel_path"] for r in pub if r["status"] != "trashed_via_writeback"]
        if bad:
            reasons.append("published_files 未全部标为 trashed_via_writeback：" + "、".join(bad[:3]))
        if om is None or om["status"] != "trashed_via_writeback":
            reasons.append(f"objects_map.status = {om['status'] if om else '（无记录）'}，不是 trashed_via_writeback")
        pd = db.execute("SELECT status FROM pending_deletes WHERE notion_id=? ORDER BY rowid DESC LIMIT 1",
                        (root,)).fetchone()
        if pd is None or pd["status"] != "confirmed":
            reasons.append(f"pending_deletes.status = {pd['status'] if pd else '（无记录）'}，不是 confirmed")
        if parent_rel and paths.vault:
            try:
                with open(wb_common.safe_join(paths.vault, parent_rel), encoding="utf-8", errors="replace") as fh:
                    ptext = fh.read()
                link = "[[" + str(pi["rel_path"] or "")[:-3] if str(pi["rel_path"] or "").endswith(".md") else None
                if root in ptext or root.replace("-", "") in ptext or (link and (link + "|" in ptext or
                                                                                 link + "]]" in ptext)):
                    reasons.append(f"父页面笔记 {parent_rel} 仍含指向该页面的链接")
            except (OSError, ValueError):
                pass
        if db.execute("SELECT 1 FROM trash_archive WHERE notion_id=?", (root,)).fetchone() is None:
            reasons.append("trash_archive 中没有存档")
        status = "needs_manual" if reasons else "verified"
        with wb_state.transaction(db):
            db.execute("UPDATE plan_items SET verify_status=?, reason=? WHERE plan_id=? AND item_id=? AND "
                       "verify_status='pending_next_round'",
                       (status, ("W4 下一轮核验未通过：" + "；".join(reasons))[:900] if reasons else pi.get("reason"),
                        pi["plan_id"], pi["item_id"]))
        out.append({"plan_id": pi["plan_id"], "item_id": pi["item_id"], "rel_path": pi["rel_path"], "status": status,
                    "reasons": reasons})
    return out


# =============================================================================================== rollback (WB §13)
RB_OPS = {"UPDATE": "RB_UPDATE", "APPEND": "RB_TRASH_BLOCK", "TRASH_BLOCK": "RB_RESTORE_BLOCK",
          "UPDATE_PROPERTIES": "RB_PROPERTIES", "RENAME": "RB_RENAME", "TRASH_PAGE": "RB_RESTORE_PAGE",
          "CREATE_PAGE": "RB_TRASH_PAGE"}


def _rb_rows(db, plan_id, item_ids):
    rows = [r for r in _j_rows(db, plan_id, ("done", "reconciled"))
            if r["op"] in RB_OPS and (not item_ids or r["item_id"] in item_ids)]
    out = []
    for r in rows:
        info = _jload(r["committed_ids"], {}) or {}
        if r["state"] == "reconciled" and str(info.get("reconciled", "")).endswith("not_committed"):
            continue
        if r["state"] == "reconciled" and info.get("reconciled") == "not_applied_by_get":
            continue
        out.append(r)
    created = set()
    for r in out:
        if r["op"] == "CREATE_PAGE":
            created.update(_nid(i) for i in (_jload(r["committed_ids"], {}) or {}).get("ids") or [])
    out = [r for r in out if not (r["op"] == "APPEND" and r["target_id"] in created)]
    done_rb = {(_jload(x["request_json"], {}) or {}).get("of_seq") for x in _j_rows(db, plan_id, ("done",))
               if x["op"].startswith("RB_")}
    return [r for r in reversed(out) if r["seq"] not in done_rb]


def _rb_provenance(plan, rows):
    """WB §13 (finding r1-never-unapproved-02): a rollback restores only what THIS plan wrote. Every forward journal
    row must name an item of the sha-verified plan.json and the op it was built from (request_json.op_index), and its
    target must be that op's own target: UPDATE / TRASH_BLOCK the op's block, with a before-image that hashes to the
    op's before_norm_sha (the content the rollback would restore); APPEND the op's parent (or a page this plan
    created); UPDATE_PROPERTIES / RENAME / TRASH_PAGE the item's page; CREATE_PAGE the op's parent. Returns
    {seq: reason} for every row that cannot be derived from the approved plan (a damaged or crafted journal)."""
    items = {it.get("item_id"): it for it in plan.get("items") or [] if isinstance(it, dict)}
    created = set()
    for r in rows:
        if r["op"] == "CREATE_PAGE":
            created.update(_nid(i) for i in (_jload(r["committed_ids"], {}) or {}).get("ids") or [])
    bad = {}
    for r in rows:
        it = items.get(r["item_id"])
        if it is None:
            bad[r["seq"]] = f"{r['item_id']} 不在计划中"
            continue
        ops = it.get("ops") or []
        oi = (_jload(r["request_json"], {}) or {}).get("op_index")
        if not isinstance(oi, int) or isinstance(oi, bool) or not 0 <= oi < len(ops) or not isinstance(ops[oi], dict):
            bad[r["seq"]] = f"{r['item_id']} 的计划中没有第 {oi} 个操作"
            continue
        op, name, jop, tgt = ops[oi], _op_name(ops[oi]), r["op"], _nid(r["target_id"])
        if not (name == jop or (jop == "APPEND" and name == "CREATE_PAGE")):
            bad[r["seq"]] = f"journal 操作 {jop} 与计划操作 {name} 不符"
            continue
        why = None
        if jop in ("UPDATE", "TRASH_BLOCK"):
            want = op.get("before_norm_sha") or op.get("before_json_sha") or op.get("json_sha")
            if not want and isinstance(op.get("before"), dict):
                want = wb_norm.block_json_sha(op["before"])
            before = _jload(r["before_json"])
            if tgt != _nid(op.get("target_id") or op.get("block_id")):
                why = "目标块不是计划中的块"
            elif not want or not isinstance(before, dict) or wb_norm.block_json_sha(before) != want:
                why = "写入前内容（before_json）与计划时的块不符"
        elif jop == "APPEND":
            ok = (tgt == _nid(op.get("parent_id"))) if name == "APPEND" else (tgt is None or tgt in created)
            if not ok:
                why = "追加的父块不是计划中的父块"
        elif jop in PAGE_OPS:
            page = _nid(it.get("notion_id") or op.get("page_id") or op.get("target_id"))
            if tgt != page or any(op.get(k) is not None and _nid(op.get(k)) != page for k in ("page_id", "target_id")):
                why = "目标页面不是本项的页面"
        elif jop == "CREATE_PAGE":
            par = op.get("parent") or {}
            if tgt != _nid(par.get("page_id") or par.get("data_source_id")):
                why = "新页面的父对象与计划不符"
        if why:
            bad[r["seq"]] = f"{r['item_id']} {jop}：{why}"
    return bad


def _rb_owner_page(r, row):
    """The page a rollback step writes to / under: the created page (CREATE_PAGE), the target page (page ops),
    otherwise the item's page from plan_items (or the block's parent chain)."""
    op = row["op"]
    if op == "CREATE_PAGE":
        ids = (_jload(row["committed_ids"], {}) or {}).get("ids") or []
        return _nid(ids[0]) if ids else None
    if op in PAGE_OPS:
        return _nid(row["target_id"])
    db = getattr(r, "db", None)
    if db is not None:
        pi = db.execute("SELECT notion_id FROM plan_items WHERE plan_id=? AND item_id=?",
                        (row["plan_id"], row["item_id"])).fetchone()
        if pi is not None and _nid(pi["notion_id"]):
            return _nid(pi["notion_id"])
    cur = _nid(row["target_id"])
    for _ in range(32):
        b = r.get_block(cur)
        par = b.get("parent") or {}
        if par.get("page_id"):
            return _nid(par["page_id"])
        cur = _nid(par.get("block_id"))
        if not cur:
            return None
    return None


def _rb_assess(r, row):
    """One rollback step for a forward journal row: {"seq", "op", "item_id", "target", "action": do|skip|manual,
    "reason", "requests": [(kind, target, body)]}. The preview line shows the REQUEST target (the new page for a
    created page, every block for an APPEND)."""
    op = row["op"]
    step = {"seq": row["seq"], "item_id": row["item_id"], "op": RB_OPS[op], "target": row["target_id"],
            "action": "skip", "reason": "", "requests": []}
    after = _jload(row["after_json"])
    before = _jload(row["before_json"], {}) or {}
    if op == "CREATE_PAGE":
        ids = (_jload(row["committed_ids"], {}) or {}).get("ids") or []
        step["target"] = _nid(ids[0]) if ids else None
        step["title"] = (_jload(row["request_json"], {}) or {}).get("title")
    if after is None and op != "TRASH_PAGE":
        step.update(action="manual", reason="journal 没有回读 JSON（after_json），不能证明未被改动：只报告")
        return step
    # WB §2.4 / §13: the tool honours page locks (the API does not); a rollback is prechecked like an apply
    owner = _rb_owner_page(r, row)
    if owner:
        op_page = r.get_page(owner)
        if op_page.get("is_locked"):
            step.update(action="manual", reason="页面已锁定：回退不写入（请先在 Notion 中解锁，再重新预览回退）")
            return step
        if op_page.get("in_trash") and op == "CREATE_PAGE":
            step["reason"] = "新页面已在回收站"
            return step
        if op_page.get("in_trash") and op != "TRASH_PAGE":
            step.update(action="manual", reason="页面已在回收站：回退不写入")
            return step
    if op == "UPDATE":
        live = r.get_block(row["target_id"])
        if live.get("in_trash") or wb_norm.json_norm(_cmp_view(live)) != wb_norm.json_norm(_cmp_view(after)):
            step["reason"] = "块在写入后又被编辑（与 after_json 不同）：跳过"
            return step
        t = _block_type(before)
        f = md2blocks.writable_fields(before)
        body = {t: {}}
        try:
            if "rich_text" in f:
                body[t]["rich_text"] = md2blocks.request_rich(md2blocks.split_for_api(f["rich_text"], render_safe=False))
            if "cells" in f:
                body[t]["cells"] = [md2blocks.request_rich(md2blocks.split_for_api(c, render_safe=False))
                                    for c in f["cells"]]
        except md2blocks.Refused as exc:
            step.update(action="manual", reason=f"写入前内容无法重建为请求（{exc.reason}）：只报告")
            return step
        for k in ("checked", "language", "expression"):
            if k in f:
                body[t][k] = f[k]
        step.update(action="do", reason="恢复为写入前内容", requests=[("update_block", row["target_id"], body)],
                    expected=md2blocks.canon_writable(before), before_live=live)
        return step
    if op == "APPEND":
        # exactly the blocks THIS row committed (journal ids), each compared with its own read-back JSON; nothing
        # that merely sits under the same parent (another plan's or a person's block) is ever trashed
        ids = [_nid(i) for i in (_jload(row["committed_ids"], {}) or {}).get("ids") or [] if _nid(i)]
        if not ids:
            step.update(action="manual", reason="journal 没有记录本次新建的块 id：只报告")
            return step
        by_after = {_nid(b.get("id")): b for b in after or [] if isinstance(b, dict)}
        bot = getattr(r, "bot", None)
        reqs = []
        for bid in ids:
            b_after = by_after.get(bid)
            if b_after is None:
                step.update(action="manual", reason=f"新块 {bid[:8]} 没有回读 JSON：只报告")
                return step
            live = r.get_block(bid)
            if live.get("in_trash"):
                continue
            if _block_type(live) in NEVER_TRASH_TYPES:
                step.update(action="manual", reason="新块类型不允许删除")
                return step
            if not bot or _nid((live.get("created_by") or {}).get("id")) != bot:
                step.update(action="manual", reason=f"块 {bid[:8]} 不能证明是写回集成创建的：只报告")
                return step
            if wb_norm.json_norm(_cmp_view(live)) != wb_norm.json_norm(_cmp_view(b_after)) or \
                    bool(live.get("has_children")) != bool(b_after.get("has_children")):
                step["reason"] = f"新块 {bid[:8]} 在写入后又被编辑：跳过整个 APPEND 回退"
                return step
            if b_after.get("_children") or live.get("has_children"):
                kids, kraw = _live_tree(r, [_nid(k.get("id")) for k in r.children(bid)])
                if [wb_norm.json_norm(_cmp_view(x)) for x in kraw] != \
                        [wb_norm.json_norm(_cmp_view(x)) for x in b_after.get("_children") or []]:
                    step["reason"] = f"新块 {bid[:8]} 的子块在写入后又被改动：跳过"
                    return step
            reqs.append(("trash_block", bid, None))
        if not reqs:
            step["reason"] = "新块已不在（已被删除）"
            return step
        step.update(action="do", target=", ".join(x[1][:8] for x in reqs),
                    reason=f"把本次追加的 {len(reqs)} 个新块移入回收站（{', '.join(x[1] for x in reqs)}）",
                    requests=reqs)
        return step
    if op == "TRASH_BLOCK":
        live = r.get_block(row["target_id"])
        if not live.get("in_trash"):
            step["reason"] = "块已不在回收站"
            return step
        step.update(action="do", reason="已恢复但位置可能变化（needs_manual）",
                    requests=[("restore_block", row["target_id"], None)], needs_manual=True)
        return step
    if op == "UPDATE_PROPERTIES":
        live = r.get_page(row["target_id"])
        exp = _jload(row["expected_after_json"], {})
        cur = {n: _prop_canon(_full_prop(r, live, n)) for n in exp}
        if cur != exp:
            step["reason"] = "属性在写入后又被修改：跳过"
            return step
        props = {}
        for n, v in before.items():
            t = (v or {}).get("type")
            val = (v or {}).get(t)
            if t in ("title", "rich_text"):
                val = md2blocks.request_rich(val or [])
            props[n] = {t: val}
        step.update(action="do", reason="属性恢复为写入前的值",
                    requests=[("update_page", row["target_id"], {"properties": props})])
        return step
    if op == "RENAME":
        live = r.get_page(row["target_id"])
        tp = before.get("title_property") or _title_prop(live)
        exp = _jload(row["expected_after_json"], {})
        if md2blocks.canon_rich_text((_full_prop(r, live, tp) or {}).get("title") or []) != exp.get("title"):
            step["reason"] = "标题在写入后又被修改：跳过"
            return step
        step.update(action="do", reason="标题改回旧标题",
                    requests=[("update_page", row["target_id"],
                               {"properties": {tp: {"title": md2blocks.request_rich(before.get("title") or [])}}})])
        return step
    if op == "TRASH_PAGE":
        live = r.get_page(row["target_id"])
        if not live.get("in_trash"):
            step["reason"] = "页面已不在回收站"
            return step
        step.update(action="do", reason="从回收站恢复（in_trash:false）；下一轮 refresh 会把笔记按 Notion 版本恢复到库内",
                    requests=[("update_page", row["target_id"], {"in_trash": False})])
        return step
    if op == "CREATE_PAGE":
        pid = step["target"]
        if not pid:
            step.update(action="manual", reason="没有新页面 id")
            return step
        live = r.get_page(pid)
        if live.get("in_trash"):
            step["reason"] = "新页面已在回收站"
            return step
        bot = getattr(r, "bot", None)
        if not bot or _nid((live.get("last_edited_by") or {}).get("id")) != bot:
            step["reason"] = "新页面最后编辑者不是写回集成（已被人编辑）：只报告"
            step["action"] = "manual"
            return step
        kids = r.children(pid)
        if any(_block_type(b) in ("child_page", "child_database") for b in kids):
            step.update(action="manual", reason="新页面下已有子页面或子数据库：只报告")
            return step
        _got, raw = _live_tree(r, [_nid(b.get("id")) for b in kids])
        want = (after or {}).get("blocks") or []
        others = [x for x in _rows_for_new_page(r, row)]
        want = want + others
        if [wb_norm.json_norm(_cmp_view(x)) for x in raw] != [wb_norm.json_norm(_cmp_view(x)) for x in want]:
            step["reason"] = "新页面内容在写入后又被编辑：只报告"
            step["action"] = "manual"
            return step
        step.update(action="do", reason=f"把本次新建的页面“{step.get('title') or ''}”移入回收站",
                    requests=[("update_page", pid, {"in_trash": True})])
        return step
    return step


def _rows_for_new_page(r, create_row):
    """after_json of the filling APPENDs of a new page (same item, after the CREATE row)."""
    db = getattr(r, "db", None)
    if db is None:
        return []
    out = []
    for x in _j_rows(db, create_row["plan_id"], ("done", "reconciled")):
        if x["item_id"] == create_row["item_id"] and x["op"] == "APPEND" and x["seq"] > create_row["seq"]:
            out += _jload(x["after_json"], []) or []
    return out


def _cmp_view(b):
    """Block JSON for 'unchanged since our write' comparisons (no volatile keys, no children payload)."""
    if not isinstance(b, dict):
        return b
    x = {k: v for k, v in b.items() if k not in ("_children", "has_children", "request_id")}
    return x


class _RbReader:
    """Read helper for rollback (read-only client; STOP checked before each request)."""

    def __init__(self, rc, paths, db, bot=None):
        self.rc, self.paths, self.db = rc, paths, db
        self.bot = bot or (_nid(wb_state.get_meta(db, BOT_META) or "") if db is not None else None)

    def check_stop(self):
        if os.path.exists(self.paths.stop):
            raise _StopRun("急停文件存在：停止")

    get_page = Ctx.get_page
    get_block = Ctx.get_block
    children = Ctx.children
    prop_items = Ctx.prop_items
    query = Ctx.query


def rollback_preview(plan_id, item_ids, *, read_client, db, work=None, bot_id=None):
    """Read-only preview of `rollback --plan P [--items]`: per journal row what would be done, skipped (edited
    since: live JSON != journal after_json) or only reported. Sends no write."""
    paths = Paths(work)
    if not isinstance(read_client, ReadOnlyClient):
        return {"ok": False, "reason": "读取客户端必须是 ReadOnlyClient", "steps": []}
    r = _RbReader(read_client, paths, db, _nid(bot_id) if bot_id else None)
    try:
        plan, _sha, _row = load_plan_verified(plan_id, work=paths.work, db=db)
    except ApplyRefused as exc:
        return {"ok": False, "reason": exc.reason, "steps": []}
    steps = []
    try:
        fwd = _rb_rows(db, plan_id, set(item_ids or []))
        bad = _rb_provenance(plan, fwd)
        for row in fwd:
            if row["seq"] in bad:
                steps.append({"seq": row["seq"], "item_id": row["item_id"], "op": RB_OPS[row["op"]],
                              "target": row["target_id"], "action": "manual", "requests": [],
                              "reason": f"journal 记录与已确认的计划不符（{bad[row['seq']]}）：整个回退会被拒绝"})
                continue
            try:
                st = _rb_assess(r, row)
            except NotionAPIError as exc:
                st = {"seq": row["seq"], "item_id": row["item_id"], "op": RB_OPS[row["op"]], "target": row["target_id"],
                      "action": "manual", "reason": f"读取失败（{exc.status} {exc.code}）", "requests": []}
            steps.append({k: v for k, v in st.items() if k not in ("before_live",)})
    except _StopRun as exc:
        return {"ok": False, "reason": exc.reason, "steps": steps}
    return {"ok": True, "plan_id": plan_id, "steps": steps,
            "counts": {a: sum(1 for s in steps if s["action"] == a) for a in ("do", "skip", "manual")},
            "lines": [f"{s['item_id']} {s['op']} {str(s['target'] or '')[:60]}：{s['action']}（{s['reason']}）"
                      for s in steps]}


def rollback_plan(plan_id, item_ids, confirmation_record, *, client, read_client, cfg, db, now=None, work=None,
                  vault=None, cfg_path=None):
    """`writeback.py rollback` after the human gate (scope `rollback` confirmations). Re-assesses every step live
    right before sending (compare with journal after_json, never only a sha); journaled; read back."""
    paths = Paths(work, vault)
    if cfg_path is None and os.path.isfile(paths.config):
        cfg_path = paths.config
    res = _empty_result(plan_id)
    try:
        with wb_locks.lock_all(["writeback", "refresh"], state_dir=paths.state):
            try:
                plan, sha, row = load_plan_verified(plan_id, work=paths.work, db=db)
                vp = vault_problem(paths, plan)
                if vp:
                    raise ApplyRefused(vp)
            except ApplyRefused as exc:
                return dict(res, reason=exc.reason)
            if row.get("status") not in ("applied", "partially_applied", "aborted"):
                return dict(res, reason=f"计划状态 {row.get('status')} 不能回退（只有已执行过的计划）")
            fwd = _rb_rows(db, plan_id, set(item_ids or []))
            items = sorted({r["item_id"] for r in fwd})
            if item_ids:
                extra = set(item_ids) - {r["item_id"] for r in _j_rows(db, plan_id)}
                if extra:
                    return dict(res, reason=f"这些项目在本计划中没有执行记录：{sorted(extra)}")
            ctx = Ctx(plan_id=plan_id, plan=plan, plan_sha=sha, plan_row=row, client=client, read_client=read_client,
                      cfg=cfg, cfg_path=cfg_path, db=db, paths=paths, now=now, label="rollback")
            ctx.selected = set(items)
            try:
                _check_token(client)
                if not isinstance(read_client, ReadOnlyClient):
                    raise ApplyRefused("读取客户端必须是 ReadOnlyClient")
                _check_record(confirmation_record, plan_id, sha, None, "rollback")
                _check_confirm_rows(db, plan_id, sha, items, "rollback")
                # r1-never-unapproved-02: what is rolled back must be what the person approved for THIS plan: the
                # original apply confirmation of every item, and every journal row derivable from plan.json
                _check_confirm_rows(db, plan_id, sha, items, "apply")
                bad = _rb_provenance(plan, fwd)
                if bad:
                    raise ApplyRefused("journal 中有与已确认计划不符的记录（" + "；".join(
                        f"#{k} {v}" for k, v in sorted(bad.items())[:5]) + "）：拒绝回退，没有发送任何请求；"
                        "state/writeback.db 可能被改动，请联系维护者检查")
                ctx.check_stop()
                mode, why = ctx.effective_mode()
                if mode not in WRITE_MODES:
                    raise ApplyRefused(f"有效模式为 {mode}，不允许写入（{'；'.join(why)}）")
                if getattr(client, "gate", None) is None:
                    raise ApplyRefused("写回客户端没有共享限流（RateGate）")
            except (ApplyRefused, _StopRun) as exc:
                return dict(res, reason=getattr(exc, "reason", str(exc)))
            prev = _install_hook(ctx)
            try:
                return _rollback_run(ctx, fwd)
            except Exception as exc:  # noqa: BLE001 - a defect: stop, report plainly, never resend
                for r in _j_rows(db, plan_id, ("intent",)):
                    if r["op"].startswith("RB_"):
                        _j_update(ctx, r["seq"], state="cancelled", committed_ids={"outcome": "cancelled"})
                return dict(res, status="partially_applied" if ctx.sends else "aborted",
                            reason=(f"回退过程中出现程序错误（{exc.__class__.__name__}）：已停止，没有重发任何请求；"
                                    "请核对当前回退报告和实际页面的状态，再联系维护者"),
                            error=f"{exc.__class__.__name__}: {exc}"[:300], requests=len(ctx.sends))
            finally:
                ctx.client.on_send = prev
    except wb_locks.LockBusy as exc:
        return dict(res, reason=str(exc))


# r1-user-journey-03: a rollback record reads as a rollback (not as a forward apply), in plain words
RB_STATUS_ZH = {"applied": "回退完成", "partially_applied": "回退部分完成，待人工处理", "aborted": "回退已中止",
                "refused": "回退被拒绝"}
RB_ACTION_ZH = {"do": "已回退", "skip": "跳过", "manual": "只报告（需人工处理）", "failed": "失败", "not_run": "未执行"}


def _rollback_run(ctx, fwd):
    r = _RbReader(ctx.rc, ctx.paths, ctx.db)
    out_steps, stopped = [], None
    try:
        r.bot = ctx.bot_id()
    except (ApplyRefused, _StopRun) as exc:
        return dict(_empty_result(ctx.plan_id), reason=getattr(exc, "reason", str(exc)))
    for row in fwd:
        if stopped:
            out_steps.append({"seq": row["seq"], "item_id": row["item_id"], "action": "not_run", "reason": stopped})
            continue
        try:
            ctx.guard_write()
            st = _rb_assess(r, row)
            if st["action"] != "do":
                out_steps.append({k: v for k, v in st.items() if k not in ("before_live",)})
                continue
            rec = {k: v for k, v in st.items() if k not in ("before_live", "requests")}
            rec["results"] = []
            for kind, target, body in st["requests"]:
                if kind == "trash_block" and not _rb_trash_allowed(ctx, r, row, target):
                    rec["results"].append({"target": target, "outcome": "refused", "reason": "不是本次写入的新块"})
                    rec["action"] = "manual"
                    continue
                seq = _j_insert(ctx, row["item_id"], st["op"], target, {"of_seq": row["seq"]},
                                {"of_seq": row["seq"], "kind": kind, "body": body}, st.get("expected"))
                fn = {"update_block": lambda t=target, b=body: ctx.client.update_block(t, b),
                      "trash_block": lambda t=target: ctx.client.trash_block(t),
                      "restore_block": lambda t=target: ctx.client.restore_block(t),
                      "update_page": lambda t=target, b=body: ctx.client.update_page(t, b)}[kind]
                res = _send(ctx, seq, fn)
                ok = res.outcome == "ok"
                if ok:
                    after = r.get_page(target) if kind == "update_page" else r.get_block(target)
                    _j_update(ctx, seq, after_json=after)
                    ok = _rb_readback_ok(kind, body, after, st)
                rec["results"].append({"target": target, "outcome": res.outcome if ok or res.outcome != "ok" else "readback_mismatch",
                                       "code": res.code})
                if not ok:
                    rec["action"] = "failed"
                    break
            if rec.get("action") == "do" and st["op"] == "RB_RESTORE_PAGE":
                _after_restore_page(ctx, row)
            if rec.get("needs_manual"):
                rec["note"] = "已恢复但位置可能变化（needs_manual）"
                try:
                    blk = r.get_block(row["target_id"])
                    par = blk.get("parent") or {}
                    pid = _nid(par.get("block_id") or par.get("page_id"))
                    sib = [_nid(x.get("id")) for x in r.children(pid)] if pid else []
                    if row["target_id"] in sib:
                        rec["note"] += f"：现位于父块第 {sib.index(row['target_id']) + 1}/{len(sib)} 个，请在 Notion 中手工移回原位置"
                except NotionAPIError:
                    pass
            out_steps.append(rec)
        except _StopRun as exc:
            stopped = exc.reason
            out_steps.append({"seq": row["seq"], "item_id": row["item_id"], "action": "not_run", "reason": stopped})
        except NotionAPIError as exc:
            out_steps.append({"seq": row["seq"], "item_id": row["item_id"], "action": "manual",
                              "reason": f"读取失败（{exc.status} {exc.code}）"})
    done = [s for s in out_steps if s.get("action") == "do"]
    bad = [s for s in out_steps if s.get("action") in ("failed", "manual", "not_run")]
    status = "aborted" if stopped else ("applied" if not bad and all(s.get("action") in ("do", "skip") for s in out_steps)
                                        else "partially_applied")
    out = _empty_result(ctx.plan_id)
    out.update(status=status, reason=stopped, executed=sorted({s["item_id"] for s in done}),
               skipped=[{"item_id": s["item_id"], "status": s.get("action"), "reason": s.get("reason")}
                        for s in out_steps if s.get("action") != "do"],
               steps=out_steps, requests=len(ctx.sends),
               needs_manual=[s for s in out_steps if s.get("needs_manual") or s.get("action") == "manual"])
    nm = len(out["needs_manual"])
    lines = [f"# 写回回退结果 {ctx.plan_id}", "",
             f"- 结果：{RB_STATUS_ZH.get(status, status)}（{status}）；写请求数：{len(ctx.sends)}" +
             (f"；需人工检查 {nm} 项（见下表“说明”）" if nm else ""), "",
             "| journal | 项目 | 回退操作 | 结果 | 说明 |", "|---|---|---|---|---|"]
    for s in out_steps:
        act = RB_ACTION_ZH.get(s.get("action"), s.get("action"))
        if s.get("action") == "do" and s.get("needs_manual"):
            act += "，需人工检查"
        lines.append(f"| {s.get('seq')} | {s.get('item_id')} | {s.get('op', '')} | {act} | "
                     f"{str(s.get('note') or s.get('reason') or '').replace('|', '／')[:200]} |")
    lines += ["", "- 库内文件不在此处修改：下一轮 refresh 会按 Notion 的当前版本更新库内笔记（先备份）。"]
    pdir = ctx.paths.plan_dir(ctx.plan_id)
    os.makedirs(pdir, exist_ok=True)
    n, p = 1, os.path.join(pdir, "rollback.md")
    while os.path.exists(p):
        n += 1
        p = os.path.join(pdir, f"rollback-{n}.md")
    wb_common.atomic_write(p, ("\n".join(lines) + "\n").encode("utf-8"))
    out["result_md"] = p
    if os.path.isdir(ctx.paths.vault):
        d = os.path.join(ctx.paths.vault, "Notion_Obsidian", "运行记录")
        os.makedirs(d, exist_ok=True)
        stem, k = f"写回-{ctx.plan_id}-rollback", 1
        cand = f"{stem}.md"
        while os.path.lexists(os.path.join(d, cand)):
            k += 1
            cand = f"{stem}-{k}.md"
        text = "\n".join(["---", "notion_kind: generated", "generated_by: writeback.py", f"plan_id: {ctx.plan_id}",
                          "---"] + lines).replace("[[", "［［").replace("]]", "］］")
        fd = os.open(os.path.join(d, cand), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as fh:
            fh.write((text.rstrip("\n") + "\n").encode("utf-8"))
        out["vault_record"] = f"Notion_Obsidian/运行记录/{cand}"
    return out


def _after_restore_page(ctx, row):
    """A rolled-back page delete (W8: no dangling rows): pending_deletes leaves `confirmed` (expired = the next
    refresh restores the note from Notion and does not hold it again, like `writeback.py restore-deleted`), and
    the item is no longer pending_next_round."""
    nid = _nid(row["target_id"])
    with wb_state.transaction(ctx.db):
        ctx.db.execute("UPDATE pending_deletes SET status='expired', ttl_until=? WHERE notion_id=? AND status='confirmed'",
                       (wb_common.now_iso(), nid))
        ctx.db.execute("UPDATE plan_items SET verify_status='rolled_back' WHERE plan_id=? AND item_id=?",
                       (ctx.plan_id, row["item_id"]))


def _rb_trash_allowed(ctx, r, row, target):
    """L-09: trash only a block THIS journal row committed (its own journal ids -- never an id that merely appears
    in a read-back of the parent), created by the write-back bot, never child_page/child_database."""
    ids = {_nid(i) for i in (_jload(row["committed_ids"], {}) or {}).get("ids") or []}
    if target not in ids:
        return False
    live = r.get_block(target)
    if _block_type(live) in NEVER_TRASH_TYPES:
        return False
    return bool(r.bot) and _nid((live.get("created_by") or {}).get("id")) == r.bot


def _rb_readback_ok(kind, body, after, st):
    if kind == "update_block":
        return md2blocks.canon_writable(after) == st.get("expected")
    if kind == "trash_block":
        return bool(after.get("in_trash"))
    if kind == "restore_block":
        return not after.get("in_trash")
    if kind == "update_page":
        if "in_trash" in body:
            return bool(after.get("in_trash")) == bool(body["in_trash"])
        for n, v in (body.get("properties") or {}).items():
            if _prop_canon((after.get("properties") or {}).get(n)) != _prop_canon(v):
                return False
        return True
    return False


__all__ = ["make_clients", "execute_confirmed_plan", "resume_plan", "rollback_preview", "rollback_plan",
           "reconcile_journal", "audit_scope", "load_plan_verified", "count_members", "creation_state_now",
           "ApplyRefused", "Paths", "STOP_FILE"]
