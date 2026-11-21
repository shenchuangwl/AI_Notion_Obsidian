#!/usr/bin/env python3
"""State for write-back: `state/writeback.db` schema + migrations, and the `state/base/<sha>` store.

IMPL_CONTRACT §5.3. Tables = WRITEBACK_SPEC §3 + base_history.seed, rejections, wb_meta,
plan_items.decision/binds; block_lines columns as speccheck §6 block_lines.jsonl + seq.

Connections from connect() are in autocommit mode (isolation_level=None). Group several writes
atomically with `with wb_state.transaction(db): ...` (BEGIN IMMEDIATE; nested use joins the outer
transaction). The helpers below join a caller's open transaction and otherwise commit on their own.
"""
import contextlib
import os
import re
import sqlite3
import sys
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402

DB_PATH = os.path.join(wb_common.STATE, "writeback.db")
BASE_DIR = os.path.join(wb_common.STATE, "base")
# 1: initial. 2: confirmations gets an AUTOINCREMENT id (one row per confirmation event; apply,
#    apply --resume and rollback of the same plan item each keep their own audit row).
SCHEMA_VERSION = 2
ORIGINS = ("REMOTE", "WRITEBACK")
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class SchemaError(RuntimeError):
    """The existing database cannot be migrated automatically."""


class BaseCorrupt(RuntimeError):
    """A file in state/base does not hash to its name."""


