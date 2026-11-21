#!/usr/bin/env python3
"""Targeted refetch after a write-back apply (WRITEBACK_SPEC v1.1 §11 step 4, IMPL_CONTRACT §5.12,
checklist R-130 / R-131).

``refetch(base_snapshot, out_path, page_ids, data_source_ids, client)`` produces a NEW snapshot that is the
base snapshot brought up to date for the listed objects, without a full crawl:

1. The finished base (``meta.finished_at`` set, no pending work, no uncheckpointed WAL or rollback journal)
   is opened with ``mode=ro&immutable=1``, including when an empty WAL or an index remains. The SQLite backup API
   copies it into a temporary file next to ``out_path``. Base size, mtime and inode are checked before publication.
2. The copy is updated with the real crawler code of ``tools/snapshot.py`` (``IncrementalInventory`` with
   ``prev_path=<base>``), through a ``ReadOnlyClient`` (GET + query/search only; give it the shared
   ``RateGate``, see ``make_client``):
   * listed pages / rows / templates: always re-fetched -- page object, properties completed like
     ``nmig.rest_inventory._complete_properties``, full block subtree (``IncrementalInventory._full``);
   * listed data sources (and the data source of every listed row): their database is re-read exactly like
     snapshot.py's database crawl (database + views + every data source + ``_row_ids`` + templates), every
     row goes back through the crawler (unchanged rows keep the snapshot.py reuse rule, changed rows are
     re-walked), new rows and templates are fetched;
   * child pages / databases that appear under a re-walked page are fetched (new) or re-parented (moved in);
   * objects that disappear (a child_page/child_database block gone from a re-walked page, a row or
     template gone from a re-listed data source, a listed page that is now trashed or unreadable) are probed
     with one GET:  trashed -> the object row is KEPT with the fresh API JSON (``in_trash: true``) and every
     descendant is re-read too (a page under a trashed parent reads ``in_trash: true``, WRITEBACK contract
     §7.7), their block content is dropped;  404/403 or moved out of scope -> the whole subtree is pruned
     (objects, work, blocks, files, comments);  moved to another in-scope parent -> that parent is
     re-fetched as well (bounded rounds). Without the prune the verifier's orphan check (C02) fails.
   * synced blocks: a reference whose source block lives on another page in the base is not walked (the
     source page owns those block rows, as in the base); if the source is missing afterwards the reference is
     walked as a repair.
   * Notion-hosted files of re-walked pages are downloaded (content-addressed ``blobs/<sha256>``), stale file
     rows of deleted blocks are removed, and hosted embeds get the same second pass as ``assets_fetch.py``.
3. Any work item that ends in error/denied, any unexplained structure (an object whose API parent says it is
   still under a page that no longer lists it), or more than ``max_rounds`` rounds raises
   ``RefetchIncomplete``: nothing is published at ``out_path`` (caller falls back to a full refresh).
4. ``meta``: ``finished_at`` (now, UTC), ``refetch_started_at``, ``refetch_base``, ``refetch_base_finished_at``,
   ``refetch_page_ids``, ``refetch_data_source_ids``, ``targeted_refetch`` (JSON summary), ``trash_markers``
   (JSON list of every object kept as a trash marker in this output). The result is checkpointed into a single
   file (``journal_mode=DELETE``: no -wal/-shm) and published with a hard link, which never replaces an
   existing ``out_path`` / ``<out_path>.roots.json`` (the base's roots, ``in_trash`` updated for trashed or
   restored roots).

Chained bases (a refetch output used as the base of the next refetch): supported, and REQUIRED for consecutive
write-backs (otherwise a second refetch from the refresh snapshot would export the notes adopted by the first
one with their pre-write-back content). Pick the base with ``choose_base()`` (the snapshot the vault was last
published from: the refresh's ``state/last_published_snapshot.txt`` or, if a write-back adoption published
later, the snapshot recorded by ``record_adopted()``). The trash markers of such a base are never trusted: each
one is re-read with one GET (top-down) before the crawl -- still trashed -> stays a marker; gone -> pruned;
restored -> re-walked with its container and its marker subtree, never reused from the base (its content was
dropped). A marker listed again by a re-walked page is re-checked the same way. A wb snapshot made by an
older version (no ``meta.trash_markers``) is refused as a base.

A wb snapshot (``snapshots/wb/``) must NEVER be used as snapshot.py's ``--incremental-from``, as
``state/latest_snapshot.txt`` / ``state/last_published_snapshot.txt``, or as the previous snapshot of
``trash_archive``: trash markers have no content, so those consumers would reuse or archive empty pages. It
is only a base for ``refetch`` (via ``choose_base``) and an input of the exporter / verifier
(``--snapshot-immutable``).

Return value: a JSON-serialisable stats dict (see ``refetch``). ``trashed`` and ``pruned`` list what left the
tree; their last content is still in the BASE snapshot (for ``trash_archive``) unless ``was_marker`` is true
(already archived when the marker was made).

CLI (manual use; the executor calls ``refetch`` directly):
  python3 -I tools/wb_refetch.py --base <snap.db>|auto [--out <snapshots/wb/snap_<ts>_<rand>.db>] --pages <id> ...
         [--data-sources <id> ...] [--pages-file F] [--workers 4] [--blobs DIR]
``prune_wb_snapshots()`` keeps ``snapshots/wb/`` at 7 days (WRITEBACK_SPEC §11.4; outside refresh's quota),
never deleting the snapshot recorded by ``record_adopted()``.
"""
import argparse
import datetime
import glob
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import snapshot as _snap  # noqa: E402  (tools/snapshot.py; also puts nmig on sys.path)
import assets_fetch as _assets  # noqa: E402
import rate_gate  # noqa: E402
import wb_common  # noqa: E402
from nmig.client import NotionAPIError  # noqa: E402
from nmig.rest_inventory import FILE_BLOCKS, Store  # noqa: E402
from nmig.rest_inventory import now as _utcnow  # noqa: E402
from readonly_client import ReadOnlyClient, make_read_client  # noqa: E402

log = logging.getLogger("wb_refetch")

WORK = wb_common.WORK
WB_DIR = os.path.join(wb_common.SNAPSHOTS, "wb")
ADOPTED_FILE = "last_adopted_wb_snapshot.json"        # in state/: written by record_adopted()
LAST_PUBLISHED_TXT = "last_published_snapshot.txt"     # in state/: written by refresh.py (D-17)
LAST_PUBLISHED_JSON = "last_published.json"            # in state/: refresh.py, {"snapshot", "at", ...}
CHILD_TYPES = ("child_page", "child_database")
_CONTAINER = {"child_page": "page", "child_database": "database"}
EMBED_TYPES = ("embed", "bookmark", "link_preview")
GONE_STATUSES = (403, 404)
MAX_HOPS = 25


class RefetchError(Exception):
    """Bad input or failed precondition. Nothing was written at out_path."""


class RefetchIncomplete(RefetchError):
    """The Notion state could not be captured consistently (failed work items, unexplained structure, too
    many rounds). Nothing was written at out_path; fall back to a full refresh."""


# ----------------------------------------------------------------------------------------------- helpers
def _nid(value):
    try:
        return wb_common.norm_notion_id(value)
    except ValueError:
        return None


def _trashed(d):
    return bool(isinstance(d, dict) and (d.get("in_trash") or d.get("archived")))


def _title(kind, d):
    if not isinstance(d, dict):
        return ""
    if kind == "database" or d.get("object") == "database":
        return "".join(x.get("plain_text", "") for x in d.get("title") or [] if isinstance(x, dict))
    for p in (d.get("properties") or {}).values():
        if isinstance(p, dict) and p.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in p.get("title") or [] if isinstance(x, dict))
    return ""


def open_immutable(path):
    """Open a finished snapshot read-only. Reject outstanding WAL or rollback-journal data."""
    con, _ = wb_common.open_snapshot_reader(path, require_finished=True, error_type=RefetchError,
                                            check_same_thread=False)
    return con


