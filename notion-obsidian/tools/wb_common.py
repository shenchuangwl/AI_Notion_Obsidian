#!/usr/bin/env python3
"""Shared helpers for the Notion <-> Obsidian write-back tools (IMPL_CONTRACT §5.1).

Local filesystem and serialization helpers, including read-only SQLite snapshot connections.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sqlite3
import stat

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_VAULT = "/data120/Obsidian/Godcreate"
STATE = os.path.join(WORK, "state")
REPORTS = os.path.join(WORK, "reports")
CONFIG_DIR = os.path.join(WORK, "config")
STAGING = os.path.join(WORK, "staging")
SNAPSHOTS = os.path.join(WORK, "snapshots")
BACKUP = os.path.join(WORK, "backup")
APPROVALS = os.path.join(WORK, "approvals")

# Preserve the selected workspace when a CLI suggests another command, including fake-service rehearsals.
_cli_context = ContextVar("cli_context", default=(None, None, ()))


class SnapshotReadError(OSError):
    """A snapshot cannot be opened safely for a read-only operation."""


def snapshot_sidecars_with_data(path):
    """Return WAL and rollback-journal paths with data, following the snapshot's real path."""
    db = os.path.realpath(path)
    busy = []
    for suffix in ("-wal", "-journal"):
        side = db + suffix
        try:
            info = os.stat(side)
        except FileNotFoundError:
            if os.path.lexists(side):
                raise SnapshotReadError(f"{side} is a dangling symlink; check the snapshot and its sidecars")
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotReadError(f"{side} is not a regular SQLite sidecar; check the snapshot and its sidecars")
        if info.st_size > 0:
            busy.append(side)
    return busy


