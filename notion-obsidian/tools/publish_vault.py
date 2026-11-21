#!/usr/bin/env python3
"""Publish a verified staging run into the formal vault (SPEC.md §11, WRITEBACK_SPEC §5.3/§6.6/§7.4/§7.5/§11.4/§14,
IMPL_CONTRACT §5.10).

Never deletes. Never overwrites a file that was edited locally since the last publish unless the difference is only
wikilink spelling (LINK_REWRITE_ONLY) or extra local frontmatter keys (LOCAL_KEYS_ONLY, the keys are kept), or the
file is tool-owned (GENERATED_OVERWRITE). Every overwrite / move is preceded by a backup; every write is
tmp -> fsync -> atomic swap (renameat2 RENAME_EXCHANGE; the displaced bytes are checked against the planned `before`,
a local save that landed after the checks is put back and the run stops with SKIP_*_RACE), an ADD never clobbers a
file that appeared meanwhile, and everything is journaled with before/after sha256. A MOVE removes the source folder
when it is left empty (never a file).

published_files keeps two hashes per file (D-07): sha256 = the vault bytes this tool last wrote or confirmed,
staged_sha256 = the Notion rendering (staged bytes) last published there. Classification of one staged file,
first match wins (D-08); L = vault sha, S = staged sha, B = published row:
  holds / override not honoured / same notion_id elsewhere ...... SKIP_HELD (reason says which)
  --adopt-writeback item ......................................... ADOPT(+REPLACE) / ADOPT_WRITEBACK / UNCHANGED, or a
                                                                   conflict copy + adopt_needs_manual.json
  Notion moved the object, old file unchanged .................... MOVE  (old file edited: CONFLICT_MOVED_EDITED)
  file missing ................................................... ADD   (restore=true when it was published)
  not a regular file ............................................. CONFLICT_NOT_FILE
  L == S ......................................................... UNCHANGED
  not published here: same notion_id in frontmatter, same body ... ADOPT (+REPLACE / LOCAL_KEYS_ONLY, origin WRITEBACK)
                      tool-owned Notion_Obsidian/* ............... GENERATED_OVERWRITE
                      anything else .............................. CONFLICT_UNTRACKED
  L == B.sha: B.staged == S ...................................... UNCHANGED (vault holds our merged/adopted bytes)
              otherwise .......................................... REPLACE (LOCAL_KEYS_ONLY when local keys must be kept)
  tool-owned Notion_Obsidian/* ................................... GENERATED_OVERWRITE (backup first)
  .base: semantically equal to S ................................. UNCHANGED_SEMANTIC
  Notion unchanged (B.staged == S) ............................... LOCAL_ONLY (no write, no conflict copy)
  equal to B modulo wikilink spelling / local-only keys .......... LINK_REWRITE_ONLY / LOCAL_KEYS_ONLY (backup first),
                                                                   unless a link the user's version resolves would no
                                                                   longer resolve to the same object after this plan
                                                                   (target held / skipped / moved locally): CONFLICT_BOTH
  otherwise ...................................................... CONFLICT_BOTH (copy written once per (id, S))
A local-only key that the exporter starts to emit (e.g. a user `aliases` once Notion renames the page) cannot be kept
next to the exporter's value: CONFLICT_BOTH, never a silent overwrite.
Name overrides ({notion_id: local_rel}, the same file the exporter got): an override the exporter applied is adopted
once (ADOPT_RENAME: published_files old_rel -> 'renamed', local_rel active, the user's file untouched); an override it
rejected or did not honour skips the whole object (never ADD at old_rel). Object-based tombstones exclude objects that
were renamed, moved or trashed via write-back (TRASHED_VIA_WRITEBACK marks those rows).

Conflict copies (L-07): Notion_Obsidian/conflicts/<run>/<folder>/<stem> (Notion 版本 <run>)<ext>; notes get
generated frontmatter (no notion_id / notion_url / aliases), other files are copied unchanged.

After apply, base_history (+ state/base content) and block_lines (staging/<run>/block_lines.jsonl) are appended in
ONE writeback.db transaction for every (notion_id, staged sha) published or confirmed (D-06, D-09); page_json_sha and
renderer_version come from staging/<run>/page_json.jsonl. LOCAL_ONLY notes (Notion unchanged, so S is the version last
published) are seeded / re-pathed too, and ADOPT_RENAME appends the latest base under the new path (WB §3). Without
page_json.jsonl or block_lines.jsonl no base row is appended (base row => its block_lines).

Crash safety: every vault write is preceded by a committed publish_intents row (export.db) that is deleted in the same
commit that records the write. A later run (plan: in memory; apply: persisted first) records an intent whose file holds
exactly the intended bytes, so an interrupted run never turns into a false conflict or a second active row.
Locking: refresh.py holds state/refresh.lock around its publish calls; a manual apply should pass
--take-refresh-lock (refused with exit 2 while refresh or another holder runs).

Usage:
  python3 -I publish_vault.py --run-id <run_id> --vault /data120/Obsidian/Godcreate            (plan only)
  python3 -I publish_vault.py --run-id <run_id> --vault ... --apply --approval <approval.md>
  python3 -I publish_vault.py --run-id <run_id> --vault ... --apply --approval <a.md> --plan-file <publish_plan.json> --plan-sha <sha>
  extra inputs: --holds <json> --overrides <json> --only-ids <file> --adopt-writeback <adopt.json> --wb-db <path>
Exit codes: 0 ok, 2 refused (bad input, plan file / sha / state mismatch), 3 post-check mismatch, 4 race stop
(SKIP_*_RACE: the remaining actions were not executed), 5 base_history / block_lines import failed (files published).
"""
import argparse
import difflib
import contextlib
import datetime
import errno
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import unicodedata
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:  # python3 -I (safe_path) does not add the script directory
    sys.path.insert(0, HERE)

import wb_common  # noqa: E402
import wb_locks  # noqa: E402
import wb_state  # noqa: E402

WORK = os.path.dirname(HERE)
EXPECTED_VAULT = "/data120/Obsidian/Godcreate"
TASK_DIR = "Notion_Obsidian"
PUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT,
  run_id TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS pf_obj ON published_files(notion_id, kind);
CREATE TABLE IF NOT EXISTS conflict_copies(obj_key TEXT NOT NULL, kind TEXT NOT NULL, staged_sha TEXT NOT NULL,
  conflict_rel TEXT, conflict_sha TEXT, vault_rel TEXT, vault_sha TEXT, op TEXT, run_id TEXT, created_at TEXT,
  PRIMARY KEY(obj_key, kind, staged_sha));
CREATE TABLE IF NOT EXISTS publish_intents(rel_path TEXT PRIMARY KEY, op TEXT, frm TEXT, before TEXT, expect TEXT,
  staged TEXT, notion_id TEXT, kind TEXT, obj_key TEXT, run_id TEXT, created_at TEXT);
"""
PF_COLS = ("rel_path", "sha256", "notion_id", "kind", "run_id", "status", "staged_sha256")
INTENT_COLS = ("rel_path", "op", "frm", "before", "expect", "staged", "notion_id", "kind", "obj_key", "run_id")

NOTE_KINDS = ("page", "row", "template", "database")       # .md notes with exporter frontmatter
OBJ_MAP_KINDS = ("page", "row", "database")                 # objects_map.published_* bookkeeping (unchanged)
REPLACE_LIKE = ("REPLACE", "LINK_REWRITE_ONLY", "LOCAL_KEYS_ONLY", "GENERATED_OVERWRITE", "ADOPT_WRITEBACK")
CONFLICT_OPS = ("CONFLICT_BOTH", "CONFLICT_MOVED_EDITED", "CONFLICT_NOT_FILE", "CONFLICT_UNTRACKED")
WRITE_OPS = ("ADD", "MOVE") + REPLACE_LIKE                  # write staged (or merged) bytes at rel
NOOP_OPS = ("UNCHANGED", "UNCHANGED_SEMANTIC")
STANDING_OPS = ("LOCAL_ONLY", "SKIP_HELD")  # L-12: re-reported every round while pending; not news by themselves
BASE_OPS = ("UNCHANGED", "ADD", "MOVE", "ADOPT", "ADOPT_RENAME", "LOCAL_ONLY") + REPLACE_LIKE  # base candidates (on success)
RUNLOG_LIST_MAX = 300
NOTION_URL = "https://app.notion.com/p/{}"   # same form as the write-back preview (wb_plan.NOTION_URL)
SKIP_DIRS = (".obsidian", ".trash", ".git")


class Refused(Exception):
    """Bad input or a plan that may not be applied (exit 2)."""


# ---------------------------------------------------------------- small helpers
def sha_file(p):
    return wb_common.sha256_file(p)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def safe_join(root, rel):
    """vault root + rel; refuses escapes and symlinked directories (SystemExit, as before)."""
    try:
        return wb_common.safe_join(root, rel)
    except wb_common.UnsafePath as exc:
        raise SystemExit(f"ABORT {exc}")


def atomic_write(dst, src_bytes):
    wb_common.atomic_write(dst, bytes(src_bytes))


# ---------------------------------------------------------------- race-free final step (r1-data-safety-01)
# The sha check before a write leaves a window (backup, base store, intent commit with fsync, journal, tmp write +
# fsync) in which an Obsidian autosave / Sync download would be overwritten without trace. The final step therefore
# swaps the new file in ATOMICALLY and inspects what it displaced: Linux renameat2(RENAME_EXCHANGE) puts the vault
# file's inode at the tmp name in the same instant the new bytes appear at the vault path, so whatever the user
# saved before that instant (in place or by rename) is in the displaced file. If it is not the planned `before`
# bytes, the swap is undone (the newer local version is back at its path) and the caller stops with
# SKIP_*_RACE (exit 4). Filesystems without RENAME_EXCHANGE use a hard link of the current inode instead (catches
# in-place saves; a rename-save inside the microseconds between link and replace is the residual window).
_AT_FDCWD = -100
_RENAME_NOREPLACE, _RENAME_EXCHANGE = 1, 2
_RENAMEAT2 = []   # [] unresolved, [None] unavailable, [(func, ctypes)]


def _rename2(src, dst, flags):
    """renameat2(src, dst, flags): True on success, None when the call or flag is unsupported (caller falls back);
    other errors raise OSError (EEXIST -> FileExistsError)."""
    if not _RENAMEAT2:
        try:
            import ctypes
            f = ctypes.CDLL(None, use_errno=True).renameat2
            f.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            f.restype = ctypes.c_int
            _RENAMEAT2.append((f, ctypes))
        except (OSError, AttributeError, ImportError):
            _RENAMEAT2.append(None)
    if _RENAMEAT2[0] is None:
        return None
    f, ctypes = _RENAMEAT2[0]
    if f(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), flags) == 0:
        return True
    e = ctypes.get_errno()
    if e in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)):
        return None
    raise OSError(e, os.strerror(e), dst)


def _write_tmp(dst, data):
    """tmp file next to dst ('.nmig-tmp-' like wb_common.atomic_write: hidden from Obsidian and every scan), written
    and fsynced; keeps dst's permission bits."""
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".nmig-tmp-{os.getpid()}-{secrets.token_hex(4)}-{os.path.basename(dst)[:40]}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        if os.path.exists(dst) and not os.path.islink(dst):
            os.fchmod(fd, os.stat(dst).st_mode & 0o7777)
        view = memoryview(bytes(data))
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_quiet(p):
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


def _keep_aside(path, keep_dir, label):
    """A file we must not delete (it may hold a user's save): moved into keep_dir; returns its new path."""
    os.makedirs(keep_dir, exist_ok=True)
    dst = os.path.join(keep_dir, f"race-{now().replace(':', '')}-{secrets.token_hex(3)}-{label[:60]}")
    shutil.move(path, dst)
    return dst


def guarded_replace(dst, data, before, keep_dir):
    """Write `data` at dst only if dst holds the bytes with sha `before` at the instant of the swap. Returns None
    when written, else {"vault_sha": sha of the newer local bytes (left at dst), "kept": path or None}."""
    tmp = _write_tmp(dst, data)
    ours = sha_bytes(bytes(data))
    try:
        if _rename2(tmp, dst, _RENAME_EXCHANGE):
            displaced = sha_file(tmp) if _is_regular(tmp) else None
            if displaced == before:
                _unlink_quiet(tmp)
                _fsync_dir(os.path.dirname(dst))
                return None
            _rename2(tmp, dst, _RENAME_EXCHANGE)          # undo: the newer local bytes go back to dst
            kept = None
            if _is_regular(tmp) and sha_file(tmp) != ours:  # dst was written again meanwhile: keep that too
                kept = _keep_aside(tmp, keep_dir, os.path.basename(dst))
            _fsync_dir(os.path.dirname(dst))
            return {"vault_sha": sha_file(dst) if _is_regular(dst) else None, "kept": kept}
        hold = tmp + ".prev"
        try:
            os.link(dst, hold)
        except OSError:
            hold = None
        if hold is None:  # no hard links either: re-check as late as possible
            cur = sha_file(dst) if _is_regular(dst) else None
            if cur != before:
                return {"vault_sha": cur, "kept": None}
            os.replace(tmp, dst)
            _fsync_dir(os.path.dirname(dst))
            return None
        os.replace(tmp, dst)
        displaced = sha_file(hold)
        if displaced == before:
            _unlink_quiet(hold)
            _fsync_dir(os.path.dirname(dst))
            return None
        if _is_regular(dst) and sha_file(dst) == ours:
            os.replace(hold, dst)                         # undo
        else:
            _keep_aside(hold, keep_dir, os.path.basename(dst))
        _fsync_dir(os.path.dirname(dst))
        return {"vault_sha": sha_file(dst) if _is_regular(dst) else None, "kept": None}
    finally:
        # never delete a tmp name that may hold displaced local bytes (an interrupted undo): only our own bytes
        if os.path.lexists(tmp):
            if _is_regular(tmp) and sha_file(tmp) == ours:
                _unlink_quiet(tmp)
            else:
                _keep_aside(tmp, keep_dir, os.path.basename(dst))


def guarded_add(dst, data):
    """Create dst with `data` only while nothing exists at dst (never clobbers a file that appeared after the plan's
    check): True when created, False when dst exists."""
    tmp = _write_tmp(dst, data)
    try:
        try:
            r = _rename2(tmp, dst, _RENAME_NOREPLACE)
        except FileExistsError:
            return False
        if r:
            _fsync_dir(os.path.dirname(dst))
            return True
        try:
            os.link(tmp, dst)
        except FileExistsError:
            return False
        except OSError:
            if os.path.lexists(dst):
                return False
            os.replace(tmp, dst)
        _fsync_dir(os.path.dirname(dst))
        return True
    finally:
        if os.path.lexists(tmp):
            _unlink_quiet(tmp)


