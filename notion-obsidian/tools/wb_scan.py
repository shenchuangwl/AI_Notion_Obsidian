#!/usr/bin/env python3
"""Read-only vault scan shared by refresh.py and the write-back planner (IMPL_CONTRACT §5.11, WB §4.0, §6.0, §7.4).

    import wb_scan
    res = wb_scan.scan(vault, export_db, wb_db, cfg)      # JSON-serialisable dict, nothing is written

What it does (and nothing more):
* walks the vault like verify_vault does (names starting with "." are skipped at every level, so
  `.obsidian/`, `.trash/` and `.nmig-tmp-*` never appear);
* identifies every note by the `notion_id` in its frontmatter (wb_common.split_note + parse_frontmatter);
  a note whose frontmatter cannot be parsed is reported in `frontmatter_errors` and is NEVER `local_new`
  (A2 request): its object is held by the caller;
* compares with `published_files` (status 'active') and `objects_map` of export.db (opened read-only,
  `immutable=1` unless a journal/WAL is non-empty) and with the open `name_overrides` /
  held `pending_deletes` of writeback.db (opened only through `wb_state.connect_readonly`).

Result keys (lists are sorted, rel paths are vault-relative POSIX paths as found on disk):
  missing              published files that are gone: [{rel, notion_id, kind, published_sha, pending}]
                       (a note whose notion_id is still present elsewhere in the vault is NOT missing)
  renamed_same_folder  [{notion_id, old_rel, new_rel, kind, content_changed, override}]  (WB 7.4.1)
  moved_other_folder   [{notion_id, old_rel, new_rel, kind, content_changed}]  UNSUPPORTED_RENAME (7.4.6)
  unsupported_rename   [{notion_id, old_rel, new_rel, kind, reason}]  same folder, but a folder note /
                       database / template (cannot be expressed as a name override)
  local_new            user files and user-only folders (no notion_id, not published, outside
                       Notion_Obsidian/), .md and non-.md alike
  local_modified       [{notion_id, rel, kind, published_sha, vault_sha}]  published path, same object, sha differs
  sync_conflict_copies [{rel, notion_id, original_rel, original_notion_id}]  Obsidian Sync "(Conflicted copy …)"
  duplicates           [{notion_id, rels, published_rels}]  one notion_id in more than one note
  generated_changed    [{rel, kind, published_sha, vault_sha}]  tool-owned Notion_Obsidian/* files edited locally
  normalization_mismatch [{rel, published_rel, notion_id, kind}]  a vault name that equals a published path only
                       after Unicode NFC normalisation (e.g. NFD from another OS). The publisher works on exact
                       paths and would see the published file as missing, so the object is held with a clear reason
  undecodable_names    [{rel, escaped, is_dir}] names that are not valid UTF-8 (e.g. extracted from a GBK zip):
                       `rel` is the surrogate-escaped str os.walk returned (usable as a path), `escaped` a printable
                       form; they appear in NO other list (never local_new: the write-back planner must not turn
                       them into Notion pages), refresh lists them for the verifier and names them in the run log
  unreadable           [{rel, error, published_notion_id}] files that exist but cannot be read (PermissionError,
                       ...): never classified; a published note's object is held (note only, anomaly_ids)
  moved_database_base  [rel] `<dir>/<X>.base` next to an identified database note `<dir>/<X>.md` at an unpublished
                       path (the database's folder was renamed/moved locally): the verifier knows such a file as the
                       database's base file, so it is not local_new (the database object is held anyway)
A file that vanishes between the listing and the read (Obsidian Sync renaming / deleting while the scan runs) makes
the scan start again once; if the vault is still changing, ScanUnsettled (a wb_common.WbError, WB 4.2
SYNC_SETTLING) is raised instead of a classification of a half-written vault.
plus diagnostics: frontmatter_errors, path_id_mismatch, unpublished_ids, renamed_attachments,
misplaced_generated, unregistered_exporter_files, symlinks, tool_files_unregistered (count),
overrides_open, pending_deletes (status hold), pending_expired (status expired: restored from Notion, never a new
local deletion), nid_index {notion_id: [rel]}, counts. A `missing` entry is `pending` when its id is a held
pending_deletes row or inside such a row's subtree, and `expired` likewise for expired rows.

Everything under `Notion_Obsidian/` is a tool file: compared with published_files when registered,
otherwise ignored; never local_new (L-07). The scan never writes: no file, database row or directory
is created (writeback.db is opened through connect_readonly, which may leave empty -wal/-shm
companions; see wb_state.connect_readonly).
"""
import json
import os
import posixpath
import sqlite3
import sys
import unicodedata
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402
import wb_state  # noqa: E402

