#!/usr/bin/env python3
"""One refresh round (Notion -> vault, one-way): sidebar -> snapshot -> assets -> scan -> export -> verify ->
publish plan -> pause evaluation -> publish apply -> post-publish verify.

  python3 -I refresh.py --mode incremental   (scheduled; reuses unchanged pages from the last snapshot)
  python3 -I refresh.py --mode full          (weekly safety net: every page re-read)
  add --dry-run to stop before applying (plan + evaluation only; no vault or state writes except reports)
  python3 -I refresh.py --use-snapshot snapshots/snap_X.db   (no sidebar capture, no Notion calls: export ->
                                             verify -> publish from that FINISHED snapshot; rehearsals/re-publish)
  python3 -I refresh.py --approve-run <run_id> | --discard-run <run_id> | --release-hold <notion_id>
                                             (human decisions: real terminal + y/N, WRITEBACK_SPEC §14.4, D-04)

Guards (WRITEBACK_SPEC §6.0, §7.4, §9, §14; IMPL_CONTRACT §3, §5.11):
* single instance: wb_locks.lock("refresh") (same file and flock as before);
* read-only vault scan (wb_scan) before export: name overrides -> exporter/publisher (--overrides), local
  deletions by mode (off -> restored as before; >= plan -> pending_deletes hold within max_local_deletes,
  else the whole round pauses), mapping anomalies / Sync conflict copies -> object-level holds;
* staging verify must PASS C01-C10 (with reports/<run>/expected_deviations.json and --snapshot-immutable);
  interim adapter: a FAIL made ONLY of C08 aliases_missing_original_title on applied name-override notes
  (exporter gap, WB 7.4.3) holds exactly those objects and re-runs the gate once;
* two-step publish: plan-only, §14.3 evaluation (object-level -> state/auto_sync_hold.json; run-level ->
  result paused_for_review + Notion_Obsidian/运行记录/<run>-已暂停.md), then --plan-file/--plan-sha apply;
  conflicts (CONFLICT_*) are registered as paused deviations at the kept vault file; only NEW conflicts (no
  F dedup) count for the run-level conflict rules; Sync-copy / frontmatter / NFC-NFD holds hold no subtree;
* post-publish verify with the same deviations; locatable FAILs -> object holds, unlocatable -> next round
  pauses; state/last_published_snapshot.txt only after an applied (non-paused) round;
* never deletes vault files; prune keeps paused runs' evidence. This module never imports a Notion write
  client and never loads the write-back token; Notion is only read (member count, new-root check).
Results: published | published_with_holds | paused_for_review | failed | published_post_verify_failed
(| planned for --dry-run). Exit: 0 published*/planned, 2 paused_for_review, 3 gate refused, 1 otherwise.
"""
import argparse
import datetime
import glob
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(WORK, "tools")
if T not in sys.path:
    sys.path.insert(0, T)

import trash_archive  # noqa: E402
import wb_common  # noqa: E402
from wb_common import cli_command
import wb_config  # noqa: E402
import wb_locks  # noqa: E402
import wb_scan  # noqa: E402
import wb_state  # noqa: E402

VAULT = wb_common.PROD_VAULT
STATE = os.path.join(WORK, "state")
BLOCKING = ["C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10"]  # C11 = warning (user: ignore)
KEEP_STAGING = 3
KEEP_SNAPSHOTS = 4
BASELINE_SNAPSHOT = "snap_20261117T0145"  # first full baseline, never pruned
PY = [sys.executable, "-I", "-B"]   # contract §1.7: no __pycache__ in WORK/tools
STANDING_APPROVAL = "standing_daily_refresh.approval.md"
RUN_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}")
EXIT_OK, EXIT_FAIL, EXIT_PAUSED, EXIT_GATE = 0, 1, 2, 3
OK_RESULTS = ("published", "published_with_holds", "planned")

# publisher ops (IMPL_CONTRACT §5.10; pipeline.md names accepted as aliases)
WRITE_OPS = frozenset({"ADD", "RESTORE", "REPLACE", "MOVE", "LINK_REWRITE_ONLY", "LOCAL_KEYS_ONLY", "GENERATED_OVERWRITE",
                       "REPLACE_KEEP_LOCAL_KEYS", "REPLACE_TOOL_OWNED", "REPLACE_BASE", "ADOPT_WRITEBACK"})
CONFLICT_OPS = frozenset({"CONFLICT_BOTH", "CONFLICT_MOVED_EDITED", "CONFLICT_LOCAL"})
LOCAL_KEPT_OPS = frozenset({"LOCAL_ONLY", "LOCAL_KEYS_ONLY", "CONFLICT_BOTH", "CONFLICT_MOVED_EDITED", "CONFLICT_LOCAL"})
HELD_OPS = frozenset({"SKIP_HELD", "HOLD_SKIP"})
NOTE_KINDS = ("page", "row", "database", "template")
SCAN_HOLD_SOURCES = ("scan", "export")      # recomputed every round (auto release when fixed)
VOLATILE_PLAN_KEYS = frozenset({"ts", "at", "created_at", "generated_at", "src", "run_id", "report_dir", "plan_sha256"})

FIX_STEPS_NEW_ROOT = ("在 Notion 中打开该页面 → 右上角 ••• → 连接 → 添加读连接“Godcreate”；"
                      "下一轮 refresh 会自动纳入该页面及其子页面。")
SIDEBAR_FAIL_ALERT = 4          # consecutive failed sidebar captures (≈ 1 day of 6-hourly rounds) -> escalated notice
FIX_STEPS_SIDEBAR = ("侧栏抓取依赖 Notion 网页界面：请确认浏览器会话 wb_dev_sidebar2 仍登录 Notion，并在终端运行 "
                     "{capture} 查看失败原因；修复前，新建的顶层页面不会被发现"
                     "（已共享的旧顶层页面照常同步）。")
BLOCK_LIMIT_WARNING = "工作区已超出免费多成员额度：宽限期 3 天后界面与 API 均无法新建内容，请升级套餐或移除成员"


# ------------------------------------------------------------------ paths and small helpers
class Paths:
    """Every location one round touches. Production: Paths() (WORK of this file, the formal vault)."""

    def __init__(self, work=WORK, vault=VAULT):
        self.work = os.path.abspath(work)
        self.tools = os.path.join(self.work, "tools")
        self.state = os.path.join(self.work, "state")
        self.reports = os.path.join(self.work, "reports")
        self.staging = os.path.join(self.work, "staging")
        self.snapshots = os.path.join(self.work, "snapshots")
        self.logs = os.path.join(self.work, "logs")
        self.evidence = os.path.join(self.work, "evidence")
        self.approvals = os.path.join(self.work, "approvals")
        self.config = os.path.join(self.work, "config", "writeback.json")
        self.export_db = os.path.join(self.state, "export.db")
        self.wb_db = os.path.join(self.state, "writeback.db")
        self.holds = os.path.join(self.state, "auto_sync_hold.json")
        self.paused_runs = os.path.join(self.state, "paused_runs.json")
        self.pause_next = os.path.join(self.state, "refresh_pause_next.json")
        self.latest_snapshot = os.path.join(self.state, "latest_snapshot.txt")
        self.last_published_txt = os.path.join(self.state, "last_published_snapshot.txt")
        self.last_published_json = os.path.join(self.state, "last_published.json")
        self.vault = os.path.abspath(vault)

    def report(self, run_id, *parts):
        return os.path.join(self.reports, run_id, *parts)


def now_iso():
    return wb_common.now_iso()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj, indent=1):
    # backslashreplace: a vault name that is not valid UTF-8 (os.walk surrogate escapes, wb_scan undecodable_names)
    # becomes the JSON escape \udcxx (json.load gives the same str back) instead of a UnicodeEncodeError
    data = (json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False) + "\n").encode("utf-8", "backslashreplace")
    wb_common.atomic_write(path, data)


def write_text(path, text):
    wb_common.atomic_write(path, text.encode("utf-8", "backslashreplace"))  # never fails on a non-UTF-8 vault name


def latest_complete_snapshot(P=None):
    P = P or Paths()
    try:
        with open(P.latest_snapshot, encoding="utf-8") as fh:
            s = fh.read().strip()
    except OSError:
        return None
    return s if s and os.path.exists(s) else None


def last_published_snapshot(P):
    try:
        with open(P.last_published_txt, encoding="utf-8") as fh:
            s = fh.read().strip()
    except OSError:
        return None
    return s if s and os.path.exists(s) else None


def latest_sidebar(P=None):
    P = P or Paths()
    c = sorted(glob.glob(os.path.join(P.evidence, "sidebar_*T*.json")))
    c = [x for x in c if not x.endswith(".raw.json")]
    good = []
    for x in c:
        d = read_json(x, {})
        if isinstance(d, dict) and d.get("order_match"):
            good.append(x)
    return good[-1] if good else None


def snapshot_meta(path):
    """meta table of a snapshot (opened read-only) as a dict; {} when unreadable."""
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        return {}
    try:
        con, _ = wb_common.open_snapshot_reader(p)
        try:
            return {k: v for k, v in con.execute("SELECT key, value FROM meta")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _nid_of_action(a):
    return a.get("nid") or a.get("notion_id")


# ------------------------------------------------------------------ external tools
class Runner:
    """Runs one tool script of P.tools as a subprocess with output appended to the round log."""

    def __init__(self, P, log):
        self.P, self.log = P, log
        self.calls = []

    def __call__(self, script, args, timeout=None):
        argv = PY + [os.path.join(self.P.tools, script)] + [str(a) for a in args]
        self.calls.append((script, list(args)))
        self.log.write(f"\n$ {' '.join(argv)}\n")
        self.log.flush()
        try:
            r = subprocess.run(argv, stdout=self.log, stderr=subprocess.STDOUT, timeout=timeout, cwd=self.P.tools)
        except subprocess.TimeoutExpired:
            self.log.write(f"TIMEOUT after {timeout}s: {script}\n")
            return 124
        return r.returncode


def default_client_factory(P):
    """Read-only Notion client (ReadOnlyClient + shared rate gate); the token is the READ token only."""
    from rate_gate import RateGate
    from readonly_client import make_read_client
    return make_read_client(gate=RateGate(os.path.join(P.state, "rate_gate.json"), total_rps=2.5), rate=2.5)


# ------------------------------------------------------------------ Notion reads: members, roots
def count_members(client):
    """{"count": persons | None, "status": "ok" | "unknown", "error": str | None}. Names are never kept."""
    try:
        n = 0
        for u in client.paginate("GET", "users"):
            if isinstance(u, dict) and u.get("type") == "person":
                n += 1
        return {"count": n, "status": "ok", "error": None}
    except Exception as exc:  # noqa: BLE001 - 403 (no user-info capability) or anything else: unknown
        st = getattr(exc, "status", None)
        code = getattr(exc, "code", None) or type(exc).__name__
        return {"count": None, "status": "unknown", "error": f"{st} {code}".strip()}


def member_status(cfg, res):
    """WB §9.2/9.3: effective members = max(API count, config); unknown or >= 2 -> warn_block_limit."""
    configured = int(cfg.get("workspace_members") or 1)
    cnt = res.get("count")
    eff = None if cnt is None else max(cnt, configured)
    warn = eff is None or eff >= 2
    paid = str(cfg.get("notion_plan") or "free").lower() != "free"
    state = "unlimited" if (paid or (eff is not None and eff < 2)) else "blocked_assumed"
    return {"count": cnt, "configured": configured, "effective": eff, "status": res.get("status"),
            "error": res.get("error"), "warn_block_limit": bool(warn and not paid), "creation_state": state}


def check_roots(client, roots):
    """Split sidebar roots into (reachable, unreachable, unknown) for the read connection (contract §7.9:
    a new top-level page is 404 until it is shared with the connection)."""
    ok, gone, unknown = [], [], []
    for r in roots:
        rid = r.get("id")
        if not rid:
            unknown.append(dict(r, error="no id"))
            continue
        try:
            client.request("GET", f"pages/{rid}")
            ok.append(r)
            continue
        except Exception as exc:  # noqa: BLE001
            st = getattr(exc, "status", None)
            if st not in (400, 403, 404):
                unknown.append(dict(r, error=f"{st} {getattr(exc, 'code', type(exc).__name__)}"))
                ok.append(r)  # keep it: the snapshot decides
                continue
        try:
            client.request("GET", f"databases/{rid}")
            ok.append(r)
        except Exception as exc:  # noqa: BLE001
            st = getattr(exc, "status", None)
            if st in (400, 403, 404):
                gone.append(dict(r, status=st))
            else:
                unknown.append(dict(r, error=f"{st} {getattr(exc, 'code', type(exc).__name__)}"))
                ok.append(r)
    return ok, gone, unknown


def effective_sidebar(sidebar, drop_ids, out_path):
    """Copy of the sidebar evidence without `drop_ids` (root_order renumbered per teamspace)."""
    d = read_json(sidebar, {})
    keep = [r for r in d.get("roots", []) if r.get("id") not in drop_ids]
    per = {}
    for r in sorted(keep, key=lambda x: (x.get("teamspace_order") or 0, x.get("root_order") or 0)):
        per[r.get("teamspace")] = per.get(r.get("teamspace"), 0) + 1
        r["root_order"] = per[r.get("teamspace")]
    d["roots"] = keep
    d["filtered_by_refresh"] = {"source": os.path.abspath(sidebar), "dropped_unreachable_roots": sorted(drop_ids)}
    write_json(out_path, d)
    return out_path


def sidebar_from_snapshot(snap, out_path):
    """--use-snapshot: a sidebar JSON built from <snap>.roots.json (or meta.roots) of that snapshot."""
    roots = read_json(snap + ".roots.json")
    if not isinstance(roots, list):
        roots = json.loads(snapshot_meta(snap).get("roots") or "null")
    if not isinstance(roots, list) or not roots:
        raise RuntimeError(f"no roots for {snap}: neither {snap}.roots.json nor meta.roots")
    seen = []
    for r in sorted(roots, key=lambda x: (x.get("teamspace_order") or 0, x.get("root_order") or 0)):
        if r.get("teamspace") not in seen:
            seen.append(r.get("teamspace"))
    req = list(wb_scan.TEAMSPACES)
    out = {"captured_at": None, "capture_local": None, "method": "refresh --use-snapshot: roots of the snapshot",
           "teamspace_order_observed": seen, "teamspace_order_required": req, "order_match": seen == req,
           "roots": [{k: r.get(k) for k in ("id", "title", "teamspace", "teamspace_order", "root_order")}
                     for r in roots if not r.get("in_trash")],
           "source_snapshot": os.path.abspath(snap)}
    write_json(out_path, out)
    return out_path


# ------------------------------------------------------------------ holds and run state
def load_holds(P):
    d = read_json(P.holds, {})
    return d if isinstance(d, dict) else {}


def save_holds(P, holds):
    write_json(P.holds, dict(sorted(holds.items())))


def load_paused(P):
    d = read_json(P.paused_runs, None)
    if not isinstance(d, dict):
        d = {}
    d.setdefault("latest", None)
    d.setdefault("runs", {})
    return d


EVIDENCE_KEPT = ("paused", "superseded")   # WB 14.4: kept until approved or discarded
MAX_SUPERSEDED_KEPT = 3                    # bound: each paused round pins ~0.8 GB (snapshot + staging + block_lines)


def paused_exempt_runs(P):
    """Runs whose evidence prune keeps: the paused run(s) plus the newest MAX_SUPERSEDED_KEPT superseded ones
    (newest by paused_at, so custom --run-id names do not change the order)."""
    runs = load_paused(P)["runs"]
    sup = sorted((r for r, e in runs.items() if e.get("status") == "superseded"),
                 key=lambda r: (str(runs[r].get("paused_at") or ""), r), reverse=True)[:MAX_SUPERSEDED_KEPT]
    return {r for r, e in runs.items() if e.get("status") == "paused"} | set(sup)


def _is_active(o):
    """objects_map row still exported (not source_missing / trashed_via_writeback)."""
    return (o.get("status") or "active") == "active"


def expand_subtree(objects, ids, active_only=False):
    """ids + every objects_map descendant (parent_id chain). active_only: skip rows whose status is not
    'active' (objects gone from Notion), as pause and delete decisions need; holds may include stale rows."""
    kids = {}
    for oid, o in objects.items():
        if o.get("parent_id") and (not active_only or _is_active(o)):
            kids.setdefault(o["parent_id"], []).append(oid)
    out, todo = {}, list(ids)
    while todo:
        i = todo.pop()
        if i in out:
            continue
        out[i] = True
        todo.extend(kids.get(i, []))
    return list(out)


def has_children(objects, nid):
    """True when an ACTIVE objects_map row has `nid` as parent (rows of objects deleted in Notion do not count)."""
    return any(o.get("parent_id") == nid and _is_active(o) for o in objects.values())


def _ttl_passed(ttl, now=None):
    try:
        exp = datetime.datetime.fromisoformat(ttl) if ttl else None
    except ValueError:
        return False
    if exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.astimezone()
    return exp < (now or datetime.datetime.now().astimezone())


def _iso_dt(x):
    try:
        d = datetime.datetime.fromisoformat(x) if isinstance(x, str) and x else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.astimezone()
    return d


def load_page_json(staging_dir):
    out = {}
    p = os.path.join(staging_dir, "page_json.jsonl")
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["notion_id"]] = r
    return out


def baseline_page_json(P, wb_ro):
    """{notion_id: (page_json_sha, renderer_version)}: latest base_history rows, else the last published
    run's staging page_json.jsonl (D-15). Empty when neither exists."""
    base = {}
    if wb_ro is not None:
        try:
            for nid, pjs, rv in wb_ro.execute(
                    "SELECT b.notion_id, b.page_json_sha, b.renderer_version FROM base_history b JOIN "
                    "(SELECT notion_id, max(seq) AS s FROM base_history GROUP BY notion_id) m "
                    "ON m.notion_id=b.notion_id AND m.s=b.seq"):
                base[nid] = (pjs, rv)
        except sqlite3.Error:
            base = {}
    if base:
        return base, "base_history"
    lp = read_json(P.last_published_json, {}) or {}
    run = lp.get("run_id")
    if run:
        pj = load_page_json(os.path.join(P.staging, run))
        if pj:
            return {k: (v.get("page_json_sha"), v.get("renderer_version")) for k, v in pj.items()}, f"page_json:{run}"
    return {}, None


