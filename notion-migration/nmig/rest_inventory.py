"""Exact block-level inventory of a Notion subtree via the official REST API.

Resumable: every object/work item is checkpointed in SQLite; re-running continues
where it stopped and never re-creates finished items.

Walk:  page -> GET page + recursive block children (all pages of results)
       child_page -> page ; child_database -> database
       database -> GET database -> each data source: GET schema + query all rows
       row -> stored as page (properties from query) + its block content
Extras: relation/people/rich_text properties with has_more are re-read with the
        paginated property-item endpoint; comments per page (if the integration
        has the capability); Notion-hosted files are downloaded once to record
        size + sha256 (content is NOT stored).
"""
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .client import NotionAPIError

log = logging.getLogger("nmig.inventory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS work(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT,
    status TEXT, attempts INT DEFAULT 0, error TEXT, updated TEXT, PRIMARY KEY(id, kind));
CREATE TABLE IF NOT EXISTS objects(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT,
    data TEXT, fetched_at TEXT, PRIMARY KEY(id, kind));
CREATE TABLE IF NOT EXISTS blocks(id TEXT PRIMARY KEY, page_id TEXT, parent_id TEXT, ord INT,
    type TEXT, has_children INT, data TEXT);
CREATE INDEX IF NOT EXISTS blocks_page ON blocks(page_id);
CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY, page_id TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS files(key TEXT PRIMARY KEY, owner_id TEXT, owner_kind TEXT, field TEXT,
    name TEXT, size INT, sha256 TEXT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT, level TEXT, obj TEXT, msg TEXT);