SCAN_VERSION = 1
TASK_DIR = "Notion_Obsidian"
TEAMSPACES = ("General", "Quantitative Investing", "x_to_notion", "Godcreate Business", "Godcreate AI工作空间")
NOTE_KINDS = ("page", "row", "database", "template")
ATT = "_attachments"


def nfc(s):
    return unicodedata.normalize("NFC", s)


def _is_task_path(n):
    return n == TASK_DIR or n.startswith(TASK_DIR + "/")


def _is_attachment_dir_file(n):
    parts = n.split("/")
    return len(parts) == 3 and parts[0] in TEAMSPACES and parts[1] == ATT


def _is_exporter_attachment(n):
    """`<teamspace>/_attachments/<name>__<sha8>.<ext>` (verify_vault's exporter-attachment rule)."""
    parts = n.split("/")
    if len(parts) != 3 or parts[0] not in TEAMSPACES or parts[1] != ATT:
        return False
    stem = parts[2].rsplit(".", 1)[0]
    if "__" not in stem:
        return False
    tail = stem.rsplit("__", 1)[1]
    return len(tail) == 8 and all(c in "0123456789abcdef" for c in tail)


def _is_folder_note(rel):
    d = posixpath.dirname(rel)
    return bool(d) and posixpath.basename(rel)[:-3] == posixpath.basename(d) if rel.endswith(".md") else False


def open_export_db(path):
    """Read-only connection to export.db (immutable when no journal/WAL holds data), or None if absent."""
    if not path:
        return None
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        return None
    busy = any(os.path.isfile(p + sfx) and os.path.getsize(p + sfx) > 0 for sfx in ("-wal", "-journal"))
    mode = "mode=ro" if busy else "mode=ro&immutable=1"
    con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?{mode}", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_export_state(export_db):
    """(published_rows, objects) from export.db; both empty if the file or a table is absent.

    published_rows: [{rel, sha, notion_id, kind, run_id, status, staged_sha}] (all statuses)
    objects: {notion_id: {kind, rel_path, parent_id, title, status, name}}"""
    con = export_db if isinstance(export_db, sqlite3.Connection) else open_export_db(export_db)
    if con is None:
        return [], {}
    try:
        t = _tables(con)
        pub, objs = [], {}
        if "published_files" in t:
            cols = {r[1] for r in con.execute("PRAGMA table_info(published_files)")}
            st = "staged_sha256" if "staged_sha256" in cols else "NULL"
            for r in con.execute(f"SELECT rel_path, sha256, notion_id, kind, run_id, status, {st} "
                                 "FROM published_files"):
                pub.append({"rel": r[0], "sha": r[1], "notion_id": r[2], "kind": r[3], "run_id": r[4],
                            "status": r[5], "staged_sha": r[6]})
        if "objects_map" in t:
            for r in con.execute("SELECT notion_id, kind, rel_path, parent_id, title, status, name FROM objects_map"):
                objs[r[0]] = {"kind": r[1], "rel_path": r[2], "parent_id": r[3], "title": r[4], "status": r[5],
                              "name": r[6]}
        return pub, objs
    finally:
        if not isinstance(export_db, sqlite3.Connection):
            con.close()


def _subtree_ids(subtree_json):
    """notion_ids listed in a pending_deletes.subtree_json ({"ids": [{"notion_id", "rel"}], ...}); [] if unreadable."""
    try:
        d = json.loads(subtree_json) if subtree_json else {}
    except ValueError:
        return []
    out = []
    for e in (d.get("ids") or []) if isinstance(d, dict) else []:
        nid = e.get("notion_id") if isinstance(e, dict) else e
        if isinstance(nid, str) and nid:
            out.append(nid)
    return out