def copy_snapshot(base, dst):
    """SQLite backup API copy of `base` (opened read-only) into the new file `dst`."""
    src = open_immutable(base)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _sig(path):
    return wb_common.snapshot_signature(path), os.path.lexists(os.path.realpath(path) + "-shm")


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_blob(http, url, blobs_dir, timeout=180):
    """Download into blobs_dir/<sha256> (tmp + fsync + os.replace, never overwritten), like snapshot.py's
    BlobInventory._hash and assets_fetch.download, with the blob directory as a parameter."""
    tmp_dir = os.path.join(blobs_dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, uuid.uuid4().hex)
    h, size = hashlib.sha256(), 0
    try:
        with http.get(url, stream=True, timeout=timeout) as r:
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
        dst = os.path.join(blobs_dir, digest)
        if os.path.exists(dst):
            os.unlink(tmp)
        else:
            os.replace(tmp, dst)
        return digest, size, ctype
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def make_client(base_url=None, token=None, *, gate=None, gate_path=None, total_rps=rate_gate.DEFAULT_TOTAL_RPS,
                rate=None, token_path=None, **kw) -> ReadOnlyClient:
    """ReadOnlyClient behind the shared cross-process RateGate (state/rate_gate.json, 2.5 rps total).
    token None -> notion_tokens.load_read_token(token_path). rate = optional per-process cap."""
    if gate is None:
        gate = rate_gate.RateGate(gate_path or rate_gate.DEFAULT_PATH, total_rps=total_rps)
    return make_read_client(base_url=base_url, token=token, rate=rate, gate=gate, token_path=token_path, **kw)


def default_out_path(now=None):
    """snapshots/wb/snap_<YYYYmmddTHHMMSS>_<8 hex>.db: unique even for two runs in the same second."""
    ts = (now or datetime.datetime.now()).strftime("%Y%m%dT%H%M%S")
    return os.path.join(WB_DIR, f"snap_{ts}_{uuid.uuid4().hex[:8]}.db")


def _default_blobs_dir():
    """snapshot.py's BLOBS (<WORK>/blobs), refused when it resolves outside <WORK>: in the dev sandbox it is a
    symlink to the PRODUCTION blob store (contract rule 1). Pass blobs_dir explicitly to use another one."""
    d = os.path.abspath(_snap.BLOBS)
    root, real = os.path.realpath(WORK), os.path.realpath(d)
    if os.path.commonpath([root, real]) != root:
        raise RefetchError(f"the default blob directory {d} resolves to {real}, outside {root} (a sandbox symlink "
                           "to production?): pass blobs_dir / --blobs explicitly")
    return d


def _link_new(src, dst):
    """Publish `src` at `dst` with a hard link: never replaces an existing `dst` (no TOCTOU overwrite)."""
    try:
        os.link(src, dst)
    except FileExistsError:
        raise RefetchError(f"refusing to overwrite {dst} (it appeared while the refetch ran)") from None
    except OSError as e:
        raise RefetchError(f"cannot publish {dst}: {type(e).__name__}: {e}") from e


def _unlink_if_same(path, ref):
    """Remove `path` only if it is the same inode as `ref` (our own link)."""
    try:
        a, b = os.stat(path), os.stat(ref)
    except OSError:
        return
    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
        os.unlink(path)


# ---------------------------------------------------------------------------------------------- inventory
class _TargetedInventory(_snap.IncrementalInventory):
    """snapshot.py's IncrementalInventory (prev = base) with: forced ids always re-walked (_full), trashed /
    unreadable forced objects reported instead of failing, databases re-listed with row bookkeeping, the
    synced-reference ownership rule, a parameterised blob directory, and a pending-only work loop."""

    def __init__(self, client, store, *, prev_path, blobs_dir, workers, http=None, markers=()):
        super().__init__(client, store, workers=workers, hash_files=True, comments=False, prev_path=prev_path)
        # trash markers of a chained base have no content: never reuse them (snapshot.py's unchanged-page rule
        # would copy their dropped, i.e. empty, block list)
        for m in markers:
            self.prev_pages.pop(m, None)
        self._own_http = http is None
        if http is not None:
            self.http.close()
            self.http = http
        self.blobs_dir = blobs_dir
        self.mu = threading.Lock()
        self.force_pages, self.force_dbs = set(), set()
        self.skip_rows = set()   # rows/templates that left their data source: probed after the round
        self.walked = {}         # page id -> {"blocks": ids before the walk, "kids": {id: type} before the walk}
        self.found = {}          # forced id -> (verdict, kind, payload): ("trashed", kind, json) | ("gone", kind, err)
        self.db_removed = []     # (database id, object id) rows/templates no longer listed by a re-listed source
        self.ds_removed = []     # (database id, data source id) data sources the database no longer lists
        self.dbs_relisted = set()
        self.synced_skipped = []  # (page id, reference block id, source block id)
        self.failures = []        # work items that failed here (fatal)
        self.tolerated = []       # non-forced items that failed here AND already failed in the base

    # -- work loop: only items this refetch queued (base error/denied rows are left alone) ---------------
    def run_pending(self, only_kind=None):
        self.s.x("UPDATE work SET status='pending' WHERE status='running'")
        while True:
            if only_kind:
                batch = self.s.q("SELECT id,kind,parent_id,root_id,depth FROM work WHERE status='pending' AND kind=? "
                                 "ORDER BY depth LIMIT 200", (only_kind,))
            else:
                batch = self.s.q("SELECT id,kind,parent_id,root_id,depth FROM work WHERE status='pending' "
                                 "ORDER BY depth LIMIT 200")
            if not batch:
                return
            for row in batch:
                self.s.set_status(row[0], row[1], "running")
            with ThreadPoolExecutor(self.workers) as ex:
                list(ex.map(self._process_safe, batch))

    def _process_safe(self, row):
        super()._process_safe(row)
        st = self.s.q("SELECT status, error FROM work WHERE id=? AND kind=?", (row[0], row[1]))
        if st and st[0][0] in ("error", "denied"):
            with self.prev_lock:
                b = self.prev.execute("SELECT status FROM work WHERE id=? AND kind=?", (row[0], row[1])).fetchone()
            rec = {"id": row[0], "kind": row[1], "status": st[0][0], "error": (st[0][1] or "")[:200]}
            old_failure = bool(b and b[0] in ("error", "denied"))
            forced = row[0] in self.force_pages or row[0] in self.force_dbs
            with self.mu:   # e.g. a re-listed row the base could not read either: same as a fresh crawl
                (self.tolerated if old_failure and not forced else self.failures).append(rec)

    def _note_found(self, oid, verdict, kind, payload):
        with self.mu:
            self.found[oid] = (verdict, kind, payload)

    # -- pages ------------------------------------------------------------------------------------------
    def page(self, pid, parent, root, depth, prefetched=False):
        if pid in self.skip_rows:
            return None
        if pid not in self.force_pages:
            return super().page(pid, parent, root, depth, prefetched)   # new pages; re-listed rows (reuse rule)
        data = None
        if prefetched:   # forced row: fresh JSON from the re-listed data source (phase A ran first)
            got = self.s.q("SELECT data FROM objects WHERE id=? AND kind='page'", (pid,))
            data = json.loads(got[0][0]) if got else None
        if data is None:
            try:
                data = self.c.retrieve_page(pid)
            except NotionAPIError as e:
                if e.status in GONE_STATUSES:
                    self._note_found(pid, "gone", "page", f"{e.status} {e.code}")
                    return None
                raise
        if _trashed(data):
            self._note_found(pid, "trashed", "page", data)
            return None
        return self._full(pid, parent, root, depth, data)

    def _full(self, pid, parent, root, depth, data):
        rows = self.s.q("SELECT id, type FROM blocks WHERE page_id=?", (pid,))
        with self.mu:
            self.walked.setdefault(pid, {"blocks": {r[0] for r in rows},
                                         "kids": {r[0]: r[1] for r in rows if r[1] in CHILD_TYPES}})
        # page-level files (cover/icon/files properties) are re-recorded by _files_of_page
        self.s.x("DELETE FROM files WHERE owner_id=? AND owner_kind='page'", (pid,))
        return super()._full(pid, parent, root, depth, data)

    def _hash(self, owner, owner_kind, field, url, name):
        """BlobInventory._hash with self.blobs_dir instead of the module-level BLOBS."""
        key = f"{owner}:{field}"
        if not url:
            return
        nm = name or url.split("?")[0].rsplit("/", 1)[-1]
        try:
            digest, size, ctype = _fetch_blob(self.http, url, self.blobs_dir)
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, size, digest, "ok", ctype[:100] or None))
        except Exception as e:  # noqa: BLE001 - recorded like snapshot.py; retried by the asset pass
            self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                     (key, owner, owner_kind, field, nm, None, None, "error", f"{type(e).__name__}: {e}"[:300]))

    def _source_owns(self, page_id, b):
        """Synced reference whose children the base stores under the SOURCE block (another page): do not
        walk it (listing a reference returns the source's block ids; blocks.id is the global PK, so walking
        would re-own the source page's rows). A reference whose listing this page owned in the base is
        walked as before."""
        src = _nid((((b.get("synced_block") or {}).get("synced_from")) or {}).get("block_id"))
        if not src:
            return False
        with self.prev_lock:
            if self.prev.execute("SELECT 1 FROM blocks WHERE parent_id=? LIMIT 1", (b["id"],)).fetchone():
                return False
            row = self.prev.execute("SELECT page_id FROM blocks WHERE id=?", (src,)).fetchone()
        if row and row[0] != page_id:
            with self.mu:
                self.synced_skipped.append((page_id, b["id"], src))
            return True
        return False

    def _walk_children(self, page_id, block_id, root, depth):
        """nmig Inventory._walk_children with one change: the synced-reference rule (_source_owns)."""
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
            elif b.get("has_children") and not (t == "synced_block" and self._source_owns(page_id, b)):
                try:
                    self._walk_children(page_id, b["id"], root, depth)
                except NotionAPIError as e:
                    if e.status not in (403, 404):
                        raise
                    self.s.event("anomaly", b["id"],
                                 f"children of {t} block not readable via API ({e.status} {e.code}): {e.message}"[:600])
            if t in FILE_BLOCKS and (b.get(t) or {}).get("type") == "file":
                self._hash(b["id"], "block", t, (b[t].get("file") or {}).get("url"), b[t].get("name"))

    # -- databases (same data shapes as snapshot._database_with_templates) --------------------------------
    def _ds_state(self, did):
        out = {}
        for dsid, data in self.s.q("SELECT id, data FROM objects WHERE kind='data_source' AND parent_id=?", (did,)):
            try:
                d = json.loads(data)
            except ValueError:
                d = {}
            out[dsid] = (list(d.get("_row_ids") or []),
                         list(d.get("_template_ids") or []) if "_template_ids" in d else None)
        return out

    def database(self, did, parent, root, depth):
        forced = did in self.force_dbs
        try:
            db = self.c.retrieve_database(did)
        except NotionAPIError as e:
            if forced and e.status in GONE_STATUSES:
                self._note_found(did, "gone", "database", f"{e.status} {e.code}")
                return
            raise
        if forced and _trashed(db):
            self._note_found(did, "trashed", "database", db)
            return
        old = self._ds_state(did)
        try:
            db["_views"] = self.c.list_views(did)
        except NotionAPIError as e:
            db["_views_error"] = f"{e.status} {e.code}"
        self.s.put(did, "database", parent, root, depth, db)
        new = {}
        for ds_ref in db.get("data_sources", []):
            dsid = ds_ref["id"]
            ds = self.c.retrieve_data_source(dsid)
            rows = self.c.query_data_source(dsid)
            ds["_row_ids"] = [r["id"] for r in rows]
            try:
                tpls = _snap.list_templates(self.c, dsid)
                ds["_templates"] = tpls
                ds["_template_ids"] = [t["id"] for t in tpls if t.get("id")]
            except NotionAPIError as e:
                ds["_templates_error"] = f"{e.status} {e.code}"
            self.s.put(dsid, "data_source", did, root, depth, ds)
            for r in rows:
                self.s.put(r["id"], "page", did, root, depth + 1, r)
                self.s.enqueue(r["id"], "row", did, root, depth + 1)
            # every listed row goes back through the crawler: fresh row JSON must never sit on stale blocks
            for chunk in _chunks(ds["_row_ids"]):
                self.s.x("UPDATE work SET status='pending', attempts=0, parent_id=?, root_id=?, depth=? "
                         f"WHERE kind='row' AND status!='running' AND id IN ({','.join('?' * len(chunk))})",
                         [did, root, depth + 1] + chunk)
            for tid in ds.get("_template_ids", []):
                self.s.enqueue(tid, "page", did, root, depth + 1)
            new[dsid] = ds
        with self.mu:
            self.dbs_relisted.add(did)
            for dsid, (rids, tids) in old.items():
                nd = new.get(dsid)
                if nd is None:
                    self.ds_removed.append((did, dsid))
                    gone = set(rids) | set(tids or [])
                else:
                    gone = set(rids) - set(nd["_row_ids"])
                    if tids is not None and "_template_ids" in nd:
                        gone |= set(tids) - set(nd["_template_ids"])
                for x in gone:
                    self.db_removed.append((did, x))
                    self.skip_rows.add(x)