def base_semantic(data):
    """Normalise a .base file: Obsidian rewrites `note.X` as `X` in order/sort and re-indents (SPEC v1.2)."""
    import yaml
    try:
        d = yaml.safe_load(data.decode("utf-8") if isinstance(data, bytes) else data) or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    strip = lambda x: x[5:] if isinstance(x, str) and x.startswith("note.") else x  # noqa: E731
    for v in d.get("views") or []:
        if isinstance(v, dict):
            if isinstance(v.get("order"), list):
                v["order"] = [strip(x) for x in v["order"]]
            for srt in v.get("sort") or []:
                if isinstance(srt, dict) and "property" in srt:
                    srt["property"] = strip(srt["property"])
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def _fold(s):
    return _nfc(s).casefold()


def _is_regular(p):
    return os.path.isfile(p) and not os.path.islink(p)


def inline_code(s):
    """Markdown inline code for a path (no wikilinks in run logs, WB 5.1a)."""
    s = str(s).replace("\n", " ")
    run = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (run + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _yaml_dump(fm):
    import yaml
    return yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=100000, default_flow_style=False)


# ---------------------------------------------------------------- notes: frontmatter split / merge
class Note:
    """A parsed note: fm (dict), fm_text (str|None), body (str), raw parts for byte-exact re-assembly."""
    __slots__ = ("fm", "fm_text", "body", "rest")

    def __init__(self, data):
        text = data.decode("utf-8")
        fm_text, body, _ = wb_common.split_note(text)
        self.fm_text = fm_text
        self.body = body
        self.fm = wb_common.parse_frontmatter(fm_text) if fm_text is not None else {}
        self.rest = body


def parse_note(data):
    """Note or None (not UTF-8, unterminated or invalid frontmatter)."""
    try:
        return Note(data)
    except (UnicodeDecodeError, ValueError):
        return None


def _key_segments(fm_text):
    """{key: verbatim YAML text of that top-level key} (yaml node marks), or None."""
    import yaml
    try:
        node = yaml.compose(fm_text, Loader=yaml.SafeLoader)
    except Exception:  # noqa: BLE001
        return None
    if node is None or not isinstance(node, yaml.MappingNode) or node.flow_style:
        return None
    lines = fm_text.split("\n")
    starts = [(k.start_mark.line, k.value) for k, _ in node.value]
    out = {}
    for i, (ln, key) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        if end <= ln:
            return None
        seg = "\n".join(lines[ln:end]).rstrip("\n")
        out[key] = seg + "\n"
    return out


def merge_local_keys(s_bytes, l_bytes, keys):
    """Staged note + the local-only frontmatter keys of the vault note (kept verbatim when possible), appended after
    the staged keys; the staged body bytes are unchanged. None if it cannot be done safely."""
    if not keys:
        return bytes(s_bytes)
    s, l_ = parse_note(s_bytes), parse_note(l_bytes)
    if s is None or l_ is None or s.fm_text is None or l_.fm_text is None:
        return None
    if any(k in s.fm for k in keys) or any(k not in l_.fm for k in keys):
        return None
    want = dict(s.fm)
    for k in keys:
        want[k] = l_.fm[k]
    s_fm_text = s.fm_text + ("\n" if s.fm_text and not s.fm_text.endswith("\n") else "")
    cands = []
    segs = _key_segments(l_.fm_text)
    if segs is not None and all(str(k) in segs for k in keys):
        cands.append(s_fm_text + "".join(segs[str(k)] for k in keys))
    cands.append(_yaml_dump(want))
    for fm_text in cands:
        try:
            got = wb_common.parse_frontmatter(fm_text)
        except ValueError:
            continue
        if got == want and list(got) == list(want):
            return ("---\n" + fm_text + "---\n" + s.rest).encode("utf-8")
    return None


# ---------------------------------------------------------------- wikilink equivalence (WB 7.5)
_WL_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_QUOTE_PREFIX_RE = re.compile(r"^(?:[ \t]*>)*[ \t]*")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_CODE_SPAN_RE = re.compile(r"(`+)(?:.+?)(?<!`)\1(?!`)")


class Resolver:
    """Obsidian-like link resolution (getFirstLinkpathDest): './' '../' relative to the source folder; exact vault
    path (with or without '.md'); otherwise a unique path-suffix / basename match (same folder as the source wins
    when several match); anything else is unresolved. Keys: ('id', notion_id) for notes, ('path', rel) otherwise."""

    def __init__(self, rels, key_of):
        self.key_of = key_of
        self.exact = {}
        self.by_base = {}
        for r in rels:
            f = _fold(r)
            # Case- or NFC-equivalent files can coexist on Linux. Neither is a proven destination.
            if self.exact.setdefault(f, r) != r:
                self.exact[f] = None
            self.by_base.setdefault(f.rsplit("/", 1)[-1], []).append(r)

    def resolve(self, linkpath, source_rel):
        lp = _nfc(linkpath.strip())
        if not lp:
            return ("self",)
        cands = [lp, lp + ".md"]
        if lp.startswith("./") or lp.startswith("../"):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), lp))
            cands = [base, base + ".md"]
        for c in cands:
            folded = _fold(c.lstrip("/"))
            if folded in self.exact:
                hit = self.exact[folded]
                return self.key_of(hit) if hit is not None else None
        if lp.startswith(("/", "./", "../")):
            return None
        for c in cands:
            c = c.lstrip("/")
            fc = _fold(c)
            pool = [r for r in self.by_base.get(fc.rsplit("/", 1)[-1], ()) if _fold(r) == fc or _fold(r).endswith("/" + fc)]
            if len(pool) == 1:
                return self.key_of(pool[0])
            if len(pool) > 1:
                src_dir = _fold(posixpath.dirname(source_rel))
                same = [r for r in pool if _fold(posixpath.dirname(r)) == src_dir]
                if len(same) == 1:
                    return self.key_of(same[0])
                return None
        return None


def _norm_links_in(text, source_rel, res, note_targets_only=False):
    def sub(m):
        if _odd_backslashes(text, m.start()):
            return m.group(0)
        inner = m.group(2)
        i = inner.find("|")
        if i >= 0:
            tgt = inner[:i - 1] if i > 0 and inner[i - 1] == "\\" else inner[:i]
            alias = inner[i + 1:]
        else:
            tgt, alias = inner, None
        path, _, subp = tgt.partition("#")
        key = res.resolve(path, source_rel)
        if key is None or (note_targets_only and key[0] != "id"):
            return m.group(0)
        if note_targets_only:
            alias = None
        return "\x00" + json.dumps([m.group(1), list(key), subp, alias], ensure_ascii=False) + "\x00"
    return _WL_RE.sub(sub, text)


def _odd_backslashes(s, i):
    n = 0
    while i - n - 1 >= 0 and s[i - n - 1] == "\\":
        n += 1
    return n % 2 == 1


def norm_body(body, source_rel, res, *, note_targets_only=False):
    """Normalize links outside code. For a retained local body, compare note destinations independent of labels."""
    out, fence = [], None
    for line in body.split("\n"):
        stripped = _QUOTE_PREFIX_RE.sub("", line, count=1)
        if fence is not None:
            m = re.match(r"^(`{3,}|~{3,})[ \t]*$", stripped)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            out.append(line)
            continue
        m = _FENCE_RE.match(stripped)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(line)
            continue
        parts, pos = [], 0
        for cm in _CODE_SPAN_RE.finditer(line):
            parts.append(_norm_links_in(line[pos:cm.start()], source_rel, res, note_targets_only))
            parts.append(cm.group(0))
            pos = cm.end()
        parts.append(_norm_links_in(line[pos:], source_rel, res, note_targets_only))
        out.append("".join(parts))
    return "\n".join(out)


def norm_value(v, source_rel, res):
    if isinstance(v, str):
        return _norm_links_in(v, source_rel, res)
    if isinstance(v, list):
        return [norm_value(x, source_rel, res) for x in v]
    if isinstance(v, dict):
        return {str(k): norm_value(x, source_rel, res) for k, x in v.items()}
    return v


def is_exporter_key(k, exporter_keys):
    """Exporter-owned: a key of the staged rendering S or of the last staged rendering Bs, or a notion_* key.
    `aliases` is exporter-owned only where the exporter emits it (file name != title), so a user-added `aliases`
    is a local key like `tags`."""
    k = str(k)
    return k in exporter_keys or k.startswith("notion_")


class _Collect:
    """Resolver wrapper that records every resolved key (the link targets a note actually reaches)."""

    def __init__(self, res):
        self.res, self.keys = res, set()

    def resolve(self, linkpath, source_rel):
        key = self.res.resolve(linkpath, source_rel)
        if key is not None and key != ("self",):
            self.keys.add(key)
        return key


class _LinkCheck:
    """Resolver wrapper for the staged note S: `want` gives the object a link of S means (staged paths), `have`
    resolves it in the vault as the plan leaves it. A link is broken when the user's version reached that object
    (`reached`) but S's spelling would not reach it after the rewrite."""

    def __init__(self, want, have, reached):
        self.want, self.have, self.reached, self.bad = want, have, reached, []

    def resolve(self, linkpath, source_rel):
        w = self.want.resolve(linkpath, source_rel)
        if w is not None and w != ("self",) and w in self.reached and self.have.resolve(linkpath, source_rel) != w:
            self.bad.append(linkpath)
        return w