def snapshot_signature(path):
    """Identify the database and data-bearing sidecars across an unlocked immutable read."""
    db = os.path.realpath(path)
    signature = []
    for suffix in ("", "-wal", "-journal"):
        try:
            info = os.stat(db + suffix)
        except FileNotFoundError:
            continue
        signature.append((suffix, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(signature)


def _snapshot_uri(db, immutable):
    mode = "mode=ro&immutable=1" if immutable else "mode=ro"
    return db.as_uri() + "?" + mode


def snapshot_read_uri(path, *, allow_immutable=True):
    """A read-only SQLite URI. Only WAL or rollback-journal data prevent immutable reads."""
    db = Path(path).resolve()
    return _snapshot_uri(db, allow_immutable and not snapshot_sidecars_with_data(db))


def open_snapshot_reader(path, *, require_finished=False, error_type=SnapshotReadError, **options):
    """Return (connection, mode) without creating sidecars or hiding committed WAL frames."""
    db = Path(path).resolve()
    try:
        busy = snapshot_sidecars_with_data(db)
    except OSError as exc:
        raise error_type(f"Could not inspect SQLite sidecars for {db}: {exc}; check directory permissions") from exc

    if busy:
        sidecars = ", ".join(busy)
        if require_finished or str(db) + "-journal" in busy:
            raise error_type(f"{sidecars} is not empty; use a finished, checkpointed snapshot after the "
                             "writer has closed and SQLite recovery has completed")
        if not os.path.isfile(str(db) + "-shm"):
            raise error_type(f"{db}: non-empty -wal needs an existing -shm index. Use a checkpointed "
                             "snapshot from the writer so this read creates no files")
    uri = _snapshot_uri(db, not busy)
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, **options)
        if busy:
            # SQLite opens the WAL/index at the first schema read, not at connect().
            con.execute("PRAGMA schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        if con is not None:
            con.close()
        if not db.exists():
            raise
        sidecars = ", ".join(busy) if busy else "no WAL or rollback-journal data"
        raise error_type(f"Cannot open snapshot {db} read-only ({sidecars}): {exc}. Check read/search "
                         "permissions on the directory and files, or use a finished, checkpointed snapshot") from exc
    except sqlite3.Error:
        if con is not None:
            con.close()
        raise
    return con, uri.partition("?")[2]


@contextmanager
def command_context(args):
    # Rehearsal endpoints can contain authentication parameters; paths can reveal account names.
    # Only remember which overrides were set. Shared previews must not disclose their values.
    extra = tuple(name for name in ("base_url", "read_token_file", "token_file") if getattr(args, name, None))
    vault = getattr(args, "vault", None)
    token = _cli_context.set((getattr(args, "work", None), os.path.abspath(vault) if vault else None, extra))
    try:
        yield
    finally:
        _cli_context.reset(token)


def cli_command(tool, *arguments, work=None, vault=None):
    """Build a shell-quoted next step from program-owned arguments. Never rewrite note text or report rows."""
    selected, selected_vault, extra = _cli_context.get()
    root = os.path.abspath(work or selected or WORK)
    vault = vault or selected_vault
    script = f"tools/{tool}.py"
    args = []
    if tool == "writeback":
        if root != WORK and not os.path.isfile(os.path.join(root, script)):
            script = os.path.join(WORK, script)
            args.extend(["--work", root])
    production_defaults = root == "/data120/Obsidian/notion-obsidian" and vault and os.fspath(vault) == PROD_VAULT
    if vault and ((tool == "writeback" and not production_defaults) or
                  (tool == "refresh" and os.fspath(vault) != PROD_VAULT)):
        args.extend(["--vault", os.fspath(vault)])
        if tool == "refresh":
            args.append("--allow-other-vault")
    command = shlex.join(["python3", "-I", script, *map(str, arguments), *args])
    if tool == "refresh" and extra and "--mode" in arguments:
        # refresh does not accept writeback's endpoint/token overrides. Pin an offline input instead of crawling live.
        command += ' --use-snapshot "${WB_REFRESH_SNAPSHOT:?请指定原演练环境的已完成快照}"'
    if tool == "writeback":
        for option in extra:
            flag = "--" + option.replace("_", "-")
            var = "WB_" + option.upper()
            command += f' {flag} "${{{var}:?请先在本机设置原演练参数{flag}}}"'
    if tool != "writeback" and root != WORK and not os.path.isfile(os.path.join(root, script)):
        # refresh resolves state from its own tools/ parent and has no --work flag.
        # A launcher in a different installation would therefore operate on the wrong workspace.
        notice = (f"此工作目录缺少 {script}。请由维护者在当前目录补全与本次演练同版本的完整 tools/，"
                  "核对目录和库路径后，再运行这条命令。")
        command = (f"if test -r {shlex.quote(script)}; then {command}; "
                   f"else printf '%s\\n' {shlex.quote(notice)} >&2; false; fi")
    return f"cd {shlex.quote(root)} && {command}"


def reconcile_hint(seq):
    """Show each permitted decision separately; the person determines the outcome from the remote record."""
    base = cli_command("writeback", "reconcile", "--journal", seq)
    return f"先在 Notion 核对：已生效用 `{base} --decision committed`，确认未生效用 `{base} --decision not_committed`"


# Obsidian Sync conflict copies, e.g. "Note (Conflicted copy 2026-11-17 1012).md"
SYNC_CONFLICT_RE = re.compile(r" \(Conflicted copy [^)]*\)\.md$")

_HEX32 = re.compile(r"[0-9a-f]{32}")


class UnsafePath(ValueError):
    """A relative path escapes its root, is absolute, or crosses a symlink."""


class FrontmatterError(ValueError):
    """Frontmatter YAML could not be parsed or is not a mapping."""


class WbError(Exception):
    """Write-back planning/executing error (message in Chinese, safe to print).

    IMPL_CONTRACT §5.13.1: wb_plan.load_plan raises it; wb_plan.WbError is this class.
    A plain Exception (not ValueError/OSError) so existing `except ValueError` handlers keep
    their meaning.
    """


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    """Local time with offset, second precision (same format as refresh.py)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def canon_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def norm_notion_id(s) -> str:
    """'8a..32hex' or dashed UUID (any case) -> dashed lowercase UUID; ValueError otherwise."""
    if not isinstance(s, str):
        raise ValueError(f"notion id must be a string: {s!r}")
    h = s.strip().lower().replace("-", "")
    if not _HEX32.fullmatch(h):
        raise ValueError(f"not a Notion id: {s!r}")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def split_note(text: str):
    """Split a note into (frontmatter_yaml_or_None, body, body_first_line_1based).

    Same rules as verify_vault.parse_frontmatter: the first line must be exactly '---'
    (a trailing CR is ignored) and the block ends at the next '---' or '...' line.
    Line numbers count the frontmatter, so they match block_lines' line numbers.
    No frontmatter (first line is not '---') -> (None, text, 1).
    A note that opens with '---' but never closes it raises FrontmatterError("unterminated
    frontmatter") (verify_vault reports the same error): such a note may well carry a notion_id,
    so callers must hold/refuse it, never treat it as a note without frontmatter.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None, text, 1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") in ("---", "..."):
            fm = "\n".join(x.rstrip("\r") for x in lines[1:i])
            return fm, "\n".join(lines[i + 1:]), i + 2
    raise FrontmatterError("unterminated frontmatter")


def parse_frontmatter(fm) -> dict:
    """YAML (yaml.safe_load) -> dict. None/blank -> {}. Raises FrontmatterError when the text
    is not valid YAML or not a mapping: callers must not treat a broken note as 'no notion_id'."""
    if fm is None or not fm.strip():
        return {}
    import yaml
    try:
        data = yaml.safe_load(fm)
    except Exception as exc:  # noqa: BLE001 - any parser error
        msg = str(exc).strip().splitlines()
        raise FrontmatterError((msg[0] if msg else exc.__class__.__name__)[:300]) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    return data


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes) -> None:
    """tmp file in the same directory -> write -> fsync -> os.replace -> fsync(dir).

    The tmp name starts with '.nmig-tmp-' like publish_vault's. A new file gets the umask
    mode; an existing file keeps its permission bits.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("atomic_write needs bytes")
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".nmig-tmp-{os.getpid()}-{secrets.token_hex(4)}-{os.path.basename(path)[:40]}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        try:
            if os.path.exists(path) and not os.path.islink(path):
                os.fchmod(fd, os.stat(path).st_mode & 0o7777)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def safe_join(root: str, rel: str, *, allow_final_symlink: bool = True) -> str:
    """root + rel, refusing escapes and symlinked directories (publish_vault.safe_join semantics).

    Raises UnsafePath (a ValueError) instead of SystemExit. Also refuses absolute or empty `rel`
    and NUL bytes. `allow_final_symlink=False` additionally refuses a symlink as the last component.
    """
    if not isinstance(rel, str) or rel == "" or "\x00" in rel:
        raise UnsafePath(f"invalid relative path: {rel!r}")
    if os.path.isabs(rel):
        raise UnsafePath(f"absolute path not allowed: {rel}")
    root_n = os.path.normpath(os.path.abspath(root))
    p = os.path.normpath(os.path.join(root_n, rel))
    if not (p == root_n or p.startswith(root_n + os.sep)):
        raise UnsafePath(f"path escapes root: {rel}")
    cur = root_n
    for part in os.path.relpath(p, root_n).split(os.sep)[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise UnsafePath(f"symlink in path: {cur}")
    if not allow_final_symlink and os.path.islink(p):
        raise UnsafePath(f"path is a symlink: {p}")
    return p


def backup_copy(src_abs: str, backup_root: str, rel: str) -> str:
    """Copy `src_abs` to `<backup_root>/<rel>` (copy2 + fsync + atomic rename) and return the path.

    An existing backup is never overwritten: identical content -> returns the existing path;
    different content -> the copy goes to '<dst>.1', '<dst>.2', ...
    A `rel` that resolves to the backup root itself ('.', 'a/..') is refused (UnsafePath): the
    '.N' suffix would otherwise land next to, i.e. outside, the backup root.
    """
    dst = safe_join(backup_root, rel)
    if dst == os.path.normpath(os.path.abspath(backup_root)):
        raise UnsafePath(f"backup target is the backup root itself: {rel!r}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src_sha = sha256_file(src_abs)
    cand, n = dst, 0
    while os.path.lexists(cand):
        if os.path.isfile(cand) and not os.path.islink(cand) and sha256_file(cand) == src_sha:
            return cand
        n += 1
        cand = f"{dst}.{n}"
    d = os.path.dirname(cand)
    tmp = os.path.join(d, f".nmig-tmp-{os.getpid()}-{secrets.token_hex(4)}-{os.path.basename(cand)[:40]}")
    try:
        shutil.copy2(src_abs, tmp)
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        if os.path.lexists(cand):  # lost a race: never overwrite a backup
            raise FileExistsError(cand)
        os.replace(tmp, cand)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)
    return cand