"""

FILE_BLOCKS = ("image", "file", "pdf", "video", "audio")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def enqueue(self, oid, kind, parent, root, depth):
        self.x("INSERT OR IGNORE INTO work(id,kind,parent_id,root_id,depth,status,updated) VALUES(?,?,?,?,?,'pending',?)",
               (oid, kind, parent, root, depth, now()))

    def set_status(self, oid, kind, status, error=None):
        self.x("UPDATE work SET status=?, error=?, attempts=attempts+1, updated=? WHERE id=? AND kind=?",
               (status, error, now(), oid, kind))

    def put(self, oid, kind, parent, root, depth, data):
        self.x("INSERT OR REPLACE INTO objects(id,kind,parent_id,root_id,depth,data,fetched_at) VALUES(?,?,?,?,?,?,?)",
               (oid, kind, parent, root, depth, json.dumps(data, ensure_ascii=False), now()))

    def event(self, level, obj, msg):
        self.x("INSERT INTO events VALUES(?,?,?,?)", (now(), level, obj, msg[:2000]))


class Inventory:
    def __init__(self, client, store, workers=4, hash_files=True, comments=True, max_attempts=4):
        self.c, self.s = client, store
        self.workers, self.hash_files, self.comments = workers, hash_files, comments
        self.max_attempts = max_attempts
        self.comments_disabled = False
        self.http = requests.Session()

    # ------------------------------------------------------------------ driver
    def seed(self, roots):
        for r in roots:
            self.s.enqueue(r["id"], r.get("kind", "page"), None, r["id"], 0)
        self.s.x("INSERT OR REPLACE INTO meta VALUES('roots', ?)", (json.dumps(roots, ensure_ascii=False),))
        if not self.s.q("SELECT value FROM meta WHERE key='started_at'"):
            self.s.x("INSERT INTO meta VALUES('started_at', ?)", (now(),))

    def run(self):
        # items left "running" by an interrupted run are retried
        self.s.x("UPDATE work SET status='pending' WHERE status='running'")
        while True:
            batch = self.s.q("SELECT id,kind,parent_id,root_id,depth FROM work WHERE status='pending' "
                             "OR (status='error' AND attempts<?) ORDER BY depth LIMIT 200", (self.max_attempts,))
            if not batch:
                break
            for row in batch:
                self.s.set_status(row[0], row[1], "running")
            with ThreadPoolExecutor(self.workers) as ex:
                list(ex.map(self._process_safe, batch))
            done = self.s.q("SELECT status, COUNT(*) FROM work GROUP BY status")
            log.info("progress %s | requests=%s retries=%s", dict(done), self.c.stats["requests"], self.c.stats["retries"])
        self.s.x("INSERT OR REPLACE INTO meta VALUES('finished_at', ?)", (now(),))

    def _process_safe(self, row):
        oid, kind, parent, root, depth = row
        try:
            if kind == "page":
                self.page(oid, parent, root, depth)
            elif kind == "row":
                self.page(oid, parent, root, depth, prefetched=True)
            elif kind == "database":
                self.database(oid, parent, root, depth)
            self.s.set_status(oid, kind, "done")
        except NotionAPIError as e:
            status = "denied" if e.status in (401, 403, 404) else "error"
            self.s.set_status(oid, kind, status, f"{e.status} {e.code}: {e.message}"[:500])
            self.s.event("error", oid, f"{kind}: {e}")
        except Exception as e:  # noqa: BLE001 - recorded, retried
            self.s.set_status(oid, kind, "error", f"{type(e).__name__}: {e}"[:500])
            self.s.event("error", oid, f"{kind}: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ pages
    def page(self, pid, parent, root, depth, prefetched=False):
        data = None
        if prefetched:
            got = self.s.q("SELECT data FROM objects WHERE id=? AND kind='page'", (pid,))
            data = json.loads(got[0][0]) if got else None
        if data is None:
            data = self.c.retrieve_page(pid)
        self._complete_properties(pid, data)
        self.s.put(pid, "page", parent, root, depth, data)
        self._files_of_page(pid, data)
        # content: delete partial blocks of an interrupted run, then walk
        self.s.x("DELETE FROM blocks WHERE page_id=?", (pid,))
        self._walk_children(pid, pid, root, depth)
        if self.comments and not self.comments_disabled:
            try:
                for cm in self.c.list_comments(pid):
                    self.s.x("INSERT OR REPLACE INTO comments VALUES(?,?,?)",
                             (cm["id"], pid, json.dumps(cm, ensure_ascii=False)))
            except NotionAPIError as e:
                if e.status in (403, 401):
                    self.comments_disabled = True
                    self.s.event("warn", pid, f"comments unavailable for this integration: {e.code}")
                else:
                    self.s.event("warn", pid, f"comments error: {e}")

    def _complete_properties(self, pid, data):
        for name, prop in (data.get("properties") or {}).items():
            if prop.get("has_more") and prop.get("id"):
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

    def _walk_children(self, page_id, block_id, root, depth):
        children = self.c.list_children(block_id)
        for i, b in enumerate(children):
            t = b.get("type")
            self.s.x("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?,?,?)",
                     (b["id"], page_id, block_id, i, t, int(bool(b.get("has_children"))),
                      json.dumps(b, ensure_ascii=False)))
            if t == "child_page":
                self.s.enqueue(b["id"], "page", page_id, root, depth + 1)
            elif t == "child_database":
                self.s.enqueue(b["id"], "database", page_id, root, depth + 1)
            elif b.get("has_children"):
                try:
                    self._walk_children(page_id, b["id"], root, depth)
                except NotionAPIError as e:
                    if e.status not in (403, 404):
                        raise
                    # e.g. synced block whose content lives in another workspace:
                    # record the unreadable subtree and continue with the page
                    self.s.event("anomaly", b["id"],
                                 f"children of {t} block not readable via API ({e.status} {e.code}): {e.message}"[:600])
            if t in FILE_BLOCKS and (b.get(t) or {}).get("type") == "file":
                self._hash(b["id"], "block", t, (b[t].get("file") or {}).get("url"), b[t].get("name"))

    # -------------------------------------------------------------- databases
    def database(self, did, parent, root, depth):
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
            self.s.put(ds_ref["id"], "data_source", did, root, depth, ds)
            for r in rows:
                self.s.put(r["id"], "page", did, root, depth + 1, r)
                self.s.enqueue(r["id"], "row", did, root, depth + 1)

    # ------------------------------------------------------------------ files
    def _files_of_page(self, pid, data):
        cov = data.get("cover") or {}
        if cov.get("type") == "file":
            self._hash(pid, "page", "cover", cov["file"].get("url"), None)
        ic = data.get("icon") or {}
        if ic.get("type") == "file":
            self._hash(pid, "page", "icon", ic["file"].get("url"), None)
        for name, prop in (data.get("properties") or {}).items():
            if prop.get("type") == "files":
                for i, f in enumerate(prop.get("files") or []):
                    if f.get("type") == "file":
                        self._hash(pid, "page", f"prop:{name}:{i}", f["file"].get("url"), f.get("name"))

    def _hash(self, owner, owner_kind, field, url, name):
        key = f"{owner}:{field}"
        if not url:
            return
        nm = name or url.split("?")[0].rsplit("/", 1)[-1]
        if not self.hash_files:
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, None, None, "not_hashed", None))
            return
        try:
            h, size = hashlib.sha256(), 0
            with self.http.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                for chunk in r.iter_content(1 << 16):
                    h.update(chunk)
                    size += len(chunk)
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, size, h.hexdigest(), "ok", None))
        except Exception as e:  # noqa: BLE001
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, None, None, "error", str(e)[:300]))


def summary(store):
    s = store
    out = {"work": dict(s.q("SELECT status, COUNT(*) FROM work GROUP BY status")),
           "objects": dict(s.q("SELECT kind, COUNT(*) FROM objects GROUP BY kind")),
           "blocks": s.q("SELECT COUNT(*) FROM blocks")[0][0],
           "block_types": dict(s.q("SELECT type, COUNT(*) FROM blocks GROUP BY type ORDER BY 2 DESC")),
           "comments": s.q("SELECT COUNT(*) FROM comments")[0][0],
           "files": dict(s.q("SELECT status, COUNT(*) FROM files GROUP BY status")),
           "errors": s.q("SELECT id, kind, status, error FROM work WHERE status IN ('error','denied')"),
           "meta": dict(s.q("SELECT key, value FROM meta WHERE key IN ('started_at','finished_at')"))}
    return out