def renderer_baseline(P, wb_ro):
    """(renderer versions accepted by the last published round, source) for D-15. The versions are recorded in
    state/last_published.json by every applied round (also by --approve-run, so an approved renderer upgrade is
    accepted once, whatever base_history rows of held / LOCAL_ONLY / conflicted objects still carry). Fallbacks
    for state written before that: the newest base_history row, then the last published run's page_json."""
    lp = read_json(P.last_published_json, {}) or {}
    rv = lp.get("renderer_version")
    if isinstance(rv, list) and rv:
        return sorted({str(x) for x in rv}), "last_published"
    if wb_ro is not None:
        try:
            row = wb_ro.execute("SELECT renderer_version FROM base_history WHERE renderer_version IS NOT NULL "
                                "ORDER BY rowid DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            return [row[0]], "base_history_newest"
    run = lp.get("run_id")
    if run:
        pj = load_page_json(os.path.join(P.staging, run))
        vs = sorted({v.get("renderer_version") for v in pj.values() if v.get("renderer_version")})
        if vs:
            return vs, f"page_json:{run}"
    return [], None


def _snapshot_blocks_flags(snap, nid):
    """(has child_database, has synced_block original) for page `nid` in the snapshot (immutable)."""
    try:
        con = trash_archive.open_snapshot(snap)
    except Exception:  # noqa: BLE001
        return True, True  # unknown -> treat as not deletable (restore)
    try:
        cdb = syn = False
        for typ, data in con.execute("SELECT type, data FROM blocks WHERE page_id=? AND type IN "
                                     "('child_database','synced_block')", (nid,)):
            if typ == "child_database":
                cdb = True
            else:
                try:
                    if (json.loads(data).get("synced_block") or {}).get("synced_from") is None:
                        syn = True
                except (ValueError, AttributeError):
                    syn = True
        return cdb, syn
    finally:
        con.close()


def deletion_decisions(scan, cfg, mode, objects, page_json, snap, root_ids, wb_ro, run_id):
    """WB §6.0 for the missing notes of `scan`. Returns {"pending": [rows to insert], "restore": [...],
    "hold_ids": {nid: entry}, "pause": reason | None, "new_missing": n}."""
    out = {"pending": [], "restore": [], "hold_ids": {}, "pause": None, "new_missing": 0, "not_exported": [],
           "new_missing_rels": []}
    missing = [m for m in scan["missing"] if m.get("notion_id") and m["rel"].endswith(".md")
               and (m.get("kind") in NOTE_KINDS or not m.get("kind"))]
    if page_json:  # a note whose object is no longer exported (deleted/moved out in Notion) is not a local deletion
        out["not_exported"] = [m for m in missing if m["notion_id"] not in page_json]
        missing = [m for m in missing if m["notion_id"] in page_json]
    # WB 6.2: a pending delete past its TTL (expired now, or marked expired by an earlier round that did not
    # publish the restore) is restored from Notion; it is never a NEW local deletion again (no re-armed hold,
    # not counted for local_mass_delete) until the note is back in the vault
    now = datetime.datetime.now().astimezone()
    expired_ids = set()
    for p in scan.get("pending_deletes", []):
        if _ttl_passed(p.get("ttl_until"), now):
            expired_ids |= {p["notion_id"], *p.get("subtree_ids", [])}
    expired = [m for m in missing if m.get("expired") or m["notion_id"] in expired_ids]
    exp_ids = {m["notion_id"] for m in expired}
    out["restore"] += [dict(m, rule="6.2 pending_delete expired (TTL) -> restored from Notion") for m in expired]
    new = [m for m in missing if not m.get("pending") and m["notion_id"] not in exp_ids]
    out["new_missing"] = len(new)
    out["new_missing_rels"] = [m["rel"] for m in new]   # named in the paused note (WB 14.4 涉及对象)
    if wb_config.mode_rank(mode) < wb_config.mode_rank("plan"):
        out["restore"] += [dict(m, rule="6.0(a) mode=off") for m in new]
        return out
    if len(new) > int(cfg["max_local_deletes"]):
        out["pause"] = (f"local_mass_delete: 本轮新出现的缺失笔记 {len(new)} 个 > max_local_deletes "
                        f"{cfg['max_local_deletes']}（需核对大批删除，本轮既不恢复也不登记删除）")
        return out
    missing_ids = {m["notion_id"] for m in missing}
    new_ids = {m["notion_id"] for m in new}
    rel_of = {m["notion_id"]: m["rel"] for m in missing}
    for m in new:
        nid = m["notion_id"]
        par = (objects.get(nid) or {}).get("parent_id")
        anc_missing = False
        while par:
            if par in new_ids:
                anc_missing = True
                break
            par = (objects.get(par) or {}).get("parent_id")
        if anc_missing:
            continue  # handled with its missing ancestor's subtree
        sub = expand_subtree(objects, [nid], active_only=True)
        why = None
        if m.get("kind") not in ("page", "row"):
            why = "DELETE_UNSUPPORTED:kind_" + str(m.get("kind"))
        elif nid in root_ids:
            why = "DELETE_UNSUPPORTED:sidebar_root"
        elif any(i not in missing_ids for i in sub if i != nid and (objects.get(i) or {}).get("kind") in NOTE_KINDS):
            why = "PARTIAL_DELETE"
        else:
            if any((objects.get(i) or {}).get("kind") == "database" for i in sub if i != nid):
                why = "DELETE_UNSUPPORTED:child_database"
            else:
                cdb, syn = _snapshot_blocks_flags(snap, nid) if snap else (True, True)
                if cdb or syn:
                    why = "DELETE_UNSUPPORTED:" + ("child_database" if cdb else "synced_block_original")
        if why is None:
            base = None
            if wb_ro is not None:
                try:
                    row = wb_ro.execute("SELECT page_json_sha FROM base_history WHERE notion_id=? ORDER BY seq DESC "
                                        "LIMIT 1", (nid,)).fetchone()
                    base = row[0] if row else None
                except sqlite3.Error:
                    base = None
            cur = (page_json.get(nid) or {}).get("page_json_sha")
            if base is None or cur is None or base != cur:
                why = "CONFLICT_DEL_EDIT" if (base and cur) else "CONFLICT_DEL_EDIT:no_base"
        if why is not None:
            for i in sub:
                if i in new_ids:
                    out["restore"].append({"rel": rel_of.get(i), "notion_id": i, "rule": "6.0(d) " + why})
            continue
        ttl = (datetime.datetime.now().astimezone() + datetime.timedelta(days=int(cfg["pending_delete_ttl_days"])))
        subtree = [{"notion_id": i, "rel": rel_of.get(i)} for i in sub]
        out["pending"].append({"notion_id": nid, "rel_path": m["rel"], "subtree_json": json.dumps(
            {"ids": subtree, "high_risk": len(sub) > 1}, ensure_ascii=False), "detected_run": run_id,
            "detected_at": now_iso(), "status": "hold", "ttl_until": ttl.isoformat(timespec="seconds")})
        out["hold_ids"][nid] = {"reason": "pending_delete" + ("(high_risk)" if len(sub) > 1 else ""),
                                "paths": [rel_of[i] for i in sub if rel_of.get(i)]}
    return out


def compute_overrides(scan, page_json=None):
    """(overrides {nid: local_rel}, new_entries [{notion_id, old_rel, local_rel}], close [nid]).

    Open overrides stay while the object's note is still at local_rel (or missing: it is restored there);
    a same-folder rename seen by the scan opens a new one (or replaces the old one); an open override whose
    note is back at another path closes."""
    by_id = {}
    for o in scan.get("overrides_open", []):
        by_id.setdefault(o["notion_id"], o)
    nid_index = scan.get("nid_index", {})
    out, new, close = {}, [], []
    renamed = {r["notion_id"]: r for r in scan.get("renamed_same_folder", [])}
    for nid, r in renamed.items():
        o = by_id.get(nid)
        out[nid] = r["new_rel"]
        if not o or o["local_rel"] != r["new_rel"]:
            new.append({"notion_id": nid, "old_rel": o["old_rel"] if o else r["old_rel"], "local_rel": r["new_rel"]})
    for nid, o in by_id.items():
        if nid in renamed:
            continue
        found = nid_index.get(nid, [])
        if not found or o["local_rel"] in found:
            out[nid] = o["local_rel"]
        elif any(posixpath_dirname(f) != posixpath_dirname(o["local_rel"]) for f in found):
            out[nid] = o["local_rel"]  # moved elsewhere: held as UNSUPPORTED_RENAME, keep the override
        else:
            close.append(nid)  # back at another name in the same folder (e.g. the computed one)
    return out, new, close


def posixpath_dirname(p):
    return p.rsplit("/", 1)[0] if "/" in p else ""


def persist_overrides(db, new, close, run_id, rejected_ids=()):
    """name_overrides after the export (WB 7.4.1/7.4.3): a new same-folder rename opens an override only when
    the exporter honoured it; one the exporter rejected (name clash, other folder, ...) is never stored as open,
    and an open override the exporter now rejects becomes 'rejected' (7.4.3: 冲突时该 override 失效并报告)."""
    rejected_ids = set(rejected_ids)
    with wb_state.transaction(db):
        for e in new:
            db.execute("UPDATE name_overrides SET status='superseded' WHERE notion_id=? AND status='open'",
                       (e["notion_id"],))
            if e["notion_id"] in rejected_ids:
                continue
            db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, notion_title_at_detect, since, status)"
                       " VALUES(?,?,?,?,?,'open')", (e["notion_id"], e["old_rel"], e["local_rel"], None, now_iso()))
        for nid in close:
            db.execute("UPDATE name_overrides SET status='closed' WHERE notion_id=? AND status='open'", (nid,))
        new_ids = {e["notion_id"] for e in new}
        for nid in sorted(rejected_ids - new_ids):
            db.execute("UPDATE name_overrides SET status='rejected' WHERE notion_id=? AND status='open'", (nid,))


def update_pending(db, scan, cfg, decisions, mode):
    """pending_deletes: reappeared -> restored, TTL passed -> expired, new holds inserted."""
    now = datetime.datetime.now().astimezone()
    present = set(scan.get("nid_index", {}))
    changed = {"restored": [], "expired": [], "inserted": []}
    with wb_state.transaction(db):
        for nid, ttl, status in db.execute("SELECT notion_id, ttl_until, status FROM pending_deletes "
                                           "WHERE status IN ('hold','expired')").fetchall():
            if nid in present:  # the note is back (put back by the user, or restored from Notion after expiry)
                db.execute("UPDATE pending_deletes SET status='restored' WHERE notion_id=? AND status=?", (nid, status))
                changed["restored"].append(nid)
                continue
            if status == "hold" and _ttl_passed(ttl, now):
                db.execute("UPDATE pending_deletes SET status='expired' WHERE notion_id=? AND status='hold'", (nid,))
                changed["expired"].append(nid)
        for row in decisions.get("pending", []):
            db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                       "status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                       (row["notion_id"], row["rel_path"], row["subtree_json"], row["detected_run"],
                        row["detected_at"], row["status"], row["ttl_until"]))
            changed["inserted"].append(row["notion_id"])
    return changed


def build_expected_deviations(run_id, scan, paused, holds, overrides_applied, local_modified_extra=(),
                              tombstone_ids=(), conflict_kept=(), local_new_extra=()):
    """reports/<run>/expected_deviations.json in speccheck §6 shape (+ paused, local_modified) — every entry
    satisfies verify_vault's Deviations validation (D report): local_new = user files/folders only, sync
    copies by their Sync names, hold paths = absent old names or the held note itself, local_modified path =
    the note of its id. `conflict_kept` [(nid, [paths])]: objects of this round's CONFLICT_* actions; the
    publisher keeps the local file and does not write Notion's version, so the object is stale exactly like a
    held one and is listed under `paused` (subtree False: its children are published) with its kept vault file
    (the old path of a CONFLICT_MOVED_EDITED) and the absent Notion path."""
    nid_index = scan.get("nid_index", {})
    own = {nid: set(v) for nid, v in nid_index.items()}
    vault = scan.get("vault") or ""

    def hold_entry(nid, e):
        paths = [p for p in (e.get("paths") or []) if p and (p in own.get(nid, set()) or
                                                          not os.path.lexists(os.path.join(vault, *p.split("/"))))]
        return {"notion_id": nid, "subtree": bool(e.get("subtree", True)), "paths": sorted(set(paths))}

    conflict_kept = [(n, ps) for n, ps in conflict_kept if n and n not in paused and n not in holds]
    held = set(paused) | set(holds) | {n for n, _ps in conflict_kept}
    lm, seen = [], set()
    for m in scan.get("local_modified", []):
        nid = m.get("notion_id")
        if nid and m["rel"].endswith(".md") and m["rel"] in own.get(nid, set()) and nid not in held:
            if (nid, m["rel"]) not in seen:
                lm.append({"notion_id": nid, "path": m["rel"]})
                seen.add((nid, m["rel"]))
    for nid, rel in local_modified_extra:
        if nid and nid not in held and (nid, rel) not in seen and rel in own.get(nid, set()):
            lm.append({"notion_id": nid, "path": rel})
            seen.add((nid, rel))
    hold_list = [hold_entry(n, e) for n, e in sorted(holds.items())]
    for nid in sorted(set(tombstone_ids) - set(holds) - set(paused)):
        hold_list.append({"notion_id": nid, "subtree": False, "paths": []})
    return {
        "schema": 1, "run_id": run_id, "generated_by": "refresh.py", "generated_at": now_iso(),
        "local_new": list(scan.get("local_new", [])) + [x for x in local_new_extra
                                                         if x not in set(scan.get("local_new", []))],
        "hold_ids": hold_list,
        "paused": [hold_entry(n, e) for n, e in sorted(paused.items())] +
                  [hold_entry(n, {"subtree": False, "paths": ps}) for n, ps in sorted(_merge_kept(conflict_kept).items())],
        "name_overrides": [{"notion_id": n, "old_rel": o.get("old_rel"), "local_rel": o["local_rel"]}
                           for n, o in sorted(overrides_applied.items())],
        "sync_conflict_copies": [{"path": s["rel"], **({"notion_id": s["notion_id"]} if s.get("notion_id") else {})}
                                 for s in scan.get("sync_conflict_copies", [])],
        "local_modified": sorted(lm, key=lambda x: x["path"]),
    }


def _merge_kept(pairs):
    out = {}
    for n, ps in pairs:
        out.setdefault(n, [])
        out[n] += [p for p in ps if p and p not in out[n]]
    return out


def split_local_new(dev, staged_vault, for_post):
    """(deviations for one verify, [(path, why)] dropped) — r1-n2o-completeness-02.

    The scan lists user files / user-only folders of the REAL vault; the staging verify checks the STAGING vault.
    A user file at a path the exporter now produces (a Notion page created or renamed onto the user's note name, or
    the empty note Obsidian creates when a dangling link is clicked) is a staged Notion note there, so
    verify_vault rejects the entry (has_notion_id) and the whole round would pause as a 'scanner defect' every
    round. The plan already handles that path object-level (CONFLICT_UNTRACKED / overwrite risk -> hold), so the
    staging verify gets local_new WITHOUT every path that exists in the staging vault. The post-publish verify
    keeps such user FILES (the publisher never overwrites them: they are still the user's files there) but not a
    user-only FOLDER the export now fills (after the publish it holds exported notes: folder_holds_has_notion_id)."""
    keep, dropped = [], []
    for rel in dev.get("local_new", []):
        full = os.path.join(staged_vault, *rel.split("/"))
        if os.path.lexists(full) and (not for_post or os.path.isdir(full)):
            dropped.append(rel)
        else:
            keep.append(rel)
    return dict(dev, local_new=keep), dropped


def conflict_kept_paths(plan):
    """[(nid, [kept vault file, Notion's path])] for the plan's CONFLICT_* actions (F: CONFLICT_MOVED_EDITED keeps
    the edited file at `frm`/`from` and does not write `rel`; CONFLICT_BOTH keeps `rel`)."""
    out = []
    for a in plan.get("actions", []):
        if a.get("op") not in CONFLICT_OPS:
            continue
        kept = a.get("frm") or a.get("from") if a.get("op") == "CONFLICT_MOVED_EDITED" else a.get("rel")
        out.append((_nid_of_action(a), [p for p in (kept, a.get("rel")) if p]))
    return out


# ------------------------------------------------------------------ verify / plan parsing
def verify_args(snap, sidebar, vault, out, deviations):
    a = ["--snapshot", snap, "--sidebar", sidebar, "--vault", vault, "--out", out, "--roots-kinds", snap + ".roots.json",
         "--snapshot-immutable"]
    if deviations:
        a += ["--expected-deviations", deviations]
    return a


def block_line_errors(report):
    stats = report.get("block_lines") or {}
    return {name: stats[name] for name in ("untagged_lines", "contiguity_violations", "own_noncontiguous")
            if stats.get(name)}


def read_verify(out_dir):
    v = read_json(os.path.join(out_dir, "verify.json"))
    if not isinstance(v, dict):
        return None
    sbc = v.get("status_by_check")
    if not sbc:
        raw = v.get("checks", {})
        sbc = {k: c.get("status") for k, c in raw.items()} if isinstance(raw, dict) else {c["id"]: c["status"] for c in raw}
    v["_sbc"] = dict(sbc)
    v["_rejected"] = list(((v.get("registered_deviations") or {}).get("rejected_entries")) or [])
    return v


# checks' failure lists (verify_vault 1.3 `ok = not (...)` expressions); anything else is informational
FAIL_KEYS = {
    "C01": ("missing", "extra", "not_a_directory"),
    "C02": ("missing", "missing_not_in_snapshot", "extra", "duplicates", "md_without_notion_id", "row_source_unavailable",
            "orphans_under_in_scope_ancestor", "generated_outside_task_dir"),
    "C03": ("issues", "scope_ids_unverifiable"),
    "C04": ("notion_order_mismatch", "child_link_missing", "child_links_out_of_order", "child_database_render_incomplete",
            "template_section_issues"),
    "C05": ("problems",),
    "C06": ("unresolved", "ambiguous", "embed_subpath_unresolved"),
    "C07": ("missing_references", "sha8_mismatch", "bad_name", "misplaced", "duplicate_content_in_teamspace",
            "cross_teamspace_reference", "snapshot_files_not_referenced"),
    "C08": ("issues",),
    "C09": ("name_issues", "nfc_casefold_collisions", "object_name_collisions"),
    "C10": ("pages_with_missing_blocks", "pages_note_missing"),
}
ID_FIELDS = ("id", "notion_id", "parent", "child", "database", "page")
PATH_FIELDS = ("path", "source", "expected_before", "expected_after", "rel", "file", "note")


class HoldContext:
    """What this round holds (r1-n2o-completeness-01 / -03): ids whose ADD/REPLACE/MOVE were skipped (holds file incl.
    subtrees + CONFLICT_* objects kept at their local file) and the staging manifest {rel: notion_id} (the SPEC path
    the exporter computed for every object, held ones included). A post-verify FAIL item that only names held objects
    is the 'issue caused by a listed object' of WRITEBACK_SPEC 14.0 (INFO, the object is already held), not a defect
    that cannot be located: the verifier misses some of these mappings (a renamed root folder page's sortspec entry
    under its computed file name, an index link to a held object's unpublished descendant)."""

    def __init__(self, held_ids, staged_rel2nid):
        self.held = set(held_ids or ())
        self.staged = dict(staged_rel2nid or {})


def load_staged_index(staging_dir):
    """{rel: notion_id} of staging/<run>/manifest.json (files without an id omitted); {} when unreadable."""
    m = read_json(os.path.join(staging_dir, "manifest.json"), {}) or {}
    out = {}
    if isinstance(m, dict):
        for rel, meta in m.items():
            if isinstance(meta, dict) and meta.get("notion_id"):
                out[rel] = meta["notion_id"]
    return out


def _posix_join(folder, name):
    return name if folder in (None, "", "/") else f"{folder}/{name}"


def _hold_candidate_paths(chk, it):
    """Vault/SPEC paths a FAIL item is about, for the held-object explanation (the item's own subject, never e.g.
    the source note of an unresolved link: a dangling link INTO a held object is caused by the hold)."""
    out = []
    if not isinstance(it, dict):
        return [it] if isinstance(it, str) else []
    if chk == "C06":
        t = it.get("target")
        if isinstance(t, str) and t.strip():
            t = t.strip().lstrip("/").split("#", 1)[0]
            out += [t, t + ".md"] if not t.endswith(".md") else [t]
        return out
    if chk == "C05":
        folder, name = it.get("folder"), it.get("name")
        if isinstance(name, str) and name:
            stem = name[:-3] if name.endswith(".md") else name
            out += [_posix_join(folder, name), _posix_join(folder, stem + ".md"),
                    _posix_join(folder, f"{stem}/{stem}.md")]          # folder-note form of the same object
        elif isinstance(folder, str) and folder not in ("", "/"):
            out.append(f"{folder}/{folder.rsplit('/', 1)[-1]}.md")      # the folder's own folder note
        return out
    if chk == "deviations":                                             # a rejected post-verify entry
        rel = it.get("path")
        if isinstance(rel, str) and rel:
            out.append(rel)
            if rel.endswith(".base"):
                out.append(rel[:-5] + ".md")                            # the database note of its .base
            d = posixpath_dirname(rel)
            while d:
                out.append(f"{d}/{d.rsplit('/', 1)[-1]}.md")            # enclosing folder notes
                d = posixpath_dirname(d)
    return out