# ------------------------------------------------------------------------------------------------ the job
class _Job:
    def __init__(self, inv, store, client, base_path, roots, stats, base_markers=()):
        self.inv, self.s, self.c = inv, store, client
        self.base_path, self.roots, self.stats = base_path, roots, stats
        self.root_ids = {_nid(r.get("id")) for r in roots if isinstance(r, dict)}
        self.base_markers = set(base_markers)   # trash markers of a chained base (content dropped)
        self.restored = set()                   # base markers found alive again (re-fetched in this run)
        self.pending_pages, self.pending_dbs = set(), set()
        self.done_pages, self.done_dbs = set(), set()
        self.analysed = set()
        self.found_done, self.removed_done, self.dsrem_done = set(), 0, 0
        self.trashed = {}        # id -> record
        self.pruned = {}         # id -> record
        self.moved = []
        self.moved_roots = set()
        self.synced_skipped = set()
        self.synced_repaired = 0

    # -- copy queries ------------------------------------------------------------------------------------
    def obj(self, oid, kind=None):
        """(kind, parent_id, root_id, depth, data) of a page/database in the copy, or None."""
        kinds = (kind,) if kind else ("database", "page")
        for k in kinds:
            r = self.s.q("SELECT parent_id, root_id, depth, data FROM objects WHERE id=? AND kind=?", (oid, k))
            if r:
                return (k, r[0][0], r[0][1], r[0][2], r[0][3])
        return None

    def work_kinds(self, oid):
        return {r[0] for r in self.s.q("SELECT kind FROM work WHERE id=?", (oid,))}

    def container(self, oid):
        o = self.obj(oid) if oid else None
        return (o[0], oid) if o else None

    def children_map(self):
        m = {}
        for oid, kind, parent in self.s.q("SELECT id, kind, parent_id FROM objects"):
            if parent:
                m.setdefault(parent, []).append((oid, kind))
        return m

    def subtree(self, oid, cmap=None):
        cmap = cmap if cmap is not None else self.children_map()
        seen, order, stack = set(), [], [oid]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            order.append(x)
            stack.extend(c for c, _k in cmap.get(x, ()))
        return order

    # -- forcing -------------------------------------------------------------------------------------------
    def force_page(self, pid):
        if not pid or pid in self.trashed or pid in self.pruned:
            return
        o = self.obj(pid, "page")
        if o is None:
            return
        if "row" in self.work_kinds(pid) and o[1]:
            self.force_db(o[1])   # a row's JSON comes from its data source's query (phase A)
        if pid not in self.done_pages and pid not in self.inv.walked:   # walked here = fresh already
            self.pending_pages.add(pid)

    def force_db(self, did):
        if not did or did in self.trashed or did in self.pruned or did in self.done_dbs \
                or did in self.inv.dbs_relisted:
            return
        if self.obj(did, "database") is not None:
            self.pending_dbs.add(did)

    def force_container(self, c):
        if not c:
            return
        if c[0] == "page":
            self.force_page(c[1])
        else:
            self.force_db(c[1])

    def handled(self, oid):
        return oid in self.trashed or oid in self.pruned

    def refetched(self, oid):
        return oid in self.inv.walked or oid in self.inv.dbs_relisted

    def marker(self, oid):
        """A trash marker of the base that this run has neither re-fetched, re-marked nor pruned."""
        return (oid in self.base_markers and oid not in self.restored and not self.handled(oid)
                and not self.refetched(oid))

    def marker_tops(self):
        """Base markers whose parent is not a marker (the roots of the marker subtrees)."""
        tops = []
        for x in sorted(self.base_markers):
            o = self.obj(x)
            if o is not None and o[1] not in self.base_markers:
                tops.append(x)
        return tops

    def recheck_markers(self, oids):
        """Trash markers of a chained base are re-read with one GET each, top-down: still trashed -> the marker
        and its subtree stay; gone -> pruned; alive under a container that is still a marker -> stays; alive
        elsewhere in scope (or a sidebar root) -> RESTORED: re-walked (page) / re-listed (database) together
        with its container (whose listing must show it again), and its marker children are checked the same
        way; alive out of scope -> pruned."""
        cmap = self.children_map()
        stack = sorted(set(oids), reverse=True)
        while stack:
            x = stack.pop()
            if not self.marker(x):
                continue
            o = self.obj(x)
            if o is None:
                continue
            kind = o[0]
            verdict, payload, owner = self.probe(x, kind)
            self.stats["markers_rechecked"] += 1
            if verdict == "gone":
                self.prune(x, f"gone ({payload}); was a trash marker")
                continue
            if verdict == "trashed":
                continue
            old = self.container(o[1]) if o[1] else None
            if x in self.root_ids:
                owner = None
            elif owner[0] == "out":
                self.prune(x, "restored out of scope; was a trash marker")
                continue
            elif self.marker(owner[1]) or owner[1] in self.trashed:
                continue          # under a container that is still a marker (trashed ancestor)
            self.restored.add(x)
            log.info("trash marker %s %s is alive again: re-fetching it", kind, x)
            if owner is not None and owner != old and owner[0] == "page":
                self.reparent(x, kind, o[1], owner[1])
            if kind == "page":
                self.force_page(x)
            else:
                self.force_db(x)
            if owner is not None:
                self.force_container(owner)
                if old is not None and old != owner and not self.marker(old[1]):
                    self.force_container(old)
            stack.extend(c for c, kc in cmap.get(x, ()) if kc != "data_source" and self.marker(c))

    # -- Notion probes (GET only) --------------------------------------------------------------------------
    def locate(self, ref):
        """Nearest in-snapshot container of an API `parent` reference: ("page"|"database", id) or ("out", None)."""
        for _ in range(MAX_HOPS):
            if not isinstance(ref, dict):
                return ("out", None)
            t = ref.get("type")
            if t == "page_id":
                x = _nid(ref.get("page_id"))
                if x and self.obj(x, "page"):
                    return ("page", x)
                if x is None:
                    return ("out", None)
                d = self._get("page", x)
            elif t == "block_id":
                x = _nid(ref.get("block_id"))
                if x is None:
                    return ("out", None)
                r = self.s.q("SELECT page_id FROM blocks WHERE id=?", (x,))
                if r and r[0][0] and self.obj(r[0][0], "page"):
                    return ("page", r[0][0])
                d = self._get("block", x)
            elif t == "data_source_id":
                ds = _nid(ref.get("data_source_id"))
                r = self.s.q("SELECT parent_id FROM objects WHERE id=? AND kind='data_source'", (ds,)) if ds else []
                if r and r[0][0] and self.obj(r[0][0], "database"):
                    return ("database", r[0][0])
                db = _nid(ref.get("database_id"))
                if db and self.obj(db, "database"):
                    return ("database", db)
                if ds is None:
                    return ("out", None)
                d = self._get("data_source", ds)
            elif t == "database_id":
                x = _nid(ref.get("database_id"))
                if x and self.obj(x, "database"):
                    return ("database", x)
                if x is None:
                    return ("out", None)
                d = self._get("database", x)
            else:   # workspace (not a sidebar root: roots have no parent) or unknown
                return ("out", None)
            if d is None:
                return ("out", None)
            ref = d.get("parent")
        raise RefetchIncomplete(f"parent chain longer than {MAX_HOPS} hops")

    def _get(self, kind, oid):
        try:
            if kind == "page":
                return self.c.retrieve_page(oid)
            if kind == "database":
                return self.c.retrieve_database(oid)
            if kind == "block":
                return self.c.retrieve_block(oid)
            return self.c.retrieve_data_source(oid)
        except NotionAPIError as e:
            if e.status in GONE_STATUSES:
                return None
            raise

    def probe(self, oid, kind):
        """("gone", err, None) | ("trashed", json, None) | ("alive", json, located_container)."""
        try:
            d = self.c.retrieve_page(oid) if kind == "page" else self.c.retrieve_database(oid)
        except NotionAPIError as e:
            if e.status in GONE_STATUSES:
                return ("gone", f"{e.status} {e.code}", None)
            raise
        if _trashed(d):
            return ("trashed", d, None)
        return ("alive", d, self.locate(d.get("parent")))

    # -- structural changes ---------------------------------------------------------------------------------
    def _drop_content(self, page_ids):
        """Delete blocks, files and comments of these pages (and files of their blocks)."""
        page_ids = list(page_ids)
        bids = []
        for ch in _chunks(page_ids):
            ph = ",".join("?" * len(ch))
            bids += [r[0] for r in self.s.q(f"SELECT id FROM blocks WHERE page_id IN ({ph})", ch)]
            self.s.x(f"DELETE FROM blocks WHERE page_id IN ({ph})", ch)
            self.s.x(f"DELETE FROM comments WHERE page_id IN ({ph})", ch)
        for ch in _chunks(page_ids + bids):
            self.s.x(f"DELETE FROM files WHERE owner_id IN ({','.join('?' * len(ch))})", ch)
        return len(bids)

    def prune(self, oid, reason):
        """Remove an object that left the tree (unreadable / moved out of scope) with its whole subtree."""
        if oid in self.pruned:
            return
        ids = self.subtree(oid)
        kinds = {}
        for ch in _chunks(ids):
            for i, k in self.s.q(f"SELECT id, kind FROM objects WHERE id IN ({','.join('?' * len(ch))})", ch):
                kinds.setdefault(i, set()).add(k)
        pages = [i for i in ids if "page" in kinds.get(i, ())]
        nblocks = self._drop_content(pages)
        for ch in _chunks(ids):
            ph = ",".join("?" * len(ch))
            self.s.x(f"DELETE FROM objects WHERE id IN ({ph})", ch)
            self.s.x(f"DELETE FROM work WHERE id IN ({ph})", ch)
        for i in ids:
            self.trashed.pop(i, None)
            self.pruned[i] = {"id": i, "kinds": sorted(kinds.get(i, ())), "reason": reason if i == oid else
                              f"under {oid}: {reason}", "was_marker": i in self.base_markers}
        self.stats["blocks_dropped"] += nblocks
        log.info("pruned %s (%s): %d objects, %d blocks", oid, reason, len(ids), nblocks)

    def mark_trashed(self, oid, kind, data, via):
        """Keep the object with its fresh API JSON (in_trash), drop its content, then re-read every descendant
        (probe) the same way: trashed or under a trashed ancestor -> marked; unreadable -> pruned; alive
        elsewhere -> its container is re-fetched; alive out of scope -> pruned."""
        cmap = self.children_map()
        stack = [(oid, kind, data, via)]
        while stack:
            x, kx, dx, vx = stack.pop()
            if x in self.trashed or x in self.pruned:
                continue
            o = self.obj(x, kx)
            if o is None:
                continue
            if kx == "database":
                try:
                    old = json.loads(o[4])
                except ValueError:
                    old = {}
                for k, v in old.items():           # snapshot annotations (_views, _views_error)
                    if k.startswith("_") and k not in dx:
                        dx[k] = v
            self.s.x("UPDATE objects SET data=?, fetched_at=? WHERE id=? AND kind=?",
                     (json.dumps(dx, ensure_ascii=False), _utcnow(), x, kx))
            nblocks = self._drop_content([x]) if kx == "page" else 0
            self.stats["blocks_dropped"] += nblocks
            self.trashed[x] = {"id": x, "kind": kx, "title": _title(kx, dx), "parent": o[1], "via": vx,
                               "in_trash_reported": bool(_trashed(dx)), "was_marker": x in self.base_markers}
            if x in self.root_ids:
                for r in self.roots:
                    if _nid(r.get("id")) == x:
                        r["in_trash"] = True
            for c, kc in cmap.get(x, ()):
                if kc == "data_source" or self.handled(c):
                    continue
                verdict, payload, owner = self.probe(c, kc)
                if verdict == "gone":
                    self.prune(c, f"gone ({payload}) under trashed {x}")
                elif verdict == "trashed":
                    stack.append((c, kc, payload, "own"))
                elif owner[0] != "out" and (owner[1] == x or owner[1] in self.trashed):
                    stack.append((c, kc, payload, f"ancestor {x}"))
                else:
                    self.alive_elsewhere(c, kc, owner, expected=("page" if kx == "page" else "database", x))

    def alive_elsewhere(self, oid, kind, owner, expected):
        """An object that left `expected` but is alive under `owner` (a located container or out of scope)."""
        if owner[0] == "out":
            self.prune(oid, "moved out of scope")
            return
        if owner == expected:
            raise RefetchIncomplete(f"{kind} {oid}: its API parent is still {expected[1]}, which no longer lists it")
        if owner[1] in self.trashed:
            o = self.obj(oid, kind)
            self.mark_trashed(oid, kind, json.loads(o[4]) if o else {}, f"ancestor {owner[1]}")
            return
        walked = owner[1] in self.inv.walked if owner[0] == "page" else owner[1] in self.inv.dbs_relisted
        if not walked:
            self.force_container(owner)      # its listing will show the object -> moved_in
            return
        if owner[0] == "page":
            listed = bool(self.s.q("SELECT 1 FROM blocks WHERE id=? AND page_id=?", (oid, owner[1])))
        else:
            listed = bool(self.obj(oid, kind)) and self.obj(oid, kind)[1] == owner[1]
        if not listed:
            raise RefetchIncomplete(f"{kind} {oid} reports parent {owner[1]}, whose fresh listing does not contain it "
                                    "(changed during the refetch?)")

    def reparent(self, oid, kind, old_parent, new_parent):
        """parent_id / root_id / depth of `oid` follow the page `new_parent` (its subtree: fix_root_depth)."""
        np = self.obj(new_parent, "page")
        root, depth = (np[2], (np[3] or 0) + 1) if np else (None, None)
        self.s.x("UPDATE objects SET parent_id=?, root_id=?, depth=? WHERE id=? AND kind=?",
                 (new_parent, root, depth, oid, kind))
        self.s.x("UPDATE work SET parent_id=?, root_id=?, depth=? WHERE id=? AND kind=?",
                 (new_parent, root, depth, oid, kind))
        self.moved.append({"id": oid, "kind": kind, "from": old_parent, "to": new_parent})
        self.moved_roots.add(oid)

    def moved_in(self, oid, kind, old_parent, new_parent):
        """`oid` appeared under the re-walked page `new_parent` and used to live under `old_parent`."""
        self.reparent(oid, kind, old_parent, new_parent)
        if kind == "page" and oid not in self.inv.walked:
            self.force_page(oid)             # its JSON (parent) is stale
        elif kind == "database" and oid not in self.inv.dbs_relisted:
            self.force_db(oid)
        self.force_container(self.container(old_parent))

    def fix_root_depth(self, oid):
        """root_id / depth of a moved subtree follow the new parent (data sources: depth of their database)."""
        o = self.obj(oid)
        if o is None:
            return
        cmap = self.children_map()
        stack = [(oid, o[2], o[3])]
        seen = set()
        while stack:
            x, root, depth = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for c, kc in cmap.get(x, ()):
                d = depth if kc == "data_source" else (depth or 0) + 1
                self.s.x("UPDATE objects SET root_id=?, depth=? WHERE id=? AND kind=?", (root, d, c, kc))
                self.s.x("UPDATE work SET root_id=?, depth=? WHERE id=?", (root, d, c))
                stack.append((c, root, d))

    # -- rounds ------------------------------------------------------------------------------------------------
    def reset(self, oid, kinds):
        """Queue a forced object again (its base work row goes back to pending; a missing row is created)."""
        ph = ",".join("?" * len(kinds))
        n = self.s.x(f"UPDATE work SET status='pending', attempts=0, error=NULL WHERE id=? AND kind IN ({ph})",
                     [oid] + list(kinds)).rowcount
        if n == 0:
            o = self.obj(oid, "database" if kinds == ("database",) else "page")
            if o is not None:
                self.s.enqueue(oid, kinds[0], o[1], o[2] or oid, o[3] or 0)

    def run(self, max_rounds):
        rnd = 0
        while self.pending_pages or self.pending_dbs or self.s.q("SELECT 1 FROM work WHERE status='pending' LIMIT 1"):
            rnd += 1
            if rnd > max_rounds:
                raise RefetchIncomplete(f"still changing after {max_rounds} rounds "
                                        f"(pages {sorted(self.pending_pages)[:5]}, databases {sorted(self.pending_dbs)[:5]})")
            dbs, pages = sorted(self.pending_dbs - self.done_dbs), sorted(self.pending_pages - self.done_pages)
            self.pending_dbs, self.pending_pages = set(), set()
            # phase A: databases (fresh data-source listings and row JSON before any row is walked)
            for did in dbs:
                self.inv.force_dbs.add(did)
                self.reset(did, ("database",))
            self.inv.run_pending("database")
            # phase B: forced pages/rows/templates + everything the walks queue (new children, re-listed rows)
            for pid in pages:
                self.inv.force_pages.add(pid)
                self.reset(pid, ("page", "row"))
            self.inv.run_pending()
            self.done_dbs |= set(dbs)
            self.done_pages |= set(pages)
            self.stats["rounds"].append({"databases": len(dbs), "pages": len(pages)})
            if self.inv.failures:
                f = self.inv.failures[:5]
                raise RefetchIncomplete(f"{len(self.inv.failures)} work item(s) failed, e.g. {f}")
            self.analyse()
            if not (self.pending_pages or self.pending_dbs):
                self.check_synced()
        return rnd

    def analyse(self):
        inv = self.inv
        # 1. forced objects found trashed / unreadable
        for oid, (verdict, kind, payload) in list(inv.found.items()):
            if oid in self.found_done:
                continue
            self.found_done.add(oid)
            o = self.obj(oid, kind)
            if o is None:
                continue
            parent = o[1]
            if verdict == "gone":
                if parent is None and oid in self.root_ids:
                    raise RefetchIncomplete(f"root {oid} is no longer readable ({payload}): run a full refresh")
                self.prune(oid, f"gone ({payload})")
            else:
                self.mark_trashed(oid, kind, payload, "own")
            self.force_container(self.container(parent))
        # 2. structure of the pages walked in this round
        walked_now = [p for p in inv.walked if p not in self.analysed]
        probes, listed_markers = [], []
        for pid in walked_now:
            self.analysed.add(pid)
            if self.handled(pid):
                continue
            before = inv.walked[pid]["kids"]
            now = dict(self.s.q("SELECT id, type FROM blocks WHERE page_id=? AND type IN ('child_page','child_database')",
                                (pid,)))
            for cid, typ in now.items():
                if self.marker(cid):
                    listed_markers.append(cid)   # a trash marker of the base listed again: restored?
                    continue
                if cid in before:
                    continue
                k = _CONTAINER[typ]
                o = self.obj(cid, k)
                if o is None or o[1] == pid or o[1] is None:
                    continue     # crawled as new / already ours / a block pointing at a sidebar root
                verdict, payload, owner = self.probe(cid, k)
                if verdict == "alive" and owner == ("page", pid):
                    self.moved_in(cid, k, o[1], pid)
                else:
                    self.stats["references_added"] += 1   # block points at an object whose API parent is elsewhere
            for cid, typ in before.items():
                if cid in now:
                    continue
                k = _CONTAINER[typ]
                o = self.obj(cid, k)
                if o is not None and o[1] == pid:
                    probes.append((cid, k, ("page", pid)))
        if listed_markers:
            self.recheck_markers(listed_markers)
        # 3. rows / templates / data sources that left a re-listed database
        for did, x in inv.db_removed[self.removed_done:]:
            o = self.obj(x, "page")
            if o is not None and o[1] == did:
                probes.append((x, "page", ("database", did)))
        self.removed_done = len(inv.db_removed)
        for did, dsid in inv.ds_removed[self.dsrem_done:]:
            self.s.x("DELETE FROM objects WHERE id=? AND kind='data_source'", (dsid,))
            self.pruned[dsid] = {"id": dsid, "kinds": ["data_source"], "reason": f"no longer listed by database {did}"}
        self.dsrem_done = len(inv.ds_removed)
        # 4. re-walked pages that moved themselves (old parent from the base, new one from the fresh JSON)
        for pid in walked_now:
            if self.handled(pid):
                continue
            with inv.prev_lock:
                b = inv.prev.execute("SELECT parent_id FROM objects WHERE id=? AND kind='page'", (pid,)).fetchone()
            if not b or b[0] is None:
                continue          # new page, or a sidebar root
            o = self.obj(pid, "page")
            owner = self.locate(json.loads(o[4]).get("parent"))
            old = self.container(b[0])
            if old is not None and owner == old:
                continue
            if owner[0] == "out":
                self.prune(pid, "moved out of scope")
            else:
                self.force_container(owner)
            self.force_container(old)
        # 5. probe what disappeared
        for cid, k, expected in probes:
            if self.handled(cid):
                continue
            verdict, payload, owner = self.probe(cid, k)
            if verdict == "gone":
                self.prune(cid, f"gone ({payload})")
            elif verdict == "trashed":
                self.mark_trashed(cid, k, payload, "own")
            elif owner[0] != "out" and owner[1] in self.trashed:
                self.mark_trashed(cid, k, payload, f"ancestor {owner[1]}")
            else:
                self.alive_elsewhere(cid, k, owner, expected)

    def check_synced(self):
        """A reference was not walked because the base stores its content under the source block on another
        page. If that source is gone now, walk the reference (repair) so its content is not lost."""
        for pid, ref, src in list(self.inv.synced_skipped):
            self.synced_skipped.add((pid, ref))
            if self.handled(pid) or not self.s.q("SELECT 1 FROM blocks WHERE id=? AND page_id=?", (ref, pid)):
                continue
            row = self.s.q("SELECT has_children FROM blocks WHERE id=?", (src,))
            if row and (not row[0][0] or self.s.q("SELECT 1 FROM blocks WHERE parent_id=? LIMIT 1", (src,))):
                continue
            w = self.s.q("SELECT root_id, depth FROM work WHERE id=? AND kind IN ('page','row')", (pid,))
            root, depth = (w[0][0], w[0][1]) if w else (pid, 0)
            self.inv._walk_children(pid, ref, root, depth)
            self.synced_repaired += 1
        self.inv.synced_skipped = []

    # -- files ------------------------------------------------------------------------------------------------
    def files_pass(self, do_assets):
        """Remove file rows of blocks that no longer exist; second asset pass (assets_fetch semantics) for the
        re-walked pages: hosted embeds without an ok row, and non-ok file rows."""
        inv = self.inv
        pages = [p for p in inv.walked if not self.handled(p) and self.obj(p, "page")]
        old_ids = set()
        for p in pages:
            old_ids |= inv.walked[p]["blocks"]
        stale = []
        for ch in _chunks(sorted(old_ids)):
            present = {r[0] for r in self.s.q(f"SELECT id FROM blocks WHERE id IN ({','.join('?' * len(ch))})", ch)}
            stale += [i for i in ch if i not in present]
        removed = 0
        for ch in _chunks(stale):
            removed += self.s.x(f"DELETE FROM files WHERE owner_id IN ({','.join('?' * len(ch))})", ch).rowcount
        self.stats["files"]["removed_stale"] = removed
        if not do_assets:
            return
        todo, bids = [], []
        for ch in _chunks(pages):
            ph = ",".join("?" * len(ch))
            for bid, typ, data in self.s.q(f"SELECT id, type, data FROM blocks WHERE page_id IN ({ph})", ch):
                bids.append(bid)
                if typ not in EMBED_TYPES:
                    continue
                url = ((json.loads(data).get(typ) or {}).get("url")) or ""
                if _assets.HOSTED.match(url):
                    key = f"{bid}:embed"
                    row = self.s.q("SELECT status FROM files WHERE key=?", (key,))
                    if not row or row[0][0] != "ok":
                        todo.append((key, bid, "block", "embed", url.split("?")[0].rsplit("/", 1)[-1]))
        keys = {t[0] for t in todo}
        for ch in _chunks(pages + bids):
            ph = ",".join("?" * len(ch))
            for row in self.s.q(f"SELECT key, owner_id, owner_kind, field, name FROM files WHERE status != 'ok' "
                                f"AND owner_id IN ({ph})", ch):
                if row[0] not in keys:
                    todo.append(tuple(row))
        for key, owner, okind, field, name in todo:
            try:
                url = _assets.fresh_url(self.c, key)
                if not url:
                    raise RuntimeError("no url on re-read")
                d, size, ctype = _fetch_blob(inv.http, url, inv.blobs_dir)
                self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                         (key, owner, okind, field, name, size, d, "ok", ctype[:100] or None))
                self.stats["files"]["asset_ok"] += 1
            except Exception as e:  # noqa: BLE001 - recorded like assets_fetch.py
                self.s.x("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                         (key, owner, okind, field, name, None, None, "error", f"{type(e).__name__}: {e}"[:300]))
                self.stats["files"]["asset_error"] += 1


# ------------------------------------------------------------------------------------------------ main API
def _base_markers(con, meta):
    """Trash markers of the base: meta.trash_markers (a refetch output) plus any page/database object whose JSON
    says in_trash/archived. A refetch output without meta.trash_markers (older version) is refused."""
    markers = set()
    if "targeted_refetch" in meta and "trash_markers" not in meta:
        raise RefetchError("the base is a refetch output without meta.trash_markers (older wb_refetch): its trash "
                           "markers cannot be re-checked; use a full snapshot as base")
    if "trash_markers" in meta:
        try:
            lst = json.loads(meta["trash_markers"])
        except ValueError:
            lst = None
        if not isinstance(lst, list) or not all(isinstance(x, str) for x in lst):
            raise RefetchError("base meta.trash_markers is not a JSON list of ids")
        markers |= set(lst)
    for oid, data in con.execute("SELECT id, data FROM objects WHERE kind IN ('page','database') AND "
                                 "(instr(data, '\"in_trash\": true') OR instr(data, '\"in_trash\":true') OR "
                                 "instr(data, '\"archived\": true') OR instr(data, '\"archived\":true'))"):
        try:
            if _trashed(json.loads(data)):
                markers.add(oid)
        except ValueError:
            pass
    present = set()
    for ch in _chunks(sorted(markers)):
        present |= {r[0] for r in con.execute(f"SELECT id FROM objects WHERE kind IN ('page','database') AND id IN "
                                              f"({','.join('?' * len(ch))})", ch)}
    return present


def _inspect_base(base, page_ids, ds_ids):
    """Check the base is a finished snapshot and classify the requested ids. -> (meta, pages, dbs, markers)."""
    con = open_immutable(base)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"meta", "work", "objects", "blocks", "files"} - tables
        if missing:
            raise RefetchError(f"base snapshot lacks tables {sorted(missing)}")
        meta = {k: v for k, v in con.execute("SELECT key, value FROM meta") if k != "roots"}
        if not meta.get("finished_at"):
            raise RefetchError("base snapshot has no meta.finished_at (unfinished crawl)")
        if not meta.get("workspace_id"):
            raise RefetchError("base snapshot has no meta.workspace_id (the token-workspace guard needs it)")
        open_items = con.execute("SELECT COUNT(*) FROM work WHERE status IN ('pending','running')").fetchone()[0]
        if open_items:
            raise RefetchError(f"base snapshot has {open_items} unfinished work items")
        markers = _base_markers(con, meta)
        pages, dbs, unknown = set(), set(), []
        for i in page_ids:
            kinds = {r[0]: r[1] for r in con.execute("SELECT kind, parent_id FROM objects WHERE id=?", (i,))}
            work = {r[0] for r in con.execute("SELECT kind FROM work WHERE id=?", (i,))}
            if "database" in kinds and "database" in work:
                dbs.add(i)
            elif "page" in kinds and work & {"page", "row"}:
                pages.add(i)
                if "row" in work and kinds["page"]:
                    dbs.add(kinds["page"])
            elif "data_source" in kinds and kinds["data_source"]:
                dbs.add(kinds["data_source"])
            else:
                unknown.append(i)
        for d in ds_ids:
            r = con.execute("SELECT parent_id FROM objects WHERE id=? AND kind='data_source'", (d,)).fetchone()
            if not r or not r[0]:
                unknown.append(d)
            else:
                dbs.add(r[0])
        if unknown:
            raise RefetchError(f"{len(unknown)} id(s) are not pages/rows/templates/databases/data sources of the base "
                               f"snapshot (new objects are found through their parent): {unknown[:10]}")
        return meta, pages, dbs, markers
    finally:
        con.close()


