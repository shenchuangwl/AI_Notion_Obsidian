#!/usr/bin/env python3
"""Write-back configuration: `config/writeback.json` over the WRITEBACK_SPEC §15 defaults (IMPL_CONTRACT §5.2).

    cfg = wb_config.load()                 # missing file -> defaults (mode "off")
    wb_config.effective_mode(cfg, db)      # configured mode lowered by D-21, WB §2.2 and wb_meta downgrades

Unknown keys, duplicate keys, wrong types and invalid values (including NaN/Infinity) raise
ConfigError (callers exit non-zero, R-001). The mode is only ever changed by the user editing the
file. Tools may only LOWER the effective mode (record_downgrade); nothing in this module raises it.

effective_mode fails CLOSED: without a readable writeback.db (db None, closed, locked, corrupt,
no wb_meta table, invalid forced_mode value) the result is capped at "plan".
"""
import copy
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402

CONFIG_PATH = os.path.join(wb_common.CONFIG_DIR, "writeback.json")
MODES = ("off", "plan", "whitelist", "full")
_RANK = {m: i for i, m in enumerate(MODES)}
# sanity bounds: Notion's documented average limit is 3 requests/s per integration; a plan older
# than a week is never a sensible confirmation basis.
MAX_RPS = 3.0
MAX_PLAN_TTL_HOURS = 168

DEFAULTS = {
    # WRITEBACK_SPEC §15
    "mode": "off",
    "workspace_members": 1,
    "notion_plan": "free",
    "whitelist_page_ids": [],
    "whitelist_scope": {},
    "whitelist_database_ids": [],
    "automation_free_database_ids": [],
    "test_page_id": None,
    "plan_ttl_hours": 24,
    "max_ops_per_page": 50,
    "max_modified_notes": 20,
    "max_local_deletes": 5,
    "max_batch_deletes": 5,
    "pending_delete_ttl_days": 14,
    "allow_block_trash": False,
    "allow_page_move": False,
    "sync_settle_minutes": 15,
    "sync_burst_files": 20,
    "rate_limit_total_rps": 2.5,
    "rate_limit_writeback_during_refresh_rps": 1.0,
    "conflict_pause_threshold": 20,
    "source_trash_pause_threshold": 10,
    "source_change_pause_ratio": 0.10,
    # IMPL_CONTRACT §5.2 additions
    "send_silent_notifications": True,
    "max_pages_per_plan": 50,
    # WB §2.2 / R-004: set by the user (ISO time or note) once the read integration is read-only
    # in Notion. While null, the effective mode is capped at "plan".
    "read_integration_readonly_confirmed_at": None,
}

# meta keys written by record_downgrade (wb_meta table of state/writeback.db)
META_FORCED_MODE = "forced_mode"
META_FORCED_REASON = "forced_mode_reason"
META_FORCED_AT = "forced_mode_at"