def locate_failures(v, vault, path_to_nid, hold_ctx=None, explained=None):
    """(located {nid: [check:list]}, unlocatable [{check, list, item}]) for the FAILing checks of verify.json.
    With `hold_ctx` (HoldContext), an item whose subject maps only to held objects is appended to `explained`
    (a list the caller passes) instead: already held, so neither a new hold nor an unlocatable failure."""
    located, unloc = {}, []
    nid_cache = dict(path_to_nid)

    def nid_for_path(p):
        if not isinstance(p, str) or not p:
            return None
        if p in nid_cache:
            return nid_cache[p]
        nid = None
        full = os.path.join(vault, *p.split("/"))
        if p.endswith(".md") and os.path.isfile(full) and not os.path.islink(full):
            try:
                with open(full, "rb") as fh:
                    nid, _k, _e = wb_scan.read_note_identity(fh.read())
            except OSError:
                nid = None
        nid_cache[p] = nid
        return nid

    def held_subject(chk, it):
        """ids of the held objects the item is about, or None when it names anything else / nothing known."""
        if hold_ctx is None or not hold_ctx.held:
            return None
        ids = set()
        for p in _hold_candidate_paths(chk, it):
            if not isinstance(p, str) or not p:
                continue
            for n in (hold_ctx.staged.get(p), nid_cache.get(p), nid_for_path(p)):
                if n:
                    ids.add(n)
        if isinstance(it, dict) and chk != "C06":
            for f in ID_FIELDS:
                x = it.get(f)
                if isinstance(x, str):
                    try:
                        ids.add(wb_common.norm_notion_id(x))
                    except ValueError:
                        pass
        return ids if ids and ids <= hold_ctx.held else None

    for chk in BLOCKING:
        if v["_sbc"].get(chk) == "PASS":
            continue
        c = (v.get("checks") or {}).get(chk) or {}
        det = c.get("details") or {}
        found_any = False
        for key in FAIL_KEYS.get(chk, ()):
            items = det.get(key)
            if not isinstance(items, list):
                continue
            for it in items:
                found_any = True
                hs = held_subject(chk, it)
                if hs is not None and explained is not None:
                    explained.append({"check": chk, "list": key, "held_ids": sorted(hs),
                                      "item": it if not isinstance(it, dict) else {k: it[k] for k in list(it)[:6]}})
                    continue
                ids = set()
                if isinstance(it, str):
                    n = nid_for_path(it)
                    ids.add(n) if n else None
                elif isinstance(it, dict):
                    for f in ID_FIELDS:
                        x = it.get(f)
                        if isinstance(x, str):
                            try:
                                ids.add(wb_common.norm_notion_id(x))
                            except ValueError:
                                pass
                    for f in PATH_FIELDS + ("paths", "entries"):
                        x = it.get(f)
                        for p in (x if isinstance(x, list) else [x]):
                            n = nid_for_path(p)
                            if n:
                                ids.add(n)
                if ids:
                    for n in ids:
                        located.setdefault(n, []).append(f"{chk}:{key}")
                else:
                    unloc.append({"check": chk, "list": key, "item": it if not isinstance(it, dict) else
                                  {k: it[k] for k in list(it)[:6]}})
        if not found_any:
            unloc.append({"check": chk, "list": None, "item": c.get("summary")})
    for ent in (v.get("_rejected") or []) if hold_ctx is not None else []:   # post-verify rejected entries
        hs = held_subject("deviations", ent)
        if hs is not None and explained is not None:
            explained.append({"check": "deviations", "list": "rejected_entries", "held_ids": sorted(hs), "item": ent})
        else:
            unloc.append({"check": "deviations", "list": "rejected_entries", "item": ent})
    return located, unloc


def override_alias_failures(v, failed, applied):
    """{nid: local_rel} when the ONLY blocking issues of verify.json `v` (failed checks `failed`) are C08
    `aliases_missing_original_title` items on notes of applied name overrides (`applied` {nid: {"local_rel"}});
    {} otherwise, so any other FAIL keeps failing the round."""
    if not applied or list(failed) != ["C08"]:
        return {}
    by_rel = {e.get("local_rel"): nid for nid, e in applied.items() if e.get("local_rel")}
    det = (((v.get("checks") or {}).get("C08") or {}).get("details")) or {}
    out = {}
    for key in FAIL_KEYS["C08"]:
        items = det.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            return {}
        for it in items:
            nid = by_rel.get(it.get("path")) if isinstance(it, dict) else None
            if not nid or it.get("issue") != "aliases_missing_original_title":
                return {}
            out[nid] = it["path"]
    return out


def plan_digest(plan):
    """sha256 of the plan's actions + tombstones without volatile keys (WB 14.4 approve comparison)."""
    acts = []
    for a in plan.get("actions", []):
        acts.append({k: v for k, v in a.items() if k not in VOLATILE_PLAN_KEYS})
    acts.sort(key=lambda a: (str(a.get("rel")), str(a.get("op")), wb_common.canon_json(a)))
    body = {"actions": acts, "tombstones": sorted(plan.get("tombstones", []) or [])}
    return wb_common.sha256_bytes(wb_common.canon_json(body).encode("utf-8"))


def plan_sha_for(plan_path, plan):
    """Value for publish_vault --plan-sha: the plan's embedded plan_sha256 when present, else the file sha."""
    if isinstance(plan, dict) and isinstance(plan.get("plan_sha256"), str):
        return plan["plan_sha256"]
    return wb_common.sha256_file(plan_path)


def plan_counts(plan):
    c = {}
    for a in plan.get("actions", []):
        c[a.get("op")] = c.get(a.get("op"), 0) + 1
    return dict(sorted(c.items()))


def evaluate_plan(plan, cfg, objects, holds, overrides, published_rels, vault):
    """WB §14.3 on a publish plan -> (run_reasons [str], obj_holds {nid: entry}, conflicts [action])."""
    run, obj = [], {}
    acts = plan.get("actions", [])
    conflicts = [a for a in acts if a.get("op") in CONFLICT_OPS]
    # a standing conflict whose copy already exists (F: dedup per (id, staged sha)) was reviewed when it was new;
    # it stays object-level and listed, and never pauses the round again (else one conflict pauses every round)
    new_conf = [a for a in conflicts if not a.get("dedup")]
    thr = int(cfg["conflict_pause_threshold"])
    if new_conf and len(new_conf) >= thr:
        standing = len(conflicts) - len(new_conf)
        run.append(f"conflicts: 本轮新冲突 {len(new_conf)} 个 ≥ conflict_pause_threshold {thr}" +
                   (f"（另有 {standing} 个已有冲突副本的冲突未计入）" if standing else ""))
    for a in new_conf:
        nid = _nid_of_action(a)
        if nid and has_children(objects, nid):
            run.append(f"conflict_on_page_with_children: `{a.get('rel')}`（notion_id {nid}）有子页面")
    old_rels = {o.get("old_rel"): n for n, o in overrides.items() if o.get("old_rel")}
    for a in acts:
        op, nid, rel = a.get("op") or "", _nid_of_action(a), a.get("rel")
        if op in HELD_OPS and nid and nid not in holds:
            # the publisher's own guard (duplicate_id_elsewhere, override_*) found a mapping anomaly (F report)
            obj.setdefault(nid, {"reason": f"plan_skip_held:{a.get('reason') or 'publisher'}", "paths": []})
        if op == "CONFLICT_NOT_FILE" or "ANOMALY" in op or "UNTRACKED" in op:
            if nid:
                e = {"reason": f"plan_{op.lower()}", "paths": []}
                if "UNTRACKED" in op and rel:  # a user file at the Notion path: released once that file is gone
                    e["release_when_absent"] = rel
                obj.setdefault(nid, e)
            else:
                run.append(f"unlocatable_plan_anomaly: {op} `{rel}`")
        if op in WRITE_OPS:
            if nid and nid in holds:
                run.append(f"plan_writes_held_object: {op} `{rel}`（notion_id {nid} 处于 hold）")
            if rel in old_rels and op in ("ADD", "RESTORE", "MOVE"):
                run.append(f"plan_writes_override_old_rel: {op} `{rel}`（命名差异 {old_rels[rel]}）")
            if op == "ADD" and rel and rel not in published_rels and \
                    os.path.lexists(os.path.join(vault, *str(rel).split("/"))):
                if nid:
                    obj.setdefault(nid, {"reason": "overwrite_risk_add_target_exists", "paths": [],
                                         "release_when_absent": rel})
                else:
                    run.append(f"overwrite_risk: ADD 的目标 `{rel}` 已存在但不在 published_files")
    return run, obj, conflicts


# ------------------------------------------------------------------ vault notes and run record
def _code(p):
    p = str(p)
    return f"`` {p} ``" if "`" in p else f"`{p}`"


def _note_path(P, name):
    return wb_common.safe_join(P.vault, f"Notion_Obsidian/运行记录/{name}", allow_final_symlink=False)


def _vault_note(P, name, lines):
    """ADD-only generated note (WB 5.1a / D report: notion_kind generated + generated_by, no notion_id, LF,
    single trailing newline, no wikilinks)."""
    p = _note_path(P, name)
    if os.path.lexists(p):
        return None
    text = "\n".join(lines).rstrip("\n") + "\n"
    text = text.replace("[[", "[ [").replace("]]", "] ]")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    write_text(p, text)
    return p


def _common_sections(status, limit=100):
    out = []
    roots = status.get("new_roots_unshared") or []
    if roots:
        out += ["", "## 新顶层页面未共享给读连接", "",
                "以下侧栏顶层页面读连接无法读取（404），本轮未同步：", ""]
        out += [f"- {_code(r.get('title') or '')}（{r.get('teamspace')}，notion_id {_code(r.get('id'))}）"
                for r in roots[:limit]]
        out += ["", f"处理：{FIX_STEPS_NEW_ROOT}"]
    sc = status.get("sidebar_capture") or {}
    if sc.get("failed"):
        out += ["", "## 侧栏抓取失败（新顶层页面不会被发现）", "",
                f"- 本轮抓取 Notion 侧栏失败（退出码 {sc.get('rc')}），改用旧证据 {_code(sc.get('evidence') or '无')}"
                f"（抓取于 {sc.get('evidence_captured_at') or '未知'}，约 {sc.get('evidence_age_hours', '?')} 小时前，"
                f"{sc.get('roots', '?')} 个顶层页面）",
                f"- 已连续失败 {sc.get('consecutive_failures', 1)} 次" +
                ("（已超过提醒阈值，请尽快处理）" if sc.get("escalated") else ""),
                f"- 处理：{FIX_STEPS_SIDEBAR.format(capture=cli_command('capture_sidebar'))}"]
    mem = status.get("members") or {}
    if mem.get("warn_block_limit"):
        cnt = mem.get("effective")
        out += ["", "## 工作区成员数告警", "",
                f"- 成员数（只计 person）：{'未知' if cnt is None else cnt}" +
                (f"（读取失败：{mem.get('error')}）" if mem.get("error") else ""),
                f"- {BLOCK_LIMIT_WARNING}"]
    return out


REJECTED_WHY = {
    "has_notion_id": "该路径上是 Notion 导出的笔记：你的本地文件与一个 Notion 页面同名（或占用了它改名后的路径）",
    "has_notion_id_field": "该文件带有 notion_id，不是本地新建文件",
    "generated_note": "该文件是工具生成的文件",
    "database_base_file": "该文件是数据库的 .base 文件（数据库文件夹被本地改名/移动）",
    "exporter_attachment_name": "该文件名是导出器的附件命名",
    "tool_file_in_task_dir": "该文件在 Notion_Obsidian/ 工具目录内",
    "teamspace_folder": "该路径是团队协作区文件夹",
    "path_belongs_to_another_notion_id": "这一文件路径已分配给另一篇 Notion 页面",
    "path_is_not_a_note_of_a_held_object": "待保留的路径在本轮指向另一篇未保留的页面",
}


def _rejected_text(e):
    why = str(e.get("reason") or "")
    plain = REJECTED_WHY.get(why) or ("该文件夹内有 Notion 导出的笔记" if why.startswith("folder_holds_") else why)
    return f"{_code(e.get('path') or e.get('notion_id') or '')}（{e.get('category')}）：{plain}"


def pause_details(reasons, *, disappeared=None, objects=None, new_missing_rels=None, rejected=None, pause_next=None,
                  changed_rels=None, plan=None, run_id=None, raced_ids=None, limit=100):
    """[{title, lines, hint?}] for a run-level pause (WB 14.4: 原因、涉及对象的行内代码路径与处理命令; r1-n2o-completeness-08,
    r1-user-journey-10): which pages vanished / which notes were deleted / which file collided / which item failed,
    and what approving would write."""
    keys = {str(r).split(":", 1)[0] for r in reasons}
    out = []

    def cap(lines, total):
        if total > limit:
            lines.append(f"……共 {total} 项，完整列表见库外 reports/{run_id or '<run>'}/")
        return lines

    if "source_mass_trash" in keys and disappeared:
        objs = objects or {}
        lines = []
        for g in disappeared[:limit]:
            nid = g.get("notion_id")
            rel = (objs.get(nid) or {}).get("rel_path")
            lines.append(f"{_code(g.get('title') or '（无标题）')}（notion_id {_code(nid)}" +
                         (f"，库内 {_code(rel)}" if rel else "") + "）")
        out.append({"key": "source_mass_trash", "title": "本轮在 Notion 中消失的页面（库内文件保留，不会删除）",
                    "lines": cap(lines, len(disappeared))})
    if "local_mass_delete" in keys and new_missing_rels:
        out.append({"key": "local_mass_delete", "title": "本轮库内新出现的缺失笔记（被删除或移走）",
                    "lines": cap([_code(r) for r in new_missing_rels[:limit]], len(new_missing_rels)),
                    "hint": "若这些删除是你有意为之，可使用下方“处理”中的命令转为待决删除，之后逐项确认是否在 Notion 中删除。"
                            "选择放行会将这些笔记按 Notion 版本恢复到库内。"})
    if "expected_deviations_rejected_entries" in keys and rejected:
        out.append({"key": "rejected", "title": "校验拒绝的预期偏差条目",
                    "lines": cap([_rejected_text(e) for e in rejected[:limit]], len(rejected)),
                    "hint": "若是本地文件与 Notion 页面同名，请先复制到库外备份，再核对名称和路径。"})
    if "previous_post_verify_unlocatable" in keys and isinstance(pause_next, dict):
        items = pause_next.get("items") or []
        lines = [f"{x.get('check')} {x.get('list') or ''}：{_code(json.dumps(x.get('item'), ensure_ascii=False)[:300])}"
                 for x in items[:limit]]
        out.append({"key": "previous_post_verify_unlocatable",
                    "title": f"上一轮（{pause_next.get('run_id')}）发布后复验失败、无法定位到对象的项目",
                    "lines": cap(lines, len(items)) or [f"检查：{pause_next.get('checks')}"],
                    "hint": f"详情见库外 reports/{pause_next.get('run_id')}/post_publish_verify/verify.md；"
                            "确认无误后放行本轮，或放弃本轮并修正后等待下一轮。"})
    if "source_mass_change" in keys and changed_rels:
        out.append({"key": "source_mass_change", "title": "本轮在 Notion 中有改动的页面",
                    "lines": cap([_code(r) for r in changed_rels[:limit]], len(changed_rels))})
    if "apply_race" in keys:
        ids = [str(x) for x in (raced_ids or [])]
        lines = [f"{_code((objects or {}).get(nid, {}).get('rel_path') or '（路径未知）')}（notion_id {_code(nid)}）"
                 for nid in ids[:limit]]
        out.append({"key": "apply_race:" + ",".join(ids),
                    "title": "执行期间检测到库内文件竞态（已写入部分文件，剩余写入已停止）",
                    "lines": cap(lines, len(ids)) or ["（未能从计划定位竞态对象）"],
                    "hint": "请先检查本轮报告和库内变动，再等待下一轮刷新。不要重复放行同一轮。"})
    if plan is not None and "retained_path_reused" not in keys:
        acts = [a for a in plan.get("actions", []) if a.get("op") in WRITE_OPS or a.get("op") == "ADD"]
        tomb = list(plan.get("tombstones") or [])
        lines = [f"{a.get('op')} {_code(a.get('rel'))}" + (f"（原路径 {_code(a.get('frm'))}）" if a.get("frm") else "")
                 for a in acts[:limit]]
        lines = cap(lines, len(acts))
        lines += [f"tombstone（Notion 中已不存在，库内保留）{_code(t)}" for t in tomb[:20]]
        title = ("中止时的发布计划（实际写入见发布日志）" if "apply_race" in keys
                 else "放行后将写入库内的改动（按 Notion 版本）")
        out.append({"key": "would_write", "title": title,
                    "lines": lines or ["（无写入：只更新同步记录）"]})
    return out


