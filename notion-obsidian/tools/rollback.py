#!/usr/bin/env python3
"""Roll back one publish run using its journal (reverse order).

Undone, each only while the vault still holds exactly what the run wrote (otherwise listed as skipped):
  ADD (incl. restores and the run log)  -> file moved to WORK/rollback/<run_id>/<rel> (never deleted)
  REPLACE, LINK_REWRITE_ONLY, LOCAL_KEYS_ONLY, GENERATED_OVERWRITE, ADOPT_WRITEBACK
                                        -> previous bytes restored from the backup the journal names
                                           (legacy journals: WORK/backup/vault-before/<run_id>/<rel>); the backup must
                                           hash to the journaled `before`
  MOVE                                  -> moved back to the old path (only if that path is free), old bytes restored
  CONFLICT_* copy written by the run    -> exactly that copy is quarantined (only if unchanged) and its
                                           conflict_copies row removed, so a later run may write it again
  ADOPT, ADOPT_RENAME, TRASHED_VIA_WRITEBACK -> database only (published_files rows / objects_map status as before)
Databases: export.db published_files / conflict_copies and writeback.db base_history are reverted for every undone
entry, using --state / --wb-db or else the paths recorded in the journal's begin line (--no-db: files only). A row is
reverted only while it is still exactly what this run wrote (a later run's row is never touched); the base_history row
this run appended for an undone file (or, for ADOPT_RENAME, the row re-pathing the base to the adopted name) is
removed only if it is still the latest row of that object.
Not undoable / nothing to undo: UNCHANGED and UNCHANGED_SEMANTIC (no file was written; their published_files refresh
and base_history seed describe files that are still in the vault), LOCAL_ONLY (no file written; its base_history
seed / re-path row records the Notion version that is still the published one), SKIP_HELD, SKIP_*_RACE, conflicts that
were deduplicated (no copy written), TOMBSTONE (journal note), block_lines rows (keyed by staged sha, harmless),
state/base content files (content-addressed), the "recovered" event (writes of an interrupted earlier run that this
run only registered; they have no "done" entry of their own). A write interrupted by a crash (journal "prepare" without
"done") is not undone here: the next publish registers it from export.db publish_intents. Legacy journals (no
db_before) are rolled back for files only.
Steps are checked against the state the earlier (later-journaled) steps leave behind, so a plan-only run reports
exactly what --apply does; --apply re-checks each file just before touching it.
Default: plan only; --apply to execute.
Usage: python3 -I rollback.py --run-id <run_id> --vault <vault> [--apply] [--allow-other-vault] [--journal J]
       [--quarantine DIR] [--log-dir DIR] [--state export.db] [--wb-db writeback.db] [--no-db]
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import publish_vault  # noqa: E402
import wb_common  # noqa: E402
import wb_locks  # noqa: E402

WORK = os.path.dirname(HERE)
EXPECTED_VAULT = "/data120/Obsidian/Godcreate"
RESTORE_OPS = ("REPLACE", "LINK_REWRITE_ONLY", "LOCAL_KEYS_ONLY", "GENERATED_OVERWRITE", "ADOPT_WRITEBACK")
CONFLICT_OPS = ("CONFLICT_BOTH", "CONFLICT_MOVED_EDITED", "CONFLICT_NOT_FILE", "CONFLICT_UNTRACKED", "CONFLICT_LOCAL")
DB_ONLY_OPS = ("ADOPT", "ADOPT_RENAME", "TRASHED_VIA_WRITEBACK")
BASE_UNDO_OPS = ("ADD", "MOVE", "ADOPT", "ADOPT_RENAME") + RESTORE_OPS
PF_COLS = ("rel_path", "sha256", "notion_id", "kind", "run_id", "status", "staged_sha256")
_GONE = object()


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _regular(p):
    return os.path.isfile(p) and not os.path.islink(p)


def load_journal(jpath):
    out = []
    with open(jpath, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


class Db:
    """export.db (+ writeback.db); every write names its columns."""

    def __init__(self, state, wb_db, apply):
        self.apply = apply
        self.con = self.wb = None
        if state and os.path.isfile(state):
            self.con = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(state))}" + ("" if apply else "?mode=ro"),
                                       uri=True, timeout=30)
        if wb_db and os.path.isfile(wb_db):
            self.wb = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(wb_db))}" + ("" if apply else "?mode=ro"),
                                      uri=True, timeout=30)
        self.cols = [r[1] for r in self.con.execute("PRAGMA table_info(published_files)")] if self.con else []

    def row(self, rel):
        if not self.cols:
            return None
        sel = ", ".join(c if c in self.cols else f"NULL AS {c}" for c in PF_COLS)
        r = self.con.execute(f"SELECT {sel} FROM published_files WHERE rel_path=?", (rel,)).fetchone()
        return dict(zip(PF_COLS, r)) if r else None

    def put(self, rel, before):
        if before is None:
            self.con.execute("DELETE FROM published_files WHERE rel_path=?", (rel,))
            return
        cols = [c for c in PF_COLS if c in self.cols]
        self.con.execute(f"INSERT OR REPLACE INTO published_files({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                         [before.get(c) for c in cols])

    def commit(self):
        for c in (self.con, self.wb):
            if c is not None:
                c.commit()

    def close(self):
        for c in (self.con, self.wb):
            if c is not None:
                c.close()


class Virtual:
    """Vault files and published_files rows as the already-planned rollback steps leave them."""

    def __init__(self, vault, db):
        self.vault, self.db, self.files, self.rows = vault, db, {}, {}

    def path(self, rel):
        return wb_common.safe_join(self.vault, rel)

    def sha(self, rel):
        if rel in self.files:
            return None if self.files[rel] is _GONE else self.files[rel]
        p = self.path(rel)
        return sha_file(p) if _regular(p) else None

    def exists(self, rel):
        if rel in self.files:
            return self.files[rel] is not _GONE
        return os.path.lexists(self.path(rel))

    def row(self, rel):
        if rel in self.rows:
            return self.rows[rel]
        return self.db.row(rel) if self.db is not None and self.db.con is not None else None


def _same_except_status(cur, before, status):
    if not cur or not before or cur.get("status") != status:
        return False
    return all(cur.get(k) == before.get(k) for k in PF_COLS if k != "status")


def db_steps(e, run_id, v):
    """[(rel, before_row, ok)] reverting the published_files rows of one done entry (empty without a database)."""
    dbb = e.get("db_before")
    if not isinstance(dbb, dict) or v.db is None or v.db.con is None:
        return []
    op, steps = e.get("op"), []
    for r, before in dbb.items():
        cur = v.row(r)
        if op in ("MOVE", "ADD") and r == e.get("frm"):
            ok = before is None or _same_except_status(cur, before, "moved")
        elif op == "ADOPT_RENAME" and r == e.get("frm"):
            ok = _same_except_status(cur, before, "renamed")
        elif op == "TRASHED_VIA_WRITEBACK":
            ok = _same_except_status(cur, before, "trashed_via_writeback")
        else:
            want = e.get("sha") if op == "ADOPT_RENAME" else e.get("after")
            ok = bool(cur) and cur.get("run_id") == run_id and cur.get("status") == "active" and cur.get("sha256") == want
        steps.append((r, before, ok))
    return steps


def plan_steps(entries, run_id, v, bk):
    """Reverse-order rollback steps checked against the virtual state; returns (steps, skipped)."""
    done = [e for e in entries if e.get("phase") == "done"]
    steps, skipped = [], []
    for e in reversed(done):
        op, rel = e.get("op"), e.get("rel")
        try:
            v.path(rel)
        except wb_common.UnsafePath as exc:
            skipped.append({"rel": rel, "op": op, "why": str(exc)})
            continue
        st = {"op": op, "rel": rel, "entry": e, "files": {}, "db": [], "db_skipped": []}
        if op in CONFLICT_OPS:
            crel = e.get("conflict_rel")
            if not crel:
                continue
            st["db_conflict"] = (e.get("obj_key"), e.get("kind"), e.get("staged"))
            if e.get("written") is False:
                st["action"] = ("db_only", None)
            elif v.sha(crel) != e.get("conflict_sha"):
                skipped.append({"rel": crel, "op": op, "why": "conflict copy changed or missing since publish"})
                continue
            else:
                st["action"] = ("remove_conflict_copy", crel)
                st["files"] = {crel: _GONE}
            steps.append(st)
            v.files.update(st["files"])
            continue
        if op in DB_ONLY_OPS:
            if op == "ADOPT" and v.sha(rel) != e.get("after"):
                skipped.append({"rel": rel, "op": op, "why": "changed or missing since publish"})
                continue
            if op == "ADOPT_RENAME" and (not e.get("frm") or v.exists(e["frm"])):
                skipped.append({"rel": rel, "op": op, "why": "old path exists again"})
                continue
            if op == "TRASHED_VIA_WRITEBACK" and v.exists(rel):
                skipped.append({"rel": rel, "op": op, "why": "file exists again"})
                continue
            dsteps = db_steps(e, run_id, v)
            if not dsteps or not all(ok for _, _, ok in dsteps):
                skipped.append({"rel": rel, "op": op, "why": "no database to revert" if not dsteps
                                else "published_files row changed since the run"})
                continue
            st["action"] = ("db_only", None)
            steps.append(st)
        else:
            if v.sha(rel) != e.get("after"):
                skipped.append({"rel": rel, "op": op, "why": "changed or missing since publish"})
                continue
            if op == "ADD":
                st["action"] = ("quarantine", None)
                st["files"] = {rel: _GONE}
            elif op in RESTORE_OPS:
                b = e.get("backup") or os.path.join(bk, rel)
                bsha = sha_file(b) if _regular(b) else None
                if bsha is None or (e.get("before") and bsha != e.get("before")):
                    skipped.append({"rel": rel, "op": op, "why": f"backup missing or not the pre-publish bytes: {b}"})
                    continue
                st["action"] = ("restore", b)
                st["files"] = {rel: bsha}
            elif op == "MOVE":
                frm = e.get("frm") or next((x.get("frm") for x in entries if x.get("op") == "MOVE"
                                            and x.get("rel") == rel and x.get("frm")), None)
                if not frm:
                    skipped.append({"rel": rel, "op": op, "why": "MOVE source unknown"})
                    continue
                if v.exists(frm):
                    skipped.append({"rel": rel, "op": op, "why": f"old path is occupied: {frm}"})
                    continue
                b = e.get("backup") or os.path.join(bk, frm)
                bsha = sha_file(b) if _regular(b) else None
                if e.get("backup") and (bsha is None or (e.get("before") and bsha != e.get("before"))):
                    skipped.append({"rel": rel, "op": op, "why": f"backup missing or not the pre-publish bytes: {b}"})
                    continue
                st["action"] = ("move_back", frm)
                st["backup"] = b if bsha else None
                st["files"] = {rel: _GONE, frm: bsha or e.get("after")}
            else:
                continue
            steps.append(st)
        ds = db_steps(e, run_id, v)
        st["db"] = [(r, before) for r, before, ok in ds if ok]
        st["db_skipped"] = [r for r, _, ok in ds if not ok]
        v.files.update(st["files"])
        for r, before in st["db"]:
            v.rows[r] = before
    return steps, skipped


def _quarantine(src, quarantine, rel):
    q = os.path.join(quarantine, rel)
    os.makedirs(os.path.dirname(q), exist_ok=True)
    k, qq = 0, q
    while os.path.lexists(qq):
        k += 1
        qq = f"{q}.{k}"
    shutil.move(src, qq)
    return qq


def _move_if_absent(src, dst):
    """An atomic no-clobber move. If the filesystem cannot provide it, leave both files untouched."""
    try:
        return bool(publish_vault._rename2(src, dst, publish_vault._RENAME_NOREPLACE))
    except (FileExistsError, FileNotFoundError):
        return False


def _restore_bytes(step, vault, quarantine):
    """Restore or move back only the planned bytes; report paths of every retained save on a late skip."""
    e = step["entry"]
    kind, extra = step["action"]
    cur = wb_common.safe_join(vault, step["rel"])
    backup = extra if kind == "restore" else step.get("backup")
    data = None
    if backup:
        try:
            with open(backup, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return {"why": f"cannot read rollback backup: {exc}"}
        planned_sha = step["files"][step["rel"] if kind == "restore" else extra]
        if wb_common.sha256_bytes(data) != planned_sha:
            return {"why": "backup changed since rollback plan", "backup": backup}
    target = cur
    if kind == "move_back":
        target = wb_common.safe_join(vault, extra)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not _move_if_absent(cur, target):
            return {"why": "old path occupied, source missing, or atomic move unavailable", "old_path": extra}

    # For MOVE a user's last save may have travelled with the inode. Check it again at the destination
    # before accepting a move; guarded_replace also checks the displaced bytes at the instant of replacement.
    try:
        if data is None:
            race = None if _regular(target) and sha_file(target) == e["after"] else {}
        else:
            race = publish_vault.guarded_replace(target, data, e["after"], os.path.join(quarantine, "race"))
    except FileNotFoundError:
        race = {"vault_sha": None}
    if kind == "move_back" and race is None and os.path.lexists(cur):
        return {"why": "original working path was recreated during rollback; both files retained",
                "paths_to_check": [step["rel"], extra]}
    if race is None:
        return None
    detail = {"why": "file changed during rollback; database and base history not reverted", **race}
    if kind == "move_back":
        returned = _move_if_absent(target, cur)
        detail["paths_to_check"] = [step["rel"]] if returned else [step["rel"], extra]
    return detail


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-other-vault", action="store_true")
    ap.add_argument("--journal", default=None)
    ap.add_argument("--quarantine", default=None)
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--state", default=None, help="export.db to revert (default: the one in the journal's begin line)")
    ap.add_argument("--wb-db", default=None, help="writeback.db to revert (default: the one in the journal's begin line)")
    ap.add_argument("--no-db", action="store_true", help="files only; leave the databases as they are")
    a = ap.parse_args(argv)
    vault = os.path.realpath(a.vault)
    if vault != EXPECTED_VAULT and not a.allow_other_vault:
        sys.exit(f"ABORT: vault must be {EXPECTED_VAULT}")
    jpath = a.journal or os.path.join(WORK, "reports", a.run_id, "publish_journal.jsonl")
    entries = load_journal(jpath)
    bk = os.path.join(WORK, "backup", "vault-before", a.run_id)
    quarantine = a.quarantine or os.path.join(WORK, "rollback", a.run_id)
    begins = [e for e in entries if e.get("event") == "begin"]
    state = None if a.no_db else (a.state or next((b.get("state") for b in reversed(begins) if b.get("state")), None))
    wb_db = None if a.no_db else (a.wb_db or next((b.get("wb_db") for b in reversed(begins) if b.get("wb_db")), None))
    if a.apply and not getattr(main, "_refresh_lock_held", False):
        state_dir = os.path.dirname(os.path.abspath(state)) if state else os.path.join(WORK, "state")
        try:
            with wb_locks.lock("refresh", state_dir=state_dir):
                main._refresh_lock_held = True
                return main(argv)
        except wb_locks.LockBusy as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            main._refresh_lock_held = False
    db = Db(state, wb_db, a.apply) if (state or wb_db) else None
    v = Virtual(vault, db)
    steps, skipped = plan_steps(entries, a.run_id, v, bk)
    db_skipped = [{"rel": r, "op": s["op"]} for s in steps for r in s["db_skipped"]]
    print(json.dumps({"plan": len(steps), "skipped": skipped[:20], "n_skipped": len(skipped),
                      "db": {"state": state, "wb_db": wb_db} if db else None, "n_db_skipped": len(db_skipped)},
                     ensure_ascii=False))
    if not a.apply:
        if db:
            db.close()
        return 0
    log_dir = a.log_dir or os.path.join(WORK, "reports", a.run_id)
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "rollback_journal.jsonl"), "a", encoding="utf-8")
    n, late = 0, []
    for st in steps:
        e, op, rel = st["entry"], st["op"], st["rel"]
        kind, extra = st["action"]
        cur = wb_common.safe_join(vault, rel)
        rec = {"op": kind, "rel": rel, "extra": extra, "journal_op": op, "ts": now()}
        if kind in ("quarantine", "restore", "move_back"):
            if not _regular(cur) or sha_file(cur) != e.get("after"):
                late.append({"rel": rel, "op": op, "why": "changed just before rollback"})
                continue
        if kind == "quarantine":
            rec["before"] = sha_file(cur)
            rec["quarantined_to"] = _quarantine(cur, quarantine, rel)
        elif kind in ("restore", "move_back"):
            race = _restore_bytes(st, vault, quarantine)
            if race is not None:
                detail = {"rel": rel, "op": op, **race}
                late.append(detail)
                log.write(json.dumps(dict(rec, skipped=detail), ensure_ascii=False) + "\n")
                log.flush()
                continue
            rec["before"] = e["after"]
            rec["after"] = st["files"][rel if kind == "restore" else extra]
        elif kind == "remove_conflict_copy":
            cp = wb_common.safe_join(vault, extra)
            if not _regular(cp) or sha_file(cp) != e.get("conflict_sha"):
                late.append({"rel": extra, "op": op, "why": "conflict copy changed just before rollback"})
                continue
            rec["before"] = sha_file(cp)
            rec["quarantined_to"] = _quarantine(cp, quarantine, extra)
        n += 1
        if db is not None and db.con is not None:
            reverted = []
            for r, before in st["db"]:
                db.put(r, before)
                reverted.append(r)
            if op == "TRASHED_VIA_WRITEBACK" and e.get("nid") and reverted and \
                    db.con.execute("SELECT 1 FROM sqlite_master WHERE name='objects_map'").fetchone():
                db.con.execute("UPDATE objects_map SET status=? WHERE notion_id=? AND status='trashed_via_writeback'",
                               (e.get("objects_map_status_before"), e["nid"]))
            if st.get("db_conflict") and all(st["db_conflict"]) and \
                    db.con.execute("SELECT 1 FROM sqlite_master WHERE name='conflict_copies'").fetchone():
                key, ck, staged = st["db_conflict"]
                db.con.execute("DELETE FROM conflict_copies WHERE obj_key=? AND kind=? AND staged_sha=? AND run_id=?",
                               (key, ck, staged, a.run_id))
                reverted.append(f"conflict_copies:{key}")
            if reverted:
                rec["db_reverted"] = reverted
        if db is not None and db.wb is not None and e.get("nid") and e.get("staged") and op in BASE_UNDO_OPS:
            try:
                r = db.wb.execute("SELECT seq, run_id, sha256 FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1",
                                  (e["nid"],)).fetchone()
            except sqlite3.Error:
                r = None
            if r and r[1] == a.run_id and r[2] == e["staged"]:
                db.wb.execute("DELETE FROM base_history WHERE notion_id=? AND seq=?", (e["nid"], r[0]))
                rec["base_history_removed"] = [e["nid"], r[0]]
        if db is not None:
            db.commit()
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.flush()
    log.close()
    if db is not None:
        db.close()
    # empty directories created by the run are left in place (recorded, never auto-removed)
    print(json.dumps({"rolled_back": n, "skipped_at_apply": late[:20], "n_db_skipped": len(db_skipped)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