# table -> (columns [(name, decl)], primary key tuple | None, indexes [(name, cols)])
# decl of a non-PK column must be ALTER TABLE ADD COLUMN compatible (nullable or NOT NULL DEFAULT).
TABLES = {
    "base_history": (
        [("notion_id", "TEXT NOT NULL"), ("seq", "INTEGER NOT NULL"), ("rel_path", "TEXT"), ("sha256", "TEXT"),
         ("page_json_sha", "TEXT"), ("renderer_version", "TEXT"), ("run_id", "TEXT"), ("origin", "TEXT"),
         ("seed", "INTEGER NOT NULL DEFAULT 0")],
        ("notion_id", "seq"), [("bh_sha", "sha256")]),
    "block_lines": (
        [("notion_id", "TEXT NOT NULL"), ("base_sha", "TEXT NOT NULL"), ("seq", "INTEGER NOT NULL"),
         ("renderer_version", "TEXT"), ("block_id", "TEXT"), ("owner_page_id", "TEXT"), ("parent_block_id", "TEXT"),
         ("ord", "INTEGER"), ("depth", "INTEGER"), ("type", "TEXT"), ("own_start", "INTEGER"), ("own_end", "INTEGER"),
         ("line_start", "INTEGER"), ("line_end", "INTEGER"), ("prefix", "TEXT"), ("zero_line", "INTEGER"),
         ("klass", "TEXT"), ("x_reason", "TEXT"), ("json_sha", "TEXT")],
        ("notion_id", "base_sha", "seq"), [("bl_block", "block_id")]),
    "plans": (
        [("plan_id", "TEXT NOT NULL"), ("created_at", "TEXT"), ("expires_at", "TEXT"), ("plan_sha256", "TEXT"),
         ("status", "TEXT"), ("scope", "TEXT"), ("counts_json", "TEXT"), ("status_reason", "TEXT")],
        ("plan_id",), [("plans_status", "status")]),
    "plan_items": (
        [("plan_id", "TEXT NOT NULL"), ("item_id", "TEXT NOT NULL"), ("notion_id", "TEXT"), ("rel_path", "TEXT"),
         ("category", "TEXT"), ("ops_json", "TEXT"), ("risk", "TEXT"), ("decision", "TEXT DEFAULT 'pending'"),
         ("reason", "TEXT"), ("l_sha", "TEXT"), ("r_json_sha", "TEXT"), ("verify_status", "TEXT"), ("binds", "TEXT")],
        ("plan_id", "item_id"), [("pi_notion", "notion_id")]),
    "confirmations": (
        [("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("plan_id", "TEXT NOT NULL"), ("item_id", "TEXT NOT NULL"),
         ("confirmed_at", "TEXT"), ("os_user", "TEXT"), ("tty", "TEXT"), ("challenge_ok", "INTEGER"),
         ("phrase_scope", "TEXT"), ("plan_sha256", "TEXT")],
        None, [("conf_item", "plan_id, item_id")]),
    "journal": (
        [("seq", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("plan_id", "TEXT"), ("item_id", "TEXT"), ("op", "TEXT"),
         ("target_id", "TEXT"), ("state", "TEXT"), ("before_json", "TEXT"), ("request_json", "TEXT"),
         ("expected_after_json", "TEXT"), ("response_status", "INTEGER"), ("committed_ids", "TEXT"),
         ("after_json", "TEXT"), ("ts", "TEXT")],
        None, [("journal_plan", "plan_id, state")]),
    "pending_deletes": (
        [("notion_id", "TEXT"), ("rel_path", "TEXT"), ("subtree_json", "TEXT"), ("detected_run", "TEXT"),
         ("detected_at", "TEXT"), ("status", "TEXT"), ("ttl_until", "TEXT")],
        None, [("pd_notion", "notion_id, status")]),
    "name_overrides": (
        [("notion_id", "TEXT"), ("old_rel", "TEXT"), ("local_rel", "TEXT"), ("notion_title_at_detect", "TEXT"),
         ("since", "TEXT"), ("status", "TEXT")],
        None, [("no_notion", "notion_id, status")]),
    "page_state": (
        [("notion_id", "TEXT NOT NULL"), ("state", "TEXT"), ("reason", "TEXT"), ("since", "TEXT")],
        ("notion_id",), []),
    "creation_state": (
        [("ts", "TEXT"), ("members", "INTEGER"), ("plan", "TEXT"), ("state", "TEXT"), ("source", "TEXT")],
        None, []),
    "block_budget": (
        [("ts", "TEXT"), ("plan_id", "TEXT"), ("item_id", "TEXT"), ("created_blocks", "INTEGER")],
        None, []),
    "trash_archive": (
        [("notion_id", "TEXT"), ("archived_at", "TEXT"), ("reason", "TEXT"), ("last_snapshot", "TEXT"),
         ("object_json", "TEXT"), ("blocks_json", "TEXT"), ("inbound_refs_json", "TEXT")],
        None, [("ta_notion", "notion_id")]),
    "rejections": (
        [("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("notion_id", "TEXT"), ("rel_path", "TEXT"), ("l_sha", "TEXT"),
         ("item_category", "TEXT"), ("reason", "TEXT"), ("created_at", "TEXT"), ("plan_id", "TEXT"),
         ("active", "INTEGER NOT NULL DEFAULT 1"), ("os_user", "TEXT"), ("restore_notion", "INTEGER NOT NULL DEFAULT 0")],
        None, [("rj_active", "active, notion_id")]),
    "wb_meta": (
        [("key", "TEXT NOT NULL"), ("value", "TEXT")],
        ("key",), []),
}

BLOCK_LINE_FIELDS = [c for c, _ in TABLES["block_lines"][0]]

# tables whose primary key changed after v1; an old copy is rebuilt (rows kept) instead of refused
_REBUILD_ON_PK_CHANGE = frozenset({"confirmations"})

# CAS transitions for plans.status (WB §3, §11.0, speccheck R-032). Terminal: applied,
# partially_applied, aborted, expired, superseded.
PLAN_TRANSITIONS = {
    "draft": {"confirmed", "expired", "superseded", "aborted"},
    "confirmed": {"applying", "expired", "superseded", "aborted"},
    "applying": {"applied", "partially_applied", "aborted"},
}


def _create_sql(name):
    cols, pk, _ = TABLES[name]
    parts = [f"{c} {d}" for c, d in cols]
    if pk:
        parts.append(f"PRIMARY KEY({', '.join(pk)})")
    return f"CREATE TABLE IF NOT EXISTS {name}({', '.join(parts)})"


def _uri(path, mode):
    return f"file:{urllib.parse.quote(os.path.abspath(path))}?mode={mode}"


@contextlib.contextmanager
def transaction(db):
    """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error). Inside an open transaction it only joins it."""
    if db.in_transaction:
        yield db
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _rebuild(db, name, have):
    """Recreate `name` with the current definition and copy every row (no column may be lost)."""
    new_cols = {c for c, _ in TABLES[name][0]}
    lost = sorted(set(have) - new_cols)
    if lost:
        raise SchemaError(f"{name}: rebuild would drop columns {lost}; cannot migrate automatically")
    old = f"{name}__pre_v{SCHEMA_VERSION}"
    db.execute(f"DROP TABLE IF EXISTS {old}")
    db.execute(f"ALTER TABLE {name} RENAME TO {old}")
    db.execute(_create_sql(name))
    shared = ", ".join(c for c, _ in TABLES[name][0] if c in have)
    db.execute(f"INSERT INTO {name}({shared}) SELECT {shared} FROM {old} ORDER BY rowid")
    db.execute(f"DROP TABLE {old}")


def _migrate(db):
    with transaction(db):
        cur = db.execute("PRAGMA user_version").fetchone()[0]
        if cur > SCHEMA_VERSION:
            raise SchemaError(f"writeback.db schema version {cur} is newer than this code ({SCHEMA_VERSION})")
        for name, (cols, pk, idx) in TABLES.items():
            db.execute(_create_sql(name))
            have = {r[1]: r for r in db.execute(f"PRAGMA table_info({name})")}
            pk_cols = set(pk or ()) | {c for c, d in cols if "PRIMARY KEY" in d}
            if name in _REBUILD_ON_PK_CHANGE and not pk_cols <= set(have):
                _rebuild(db, name, have)
                have = {r[1]: r for r in db.execute(f"PRAGMA table_info({name})")}
            for c, d in cols:
                if c in have:
                    continue
                if c in pk_cols:
                    raise SchemaError(f"{name}: primary-key column {c} missing; cannot migrate automatically")
                db.execute(f"ALTER TABLE {name} ADD COLUMN {c} {d}")
            for iname, icols in idx:
                db.execute(f"CREATE INDEX IF NOT EXISTS {iname} ON {name}({icols})")
        if cur != SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _db_path(path):
    """None -> DB_PATH; otherwise an absolute file path. ':memory:', '' and 'file:' URIs are refused:
    the base store lives next to the database file, and an empty string must never silently mean
    the default (deployed) database."""
    if path is None:
        return DB_PATH
    s = os.fspath(path)
    if not isinstance(s, str) or s.strip() == "" or s == ":memory:" or s.startswith("file:"):
        raise ValueError(f"writeback.db must be a file path, got {s!r}")
    return os.path.abspath(s)


def connect(path=None) -> sqlite3.Connection:
    """Open (create/migrate) state/writeback.db: WAL, synchronous=FULL, autocommit, sqlite3.Row rows.

    `path` must be a real file path (ValueError for ':memory:', '' or a 'file:' URI)."""
    p = _db_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, timeout=30, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        _migrate(db)
    except BaseException:
        db.close()
        raise
    return db


def connect_readonly(path=None):
    """Read-only connection (no create, no migration) or None if the database does not exist.

    Logically read-only: no row, schema or main-file byte changes. It is NOT free of filesystem side
    effects: writeback.db is in WAL mode, and SQLite needs the '-wal' and '-shm' companion files to
    read it consistently while a writer may be active, so it creates them when absent (empty) and a
    read-only connection leaves them behind; the next read-write connection reuses and removes them.
    (`immutable=1` would avoid that but ignores the WAL and locking, i.e. could miss committed holds
    or read torn pages, so it is deliberately not used.) If the companions are missing and the
    directory is not writable, the open fails here (sqlite3.OperationalError) instead of at the
    first query.
    """
    p = _db_path(path)
    if not os.path.exists(p):
        return None
    db = sqlite3.connect(_uri(p, "ro"), uri=True, timeout=30, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fail now, not at the first query
    except BaseException:
        db.close()
        raise
    return db


def db_file(db):
    """Filesystem path of the main database of `db`, or None (in-memory)."""
    for row in db.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or None
    return None


def base_dir_for(db=None) -> str:
    """`<dir of writeback.db>/base`; db None -> state/base (the default database's store).

    A connection without a database file (in-memory / temporary) raises ValueError instead of
    falling back to state/base: pass base_dir= explicitly for such connections."""
    if db is None:
        return BASE_DIR
    f = db_file(db)
    if not f:
        raise ValueError("database has no file (in-memory or temporary); pass base_dir= explicitly")
    return os.path.join(os.path.dirname(f), "base")


def _check_sha(sha):
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise ValueError(f"not a sha256 hex digest: {sha!r}")


# ---------------------------------------------------------------- content store
def store_content(content: bytes, *, base_dir=None, db=None) -> str:
    """Write `content` to <base>/<sha256> once (tmp -> fsync -> os.replace). Returns the sha.

    An existing file with the right hash is left alone; a corrupt one is replaced."""
    sha = wb_common.sha256_bytes(content)
    d = base_dir or base_dir_for(db)
    p = os.path.join(d, sha)
    if os.path.isfile(p) and not os.path.islink(p):
        try:
            if wb_common.sha256_file(p) == sha:
                return sha
        except OSError:
            pass
    wb_common.atomic_write(p, bytes(content))
    if wb_common.sha256_file(p) != sha:
        raise BaseCorrupt(f"state/base write verification failed for {sha}")
    return sha


def base_content(sha, *, base_dir=None, db=None):
    """Bytes stored under `sha`, None if absent; raises BaseCorrupt if the bytes do not hash to `sha`."""
    _check_sha(sha)
    p = os.path.join(base_dir or base_dir_for(db), sha)
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    if wb_common.sha256_bytes(data) != sha:
        raise BaseCorrupt(f"state/base/{sha} is corrupt (content hash differs)")
    return data


# ---------------------------------------------------------------- base_history
def latest_base(db, notion_id):
    row = db.execute("SELECT * FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1", (notion_id,)).fetchone()
    return dict(row) if row else None


def base_history_for(db, notion_id) -> list:
    """All base_history rows of `notion_id`, oldest first."""
    return [dict(r) for r in db.execute("SELECT * FROM base_history WHERE notion_id=? ORDER BY seq", (notion_id,))]


def append_base(db, *, notion_id, rel_path, sha, page_json_sha, renderer_version, run_id, origin, content: bytes,
                seed=0):
    """Store `content` in state/base and append a base_history row (D-06).

    Appends only if (rel_path, sha, page_json_sha, renderer_version) differs from the latest row of
    `notion_id`. Returns the new seq, or None when nothing was appended. `content` must hash to `sha`.
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, got {origin!r}")
    _check_sha(sha)
    if not notion_id or not rel_path:
        raise ValueError("notion_id and rel_path are required")
    if wb_common.sha256_bytes(content) != sha:
        raise ValueError(f"content does not hash to {sha} ({rel_path})")
    store_content(content, db=db)
    with transaction(db):
        last = latest_base(db, notion_id)
        if last and (last["rel_path"], last["sha256"], last["page_json_sha"], last["renderer_version"]) == \
                (rel_path, sha, page_json_sha, renderer_version):
            return None
        seq = (last["seq"] if last else 0) + 1
        db.execute("INSERT INTO base_history(notion_id, seq, rel_path, sha256, page_json_sha, renderer_version, run_id,"
                   " origin, seed) VALUES(?,?,?,?,?,?,?,?,?)",
                   (notion_id, seq, rel_path, sha, page_json_sha, renderer_version, run_id, origin, int(seed)))
    return seq


# ---------------------------------------------------------------- block_lines
def import_block_lines(db, notion_id, base_sha, rows) -> None:
    """Replace the block_lines of (notion_id, base_sha) with `rows` (idempotent, atomic).

    Rows use the block_lines.jsonl shape; a row's own notion_id/base_sha must match the arguments.
    Missing `seq` on every row -> list order; unknown keys are ignored.
    """
    _check_sha(base_sha)
    rows = list(rows)
    have_seq = [("seq" in r and r["seq"] is not None) for r in rows]
    if any(have_seq) and not all(have_seq):
        raise ValueError("block_lines rows: either every row or no row carries seq")
    vals, seen = [], set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ValueError("block_lines rows must be dicts")
        if r.get("notion_id") not in (None, notion_id):
            raise ValueError(f"block_lines row {i}: notion_id {r.get('notion_id')!r} != {notion_id!r}")
        if r.get("base_sha") not in (None, base_sha):
            raise ValueError(f"block_lines row {i}: base_sha {r.get('base_sha')!r} != {base_sha!r}")
        seq = int(r["seq"]) if have_seq[i] else i
        if seq in seen:
            raise ValueError(f"block_lines: duplicate seq {seq}")
        seen.add(seq)
        row = dict(r, notion_id=notion_id, base_sha=base_sha, seq=seq)
        if isinstance(row.get("zero_line"), bool):
            row["zero_line"] = int(row["zero_line"])
        vals.append(tuple(row.get(f) for f in BLOCK_LINE_FIELDS))
    sql = (f"INSERT INTO block_lines({', '.join(BLOCK_LINE_FIELDS)}) "
           f"VALUES({', '.join('?' * len(BLOCK_LINE_FIELDS))})")
    with transaction(db):
        db.execute("DELETE FROM block_lines WHERE notion_id=? AND base_sha=?", (notion_id, base_sha))
        db.executemany(sql, vals)


def block_lines_for(db, notion_id, base_sha) -> list:
    return [dict(r) for r in db.execute("SELECT * FROM block_lines WHERE notion_id=? AND base_sha=? ORDER BY seq",
                                        (notion_id, base_sha))]


# ---------------------------------------------------------------- plans
def cas_plan_status(db, plan_id, from_status, to_status, reason=None) -> bool:
    """UPDATE plans SET status=to WHERE plan_id=? AND status=from; True iff exactly one row changed."""
    if to_status not in PLAN_TRANSITIONS.get(from_status, ()):
        raise ValueError(f"plan status transition not allowed: {from_status} -> {to_status}")
    with transaction(db):
        cur = db.execute("UPDATE plans SET status=?, status_reason=COALESCE(?, status_reason) WHERE plan_id=? AND status=?",
                         (to_status, reason, plan_id, from_status))
        return cur.rowcount == 1


# ---------------------------------------------------------------- wb_meta
def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM wb_meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def set_meta(db, key, value) -> None:
    """Store `value` as text (non-strings as canonical JSON); None deletes the key."""
    with transaction(db):
        if value is None:
            db.execute("DELETE FROM wb_meta WHERE key=?", (key,))
        else:
            v = value if isinstance(value, str) else wb_common.canon_json(value)
            db.execute("INSERT INTO wb_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, v))


def get_meta_json(db, key, default=None):
    import json
    v = get_meta(db, key)
    return default if v is None else json.loads(v)


# ---------------------------------------------------------------- export.db
def ensure_export_db(export_db_path) -> None:
    """Add published_files.staged_sha256 TEXT to an existing export.db if missing (D-07; idempotent).

    Never creates the database (mode=rw URI). If published_files does not exist yet nothing is
    changed (the publisher creates the table; call this again after it does).

    DEPLOYMENT ORDER (cross-module, cannot be enforced here): the pre-D-07 publisher writes
    published_files with POSITIONAL 6-value INSERTs, which fail ("table published_files has 7
    columns but 6 values were supplied") once this column exists. Only the D-07 publisher, which
    uses named-column INSERTs, may call this, and it must never be run by hand on a live export.db
    whose publisher is older.
    """
    p = os.path.abspath(export_db_path)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    con = sqlite3.connect(_uri(p, "rw"), uri=True, timeout=30, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        with transaction(con):
            cols = [r[1] for r in con.execute("PRAGMA table_info(published_files)")]
            if cols and "staged_sha256" not in cols:
                con.execute("ALTER TABLE published_files ADD COLUMN staged_sha256 TEXT")
    finally:
        con.close()