def runlog_notes(status, plan=None, scan=None, limit=100):
    """Sections for publish_vault --runlog-notes: warnings of a PUBLISHED round that the user, who works in the vault,
    must see (G-refresh.md request to F, L-13; WB §9.3; r1-n2o-completeness-06/-07, r1-operability-02/-03).
    Each note has a stable `key` that enters the L-12 standing digest, so an unchanged warning is not repeated in a
    new run log every round, while a changed one is."""
    notes = []
    roots = status.get("new_roots_unshared") or []
    if roots:
        notes.append({"key": "new_roots_unshared:" + ",".join(sorted(str(r.get("id")) for r in roots)),
                      "title": "新顶层页面未共享给读连接",
                      "lines": [f"{_code(r.get('title') or '')}（{r.get('teamspace')}，notion_id {_code(r.get('id'))}）"
                                for r in roots[:limit]] + [f"处理：{FIX_STEPS_NEW_ROOT}"]})
    sc = status.get("sidebar_capture") or {}
    if sc.get("failed"):
        notes.append({"key": f"sidebar_capture_failed:{sc.get('evidence')}:{bool(sc.get('escalated'))}",
                      "title": "侧栏抓取失败（新顶层页面不会被发现）",
                      "lines": [f"本轮改用旧证据 {_code(sc.get('evidence') or '无')}（约 {sc.get('evidence_age_hours', '?')} "
                                f"小时前，{sc.get('roots', '?')} 个顶层页面），已连续失败 {sc.get('consecutive_failures', 1)} 次",
                                f"处理：{FIX_STEPS_SIDEBAR.format(capture=cli_command('capture_sidebar'))}"]})
    mem = status.get("members") or {}
    if mem.get("warn_block_limit"):
        cnt = mem.get("effective")
        notes.append({"key": f"warn_block_limit:{cnt}", "title": "工作区成员数告警",
                      "lines": [f"成员数（只计 person）：{'未知' if cnt is None else cnt}", BLOCK_LIMIT_WARNING]})
    coll = status.get("local_new_collisions") or []
    if coll:
        held = {}
        for a in (plan or {}).get("actions", []):
            if a.get("rel") in coll:
                held[a.get("rel")] = _nid_of_action(a)
        notes.append({"key": "local_new_collisions:" + ",".join(sorted(coll)),
                      "title": "本地文件占用了 Notion 页面的路径（该页面暂停同步，你的文件不会被覆盖）",
                      "lines": [f"{_code(c)}" + (f"（Notion 页面 notion_id {_code(held[c])}）" if held.get(c) else "")
                                for c in coll[:limit]] +
                               ["处理：把你的文件改名或移到别处（若是点击失效链接自动生成的空笔记，可直接删除），"
                                "下一轮会自动同步该 Notion 页面。"]})
    names = (status.get("scan_lists") or {}).get("undecodable_names") or []
    if names:
        notes.append({"key": "undecodable_names:" + ",".join(sorted(names)),
                      "title": "文件名不是有效的 UTF-8（工具不会处理这些文件）",
                      "lines": [_code(n) for n in names[:limit]] +
                               ["处理：在文件管理器中把它们改成正常的文件名（常见于从 GBK 编码的 zip 解压的文件）。"]})
    unread = (scan or {}).get("unreadable") or []
    if unread:
        notes.append({"key": "unreadable:" + ",".join(sorted(u["rel"] for u in unread)),
                      "title": "无法读取的文件（权限不足等；已发布的笔记本轮暂停同步）",
                      "lines": [f"{_code(u['rel'])}：{u.get('error')}" for u in unread[:limit]] +
                               ["处理：修正文件权限（例如 chmod 644）后，下一轮自动恢复。"]})
    canonical = (status.get("discarded_held_redirect") or {}).get("targets", {})
    if canonical:
        locations = {_nid_of_action(a): a.get("rel") for a in (plan or {}).get("actions", [])
                     if a.get("op") in HELD_OPS and _nid_of_action(a) in canonical}
        notes.append({"key": "held_links_at_source:" + ",".join(f"{n}>{locations.get(n)}" for n in sorted(canonical)),
                      "title": "保留笔记的链接使用 Notion 当前路径",
                      "lines": [f"链接指向 {_code(locations.get(n))}。请从文件列表打开保留笔记 {_code(canonical[n])}"
                                f"（notion_id {_code(n)}）。" for n in sorted(canonical)[:limit]]})
    moved = [a for a in (plan or {}).get("actions", []) if a.get("op") == "CONFLICT_MOVED_EDITED"]
    if moved:
        verified_targets = status.get("link_targets") or {}
        lines = []
        for action in moved[:limit]:
            nid = _nid_of_action(action)
            kept = verified_targets.get(nid) or action.get("frm") or action.get("from")
            help_text = ("请从目录索引打开保留的笔记。" if nid in verified_targets
                         else "请在文件列表中打开该保留路径，并核对冲突副本。")
            lines.append(f"Notion 的新路径 {_code(action.get('rel'))} 尚未写入库内；你修改过的笔记仍在 "
                         f"{_code(kept)}。{help_text}")
        notes.append({"key": "conflict_moved_edited:" + ",".join(sorted(f"{_nid_of_action(a)}>{a.get('rel')}"
                                                                         for a in moved)),
                      "title": "冲突期间保留原笔记的路径（Notion 改名或移动了你修改过的笔记）",
                      "lines": lines})
    return notes


REDIRECT_RECOVERY = ("冲突未解决前，每一轮都会停下。请按《同步相通执行文档》第 6.3 节先将本地笔记复制到库外备份，"
                     "核对保留路径和 notion_id；若目录页与子笔记分处两个位置，按该节撤回冲突改名，"
                     "待刷新通过后再恢复独立正文。详细报告见 reports/{run}/run_record.md。")

RETAINED_PATH_RECOVERY = ("其他页面也会暂停。请把保留笔记和已有的 Sync 冲突副本复制到库外备份，按 notion_id 核对两篇页面，"
                          "在 Notion 给占用旧名的同级页面改用唯一名称，再等待下一轮刷新。详见《同步相通执行文档》第 6.3 节。")

REDIRECT_APPLY_RECOVERY = ("本轮尚未向库内发布笔记。请等待下一轮重新扫描文件。若再次出现，请按《同步相通执行文档》"
                           "第 6.3 节先将相关笔记复制到库外备份，再核对路径和 notion_id。")

FAILURE_HELP = (
    ("无法读取保留位置的笔记", "发布前复核时保留笔记已移走或无法读取。{detail}", REDIRECT_APPLY_RECOVERY),
    ("移动后链接的文件身份已变化", "发布前复核时保留笔记的身份已改变。{detail}", REDIRECT_APPLY_RECOVERY),
    ("移动后笔记链接的暂存校验失败", "{detail}", REDIRECT_RECOVERY),
    ("无法确认移动后笔记的唯一位置", "{detail}", REDIRECT_RECOVERY),
    ("移动后笔记路径冲突", "{detail}", REDIRECT_RECOVERY),
    ("重定向导出", "{detail}", REDIRECT_RECOVERY),
    ("missing standing approval", "缺少本地同步发布的长期批准文件，已在读取快照和发布笔记前停止。",
     f"请维护者从备份恢复 approvals/{STANDING_APPROVAL} 后等待下一轮刷新。"),
    ("blocking checks failed", "暂存库校验未通过（{detail}），本轮没有向库内写入任何笔记。",
     "查看库外 reports/{run}/verify/verify.md 了解哪项检查失败；下一轮会自动重试，连续两次失败请把该报告交给维护者。"),
    ("staging verify", "暂存库校验没有产生结果，本轮没有向库内写入任何笔记。",
     "查看库外日志 logs/refresh_{run}.log；下一轮会自动重试。"),
    ("export failed", "把 Notion 快照导出为笔记时失败，本轮没有向库内写入任何笔记。",
     "查看库外日志 logs/refresh_{run}.log；下一轮会自动重试，连续失败请交给维护者。"),
    ("snapshot failed", "读取 Notion 内容（快照）失败，可能是网络或 Notion 接口暂时不可用。",
     "检查网络后等待下一轮自动重试（每 6 小时一次），无需操作库内文件。"),
    ("no usable sidebar evidence", "没有可用的 Notion 侧栏证据，无法确定要同步的顶层页面。", FIX_STEPS_SIDEBAR),
    ("publish plan failed", "生成发布计划失败，本轮没有向库内写入任何笔记。",
     "查看库外日志 logs/refresh_{run}.log；下一轮会自动重试。"),
    ("publish failed", "写入库内时失败（{detail}），部分笔记可能已写入，写入前均有备份。",
     "查看库外 reports/{run}/publish_journal.jsonl 与日志；下一轮会重新比较并继续，需要撤销已发布的改动时，请把该轮次的发布记录交给维护者核对。"),
    ("--use-snapshot refused", "指定的快照文件不可用（未完成或不是快照）。",
     "检查 --use-snapshot 的路径是否写对，或改用已完成的快照。"),
    ("SYNC_SETTLING", "库内文件在扫描期间持续变化（Obsidian Sync 可能正在同步），本轮放弃。",
     "等待同步完成；下一轮会自动重试，无需操作。"),
    ("run id", "运行编号不可用。", "换一个未使用过的 --run-id。"),
)


def explain_failure(status):
    """(Chinese reason, next step) for a failed round's vault note (r1-operability-08); the raw error stays below."""
    err = str(status.get("error") or "")
    run = status.get("run_id") or "<run>"
    if not err and status.get("post_verify_failed"):
        unloc = status.get("post_verify_unlocatable") or []
        return (f"已发布，但发布后复验未通过（{status.get('post_verify_failed')}）。",
                (f"能定位到的对象已暂停自动同步（见下）；{'无法定位的项目会让下一轮整轮暂停等待你放行，' if unloc else ''}"
                 f"详情见库外 reports/{run}/post_publish_verify/verify.md。"))
    for needle, why, step in FAILURE_HELP:
        if needle in err:
            detail = err.split(":", 1)[1].strip() if ":" in err else err
            reason = why.format(detail=detail[:200], run=run)
            if step == REDIRECT_APPLY_RECOVERY:
                reason = why.format(detail=err, run=run)
            if step == REDIRECT_RECOVERY:
                reason = err
                targets = status.get("redirect_failed_targets") or {}
                if targets:
                    examples = "、".join(f"{_code(rel)}（notion_id {_code(nid)}）" for nid, rel in list(targets.items())[:3])
                    reason = f"{reason} 本次重定向涉及 {len(targets)} 个对象：{examples}。完整清单见 reports/{run}/link_targets.json。"
            return reason, step.format(run=run, capture=cli_command("capture_sidebar"))
    return ("程序遇到意外错误，本轮未完成。", f"查看库外日志 logs/refresh_{run}.log；下一轮会自动重试，连续失败请交给维护者。")


def write_paused_note(P, run_id, status):
    reasons = status.get("pause_reasons") or []
    result_text = "本轮已部分写入，剩余写入已停止" if status.get("partially_applied") else "本轮未向库内写入任何笔记"
    lines = ["---", "notion_kind: generated", "generated_by: refresh.py", f'run_id: "{run_id}"', "---", "",
             f"# 本轮已暂停：{run_id}", "",
             f"- 结果：paused_for_review（{result_text}）",
             f"- 时间：{status.get('finished') or now_iso()}", "- 原因："]
    lines += [f"  - {r}" for r in reasons[:100]] or ["  - （见状态文件）"]
    for d in status.get("pause_details") or []:
        lines += ["", f"## {d['title']}", ""] + [f"- {x}" for x in d.get("lines") or []]
        if d.get("hint"):
            lines += ["", f"处理：{d['hint']}"]
    objs = status.get("pause_objects") or []
    if objs:
        lines += ["", "## 涉及对象（本轮新冲突）", ""]
        lines += [f"- {_code(o.get('rel') or '')}" + (f"（notion_id {_code(o['notion_id'])}）" if o.get("notion_id") else "")
                  for o in objs[:100]]
        if len(objs) > 100:
            lines.append(f"- ……共 {len(objs)} 个，完整列表见库外 reports/{run_id}/run_record.md")
    lines += _common_sections(status)
    if any(str(r).startswith("retained_path_reused:") for r in reasons):
        lines += ["", "## 处理", "", RETAINED_PATH_RECOVERY,
                  f"库外完整报告见 {_code(f'reports/{run_id}/run_record.md')}。"]
        return _vault_note(P, f"{run_id}-已暂停.md", lines)
    if status.get("partially_applied") or any(str(r).partition(":")[0] == "apply_race" for r in reasons):
        lines += ["", "## 处理", "",
                  f"- 核对 {_code(f'reports/{run_id}/publish_journal.jsonl')} 中实际写入的文件，保留当前库内编辑。",
                  "- 等待下一轮刷新重新扫描；如对象仍处于暂停状态，查看该对象的新报告。"]
        return _vault_note(P, f"{run_id}-已暂停.md", lines)
    lines += ["", "## 处理", "",
              f"- 详情：库外 {_code(f'reports/{run_id}/run_record.md')}、{_code(f'reports/{run_id}/refresh_status.json')}",
              f"- 放行（本人在普通终端运行，按 Notion 版本发布）：{_code(cli_command('refresh', '--approve-run', run_id, work=P.work, vault=P.vault))}",
              f"- 放弃本轮：{_code(cli_command('refresh', '--discard-run', run_id, work=P.work, vault=P.vault))}"]
    if any(str(r).startswith("local_mass_delete") for r in reasons):
        lines.append(f"- 或把本轮的本地删除转为待决删除（hold）：{_code(cli_command('writeback', 'hold', '--run', run_id, work=P.work, vault=P.vault))}")
    lines += ["- 放行前请尽快决定：暂停后若库内又有改动，重新生成的计划会不同，放行会被拒绝（届时放弃本轮并等待下一轮）。",
              "- 暂停期间本轮的暂存库、快照与报告不会被清理。"]
    return _vault_note(P, f"{run_id}-已暂停.md", lines)


def write_failure_note(P, run_id, status, name=None):
    """Visible alert inside the vault (ADD-only) when a round fails or post-verify fails: a Chinese reason, the next
    step (## 处理) and the raw technical message (r1-operability-08)."""
    try:
        why, step = explain_failure(dict(status, run_id=run_id))
        raw = status.get("error") or status.get("post_verify_failed") or "见日志"
        lines = ["---", "notion_kind: generated", "generated_by: refresh.py", f'run_id: "{run_id}"', "---", "",
                 f"# 运行未完成：{run_id}", "", f"- 结果：{status.get('result')}",
                 f"- 原因：{why}",
                 f"- 技术信息：{_code(str(raw)[:300])}",
                 f"- 日志：库外 logs/refresh_{run_id}.log；状态：reports/{run_id}/refresh_status.json"]
        if status.get("partially_applied"):
            lines += ["", "## 写入状态", "", "- 本轮已部分写入，剩余写入已停止"]
        for d in status.get("pause_details") or []:
            lines += ["", f"## {d['title']}", ""] + [f"- {x}" for x in (d.get("lines") or [])[:100]]
            if d.get("hint"):
                lines += ["", f"处理：{d['hint']}"]
        holds = status.get("post_verify_holds") or []
        if holds:
            lines += ["", "## 已暂停自动同步的对象（auto_sync_hold）", ""]
            lines += [f"- notion_id {_code(h)}" for h in holds[:100]]
        unloc = status.get("post_verify_unlocatable") or []
        if unloc:
            lines += ["", "## 复验失败但无法定位到对象的项目（下一轮会整轮暂停等待放行）", ""]
            lines += [f"- {x.get('check')} {x.get('list') or ''}：{_code(json.dumps(x.get('item'), ensure_ascii=False)[:300])}"
                      for x in unloc[:50]]
        lines += _common_sections(status)
        lines += ["", "## 处理", "", f"- {step}",
                  f"- 详情：库外 {_code(f'reports/{run_id}/run_record.md')}"]
        return _vault_note(P, name or f"{run_id}-未完成.md", lines)
    except Exception:  # noqa: BLE001
        return None


def write_run_record(P, run_id, status):
    """reports/<run>/run_record.md (outside the vault): everything a reviewer needs for this round."""
    L = [f"# refresh 运行记录 {run_id}", "",
         f"- 结果：{status.get('result')}" + (f"（{status.get('error')}）" if status.get("error") else ""),
         f"- 模式：{status.get('mode')}；生效写回模式：{status.get('effective_mode')}；dry-run：{bool(status.get('dry_run'))}",
         f"- 快照：{status.get('snapshot')}；侧栏：{status.get('sidebar')}",
         f"- 开始 {status.get('started')}，结束 {status.get('finished')}"]
    if status.get("error"):
        reason, step = explain_failure(dict(status, run_id=run_id))
        L.extend([f"- 原因：{reason}", f"- 处理：{step}"])
    if status.get("pause_reasons"):
        L += ["", "## 暂停原因", ""] + [f"- {r}" for r in status["pause_reasons"]]
    if status.get("would_pause"):
        L += ["", "## 将会暂停（dry-run）", ""] + [f"- {r}" for r in status["would_pause"]]
    h = status.get("holds") or {}
    if h.get("this_round"):
        L += ["", "## 跳过（暂停/保留）的对象", "", "| notion_id | 原因 | 来源 | 路径 |", "| --- | --- | --- | --- |"]
        for nid, e in sorted(h["this_round"].items()):
            L.append(f"| {nid} | {e.get('reason')} | {e.get('source')} | {', '.join(e.get('paths') or [])} |")
    dl = status.get("deletions") or {}
    if dl.get("pending") or dl.get("restore"):
        L += ["", "## 库内删除", ""]
        L += [f"- 待决删除（hold）：`{p}`" for p in dl.get("pending", [])]
        L += [f"- 按 Notion 恢复：`{r.get('rel')}`（{r.get('rule')}）" for r in dl.get("restore", [])]
    ov = status.get("overrides") or {}
    if ov.get("applied") or ov.get("rejected"):
        L += ["", "## 命名差异", ""] + [f"- `{v}`（notion_id {k}）" for k, v in sorted((ov.get("applied") or {}).items())]
    if ov.get("rejected"):
        L += [f"- 导出器拒绝（命名差异失效，本轮该对象保留未发布）：`{r.get('local_rel')}`"
              f"（{r.get('reason')}，notion_id {r.get('notion_id')}）" for r in ov["rejected"]]
    if status.get("override_alias_holds"):
        L += ["", "## 命名差异缺少 aliases（导出器缺口，本轮对象级保留）", ""] + \
             [f"- notion_id {n}" for n in status["override_alias_holds"]]
    if status.get("retained_path_reused"):
        L += ["", "## 旧路径被另一篇页面使用", "", RETAINED_PATH_RECOVERY]
    conf = status.get("conflicts") or []
    if conf:
        L += ["", "## 冲突（库内文件保持不动）", ""]
        L += [f"- {c.get('op')} `{c.get('kept') or c.get('rel')}`（notion_id {c.get('notion_id')}）"
              + ("：此前已写过冲突副本（未计入暂停阈值）" if c.get("dedup") else "：本轮新冲突") for c in conf[:500]]
    sc = status.get("scan_lists") or {}
    for key, title in (("sync_conflict_copies", "Sync 冲突副本"), ("duplicates", "同一 notion_id 多个文件"),
                       ("moved_other_folder", "跨文件夹移动（不支持，已保留）"), ("frontmatter_errors", "frontmatter 无法解析"),
                       ("normalization_mismatch", "文件名 Unicode 规范化不同（NFC/NFD，已保留）")):
        if sc.get(key):
            L += ["", f"## {title}", ""] + [f"- {json.dumps(x, ensure_ascii=False)}" for x in sc[key][:200]]
    L += _common_sections(status, limit=1000)
    src = status.get("source_change") or {}
    if src:
        L += ["", "## 源端变化", "", f"- {json.dumps(src, ensure_ascii=False)}"]
    for k in ("verify", "post_publish_verify"):
        if status.get(k):
            L += ["", f"## {k}", "", f"- {json.dumps(status[k], ensure_ascii=False)}"]
    if status.get("local_new_collisions"):
        L += ["", "## 本地文件占用了 Notion 页面的路径（该页面对象级暂停，文件不覆盖）", ""] + \
             [f"- `{c}`" for c in status["local_new_collisions"][:200]]
    if status.get("holds_released"):
        L += ["", "## 自动解除的保留（占用路径的本地文件已不存在）", ""] + \
             [f"- notion_id {h.get('notion_id')}：`{h.get('path')}`（{h.get('reason')}）" for h in status["holds_released"]]
    for d in status.get("pause_details") or []:
        L += ["", f"## {d['title']}", ""] + [f"- {x}" for x in (d.get("lines") or [])[:1000]]
        if d.get("hint"):
            L += ["", f"处理：{d['hint']}"]
    if status.get("post_verify_explained_by_holds"):
        L += ["", "## 复验问题均由本轮暂停（hold）的对象引起（保留本地版本，复验记为提示）", ""] + \
             [f"- {json.dumps(x, ensure_ascii=False)[:500]}" for x in status["post_verify_explained_by_holds"][:200]]
    if status.get("post_verify_unlocatable"):
        L += ["", "## 复验失败但无法定位到对象（下一轮整轮暂停）", ""] + \
             [f"- {json.dumps(x, ensure_ascii=False)[:500]}" for x in status["post_verify_unlocatable"][:200]]
    os.makedirs(P.report(run_id), exist_ok=True)
    write_text(P.report(run_id, "run_record.md"), "\n".join(L) + "\n")


# ------------------------------------------------------------------ prune
def _staging_time(d):
    """When a staging run was produced: mtime of its manifest.json / export_report.json (written once by the
    exporter, never touched by prune), else of the directory. Run ids are not ordered (--run-id names)."""
    for f in ("manifest.json", "export_report.json"):
        try:
            return os.stat(os.path.join(d, f)).st_mtime
        except OSError:
            continue
    try:
        return os.stat(d).st_mtime
    except OSError:
        return 0.0