def _norm_ids(ids, what):
    out = []
    for x in ids or ():
        i = _nid(x)
        if i is None:
            raise RefetchError(f"{what}: not a Notion id: {str(x)[:80]!r}")
        if i not in out:
            out.append(i)
    return out


def _gated(client):
    """True when every request of `client` passes the shared cross-process RateGate (directly, or behind a
    per-process cap: readonly_client._CapThenGate)."""
    limiter = getattr(client, "limiter", None)
    return isinstance(limiter, rate_gate.RateGate) or isinstance(getattr(limiter, "gate", None), rate_gate.RateGate)


def refetch(base_snapshot: str, out_path: str, page_ids: list, data_source_ids: list, client, *,
            blobs_dir=None, workers=4, http=None, max_rounds=8, assets=True) -> dict:
    """Targeted refetch (module docstring). `client` must be a ReadOnlyClient behind the shared RateGate (see
    make_client). blobs_dir None -> <WORK>/blobs, refused when it resolves outside <WORK>. Raises RefetchError /
    RefetchIncomplete without publishing anything at out_path. Returns stats:
    {base, out, roots_json, requested, rounds, refetched_pages, reused_rows, relisted_databases, new_objects,
     trashed, pruned, moved, restored, trash_markers, markers_rechecked, references_added, synced_skipped,
     synced_repaired, blocks_dropped, files, requests, objects, blocks, started_at, finished_at, elapsed_s}."""
    t0 = time.time()
    if not isinstance(client, ReadOnlyClient):
        raise RefetchError("refetch needs a ReadOnlyClient (make_client / readonly_client.make_read_client)")
    if not _gated(client):
        raise RefetchError("refetch needs a client behind the shared RateGate (make_client, or make_read_client("
                           "..., gate=RateGate(...))): the 2.5 rps budget is shared by every Notion API process")
    base, out = os.path.abspath(base_snapshot), os.path.abspath(out_path)
    if not os.path.isfile(base):
        raise RefetchError(f"base snapshot not found: {base}")
    if os.path.realpath(base) == os.path.realpath(out):
        raise RefetchError("out_path must not be the base snapshot")
    for p in (out, out + ".roots.json", out + "-wal", out + "-shm", out + "-journal"):
        if os.path.lexists(p):
            raise RefetchError(f"refusing to overwrite {p}")
    roots_src = base + ".roots.json"
    if not os.path.isfile(roots_src):
        raise RefetchError(f"{roots_src} is missing (the exporter and verifier need it)")
    with open(roots_src, encoding="utf-8") as fh:
        roots = json.load(fh)
    if not isinstance(roots, list):
        raise RefetchError(f"{roots_src}: expected a JSON list")
    req_pages, req_ds = _norm_ids(page_ids, "page_ids"), _norm_ids(data_source_ids, "data_source_ids")
    blobs_dir = os.path.abspath(blobs_dir) if blobs_dir else _default_blobs_dir()
    sig0 = _sig(base)
    meta, pages, dbs, markers = _inspect_base(base, req_pages, req_ds)
    req0 = dict(client.stats)
    try:
        ws = ((client.me() or {}).get("bot") or {}).get("workspace_id")
    except NotionAPIError as e:
        raise RefetchIncomplete(f"users/me failed: {e.status} {e.code}") from e
    if ws != meta["workspace_id"]:
        raise RefetchError(f"client workspace {ws} != base snapshot workspace {meta['workspace_id']}")
    started = _utcnow()
    out_dir = os.path.dirname(out)
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, f".{os.path.basename(out)}.part-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp_roots = tmp + ".roots.json"
    store = inv = None
    stats = {"base": base, "out": out, "roots_json": out + ".roots.json",
             "requested": {"page_ids": req_pages, "data_source_ids": req_ds}, "rounds": [],
             "references_added": 0, "blocks_dropped": 0, "rate_gate": True, "markers_rechecked": 0,
             "files": {"removed_stale": 0, "asset_ok": 0, "asset_error": 0}}
    try:
        copy_snapshot(base, tmp)
        store = Store(tmp)   # WAL on the copy only
        inv = _TargetedInventory(client, store, prev_path=base, blobs_dir=blobs_dir, workers=workers,
                                 http=http, markers=markers)
        job = _Job(inv, store, client, base, roots, stats, base_markers=markers)
        for d in dbs:
            job.force_db(d)
        for p in pages:
            job.force_page(p)
        job.recheck_markers(job.marker_tops())     # chained base: never trust its trash markers
        job.run(max_rounds)
        for oid in sorted(job.moved_roots):
            if not job.handled(oid):
                job.fix_root_depth(oid)
        job.files_pass(assets)
        # a marker found alive must have been re-fetched (its content was dropped): fail closed otherwise
        for x in sorted(job.restored):
            if not job.handled(x) and not job.refetched(x):
                raise RefetchIncomplete(f"trash marker {x} is alive again but was not re-fetched")
        restored = sorted(x for x in markers if job.refetched(x) and not job.handled(x))
        markers_out = sorted(x for x in (markers - set(restored)) | set(job.trashed)
                             if x not in job.pruned and job.obj(x) is not None)
        for r in roots:
            if isinstance(r, dict) and r.get("in_trash") and _nid(r.get("id")) in restored:
                r["in_trash"] = False
        # stats
        with inv.prev_lock:
            base_objs = {(r[0], r[1]) for r in inv.prev.execute("SELECT id, kind FROM objects")}
        now_objs = store.q("SELECT id, kind, parent_id FROM objects")
        stats.update({
            "rounds": stats["rounds"],
            "refetched_pages": sorted(p for p in inv.walked if not job.handled(p)),
            "reused_rows": inv.reused,
            "relisted_databases": sorted(d for d in inv.dbs_relisted if not job.handled(d)),
            "new_objects": [{"id": i, "kind": k, "parent": p} for i, k, p in now_objs if (i, k) not in base_objs],
            "trashed": sorted(job.trashed.values(), key=lambda r: r["id"]),
            "pruned": sorted(job.pruned.values(), key=lambda r: r["id"]),
            "restored": [{"id": x, "kind": job.obj(x)[0]} for x in restored],
            "trash_markers": len(markers_out),
            "moved": job.moved,
            "synced_skipped": len(job.synced_skipped),
            "synced_repaired": job.synced_repaired,
            "tolerated_failures": inv.tolerated,
            "objects": dict(store.q("SELECT kind, COUNT(*) FROM objects GROUP BY kind")),
            "blocks": store.q("SELECT COUNT(*) FROM blocks")[0][0],
            "requests": {k: client.stats.get(k, 0) - req0.get(k, 0) for k in client.stats},
            "started_at": started,
        })
        finished = _utcnow()
        stats["finished_at"] = finished
        stats["elapsed_s"] = round(time.time() - t0, 3)
        summary = {k: stats[k] for k in ("requested", "rounds", "refetched_pages", "relisted_databases", "moved",
                                          "synced_repaired", "requests")}
        summary.update({"new_objects": [o["id"] for o in stats["new_objects"]],
                        "trashed": [t["id"] for t in stats["trashed"]], "pruned": [p["id"] for p in stats["pruned"]],
                        "restored": restored})
        for k, v in (("finished_at", finished), ("refetch_started_at", started), ("refetch_base", base),
                     ("refetch_base_finished_at", meta.get("finished_at")),
                     ("refetch_page_ids", json.dumps(req_pages)), ("refetch_data_source_ids", json.dumps(req_ds)),
                     ("trash_markers", json.dumps(markers_out)),
                     ("targeted_refetch", json.dumps(summary, ensure_ascii=False, sort_keys=True))):
            store.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (k, v))
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.db.execute("PRAGMA journal_mode=DELETE")   # a finished snapshot is one file
        store.db.close()
        store = None
        inv.prev.close()
        _fsync_file(tmp)
        wb_common.atomic_write(tmp_roots, (json.dumps(roots, ensure_ascii=False, indent=1)).encode("utf-8"))
        if _sig(base) != sig0:
            raise RefetchError("the base snapshot changed while it was being read")
        # publish without ever replacing anything (hard links; the .part files are removed below)
        for p in (out + "-wal", out + "-shm", out + "-journal"):
            if os.path.lexists(p):
                raise RefetchError(f"refusing to publish next to {p} (it appeared while the refetch ran)")
        _link_new(tmp_roots, out + ".roots.json")
        try:
            _link_new(tmp, out)
        except BaseException:
            _unlink_if_same(out + ".roots.json", tmp_roots)
            raise
        wb_common._fsync_dir(out_dir)
    except NotionAPIError as e:
        raise RefetchIncomplete(f"Notion API error during the refetch: {e.status} {e.code}") from e
    finally:
        if store is not None:
            try:
                store.db.close()
            except sqlite3.Error:
                pass
        if inv is not None:
            try:
                inv.prev.close()
            except sqlite3.Error:
                pass
            if inv._own_http:
                inv.http.close()
        for p in (tmp, tmp + "-wal", tmp + "-shm", tmp + "-journal", tmp_roots):
            if os.path.lexists(p):
                os.unlink(p)
    log.info("refetch %s -> %s: %d pages re-walked, %d new, %d trashed, %d pruned, %d restored, %s requests",
             os.path.basename(base), os.path.basename(out), len(stats["refetched_pages"]), len(stats["new_objects"]),
             len(stats["trashed"]), len(stats["pruned"]), len(stats["restored"]), stats["requests"].get("requests"))
    return stats


