#!/usr/bin/env python3
"""Read-only full snapshot of the Godcreate workspace scope (Notion REST API) for the
Notion -> Obsidian export.

Reuses nmig.rest_inventory (resumable SQLite crawler) from
/data120/Obsidian/notion-migration and adds:
  * root kind detection (page / database) for the sidebar root list;
  * content-addressed storage of Notion-hosted files: each signed URL (valid ~1 h)
    is downloaded immediately while crawling into <WORK>/blobs/<sha256>
    (tmp file + os.replace, never overwritten), and the files table records sha256.

Only GET / search / data-source query requests are issued (read-only). Tokens are read
by nmig.config from ~/.config/notion-migrate/.env and never printed or logged.

Usage:
  python3 snapshot.py --roots <roots.json> --db <snapshot.db> [--workers 4] [--rate 2.7]
Re-running the same command resumes an interrupted snapshot.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import uuid

NMIG = "/data120/Obsidian/notion-migration"
sys.path.insert(0, NMIG)

from nmig.client import NotionAPIError  # noqa: E402
from nmig.config import load_config  # noqa: E402
from nmig.rest_inventory import Inventory, Store, summary  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tools/ (python3 -I drops the script dir)
from wb_common import open_snapshot_reader  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import make_read_client  # noqa: E402

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATE_GATE_STATE = os.path.join(WORK, "state", "rate_gate.json")
RATE_GATE_TOTAL_RPS = 2.5  # WRITEBACK_SPEC §15 rate_limit_total_rps, shared by every Notion API process
BLOBS = os.path.join(WORK, "blobs")
log = logging.getLogger("snapshot")


class BlobInventory(Inventory):
    """Inventory that keeps the bytes of Notion-hosted files (content-addressed)."""

    def _hash(self, owner, owner_kind, field, url, name):
        key = f"{owner}:{field}"
        if not url:
            return
        nm = name or url.split("?")[0].rsplit("/", 1)[-1]
        tmp = os.path.join(BLOBS, "tmp", uuid.uuid4().hex)
        try:
            h, size = hashlib.sha256(), 0
            with self.http.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        h.update(chunk)
                        size += len(chunk)
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            digest = h.hexdigest()
            dst = os.path.join(BLOBS, digest)
            if os.path.exists(dst):
                os.unlink(tmp)
            else:
                os.replace(tmp, dst)
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, size, digest, "ok", ctype[:100] or None))
        except Exception as e:  # noqa: BLE001 - recorded; retried by refresh/assets step
            if os.path.exists(tmp):
                os.unlink(tmp)
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, None, None, "error", f"{type(e).__name__}: {e}"[:300]))


def list_templates(client, dsid):
    """GET /v1/data_sources/{id}/templates (paginated; items under "templates")."""
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = client.request("GET", f"data_sources/{dsid}/templates", params=params)
        out += [{"id": t.get("id"), "name": t.get("name"), "is_default": t.get("is_default")}
                for t in (res.get("templates") or res.get("results") or [])]
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def _database_with_templates(self, did, parent, root, depth):
    """Inventory.database + data-source page templates (SPEC v1.2 §13)."""
    db = self.c.retrieve_database(did)
    try:
        db["_views"] = self.c.list_views(did)
    except NotionAPIError as e:
        db["_views_error"] = f"{e.status} {e.code}"
    self.s.put(did, "database", parent, root, depth, db)
    for ds_ref in db.get("data_sources", []):
        ds = self.c.retrieve_data_source(ds_ref["id"])
        rows = self.c.query_data_source(ds_ref["id"])
        ds["_row_ids"] = [r["id"] for r in rows]
        try:
            tpls = list_templates(self.c, ds_ref["id"])
            ds["_templates"] = tpls
            ds["_template_ids"] = [t["id"] for t in tpls if t.get("id")]
        except NotionAPIError as e:
            ds["_templates_error"] = f"{e.status} {e.code}"
        self.s.put(ds_ref["id"], "data_source", did, root, depth, ds)
        for r in rows:
            self.s.put(r["id"], "page", did, root, depth + 1, r)
            self.s.enqueue(r["id"], "row", did, root, depth + 1)
        for tid in ds.get("_template_ids", []):
            self.s.enqueue(tid, "page", did, root, depth + 1)


BlobInventory.database = _database_with_templates


def add_templates(client, store, inv):
    """Top up an existing snapshot with data-source templates (no full re-crawl)."""
    n = 0
    for dsid, did, root, depth, data in store.q("SELECT id,parent_id,root_id,depth,data FROM objects WHERE kind='data_source'"):
        ds = json.loads(data)
        tpls = list_templates(client, dsid)
        ds["_templates"] = tpls
        ds["_template_ids"] = [t["id"] for t in tpls if t.get("id")]
        store.put(dsid, "data_source", did, root, depth, ds)
        for tid in ds["_template_ids"]:
            store.enqueue(tid, "page", did, root, (depth or 0) + 1)
            n += 1
    inv.run()
    return n


class IncrementalInventory(BlobInventory):
    """Reuse blocks/files of pages whose last_edited_time did not change since `prev` snapshot."""

    SAFETY_SECONDS = 120  # last_edited_time has minute precision

    def __init__(self, *args, prev_path=None, **kw):
        super().__init__(*args, **kw)
        import datetime as _dt
        self._dt = _dt
        self.prev, _ = open_snapshot_reader(prev_path, check_same_thread=False)
        self.prev_lock = threading.Lock()
        self.prev_pages = {}
        for oid, data, fetched in self.prev.execute("SELECT id,data,fetched_at FROM objects WHERE kind='page'"):
            try:
                self.prev_pages[oid] = (json.loads(data).get("last_edited_time"), fetched)
            except Exception:  # noqa: BLE001
                pass
        done = {r[0] for r in self.prev.execute("SELECT id FROM work WHERE kind IN ('page','row') AND status='done'")}
        self.prev_pages = {k: v for k, v in self.prev_pages.items() if k in done}
        self.reused = 0

    def _unchanged(self, pid, data):
        pv = self.prev_pages.get(pid)
        if not pv or not pv[0] or pv[0] != data.get("last_edited_time"):
            return False
        try:
            le = self._dt.datetime.fromisoformat(pv[0].replace("Z", "+00:00"))
            fa = self._dt.datetime.fromisoformat(pv[1])
        except Exception:  # noqa: BLE001
            return False
        return (fa - le).total_seconds() > self.SAFETY_SECONDS

    def page(self, pid, parent, root, depth, prefetched=False):
        data = None
        if prefetched:
            got = self.s.q("SELECT data FROM objects WHERE id=? AND kind='page'", (pid,))
            data = json.loads(got[0][0]) if got else None
        if data is None:
            data = self.c.retrieve_page(pid)
        if not self._unchanged(pid, data):
            return self._full(pid, parent, root, depth, data)
        self._complete_properties(pid, data)
        self.s.put(pid, "page", parent, root, depth, data)
        with self.prev_lock:
            blocks = self.prev.execute("SELECT id,page_id,parent_id,ord,type,has_children,data FROM blocks WHERE page_id=?", (pid,)).fetchall()
            ids = [b[0] for b in blocks] + [pid]
            files = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = " OR ".join(["key LIKE ?"] * len(chunk))
                files += self.prev.execute(f"SELECT * FROM files WHERE {q}", [c + ":%" for c in chunk]).fetchall()
        self.s.x("DELETE FROM blocks WHERE page_id=?", (pid,))
        for b in blocks:
            self.s.x("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?,?,?)", b)
            if b[4] == "child_page":
                self.s.enqueue(b[0], "page", pid, root, depth + 1)
            elif b[4] == "child_database":
                self.s.enqueue(b[0], "database", pid, root, depth + 1)
        for f in files:
            if f[7] == "ok" and f[6] and os.path.exists(os.path.join(BLOBS, f[6])):
                self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", f)
        self.reused += 1

    def _full(self, pid, parent, root, depth, data):
        # same as Inventory.page but with the page object already retrieved
        self._complete_properties(pid, data)
        self.s.put(pid, "page", parent, root, depth, data)
        self._files_of_page(pid, data)
        self.s.x("DELETE FROM blocks WHERE page_id=?", (pid,))
        self._walk_children(pid, pid, root, depth)
        if self.comments and not self.comments_disabled:
            try:
                for cm in self.c.list_comments(pid):
                    self.s.x("INSERT OR REPLACE INTO comments VALUES(?,?,?)", (cm["id"], pid, json.dumps(cm, ensure_ascii=False)))
            except NotionAPIError as e:
                if e.status in (401, 403):
                    self.comments_disabled = True


def detect_kinds(client, roots):
    out = []
    for r in roots:
        r = dict(r)
        try:
            p = client.retrieve_page(r["id"])
            r["kind"] = "page"
            r["in_trash"] = bool(p.get("in_trash") or p.get("archived"))
        except NotionAPIError as e:
            if e.status not in (400, 404):
                raise
            d = client.retrieve_database(r["id"])
            r["kind"] = "database"
            r["in_trash"] = bool(d.get("in_trash") or d.get("archived"))
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=2.7,
                    help="per-process cap in requests/s; the shared rate gate (state/rate_gate.json) "
                         "limits all processes together to 2.5 requests/s")
    ap.add_argument("--no-comments", action="store_true")
    ap.add_argument("--incremental-from", default=None, help="previous complete snapshot to reuse unchanged pages from")
    ap.add_argument("--add-templates", action="store_true", help="only top up an existing snapshot with data-source templates")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    os.makedirs(os.path.join(BLOBS, "tmp"), exist_ok=True)
    cfg = load_config()
    client = make_read_client(token=cfg["target_token"], version=cfg["version"], label="godcreate-ro", rate=a.rate,
                              gate=RateGate(RATE_GATE_STATE, total_rps=RATE_GATE_TOTAL_RPS))
    me = client.me()
    ws = (me.get("bot") or {}).get("workspace_id")
    if ws != "c893af62-39e0-4e16-bfa8-2ca081395c81":
        sys.exit(f"ABORT: token workspace mismatch ({ws})")
    kinds_path = a.db + ".roots.json"
    store = Store(a.db)
    if os.path.exists(kinds_path):  # resume: reuse the kinds detected on the first run
        roots = json.load(open(kinds_path, encoding="utf-8"))
    else:
        roots = detect_kinds(client, json.load(open(a.roots, encoding="utf-8")))
        json.dump(roots, open(kinds_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (ws,))
        store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", (cfg["version"],))
    if a.add_templates:
        inv = BlobInventory(client, store, workers=a.workers, hash_files=True, comments=not a.no_comments)
        n = add_templates(client, store, inv)
        store.x("INSERT OR REPLACE INTO meta VALUES('templates_added_at', datetime('now'))")
        print(json.dumps({"templates_enqueued": n, "summary": summary(store), "requests": client.stats}, ensure_ascii=False, default=str)[:4000])
        return
    if a.incremental_from:
        inv = IncrementalInventory(client, store, workers=a.workers, hash_files=True, comments=not a.no_comments,
                                   prev_path=a.incremental_from)
        store.x("INSERT OR REPLACE INTO meta VALUES('incremental_from', ?)", (os.path.basename(a.incremental_from),))
    else:
        inv = BlobInventory(client, store, workers=a.workers, hash_files=True, comments=not a.no_comments)
    inv.seed([r for r in roots if not r.get("in_trash")])
    inv.run()
    s = summary(store)
    s["requests"] = client.stats
    s["reused_pages"] = getattr(inv, "reused", None)
    print(json.dumps(s, ensure_ascii=False, indent=1, default=str)[:20000])


if __name__ == "__main__":
    main()