class ConfigError(ValueError):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    """int/float, not bool, finite (JSON NaN / Infinity / 1e999 are refused)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # int too large for a float
        return False


def _ids(key, v):
    if not isinstance(v, list):
        raise ConfigError(f"{key}: expected a list of Notion ids, got {type(v).__name__}")
    out = []
    for x in v:
        try:
            nid = wb_common.norm_notion_id(x)
        except ValueError as exc:
            raise ConfigError(f"{key}: {exc}") from None
        if nid not in out:
            out.append(nid)
    return out


def _validate(cfg):
    for k in cfg:
        if k not in DEFAULTS:
            raise ConfigError(f"unknown key: {k}")
    out = dict(cfg)
    m = out["mode"]
    if m not in MODES:
        raise ConfigError(f"mode: must be one of {', '.join(MODES)}, got {m!r}")
    if not _is_int(out["workspace_members"]) or out["workspace_members"] < 1:
        raise ConfigError("workspace_members: expected an integer >= 1")
    if not isinstance(out["notion_plan"], str) or not out["notion_plan"].strip():
        raise ConfigError("notion_plan: expected a non-empty string")
    for k in ("whitelist_page_ids", "whitelist_database_ids", "automation_free_database_ids"):
        out[k] = _ids(k, out[k])
    sc = out["whitelist_scope"]
    if not isinstance(sc, dict):
        raise ConfigError("whitelist_scope: expected an object {page_id: \"page\"|\"subtree\"}")
    nsc = {}
    for pid, scope in sc.items():
        try:
            nid = wb_common.norm_notion_id(pid)
        except ValueError as exc:
            raise ConfigError(f"whitelist_scope: {exc}") from None
        if scope not in ("page", "subtree"):
            raise ConfigError(f"whitelist_scope[{pid}]: expected \"page\" or \"subtree\", got {scope!r}")
        if nsc.get(nid, scope) != scope:
            raise ConfigError(f"whitelist_scope: {nid} is listed more than once with different scopes "
                              f"({nsc[nid]!r} and {scope!r}); keep exactly one entry")
        nsc[nid] = scope
    out["whitelist_scope"] = nsc
    if out["test_page_id"] is not None:
        try:
            out["test_page_id"] = wb_common.norm_notion_id(out["test_page_id"])
        except ValueError as exc:
            raise ConfigError(f"test_page_id: {exc}") from None
    for k in ("max_ops_per_page", "max_modified_notes", "max_local_deletes", "max_batch_deletes",
              "pending_delete_ttl_days", "sync_settle_minutes", "sync_burst_files",
              "conflict_pause_threshold", "source_trash_pause_threshold", "max_pages_per_plan"):
        if not _is_int(out[k]) or out[k] < 0:
            raise ConfigError(f"{k}: expected an integer >= 0")
    for k in ("plan_ttl_hours", "rate_limit_total_rps", "rate_limit_writeback_during_refresh_rps"):
        if not _is_num(out[k]) or not out[k] > 0:
            raise ConfigError(f"{k}: expected a finite number > 0")
    if out["rate_limit_total_rps"] > MAX_RPS:
        raise ConfigError(f"rate_limit_total_rps: must not exceed {MAX_RPS} (Notion API average limit)")
    if out["plan_ttl_hours"] > MAX_PLAN_TTL_HOURS:
        raise ConfigError(f"plan_ttl_hours: must not exceed {MAX_PLAN_TTL_HOURS}")
    if out["rate_limit_writeback_during_refresh_rps"] > out["rate_limit_total_rps"]:
        raise ConfigError("rate_limit_writeback_during_refresh_rps must not exceed rate_limit_total_rps")
    r = out["source_change_pause_ratio"]
    if not _is_num(r) or not 0 < r <= 1:
        raise ConfigError("source_change_pause_ratio: expected a number in (0, 1]")
    for k in ("allow_block_trash", "allow_page_move", "send_silent_notifications"):
        if not isinstance(out[k], bool):
            raise ConfigError(f"{k}: expected true or false")
    v = out["read_integration_readonly_confirmed_at"]
    if v is not None and (not isinstance(v, str) or not v.strip()):
        raise ConfigError("read_integration_readonly_confirmed_at: expected null or a non-empty string")
    return out


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def _pairs_no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ConfigError(f"duplicate key in JSON object: {k!r}")
        out[k] = v
    return out


def _no_constant(name):
    raise ConfigError(f"non-finite number {name} is not allowed")


def load(path=None) -> dict:
    """Defaults merged with the JSON file (default config/writeback.json). Missing file -> defaults."""
    p = path or CONFIG_PATH
    cfg = defaults()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh, object_pairs_hook=_pairs_no_dupes, parse_constant=_no_constant)
        except ConfigError as exc:
            raise ConfigError(f"{p}: {exc}") from None
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {p}: {exc}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{p}: top level must be a JSON object")
        cfg.update(data)
    return _validate(cfg)


def config_sha(cfg) -> str:
    """sha256 of the canonical JSON of a loaded config (plan.json `config_sha`)."""
    return wb_common.sha256_bytes(wb_common.canon_json(cfg).encode("utf-8"))


def mode_rank(mode) -> int:
    if mode not in _RANK:
        raise ValueError(f"unknown mode {mode!r}")
    return _RANK[mode]


def _read_downgrade(db):
    """(forced_mode | None, reason | None) from wb_meta. Fails closed: whenever the downgrade state
    cannot be read with certainty the answer is ("plan", why) (WB §2.3 / §11.3)."""
    if db is None:
        return "plan", "未提供 writeback.db 连接，无法确认是否存在自动降级记录，按 plan 处理"
    try:
        rows = {r[0]: r[1] for r in db.execute("SELECT key, value FROM wb_meta WHERE key IN (?, ?)",
                                                (META_FORCED_MODE, META_FORCED_REASON))}
    except Exception as exc:  # noqa: BLE001 - closed/locked/corrupt db, no wb_meta table, not a connection
        return "plan", f"无法读取 writeback.db 的自动降级记录（{exc.__class__.__name__}: {exc}），按 plan 处理"
    forced = rows.get(META_FORCED_MODE)
    if forced is None:
        return None, None
    if forced not in _RANK:
        return "plan", f"自动降级记录无效（forced_mode={forced!r}），按 plan 处理"
    return forced, rows.get(META_FORCED_REASON) or "auto downgrade"


def effective_mode_info(cfg, db) -> dict:
    """{"mode": effective, "configured": cfg mode, "reasons": [str, ...]} (reasons explain each cap).

    `db` is required: the writeback.db connection whose wb_meta holds auto-downgrades. Passing None
    or an unreadable connection caps the result at "plan" (fail closed)."""
    m = cfg["mode"]
    reasons = []

    def cap(to, why):
        nonlocal m
        # Report each independent restriction on the configured mode, even if another one already capped m.
        if _RANK[cfg["mode"]] > _RANK[to]:
            reasons.append(why)
        if _RANK[m] > _RANK[to]:
            m = to

    if any(v == "subtree" for v in (cfg.get("whitelist_scope") or {}).values()):
        cap("plan", "whitelist_scope 含 subtree：当前只支持逐页授权，请用户本人核对白名单，只保留已批准的叶子页范围")
    if not cfg.get("read_integration_readonly_confirmed_at"):
        cap("plan", "read_integration_readonly_confirmed_at 为空：请用户本人在 Notion 核对读连接只读权限，依据真实核验记录填写确认时间，勿发送写请求来验证")
    forced, why = _read_downgrade(db)
    if forced is not None:
        recovery = "请维护者核实降级记录和状态库，再重新检查有效模式"
        if isinstance(why, str) and why.startswith("audit-scope"):
            audit = wb_common.cli_command("writeback", "audit-scope")
            recovery = (f"请用户本人在 Notion 核对写连接的页面共享，修正多余或不可达的对象，再运行 `{audit}`；"
                        "只有审计通过并核对结果后，才按提示解除自动降级")
        cap(forced, f"自动降级记录：{forced}（{why}）；{recovery}")
    return {"mode": m, "configured": cfg["mode"], "reasons": reasons}


def effective_mode(cfg, db) -> str:
    """Effective mode (see effective_mode_info). Re-read before every page and every write."""
    return effective_mode_info(cfg, db)["mode"]


def record_downgrade(db, mode, reason) -> bool:
    """Record an automatic downgrade in wb_meta (e.g. audit-scope mismatch -> "plan").

    Only lowers: an existing lower forced mode is kept. Returns True if the record changed.
    Clearing a downgrade is a human decision (writeback CLI behind the human gate).
    """
    if mode not in _RANK:
        raise ValueError(f"unknown mode {mode!r}")
    import wb_state
    with wb_state.transaction(db):
        cur = wb_state.get_meta(db, META_FORCED_MODE)
        if cur in _RANK and _RANK[cur] <= _RANK[mode]:
            return False
        wb_state.set_meta(db, META_FORCED_MODE, mode)
        wb_state.set_meta(db, META_FORCED_REASON, str(reason))
        wb_state.set_meta(db, META_FORCED_AT, wb_common.now_iso())
    return True
