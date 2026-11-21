#!/usr/bin/env python3
"""Token loading for the Notion <-> Obsidian sync tools (WRITEBACK_SPEC v1.1 §2.1).

Two credentials exist and they are kept strictly apart:

* read token  -- ``NOTION_TARGET_TOKEN`` in ``~/.config/notion-migrate/.env`` (the existing,
  read-only ``Godcreate`` integration). Used by refresh / snapshot / assets_fetch and by every
  read of the write-back tool. ``load_read_token()`` mirrors ``nmig.config.load_config``:
  the environment variable wins over the dotenv file when no explicit path is given.
* write-back token -- ``NOTION_WRITEBACK_TOKEN`` in ``~/.config/notion-obsidian/writeback.env``.
  Only ``writeback.py apply|rollback|audit-scope`` may load it. ``load_writeback_token()``
  never consults the environment, and refuses unless the file is a regular file owned by the
  current user with mode exactly 0600 inside a directory owned by the current user with mode
  exactly 0700 (spec: "不是 600 就拒绝运行").

Tokens are never printed or logged: every error message names the file and the problem but
never the value. Tests must pass explicit temp-file paths; nothing here is read at import time.
"""
import os
import stat

READ_ENV_FILE = "~/.config/notion-migrate/.env"
READ_VAR = "NOTION_TARGET_TOKEN"
WRITEBACK_ENV_FILE = "~/.config/notion-obsidian/writeback.env"
WRITEBACK_VAR = "NOTION_WRITEBACK_TOKEN"


class TokenError(Exception):
    """Raised when a token cannot be loaded safely. The message never contains the token."""


def _parse_dotenv_text(text):
    """KEY=VALUE lines; '#' comments; optional 'export ' prefix; matching quotes stripped."""
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        vals[k] = v
    return vals


def _check_value(value, var, where):
    if not value:
        raise TokenError(f"{var} 未在 {where} 中设置（或为空）")
    if any(c.isspace() for c in value) or any(ord(c) < 32 for c in value):
        raise TokenError(f"{var}（{where}）含空白或控制字符，拒绝使用")
    return value


def _read_regular_file(path, *, strict):
    """Open without following symlinks and fstat the open descriptor (no TOCTOU between check
    and read). ``strict`` enforces owner == euid and mode == 0600."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise TokenError(f"凭据文件不存在：{path}") from None
    except OSError as e:  # ELOOP for a symlink with O_NOFOLLOW, EACCES, ...
        raise TokenError(f"无法安全打开凭据文件 {path}（{type(e).__name__}，不允许符号链接）") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenError(f"凭据文件不是普通文件：{path}")
        if strict:
            mode = stat.S_IMODE(st.st_mode)
            if mode != 0o600:
                raise TokenError(f"凭据文件权限为 {oct(mode)}，必须为 0o600：{path}（chmod 600 后重试）")
            if st.st_uid != os.geteuid():
                raise TokenError(f"凭据文件不属于当前用户：{path}")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            if sum(len(c) for c in chunks) > 1 << 20:
                raise TokenError(f"凭据文件过大：{path}")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def load_read_token(path=None) -> str:
    """Read-integration token. ``path`` given (tests): only that file is read, the environment is
    ignored. ``path`` None: env ``NOTION_TARGET_TOKEN`` wins, else ``~/.config/notion-migrate/.env``
    (same precedence as nmig.config.load_config)."""
    if path is None:
        env = os.environ.get(READ_VAR)
        if env:
            return _check_value(env, READ_VAR, "环境变量")
        path = os.path.expanduser(READ_ENV_FILE)
    text = _read_regular_file(path, strict=False)
    return _check_value(_parse_dotenv_text(text).get(READ_VAR), READ_VAR, path)


def load_writeback_token(path=None) -> str:
    """Write-back token from ``writeback.env`` (never from the environment). Refuses unless the
    file is a regular non-symlink file owned by the current user with mode 0600 and its directory
    is owned by the current user with mode 0700."""
    path = os.path.expanduser(path or WRITEBACK_ENV_FILE)
    d = os.path.dirname(os.path.abspath(path))
    try:
        dst = os.lstat(d)
    except FileNotFoundError:
        raise TokenError(f"凭据目录不存在：{d}") from None
    if stat.S_ISLNK(dst.st_mode) or not stat.S_ISDIR(dst.st_mode):
        raise TokenError(f"凭据目录不是普通目录（或为符号链接）：{d}")
    dmode = stat.S_IMODE(dst.st_mode)
    if dmode != 0o700:
        raise TokenError(f"凭据目录权限为 {oct(dmode)}，必须为 0o700：{d}（chmod 700 后重试）")
    if dst.st_uid != os.geteuid():
        raise TokenError(f"凭据目录不属于当前用户：{d}")
    text = _read_regular_file(path, strict=True)
    return _check_value(_parse_dotenv_text(text).get(WRITEBACK_VAR), WRITEBACK_VAR, path)


def mask(token) -> str:
    """Describe a token without revealing it (safe for logs/reports)."""
    if not token:
        return "未设置"
    prefix = "ntn_" if token.startswith("ntn_") else ("secret_" if token.startswith("secret_") else "其他前缀")
    return f"已设置（{prefix}，长度 {len(token)}）"