# ---------------------------------------------------------------- input files
def load_json_file(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refused(f"{what} unreadable: {path}: {exc}")


def load_holds(path):
    """--holds: ["id", ...] | {id: {"reason", "source", "subtree"}} | {id: "reason"} | {"holds": {...}}."""
    data = load_json_file(path, "--holds")
    if isinstance(data, dict) and isinstance(data.get("holds"), (dict, list)) and len(data) <= 3:
        data = data["holds"]
    out = {}
    items = [(x, {}) for x in data] if isinstance(data, list) else list(data.items()) if isinstance(data, dict) else None
    if items is None:
        raise Refused("--holds must be a JSON list or object")
    for k, v in items:
        try:
            nid = wb_common.norm_notion_id(k)
        except ValueError:
            raise Refused(f"--holds: not a notion id: {k!r}")
        if isinstance(v, str):
            v = {"reason": v}
        if not isinstance(v, dict):
            v = {}
        out[nid] = {"reason": str(v.get("reason") or "hold"), "source": v.get("source"),
                    "subtree": v.get("subtree") is not False}
    return out


def load_overrides(path):
    """--overrides: {id: local_rel} (exporter format) | {id: {"local_rel": ...}} | {"overrides": {...}}."""
    data = load_json_file(path, "--overrides")
    if isinstance(data, dict) and isinstance(data.get("overrides"), dict):
        data = data["overrides"]
    if not isinstance(data, dict):
        raise Refused("--overrides must be a JSON object")
    out = {}
    for k, v in data.items():
        try:
            nid = wb_common.norm_notion_id(k)
        except ValueError:
            raise Refused(f"--overrides: not a notion id: {k!r}")
        rel = v.get("local_rel") if isinstance(v, dict) else v
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
            raise Refused(f"--overrides: bad local_rel for {nid}: {rel!r}")
        out[nid] = posixpath.normpath(rel)
    return out


def load_only_ids(path):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as exc:
        raise Refused(f"--only-ids unreadable: {exc}")
    out = set()
    for ln in lines:
        s = ln.split("#", 1)[0].strip()
        if not s:
            continue
        for tok in re.split(r"[,\s]+", s):
            if tok:
                try:
                    out.add(wb_common.norm_notion_id(tok))
                except ValueError:
                    raise Refused(f"--only-ids: not a notion id: {tok!r}")
    return out


def load_adopt(path):
    """--adopt-writeback: {id: {"rel": ..., "l_sha": ...}} (an entry may carry "trashed": true)."""
    data = load_json_file(path, "--adopt-writeback")
    if isinstance(data, dict) and isinstance(data.get("items"), dict):
        data = data["items"]
    if not isinstance(data, dict):
        raise Refused("--adopt-writeback must be a JSON object")
    out = {}
    for k, v in data.items():
        try:
            nid = wb_common.norm_notion_id(k)
        except ValueError:
            raise Refused(f"--adopt-writeback: not a notion id: {k!r}")
        if not isinstance(v, dict):
            raise Refused(f"--adopt-writeback: entry of {nid} must be an object")
        out[nid] = {"rel": v.get("rel"), "l_sha": v.get("l_sha"), "trashed": bool(v.get("trashed"))}
    return out


def plan_sha256(plan):
    return sha_bytes(wb_common.canon_json({k: v for k, v in plan.items() if k != "plan_sha256"}).encode("utf-8"))


# ---------------------------------------------------------------- publisher
class Publisher:
    def __init__(self, run_id, vault, state, extra=None, auto=False, report_dir=None, *, staging_dir=None,
                 backup_dir=None, wb_db=None, holds=None, overrides=None, only_ids=None, adopt=None):
        self.run_id, self.vault = run_id, os.path.realpath(vault)
        self.stage = os.path.abspath(staging_dir) if staging_dir else os.path.join(WORK, "staging", run_id)
        self.sv = os.path.join(self.stage, "vault")
        with open(os.path.join(self.stage, "manifest.json"), encoding="utf-8") as fh:
            self.manifest = json.load(fh)
        for rel, meta in (extra or {}).items():
            self.manifest[rel] = meta
        self.state = os.path.abspath(state)
        self.wb_db = os.path.abspath(wb_db) if wb_db else os.path.join(os.path.dirname(self.state), "writeback.db")
        self.base_dir = os.path.join(os.path.dirname(self.wb_db), "base")
        self.auto = auto
        self.rep_dir = report_dir or os.path.join(WORK, "reports", run_id)
        os.makedirs(self.rep_dir, exist_ok=True)
        self.bk = os.path.abspath(backup_dir) if backup_dir else os.path.join(WORK, "backup", "vault-before", run_id)
        self.holds = holds or {}
        self.overrides = overrides or {}
        self.only_ids = only_ids
        self.adopt = adopt
        self.db = None
        self.rw = False
        self._open_ro()
        self.parents = self._load_parents()
        self.override_rejected, self.override_applied = self._load_override_report()
        self.page_json = self._load_page_json()
        self.needs_manual = []
        self.notes = []
        self._now_res = self._then_res = self._staged_res = None
        self._fm_nid_cache = {}
        self._id_index = None
        self._walk = None
        self._pub_all = None
        self._recovery = []
        self.recovered = []

    # ------------------------------------------------------------ state
    def _open_ro(self):
        if os.path.isfile(self.state):
            self.db = sqlite3.connect(f"file:{urllib.parse.quote(self.state)}?mode=ro", uri=True, timeout=30)
        else:
            self.db = None

    def open_rw(self):
        """Read-write export.db for apply: creates published_files / conflict_copies and adds staged_sha256 (D-07,
        wb_state.ensure_export_db; every INSERT below names its columns, L-02)."""
        if self.rw:
            return
        if self.db is not None:
            self.db.close()
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        self.db = sqlite3.connect(self.state, timeout=30)
        self.db.executescript(PUB_SCHEMA)
        self.db.commit()
        wb_state.ensure_export_db(self.state)
        self.rw = True
        self._pub_all = None
        self._persist_intents()

    # ------------------------------------------------------------ crash recovery (publish_intents)
    def _intent_rows(self):
        if not self._has_table("publish_intents"):
            return []
        return [dict(zip(INTENT_COLS, r)) for r in
                self.db.execute(f"SELECT {', '.join(INTENT_COLS)} FROM publish_intents ORDER BY rowid")]

    def _eval_intents(self, rows):
        """[(intent, record|None)] for the writes an interrupted earlier apply made but never recorded. record =
        {"sha", "staged"} (a published file; a MOVE/ADD source becomes 'moved'), {"conflict": True} (a conflict copy),
        or None: the vault does not hold the intended bytes (write not done, or changed since) - nothing to record."""
        out = []
        for it in self._intent_rows():
            rec = None
            try:
                p = safe_join(self.vault, it["rel_path"])
                cur = sha_file(p) if _is_regular(p) else None
            except (OSError, SystemExit):
                cur = None
            if cur and it["op"] in CONFLICT_OPS:
                rec = {"conflict": True} if cur == it["expect"] else None
            elif cur and cur == it["expect"]:
                rec = {"sha": cur, "staged": it["staged"]}
            elif cur and it["op"] == "MOVE" and cur == it["before"] and it["frm"] and \
                    not os.path.lexists(safe_join(self.vault, it["frm"])):
                old = rows.get(it["frm"]) or {}  # renamed, then interrupted before the content write
                rec = {"sha": cur, "staged": old.get("staged") or old.get("sha")}
            out.append((it, rec))
        return out

    def _overlay_intents(self, rows):
        self._recovery = self._eval_intents(rows)
        for it, rec in self._recovery:
            if rec is None or rec.get("conflict"):
                continue
            rows[it["rel_path"]] = {"sha": rec["sha"], "nid": it["notion_id"], "kind": it["kind"], "status": "active",
                                    "run_id": it["run_id"], "staged": rec["staged"]}
            if it["frm"] and it["op"] in ("MOVE", "ADD") and (rows.get(it["frm"]) or {}).get("status") == "active":
                rows[it["frm"]] = dict(rows[it["frm"]], status="moved")
        return rows

    def _persist_intents(self):
        """apply: record what the plan saw through the overlay, then clear publish_intents (one commit)."""
        rows = self._db_rows()
        self.recovered = []
        for it, rec in self._eval_intents(rows):
            self.recovered.append({"rel": it["rel_path"], "op": it["op"], "run_id": it["run_id"],
                                   "recorded": rec is not None})
            if rec is None:
                continue
            if rec.get("conflict"):
                self.db.execute("INSERT OR REPLACE INTO conflict_copies(obj_key, kind, staged_sha, conflict_rel,"
                                " conflict_sha, vault_rel, vault_sha, op, run_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (it["obj_key"], it["kind"], it["staged"], it["rel_path"], it["expect"], it["frm"],
                                 it["before"], it["op"], it["run_id"], now()))
                continue
            self.db.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status,"
                            " staged_sha256) VALUES(?,?,?,?,?,'active',?)",
                            (it["rel_path"], rec["sha"], it["notion_id"], it["kind"], it["run_id"], rec["staged"]))
            if it["frm"] and it["op"] in ("MOVE", "ADD"):
                self.db.execute("UPDATE published_files SET status='moved' WHERE rel_path=? AND status='active'",
                                (it["frm"],))
            if it["notion_id"] and it["kind"] in OBJ_MAP_KINDS and self._has_table("objects_map"):
                self.db.execute("UPDATE objects_map SET published_sha256=?, published_path=?, published_run=?"
                                " WHERE notion_id=?", (rec["sha"], it["rel_path"], it["run_id"], it["notion_id"]))
        if self.recovered:
            self.db.execute("DELETE FROM publish_intents")
        self.db.commit()
        self._pub_all = None

    def _intent(self, a, rel, expect, *, frm=None, before=None, staged=None, obj_key=None):
        """Committed BEFORE the vault write; deleted in the commit that records it."""
        self.db.execute("INSERT OR REPLACE INTO publish_intents(rel_path, op, frm, before, expect, staged, notion_id, kind,"
                        " obj_key, run_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (rel, a["op"], frm, before, expect, staged, a.get("nid"), a.get("kind"), obj_key, self.run_id,
                         now()))
        self.db.commit()

    def _intent_done(self, rel):
        self.db.execute("DELETE FROM publish_intents WHERE rel_path=?", (rel,))

    def _has_table(self, name):
        if self.db is None:
            return False
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _pf_cols(self):
        if not self._has_table("published_files"):
            return []
        return [r[1] for r in self.db.execute("PRAGMA table_info(published_files)")]

    def _db_rows(self):
        cols = self._pf_cols()
        out = {}
        if cols:
            staged = "staged_sha256" if "staged_sha256" in cols else "NULL"
            for r in self.db.execute(f"SELECT rel_path, sha256, notion_id, kind, status, run_id, {staged} FROM published_files"):
                out[r[0]] = {"sha": r[1], "nid": r[2], "kind": r[3], "status": r[4], "run_id": r[5], "staged": r[6]}
        return out

    def published_all(self):
        """{rel: {sha, staged, nid, kind, status, run_id}} for every published_files row (any status), with the
        writes of an interrupted earlier apply recorded (publish_intents overlay; persisted by open_rw)."""
        if self._pub_all is not None:
            return self._pub_all
        self._pub_all = self._overlay_intents(self._db_rows())
        return self._pub_all

    def published(self):
        return {rel: m for rel, m in self.published_all().items() if m["status"] == "active"}

    def _row(self, rel):
        cols = self._pf_cols()
        if not cols:
            return None
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in PF_COLS)
        r = self.db.execute(f"SELECT {sel} FROM published_files WHERE rel_path=?", (rel,)).fetchone()
        return dict(zip(PF_COLS, r)) if r else None

    def state_fingerprint(self):
        """sha256 over published_files (incl. staged) and conflict_copies, as the plan sees them (interrupted writes
        recorded): apply refuses a plan computed on other state."""
        h = hashlib.sha256()
        pub = self.published_all()
        for rel in sorted(pub):
            m = pub[rel]
            h.update(("\x1f".join([rel] + [m[k] or "" for k in ("sha", "nid", "kind", "status", "staged")]) + "\x1e")
                     .encode("utf-8"))
        h.update(b"\x1d")
        keys = set()
        if self._has_table("conflict_copies"):
            keys = {tuple(r) for r in self.db.execute("SELECT obj_key, kind, staged_sha FROM conflict_copies")}
        keys |= {k for k in self._recovered_conflicts()}
        for r in sorted(tuple(x or "" for x in k) for k in keys):
            h.update(("\x1f".join(r) + "\x1e").encode("utf-8"))
        return h.hexdigest()

    def _recovered_conflicts(self):
        self.published_all()
        return {(it["obj_key"], it["kind"], it["staged"]): (it["rel_path"], it["run_id"])
                for it, rec in (self._recovery or []) if rec and rec.get("conflict")}

    def _conflict_row(self, key, kind, staged):
        r = None
        if self._has_table("conflict_copies"):
            r = self.db.execute("SELECT conflict_rel, run_id FROM conflict_copies WHERE obj_key=? AND kind=? AND staged_sha=?",
                                (key, kind, staged)).fetchone()
        return r or self._recovered_conflicts().get((key, kind, staged))

    def _load_parents(self):
        if not self._has_table("objects_map"):
            return {}
        return {r[0]: r[1] for r in self.db.execute("SELECT notion_id, parent_id FROM objects_map")}

    def _load_override_report(self):
        """({id: reason} of overrides the exporter rejected, {id: computed_rel} of the ones it applied)."""
        p = os.path.join(self.stage, "export_report.json")
        rejected, applied = {}, {}
        try:
            with open(p, encoding="utf-8") as fh:
                rep = json.load(fh)
        except (OSError, ValueError):
            return rejected, applied
        ov = rep.get("overrides") or {}
        for key, out in (("rejected", rejected), ("applied", applied)):
            for r in ov.get(key) or []:
                try:
                    nid = wb_common.norm_notion_id(r.get("notion_id"))
                except (ValueError, AttributeError):
                    continue
                out[nid] = str(r.get("reason") or "rejected") if key == "rejected" else r.get("computed_rel")
        return rejected, applied

    def _both_renamed(self, nid, old_rel):
        """WB 7.4.5: the exporter's title-derived path is neither the original published name nor an earlier local
        name of this object, i.e. Notion renamed it while the local name override is open."""
        comp = self.override_applied.get(nid)
        if not comp:
            return None
        known = {old_rel} | {r for r, m in self.published_all().items() if m["nid"] == nid and m["status"] == "renamed"}
        return comp if comp not in known else None

    def _load_page_json(self):
        p = os.path.join(self.stage, "page_json.jsonl")
        if not os.path.isfile(p):
            return None
        out = {}
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    out[r["rel_path"]] = r
        return out

    # ------------------------------------------------------------ files
    def src_of(self, rel):
        m = self.manifest.get(rel) or {}
        return m.get("src") or os.path.join(self.sv, rel)

    def staged_bytes(self, rel, sha):
        with open(self.src_of(rel), "rb") as fh:
            data = fh.read()
        return data if sha_bytes(data) == sha else None

    def base_bytes(self, sha, rel=None):
        """Bytes of a published version: state/base content store, else the staging run that published it."""
        if not sha:
            return None
        try:
            data = wb_state.base_content(sha, base_dir=self.base_dir)
        except (wb_state.BaseCorrupt, ValueError):
            data = None
        if data is not None:
            return data
        row = self.published_all().get(rel) if rel else None
        runs = [row["run_id"]] if row and row.get("run_id") else []
        for run in runs:
            p = os.path.join(os.path.dirname(self.stage), run, "vault", rel)
            try:
                if _is_regular(p) and sha_file(p) == sha:
                    with open(p, "rb") as fh:
                        return fh.read()
            except OSError:
                pass
        m = self.manifest.get(rel) if rel else None
        if m and m.get("sha256") == sha:
            try:
                return self.staged_bytes(rel, sha)
            except OSError:
                return None
        return None

    def vault_bytes(self, rel):
        with open(safe_join(self.vault, rel), "rb") as fh:
            return fh.read()

    # ------------------------------------------------------------ vault indexes (lazy)
    def _walk_vault(self):
        if self._walk is not None:
            return list(self._walk)
        out = []
        for d, dirs, files in os.walk(self.vault):
            reld = os.path.relpath(d, self.vault)
            reld = "" if reld == "." else reld.replace(os.sep, "/")
            dirs[:] = [x for x in dirs if not (reld == "" and x in SKIP_DIRS) and not os.path.islink(os.path.join(d, x))]
            for f in files:
                if f.startswith(".nmig-tmp-"):
                    continue
                out.append(f if not reld else f"{reld}/{f}")
        self._walk = out
        return list(out)

    def fm_nid(self, rel):
        """notion_id from the frontmatter of a vault .md file (cached), or None."""
        if rel in self._fm_nid_cache:
            return self._fm_nid_cache[rel]
        nid = None
        try:
            p = safe_join(self.vault, rel)
            if _is_regular(p):
                with open(p, "rb") as fh:
                    head = fh.read(65536)
                text = head.decode("utf-8", "replace")
                if text.startswith("---"):
                    end = re.search(r"\n(?:---|\.\.\.)[ \t]*\r?\n", text)
                    fm = text[:end.start()] if end else text
                    m = re.search(r"^notion_id:[ \t]*['\"]?([0-9a-fA-F-]{32,36})['\"]?[ \t]*\r?$", fm, re.M)
                    if m:
                        try:
                            nid = wb_common.norm_notion_id(m.group(1))
                        except ValueError:
                            nid = None
        except (OSError, SystemExit):
            nid = None
        self._fm_nid_cache[rel] = nid
        return nid

    def nid_elsewhere(self, nid, rel):
        """Vault notes (outside staged paths and Notion_Obsidian/) whose frontmatter carries `nid`."""
        if self._id_index is None:
            idx = {}
            for r in self._walk_vault():
                if not r.endswith(".md") or r.startswith(TASK_DIR + "/"):
                    continue
                n = self.fm_nid(r)
                if n:
                    idx.setdefault(n, []).append(r)
            self._id_index = idx
        return [r for r in self._id_index.get(nid, []) if r != rel and r not in self.manifest]

    def now_resolver(self):
        if self._now_res is None:
            def key_of(r):
                if r.endswith(".md"):
                    n = self.fm_nid(r)
                    if n:
                        return ("id", n)
                return ("path", r)
            self._now_res = Resolver(self._walk_vault(), key_of)
        return self._now_res

    def then_resolver(self):
        if self._then_res is None:
            nid_of = {}  # B-time paths: published rows first (active before moved/renamed), then new staged paths
            for rel, m in sorted(self.published_all().items(), key=lambda x: x[1]["status"] != "active"):
                if m["nid"]:
                    nid_of.setdefault(rel, m["nid"])
            for rel, m in self.manifest.items():
                if m.get("notion_id"):
                    nid_of.setdefault(rel, m["notion_id"])

            def key_of(r):
                if r.endswith(".md") and nid_of.get(r):
                    return ("id", nid_of[r])
                return ("path", r)
            self._then_res = Resolver(set(self.published_all()) | set(self.manifest), key_of)
        return self._then_res

    def staged_resolver(self):
        """What a link of a staged note means: the exporter rendered it against the staged paths."""
        if self._staged_res is None:
            def key_of(r):
                n = (self.manifest.get(r) or {}).get("notion_id")
                return ("id", n) if r.endswith(".md") and n else ("path", r)
            self._staged_res = Resolver(set(self.manifest), key_of)
        return self._staged_res

    def post_resolver(self, actions):
        """The vault as this plan leaves it: current files, plus ADD / MOVE targets, minus MOVE sources."""
        rels, added = set(self._walk_vault()), {}
        for a in actions:
            if a["op"] in ("ADD", "MOVE"):
                if a["op"] == "MOVE":
                    rels.discard(a["frm"])
                rels.add(a["rel"])
                added[a["rel"]] = a.get("nid")

        def key_of(r):
            if r.endswith(".md"):
                n = added.get(r) if r in added else self.fm_nid(r)
                if n:
                    return ("id", n)
            return ("path", r)
        return Resolver(rels, key_of)

    def _check_rewrites(self, actions):
        """A rewrite that replaces the user's link spelling with the Notion rendering (LINK_REWRITE_ONLY, LOCAL_KEYS_ONLY
        with links, an adopted note's REPLACE) must not break a link: every object the user's version reaches must
        still be reached by S's link to it in the vault as this plan leaves it. Otherwise the user's file is kept
        (CONFLICT_BOTH; for an adopted note the ADOPT is dropped too: CONFLICT_UNTRACKED)."""
        idx = [i for i, a in enumerate(actions) if a.get("links_rewritten")]
        if not idx:
            return
        post, want, now_r = self.post_resolver(actions), self.staged_resolver(), self.now_resolver()
        drop = set()
        for i in idx:
            a = actions[i]
            rel = a["rel"]
            try:
                l_bytes, s_bytes = self.vault_bytes(rel), self.staged_bytes(rel, a["sha"])
            except OSError:
                continue
            L, S = parse_note(l_bytes), parse_note(s_bytes) if s_bytes is not None else None
            if L is None or S is None:
                continue
            have = _Collect(now_r)
            norm_body(L.body, rel, have)
            norm_value(L.fm, rel, have)
            chk = _LinkCheck(want, post, have.keys)
            norm_body(S.body, rel, chk)
            norm_value(S.fm, rel, chk)
            if not chk.bad:
                continue
            why = f"the Notion link spelling would break {len(chk.bad)} working local link(s): " + \
                ", ".join(sorted(set(chk.bad))[:3])
            base = {"rel": rel, "sha": a["sha"], "nid": a.get("nid"), "kind": a.get("kind")}
            if a.get("origin") == "WRITEBACK" and i > 0 and actions[i - 1]["op"] == "ADOPT" and actions[i - 1]["rel"] == rel:
                drop.add(i - 1)
                actions[i] = self._conflict(dict(base, op="CONFLICT_UNTRACKED", cur=a["before"], reason=why))
            else:
                actions[i] = self._conflict(dict(base, op="CONFLICT_BOTH", cur=a["before"], reason=why))
        if drop:
            actions[:] = [a for i, a in enumerate(actions) if i not in drop]

    def _skip_orphan_adds(self, actions):
        """A non-note file of an object whose note is skipped (e.g. the .base of a database whose folder the user
        renamed: DB.md SKIP_HELD duplicate_id_elsewhere) is not re-ADDed at the old path either."""
        held = {}
        for a in actions:
            if a["op"] == "SKIP_HELD" and a.get("nid") and a.get("kind") in NOTE_KINDS:
                held.setdefault(a["nid"], a.get("reason"))
        for i, a in enumerate(actions):
            if a["op"] == "ADD" and a.get("nid") in held and a.get("kind") not in NOTE_KINDS:
                actions[i] = {"rel": a["rel"], "sha": a["sha"], "nid": a["nid"], "kind": a["kind"], "op": "SKIP_HELD",
                              "reason": f"object_skipped:{held[a['nid']]}"}

    # ------------------------------------------------------------ equivalence
    def equiv(self, l_bytes, l_rel, bv_bytes, b_rel, s_bytes, bs_bytes):
        """L equal to the published vault bytes Bv modulo wikilink spelling and local-only frontmatter keys?
        Returns None (a real edit) or {"local_keys": [...], "links_changed": bool, "format_changed": bool}.
        Exporter-owned keys = keys of S and of the last staged rendering Bs, and notion_* (is_exporter_key)."""
        L, Bv, S = parse_note(l_bytes), parse_note(bv_bytes), parse_note(s_bytes)
        if L is None or Bv is None or S is None:
            return None
        Bs = parse_note(bs_bytes) if bs_bytes is not None else None
        exporter_keys = {str(k) for k in S.fm} | ({str(k) for k in Bs.fm} if Bs is not None else set())
        local_l = [k for k in L.fm if not is_exporter_key(k, exporter_keys)]
        local_b = [k for k in Bv.fm if not is_exporter_key(k, exporter_keys)]
        core_l = {str(k): v for k, v in L.fm.items() if k not in local_l}
        core_b = {str(k): v for k, v in Bv.fm.items() if k not in local_b}
        if (L.fm_text is None) != (Bv.fm_text is None):
            return None
        now_r, then_r = self.now_resolver(), self.then_resolver()
        if norm_value(core_l, l_rel, now_r) != norm_value(core_b, b_rel, then_r):
            return None
        if norm_body(L.body, l_rel, now_r) != norm_body(Bv.body, b_rel, then_r):
            return None
        links = core_l != core_b or L.body != Bv.body
        return {"local_keys": [str(k) for k in local_l], "links_changed": links,
                "format_changed": not links and not local_l}

    def _respelled_link_lines(self, previous, staged, local, rel):
        """Return changed export lines and the local body only when the remote change merely respelled links."""
        if previous is None or staged is None or previous == staged:
            return None
        old, new, saved = parse_note(previous), parse_note(staged), parse_note(local)
        if any(note is None for note in (old, new, saved)) or old.fm_text != new.fm_text or old.body == new.body:
            return None
        if any("\x00" in note.body for note in (old, new, saved)):
            return None
        staged_res = self.staged_resolver()
        # The source must have changed link paths only. A remote label, fragment or inline text change is real.
        normalized = norm_body(new.body, rel, staged_res)
        if norm_body(old.body, rel, self.then_resolver()) != normalized:
            return None
        link_lines = {i for i, (before, after) in enumerate(zip(old.body.split("\n"), new.body.split("\n")))
                      if before != after}
        expected = norm_body(new.body, rel, staged_res, note_targets_only=True).split("\n")
        return link_lines, expected, saved.body

    def _check_mixed_edits(self, actions, edits):
        if not edits:
            return
        # Alias differences are safe on retained local lines. A MOVE later in this plan can invalidate a spelling
        # that works today, so compare the exporter's intended notion_id with the destination after publication.
        post = self.post_resolver(actions)
        for index, (link_lines, expected, saved) in edits.items():
            a = actions[index]
            retained = norm_body(saved, a["rel"], post, note_targets_only=True).split("\n")
            matching = difflib.SequenceMatcher(None, expected, retained, autojunk=False).get_matching_blocks()
            for start, _, size in matching:
                link_lines.difference_update(range(start, start + size))
            if link_lines:
                actions[index] = self._conflict(a)
            else:
                a.update(op="LOCAL_ONLY", variant="body",
                         reason="local edit kept; the export only respelled links to the same objects")

    def local_keys_vs_staged(self, l_bytes, s_bytes, bs_bytes):
        """Local-only keys of a vault note that equals what we published (L == B.sha): keys not produced by the
        exporter in S or in the last staged rendering Bs."""
        L, S = parse_note(l_bytes), parse_note(s_bytes)
        if L is None or S is None:
            return None
        Bs = parse_note(bs_bytes) if bs_bytes is not None else None
        exporter_keys = {str(k) for k in S.fm} | ({str(k) for k in Bs.fm} if Bs is not None else set())
        return [str(k) for k in L.fm if not is_exporter_key(k, exporter_keys)]

    # ------------------------------------------------------------ holds
    def hold_reason(self, nid):
        if not nid or not self.holds:
            return None
        if nid in self.holds:
            h = self.holds[nid]
            return f"hold:{h['reason']}" + (f"({h['source']})" if h.get("source") else "")
        seen, cur = {nid}, self.parents.get(nid)
        while cur and cur not in seen:
            seen.add(cur)
            h = self.holds.get(cur)
            if h and h.get("subtree", True):
                return f"hold_subtree:{cur}:{h['reason']}"
            cur = self.parents.get(cur)
        return None

    # ------------------------------------------------------------ plan
    def plan(self):
        pub_all = self.published_all()
        pub = {rel: dict(m) for rel, m in pub_all.items() if m["status"] == "active"}
        by_obj = {}
        for rel, m in pub.items():
            if m["nid"]:
                by_obj[(m["nid"], m["kind"])] = rel
        staged_rels = set(self.manifest)
        staged_obj = {}
        for rel, m in self.manifest.items():
            if m.get("notion_id"):
                staged_obj.setdefault((m["notion_id"], m["kind"]), rel)
        actions, skip_obj, renamed_from, moved_from = [], {}, set(), set()
        mixed_edits = {}
        adopt_seen = set()
        self.notes = []
        self.needs_manual = []
        self.published_all()
        for r in self._recovery:
            self.notes.append({"type": "recovered", "rel": r[0]["rel_path"], "op": r[0]["op"], "run_id": r[0]["run_id"],
                               "recorded": r[1] is not None})

        # (A) name overrides (WB 7.4.2): adopt applied ones, skip objects whose override was not honoured
        for nid in sorted(self.overrides):
            local = self.overrides[nid]
            if self.only_ids is not None and nid not in self.only_ids:
                continue
            srels = [(k, r) for (n, k), r in staged_obj.items() if n == nid and k in ("page", "row", "template")]
            if not srels:
                self.notes.append({"type": "override", "nid": nid, "local_rel": local, "state": "not_staged"})
                continue
            kind, srel = srels[0]
            if nid in self.override_rejected or srel != local:
                why = f"override_rejected:{self.override_rejected[nid]}" if nid in self.override_rejected \
                    else "override_not_applied"
                skip_obj[nid] = why
                self.notes.append({"type": "override", "nid": nid, "local_rel": local, "staged_rel": srel,
                                   "state": "not_applied", "reason": why})
                continue
            old = by_obj.get((nid, kind))
            if not old or old == local:
                self.notes.append({"type": "override", "nid": nid, "local_rel": local, "state": "in_effect",
                                   "old_rel": old, "notion_renamed": self._both_renamed(nid, None)})
                continue
            old_abs, new_abs = safe_join(self.vault, old), safe_join(self.vault, local)
            if os.path.lexists(old_abs) and os.path.lexists(new_abs):
                skip_obj[nid] = "override_both_paths_exist"
            elif os.path.lexists(old_abs) or not os.path.lexists(new_abs):
                skip_obj[nid] = "override_target_missing"
            elif not _is_regular(new_abs) or self.fm_nid(local) != nid:
                skip_obj[nid] = "override_target_mismatch"
            elif self.hold_reason(nid):
                skip_obj[nid] = self.hold_reason(nid)
            else:
                cur = sha_file(new_abs)
                b = pub.pop(old)
                actions.append({"op": "ADOPT_RENAME", "rel": local, "frm": old, "sha": b["sha"], "staged": b["staged"],
                                "cur": cur, "nid": nid, "kind": kind, "reason": "local same-folder rename adopted"})
                b = dict(b, run_id=self.run_id)
                pub[local] = b
                by_obj[(nid, kind)] = local
                renamed_from.add(old)
                self.notes.append({"type": "override", "nid": nid, "local_rel": local, "state": "adopted", "old_rel": old,
                                   "notion_renamed": self._both_renamed(nid, old)})
                continue
            self.notes.append({"type": "override", "nid": nid, "local_rel": local, "state": "skipped",
                               "reason": skip_obj[nid], "old_rel": old})

        # (B) every staged file
        for rel in sorted(self.manifest):
            m = self.manifest[rel]
            nid, kind, S = m.get("notion_id"), m["kind"], m["sha256"]
            if self.only_ids is not None and nid not in self.only_ids:
                continue
            base = {"rel": rel, "sha": S, "nid": nid, "kind": kind}
            why = self.hold_reason(nid) or skip_obj.get(nid)
            ent = self.adopt.get(nid) if (self.adopt is not None and nid and kind in NOTE_KINDS) else None
            if ent is not None and not why and ent.get("trashed"):
                why = "adopt-writeback: trashed via write-back but still in staging"
            if why:
                actions.append(dict(base, op="SKIP_HELD", reason=why))
                if ent is not None and nid not in adopt_seen:
                    adopt_seen.add(nid)
                    self._manual_entry(nid, ent, rel, f"not adopted: {why}")
                continue
            if ent is not None:
                adopt_seen.add(nid)
                actions.extend(self._plan_adopt(rel, m, ent, pub, by_obj))
                continue
            old_rel = by_obj.get((nid, kind)) if nid else None
            dst = safe_join(self.vault, rel)
            if old_rel and old_rel != rel and old_rel not in staged_rels:
                old_abs = safe_join(self.vault, old_rel)
                if os.path.exists(old_abs) and not os.path.lexists(dst):
                    moved_from.add(old_rel)
                    ob = pub[old_rel]
                    if _is_regular(old_abs) and sha_file(old_abs) == ob["sha"]:
                        a = dict(base, op="MOVE", frm=old_rel, before=ob["sha"], expect=S,
                                 reason="Notion renamed/moved the object; old file unchanged")
                        a["from"] = old_rel
                        if (ob["staged"] or ob["sha"]) != ob["sha"] and kind in NOTE_KINDS:
                            a = self._plan_keys(a, self.vault_bytes(old_rel), rel, S, ob)
                        actions.append(a)
                        continue
                    actions.append(self._conflict(dict(base, op="CONFLICT_MOVED_EDITED", frm=old_rel,
                                                       reason="Notion moved the object; old file edited locally")))
                    actions[-1]["from"] = old_rel
                    continue
            if not os.path.lexists(dst):
                other = self.nid_elsewhere(nid, rel) if nid and kind in NOTE_KINDS else []
                if other:
                    actions.append(dict(base, op="SKIP_HELD", reason="duplicate_id_elsewhere", found=sorted(other)[:5]))
                    continue
                a = dict(base, op="ADD", expect=S, restore=rel in pub,
                         reason="restore missing published file" if rel in pub else "new")
                if old_rel and old_rel != rel and not os.path.lexists(safe_join(self.vault, old_rel)):
                    a["frm"] = old_rel  # Notion moved it and the old file is gone: the old row becomes 'moved'
                    moved_from.add(old_rel)
                actions.append(a)
                continue
            if os.path.islink(dst) or not os.path.isfile(dst):
                actions.append(self._conflict(dict(base, op="CONFLICT_NOT_FILE", reason="target is not a regular file")))
                continue
            l_bytes = self.vault_bytes(rel)
            L = sha_bytes(l_bytes)
            if L == S:
                actions.append(dict(base, op="UNCHANGED", cur=L, reason="vault == staged"))
                continue
            B = pub.get(rel)
            if B is None:
                actions.extend(self._plan_untracked(rel, m, base, l_bytes, L, by_obj))
                continue
            staged_eff = B["staged"] or B["sha"]
            if L == B["sha"]:
                if staged_eff == S:
                    actions.append(dict(base, op="UNCHANGED", cur=L, reason="vault == published, Notion unchanged"))
                    continue
                a = dict(base, op="REPLACE", before=L, expect=S, reason="vault == published, Notion changed")
                if B["sha"] != staged_eff and kind in NOTE_KINDS:
                    a = self._plan_keys(a, l_bytes, rel, S, B)
                actions.append(a)
                continue
            if kind == "generated" and rel.startswith(TASK_DIR + "/"):
                actions.append(dict(base, op="GENERATED_OVERWRITE", before=L, expect=S,
                                    reason="tool-owned file edited locally: backup + overwrite"))
                continue
            if kind == "base":
                sem_l = base_semantic(l_bytes)
                s_bytes = self.staged_bytes(rel, S)
                if sem_l is not None and s_bytes is not None and sem_l == base_semantic(s_bytes):
                    actions.append(dict(base, op="UNCHANGED_SEMANTIC", cur=L, reason=".base re-serialised (same meaning)"))
                elif staged_eff == S:
                    actions.append(dict(base, op="LOCAL_ONLY", cur=L, variant="base", reason="local edit, Notion unchanged"))
                else:
                    bv = self.base_bytes(B["sha"], rel)
                    if sem_l is not None and bv is not None and sem_l == base_semantic(bv):
                        actions.append(dict(base, op="REPLACE", before=L, expect=S,
                                            reason=".base equal in meaning to the published version, Notion changed"))
                    else:
                        actions.append(self._conflict(dict(base, op="CONFLICT_BOTH", cur=L,
                                                           reason="local edit and Notion change")))
                continue
            is_note = kind in NOTE_KINDS and rel.endswith(".md")
            if staged_eff == S:
                a = dict(base, op="LOCAL_ONLY", cur=L, reason="local edit, Notion unchanged")
                if is_note:
                    a["variant"] = self._variant(l_bytes, rel, B, rel)
                actions.append(a)
                continue
            if is_note:
                bv = self.base_bytes(B["sha"], rel)
                bs = bv if B["sha"] == staged_eff else self.base_bytes(staged_eff, rel)
                s_bytes = self.staged_bytes(rel, S)
                eq = self.equiv(l_bytes, rel, bv, rel, s_bytes, bs) if bv is not None and s_bytes is not None else None
                if eq is not None:
                    if eq["local_keys"]:
                        merged = merge_local_keys(s_bytes, l_bytes, eq["local_keys"])
                        if merged is not None:
                            actions.append(dict(base, op="LOCAL_KEYS_ONLY", before=L, keys=eq["local_keys"],
                                                expect=sha_bytes(merged), links_rewritten=eq["links_changed"],
                                                reason="only local frontmatter keys (and link spelling) differ; keys kept"))
                            continue
                    else:
                        actions.append(dict(base, op="LINK_REWRITE_ONLY", before=L, expect=S,
                                            links_rewritten=eq["links_changed"],
                                            reason="only wikilink spelling differs" if eq["links_changed"]
                                            else "only frontmatter formatting differs"))
                        continue
                mixed = self._respelled_link_lines(bs, s_bytes, l_bytes, rel)
                if mixed is not None:
                    mixed_edits[len(actions)] = mixed
                    actions.append(dict(base, op="CONFLICT_BOTH", cur=L, reason="local edit and Notion change"))
                    continue
            actions.append(self._conflict(dict(base, op="CONFLICT_BOTH", cur=L, reason="local edit and Notion change")))

        self._skip_orphan_adds(actions)
        self._check_mixed_edits(actions, mixed_edits)
        self._check_rewrites(actions)

        # (C) objects that left the staging run: trashed via write-back, or tombstones (object-based, WB 6.6 / 14.3)
        confirmed = self._confirmed_trash()
        tomb = []
        for rel in sorted(pub):
            p = pub[rel]
            if rel in moved_from or rel in renamed_from or rel.startswith(f"{TASK_DIR}/运行记录/") \
                    or rel.startswith(".obsidian/") or p.get("kind") in ("obsidian-config", "runlog"):
                continue
            if p["nid"]:
                if (p["nid"], p["kind"]) in staged_obj:
                    continue
            elif rel in staged_rels:
                continue
            if self.only_ids is not None and p["nid"] not in self.only_ids:
                continue
            if p["nid"] and p["nid"] in confirmed and not os.path.lexists(safe_join(self.vault, rel)):
                actions.append({"op": "TRASHED_VIA_WRITEBACK", "rel": rel, "sha": p["sha"], "nid": p["nid"],
                                "kind": p["kind"], "reason": "page trashed via write-back; file gone"})
                continue
            if self.only_ids is None:
                tomb.append(rel)

        # (D) --adopt-writeback entries that nothing consumed: never silently dropped (I2 marks them 待人工处理)
        for nid, ent in sorted((self.adopt or {}).items()):
            if nid in adopt_seen:
                continue
            if ent.get("trashed"):
                if any(a["op"] == "TRASHED_VIA_WRITEBACK" and a.get("nid") == nid for a in actions) or \
                        any(m["nid"] == nid and m["status"] == "trashed_via_writeback" for m in pub_all.values()):
                    continue
                why = "trashed via write-back, but the vault file still exists or the object was never published here"
            else:
                why = "not in staging (dropped by refetch / export): not adopted"
            self._manual_entry(nid, ent, None, why)
        return actions, tomb

    def _manual_entry(self, nid, ent, staged_rel, why):
        rel = ent.get("rel") or staged_rel
        cur = None
        try:
            p = safe_join(self.vault, rel) if rel else None
            cur = sha_file(p) if p and _is_regular(p) else None
        except (OSError, SystemExit):
            cur = None
        self.needs_manual.append({"notion_id": nid, "rel": rel, "staged_rel": staged_rel, "reason": why,
                                  "l_sha": ent.get("l_sha"), "cur": cur})

    def _confirmed_trash(self):
        ids = {n for n, e in (self.adopt or {}).items() if e.get("trashed")}
        if os.path.isfile(self.wb_db):
            try:
                con = wb_state.connect_readonly(self.wb_db)
            except (sqlite3.Error, ValueError):
                con = None
            if con is not None:
                try:
                    ids |= {r[0] for r in con.execute("SELECT notion_id FROM pending_deletes WHERE status='confirmed'")
                            if r[0]}
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        out = set()
        for i in ids:
            try:
                out.add(wb_common.norm_notion_id(i))
            except ValueError:
                pass
        return out

    def _keys_plan(self, l_bytes, s_bytes, bs):
        """(keys, merged, clash) for writing S over L: L's local-only keys, S with them merged in, and the keys that
        were local in L (not produced by the last staged rendering Bs) but that S now produces itself (e.g. a user
        `aliases` once the exporter emits aliases): those cannot be kept, so the write must not happen."""
        keys = self.local_keys_vs_staged(l_bytes, s_bytes, bs) or []
        merged = merge_local_keys(s_bytes, l_bytes, keys) if keys else None
        clash = []
        L, Sn, Bs = parse_note(l_bytes), parse_note(s_bytes), parse_note(bs) if bs is not None else None
        if L is not None and Sn is not None and Bs is not None:
            bs_keys, s_keys = {str(k) for k in Bs.fm}, {str(k) for k in Sn.fm}
            clash = [str(k) for k in L.fm if not is_exporter_key(k, bs_keys) and str(k) in s_keys]
        return keys, merged, clash

    def _plan_keys(self, a, l_bytes, rel, S, B):
        """A write over a file that holds previously kept local keys (B.sha = merged bytes): keep them again
        (LOCAL_KEYS_ONLY). Returns the action to plan; a conflict when a kept key clashes with a key S now produces
        or the keys cannot be merged (the local value would otherwise be overwritten silently)."""
        s_bytes = self.staged_bytes(rel, S)
        if s_bytes is None:
            return a
        bs = self.base_bytes(B["staged"], rel) if B.get("staged") else None
        keys, merged, clash = self._keys_plan(l_bytes, s_bytes, bs)
        if clash or (keys and merged is None):
            why = (f"local frontmatter key(s) now also produced by Notion: {', '.join(clash)}" if clash
                   else "local frontmatter keys cannot be merged into the Notion version")
            base = {"rel": a["rel"], "sha": a["sha"], "nid": a.get("nid"), "kind": a.get("kind")}
            if a["op"] == "MOVE":
                c = self._conflict(dict(base, op="CONFLICT_MOVED_EDITED", frm=a["frm"], reason=why))
                c["from"] = a["frm"]
                return c
            return self._conflict(dict(base, op="CONFLICT_BOTH", cur=a["before"], reason=why))
        if keys:
            a["keys"] = keys
            a["expect"] = sha_bytes(merged)
            if a["op"] == "REPLACE":
                a["op"] = "LOCAL_KEYS_ONLY"
                a["reason"] = "vault == published (kept local keys), Notion changed; keys kept again"
        return a

    def _variant(self, l_bytes, rel, B, b_rel):
        bv = self.base_bytes(B["sha"], b_rel)
        staged_eff = B["staged"] or B["sha"]
        if bv is None:
            return "unknown"
        bs = bv if staged_eff == B["sha"] else self.base_bytes(staged_eff, b_rel)
        s_bytes = bs if bs is not None else bv
        eq = self.equiv(l_bytes, rel, bv, b_rel, s_bytes, bs)
        if eq is None:
            return "content"
        if eq["local_keys"] and eq["links_changed"]:
            return "keys_and_links"
        if eq["local_keys"]:
            return "keys_only"
        return "links_only" if eq["links_changed"] else "format_only"

    def _conflict(self, a):
        """Conflict action: where the copy goes, and whether this (object, staged sha) already has one (D-13)."""
        a["obj_key"] = a["nid"] or f"path:{a['rel']}"
        a["conflict_rel"] = self.conflict_rel(a["rel"])
        row = self._conflict_row(a["obj_key"], a["kind"], a["sha"])
        if row:
            a["dedup"] = True
            a["conflict_rel"] = row[0]
            a["conflict_run"] = row[1]
        return a

    def conflict_rel(self, rel):
        d, b = posixpath.split(rel)
        stem, ext = posixpath.splitext(b)
        suffix = f" (Notion 版本 {self.run_id}){ext}"
        while stem and len((stem + suffix).encode("utf-8")) > 250:
            stem = stem[:-1]
        parts = [TASK_DIR, "conflicts", self.run_id] + ([d] if d else []) + [stem + suffix]
        return "/".join(parts)

    def conflict_content(self, a, s_bytes):
        """L-07: notes get generated frontmatter (no notion_id / notion_url / aliases), body unchanged; other files
        (.base, .canvas, attachments) are copied unchanged."""
        if not (a["kind"] in NOTE_KINDS and a["rel"].endswith(".md")):
            return bytes(s_bytes)
        n = parse_note(s_bytes)
        fm = {"notion_kind": "generated", "generated_by": "publish_vault conflict-copy",
              "conflict_of_notion_id": a.get("nid"), "conflict_run": self.run_id, "conflict_vault_path": a["rel"]}
        if n is None:
            return ("---\n" + _yaml_dump(fm) + "---\n\n").encode("utf-8") + bytes(s_bytes)
        for k, v in n.fm.items():
            if str(k) not in ("notion_id", "notion_url", "aliases", "notion_kind") and str(k) not in fm:
                fm[str(k)] = v
        return ("---\n" + _yaml_dump(fm) + "---\n" + n.rest).encode("utf-8")

    def _plan_untracked(self, rel, m, base, l_bytes, L, by_obj):
        """Target exists but this tool never published it here."""
        nid, kind, S = m.get("notion_id"), m["kind"], m["sha256"]
        if kind == "generated" and rel.startswith(TASK_DIR + "/"):
            return [dict(base, op="GENERATED_OVERWRITE", before=L, expect=S, reason="tool-owned file not in published_files")]
        if kind == "base":
            s_bytes = self.staged_bytes(rel, S)
            sem = base_semantic(l_bytes)
            if sem is not None and s_bytes is not None and sem == base_semantic(s_bytes):
                return [dict(base, op="UNCHANGED_SEMANTIC", cur=L, reason=".base re-serialised (same meaning)")]
        if nid and kind in NOTE_KINDS and (nid, kind) not in by_obj and self.fm_nid(rel) == nid:
            # WB 5.3.4: a note created by write-back (frontmatter written after POST): adopt, never ADD a 2nd file
            s_bytes = self.staged_bytes(rel, S)
            Ln, Sn = parse_note(l_bytes), parse_note(s_bytes) if s_bytes is not None else None
            if Ln is not None and Sn is not None and \
                    norm_body(Ln.body, rel, self.now_resolver()) == norm_body(Sn.body, rel, self.then_resolver()):
                fm_ok, fm_spelling = self._adopt_fm(Ln, Sn, rel)
                if not fm_ok:
                    return [self._conflict(dict(base, op="CONFLICT_UNTRACKED", cur=L,
                                                reason="same notion_id and body, but exporter-owned frontmatter "
                                                       "(properties) differs from the Notion version"))]
                links = Ln.body != Sn.body or fm_spelling
                acts = [dict(base, op="ADOPT", cur=L, origin="WRITEBACK", reason="file created by write-back (same id, same body)")]
                keys = self.local_keys_vs_staged(l_bytes, s_bytes, None) or []
                merged = merge_local_keys(s_bytes, l_bytes, keys) if keys else None
                if keys and merged is not None:
                    acts.append(dict(base, op="LOCAL_KEYS_ONLY", before=L, keys=keys, expect=sha_bytes(merged),
                                     origin="WRITEBACK", links_rewritten=links,
                                     reason="adopted note: staged version, local keys kept"))
                elif not keys:
                    acts.append(dict(base, op="REPLACE", before=L, expect=S, origin="WRITEBACK", links_rewritten=links,
                                     reason="adopted note: staged version (D-19)"))
                else:
                    acts = [self._conflict(dict(base, op="CONFLICT_UNTRACKED", cur=L,
                                                reason="same notion_id, local keys cannot be merged"))]
                return acts
            return [self._conflict(dict(base, op="CONFLICT_UNTRACKED", cur=L,
                                        reason="same notion_id but body differs from the Notion version"))]
        return [self._conflict(dict(base, op="CONFLICT_UNTRACKED", cur=L,
                                    reason="target exists and was not published by this tool"))]

    def _adopt_fm(self, L, S, rel):
        """(ok, spelling_differs) for adopting an untracked note: every exporter-owned key of S other than the notion_*
        bookkeeping (row properties, aliases) that L carries must have the same value (wikilinks normalised). A key of S
        that L lacks is a local deletion when L was rendered by the exporter (it carries notion_kind); a note whose
        frontmatter write-back wrote (WB 5.3 step 4: notion_id etc.) may lack keys."""
        lf = {str(k): v for k, v in L.fm.items()}
        now_r, then_r = self.now_resolver(), self.then_resolver()
        spelling = False
        for k, v in S.fm.items():
            k = str(k)
            if k.startswith("notion_"):
                continue
            if k not in lf:
                if "notion_kind" in lf:
                    return False, spelling
                continue
            if norm_value(lf[k], rel, now_r) != norm_value(v, rel, then_r):
                return False, spelling
            spelling = spelling or lf[k] != v
        return True, spelling

    def _plan_adopt(self, rel, m, ent, pub, by_obj):
        """--adopt-writeback item (WB 11.4): replace with staged only if the file is still exactly what was written back."""
        nid, kind, S = m.get("notion_id"), m["kind"], m["sha256"]
        base = {"rel": rel, "sha": S, "nid": nid, "kind": kind}
        lrel, l_sha = ent.get("rel") or rel, ent.get("l_sha")
        dst = safe_join(self.vault, rel)

        def manual(op, why, cur=None):
            self.needs_manual.append({"notion_id": nid, "rel": lrel, "staged_rel": rel, "reason": why, "l_sha": l_sha,
                                      "cur": cur})
            return [self._conflict(dict(base, op=op, cur=cur, reason=f"adopt-writeback: {why} (待人工处理)"))]
        if posixpath.normpath(lrel) != rel:
            return manual("CONFLICT_BOTH", "staged path differs from the written-back file")
        if not os.path.lexists(dst):
            return manual("CONFLICT_BOTH", "written-back file is missing")
        if not _is_regular(dst):
            return manual("CONFLICT_NOT_FILE", "written-back path is not a regular file")
        l_bytes = self.vault_bytes(rel)
        cur = sha_bytes(l_bytes)
        if not l_sha or cur != l_sha:
            return manual("CONFLICT_BOTH", "file changed after the write-back apply", cur)
        B = pub.get(rel)
        s_bytes = self.staged_bytes(rel, S)
        keys, merged, clash = [], None, []
        if kind in NOTE_KINDS and s_bytes is not None:
            bs = self.base_bytes(B["staged"], rel) if B and B.get("staged") else None
            keys, merged, clash = self._keys_plan(l_bytes, s_bytes, bs)
        if cur != S and (clash or (keys and merged is None)):
            return manual("CONFLICT_BOTH", "local frontmatter keys cannot be kept next to the Notion version"
                          + (f" ({', '.join(clash)})" if clash else ""), cur)
        acts = []
        if B is None:
            if (nid, kind) in by_obj:
                return manual("CONFLICT_BOTH", "object is published at another path")
            acts.append(dict(base, op="ADOPT", cur=cur, origin="WRITEBACK", reason="adopt-writeback: register new note"))
            if cur == S:
                return acts
            op = "LOCAL_KEYS_ONLY" if keys and merged is not None else "REPLACE"
        else:
            if cur == S:
                return [dict(base, op="UNCHANGED", cur=cur, origin="WRITEBACK", reason="adopt-writeback: vault == staged")]
            op = "ADOPT_WRITEBACK"
        a = dict(base, op=op, before=cur, expect=S, origin="WRITEBACK", reason="adopt-writeback: staged version replaces L")
        if keys and merged is not None:
            a["keys"], a["expect"] = keys, sha_bytes(merged)
        acts.append(a)
        return acts

    # ------------------------------------------------------------ apply
    def apply(self, actions, tomb, approval, plan_sha=None):
        """Execute `actions` in order. A race (vault or staging changed since the plan) stops all remaining actions
        (self.stopped). Returns {"executed": [(action, expect_sha)], "stopped": action|None, "base": stats}."""
        self.open_rw()
        jpath = os.path.join(self.rep_dir, "publish_journal.jsonl")
        journal = open(jpath, "a", encoding="utf-8")

        def log(**kw):
            kw["ts"] = now()
            journal.write(json.dumps(kw, ensure_ascii=False) + "\n")
            journal.flush()
        log(event="begin", run_id=self.run_id, vault=self.vault, approval=approval, n_actions=len(actions),
            plan_sha256=plan_sha, state=self.state, wb_db=self.wb_db, backup_dir=self.bk)
        if self.recovered:
            log(event="recovered", note="writes of an interrupted earlier apply (publish_intents)", items=self.recovered)
        executed, base_queue, self.stopped = [], [], None
        since_commit = 0

        def stop(a, race_op, **kw):
            log(op=race_op, rel=a["rel"], **kw)
            self.stopped = dict(a, race=race_op)
            log(event="stopped", run_id=self.run_id, rel=a["rel"], op=a["op"], race=race_op,
                note="remaining actions not executed (WB 14.3)")

        try:
            for a in actions:
                if self.stopped:
                    break
                op = a["op"]
                rel = a["rel"]
                if op in NOOP_OPS:
                    dst = safe_join(self.vault, rel)
                    if not _is_regular(dst) or sha_file(dst) != a.get("cur"):
                        log(op="SKIP_UNCHANGED_DRIFT", rel=rel, note="vault changed since the plan; not recorded")
                        continue
                    self._record(rel, a["cur"], a, staged=a["sha"])
                    base_queue.append(a)
                    since_commit += 1
                    if since_commit >= 1000:
                        self.db.commit()
                        since_commit = 0
                    continue
                if op in ("LOCAL_ONLY", "SKIP_HELD"):
                    log(op=op, rel=rel, nid=a.get("nid"), reason=a.get("reason"), variant=a.get("variant"))
                    if op == "LOCAL_ONLY" and a.get("variant") != "body":
                        base_queue.append(a)  # seed only an unchanged staged version previously published to this path
                    continue
                if op in CONFLICT_OPS:
                    self._apply_conflict(a, log, stop)
                    continue
                if op == "ADOPT_RENAME":
                    if self._apply_adopt_rename(a, log, stop):
                        base_queue.append(a)
                    continue
                if op == "ADOPT":
                    if self._apply_adopt(a, log, stop):
                        if a.get("cur") == a["sha"]:
                            base_queue.append(a)
                    continue
                if op == "TRASHED_VIA_WRITEBACK":
                    self._apply_trashed(a, log, stop)
                    continue
                if op in WRITE_OPS:
                    res = self._apply_write(a, log, stop)
                    if res is not None:
                        executed.append((a, res))
                        base_queue.append(a)
                    continue
                log(op="UNKNOWN_OP", rel=rel, note=f"not executed: {op}")
            for rel in tomb:
                log(op="TOMBSTONE", rel=rel, note="source object missing; vault file kept")
            self.db.commit()
            base = self.import_base(base_queue, log)
            log(event="end", run_id=self.run_id, stopped=bool(self.stopped))
        finally:
            journal.close()
        return {"executed": executed, "stopped": self.stopped, "base": base}

    def _staged_or_stop(self, a, log, stop):
        try:
            data = self.staged_bytes(a["rel"], a["sha"])
        except OSError:
            data = None
        if data is None:
            stop(a, "SKIP_STAGING_RACE", note="staged file changed or missing since the plan")
        return data

    def _race_dir(self):
        return os.path.join(self.bk, ".race-kept")

    def _apply_write(self, a, log, stop):
        op, rel = a["op"], a["rel"]
        dst = safe_join(self.vault, rel)
        data = self._staged_or_stop(a, log, stop)
        if data is None:
            return None
        backup = None
        moved_frm = None
        if op == "ADD":
            if os.path.lexists(dst):
                stop(a, "SKIP_ADD_RACE")
                return None
            out = data
            db_before = {rel: self._row(rel)}
            self._intent(a, rel, a["expect"], frm=a.get("frm"), staged=a["sha"])
            if a.get("frm"):
                db_before[a["frm"]] = self._row(a["frm"])
                moved_frm = a["frm"]   # its row becomes 'moved' once the new file is written
        elif op == "MOVE":
            frm = a["frm"]
            old = safe_join(self.vault, frm)
            if not _is_regular(old) or sha_file(old) != a["before"] or os.path.lexists(dst):
                stop(a, "SKIP_MOVE_RACE", frm=frm)
                return None
            out = data
            if a.get("keys"):
                with open(old, "rb") as fh:
                    out = merge_local_keys(data, fh.read(), a["keys"])
                if out is None or sha_bytes(out) != a["expect"]:
                    stop(a, "SKIP_MOVE_RACE", frm=frm, note="local keys could not be merged as planned")
                    return None
            backup = wb_common.backup_copy(old, self.bk, frm)
            if sha_file(backup) != a["before"]:
                stop(a, "SKIP_MOVE_RACE", frm=frm, note="old file changed while backing up")
                return None
            db_before = {frm: self._row(frm), rel: self._row(rel)}
            if out is not data:
                self._store(out)
                self._store(data)  # Bs for the next key comparison
            self._intent(a, rel, a["expect"], frm=frm, before=a["before"], staged=a["sha"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                moved = _rename2(old, dst, _RENAME_NOREPLACE)
            except FileExistsError:
                moved = False
            if moved is None:
                if os.path.lexists(dst):
                    moved = False
                else:
                    os.replace(old, dst)
                    moved = True
            if not moved:
                self._intent_done(rel)
                self.db.commit()
                stop(a, "SKIP_MOVE_RACE", frm=frm, note="target appeared during the move; nothing moved")
                return None
            if not _is_regular(dst) or sha_file(dst) != a["before"]:
                # the old file was saved after the backup check: the user's newer bytes moved with it -> put them
                # back at the path the user had, write nothing (r1-data-safety-01)
                if not os.path.lexists(old):
                    os.replace(dst, old)
                self._intent_done(rel)
                self.db.commit()
                stop(a, "SKIP_MOVE_RACE", frm=frm, note="old file changed during the move; kept at its old path")
                return None
            log(op="MOVE", rel=rel, frm=frm, before=a["before"], backup=backup, phase="moved")
            if a["before"] != a["expect"]:
                newer = guarded_replace(dst, out, a["before"], self._race_dir())
                if newer is not None:
                    if not os.path.lexists(old):
                        os.replace(dst, old)   # the newer local version goes back to where the user saved it
                    self._intent_done(rel)
                    self.db.commit()
                    stop(a, "SKIP_MOVE_RACE", frm=frm, vault_sha=newer["vault_sha"], kept=newer["kept"],
                         note="file changed during the write; the newer local version was kept at its old path")
                    return None
            self.db.execute("UPDATE published_files SET status='moved' WHERE rel_path=?", (frm,))
            after = sha_file(dst)
            log(op="MOVE", rel=rel, frm=frm, before=a["before"], after=after, ok=after == a["expect"], phase="done",
                backup=backup, db_before=db_before, nid=a.get("nid"), kind=a.get("kind"), staged=a["sha"])
            self._record(rel, after, a, staged=a["sha"])
            self._intent_done(rel)
            self.db.commit()
            self._prune_empty_dirs(posixpath.dirname(frm), log)
            return a["expect"]
        else:  # REPLACE-like
            if not _is_regular(dst):
                stop(a, "SKIP_REPLACE_RACE")
                return None
            with open(dst, "rb") as fh:
                l_bytes = fh.read()
            if sha_bytes(l_bytes) != a["before"]:
                stop(a, "SKIP_REPLACE_RACE")
                return None
            out = data
            if a.get("keys"):
                out = merge_local_keys(data, l_bytes, a["keys"])
                if out is None or sha_bytes(out) != a["expect"]:
                    stop(a, "SKIP_REPLACE_RACE", note="local keys could not be merged as planned")
                    return None
            backup = wb_common.backup_copy(dst, self.bk, rel)
            if sha_file(backup) != a["before"]:
                stop(a, "SKIP_REPLACE_RACE", note="file changed while backing up")
                return None
            db_before = {rel: self._row(rel)}
            if out is not data:  # Bv and Bs for the next link/keys comparison (before the write: crash-safe)
                self._store(out)
                self._store(data)
            self._intent(a, rel, a["expect"], before=a["before"], staged=a["sha"])
        log(op=op, rel=rel, before=a.get("before"), expect_after=a["expect"], backup=backup, phase="prepare")
        if op == "ADD":
            if not guarded_add(dst, out):
                self._intent_done(rel)
                self.db.commit()
                stop(a, "SKIP_ADD_RACE", note="a file appeared at the target during the write; not overwritten")
                return None
            if moved_frm:
                self.db.execute("UPDATE published_files SET status='moved' WHERE rel_path=? AND status='active'",
                                (moved_frm,))
        else:
            newer = guarded_replace(dst, out, a["before"], self._race_dir())
            if newer is not None:
                self._intent_done(rel)
                self.db.commit()
                stop(a, "SKIP_REPLACE_RACE", vault_sha=newer["vault_sha"], kept=newer["kept"],
                     note="file changed during the write; the newer local version was kept")
                return None
        after = sha_file(dst)
        log(op=op, rel=rel, before=a.get("before"), after=after, ok=after == a["expect"], phase="done", backup=backup,
            db_before=db_before, nid=a.get("nid"), kind=a.get("kind"), staged=a["sha"], origin=a.get("origin"),
            restore=a.get("restore"), frm=a.get("frm"))
        self._record(rel, after, a, staged=a["sha"])
        self._intent_done(rel)
        self.db.commit()
        return a["expect"]

    def _prune_empty_dirs(self, d, log):
        """After a MOVE: remove the source folder (and its parents) while they are EMPTY directories inside a
        teamspace folder (r1-n2o-completeness-09: a Notion folder rename / folder page turned leaf left ghost
        folders that showed in Obsidian and became 'user folders' in the scan). Never a file, never the vault root,
        a teamspace folder or anything under Notion_Obsidian/ or a hidden name."""
        while d and "/" in d:   # a teamspace folder itself ("General") has no '/'
            if d.startswith(TASK_DIR + "/") or any(part.startswith(".") for part in d.split("/")):
                return
            try:
                full = safe_join(self.vault, d)
            except SystemExit:
                return
            if os.path.islink(full) or not os.path.isdir(full):
                return
            try:
                os.rmdir(full)   # fails (not empty) as soon as anything is in it
            except OSError:
                return
            log(op="RMDIR_EMPTY", rel=d, note="source folder left empty by a MOVE (rollback re-creates it)", phase="rmdir")
            d = posixpath.dirname(d)

    def _store(self, data):
        try:
            wb_state.store_content(data, base_dir=self.base_dir)
        except OSError:
            pass

    def _apply_conflict(self, a, log, stop):
        op, rel = a["op"], a["rel"]
        row = self._conflict_row(a["obj_key"], a["kind"], a["sha"])
        if a.get("dedup") or row:
            log(op=op, rel=rel, frm=a.get("frm"), dedup=True, conflict_rel=row[0] if row else a.get("conflict_rel"),
                note="conflict copy for this (object, staged sha) already exists; not written again")
            return
        data = self._staged_or_stop(a, log, stop)
        if data is None:
            return
        content = self.conflict_content(a, data)
        csha = sha_bytes(content)
        crel = a["conflict_rel"]
        cdst = safe_join(self.vault, crel)
        if os.path.lexists(cdst):
            if not (_is_regular(cdst) and sha_file(cdst) == csha):
                stop(a, "SKIP_CONFLICT_RACE", conflict_rel=crel, note="conflict copy path exists with other content")
                return
            written = False
        else:
            self._intent(a, crel, csha, frm=rel, before=a.get("cur"), staged=a["sha"], obj_key=a["obj_key"])
            atomic_write(cdst, content)
            written = True
        log(op=op, rel=rel, frm=a.get("frm"), vault_sha=a.get("cur"), notion_version=crel, conflict_rel=crel,
            conflict_sha=csha, obj_key=a["obj_key"], kind=a["kind"], staged=a["sha"], written=written,
            reason=a.get("reason"), phase="done")
        self.db.execute("INSERT OR REPLACE INTO conflict_copies(obj_key, kind, staged_sha, conflict_rel, conflict_sha,"
                        " vault_rel, vault_sha, op, run_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (a["obj_key"], a["kind"], a["sha"], crel, csha, rel, a.get("cur"), op, self.run_id, now()))
        self._intent_done(crel)
        self.db.commit()

    def _apply_adopt_rename(self, a, log, stop):
        rel, frm = a["rel"], a["frm"]
        new_abs, old_abs = safe_join(self.vault, rel), safe_join(self.vault, frm)
        if os.path.lexists(old_abs) or not _is_regular(new_abs) or sha_file(new_abs) != a["cur"]:
            stop(a, "SKIP_ADOPT_RENAME_RACE", frm=frm)
            return False
        old_row = self._row(frm)
        if not old_row or old_row["status"] != "active" or old_row["notion_id"] != a["nid"]:
            stop(a, "SKIP_ADOPT_RENAME_RACE", frm=frm, note="published_files changed since the plan")
            return False
        db_before = {frm: old_row, rel: self._row(rel)}
        self.db.execute("UPDATE published_files SET status='renamed' WHERE rel_path=?", (frm,))
        self.db.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status,"
                        " staged_sha256) VALUES(?,?,?,?,?,'active',?)",
                        (rel, old_row["sha256"], a["nid"], a["kind"], self.run_id, old_row["staged_sha256"]))
        if a["kind"] in OBJ_MAP_KINDS and self._has_table("objects_map"):
            self.db.execute("UPDATE objects_map SET published_path=? WHERE notion_id=?", (rel, a["nid"]))
        self.db.commit()
        log(op="ADOPT_RENAME", rel=rel, frm=frm, sha=old_row["sha256"], after=a["cur"], phase="done",
            db_before=db_before, nid=a["nid"], kind=a["kind"], staged=old_row["staged_sha256"] or old_row["sha256"])
        return True

    def _apply_adopt(self, a, log, stop):
        rel = a["rel"]
        dst = safe_join(self.vault, rel)
        if not _is_regular(dst) or sha_file(dst) != a["cur"]:
            stop(a, "SKIP_ADOPT_RACE")
            return False
        before = self._row(rel)
        if before and before["status"] == "active":
            stop(a, "SKIP_ADOPT_RACE", note="published_files changed since the plan")
            return False
        staged = a["sha"] if a["cur"] == a["sha"] else None  # D-19: staged NULL until the replace
        self._record(rel, a["cur"], a, staged=staged)
        try:
            wb_state.store_content(self.vault_bytes(rel), base_dir=self.base_dir)
        except (OSError, wb_state.BaseCorrupt):
            pass
        self.db.commit()
        log(op="ADOPT", rel=rel, sha=a["cur"], after=a["cur"], phase="done", db_before={rel: before}, nid=a.get("nid"),
            kind=a.get("kind"), staged=staged, origin=a.get("origin"))
        return True

    def _apply_trashed(self, a, log, stop):
        rel = a["rel"]
        if os.path.lexists(safe_join(self.vault, rel)):
            stop(a, "SKIP_TRASHED_RACE", note="file reappeared")
            return
        before = self._row(rel)
        om_before = None
        if self._has_table("objects_map"):
            r = self.db.execute("SELECT status FROM objects_map WHERE notion_id=?", (a["nid"],)).fetchone()
            om_before = r[0] if r else None
            self.db.execute("UPDATE objects_map SET status='trashed_via_writeback' WHERE notion_id=?", (a["nid"],))
        self.db.execute("UPDATE published_files SET status='trashed_via_writeback' WHERE rel_path=?", (rel,))
        self.db.commit()
        log(op="TRASHED_VIA_WRITEBACK", rel=rel, nid=a["nid"], phase="done", db_before={rel: before},
            objects_map_status_before=om_before)

    def _record(self, rel, sha, a, staged=None):
        self.db.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status,"
                        " staged_sha256) VALUES(?,?,?,?,?,'active',?)",
                        (rel, sha, a.get("nid"), a.get("kind"), self.run_id, staged))
        if a.get("nid") and a.get("kind") in OBJ_MAP_KINDS and self._has_table("objects_map"):
            self.db.execute("UPDATE objects_map SET published_sha256=?, published_path=?, published_run=? WHERE notion_id=?",
                            (sha, rel, self.run_id, a["nid"]))

    # ------------------------------------------------------------ base_history + block_lines (D-06, D-09)
    def import_base(self, queue, log=None):
        """Append base_history (content = staged bytes) and import block_lines for every published/confirmed
        (notion_id, staged sha), all in ONE writeback.db transaction. Notes only (page_json.jsonl rows).
        LOCAL_ONLY (Notion unchanged: S is the version last published) appends only when the object has no row yet
        (seed) or its latest row has another path or sha. ADOPT_RENAME appends the latest row again under the new
        path (same sha, so its block_lines are already there). Without page_json.jsonl or block_lines.jsonl no
        S-based row is appended (a base row always comes with its block_lines)."""
        stats = {"candidates": 0, "appended": 0, "seeded": 0, "block_lines_notes": 0, "block_lines_rows": 0,
                 "skipped": 0, "error": None}
        cand = [a for a in queue if a.get("nid") and a["op"] in BASE_OPS and a.get("kind") in NOTE_KINDS]
        stats["candidates"] = len(cand)
        if not cand:
            return stats
        bl = os.path.join(self.stage, "block_lines.jsonl")
        missing = None
        if self.page_json is None:
            missing = "staging has no page_json.jsonl: base_history not appended"
        elif not os.path.isfile(bl):
            missing = "staging has no block_lines.jsonl: base_history not appended"
        if missing:
            stats["note"] = missing
            stats["skipped"] = sum(1 for a in cand if a["op"] != "ADOPT_RENAME")
            cand = [a for a in cand if a["op"] == "ADOPT_RENAME"]
            if not cand:
                if log:
                    log(event="base_history", **stats)
                return stats
        try:
            wbdb = wb_state.connect(self.wb_db)
        except Exception as exc:  # noqa: BLE001
            stats["error"] = f"writeback.db: {exc}"
            return stats
        appended = set()
        try:
            with wb_state.transaction(wbdb):
                for a in cand:
                    rel, nid, S = a["rel"], a["nid"], a["sha"]
                    last = wb_state.latest_base(wbdb, nid)
                    if a["op"] == "ADOPT_RENAME":
                        if self._append_rename(wbdb, a, last):
                            stats["appended"] += 1
                        continue
                    pj = self.page_json.get(rel)
                    if not pj or pj.get("notion_id") != nid or pj.get("sha256") != S:
                        stats["skipped"] += 1
                        continue
                    if a["op"] == "LOCAL_ONLY" and last and (last["rel_path"], last["sha256"]) == (rel, S):
                        continue
                    key = (rel, S, pj.get("page_json_sha"), pj.get("renderer_version"))
                    if last and (last["rel_path"], last["sha256"], last["page_json_sha"], last["renderer_version"]) == key:
                        continue
                    content = self.staged_bytes(rel, S)
                    if content is None:
                        raise RuntimeError(f"staged file changed: {rel}")
                    seq = wb_state.append_base(wbdb, notion_id=nid, rel_path=rel, sha=S,
                                               page_json_sha=pj.get("page_json_sha"),
                                               renderer_version=pj.get("renderer_version"), run_id=self.run_id,
                                               origin="WRITEBACK" if a.get("origin") == "WRITEBACK" else "REMOTE",
                                               content=content, seed=1 if last is None else 0)
                    if seq is not None:
                        stats["appended"] += 1
                        stats["seeded"] += 1 if last is None else 0
                        appended.add((nid, S))
                if appended:
                    for (nid, sha), rows in iter_block_line_groups(bl, appended):
                        wb_state.import_block_lines(wbdb, nid, sha, rows)
                        stats["block_lines_notes"] += 1
                        stats["block_lines_rows"] += len(rows)
        except Exception as exc:  # noqa: BLE001 - the transaction rolled back; files are published already
            stats["error"] = f"{type(exc).__name__}: {exc}"
            stats["appended"] = stats["seeded"] = stats["block_lines_notes"] = stats["block_lines_rows"] = 0
        finally:
            wbdb.close()
        if log:
            log(event="base_history", **stats)
        return stats

    def _append_rename(self, wbdb, a, last):
        """ADOPT_RENAME (WB §3): the base version is unchanged, only its path; appended only while the latest row is
        the version published_files had (a later classification of the same file appends / seeds otherwise)."""
        want = a.get("staged") or a.get("sha")
        if not last or last["sha256"] != want or last["rel_path"] == a["rel"]:
            return False
        try:
            content = wb_state.base_content(last["sha256"], base_dir=self.base_dir)
        except (wb_state.BaseCorrupt, ValueError):
            content = None
        if content is None:
            return False
        return wb_state.append_base(wbdb, notion_id=a["nid"], rel_path=a["rel"], sha=last["sha256"],
                                    page_json_sha=last["page_json_sha"], renderer_version=last["renderer_version"],
                                    run_id=self.run_id, origin=last["origin"], content=content, seed=0) is not None


_BL_PREFIX = '{"notion_id":"'
_BL_MID = '","base_sha":"'


def iter_block_line_groups(path, wanted):
    """Yield ((notion_id, base_sha), rows) for the wanted keys of a block_lines.jsonl (rows are grouped per note);
    only the wanted rows are JSON-decoded. A key that re-appears after its group ended is an error."""
    cur_key, rows, done = None, [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if line.startswith(_BL_PREFIX) and line[50:64] == _BL_MID:
                key = (line[14:50], line[64:128])
            else:
                r = json.loads(line)
                key = (r.get("notion_id"), r.get("base_sha"))
            if key != cur_key:
                if cur_key in wanted:
                    yield cur_key, rows
                    done.add(cur_key)
                if key in done:
                    raise ValueError(f"block_lines.jsonl: rows of {key[0]} are not contiguous")
                cur_key, rows = key, []
            if key in wanted:
                rows.append(json.loads(line))
    if cur_key in wanted:
        yield cur_key, rows


def backup_tar(vault, run_id):
    d = os.path.join(WORK, "backup", "vault-before")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"{run_id}.tar")
    if os.path.exists(out):
        return out
    with tarfile.open(out + ".part", "w") as tf:
        tf.add(vault, arcname=os.path.basename(vault))
    os.replace(out + ".part", out)
    with open(out + ".sha256", "w") as fh:
        fh.write(f"{sha_file(out)}  {os.path.basename(out)}\n")
    return out


def count_ops(actions):
    counts = {}
    for x in actions:
        counts[x["op"]] = counts.get(x["op"], 0) + 1
    return counts


def build_plan(p, actions, tomb, inputs):
    plan = {"run_id": p.run_id, "vault": p.vault, "counts": count_ops(actions), "tombstones": tomb,
            "actions": [{k: v for k, v in x.items() if k != "src"} for x in actions],
            "state_fingerprint": p.state_fingerprint(), "inputs": inputs, "notes": p.notes,
            "adopt_needs_manual": p.needs_manual}
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def load_plan_file(path, want_sha, p):
    """Read a plan written by the plan step; refuse unless it is exactly the approved one and matches this run."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        plan = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"--plan-file unreadable: {exc}")
    if not isinstance(plan, dict) or not isinstance(plan.get("actions"), list):
        raise Refused("--plan-file is not a publish plan")
    canon = plan_sha256(plan)
    if plan.get("plan_sha256") != canon:
        raise Refused("--plan-file content does not match its embedded plan_sha256 (modified after planning)")
    if want_sha not in (canon, sha_bytes(raw)):
        raise Refused(f"--plan-sha mismatch: plan is {canon}, got {want_sha}")
    if plan.get("run_id") != p.run_id:
        raise Refused(f"--plan-file is for run {plan.get('run_id')}, not {p.run_id}")
    if plan.get("vault") != p.vault:
        raise Refused(f"--plan-file is for vault {plan.get('vault')}, not {p.vault}")
    if plan.get("state_fingerprint") != p.state_fingerprint():
        raise Refused("published state (export.db) changed since the plan; re-plan")
    for a in plan["actions"]:
        if a.get("rel") not in p.manifest and a.get("op") not in ("TRASHED_VIA_WRITEBACK",):
            raise Refused(f"--plan-file action for a path not in staging: {a.get('rel')}")
    return plan


def _rel_or_abs(path):
    rel = os.path.relpath(os.path.abspath(path), WORK)
    return os.path.abspath(path) if rel.startswith("..") else rel


def load_runlog_notes(path):
    """--runlog-notes: [{key, title, lines[, hint]}] sections the caller (refresh.py) wants in the vault run log
    (G-refresh.md request to F, L-13: new top-level pages not shared, member / block-limit warning, sidebar capture
    failure, ...). `key` is the note's standing identity (L-12 digest)."""
    data = load_json_file(path, "--runlog-notes")
    if not isinstance(data, list):
        raise Refused("--runlog-notes must be a JSON list")
    out = []
    for n in data:
        if not isinstance(n, dict) or not isinstance(n.get("title"), str) or not isinstance(n.get("lines", []), list) \
                or not all(isinstance(x, str) for x in n.get("lines", [])):
            raise Refused("--runlog-notes entries need a string title and a list of string lines")
        out.append({"key": str(n.get("key") or n["title"]), "title": n["title"], "lines": list(n.get("lines", [])),
                    "hint": n.get("hint") if isinstance(n.get("hint"), str) else None})
    return out


def _nid32(nid):
    return str(nid).replace("-", "")


def _notion_link(nid):
    """'（[打开 Notion](url)）' for an entry with a notion_id (R-093 / WB 5.1a: inline-code path plus Notion link)."""
    return f"（[打开 Notion]({NOTION_URL.format(_nid32(nid))})）" if nid else ""


def write_runlog(vault, run_id, counts, tomb, bad, p, actions=None, result=None, runlog_notes=None):
    """Notion_Obsidian/运行记录/<run_id>.md (ADD-only). Counts plus sections with inline-code paths (no wikilinks) and,
    for objects, a Notion link. Not written for a run in which nothing happened (only UNCHANGED / UNCHANGED_SEMANTIC,
    nothing else to report). `runlog_notes` (--runlog-notes) are standing warnings of the caller: they make a round
    eventful and take part in the L-12 standing digest (an unchanged warning is not repeated every round)."""
    actions = actions or []
    result = result or {}
    notes_in = list(runlog_notes or [])
    stopped = result.get("stopped")
    base = result.get("base") or {}
    recovered = [n for n in (p.notes or []) if n.get("type") == "recovered"]
    eventful = any(a["op"] not in NOOP_OPS for a in actions) or tomb or bad or stopped or base.get("error") \
        or p.needs_manual or recovered or notes_in
    if not eventful:
        return None
    rel = f"{TASK_DIR}/运行记录/{run_id}.md"
    dst = safe_join(vault, rel)
    if os.path.lexists(dst):
        return None
    # L-12 (lead decision 2026-11-18): a round whose only non-no-op items are STANDING ones (pending LOCAL_ONLY,
    # holds, already-copied conflicts, tombstones, overrides in effect) writes a vault run log only when that
    # standing set differs from the one in the last run log still present in the vault. Otherwise every 6-hourly
    # round would add a note while a local edit is pending or a tombstone exists. reports/<run>/ keeps the details.
    def _standing(a):
        return a["op"] in STANDING_OPS or (a["op"] in CONFLICT_OPS and a.get("dedup"))
    standing_only = (all(_standing(a) for a in actions if a["op"] not in NOOP_OPS)
                     and not (bad or stopped or base.get("error") or p.needs_manual or recovered))
    digest_src = {
        "actions": sorted([a["op"], a["rel"], str(a.get("variant") or ""), str(a.get("reason") or ""),
                           bool(a.get("dedup"))] for a in actions if a["op"] not in NOOP_OPS and _standing(a)),
        "tomb": sorted(tomb),
        # an override adopted this round and one in effect are the same standing fact (the next round reports
        # "in_effect" for the rename this round adopted), so they share one digest entry
        "names": sorted([("in_effect" if n.get("state") == "adopted" else n.get("state")), n.get("local_rel"),
                         n.get("reason"), n.get("notion_renamed")]
                        for n in (p.notes or []) if n.get("type") == "override"),
        "notes": sorted(n["key"] for n in notes_in),
    }
    digest = hashlib.sha256(json.dumps(digest_src, ensure_ascii=False, sort_keys=True, default=str)
                            .encode("utf-8")).hexdigest()
    digest_file = os.path.join(os.path.dirname(p.state), "runlog_standing_digest.json")
    if standing_only:
        try:
            with open(digest_file, encoding="utf-8") as fh:
                last = json.load(fh)
        except (OSError, ValueError):
            last = {}
        if not isinstance(last, dict):  # valid JSON but not ours ([], null, "x"): costs one extra run log only
            last = {}
        if last.get("digest") == digest and isinstance(last.get("rel"), str) \
                and os.path.isfile(safe_join(vault, last["rel"])):
            result["runlog_skipped"] = {"reason": "standing items unchanged", "same_as": last["rel"]}
            return None
    by = {}
    for a in actions:
        by.setdefault(a["op"], []).append(a)
    conflicts = [a for a in actions if a["op"] in CONFLICT_OPS]
    new_conf = [a for a in conflicts if not a.get("dedup")]
    lines = ["---", "notion_kind: generated", "generated_by: publish_vault.py", f"run_id: \"{run_id}\"",
             f"published_at: \"{now()}\"", "---", "",
             f"# 运行记录 {run_id}", "",
             "| 动作 | 数量 |", "| --- | --- |"]
    for k in sorted(counts):
        lines.append(f"| {k} | {counts[k]} |")
    lines += ["", f"- 源端已不存在、库内保留未删的文件（tombstone）：{len(tomb)}",
              f"- 冲突（库内文件保持不动，Notion 版本写入 `{TASK_DIR}/conflicts/{run_id}/`）：{len(new_conf)}"
              + (f"（另有 {len(conflicts) - len(new_conf)} 项此前已写过冲突副本，本轮不再重复）" if len(conflicts) > len(new_conf) else ""),
              f"- 发布后回读校验不一致：{len(bad)}",
              f"- 明细：库外 `{_rel_or_abs(p.rep_dir)}/publish_plan.json`、`publish_journal.jsonl`"]
    if stopped:
        lines.append(f"- 本轮因覆盖风险提前停止（{stopped.get('race')}）于 {inline_code(stopped['rel'])}，其后的动作未执行")
    if base.get("error"):
        lines.append(f"- base_history 导入失败（库内文件已发布）：{base['error'][:200]}")
    if recovered:
        lines.append(f"- 上一轮中断前已写入但未登记的文件：{sum(1 for n in recovered if n.get('recorded'))} 项已补登记，"
                     f"{sum(1 for n in recovered if not n.get('recorded'))} 项未写入或已被改动（按本轮分类处理）")
    lines.append("")

    def section(title, items, fmt, intro=None):
        if not items:
            return
        lines.extend([f"## {title}", "", intro or f"共 {len(items)} 项：", ""])
        for a in items[:RUNLOG_LIST_MAX]:
            lines.append("- " + fmt(a))
        if len(items) > RUNLOG_LIST_MAX:
            lines.append(f"- …另有 {len(items) - RUNLOG_LIST_MAX} 项，见 publish_plan.json")
        lines.append("")
    variants = {"links_only": "仅链接写法", "keys_only": "仅本地 frontmatter 键", "keys_and_links": "本地键与链接写法",
                "format_only": "仅 frontmatter 格式", "content": "正文或属性修改", "base": ".base 视图修改", "unknown": "未知"}
    for n in notes_in:   # the caller's warnings first: they ask the user to act
        lines.extend([f"## {n['title']}", ""] + [f"- {x}" for x in n["lines"][:RUNLOG_LIST_MAX]] +
                     ([f"", f"处理：{n['hint']}"] if n.get("hint") else []) + [""])
    pub_nid = {r: m.get("nid") for r, m in (p.published_all() or {}).items()} if tomb else {}
    section("待写回的本地修改 (LOCAL_ONLY)", by.get("LOCAL_ONLY", []),
            lambda a: f"{inline_code(a['rel'])}（{variants.get(a.get('variant'), '本地修改')}）{_notion_link(a.get('nid'))}",
            f"共 {len(by.get('LOCAL_ONLY', []))} 项（库内修改原样保留，未覆盖，未写冲突副本）：")
    names = []
    for n in p.notes:
        if n.get("type") != "override":
            continue
        both = f"；双方改名冲突：Notion 名称现为 {inline_code(n['notion_renamed'])}" if n.get("notion_renamed") else ""
        lk = _notion_link(n.get("nid"))
        if n["state"] == "adopted":
            names.append(f"{inline_code(n['old_rel'])} → {inline_code(n['local_rel'])}（本轮采纳 ADOPT_RENAME{both}）{lk}")
        elif n["state"] == "in_effect":
            names.append(f"{inline_code(n['local_rel'])}（命名差异生效中{both}）{lk}")
        elif n["state"] == "not_applied":
            names.append(f"{inline_code(n['local_rel'])}（未生效：{n.get('reason')}；该对象本轮跳过）{lk}")
        elif n["state"] == "skipped":
            names.append(f"{inline_code(n['local_rel'])}（未采纳：{n.get('reason')}；该对象本轮跳过）{lk}")
    if names:
        lines.extend(["## 命名差异", "", f"共 {len(names)} 项：", ""] + [f"- {x}" for x in names[:RUNLOG_LIST_MAX]] + [""])
    section("跳过（暂停/保留）", by.get("SKIP_HELD", []),
            lambda a: f"{inline_code(a['rel'])}：{a.get('reason')}{_notion_link(a.get('nid'))}")
    section("冲突", conflicts, lambda a: f"{inline_code(a['rel'])} → {inline_code(a.get('conflict_rel'))}"
            + ("（已有冲突副本，未重复写）" if a.get("dedup") else "") + _notion_link(a.get("nid")))
    section("按 Notion 版本恢复（库内缺失）", [a for a in by.get("ADD", []) if a.get("restore")],
            lambda a: inline_code(a["rel"]) + _notion_link(a.get("nid")))
    section("链接写法或本地键差异（已备份后按 Notion 版本更新）",
            by.get("LINK_REWRITE_ONLY", []) + by.get("LOCAL_KEYS_ONLY", []),
            lambda a: f"{inline_code(a['rel'])}（{a['op']}）{_notion_link(a.get('nid'))}")
    section("工具生成文件被本地修改（已备份后覆盖）", by.get("GENERATED_OVERWRITE", []), lambda a: inline_code(a["rel"]))
    section("采纳写回结果", by.get("ADOPT", []) + by.get("ADOPT_WRITEBACK", []),
            lambda a: f"{inline_code(a['rel'])}（{a['op']}）{_notion_link(a.get('nid'))}")
    if p.needs_manual:
        section("待人工处理（写回采纳）", p.needs_manual,
                lambda e: f"{inline_code(e.get('rel') or e.get('staged_rel') or e['notion_id'])}：{e['reason']}"
                          f"{_notion_link(e.get('notion_id'))}")
    section("已在 Notion 移入回收站（写回）", by.get("TRASHED_VIA_WRITEBACK", []),
            lambda a: inline_code(a["rel"]) + _notion_link(a.get("nid")))
    section("源端已不存在（tombstone，库内文件保留）", [{"rel": r, "nid": pub_nid.get(r)} for r in tomb],
            lambda a: inline_code(a["rel"]) + _notion_link(a.get("nid")))
    data = "\n".join(lines).rstrip("\n").encode("utf-8") + b"\n"
    atomic_write(dst, data)
    sha = sha_bytes(data)
    with open(os.path.join(p.rep_dir, "publish_journal.jsonl"), "a", encoding="utf-8") as j:  # rollback sees it
        j.write(json.dumps({"op": "ADD", "rel": rel, "before": None, "after": sha, "ok": True, "phase": "done",
                            "kind": "runlog", "db_before": {rel: None}, "ts": now()}, ensure_ascii=False) + "\n")
    p.open_rw()
    p.db.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status, staged_sha256)"
                 " VALUES(?,?,?,?,?,'active',?)", (rel, sha, None, "runlog", run_id, sha))
    p.db.commit()
    try:  # tmp + fsync + os.replace (a torn digest costs one extra run log, never a failed publish)
        wb_common.atomic_write(digest_file, json.dumps({"digest": digest, "rel": rel, "run_id": run_id},
                                                       ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # only costs one extra run log next round
    return rel


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--state", default=os.path.join(WORK, "state", "export.db"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approval", default=None, help="approval record (required with --apply)")
    ap.add_argument("--extra", default=None, help="JSON {rel: {sha256, kind, src}} of additional whitelisted files (.obsidian plugin)")
    ap.add_argument("--full-backup", action="store_true", help="tar the whole vault before applying")
    ap.add_argument("--allow-other-vault", action="store_true", help="for rehearsal copies only")
    ap.add_argument("--report-dir", default=None, help="where plan/journal go (rehearsals use their own dir)")
    ap.add_argument("--staging-dir", default=None, help="staging run dir (default WORK/staging/<run_id>)")
    ap.add_argument("--backup-dir", default=None, help="per-file backups (default WORK/backup/vault-before/<run_id>)")
    ap.add_argument("--holds", default=None, help="JSON of held notion_ids (ADD/REPLACE/MOVE skipped, incl. subtree)")
    ap.add_argument("--overrides", default=None, help="JSON {notion_id: local_rel}: the same file the exporter got")
    ap.add_argument("--plan-file", default=None, help="apply exactly this publish_plan.json (needs --plan-sha)")
    ap.add_argument("--plan-sha", default=None, help="the plan's plan_sha256 (or the sha256 of the plan file)")
    ap.add_argument("--only-ids", default=None, help="file with notion_ids (one per line): publish only these objects")
    ap.add_argument("--adopt-writeback", default=None, help="JSON {notion_id: {rel, l_sha}} from a write-back apply")
    ap.add_argument("--wb-db", default=None, help="writeback.db (default: writeback.db next to --state)")
    ap.add_argument("--runlog-notes", default=None,
                    help="JSON [{key, title, lines}] of warnings for the vault run log (refresh.py: new top-level pages "
                         "not shared, member/block-limit warning, sidebar capture failure, ...); used with --apply")
    ap.add_argument("--take-refresh-lock", action="store_true",
                    help="hold state/refresh.lock (next to --state) during the apply; for manual runs outside refresh.py")
    a = ap.parse_args(argv)
    vault = os.path.realpath(a.vault)
    if vault != EXPECTED_VAULT and not a.allow_other_vault:
        sys.exit(f"ABORT: vault must be {EXPECTED_VAULT} (got {vault})")
    if not os.path.isdir(vault) or os.path.islink(a.vault):
        sys.exit("ABORT: vault root missing or a symlink")
    try:
        if a.plan_file and not (a.apply and a.plan_sha):
            raise Refused("--plan-file needs --apply and --plan-sha")
        if a.plan_sha and not a.plan_file:
            raise Refused("--plan-sha needs --plan-file")
        extra = load_json_file(a.extra, "--extra") if a.extra else None
        holds = load_holds(a.holds) if a.holds else None
        overrides = load_overrides(a.overrides) if a.overrides else None
        only_ids = load_only_ids(a.only_ids) if a.only_ids else None
        adopt = load_adopt(a.adopt_writeback) if a.adopt_writeback else None
        runlog_notes = load_runlog_notes(a.runlog_notes) if a.runlog_notes else None
        if adopt is not None and only_ids is not None and set(adopt) - only_ids:
            raise Refused("--adopt-writeback ids missing from --only-ids (they would be silently skipped): "
                          + ", ".join(sorted(set(adopt) - only_ids)[:5]))
        p = Publisher(a.run_id, vault, a.state, extra, auto=False, report_dir=a.report_dir, staging_dir=a.staging_dir,
                      backup_dir=a.backup_dir, wb_db=a.wb_db, holds=holds, overrides=overrides, only_ids=only_ids,
                      adopt=adopt)
        if a.plan_file:
            plan = load_plan_file(a.plan_file, a.plan_sha, p)
            actions, tomb = plan["actions"], plan["tombstones"]
            p.notes = plan.get("notes") or []
            p.needs_manual = plan.get("adopt_needs_manual") or []
        else:
            actions, tomb = p.plan()
            inputs = {"holds": len(holds or {}), "overrides": len(overrides or {}),
                      "only_ids": None if only_ids is None else len(only_ids), "adopt": None if adopt is None else len(adopt),
                      "override_rejected": sorted(p.override_rejected)}
            plan = build_plan(p, actions, tomb, inputs)
            plan_path = os.path.join(p.rep_dir, "publish_plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, ensure_ascii=False, indent=0)
            if p.needs_manual:
                with open(os.path.join(p.rep_dir, "adopt_needs_manual.json"), "w", encoding="utf-8") as fh:
                    json.dump(p.needs_manual, fh, ensure_ascii=False, indent=1)
            print(json.dumps({"plan": plan_path, "plan_sha256": plan["plan_sha256"], "counts": plan["counts"],
                              "tombstones": len(tomb)}, ensure_ascii=False))
            if not a.apply:
                return 0
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if not a.approval or not os.path.exists(a.approval):
        sys.exit("ABORT: --apply requires an existing --approval record")
    if a.full_backup:
        print("backup:", backup_tar(vault, a.run_id))
    try:
        appr = os.path.relpath(a.approval, WORK)
    except ValueError:
        appr = a.approval
    lock = contextlib.nullcontext()
    if a.take_refresh_lock:
        lock = wb_locks.lock("refresh", state_dir=os.path.dirname(os.path.abspath(a.state)))
    try:
        with lock:
            result = p.apply(actions, tomb, appr, plan_sha=plan["plan_sha256"])
    except wb_locks.LockBusy as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    bad = []
    for x, expect in result["executed"]:
        f = safe_join(vault, x["rel"])
        if not os.path.exists(f) or sha_file(f) != expect:
            bad.append(x["rel"])
    stopped = result["stopped"]
    print(json.dumps({"applied": True, "post_check_mismatch": len(bad), "examples": bad[:5],
                      "stopped": None if not stopped else {"rel": stopped["rel"], "op": stopped["op"], "race": stopped["race"]},
                      "base_history": result["base"]}, ensure_ascii=False))
    try:  # the files are applied: a run-log problem is reported, it never changes the publish's exit status
        write_runlog(vault, a.run_id, plan["counts"], tomb, bad, p, actions, result, runlog_notes)
    except Exception as exc:  # noqa: BLE001 (r1-data-safety-06)
        print(f"WARNING: vault run log not written: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            with open(os.path.join(p.rep_dir, "publish_journal.jsonl"), "a", encoding="utf-8") as j:
                j.write(json.dumps({"event": "runlog_failed", "error": f"{type(exc).__name__}: {exc}"[:500],
                                    "ts": now()}, ensure_ascii=False) + "\n")
        except OSError:
            pass
    if bad:
        return 3
    if stopped:
        return 4
    if (result["base"] or {}).get("error"):
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