# ------------------------------------------------------------------------------------- base selection
def _state_dir(state_dir):
    return os.path.abspath(state_dir or wb_common.STATE)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise RefetchError(f"{path} is unreadable: {type(e).__name__}") from e


def _epoch(at):
    """ISO time (now_iso / _utcnow format) -> POSIX seconds, or None."""
    if not isinstance(at, str) or not at:
        return None
    try:
        d = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def read_adopted(*, state_dir=None):
    """The record written by record_adopted(), or None."""
    rec = _read_json(os.path.join(_state_dir(state_dir), ADOPTED_FILE))
    if rec is not None and not (isinstance(rec, dict) and isinstance(rec.get("snapshot"), str)
                                and _epoch(rec.get("at")) is not None):
        raise RefetchError(f"{ADOPTED_FILE} is malformed")
    return rec


def record_adopted(snapshot_path, *, plan_id=None, state_dir=None) -> dict:
    """Record that the export of the refetch output `snapshot_path` has just been PUBLISHED into the vault
    (publish_vault.py --adopt-writeback ran in apply mode, whatever its exit code, i.e. files may have been
    written). Until a refresh publishes again, choose_base() returns it, so the next write-back's refetch starts
    from what the vault now contains. Writes state/last_adopted_wb_snapshot.json atomically."""
    p = os.path.abspath(snapshot_path)
    if not os.path.isfile(p) or not os.path.isfile(p + ".roots.json"):
        raise RefetchError(f"not a refetch output (missing file or .roots.json): {p}")
    con = open_immutable(p)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key IN "
                                "('finished_at','targeted_refetch','trash_markers','refetch_base')"))
    except sqlite3.Error as e:
        raise RefetchError(f"not a snapshot: {p}: {e}") from e
    finally:
        con.close()
    if not meta.get("finished_at") or "targeted_refetch" not in meta or "trash_markers" not in meta:
        raise RefetchError(f"not a finished refetch output of this version: {p}")
    at_epoch = time.time()
    rec = {"snapshot": p, "finished_at": meta["finished_at"], "refetch_base": meta.get("refetch_base"),
           "plan_id": plan_id, "at": wb_common.now_iso(), "at_epoch": at_epoch}
    wb_common.atomic_write(os.path.join(_state_dir(state_dir), ADOPTED_FILE),
                           (json.dumps(rec, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return rec


def choose_base(*, state_dir=None) -> str:
    """Base snapshot of the next targeted refetch (WRITEBACK_SPEC §11 step 4): the snapshot the vault was LAST
    PUBLISHED from -- by publish time, the newer of the refresh's state/last_published_snapshot.txt (time:
    last_published.json "at" when it names the same snapshot, else the .txt mtime) and the write-back adoption
    recorded by record_adopted(). Fails closed (RefetchError) when the chosen snapshot is missing: falling back
    to an older one would publish stale content over adopted notes."""
    sd = _state_dir(state_dir)
    txt = os.path.join(sd, LAST_PUBLISHED_TXT)
    lp = lp_epoch = None
    try:
        with open(txt, encoding="utf-8") as fh:
            lp = fh.read().strip() or None
        mtime = os.path.getmtime(txt)
    except FileNotFoundError:
        lp = None
    except OSError as e:
        raise RefetchError(f"{txt} is unreadable: {type(e).__name__}") from e
    if lp:
        lp = os.path.abspath(lp)
        lpj = _read_json(os.path.join(sd, LAST_PUBLISHED_JSON))
        same = isinstance(lpj, dict) and isinstance(lpj.get("snapshot"), str) and os.path.abspath(lpj["snapshot"]) == lp
        lp_epoch = (_epoch(lpj.get("at")) if same else None) or mtime
    ad = read_adopted(state_dir=sd)
    if ad is not None:
        ad_epoch = ad.get("at_epoch") if isinstance(ad.get("at_epoch"), (int, float)) else _epoch(ad.get("at"))
        if lp is None or ad_epoch > lp_epoch:
            snap = os.path.abspath(ad["snapshot"])
            if not (os.path.isfile(snap) and os.path.isfile(snap + ".roots.json")):
                raise RefetchError(f"the last adopted write-back snapshot {snap} is missing: run a refresh first")
            return snap
    if lp is None:
        raise RefetchError(f"no published snapshot ({txt} missing or empty): run a refresh first")
    if not (os.path.isfile(lp) and os.path.isfile(lp + ".roots.json")):
        raise RefetchError(f"the last published snapshot {lp} (or its .roots.json) is missing")
    return lp


def prune_wb_snapshots(wb_dir=None, keep_days=7, *, now=None, keep=(), state_dir=None) -> list:
    """Delete snapshots/wb/snap_*.db (with -wal/-shm/-journal/.roots.json), orphan snap_*.db.roots.json and
    stale .part files older than keep_days (mtime). Never deletes the snapshot recorded by record_adopted()
    (the base of the next refetch); if that record is unreadable nothing is deleted. Only operates on a
    directory named 'wb'; never follows symlinks. -> removed paths."""
    wb_dir = os.path.abspath(wb_dir or WB_DIR)
    if os.path.basename(wb_dir) != "wb":
        raise ValueError(f"prune_wb_snapshots only works on a 'wb' directory: {wb_dir}")
    if not os.path.isdir(wb_dir):
        return []
    try:
        adopted = read_adopted(state_dir=state_dir)
    except RefetchError as e:
        log.warning("prune_wb_snapshots: nothing pruned: %s", e)
        return []
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    keep_real = {os.path.realpath(k) for k in keep}
    if adopted is not None:
        keep_real.add(os.path.realpath(adopted["snapshot"]))
    removed = []
    pat = glob.escape(wb_dir)
    for p in sorted(glob.glob(os.path.join(pat, "snap_*.db")) + glob.glob(os.path.join(pat, ".snap_*.db.part-*"))):
        if p.endswith(("-wal", "-shm", "-journal", ".roots.json")) and ".part-" in p:
            continue
        if os.path.islink(p) or not os.path.isfile(p) or os.path.realpath(p) in keep_real:
            continue
        if os.path.getmtime(p) >= cutoff:
            continue
        for suffix in ("", "-wal", "-shm", "-journal", ".roots.json"):
            q = p + suffix
            if os.path.isfile(q) and not os.path.islink(q):
                os.unlink(q)
                removed.append(q)
    for q in sorted(glob.glob(os.path.join(pat, "snap_*.db.roots.json"))):   # left by an interrupted publish
        db = q[:-len(".roots.json")]
        if os.path.lexists(db) or os.path.islink(q) or not os.path.isfile(q) or os.path.realpath(db) in keep_real:
            continue
        if os.path.getmtime(q) < cutoff:
            os.unlink(q)
            removed.append(q)
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Targeted refetch of a finished snapshot (read-only Notion API).")
    ap.add_argument("--base", required=True, help="finished base snapshot (opened read-only), or "
                    "'auto' = choose_base(): the snapshot the vault was last published from")
    ap.add_argument("--out", default=None, help="new snapshot path (default snapshots/wb/snap_<ts>_<rand>.db)")
    ap.add_argument("--pages", nargs="*", default=[], help="page / row / template / database ids to re-fetch")
    ap.add_argument("--pages-file", default=None, help="file with one id per line (# comments allowed)")
    ap.add_argument("--data-sources", nargs="*", default=[], help="data source ids to re-list")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--blobs", default=None, help="blob directory (default <WORK>/blobs; refused when it resolves "
                    "outside <WORK>, e.g. the dev sandbox's symlink to production)")
    ap.add_argument("--base-url", default=None, help="API base URL (tests: a local fake)")
    ap.add_argument("--token-file", default=None, help="read token file (default: the read connection's)")
    ap.add_argument("--gate-state", default=None, help="rate gate state file (default state/rate_gate.json)")
    ap.add_argument("--total-rps", type=float, default=rate_gate.DEFAULT_TOTAL_RPS)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    pages = list(a.pages)
    if a.pages_file:
        with open(a.pages_file, encoding="utf-8") as fh:
            pages += [ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()]
    client = make_client(base_url=a.base_url, gate_path=a.gate_state, total_rps=a.total_rps, token_path=a.token_file)
    try:
        base = choose_base() if a.base == "auto" else a.base
        st = refetch(base, a.out or default_out_path(), pages, a.data_sources, client, blobs_dir=a.blobs,
                     workers=a.workers)
    except RefetchIncomplete as e:
        print(f"INCOMPLETE: {e}", file=sys.stderr)
        return 3
    except RefetchError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
