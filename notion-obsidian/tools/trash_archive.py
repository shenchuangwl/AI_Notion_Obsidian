#!/usr/bin/env python3
"""Archive objects that disappeared from the Notion snapshot (WRITEBACK_SPEC §3 trash_archive, §6.5; R-108).

    python3 -I trash_archive.py --prev snapshots/snap_A.db --new snapshots/snap_B.db [--wb-db state/writeback.db]
                                [--run-id R] [--exclude-ids FILE] [--dry-run]

Compares the previous and the new snapshot (both opened read-only; a snapshot whose -wal or
-journal holds data is refused, because immutable reads would silently miss those pages). Every page /
database / data source present in the previous snapshot and absent (or newly `in_trash`) in the new one gets
ONE `trash_archive` row in writeback.db: the last page object JSON, its blocks JSON (ordered by parent, ord)
and the inbound references found in the previous snapshot (blocks or objects that mention its id).

Nothing is ever deleted: not in the snapshots, not in the vault, not in writeback.db. A row is not repeated
for the same (notion_id, previous snapshot). `--dry-run` only reports. Ids listed in --exclude-ids (objects
trashed by write-back, `published_files.status = trashed_via_writeback`) are archived if not archived yet but
are not counted for the WB §14.3 "源端消失" pause threshold.
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402

ARCHIVE_KINDS = ("page", "database", "data_source")
COUNT_KINDS = ("page", "database")
REFS_MAX_OBJECTS = 300      # above this many disappeared objects the inbound-reference scan is skipped
REFS_MAX_PER_OBJECT = 200


class SnapshotRefused(RuntimeError):
    pass


def open_snapshot(path):
    """Read-only connection; refuses a snapshot with a non-empty -wal / -journal."""
    p = os.path.realpath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    con, _ = wb_common.open_snapshot_reader(p, require_finished=True, error_type=SnapshotRefused)
    return con


def object_title(kind, data):
    try:
        if kind in ("database", "data_source"):
            return "".join(t.get("plain_text") or (t.get("text") or {}).get("content", "") for t in data.get("title") or [])
        for prop in (data.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return "".join(t.get("plain_text") or (t.get("text") or {}).get("content", "") for t in prop.get("title") or [])
    except (AttributeError, TypeError):
        pass
    return ""


def _objects(con):
    out = {}
    for oid, kind, parent, data in con.execute("SELECT id, kind, parent_id, data FROM objects"):
        if kind not in ARCHIVE_KINDS:
            continue
        try:
            d = json.loads(data) if data else {}
        except ValueError:
            d = {}
        prev = out.get(oid)
        if prev is None or (prev["kind"] == "data_source" and kind != "data_source"):
            out[oid] = {"kind": kind, "parent_id": parent, "data": d, "raw": data}
    return out


def diff(prev_con, new_con):
    """[{notion_id, kind, parent_id, title, reason}] of objects that disappeared (or became in_trash)."""
    old, new = _objects(prev_con), _objects(new_con)
    gone = []
    for oid, o in sorted(old.items()):
        n = new.get(oid)
        if n is None:
            reason = "disappeared_from_snapshot"
        elif bool((n["data"] or {}).get("in_trash")) and not bool((o["data"] or {}).get("in_trash")):
            reason = "in_trash_in_snapshot"
        else:
            continue
        gone.append({"notion_id": oid, "kind": o["kind"], "parent_id": o["parent_id"],
                     "title": object_title(o["kind"], o["data"]), "reason": reason})
    return gone, old


def _blocks_of(con, page_id):
    rows = con.execute("SELECT id, parent_id, ord, type, has_children, data FROM blocks WHERE page_id=? "
                       "ORDER BY parent_id, ord", (page_id,)).fetchall()
    out = []
    for bid, parent, ordv, typ, hc, data in rows:
        try:
            d = json.loads(data) if data else None
        except ValueError:
            d = data
        out.append({"id": bid, "parent_id": parent, "ord": ordv, "type": typ, "has_children": hc, "data": d})
    return out


def _inbound_refs(con, ids, old_objects):
    """{id: [ref]} — blocks / objects of the previous snapshot whose JSON contains the id (dashed or 32-hex)."""
    if not ids:
        return {}
    forms = {}
    for i in ids:
        forms[i] = i
        forms[i.replace("-", "")] = i
    rx = re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)))
    refs = {i: [] for i in ids}
    for bid, page_id, typ, data in con.execute("SELECT id, page_id, type, data FROM blocks"):
        if not data:
            continue
        hit = {forms[m.group(0)] for m in rx.finditer(data)}
        for i in hit:
            if page_id == i or bid == i and typ not in ("child_page", "child_database"):
                continue
            if len(refs[i]) < REFS_MAX_PER_OBJECT:
                refs[i].append({"source": "block", "block_id": bid, "page_id": page_id, "type": typ})
    for oid, o in old_objects.items():
        raw = o.get("raw") or ""
        hit = {forms[m.group(0)] for m in rx.finditer(raw)}
        for i in hit:
            if i == oid:
                continue
            if o["parent_id"] == i or (o["data"].get("parent") or {}).get("page_id") == i:
                continue  # its own child: archived on its own, not a reference
            if len(refs[i]) < REFS_MAX_PER_OBJECT:
                refs[i].append({"source": "object", "object_id": oid, "kind": o["kind"]})
    return refs


def archive(prev_snapshot, new_snapshot, db=None, *, run_id=None, exclude_ids=(), dry_run=False) -> dict:
    """Diff the two snapshots and (unless dry_run) insert trash_archive rows into `db` (a writeback.db
    connection from wb_state.connect). Returns a JSON-serialisable summary. Never deletes anything."""
    exclude = set(exclude_ids or ())
    pc, nc = open_snapshot(prev_snapshot), open_snapshot(new_snapshot)
    try:
        gone, old = diff(pc, nc)
        ids = [g["notion_id"] for g in gone]
        refs_mode = "computed" if len(ids) <= REFS_MAX_OBJECTS else "skipped_too_many"
        refs = _inbound_refs(pc, ids, old) if refs_mode == "computed" else {}
        last = os.path.abspath(prev_snapshot)
        archived, existing = [], []
        if not dry_run and gone and db is None:
            raise ValueError("archive needs a writeback.db connection unless dry_run")
        for g in gone:
            nid = g["notion_id"]
            if db is not None and db.execute("SELECT 1 FROM trash_archive WHERE notion_id=? AND last_snapshot=?",
                                             (nid, last)).fetchone():
                existing.append(nid)
                continue
            if dry_run:
                archived.append(nid)
                continue
            o = old[nid]
            blocks = _blocks_of(pc, nid) if o["kind"] == "page" else []
            reason = g["reason"] + (f" run={run_id}" if run_id else "")
            db.execute("INSERT INTO trash_archive(notion_id, archived_at, reason, last_snapshot, object_json, "
                       "blocks_json, inbound_refs_json) VALUES(?,?,?,?,?,?,?)",
                       (nid, wb_common.now_iso(), reason, last, o["raw"],
                        json.dumps(blocks, ensure_ascii=False),
                        json.dumps({"mode": refs_mode, "refs": refs.get(nid, [])}, ensure_ascii=False)))
            archived.append(nid)
        counted = [g for g in gone if g["kind"] in COUNT_KINDS and g["notion_id"] not in exclude]
        return {"prev": last, "new": os.path.abspath(new_snapshot), "disappeared": gone,
                "archived": len(archived), "already_archived": len(existing), "dry_run": bool(dry_run),
                "count_for_pause": len(counted),
                "excluded_trashed_via_writeback": sorted(g["notion_id"] for g in gone if g["notion_id"] in exclude),
                "inbound_refs": refs_mode}
    finally:
        pc.close()
        nc.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prev", required=True, help="previous snapshot (read-only)")
    ap.add_argument("--new", required=True, help="new snapshot (read-only)")
    ap.add_argument("--wb-db", default=None, help="writeback.db (default state/writeback.db)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--exclude-ids", default=None, help="file with one notion_id per line (trashed via write-back)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    exclude = []
    if a.exclude_ids:
        with open(a.exclude_ids, encoding="utf-8") as fh:
            exclude = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    db = None
    if not a.dry_run:
        import wb_state
        db = wb_state.connect(a.wb_db)
    try:
        res = archive(a.prev, a.new, db, run_id=a.run_id, exclude_ids=exclude, dry_run=a.dry_run)
    finally:
        if db is not None:
            db.close()
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