def load_wb_rows(wb_db) -> dict:
    """writeback.db rows the scan needs (read-only): {"overrides": open name_overrides, "pending": held
    pending_deletes, "expired": expired pending_deletes (each with `subtree_ids`), "page_state": BLOCKED /
    PARTIAL_CREATE rows}. `wb_db`: a path (wb_state.connect_readonly; absent file -> empty), a connection, or None."""
    out = {"overrides": [], "pending": [], "expired": [], "page_state": []}
    if wb_db is None:
        return out
    own = not isinstance(wb_db, sqlite3.Connection)
    con = wb_state.connect_readonly(wb_db) if own else wb_db
    if con is None:
        return out
    try:
        t = _tables(con)
        if "name_overrides" in t:
            out["overrides"] = [dict(zip(("notion_id", "old_rel", "local_rel", "since"), r)) for r in con.execute(
                "SELECT notion_id, old_rel, local_rel, since FROM name_overrides WHERE status='open' ORDER BY rowid")]
        if "pending_deletes" in t:
            for status, key in (("hold", "pending"), ("expired", "expired")):
                for r in con.execute("SELECT notion_id, rel_path, detected_run, ttl_until, subtree_json FROM "
                                     "pending_deletes WHERE status=? ORDER BY rowid", (status,)):
                    e = dict(zip(("notion_id", "rel_path", "detected_run", "ttl_until"), r[:4]))
                    e["subtree_ids"] = [i for i in _subtree_ids(r[4]) if i != r[0]]
                    out[key].append(e)
        if "page_state" in t:
            out["page_state"] = [dict(zip(("notion_id", "state", "reason"), r)) for r in con.execute(
                "SELECT notion_id, state, reason FROM page_state WHERE state IN ('BLOCKED','PARTIAL_CREATE')")]
        return out
    finally:
        if own:
            con.close()


def load_wb_state(wb_db):
    """(open name_overrides, held pending_deletes, page_state rows) from writeback.db (read-only).

    `wb_db`: a path (opened with wb_state.connect_readonly; absent file -> empty), an open connection, or None."""
    d = load_wb_rows(wb_db)
    return d["overrides"], d["pending"], d["page_state"]


def walk_vault(root):
    """(files, dirs): vault-relative POSIX paths, sorted, hidden names skipped (same walk as verify_vault)."""
    files, dirs = [], []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        rel_dp = os.path.relpath(dp, root)
        rel_dp = "" if rel_dp == "." else rel_dp.replace(os.sep, "/")
        for d in dns:
            dirs.append(posixpath.join(rel_dp, d) if rel_dp else d)
        for f in sorted(fns):
            if not f.startswith("."):
                files.append(posixpath.join(rel_dp, f) if rel_dp else f)
    return files, dirs