def prune(log, P=None, extra_exempt_runs=()):
    """Retention: staging vault copies beyond the newest KEEP_STAGING (plus their block_lines.jsonl and
    page_json.jsonl), snapshots beyond the newest KEEP_SNAPSHOTS. Exempt (WB 14.4): the paused run and the newest
    MAX_SUPERSEDED_KEPT superseded paused runs until approved/discarded; the latest
    and the last-published snapshot, the baseline, and the last published run's page_json.jsonl (D-15)."""
    P = P or Paths()
    paused = load_paused(P)
    kept = paused_exempt_runs(P)
    exempt_runs = kept | set(extra_exempt_runs)
    exempt_snaps = {os.path.abspath(e["snapshot"]) for r, e in paused["runs"].items() if r in kept and e.get("snapshot")}
    lp = read_json(P.last_published_json, {}) or {}
    stg = sorted((d for d in glob.glob(os.path.join(P.staging, "*")) if os.path.isdir(d)),
                 key=lambda d: (_staging_time(d), os.path.basename(d)))
    for d in stg[:-KEEP_STAGING] if KEEP_STAGING else stg:
        name = os.path.basename(d)
        if name in exempt_runs:
            continue
        v = os.path.join(d, "vault")
        if os.path.isdir(v):
            shutil.rmtree(v)
            log.write(f"pruned staging vault copy {v}\n")
        for side in ("block_lines.jsonl", "page_json.jsonl"):
            if side == "page_json.jsonl" and name == lp.get("run_id"):
                continue
            f = os.path.join(d, side)
            if os.path.isfile(f):
                os.unlink(f)
                log.write(f"pruned {f}\n")
    protect = {x for x in (latest_complete_snapshot(P), last_published_snapshot(P)) if x}
    wb_dir = os.path.join(P.snapshots, "wb")
    if os.path.isdir(wb_dir):  # targeted write-back refetch snapshots (H, R-131): older than 7 days
        try:
            import wb_refetch
            for x in wb_refetch.prune_wb_snapshots(wb_dir, keep=sorted(protect | exempt_snaps)):
                log.write(f"pruned write-back snapshot {x}\n")
        except Exception as e:  # noqa: BLE001 - retention must never fail a round
            log.write(f"wb snapshot prune skipped: {type(e).__name__}: {e}\n")
    snaps = sorted(glob.glob(os.path.join(P.snapshots, "snap_*.db")))
    keep = set(snaps[-KEEP_SNAPSHOTS:])
    protect = {os.path.abspath(x) for x in protect} | exempt_snaps
    for s in snaps:
        if s in keep or os.path.abspath(s) in protect or BASELINE_SNAPSHOT in s:
            continue
        for ext in ("", "-wal", "-shm", ".roots.json"):
            if os.path.exists(s + ext):
                os.unlink(s + ext)
        log.write(f"pruned snapshot {s}\n")


KEEP_BASES_WITH_BLOCK_LINES = 2   # latest base + the one before it (planner: latest; rollback window: N-1)
VACUUM_MIN_DAYS = 7
VACUUM_FREE_RATIO = 0.25


def maintain_writeback_db(P, log):
    """Bounded growth of state/writeback.db (r1-operability-13). block_lines rows are kept only for the newest
    KEEP_BASES_WITH_BLOCK_LINES base_history versions of each note (the planner reads the latest base's rows only);
    base_history rows and state/base/<sha> contents are KEPT (WB §3 version chain, §4.2 STALE_REVERT reads every
    REMOTE base). The file is VACUUMed at most every VACUUM_MIN_DAYS days, when >= VACUUM_FREE_RATIO of its pages are
    free. Runs only while no writeback.py command holds the writeback lock (skipped otherwise, never blocks).
    Expected size: ~28 KB of block_lines per note version kept (x 2) + ~1 KB of base_history per version; for the
    5.6k-note vault ≈ 300 MB at most after the first rounds, ~6 KB per changed note version in state/base."""
    if not os.path.exists(P.wb_db):
        return None
    try:
        lk = wb_locks.lock("writeback", state_dir=P.state)
        lk.__enter__()
    except wb_locks.LockBusy:
        log.write("writeback.db maintenance skipped: writeback lock busy\n")
        return {"skipped": "writeback lock busy"}
    out = {"block_lines_deleted": 0, "vacuumed": False}
    try:
        db = wb_state.connect(P.wb_db)
        try:
            keep = set()
            rows = db.execute("SELECT notion_id, seq, sha256 FROM base_history ORDER BY notion_id, seq DESC").fetchall()
            per = {}
            for nid, _seq, sha in rows:
                lst = per.setdefault(nid, [])
                if sha and sha not in lst and len(lst) < KEEP_BASES_WITH_BLOCK_LINES:
                    lst.append(sha)
            for nid, shas in per.items():
                keep.update((nid, x) for x in shas)
            stale = [k for k in db.execute("SELECT DISTINCT notion_id, base_sha FROM block_lines").fetchall()
                     if (k[0], k[1]) not in keep and k[0] in per]
            if stale:
                with wb_state.transaction(db):
                    for nid, bsha in stale:
                        out["block_lines_deleted"] += db.execute(
                            "DELETE FROM block_lines WHERE notion_id=? AND base_sha=?", (nid, bsha)).rowcount
            last = _iso_dt(wb_state.get_meta(db, "last_vacuum_at"))
            due = last is None or (datetime.datetime.now().astimezone() - last).days >= VACUUM_MIN_DAYS
            pages = db.execute("PRAGMA page_count").fetchone()[0] or 0
            free = db.execute("PRAGMA freelist_count").fetchone()[0] or 0
            out.update(pages=pages, free_pages=free)
            if due and pages and free / pages >= VACUUM_FREE_RATIO:
                db.execute("VACUUM")
                out["vacuumed"] = True
                wb_state.set_meta(db, "last_vacuum_at", now_iso())
        finally:
            db.close()
    finally:
        lk.__exit__(None, None, None)
    if out["block_lines_deleted"] or out["vacuumed"]:
        log.write(f"writeback.db maintenance: {out}\n")
    return out


# ------------------------------------------------------------------ one round
class RedirectValidationError(RuntimeError):
    def __init__(self, message, involved=()):
        super().__init__(message)
        self.involved = set(involved)


