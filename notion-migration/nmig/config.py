"""Credential/config loading. Tokens are read from the environment or from a
chmod-600 dotenv file and are NEVER printed or logged."""
import os
import stat

DEFAULT_ENV_FILE = os.path.expanduser(
    os.environ.get("NOTION_MIGRATE_ENV_FILE", "~/.config/notion-migrate/.env"))
DEFAULT_VERSION = "2026-03-11"


def _parse_dotenv(path):
    vals = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            vals[k] = v
    return vals


def load_config(env_file=DEFAULT_ENV_FILE):
    """Environment variables win over the dotenv file."""
    file_vals, warnings = {}, []
    if env_file and os.path.exists(env_file):
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        if mode & 0o077:
            warnings.append(f"{env_file} 权限为 {oct(mode)}，建议 chmod 600")
        file_vals = _parse_dotenv(env_file)
    get = lambda k, d=None: os.environ.get(k) or file_vals.get(k) or d
    return {
        "source_token": get("NOTION_SOURCE_TOKEN"),
        "target_token": get("NOTION_TARGET_TOKEN"),
        "version": get("NOTION_VERSION", DEFAULT_VERSION),
        "env_file": env_file,
        "warnings": warnings,
    }


def mask(token):
    """Describe a token without revealing it."""
    if not token:
        return "未设置"
    prefix = "ntn_" if token.startswith("ntn_") else ("secret_" if token.startswith("secret_") else "其他前缀")
    return f"已设置（{prefix}，长度 {len(token)}）"