def read_note_identity(data: bytes):
    """(notion_id | None, notion_kind | None, error | None) of a note's bytes.

    error is set for: not UTF-8, unterminated or invalid YAML frontmatter, frontmatter not a mapping,
    a notion_id that is not a Notion id. The caller must hold/refuse such a note (never local_new)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "not_utf8"
    if text.startswith("﻿"):
        text = text[1:]
    try:
        fm, _body, _first = wb_common.split_note(text)
        meta = wb_common.parse_frontmatter(fm)
    except wb_common.FrontmatterError as exc:
        return None, None, f"frontmatter: {str(exc)[:200]}"
    kind = meta.get("notion_kind")
    kind = kind if isinstance(kind, str) else None
    raw = meta.get("notion_id")
    if raw in (None, ""):
        return None, kind, None
    try:
        return wb_common.norm_notion_id(str(raw)), kind, None
    except ValueError:
        return None, kind, "frontmatter: notion_id is not a Notion id"


def _sync_original(rel):
    """'A/X (Conflicted copy 2026-…).md' -> 'A/X.md'."""
    m = wb_common.SYNC_CONFLICT_RE.search(rel)
    return rel[:m.start()] + ".md" if m else None


class ScanUnsettled(wb_common.WbError):
    """Files kept vanishing between the vault listing and their read in two consecutive passes (WB 4.2
    SYNC_SETTLING: Obsidian Sync is still writing). Fail closed; the next round / plan tries again."""


class _Vanished(Exception):
    pass


def undecodable(rel):
    """True when `rel` (a str from os.walk) holds surrogate escapes, i.e. the on-disk name is not valid UTF-8."""
    return any("\udc80" <= ch <= "\udcff" for ch in rel)


def escaped_name(rel):
    """Printable, valid-UTF-8 form of a surrogate-escaped name ('\\xc4\\xe3' for the undecodable bytes)."""
    return rel.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _read_bytes(full):
    """File bytes; _Vanished when the file disappeared (or became a directory) since the listing."""
    try:
        with open(full, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
        raise _Vanished(str(exc)) from exc


def scan(vault, export_db, wb_db, cfg=None) -> dict:
    """Pure read-only scan of `vault` (see the module docstring). `cfg` is the loaded wb_config dict (only
    its `mode` is echoed; classification is mode-independent, deletion handling is the caller's job).
    A file vanishing mid-scan (Sync) restarts the scan once; ScanUnsettled when it happens again."""
    vanished = []
    for attempt in range(2):
        try:
            res = _scan_once(vault, export_db, wb_db, cfg)
        except _Vanished as exc:
            vanished.append(str(exc)[:300])
            continue
        if vanished:
            res["rescanned_after_vanished"] = vanished
        return res
    raise ScanUnsettled("库内文件在扫描期间持续变化（可能是 Obsidian Sync 正在同步，SYNC_SETTLING），"
                        "本次扫描放弃，请稍后再试 / the vault kept changing while it was scanned: "
                        + "; ".join(vanished)[:600])


def _scan_once(vault, export_db, wb_db, cfg=None) -> dict:
    root = os.path.abspath(vault)
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError(f"vault root missing or a symlink: {root}")
    pub_rows, objects = load_export_state(export_db)
    wbr = load_wb_rows(wb_db)
    overrides_open, pending, expired, page_state = wbr["overrides"], wbr["pending"], wbr["expired"], wbr["page_state"]
    pending_ids = {i for p in pending for i in [p["notion_id"]] + p["subtree_ids"]}
    expired_ids = {i for p in expired for i in [p["notion_id"]] + p["subtree_ids"]} - pending_ids
    active = {}
    for r in pub_rows:
        if (r["status"] or "active") == "active" and r["rel"]:
            active[nfc(r["rel"])] = r
    pub_note_rels = {}  # nid -> [nfc rel] (.md notes of the object)
    for n, r in active.items():
        if r["notion_id"] and n.endswith(".md") and not _is_task_path(n) and (r["kind"] in NOTE_KINDS or not r["kind"]):
            pub_note_rels.setdefault(r["notion_id"], []).append(n)
    files, dirs = walk_vault(root)
    bad_names = [{"rel": d, "escaped": escaped_name(d), "is_dir": True} for d in dirs if undecodable(d)] + \
                [{"rel": f, "escaped": escaped_name(f), "is_dir": False} for f in files if undecodable(f)]
    if bad_names:  # never classified: no other list may carry a name that is not valid UTF-8
        files = [f for f in files if not undecodable(f)]
        dirs = [d for d in dirs if not undecodable(d)]
    vault_nfc = {nfc(f): f for f in files}

    nid_index, note_sha = {}, {}
    local_new, local_modified, sync_copies, generated_changed = [], [], [], []
    fm_errors, path_mismatch, misplaced_generated, exporter_unreg, symlinks = [], [], [], [], []
    norm_mismatch, unreadable = [], []
    note_kind = {}  # nfc rel -> notion_kind of identified notes (moved database .base detection)
    tool_unregistered = []
    other_files = {}  # nfc rel -> sha for non-published, non-note files (renamed-attachment detection)
    for rel in files:
        n = nfc(rel)
        full = os.path.join(root, *rel.split("/"))
        pubrow = active.get(n)
        if pubrow is not None and pubrow["rel"] != rel:
            # equal only after NFC: publish_vault works on exact paths and would not find the published file
            norm_mismatch.append({"rel": rel, "published_rel": pubrow["rel"], "notion_id": pubrow["notion_id"],
                                  "kind": pubrow["kind"]})
        if os.path.islink(full):
            symlinks.append(rel)
            if pubrow is not None:
                path_mismatch.append({"rel": rel, "published_notion_id": pubrow["notion_id"], "found_notion_id": None,
                                      "reason": "not_a_regular_file"})
            elif not _is_task_path(n):
                local_new.append(rel)
            continue
        if _is_task_path(n):
            if pubrow is None:
                tool_unregistered.append(rel)
                continue
            try:
                sha = wb_common.sha256_bytes(_read_bytes(full))
            except OSError as exc:
                unreadable.append({"rel": rel, "error": f"{type(exc).__name__}: {exc.strerror or exc}",
                                   "published_notion_id": pubrow["notion_id"]})
                continue
            if sha != pubrow["sha"]:
                generated_changed.append({"rel": rel, "kind": pubrow["kind"], "published_sha": pubrow["sha"],
                                          "vault_sha": sha})
            continue
        try:
            data = _read_bytes(full)
        except _Vanished:
            raise
        except OSError as exc:  # exists but cannot be read (mode 000, ...): never classified, reported
            unreadable.append({"rel": rel, "error": f"{type(exc).__name__}: {exc.strerror or exc}",
                               "published_notion_id": pubrow["notion_id"] if pubrow else None})
            continue
        sha = wb_common.sha256_bytes(data)
        # notes = .md outside `<teamspace>/_attachments/` (verify_vault exempts .md attachments from note
        # checks) that are not registered as a non-note file (attachment, .base, generated)
        is_md = rel.endswith(".md") and not _is_attachment_dir_file(n) and \
            (pubrow is None or pubrow["kind"] in NOTE_KINDS or not pubrow["kind"])
        nid = kind = err = None
        if is_md:
            nid, kind, err = read_note_identity(data)
        if is_md and wb_common.SYNC_CONFLICT_RE.search(posixpath.basename(rel)):
            orig = _sync_original(rel)
            orow = active.get(nfc(orig))
            sync_copies.append({"rel": rel, "notion_id": nid, "original_rel": orig,
                                "original_notion_id": (orow or {}).get("notion_id") or nid,
                                "frontmatter_error": err})
            continue
        if err is not None:
            fm_errors.append({"rel": rel, "error": err,
                              "published_notion_id": pubrow["notion_id"] if pubrow else None,
                              "possible_ids": []})
            continue
        if is_md and nid is not None:
            nid_index.setdefault(nid, []).append(rel)
            note_sha[rel] = sha
            note_kind[n] = kind
        if pubrow is not None:
            pk = pubrow["kind"]
            if is_md and (pk in NOTE_KINDS or pubrow["notion_id"]):
                if nid != pubrow["notion_id"]:
                    path_mismatch.append({"rel": rel, "published_notion_id": pubrow["notion_id"],
                                          "found_notion_id": nid,
                                          "reason": "notion_id_removed" if nid is None else "other_notion_id"})
                    continue
            if sha != pubrow["sha"]:
                local_modified.append({"notion_id": pubrow["notion_id"], "rel": rel, "kind": pk,
                                       "published_sha": pubrow["sha"], "vault_sha": sha})
            continue
        # not a published path
        if is_md and nid is not None:
            continue  # identity handled below (rename / move / duplicate / unpublished)
        if is_md and kind == "generated":
            misplaced_generated.append(rel)
            continue
        if _is_exporter_attachment(n):
            exporter_unreg.append(rel)
            continue
        local_new.append(rel)
        if not is_md:
            other_files[n] = sha

    renamed, moved, unsupported, duplicates, unpublished = [], [], [], [], []
    ovr_by_id = {}
    for o in overrides_open:
        ovr_by_id.setdefault(o["notion_id"], o)
    err_paths = {nfc(e["rel"]) for e in fm_errors}
    claimed = set()  # published note rels accounted for by a rename/move/duplicate
    for nid in sorted(set(nid_index) | set(pub_note_rels)):
        found = sorted(nid_index.get(nid, []))
        prels = sorted(pub_note_rels.get(nid, []))
        if len(found) > 1:
            duplicates.append({"notion_id": nid, "rels": found, "published_rels": prels})
            claimed.update(prels)
            continue
        if not found:
            continue
        f = found[0]
        fn = nfc(f)
        if fn in prels:
            continue
        if not prels:
            unpublished.append({"notion_id": nid, "rel": f, "known_object": nid in objects})
            continue
        old = prels[0]
        claimed.update(prels)
        kind = active[old]["kind"]
        changed = note_sha.get(f) != active[old]["sha"]
        if old in vault_nfc:  # the published path is occupied by another (or a broken) note
            path_mismatch.append({"rel": f, "published_notion_id": nid, "found_notion_id": nid,
                                  "reason": "relocated_while_published_path_occupied",
                                  "published_rel": active[old]["rel"],
                                  "occupant": "frontmatter_error" if old in err_paths else "other_note"})
            continue
        entry = {"notion_id": nid, "old_rel": active[old]["rel"], "new_rel": f, "kind": kind,
                 "content_changed": changed}
        if posixpath.dirname(old) != posixpath.dirname(fn):
            moved.append(entry)
        elif kind not in ("page", "row") or _is_folder_note(old) or _is_folder_note(fn):
            unsupported.append(dict(entry, reason="folder_note" if (_is_folder_note(old) or _is_folder_note(fn))
                                    else f"kind_{kind}"))
        else:
            o = ovr_by_id.get(nid)
            renamed.append(dict(entry, override="existing" if o and nfc(o["local_rel"]) == fn else "new"))
    missing, renamed_att = [], []
    for n, r in sorted(active.items()):
        if n in vault_nfc:
            continue
        if n.startswith(".") or "/." in n:
            continue  # hidden (e.g. .obsidian plugin config): not scanned, never missing
        nid = r["notion_id"]
        if n.endswith(".md") and nid and (nid in nid_index or n in claimed):
            continue  # the object's note lives elsewhere (rename / move / duplicate)
        missing.append({"rel": r["rel"], "notion_id": nid, "kind": r["kind"], "published_sha": r["sha"],
                        "pending": bool(nid and nid in pending_ids), "expired": bool(nid and nid in expired_ids)})
        if not n.endswith(".md"):
            d = posixpath.dirname(n)
            cands = sorted(o for o, s in other_files.items() if s == r["sha"] and posixpath.dirname(o) == d)
            if cands:
                renamed_att.append({"old_rel": r["rel"], "new_rel": vault_nfc[cands[0]], "kind": r["kind"]})
    missing_note_ids_by_dir = {}
    for m in missing:
        if m["notion_id"] and m["rel"].endswith(".md"):
            missing_note_ids_by_dir.setdefault(posixpath.dirname(nfc(m["rel"])), []).append(m["notion_id"])
    for e in fm_errors:
        if e["published_notion_id"] is None:
            e["possible_ids"] = sorted(set(missing_note_ids_by_dir.get(posixpath.dirname(nfc(e["rel"])), [])))

    # a database's .base next to its identified note at an unpublished path (folder renamed / moved locally) is the
    # database's file (verify_vault: database_base_file), never a user file
    moved_base = [x for x in local_new if x.endswith(".base") and note_kind.get(nfc(x[:-5] + ".md")) == "database"]
    if moved_base:
        local_new = [x for x in local_new if x not in set(moved_base)]
    # user-only folders: no published path below them, no identified/broken/tool file below them
    exported_dirs = set()
    for n in active:
        parts = n.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            exported_dirs.add("/".join(parts[:i]))
    nonuser_dirs = set(exported_dirs)
    user_files = {nfc(x) for x in local_new}
    for f in files:
        n = nfc(f)
        if n in user_files:
            continue
        parts = n.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            nonuser_dirs.add("/".join(parts[:i]))
    folders = []
    for d in dirs:
        n = nfc(d)
        if _is_task_path(n) or n in TEAMSPACES or n in nonuser_dirs:
            continue
        if os.path.islink(os.path.join(root, *d.split("/"))):
            continue
        folders.append(d)

    res = {
        "schema": SCAN_VERSION,
        "vault": root,
        "scanned_at": wb_common.now_iso(),
        "mode": (cfg or {}).get("mode"),
        "missing": missing,
        "missing_tool_files": sorted(m["rel"] for m in missing if _is_task_path(nfc(m["rel"]))),
        "tool_files_unregistered_rels": sorted(tool_unregistered),
        "renamed_same_folder": sorted(renamed, key=lambda x: x["new_rel"]),
        "moved_other_folder": sorted(moved, key=lambda x: x["new_rel"]),
        "unsupported_rename": sorted(unsupported, key=lambda x: x["new_rel"]),
        "local_new": sorted(local_new + folders),
        "local_new_folders": sorted(folders),
        "local_modified": sorted(local_modified, key=lambda x: x["rel"]),
        "sync_conflict_copies": sorted(sync_copies, key=lambda x: x["rel"]),
        "duplicates": duplicates,
        "generated_changed": sorted(generated_changed, key=lambda x: x["rel"]),
        "normalization_mismatch": sorted(norm_mismatch, key=lambda x: x["rel"]),
        "frontmatter_errors": sorted(fm_errors, key=lambda x: x["rel"]),
        "path_id_mismatch": sorted(path_mismatch, key=lambda x: x["rel"]),
        "unpublished_ids": sorted(unpublished, key=lambda x: x["rel"]),
        "renamed_attachments": renamed_att,
        "misplaced_generated": sorted(misplaced_generated),
        "unregistered_exporter_files": sorted(exporter_unreg),
        "symlinks": sorted(symlinks),
        "undecodable_names": bad_names,
        "unreadable": sorted(unreadable, key=lambda x: x["rel"]),
        "moved_database_base": sorted(moved_base),
        "tool_files_unregistered": len(tool_unregistered),
        "overrides_open": overrides_open,
        "pending_deletes": pending,
        "pending_expired": expired,
        "page_state": page_state,
        "nid_index": {k: sorted(v) for k, v in sorted(nid_index.items())},
    }
    res["counts"] = {k: len(v) for k, v in res.items() if isinstance(v, list)}
    res["counts"].update({"files": len(files), "dirs": len(dirs), "notes_identified": len(nid_index),
                          "published_active": len(active), "tool_files_unregistered": len(tool_unregistered),
                          "missing_notes_new": sum(1 for m in missing if m["notion_id"] and m["rel"].endswith(".md")
                                                   and m["kind"] in ("page", "row") and not m["pending"]
                                                   and not m["expired"])})
    return res


# anomalies that concern one note only (WB 4.0: "only the corresponding note is neither written back nor
# overwritten this round"): its object is held WITHOUT its subtree; every other anomaly (duplicate id,
# UNSUPPORTED_RENAME incl. folder renames, path/id mismatch, ...) holds the subtree too (WB 7.4.6, 14.3)
NOTE_ONLY_REASONS = ("sync_conflict_copy", "frontmatter_error", "frontmatter_error_possible_owner",
                     "path_normalization_mismatch", "unreadable_file")


def anomaly_ids(res) -> dict:
    """{notion_id: {"reason", "paths", "subtree"}} of every object the scan says must not be auto-published this
    round (WB 14.3 mapping anomalies, WB 4.0 Sync conflict originals, unparseable frontmatter of a published note,
    NFC/NFD path mismatches). `paths` lists only vault paths verify_vault accepts for a held object: its own
    note(s) or absent old names. `subtree` is False when every reason is a NOTE_ONLY_REASONS one."""
    out = {}

    def add(nid, reason, paths=()):
        if not nid:
            return
        e = out.setdefault(nid, {"reason": reason, "paths": []})
        if reason not in e["reason"].split("+"):
            e["reason"] += "+" + reason
        for p in paths:
            if p and p not in e["paths"]:
                e["paths"].append(p)

    for d in res.get("duplicates", []):
        add(d["notion_id"], "mapping_duplicate", d["rels"])
    for m in res.get("moved_other_folder", []):
        add(m["notion_id"], "unsupported_rename", [m["old_rel"], m["new_rel"]])
    for m in res.get("unsupported_rename", []):
        add(m["notion_id"], "unsupported_rename", [m["old_rel"], m["new_rel"]])
    for m in res.get("path_id_mismatch", []):
        add(m.get("published_notion_id"), "mapping_path_id_mismatch")
        fid = m.get("found_notion_id")
        if fid:
            add(fid, "mapping_path_id_mismatch", [m["rel"]])
    for e in res.get("frontmatter_errors", []):
        add(e.get("published_notion_id"), "frontmatter_error")
        for pid in e.get("possible_ids") or []:
            add(pid, "frontmatter_error_possible_owner")
    for s in res.get("sync_conflict_copies", []):
        add(s.get("original_notion_id"), "sync_conflict_copy",
            [s["original_rel"]] if s.get("original_notion_id") and s.get("original_rel") else [])
    for m in res.get("normalization_mismatch", []):
        add(m.get("notion_id"), "path_normalization_mismatch", [m["rel"], m["published_rel"]])
    for u in res.get("unreadable", []):  # the publisher must not read / replace a file it cannot read
        add(u.get("published_notion_id"), "unreadable_file", [u["rel"]])
    nid_index = res.get("nid_index", {})
    for nid, e in out.items():  # keep only paths that are the object's own notes or absent
        own = set(nid_index.get(nid, []))
        e["paths"] = [p for p in e["paths"] if p in own or not os.path.lexists(os.path.join(res["vault"], *p.split("/")))]
        e["subtree"] = any(r not in NOTE_ONLY_REASONS for r in e["reason"].split("+"))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="read-only vault scan (prints JSON; writes nothing)")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--export-db", default=os.path.join(wb_common.STATE, "export.db"))
    ap.add_argument("--wb-db", default=None, help="writeback.db (opened read-only; default: none)")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    a = ap.parse_args(argv)
    import wb_config
    res = scan(a.vault, a.export_db, a.wb_db, wb_config.load())
    out = res["counts"] if a.summary else res
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