class Round:
    def __init__(self, P, opts, runner, client_factory, log):
        self.P, self.o, self.run, self.client_factory, self.log = P, opts, runner, client_factory, log
        self.status = {}

    # -- helpers
    def note(self, msg):
        self.log.write(msg.rstrip("\n") + "\n")
        self.log.flush()

    def warn(self, w):
        self.status.setdefault("warnings", [])
        if w not in self.status["warnings"]:
            self.status["warnings"].append(w)

    def publish_args(self, run_id, holds_file, overrides_file, report_dir):
        a = ["--run-id", run_id, "--vault", self.P.vault, "--state", self.P.export_db, "--report-dir", report_dir,
             "--holds", holds_file, "--overrides", overrides_file, "--wb-db", self.P.wb_db]
        if self.P.vault != os.path.abspath(VAULT):
            a.append("--allow-other-vault")
        return a

    def plan(self, run_id, holds_file, overrides_file, report_dir):
        os.makedirs(report_dir, exist_ok=True)
        pp = os.path.join(report_dir, "publish_plan.json")
        if os.path.exists(pp):
            os.replace(pp, pp + ".prev")
        rc = self.run("publish_vault.py", self.publish_args(run_id, holds_file, overrides_file, report_dir))
        plan = read_json(pp)
        if rc != 0 or not isinstance(plan, dict):
            raise RuntimeError(f"publish plan failed (rc={rc})")
        return pp, plan

    def verify_redirected_staging(self, alt, plan, targets, run_id, snap, sidebar, report_dir):
        # Pure staging retains canonical *publish* paths. Check the exact second export's bytes in a
        # read-only verification view with just the retained notes at their post-publish locations.
        view = os.path.join(alt, "verify-vault")
        shutil.copytree(os.path.join(alt, "vault"), view, copy_function=os.link)
        dev = read_json(os.path.join(report_dir, "expected_deviations.json"))
        held = {h["notion_id"] for k in ("hold_ids", "paused") for h in dev[k]}
        for a in plan["actions"]:
            nid = _nid_of_action(a)
            if nid not in targets or a["kind"] not in NOTE_KINDS:
                continue
            old = wb_common.safe_join(view, a["rel"])
            dst = wb_common.safe_join(view, targets[nid])
            if os.path.lexists(dst):
                raise RedirectValidationError(f"移动后笔记路径冲突：{targets[nid]}（{nid}）", [nid])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(old, dst)
            if nid not in held:
                dev["paused"].append({"notion_id": nid, "subtree": False, "paths": [targets[nid]]})
        dev_path = os.path.join(report_dir, "link_target_deviations.json")
        write_json(dev_path, dev)
        out = self.P.report(run_id, "verify_link_targets")
        rc = self.run("verify_vault.py", verify_args(snap, sidebar, view, out, dev_path), timeout=3600)
        verified = read_verify(out)
        if (rc != 0 or verified is None or verified["_rejected"]
                or any(verified["_sbc"].get(k) != "PASS" for k in BLOCKING)):
            checks = (verified or {}).get("checks", {})
            issues = checks.get("C03", {}).get("details", {}).get("issues", []) if isinstance(checks, dict) else []
            child = next((i for i in issues if i.get("issue") == "wrong_location"), None)
            help_text = (f"子笔记 {child.get('path')} 与保留目录页的位置不一致。请先备份本地修改，"
                         "核对目录页改名或移动，并在处理冲突后等待下一轮自动同步。" if child else "")
            involved = {nid for item in issues if item.get("issue") == "wrong_location"
                        for nid in (item.get("id"), item.get("parent")) if nid}
            raise RedirectValidationError(f"移动后笔记链接的暂存校验失败。{help_text}请查看 {os.path.join(out, 'verify.json')}",
                                          involved)
        self.status["link_targets_verify"] = verified["_sbc"]

    def redirect_held_conflict_links(self, run_id, snap, sidebar, overrides_file, report_dir, stg, plan, scan,
                                     published_rel2nid):
        """A held remote move leaves the note at its observed path, even if a personal file occupies the new name."""
        published_notes = {(nid, rel) for rel, nid in published_rel2nid.items() if nid and rel.endswith(".md")}
        published_ids = {nid for nid, _ in published_notes}
        targets = {}
        strict_ids = set()
        for a in plan.get("actions", []):
            op, nid, rel = a.get("op"), _nid_of_action(a), a.get("rel")
            if not nid or a.get("kind") not in NOTE_KINDS:
                continue
            if op != "CONFLICT_MOVED_EDITED" and not (op in HELD_OPS and nid in published_ids
                                                     and (nid, rel) not in published_notes):
                continue
            found = (scan.get("nid_index") or {}).get(nid, [])
            if len(found) != 1:
                if op in HELD_OPS:
                    # A deletion or duplicate holds this object, with no unique local target to advertise.
                    continue
                raise RuntimeError(f"无法确认移动后笔记的唯一位置，请核对 notion_id {nid} 和库内文件 {found!r}")
            if found[0] != rel:
                targets[nid] = found[0]
                if op == "CONFLICT_MOVED_EDITED":
                    strict_ids.add(nid)
        while targets:
            try:
                self.export_redirected_staging(run_id, snap, sidebar, overrides_file, report_dir, stg, plan, targets)
                return targets
            except RuntimeError as exc:
                optional = targets.keys() - strict_ids
                if not optional:
                    self.status["redirect_failed_targets"] = dict(sorted(targets.items()))
                    raise
                # A known bad folder need not displace an independently valid local link. If the
                # cause cannot be isolated, retry only strict conflicts. Every retry removes targets.
                involved = exc.involved if isinstance(exc, RedirectValidationError) else set()
                drop = (optional & involved) or optional
                discarded = self.status.setdefault("discarded_held_redirect", {"targets": {}, "attempts": []})
                parent = os.path.join(report_dir, "discarded_held_redirect")
                os.makedirs(parent, exist_ok=True)
                archive = tempfile.mkdtemp(prefix=f"attempt-{len(discarded['attempts']) + 1}-", dir=parent)
                attempt = os.path.relpath(archive, report_dir)
                for name in ("link_targets.json", "link_target_export_report.json", "link_target_deviations.json",
                             "verify_link_targets"):
                    path = os.path.join(report_dir, name)
                    if os.path.exists(path):
                        os.replace(path, os.path.join(archive, name))
                reason = str(exc).replace(os.path.join(report_dir, "verify_link_targets"),
                                          os.path.join(archive, "verify_link_targets"))
                discarded["attempts"].append({"reason": reason, "report": attempt, "dropped": sorted(drop)})
                for nid in sorted(drop):
                    discarded["targets"][nid] = targets.pop(nid)
                self.note(f"保留笔记的链接采用 Notion 路径，候选校验见 {archive}：{reason}")
        return {}

    def export_redirected_staging(self, run_id, snap, sidebar, overrides_file, report_dir, stg, plan, targets):
        link_file = os.path.join(report_dir, "link_targets.json")
        write_json(link_file, dict(sorted(targets.items())))
        alt = tempfile.mkdtemp(prefix=f".{run_id}.link-targets-", dir=self.P.staging)
        try:
            rc = self.run("export_obsidian.py", ["--snapshot", snap, "--sidebar", sidebar, "--run-id", run_id,
                                                  "--immutable", "--overrides", overrides_file,
                                                  "--link-targets", link_file, "--out", alt], timeout=3600)
            alt_vault = os.path.join(alt, "vault")
            if rc != 0 or not os.path.isdir(alt_vault):
                raise RuntimeError(f"重定向导出失败（退出码 {rc}）。")
            outputs = ("manifest.json", "export_report.json", "page_json.jsonl", "attachments.json", "block_lines.jsonl")
            for name in outputs:
                if not os.path.isfile(os.path.join(alt, name)):
                    raise RuntimeError(f"重定向导出缺少 {name}，已停止本轮发布。")
            export_report = read_json(os.path.join(alt, "export_report.json"))
            if not isinstance(export_report, dict) or not isinstance(export_report.get("block_lines"), dict):
                raise RuntimeError("重定向导出缺少可用的 block_lines 自检结果，已停止本轮发布。")
            write_json(os.path.join(report_dir, "link_target_export_report.json"), export_report)
            violations = block_line_errors(export_report)
            if violations:
                raise RuntimeError(f"重定向导出的行映射自检失败：{violations}。已停止本轮发布。")
            self.verify_redirected_staging(alt, plan, targets, run_id, snap, sidebar, report_dir)
            self.status["link_targets_block_lines"] = export_report["block_lines"]
            shutil.copytree(alt_vault, os.path.join(stg, "vault"), dirs_exist_ok=True)
            for name in outputs:
                shutil.copy2(os.path.join(alt, name), os.path.join(stg, name))
            self.status["link_targets"] = dict(sorted(targets.items()))
        finally:
            shutil.rmtree(alt, ignore_errors=True)

    def apply(self, run_id, holds_file, overrides_file, report_dir, plan_path, plan, approval, notes_file=None,
              link_targets=None):
        if link_targets:
            retained = {_nid_of_action(a) for a in plan["actions"]
                        if a.get("kind") in NOTE_KINDS and (a["op"] in HELD_OPS or a["op"] == "CONFLICT_MOVED_EDITED")}
            for nid, rel in link_targets.items():
                try:
                    path = wb_common.safe_join(self.P.vault, rel, allow_final_symlink=False)
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as fh:
                        data = fh.read() if stat.S_ISREG(os.fstat(fd).st_mode) else b""
                    actual, _, error = wb_scan.read_note_identity(data)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"无法读取保留位置的笔记，请重新刷新：{rel}（{nid}）") from exc
                if nid not in retained or error or actual != nid:
                    raise RuntimeError(f"移动后链接的文件身份已变化，请重新刷新：{rel}（{nid}）")
        a = self.publish_args(run_id, holds_file, overrides_file, report_dir)
        a += ["--apply", "--approval", approval, "--plan-file", plan_path, "--plan-sha", plan_sha_for(plan_path, plan)]
        if notes_file:  # G-refresh.md request to F (L-13): warnings of a published round reach the vault run log
            a += ["--runlog-notes", notes_file]
        return self.run("publish_vault.py", a)

    def raced_ids(self, report_dir, plan):
        rel2nid = {a.get("rel"): _nid_of_action(a) for a in plan.get("actions", [])}
        out = set()
        jp = os.path.join(report_dir, "publish_journal.jsonl")
        try:
            with open(jp, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if str(e.get("op", "")).endswith("_RACE") or e.get("event") == "stopped":
                        nid = e.get("nid") or e.get("notion_id") or rel2nid.get(e.get("rel"))
                        if nid:
                            out.add(nid)
        except OSError:
            pass
        return sorted(out)

    def post_verify(self, run_id, snap, sidebar, deviations, out_dir, path_to_nid, hold_ctx=None):
        """-> (result, failed checks, located ids, unlocatable). With `hold_ctx`, FAIL items (and rejected entries)
        about objects this round already holds are WRITEBACK_SPEC 14.0 INFO (status post_verify_explained_by_holds):
        when nothing else fails the result is "ok" (the caller reports published_with_holds)."""
        self.run("verify_vault.py", verify_args(snap, sidebar, self.P.vault, out_dir, deviations), timeout=3600)
        v = read_verify(out_dir)
        if v is None:
            return "published_post_verify_failed", ["report_missing"], {}, [{"check": None, "item": "verify.json missing"}]
        self.status["post_publish_verify"] = v["_sbc"]
        bad = [c for c in BLOCKING if v["_sbc"].get(c) != "PASS"]
        if v["_rejected"]:
            self.status["post_verify_rejected_entries"] = v["_rejected"][:200]
        if not bad and not v["_rejected"]:
            return "ok", [], {}, []
        if hold_ctx is not None:
            explained = []
            located, unloc = locate_failures(v, self.P.vault, path_to_nid, hold_ctx, explained)
            if explained:
                self.status["post_verify_explained_by_holds"] = explained[:200]
            if not located and not unloc:
                self.note(f"post-publish verify: {len(explained)} FAIL item(s) only about held objects (WB 14.0 INFO)")
                return "ok", [], {}, []
        else:
            located, unloc = locate_failures(v, self.P.vault, path_to_nid) if bad else ({}, [])
            if v["_rejected"]:
                unloc.append({"check": "deviations", "list": "rejected_entries", "item": v["_rejected"][:20]})
        return "published_post_verify_failed", bad or ["rejected_entries"], located, unloc

    def record_post_verify(self, run_id, located, unloc, bad):
        P = self.P
        if located:
            holds = load_holds(P)
            for nid, why in located.items():
                holds.setdefault(nid, {"reason": "post_verify_" + "+".join(sorted({w.split(':')[0] for w in why})),
                                       "run_id": run_id, "since": now_iso(), "subtree": True,
                                       "source": "post_verify", "auto_release": False, "paths": []})
            save_holds(P, holds)
        if unloc:
            write_json(P.pause_next, {"run_id": run_id, "at": now_iso(), "checks": bad,
                                      "items": unloc[:200], "reason": "post_publish_verify_unlocatable"})
        self.status["post_verify_failed"] = bad
        self.status["post_verify_holds"] = sorted(located)
        self.status["post_verify_unlocatable"] = unloc[:200]

    def mark_published(self, run_id, snap, sidebar, plan_path, plan, renderer_version=None):
        """D-17 files. `renderer_version` (the run's page_json versions) is the accepted renderer baseline of the
        next round's D-15 check (an approved renderer upgrade is accepted once, not per object)."""
        P = self.P
        write_text(P.last_published_txt, os.path.abspath(snap) + "\n")
        write_json(P.last_published_json, {"run_id": run_id, "snapshot": os.path.abspath(snap),
                                           "sidebar": sidebar, "plan": plan_path, "plan_digest": plan_digest(plan),
                                           "renderer_version": sorted(renderer_version or []) or None,
                                           "at": now_iso()})
        # a published run supersedes every other paused run: they were all paused before this publish (one
        # round at a time under the lock), whatever their run-id names (approve-run only accepts the latest pause)
        pr = load_paused(P)
        changed = False
        for r, e in pr["runs"].items():
            if e.get("status") == "paused" and r != run_id:
                e["status"], e["decided_at"], e["decided_by"] = "superseded", now_iso(), f"published:{run_id}"
                changed = True
        if changed:
            write_json(P.paused_runs, pr)

    # -- the round
    def execute(self):
        P, o, st = self.P, self.o, self.status
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M")
        run_id = getattr(self, "forced_run_id", None) or o.run_id or f"{ts}-{'T1' if o.mode == 'incremental' else 'T2'}"
        ts = run_id[:13] if re.fullmatch(r"\d{8}T\d{4}", run_id[:13]) else ts
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        st.update({"run_id": run_id, "mode": o.mode, "started": now_iso(), "dry_run": bool(o.dry_run),
                   "use_snapshot": o.use_snapshot or None, "vault": P.vault})
        if o.run_id and (os.path.exists(os.path.join(P.staging, run_id)) or os.path.exists(P.report(run_id))):
            raise RuntimeError(f"run id {run_id} already used (staging/ or reports/ exists)")
        rep = P.report(run_id)
        os.makedirs(rep, exist_ok=True)
        if not o.dry_run and not os.path.isfile(os.path.join(P.approvals, STANDING_APPROVAL)):
            st["missing_standing_approval"] = True
            raise RuntimeError(f"missing standing approval: approvals/{STANDING_APPROVAL}")
        cfg = wb_config.load(P.config)
        self.wb_ro = wb_ro = wb_state.connect_readonly(P.wb_db) if os.path.exists(P.wb_db) else None
        mode = wb_config.effective_mode(cfg, wb_ro)  # None (no writeback.db yet) can only lower the mode
        st["effective_mode"] = mode
        roots_ids = set()
        # 1. inputs: snapshot + sidebar
        if o.use_snapshot:
            snap = os.path.abspath(o.use_snapshot)
            meta = snapshot_meta(snap)
            if not meta.get("finished_at"):
                raise RuntimeError(f"--use-snapshot refused: {snap} has no meta.finished_at (unfinished or not a snapshot)")
            sidebar = os.path.abspath(o.sidebar) if o.sidebar else sidebar_from_snapshot(snap, os.path.join(rep, "sidebar_from_snapshot.json"))
            st.update({"sidebar": sidebar, "sidebar_fresh": None, "snapshot": snap,
                       "members": {"status": "skipped (--use-snapshot)"}})
        else:
            snap = os.path.join(P.snapshots, f"snap_{ts}.db")
            prev = latest_complete_snapshot(P)
            rc = self.run("capture_sidebar.py", [], timeout=300)
            sidebar = latest_sidebar(P)
            st["sidebar"] = os.path.basename(sidebar) if sidebar else None
            st["sidebar_fresh"] = rc == 0
            self._sidebar_capture_status(rc, sidebar)
            if not sidebar:
                raise RuntimeError("no usable sidebar evidence")
            client = None
            try:
                client = self.client_factory(P)
            except Exception as exc:  # noqa: BLE001
                self.warn(f"read client unavailable: {type(exc).__name__}")
                self.note(f"read client unavailable: {exc}")
            mem = member_status(cfg, count_members(client) if client is not None else
                                {"count": None, "status": "unknown", "error": "no read client"})
            st["members"] = mem
            if mem["warn_block_limit"]:
                self.warn("warn_block_limit")
                print(f"警告：{BLOCK_LIMIT_WARNING}（成员数 {mem['effective'] if mem['effective'] is not None else '未知'}）")
            roots_all = (read_json(sidebar, {}) or {}).get("roots", [])
            if client is not None:
                _ok, gone, unknown = check_roots(client, roots_all)
                if unknown:
                    st["roots_check_unknown"] = [{"id": r.get("id"), "error": r.get("error")} for r in unknown]
                if gone:
                    st["new_roots_unshared"] = [{"id": r.get("id"), "title": r.get("title"),
                                                 "teamspace": r.get("teamspace"), "status": r.get("status")} for r in gone]
                    self.warn("new_roots_unshared")
                    sidebar = effective_sidebar(sidebar, {r.get("id") for r in gone},
                                                os.path.join(rep, "sidebar_effective.json"))
                    st["sidebar_effective"] = sidebar
            roots = os.path.join(P.state, f"roots_{ts}.json")
            write_json(roots, (read_json(sidebar, {}) or {})["roots"])
            args = ["--roots", roots, "--db", snap, "--workers", "4", "--rate", "2.5"]
            if o.mode == "incremental" and prev:
                args += ["--incremental-from", prev]
            if self.run("snapshot.py", args, timeout=6 * 3600) != 0:
                raise RuntimeError("snapshot failed")
            self.run("assets_fetch.py", ["--db", snap], timeout=3600)
            st["snapshot"] = snap
            if not o.dry_run:
                self._record_creation_state(mem)
        roots_ids = {r.get("id") for r in ((read_json(sidebar, {}) or {}).get("roots") or [])}
        # 2. source disappearance (trash_archive) vs the last published snapshot
        prev_cmp = last_published_snapshot(P) or (None if o.use_snapshot else latest_complete_snapshot(P))
        src, ta = {}, None
        if prev_cmp and os.path.abspath(prev_cmp) != os.path.abspath(snap):
            excl = self._trashed_via_writeback()
            db = None if o.dry_run else wb_state.connect(P.wb_db)
            try:
                ta = trash_archive.archive(prev_cmp, snap, db, run_id=run_id, exclude_ids=excl, dry_run=bool(o.dry_run))
            finally:
                if db is not None:
                    db.close()
            write_json(os.path.join(rep, "trash_archive.json"), ta)
            src.update({"compared_with": prev_cmp, "disappeared": len(ta["disappeared"]),
                        "disappeared_counted": ta["count_for_pause"], "archived": ta["archived"]})
        st["source_change"] = src
        # 3. read-only vault scan
        scan = wb_scan.scan(P.vault, P.export_db, P.wb_db if os.path.exists(P.wb_db) else None, cfg)
        write_json(os.path.join(rep, "wb_scan.json"), scan)
        st["scan_counts"] = scan["counts"]
        st["scan_lists"] = {k: scan[k][:50] for k in ("sync_conflict_copies", "duplicates", "moved_other_folder",
                                                       "frontmatter_errors", "unsupported_rename", "path_id_mismatch",
                                                       "normalization_mismatch", "unreadable", "moved_database_base",
                                                       "missing_tool_files", "tool_files_unregistered_rels")
                            if scan.get(k)}
        bad_names = scan.get("undecodable_names") or []
        if bad_names:  # never classified by the scan; the verifier sees them as user files (r1-data-safety-04)
            st["scan_lists"]["undecodable_names"] = [u["escaped"] for u in bad_names][:50]
            self.warn("undecodable_names")
        if scan.get("rescanned_after_vanished"):
            st["scan_rescanned"] = scan["rescanned_after_vanished"][:5]
        local_new_extra = [u["rel"] for u in bad_names]
        overrides, new_ovr, close_ovr = compute_overrides(scan)
        ovr_meta = {o_["notion_id"]: o_ for o_ in scan.get("overrides_open", [])}
        for e in new_ovr:
            ovr_meta[e["notion_id"]] = e
        overrides_file = os.path.join(rep, "overrides.json")
        write_json(overrides_file, dict(sorted(overrides.items())))
        # 4. export
        if self.run("export_obsidian.py", ["--snapshot", snap, "--sidebar", sidebar, "--run-id", run_id, "--immutable",
                                           "--overrides", overrides_file]) != 0:
            raise RuntimeError("export failed")
        stg = os.path.join(P.staging, run_id)
        er = read_json(os.path.join(stg, "export_report.json"), {}) or {}
        run_reasons = []
        bad_bl = block_line_errors(er)
        if bad_bl:
            run_reasons.append(f"export_block_lines_selfcheck: {bad_bl}（导出器行映射自检失败）")
        ovr_rep = er.get("overrides") or {}
        rejected_ovr = list(ovr_rep.get("rejected") or [])
        applied = {}
        rej_ids = {r.get("notion_id") for r in rejected_ovr}
        for nid, local_rel in overrides.items():
            if nid not in rej_ids:
                applied[nid] = {"old_rel": (ovr_meta.get(nid) or {}).get("old_rel"), "local_rel": local_rel}
        st["overrides"] = {"applied": {k: v["local_rel"] for k, v in applied.items()}, "rejected": rejected_ovr}
        rej_known = {i for i in rej_ids if i in overrides}
        if not o.dry_run and (new_ovr or close_ovr or rej_known):
            db = wb_state.connect(P.wb_db)  # after the export: only honoured overrides are stored as open (7.4.3)
            try:
                persist_overrides(db, new_ovr, close_ovr, run_id, rejected_ids=rej_known)
            finally:
                db.close()
        page_json = load_page_json(stg)
        current_rv = sorted({r.get("renderer_version") for r in page_json.values() if r.get("renderer_version")}) or \
            ([er["renderer_version"]] if er.get("renderer_version") else [])
        # 5. D-15: renderer version (vs the version the last published round accepted) and source change ratio
        self.current_rv = current_rv
        old_rv, rv_src = renderer_baseline(P, wb_ro)
        base, base_src = baseline_page_json(P, wb_ro)
        src["renderer_version"] = {"current": current_rv, "baseline": old_rv or None, "baseline_source": rv_src}
        if old_rv and current_rv and set(old_rv) != set(current_rv):
            run_reasons.append(f"renderer_version_changed: {old_rv} → {current_rv}（需人工核对后放行）")
        elif base and page_json:
            changed_ids = sorted(nid for nid, r in page_json.items()
                                 if (base.get(nid) or (None,))[0] != r.get("page_json_sha"))
            self.changed_rels = [page_json[n].get("rel_path") or n for n in changed_ids]
            changed = len(changed_ids)
            ratio = changed / len(page_json)
            src.update({"pages": len(page_json), "pages_changed": changed, "ratio": round(ratio, 4),
                        "page_json_baseline_source": base_src})
            if ratio >= float(cfg["source_change_pause_ratio"]):
                run_reasons.append(f"source_mass_change: 源端改动页面 {changed}/{len(page_json)} = {ratio:.1%} ≥ "
                                   f"{cfg['source_change_pause_ratio']:.0%}")
        if src.get("disappeared_counted", 0) >= int(cfg["source_trash_pause_threshold"]):
            run_reasons.append(f"source_mass_trash: 本轮源端消失 {src['disappeared_counted']} 个 ≥ "
                               f"source_trash_pause_threshold {cfg['source_trash_pause_threshold']}")
        pn = read_json(P.pause_next)
        if isinstance(pn, dict):
            run_reasons.append(f"previous_post_verify_unlocatable: 上一轮（{pn.get('run_id')}）发布后复验失败且无法定位到对象")
        # 6. holds: deletions by mode, scan anomalies, rejected overrides, unpublished ids
        _pub, objects = wb_scan.load_export_state(P.export_db)
        published_rel2nid = {r["rel"]: r["notion_id"] for r in _pub if (r["status"] or "active") == "active"}
        dec = deletion_decisions(scan, cfg, mode, objects, page_json, snap, roots_ids, wb_ro, run_id)
        st["deletions"] = {"new_missing": dec["new_missing"], "pending": [r["rel_path"] for r in dec["pending"]][:200],
                           "restore": dec["restore"][:200], "restore_total": len(dec["restore"]),
                           "missing_not_exported": [m["rel"] for m in dec["not_exported"]][:200]}
        if dec["pause"]:
            run_reasons.append(dec["pause"])
        scan_holds = {}
        for nid, e in wb_scan.anomaly_ids(scan).items():  # note-only anomalies (Sync copy, ...) hold no subtree
            scan_holds[nid] = {"reason": e["reason"], "paths": e["paths"], "source": "scan",
                               "subtree": e.get("subtree", True)}
        for r in rejected_ovr:
            nid = r.get("notion_id")
            if nid:
                scan_holds.setdefault(nid, {"reason": f"override_rejected:{r.get('reason')}", "paths": [],
                                            "source": "export"})
        for u in scan.get("unpublished_ids", []):
            pj = page_json.get(u["notion_id"])
            if pj is None or pj.get("rel_path") != u["rel"]:
                scan_holds.setdefault(u["notion_id"], {"reason": "mapping_unpublished_id_elsewhere" if pj else
                                                       "mapping_unknown_notion_id", "paths": [u["rel"]], "source": "scan"})
        holds_state = load_holds(P)
        for nid in [k for k, v in holds_state.items() if v.get("source") in SCAN_HOLD_SOURCES and v.get("auto_release", True)]:
            if nid not in scan_holds:
                holds_state.pop(nid)
        released = []
        for nid, e in list(holds_state.items()):
            tgt = e.get("release_when_absent") if e.get("source") == "plan" else None
            if isinstance(tgt, str) and tgt and not os.path.lexists(os.path.join(P.vault, *tgt.split("/"))):
                holds_state.pop(nid)   # the user's file that occupied the Notion path is gone: publish it again
                released.append({"notion_id": nid, "path": tgt, "reason": e.get("reason")})
        if released:
            st["holds_released"] = released
        for nid, e in scan_holds.items():
            prevh = holds_state.get(nid) or {}
            if prevh and prevh.get("source") not in SCAN_HOLD_SOURCES:
                continue  # a manual-release hold already covers it
            holds_state[nid] = {"reason": e["reason"], "run_id": prevh.get("run_id") or run_id,
                                "since": prevh.get("since") or now_iso(), "subtree": e.get("subtree", True) is not False,
                                "source": e["source"], "auto_release": True, "paths": e["paths"]}
        if not o.dry_run:
            save_holds(P, holds_state)
            db = wb_state.connect(P.wb_db)
            try:
                st["pending_deletes_update"] = update_pending(db, scan, cfg, dec, mode)
            finally:
                db.close()
        pending_holds = {}
        if wb_config.mode_rank(mode) >= wb_config.mode_rank("plan"):
            for p in scan.get("pending_deletes", []):
                if p["notion_id"] not in scan.get("nid_index", {}):
                    try:
                        exp_t = datetime.datetime.fromisoformat(p.get("ttl_until")) if p.get("ttl_until") else None
                    except ValueError:
                        exp_t = None
                    if exp_t is None or exp_t >= datetime.datetime.now().astimezone():
                        pending_holds[p["notion_id"]] = {"reason": "pending_delete", "paths": [p.get("rel_path")]}
            pending_holds.update(dec["hold_ids"])
        for p in scan.get("page_state", []):
            pending_holds.setdefault(p["notion_id"], {"reason": f"page_state_{p['state']}", "paths": []})
        holds_file, publish_holds = self._write_holds_file(rep, holds_state, pending_holds, objects)
        # 7. expected deviations + staging verify
        stg_vault = os.path.join(stg, "vault")
        dev, collided = split_local_new(build_expected_deviations(run_id, scan, holds_state, pending_holds, applied,
                                                                  local_new_extra=local_new_extra), stg_vault, False)
        if collided:
            st["local_new_collisions"] = collided[:200]
        dev_file = os.path.join(rep, "expected_deviations.json")
        write_json(dev_file, dev)
        st["expected_deviations"] = {"file": dev_file, "sha256_staging": wb_common.sha256_file(dev_file)}
        vout = P.report(run_id, "verify")
        self.run("verify_vault.py", verify_args(snap, sidebar, os.path.join(stg, "vault"), vout, dev_file), timeout=3600)
        v = read_verify(vout)
        if v is None:
            raise RuntimeError("staging verify produced no verify.json")
        st["verify"] = v["_sbc"]
        failed = [c for c in BLOCKING if v["_sbc"].get(c) != "PASS"]
        alias = override_alias_failures(v, failed, applied) if failed else {}
        if alias:
            # interim adapter (finding: exporter keeps no `aliases` for an applied name override, WB 7.4.3 / SPEC 4):
            # the ONLY blocking issues are C08 aliases_missing_original_title on override notes -> hold exactly
            # those objects this round (object level, WB 14.3) and re-run the whole-run gate; everything else
            # is published. Recomputed every round; it disappears once B emits the alias or D accepts it.
            for nid, rel in sorted(alias.items()):
                paths = [p for p in (rel, (applied.get(nid) or {}).get("old_rel")) if p]
                prev = holds_state.get(nid)
                if prev:  # already held for another reason whose paths did not name the override note
                    prev["paths"] = list(dict.fromkeys(list(prev.get("paths") or []) + paths))
                    if "override_aliases_missing" not in str(prev.get("reason")).split("+"):
                        prev["reason"] = f"{prev.get('reason')}+override_aliases_missing"
                    continue
                holds_state[nid] = {"reason": "override_aliases_missing", "run_id": run_id, "since": now_iso(),
                                    "subtree": False, "source": "export", "auto_release": True, "paths": paths}
            if not o.dry_run:
                save_holds(P, holds_state)
            holds_file, publish_holds = self._write_holds_file(rep, holds_state, pending_holds, objects)
            dev, _c = split_local_new(build_expected_deviations(run_id, scan, holds_state, pending_holds, applied,
                                                                local_new_extra=local_new_extra), stg_vault, False)
            write_json(dev_file, dev)
            st["expected_deviations"]["sha256_staging"] = wb_common.sha256_file(dev_file)
            st["verify_first"] = v["_sbc"]
            st["override_alias_holds"] = sorted(alias)
            self.warn("override_aliases_missing")
            vout = P.report(run_id, "verify_retry")
            self.run("verify_vault.py", verify_args(snap, sidebar, os.path.join(stg, "vault"), vout, dev_file),
                     timeout=3600)
            v = read_verify(vout)
            if v is None:
                raise RuntimeError("staging verify (retry) produced no verify.json")
            st["verify"] = v["_sbc"]
            failed = [c for c in BLOCKING if v["_sbc"].get(c) != "PASS"]
        if failed:
            raise RuntimeError(f"blocking checks failed: {failed}")
        if v["_rejected"]:
            st["staging_rejected_entries"] = v["_rejected"][:200]
            run_reasons.append(f"expected_deviations_rejected_entries: {len(v['_rejected'])} 条，请核对下方文件路径和页面身份")
        if not o.use_snapshot:
            write_text(P.latest_snapshot, snap)
        # 8. publish plan + §14.3 evaluation
        pp, plan = self.plan(run_id, holds_file, overrides_file, rep)
        shutil.copyfile(pp, os.path.join(rep, "publish_plan.initial.json"))
        r2, obj, conflicts = evaluate_plan(plan, cfg, objects, publish_holds, applied, set(published_rel2nid), P.vault)
        if obj:
            for nid, e in obj.items():
                h = {"reason": e["reason"], "run_id": run_id, "since": now_iso(), "subtree": True, "source": "plan",
                     "auto_release": False, "paths": []}
                if e.get("release_when_absent"):
                    h["release_when_absent"] = e["release_when_absent"]
                holds_state.setdefault(nid, h)
            if not o.dry_run:
                save_holds(P, holds_state)
            holds_file, publish_holds = self._write_holds_file(rep, holds_state, pending_holds, objects)
            pp, plan = self.plan(run_id, holds_file, overrides_file, rep)
            r2, obj2, conflicts = evaluate_plan(plan, cfg, objects, publish_holds, applied, set(published_rel2nid), P.vault)
            if obj2:
                r2.append(f"plan_unstable: 加入对象级保留后计划仍有异常 {sorted(obj2)[:10]}")
        # A new remote owner of the retained path could overwrite or impersonate its current
        # owner through links. A fresh snapshot is required after the competing name is changed.
        protected = set(publish_holds) | {entry.get("notion_id") for entry in scan.get("local_modified", [])}
        reused = []
        for action in plan["actions"]:
            incoming, rel = _nid_of_action(action), action["rel"]
            previous = published_rel2nid.get(rel)
            if (previous in protected and previous != incoming and incoming and action.get("kind") in NOTE_KINDS
                    and previous in page_json and page_json[previous].get("rel_path") != rel
                    and rel in scan.get("nid_index", {}).get(previous, ())):
                reused.append({"path": rel, "retained_id": previous, "incoming_id": incoming})
        if reused:
            st["retained_path_reused"] = reused
            for item in reused:
                run_reasons.append(f"retained_path_reused: {_code(item['path'])} 保留本地页面 {_code(item['retained_id'])}，"
                                   f"另一篇 Notion 页面 {_code(item['incoming_id'])} 使用了旧名。")
            redirected = {}
        else:
            # Include a personal file at the new path: it turns a moved conflict into SKIP_HELD.
            redirected = self.redirect_held_conflict_links(run_id, snap, sidebar, overrides_file, rep, stg, plan, scan,
                                                          published_rel2nid)
        if redirected:
            pp, plan = self.plan(run_id, holds_file, overrides_file, rep)
            write_json(os.path.join(rep, "publish_plan.link_targets.json"), plan)
            r2, obj2, conflicts = evaluate_plan(plan, cfg, objects, publish_holds, applied,
                                                 set(published_rel2nid), P.vault)
            if obj2:
                r2.append(f"plan_unstable: link-target 重导出后仍有异常 {sorted(obj2)[:10]}")
        run_reasons += r2
        st["plan_counts"] = plan_counts(plan)
        st["plan_digest"] = plan_digest(plan)
        tomb_ids = sorted({published_rel2nid.get(t) for t in plan.get("tombstones", []) or [] if published_rel2nid.get(t)})
        kept = [(_nid_of_action(a), a.get("rel")) for a in plan.get("actions", [])
                if a.get("op") in LOCAL_KEPT_OPS and a.get("op") not in CONFLICT_OPS]
        conflict_kept = conflict_kept_paths(plan)
        dev, _dropped = split_local_new(build_expected_deviations(run_id, scan, holds_state, pending_holds, applied,
                                                                  kept, tomb_ids, conflict_kept,
                                                                  local_new_extra=local_new_extra), stg_vault, True)
        # Only post-publish layout may split a held folder note from its unheld children. The
        # canonical folder comes from the verified export; no child is registered as a deviation.
        discarded = st.get("discarded_held_redirect", {}).get("targets", {})
        for cat in ("hold_ids", "paused"):
            for entry in dev[cat]:
                nid = entry["notion_id"]
                if nid in discarded:
                    rel = (page_json.get(nid) or {}).get("rel_path")
                    if rel and posixpath.basename(posixpath.dirname(rel)) + ".md" == posixpath.basename(rel):
                        entry["staged_folder_note"] = rel
        write_json(dev_file, dev)
        st["expected_deviations"]["sha256_post"] = wb_common.sha256_file(dev_file)
        this_round = {}
        for nid, e in publish_holds.items():
            src_e = holds_state.get(nid) or pending_holds.get(nid) or {}
            this_round[nid] = {"reason": e.get("reason"), "source": e.get("source"), "paths": src_e.get("paths") or []}
        top = {k: v for k, v in this_round.items() if not str(v.get("reason", "")).startswith("subtree_of")}
        st["holds"] = {"this_round": dict(sorted(top.items())[:200]), "objects": len(top),
                       "publish_holds_total": len(publish_holds), "file": holds_file}
        kept_of = dict(_merge_kept(conflict_kept))
        st["conflicts"] = [{"rel": a.get("rel"), "notion_id": _nid_of_action(a), "op": a.get("op"),
                            "dedup": bool(a.get("dedup")), "kept": (kept_of.get(_nid_of_action(a)) or [None])[0]}
                           for a in conflicts]
        if run_reasons:
            st["pause_objects"] = [{"rel": a.get("rel"), "notion_id": _nid_of_action(a)} for a in conflicts
                                   if not a.get("dedup")]
            st["pause_details"] = pause_details(
                run_reasons, disappeared=(ta or {}).get("disappeared"), objects=objects,
                new_missing_rels=dec.get("new_missing_rels"), rejected=v["_rejected"],
                pause_next=pn if isinstance(pn, dict) else None, changed_rels=getattr(self, "changed_rels", None),
                plan=plan, run_id=run_id)
            if o.dry_run:
                st["would_pause"] = run_reasons
                st["result"] = "planned"
                return
            self._pause(run_id, run_reasons, snap, sidebar, pp, plan, holds_file, overrides_file, dev_file)
            return
        if o.dry_run:
            st["result"] = "planned"
            return
        # 9. apply exactly that plan
        approval = os.path.join(P.approvals, STANDING_APPROVAL)
        notes = runlog_notes(st, plan, scan)
        notes_file = os.path.join(rep, "runlog_notes.json")
        write_json(notes_file, notes)
        rc = self.apply(run_id, holds_file, overrides_file, rep, pp, plan, approval, notes_file=notes_file,
                        link_targets=redirected)
        if rc == 4:
            raced = self.raced_ids(rep, plan)
            hs = load_holds(P)
            for nid in raced:
                hs.setdefault(nid, {"reason": "apply_race", "run_id": run_id, "since": now_iso(), "subtree": True,
                                    "source": "race", "auto_release": False, "paths": []})
            save_holds(P, hs)
            st["raced_ids"] = raced
            self._pause(run_id, ["apply_race: 执行中出现 SKIP_*_RACE，本轮剩余写入已停止"],
                        snap, sidebar, pp, plan, holds_file, overrides_file, dev_file, partial=True,
                        raced_ids=raced, objects=objects)
            return
        if rc not in (0, 5):
            raise RuntimeError(f"publish failed (rc={rc})")
        # rc 5 = files published, base_history/block_lines import failed: the vault holds this run's content (F: warn)
        self.mark_published(run_id, snap, sidebar, pp, plan, current_rv)
        if rc == 5:
            self.warn("base_history_import_failed")
            st["base_history_error"] = "publish_vault exit 5: base_history / block_lines import failed (files were published)"
        # 10. post-publish verify
        path_to_nid = {}
        for nid, rels in scan.get("nid_index", {}).items():
            for r in rels:
                path_to_nid[r] = nid
        path_to_nid.update({r: n for r, n in published_rel2nid.items() if n})
        hold_ctx = HoldContext(set(publish_holds) | {_nid_of_action(a) for a in conflicts if _nid_of_action(a)},
                               load_staged_index(stg))
        res, bad, located, unloc = self.post_verify(run_id, snap, sidebar, dev_file, P.report(run_id, "post_publish_verify"),
                                                    path_to_nid, hold_ctx)
        if res == "ok":
            st["result"] = "published_with_holds" if (publish_holds or conflicts or
                                                      st.get("post_verify_explained_by_holds")) else "published"
            if os.path.exists(P.pause_next):
                os.unlink(P.pause_next)  # a clean published round settles an older unlocatable failure
        else:
            st["result"] = "published_post_verify_failed"
            self.record_post_verify(run_id, located, unloc, bad)

    # -- pieces
    def _sidebar_capture_status(self, rc, sidebar):
        """r1-n2o-completeness-07 / r1-operability-02: a failed capture (exit != 0) falls back to older evidence, so
        new top-level pages stay invisible: warn with the evidence's age and root count, count consecutive failures
        (state/sidebar_capture_failures.json; escalated from SIDEBAR_FAIL_ALERT on) and surface it in the run
        record and the vault (run log notes / paused / failure notes)."""
        P, st = self.P, self.status
        fpath = os.path.join(P.state, "sidebar_capture_failures.json")
        prev = read_json(fpath, {}) or {}
        if rc == 0:
            if prev and not self.o.dry_run:
                try:
                    os.unlink(fpath)
                except OSError:
                    pass
            return
        n = int(prev.get("consecutive") or 0) + 1 if isinstance(prev, dict) else 1
        d = read_json(sidebar, {}) if sidebar else {}
        d = d if isinstance(d, dict) else {}
        cap = _iso_dt(d.get("captured_at") or d.get("capture_local"))
        if cap is None and sidebar:
            try:
                cap = datetime.datetime.fromtimestamp(os.stat(sidebar).st_mtime).astimezone()
            except OSError:
                cap = None
        age = None if cap is None else round((datetime.datetime.now().astimezone() - cap).total_seconds() / 3600, 1)
        info = {"failed": True, "rc": rc, "evidence": os.path.basename(sidebar) if sidebar else None,
                "evidence_captured_at": cap.isoformat(timespec="minutes") if cap else None,
                "evidence_age_hours": age, "roots": len(d.get("roots") or []), "consecutive_failures": n,
                "since": (prev.get("since") if isinstance(prev, dict) else None) or now_iso(),
                "escalated": n >= SIDEBAR_FAIL_ALERT}
        st["sidebar_capture"] = info
        self.warn("sidebar_capture_failed")
        print(f"警告：侧栏抓取失败（退出码 {rc}，连续 {n} 次），使用旧证据 {info['evidence']}"
              f"（约 {age} 小时前），新顶层页面不会被发现")
        if not self.o.dry_run:
            write_json(fpath, {"consecutive": n, "since": info["since"], "last_rc": rc, "last_at": now_iso()})

    def close(self):
        if getattr(self, "wb_ro", None) is not None:
            self.wb_ro.close()
            self.wb_ro = None

    def _write_holds_file(self, rep, holds_state, pending_holds, objects):
        """reports/<run>/holds.json for publish_vault --holds: {nid: {reason, source, subtree}} incl. subtrees."""
        out = {}
        for nid, e in sorted(holds_state.items()):
            out[nid] = {"reason": e.get("reason"), "source": e.get("source") or "auto_sync_hold",
                        "subtree": e.get("subtree", True) is not False}
        for nid, e in sorted(pending_holds.items()):
            out.setdefault(nid, {"reason": e.get("reason"), "source": "pending_delete"
                                 if str(e.get("reason", "")).startswith("pending_delete") else "page_state",
                                 "subtree": True})
        for nid in [k for k, v in out.items() if v["subtree"]]:  # note-only holds (Sync copy, ...) stay alone
            for d in expand_subtree(objects, [nid]):
                if d != nid and d not in out:
                    out[d] = {"reason": f"subtree_of:{nid}", "source": out[nid]["source"], "subtree": True}
        p = os.path.join(rep, "holds.json")
        write_json(p, out)
        return p, out

    def _trashed_via_writeback(self):
        pub, objs = wb_scan.load_export_state(self.P.export_db)
        ids = {r["notion_id"] for r in pub if r["status"] == "trashed_via_writeback" and r["notion_id"]}
        ids |= {k for k, v in objs.items() if v.get("status") == "trashed_via_writeback"}
        return sorted(ids)

    def _record_creation_state(self, mem):
        db = wb_state.connect(self.P.wb_db)
        try:
            last = db.execute("SELECT members, state FROM creation_state ORDER BY rowid DESC LIMIT 1").fetchone()
            state = mem["creation_state"]
            if last and last[1] == "blocked_confirmed":
                return  # only a person clears a confirmed block (WB 9.2)
            if not last or last[0] != mem.get("effective") or last[1] != state:
                with wb_state.transaction(db):
                    db.execute("INSERT INTO creation_state(ts, members, plan, state, source) VALUES(?,?,?,?,?)",
                               (now_iso(), mem.get("effective"), None, state, "refresh"))
        finally:
            db.close()

    def _pause(self, run_id, reasons, snap, sidebar, plan_path, plan, holds_file, overrides_file, dev_file,
               partial=False, raced_ids=None, objects=None):
        P, st = self.P, self.status
        paused_plan = os.path.join(os.path.dirname(plan_path), "publish_plan.paused.json")
        shutil.copyfile(plan_path, paused_plan)
        st["result"] = "paused_for_review"
        st["pause_reasons"] = reasons
        if partial:
            st["partially_applied"] = True
        if raced_ids:
            st["raced_ids"] = list(raced_ids)
            st["pause_details"] = pause_details(reasons, objects=objects or {}, plan=plan,
                                                  run_id=run_id, raced_ids=raced_ids)
        pr = load_paused(P)
        for r, e in pr["runs"].items():
            if e.get("status") == "paused" and r != run_id:  # only the latest pause is approvable (WB 14.4)
                e.update(status="superseded", decided_at=now_iso(), decided_by=f"paused:{run_id}")
        pr["runs"][run_id] = {"paused_at": now_iso(), "reasons": reasons, "plan": paused_plan,
                              "plan_digest": plan_digest(plan), "snapshot": os.path.abspath(snap), "sidebar": sidebar,
                              "staging": os.path.join(P.staging, run_id), "holds": holds_file,
                              "overrides": overrides_file, "deviations": dev_file, "status": "paused",
                              "renderer_version": list(getattr(self, "current_rv", None) or []) or None,
                              "partially_applied": bool(partial), "decided_at": None, "decided_by": None}
        pr["latest"] = run_id
        write_json(P.paused_runs, pr)
        st["finished"] = now_iso()
        write_paused_note(P, run_id, st)


def _history(P, status):
    os.makedirs(P.logs, exist_ok=True)
    with open(os.path.join(P.logs, "refresh_history.jsonl"), "a", encoding="utf-8") as h:
        h.write(json.dumps(status, ensure_ascii=False, default=str) + "\n")


def run_round(P, opts, runner_factory=None, client_factory=None):
    """One scheduled/manual round. Returns (exit code, status dict)."""
    os.makedirs(P.logs, exist_ok=True)
    try:
        lk = wb_locks.lock("refresh", state_dir=P.state)
        held = lk.__enter__()
    except wb_locks.LockBusy:
        print("another refresh is running; exit")
        _history(P, {"mode": opts.mode, "result": "skipped_lock_busy", "at": now_iso()})
        return EXIT_OK, {"result": "skipped_lock_busy"}
    del held
    try:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M")
        rid = opts.run_id or f"{ts}-{'T1' if opts.mode == 'incremental' else 'T2'}"
        log = open(os.path.join(P.logs, f"refresh_{rid}.log"), "a", encoding="utf-8")
        rnd = Round(P, opts, (runner_factory or Runner)(P, log), client_factory or default_client_factory, log)
        rnd.forced_run_id = rid
        st = rnd.status
        try:
            rnd.execute()
        except Exception as e:  # noqa: BLE001
            st["result"] = "failed"
            st["error"] = str(e)[:500]
            log.write(traceback.format_exc())
        finally:
            rnd.close()
            st.setdefault("run_id", rid)
            st["finished"] = now_iso()
            if not opts.dry_run and not st.get("missing_standing_approval"):  # no cleanup before approval
                try:
                    prune(log, P)
                except Exception as e:  # noqa: BLE001
                    log.write(f"prune error {e}\n")
                try:
                    st["writeback_db_maintenance"] = maintain_writeback_db(P, log)
                except Exception as e:  # noqa: BLE001 - retention must never fail a round
                    log.write(f"writeback.db maintenance error {type(e).__name__}: {e}\n")
            os.makedirs(P.report(st["run_id"]), exist_ok=True)
            write_json(P.report(st["run_id"], "refresh_status.json"), st)
            try:
                write_run_record(P, st["run_id"], st)
            except Exception as e:  # noqa: BLE001
                log.write(f"run record error {e}\n")
            if st.get("result") not in OK_RESULTS + ("paused_for_review",) and not opts.dry_run:
                write_failure_note(P, st["run_id"], st)
            _history(P, st)
            log.close()
    finally:
        lk.__exit__(None, None, None)
    print(json.dumps(st, ensure_ascii=False, default=str)[:20000])
    res = st.get("result")
    return (EXIT_OK if res in OK_RESULTS else EXIT_PAUSED if res == "paused_for_review" else EXIT_FAIL), st


# ------------------------------------------------------------------ human decisions
def _published_after(lp, e, run_id):
    """True when last_published.json `lp` records a publish later than the pause `e` of run_id (by time; run ids
    are not ordered once --run-id names exist; legacy files without times fall back to the id order)."""
    a, b = _iso_dt(lp.get("at")), _iso_dt(e.get("paused_at"))
    if a is not None and b is not None:
        return a > b
    return str(lp["run_id"]) > str(run_id)


def _pause_next_covered(pn, e, run_id):
    """refresh_pause_next.json written by run pn["run_id"] at pn["at"] is settled by discarding the pause `e`
    of run_id when it was written before (or by) that pause."""
    if pn.get("run_id") == run_id:
        return True
    a, b = _iso_dt(pn.get("at")), _iso_dt(e.get("paused_at"))
    if a is not None and b is not None:
        return a <= b
    return str(pn["run_id"]) <= str(run_id)


def _gate(action):
    import wb_gate
    try:
        return wb_gate.require_human(action), wb_gate
    except wb_gate.GateRefused as exc:
        print(exc.reason, file=sys.stderr)
        return None, wb_gate


def plan_diff(old_plan, new_plan, limit=20):
    """Lines '`rel`：暂停时 OP → 现在 OP' for the actions (and tombstones) in which two plans differ (r1-user-journey-05)."""
    def norm(a):
        return {k: v for k, v in a.items() if k not in VOLATILE_PLAN_KEYS}
    o, n = {}, {}
    for a in (old_plan or {}).get("actions", []):
        o.setdefault(a.get("rel"), []).append(norm(a))
    for a in (new_plan or {}).get("actions", []):
        n.setdefault(a.get("rel"), []).append(norm(a))
    out = []
    for rel in sorted(set(o) | set(n), key=str):
        a, b = o.get(rel) or [], n.get(rel) or []
        if sorted(map(wb_common.canon_json, a)) == sorted(map(wb_common.canon_json, b)):
            continue
        ops_a = "+".join(sorted(x.get("op") or "?" for x in a)) or "（无）"
        ops_b = "+".join(sorted(x.get("op") or "?" for x in b)) or "（无）"
        why = (b[0].get("reason") if b else "") or ""
        out.append(f"{_code(rel)}：暂停时 {ops_a} → 现在 {ops_b}" + (f"（{why}）" if why and ops_a != ops_b else
                                                                   "（内容或文件已变化）" if ops_a == ops_b else ""))
    to, tn = set((old_plan or {}).get("tombstones") or []), set((new_plan or {}).get("tombstones") or [])
    out += [f"tombstone {_code(t)}：{'新增' if t in tn else '不再出现'}" for t in sorted(to ^ tn)]
    if len(out) > limit:
        out = out[:limit] + [f"……共 {len(out)} 处不同"]
    return out


def _superseded_text(run_id, e, pr):
    by = str(e.get("decided_by") or "")
    if e.get("status") == "superseded" and by.startswith("paused:"):
        newer = by.split(":", 1)[1]
        cur = (pr["runs"].get(newer) or {}).get("status")
        tail = (f"请评审并放行较新的暂停轮次：{cli_command('refresh', '--approve-run', newer)}"
                if cur == "paused" and pr.get("latest") == newer else f"（{newer} 现在的状态：{cur}）")
        return f"拒绝：{run_id} 已被之后暂停的轮次 {newer} 取代。{tail}"
    if e.get("status") == "superseded" and by.startswith("published:"):
        return f"拒绝：{run_id} 已被之后发布的轮次 {by.split(':', 1)[1]} 取代，无需再放行。"
    if e.get("status") == "superseded":
        return f"拒绝：{run_id} 已被取代（{by}），无需再放行。"
    return f"拒绝：{run_id} 已处理（状态 {e.get('status')}，{by}），不能再放行。"


def approve_run(P, run_id, runner_factory=None):
    """--approve-run: only the latest paused run; re-plan (not reuse); a different plan is refused (WB 14.4).
    The re-plan is compared BEFORE the y/N question; a refusal says what differs and what to do next. A newer pause
    or publish is named. Publisher exit codes are handled like a normal round (5: published + warning; 4: race ->
    apply_race holds, the run is superseded), post-verify uses the same held-object attribution, holds/conflicts give
    published_with_holds, and a failure leaves <run>-未完成.md in the vault (r1-n2o-completeness-12, r1-operability-11,
    r1-user-journey-05)."""
    tty, gate = _gate(f"本地刷新操作 --approve-run {run_id}")
    if tty is None:
        return EXIT_GATE
    try:
        lk = wb_locks.lock("refresh", state_dir=P.state)
        lk.__enter__()
    except wb_locks.LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    try:
        pr = load_paused(P)
        e = pr["runs"].get(run_id)
        if not e:
            print(f"拒绝：{run_id} 不是暂停的轮次（paused_runs.json 中没有记录）" +
                  (f"；最近一次暂停的轮次是 {pr.get('latest')}" if pr.get("latest") else ""), file=sys.stderr)
            return EXIT_FAIL
        if e.get("status") != "paused":
            print(_superseded_text(run_id, e, pr), file=sys.stderr)
            return EXIT_FAIL
        if any(str(reason).startswith("retained_path_reused:") for reason in e.get("reasons", [])):
            print(f"拒绝放行 {run_id}：旧路径仍由两个页面共用。{RETAINED_PATH_RECOVERY}", file=sys.stderr)
            return EXIT_FAIL
        if pr.get("latest") != run_id:
            print(f"拒绝：只能放行最近一次被暂停的轮次（{pr.get('latest')}）", file=sys.stderr)
            return EXIT_FAIL
        lp = read_json(P.last_published_json, {}) or {}
        if lp.get("run_id") and lp["run_id"] != run_id and _published_after(lp, e, run_id):
            e.update(status="superseded", decided_at=now_iso(), decided_by=f"published:{lp['run_id']}")
            write_json(P.paused_runs, pr)
            print(f"拒绝：之后的轮次 {lp['run_id']} 已发布，旧的暂存内容不能覆盖新内容（已标为 superseded）", file=sys.stderr)
            return EXIT_FAIL
        paused_st = read_json(P.report(run_id, "refresh_status.json"), {}) or {}
        os.makedirs(P.logs, exist_ok=True)
        log = open(os.path.join(P.logs, f"refresh_{run_id}.approve.log"), "a", encoding="utf-8")
        st = {}
        try:
            opts = argparse.Namespace(mode="incremental", dry_run=False, use_snapshot=None, sidebar=None, run_id=run_id)
            rnd = Round(P, opts, (runner_factory or Runner)(P, log), None, log)
            st = rnd.status
            st.update({"run_id": run_id, "action": "approve-run", "started": now_iso(), "tty": tty})
            rep = P.report(run_id, "approve")
            # re-plan first (read-only: plan files under reports/<run>/approve/), compare, THEN ask (WB 14.4)
            pp, plan = rnd.plan(run_id, e["holds"], e["overrides"], rep)
            if plan_digest(plan) != e.get("plan_digest"):
                e.setdefault("approve_attempts", []).append({"at": now_iso(), "result": "plan_differs"})
                write_json(P.paused_runs, pr)
                diff = plan_diff(read_json(e.get("plan"), {}) or {}, plan)
                print("拒绝：重新生成的发布计划与暂停时不同，需要重新评审。不同之处：", file=sys.stderr)
                for d in diff or ["（动作相同，计划细节不同）"]:
                    print(f"  - {gate.display_safe(d)}", file=sys.stderr)
                print(f"处理：暂停后库内或 Notion 又有改动。请放弃本轮（{cli_command('refresh', '--discard-run', run_id)}），"
                      f"等待下一轮自动刷新（或手动运行一次 {cli_command('refresh', '--mode', 'incremental')}）；若下一轮仍暂停，"
                      "请在它暂停后尽快评审并放行那一轮（放行前不要再修改库内笔记）。", file=sys.stderr)
                return EXIT_FAIL
            saved_targets = read_json(P.report(run_id, "link_targets.json"))
            has_moved_notes = any(a.get("op") == "CONFLICT_MOVED_EDITED" and a.get("kind") in NOTE_KINDS
                                  for a in plan.get("actions", []))
            if saved_targets or paused_st.get("link_targets") or has_moved_notes:
                checks = paused_st.get("link_targets_verify") or {}
                blocks = paused_st.get("link_targets_block_lines")
                if (not saved_targets or saved_targets != paused_st.get("link_targets")
                        or any(checks.get(c) != "PASS" for c in BLOCKING)
                        or not isinstance(blocks, dict) or block_line_errors({"block_lines": blocks})):
                    print(f"拒绝放行 {run_id}：保留路径的暂存校验或行映射自检记录不完整。"
                          "请等待下一轮自动同步重新刷新。", file=sys.stderr)
                    return EXIT_FAIL
            print(f"暂停轮次 {run_id} 的原因：")
            for r in e.get("reasons", []):
                print(f"  - {gate.display_safe(r)}")
            objs = paused_st.get("pause_objects") or []
            if objs:
                print("本轮新冲突（库内文件保持不动，Notion 版本写入冲突副本）：")
                for o in objs[:50]:
                    print(f"  - {gate.display_safe(_code(o.get('rel') or ''))}")
            for d in paused_st.get("pause_details") or pause_details(e.get("reasons", []), plan=plan, run_id=run_id):
                print(f"{gate.display_safe(d.get('title'))}：")
                for x in (d.get("lines") or [])[:50]:
                    print(f"  - {gate.display_safe(x)}")
            if not gate.confirm_yes_no(f"按 Notion 版本放行并发布 {run_id}？"):
                print("已取消。")
                st["result"] = "cancelled"
                return EXIT_FAIL
            ap = os.path.join(P.approvals, f"approve-run-{run_id}.approval.md")
            os.makedirs(P.approvals, exist_ok=True)
            write_text(ap, "\n".join([f"# 授权记录：放行暂停轮次 {run_id}，{now_iso()[:10]}", "",
                                      "来源/依据：", f"- 用户本人在终端 {tty} 运行 {cli_command('refresh', '--approve-run', run_id)} 并确认 y",
                                      "", "据此授权范围（执行代理据实记录，未扩大）：",
                                      f"1. 以暂停时评审过的同一计划（digest {e.get('plan_digest')}）发布轮次 {run_id}",
                                      "", "暂停原因："] + [f"- {r}" for r in e.get("reasons", [])]) + "\n")
            scan = read_json(P.report(run_id, "wb_scan.json"), {}) or {}
            notes_file = os.path.join(rep, "runlog_notes.json")
            write_json(notes_file, runlog_notes(paused_st, plan, scan))
            try:
                rc = rnd.apply(run_id, e["holds"], e["overrides"], rep, pp, plan, ap, notes_file=notes_file,
                               link_targets=paused_st.get("link_targets"))
            except RuntimeError as exc:
                rc = EXIT_FAIL
                st["error"] = str(exc)
            new_status = "approved"
            holds_pub = read_json(e["holds"], {}) or {}
            conflicts = [a for a in plan.get("actions", []) if a.get("op") in CONFLICT_OPS]
            if rc == 4:
                raced = rnd.raced_ids(rep, plan)
                hs = load_holds(P)
                for nid in raced:
                    hs.setdefault(nid, {"reason": "apply_race", "run_id": run_id, "since": now_iso(), "subtree": True,
                                        "source": "race", "auto_release": False, "paths": []})
                save_holds(P, hs)
                race_objects = {_nid_of_action(a): {"rel_path": a.get("rel")}
                                for a in plan.get("actions", []) if _nid_of_action(a) in set(raced)}
                st["pause_details"] = pause_details(
                    ["apply_race: 执行中出现 SKIP_*_RACE，本轮剩余写入已停止"],
                    objects=race_objects, plan=plan, run_id=run_id, raced_ids=raced)
                st.update(result="failed", raced_ids=raced, partially_applied=True,
                          error="publish failed (rc=4): apply_race — 放行发布中出现覆盖风险（SKIP_*_RACE），已写入部分文件后停止")
                new_status = "superseded"   # partly applied: the next round re-plans from the new vault state
            elif rc not in (0, 5):
                st["result"] = "failed"
                st.setdefault("error", f"publish failed (rc={rc})")
                new_status = "paused"
            else:
                rv = e.get("renderer_version") or sorted({r.get("renderer_version") for r in load_page_json(
                    e.get("staging") or os.path.join(P.staging, run_id)).values() if r.get("renderer_version")})
                rnd.mark_published(run_id, e["snapshot"], e["sidebar"], pp, plan, rv)
                if rc == 5:
                    rnd.warn("base_history_import_failed")
                    st["base_history_error"] = ("publish_vault exit 5: base_history / block_lines import failed "
                                                "(files were published)")
                _pub, _o = wb_scan.load_export_state(P.export_db)
                p2n = {r["rel"]: r["notion_id"] for r in _pub if r["notion_id"]}
                for nid, rels in (scan.get("nid_index") or {}).items():
                    for r in rels:
                        p2n.setdefault(r, nid)
                hold_ctx = HoldContext(set(holds_pub) | {_nid_of_action(a) for a in conflicts if _nid_of_action(a)},
                                       load_staged_index(e.get("staging") or os.path.join(P.staging, run_id)))
                res, bad, located, unloc = rnd.post_verify(run_id, e["snapshot"], e["sidebar"], e["deviations"],
                                                           os.path.join(rep, "post_publish_verify"), p2n, hold_ctx)
                if res == "ok":
                    st["result"] = "published_with_holds" if (holds_pub or conflicts or
                                                              st.get("post_verify_explained_by_holds")) else "published"
                    if os.path.exists(P.pause_next):
                        os.unlink(P.pause_next)
                else:
                    st["result"] = "published_post_verify_failed"
                    rnd.record_post_verify(run_id, located, unloc, bad)
            e.update(status=new_status, decided_at=now_iso(),
                     decided_by=tty if new_status != "superseded" else f"approve-run:apply_race:{tty}")
            write_json(P.paused_runs, pr)
            st["finished"] = now_iso()
            write_json(os.path.join(rep, "refresh_status.json"), st)
            _history(P, st)
            if st["result"] not in OK_RESULTS:
                write_failure_note(P, run_id, st)
        finally:
            log.close()
        print(json.dumps(st, ensure_ascii=False, default=str)[:5000])
        return EXIT_OK if st.get("result") in ("published", "published_with_holds") else EXIT_FAIL
    finally:
        lk.__exit__(None, None, None)


def discard_run(P, run_id):
    """--discard-run: mark a paused run discarded (no vault write); its evidence may then be pruned."""
    tty, gate = _gate(f"本地刷新操作 --discard-run {run_id}")
    if tty is None:
        return EXIT_GATE
    try:
        lk = wb_locks.lock("refresh", state_dir=P.state)
        lk.__enter__()
    except wb_locks.LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    try:
        pr = load_paused(P)
        e = pr["runs"].get(run_id)
        if not e or e.get("status") not in EVIDENCE_KEPT:
            print(f"拒绝：{run_id} 不是处于暂停（或暂停后被取代）状态的轮次", file=sys.stderr)
            return EXIT_FAIL
        if not gate.confirm_yes_no(f"放弃暂停轮次 {run_id}（不写库，之后允许清理其证据）？"):
            print("已取消。")
            return EXIT_FAIL
        e.update(status="discarded", decided_at=now_iso(), decided_by=tty)
        if pr.get("latest") == run_id:
            pr["latest"] = None
        write_json(P.paused_runs, pr)
        pn = read_json(P.pause_next)
        if isinstance(pn, dict) and pn.get("run_id") and _pause_next_covered(pn, e, run_id):
            os.unlink(P.pause_next)
        _history(P, {"run_id": run_id, "action": "discard-run", "result": "discarded", "at": now_iso(), "tty": tty})
        print(f"已放弃 {run_id}。")
        return EXIT_OK
    finally:
        lk.__exit__(None, None, None)


def release_hold(P, nid):
    """--release-hold: remove one object from state/auto_sync_hold.json (it is published again next round)."""
    tty, gate = _gate(f"本地刷新操作 --release-hold {nid}")
    if tty is None:
        return EXIT_GATE
    try:
        nid = wb_common.norm_notion_id(nid)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    try:
        lk = wb_locks.lock("refresh", state_dir=P.state)
        lk.__enter__()
    except wb_locks.LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    try:
        holds = load_holds(P)
        if nid not in holds:
            print(f"{nid} 不在 auto_sync_hold.json 中")
            return EXIT_FAIL
        print(gate.display_safe(json.dumps(holds[nid], ensure_ascii=False)))
        if not gate.confirm_yes_no(f"解除 {nid} 的自动同步暂停（下一轮按 Notion 版本发布）？"):
            print("已取消。")
            return EXIT_FAIL
        holds.pop(nid)
        save_holds(P, holds)
        _history(P, {"action": "release-hold", "notion_id": nid, "at": now_iso(), "tty": tty})
        return EXIT_OK
    finally:
        lk.__exit__(None, None, None)


HELP_DESCRIPTION = """Notion → Obsidian 一轮刷新（每 6 小时由定时器自动运行；单向：Notion 的内容写入库内）。
One Notion -> Obsidian refresh round (run by the timer every 6 h; one way: Notion content into the vault)."""

HELP_EPILOG = """模式 / modes:
  （无参数）--mode incremental   定时运行：抓侧栏 → 增量快照 → 导出 → 校验 → 发布 / scheduled incremental round
  --mode full                     每周一次：重新读取所有页面 / weekly full re-read
  --dry-run                       发布前停止；实时采集仍会写入工作目录的快照、根清单和报告
                                  stop before vault publication; live input collection still writes snapshots and root lists
  --use-snapshot SNAP             不访问 Notion：用已完成的快照导出 → 校验 → 发布（演练/重新发布）

退出码 / exit codes:
  0 已发布（published / published_with_holds / planned）  1 失败（库内留有 运行记录/<run>-未完成.md）
  2 本轮已暂停（paused_for_review，库内留有 运行记录/<run>-已暂停.md）  3 人工确认被拒绝（不是本人终端）

本轮被暂停时 / when a round is paused:
  1. 阅读库内 Notion_Obsidian/运行记录/<run>-已暂停.md（原因、涉及的页面与路径、放行后将写入什么）
     read the paused note in the vault (reasons, objects, what approving would write)
  2. 放行（在本人的普通终端中运行，回答 y）：按 Notion 版本发布暂停时评审过的同一计划
     approve: python3 -I tools/refresh.py --approve-run "${RUN_ID:?请先设置暂停轮次编号}"
  3. 或放弃本轮（不写库）：python3 -I tools/refresh.py --discard-run "${RUN_ID:?请先设置暂停轮次编号}"
     本地大批量删除导致的暂停，也可以把删除转为待决删除：
     python3 -I tools/writeback.py hold --run "${RUN_ID:?请先设置暂停轮次编号}"
  4. 某个对象被自动暂停同步（auto_sync_hold）且问题已处理：python3 -I tools/refresh.py --release-hold "${NOTION_ID:?请先设置暂停对象编号}"
  只能放行最近一次暂停的轮次；暂停后库内又有改动时计划会不同，放行会被拒绝（请放弃并在下一轮暂停后尽快放行）。

示例 / examples:
  python3 -I tools/refresh.py --dry-run
  python3 -I tools/refresh.py --help
"""


def build_parser():
    epilog = HELP_EPILOG
    for tool in ("writeback", "refresh"):
        epilog = epilog.replace(f"python3 -I tools/{tool}.py", cli_command(tool))
    ap = argparse.ArgumentParser(description=HELP_DESCRIPTION, epilog=epilog,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["incremental", "full"], default="incremental",
                    help="incremental：只重读有改动的页面（默认，定时运行）；full：重读所有页面 / "
                         "incremental (default, scheduled) or full re-read")
    ap.add_argument("--dry-run", action="store_true",
                    help="到发布前停止，仍会生成库外快照、根清单及报告 / stop before publishing "
                         "(live input collection still writes the work directory)")
    ap.add_argument("--use-snapshot", default=None, metavar="SNAP_DB",
                    help="不抓侧栏、不访问 Notion，用这个已完成的快照导出 → 校验 → 发布 / skip sidebar capture and "
                         "the Notion crawl; export -> verify -> publish from this finished snapshot")
    ap.add_argument("--sidebar", default=None,
                    help="仅与 --use-snapshot 一起：使用的侧栏证据（默认用快照记录的顶层页面）/ with --use-snapshot: "
                         "sidebar evidence to use (default: the snapshot's roots)")
    ap.add_argument("--run-id", default=None, help="指定运行编号（演练用，必须未用过）/ explicit run id (rehearsals); must be unused")
    ap.add_argument("--vault", default=None,
                    help=f"库路径（默认 {VAULT}；其他路径需要 --allow-other-vault）/ vault (default {VAULT})")
    ap.add_argument("--allow-other-vault", action="store_true", help="只用于演练副本 / for rehearsal copies only")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve-run", metavar="RUN_ID",
                   help="放行最近一次暂停的轮次（本人终端 + y/N）/ publish the latest paused run (human terminal + y/N)")
    g.add_argument("--discard-run", metavar="RUN_ID",
                   help="放弃一个暂停的轮次，不写库（本人终端 + y/N）/ discard a paused run (human terminal + y/N)")
    g.add_argument("--release-hold", metavar="NOTION_ID",
                   help="解除一个对象的自动同步暂停（auto_sync_hold.json，本人终端 + y/N）/ remove an object from "
                        "auto_sync_hold.json (human)")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    with wb_common.command_context(a):
        return _dispatch(a)


def _dispatch(a):
    vault = os.path.abspath(a.vault) if a.vault else VAULT
    if vault != os.path.abspath(VAULT) and not a.allow_other_vault:
        print(f"中止：库必须是 {VAULT}（演练副本请加 --allow-other-vault）/ ABORT: vault must be {VAULT} "
              "(use --allow-other-vault for rehearsal copies)", file=sys.stderr)
        return EXIT_FAIL
    if a.sidebar and not a.use_snapshot:
        print("中止：--sidebar 只能与 --use-snapshot 一起使用 / ABORT: --sidebar only with --use-snapshot", file=sys.stderr)
        return EXIT_FAIL
    P = Paths(WORK, vault)
    if a.approve_run:
        return approve_run(P, a.approve_run)
    if a.discard_run:
        return discard_run(P, a.discard_run)
    if a.release_hold:
        return release_hold(P, a.release_hold)
    if a.run_id and not RUN_ID_RE.fullmatch(a.run_id):
        print("中止：--run-id 只能包含字母、数字、_ 和 - / ABORT: --run-id may only contain [0-9A-Za-z_-]", file=sys.stderr)
        return EXIT_FAIL
    rc, _st = run_round(P, a)
    return rc


if __name__ == "__main__":
    sys.exit(main())
