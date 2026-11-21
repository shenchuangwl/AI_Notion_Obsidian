#!/usr/bin/env python3
"""Write-back Notion client (WRITEBACK_SPEC v1.1 §2.4, §2.5, §9, §11.3.5; checklist R-013..R-019, R-172).

Only ``writeback.py apply|rollback|audit-scope`` may construct this, with the token from
``notion_tokens.load_writeback_token()``.

Writes (each returns a ``WriteResult``, never raises for HTTP/network failures):

=====================  ======================================  =========================
method                 request                                 notifications silent
=====================  ======================================  =========================
update_block           PATCH  blocks/{id}  (never in_trash=true) no (endpoint has no param)
restore_block          PATCH  blocks/{id}  {"in_trash": false}  no
append_children        PATCH  blocks/{id}/children (+position) yes
trash_block            DELETE blocks/{id}  (allow_block_trash)  no
update_page            PATCH  pages/{id}  (properties/in_trash) yes
create_page            POST   pages                             yes
=====================  ======================================  =========================

Retry policy -- a write is sent at most once, except:

* 429 and 529 are retried (the request was not processed) after ``Retry-After`` seconds
  (exponential backoff when absent), up to ``max_429_retries`` times, and never when
  ``rate_limit_reason == "public_api_request_blocked"`` or ``Retry-After > max_retry_after``.
  Each such pause also sets the shared ``RateGate`` global pause and is reported through
  ``on_pause(seconds, reason)`` and ``WriteResult.paused_s`` (the caller re-runs its page
  precheck after a pause > 30 s, spec §11.3.5).
* Nothing else is ever resent: not a timeout, not a dropped connection, not 409, not 5xx.

Outcomes (``WriteResult.outcome``):

* ``ok``       -- 2xx with a JSON body (``data``; ``committed_ids`` = new page id or the ids of the
                  appended first-level blocks, validated: a page object whose id is not the parent;
                  exactly one distinct id per child sent, none equal to the parent/anchor); or a
                  503 whose retry guidance has exactly the shape the operation can commit
                  (``status=503``, ``code="committed"``, ``data=None``, ``verify_required=True``:
                  the caller GET-verifies each committed id and never resends) --
                  CREATE_PAGE: only ``committed_resource_id`` (!= parent); APPEND: only
                  ``committed_child_ids``, one per child, none = parent/anchor; UPDATE /
                  RESTORE_BLOCK / TRASH_BLOCK / PAGE_PATCH: only ``committed_resource_id`` == target.
* ``rejected`` -- a definitive 4xx (incl. 409 and an exhausted/blocked 429): not applied.
* ``unknown``  -- the server may or may not have applied it: read timeout or connection error
                  after sending, any 5xx, a 2xx whose body cannot be parsed (``bad_response``) or
                  does not identify exactly the created ids (``ids_ambiguous``, ``data`` kept), a
                  3xx. A 503 is ``unknown`` unless its guidance matches the operation (above):
                  no guidance -> the API code; malformed -> ``guidance_unparseable``; a shape that
                  does not fit the operation -> ``guidance_mismatch`` (``committed_ids`` empty: the
                  named ids are not trusted); fewer child ids than children ->
                  ``partially_committed`` with the named ids.
                  The caller reconciles by set difference (spec §11.3.5) and never resends a
                  non-idempotent request.
* ``not_sent`` -- definitely not sent: connect timeout, connection refused / DNS failure,
                  invalid URL.

Error codes: the API code is kept in ``api_code``; ``code`` is the API code except
``block_limit`` (403 ``restricted_resource`` with ``additional_data.block_limit ==
"block_creation"``, spec §9.1) and ``notifications_rejected`` (400 ``validation_error`` that
mentions ``notifications``; surfaced, never silently retried without the field).

Policy checks raise ``WriteForbidden`` before any I/O: DELETE blocks unless
``allow_block_trash``; ``in_trash: true`` through ``update_block`` (always -- it would bypass
``allow_block_trash``, and on a child_page id trash a page subtree); ``is_locked``/``erase_content``/``archived`` or other unknown keys in
PATCH pages; updates of structural block types; >100 children; creating structural blocks;
POST pages without a page/data-source parent.

Reads (``get_page``, ``get_block``, ``list_children_all``, ``get_property_all``, ``search_all``,
``users_list_all``) go through an internal ``ReadOnlyClient`` sharing token, base URL and gate:
same semantics as the read client, retried on 429/5xx (reads are idempotent).

``Notion-Version`` is pinned to 2026-03-11 (``in_trash``; ``archived`` is refused). The token is
never logged, printed, put in a result or in ``repr``; request bodies are never logged.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import requests

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from readonly_client import NMIG, ReadOnlyClient  # noqa: E402,F401  (NMIG is put on sys.path there)
from nmig.client import API, RateLimiter  # noqa: E402

NOTION_VERSION = "2026-03-11"
log = logging.getLogger("writeback_client")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")

# block types the write-back never updates or creates (spec §2.4: synced blocks, columns, table of
# contents, link_preview; child pages/databases are pages/databases, not block writes)
STRUCTURAL_TYPES = frozenset({
    "synced_block", "column_list", "column", "table_of_contents", "link_preview", "child_page",
    "child_database", "breadcrumb", "template", "unsupported", "meeting_notes", "transcription",
})
PAGE_PATCH_KEYS = frozenset({"properties", "in_trash"})
PAGE_CREATE_KEYS = frozenset({"parent", "properties", "children", "icon", "cover"})
FORBIDDEN_PAGE_KEYS = frozenset({"is_locked", "erase_content", "archived"})
MAX_CHILDREN = 100


class WriteForbidden(Exception):
    """The requested write is outside the write-back policy; raised before any I/O."""


def norm_id(value):
    """Dashed lowercase UUID, or None when ``value`` is not a Notion id."""
    if not isinstance(value, str) or not _UUID_RE.match(value.strip()):
        return None
    h = value.strip().replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


@dataclass
class WriteResult:
    outcome: str                      # ok | rejected | unknown | not_sent
    status: int | None = None
    code: str | None = None
    message: str | None = None
    data: dict | None = None
    committed_ids: list[str] = field(default_factory=list)
    retry_after: float | None = None
    # additions (contract §5 allows adding)
    api_code: str | None = None       # code exactly as returned by the API
    request_id: str | None = None
    op: str | None = None             # UPDATE | RESTORE_BLOCK | APPEND | TRASH_BLOCK | PAGE_PATCH | CREATE_PAGE
    path: str | None = None           # relative API path (ids only, no content)
    attempts: int = 0                 # HTTP sends (>1 only after 429/529)
    paused_s: float = 0.0             # total seconds waited on 429/529 Retry-After
    verify_required: bool = False     # True for a 503-with-committed-ids "ok"
    additional_data: dict | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def _parse_retry_after(value):
    if value is None:
        return None
    try:
        v = float(str(value).strip())
    except ValueError:
        try:
            dt = parsedate_to_datetime(str(value))
            v = dt.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if not math.isfinite(v):
        return None
    return max(0.0, v)


def _is_connect_phase(exc):
    """True when a requests ConnectionError provably happened before the request was sent."""
    seen, e = set(), exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        name = type(e).__name__
        if name in ("NewConnectionError", "NameResolutionError", "ConnectTimeoutError") or \
                isinstance(e, (ConnectionRefusedError,)):
            return True
        if name in ("ProtocolError", "RemoteDisconnected", "ReadTimeoutError", "IncompleteRead",
                    "BadStatusLine") or isinstance(e, (ConnectionResetError, BrokenPipeError)):
            return False
        nxt = getattr(e, "reason", None)
        if nxt is None and e.args and isinstance(e.args[0], BaseException):
            nxt = e.args[0]
        e = nxt if nxt is not None else (e.__cause__ or e.__context__)
    return False


def _walk_blocks(children, depth=1):
    for b in children or []:
        if not isinstance(b, dict):
            raise WriteForbidden("children must be block objects")
        t = b.get("type") or next((k for k in b if k not in ("object", "children")), None)
        yield t, b, depth
        inner = (b.get(t) or {}).get("children") if isinstance(b.get(t), dict) else None
        for x in _walk_blocks(inner or b.get("children"), depth + 1):
            yield x


class WritebackClient:
    """See module docstring. Not a NotionClient subclass: nmig's request() retries every method.
    Not thread-safe: the executor sends one journaled write at a time (each write uses a fresh
    connection; the session's pools are dropped around every write)."""

    RETRY_STATUS = frozenset({429, 529})

    def __init__(self, token, base_url=API, gate=None, timeout=75, silent=True, *,
                 allow_block_trash=False, session=None, sleep=time.sleep, rate=2.5,
                 max_429_retries=5, max_retry_after=120.0, on_send=None, on_response=None,
                 on_pause=None, label="godcreate-wb", read_session=None):
        if not token:
            raise ValueError(f"{label}: token 未配置")
        self._token = token
        self.label = label
        self.version = NOTION_VERSION
        self.base_url = (base_url or API).rstrip("/")
        if isinstance(timeout, (tuple, list)):
            self.timeout = (float(timeout[0]), float(timeout[1]))
        else:
            self.timeout = (min(10.0, float(timeout)), float(timeout))   # (connect, read)
        self.silent = bool(silent)
        self.allow_block_trash = bool(allow_block_trash)
        self.session = session or requests.Session()
        self.gate = gate
        self.limiter = gate if gate is not None else RateLimiter(rate)
        self.sleep = sleep
        self.max_429_retries = int(max_429_retries)
        self.max_retry_after = float(max_retry_after)
        self.on_send, self.on_response, self.on_pause = on_send, on_response, on_pause
        self.stats = {"requests": 0, "retries": 0, "errors": 0}
        self._reader = ReadOnlyClient(token, version=NOTION_VERSION, label=label + "-read", rate=rate,
                                      timeout=self.timeout, base_url=self.base_url,
                                      session=read_session, sleep=sleep)
        self._reader.limiter = self.limiter          # reads and writes share one budget

    def __repr__(self):  # never expose the token
        return f"<WritebackClient {self.label} version={self.version}>"

    __str__ = __repr__

    # ------------------------------------------------------------------ reads
    def get_page(self, page_id):
        return self._reader.retrieve_page(page_id)

    def get_block(self, block_id):
        return self._reader.retrieve_block(block_id)

    def list_children_all(self, block_id):
        return self._reader.list_children(block_id)

    def get_property_all(self, page_id, prop_id):
        return self._reader.retrieve_page_property(page_id, prop_id)

    def search_all(self, query="", object_type=None):
        return self._reader.search(query, object_type)

    def users_list_all(self):
        return list(self._reader.paginate("GET", "users"))

    # ------------------------------------------------------------------ writes
    def update_block(self, block_id, body) -> WriteResult:
        """PATCH blocks/{id}: content of one writable block type, optionally with
        ``in_trash: false`` (restore). ``in_trash: true`` is ALWAYS refused here -- it would trash
        the block (or, on a child_page/child_database id, a whole page subtree) behind the
        ``allow_block_trash`` switch; blocks are trashed only via ``trash_block()`` and pages only
        via ``update_page()``."""
        bid = self._id(block_id, "block_id")
        if not isinstance(body, dict) or not body:
            raise WriteForbidden("update_block: body must be a non-empty dict")
        bad = set(body) & {"archived", "type", "children", "notifications"}
        if bad:
            raise WriteForbidden(f"update_block: keys not allowed: {sorted(bad)}")
        if "in_trash" in body:
            if not isinstance(body["in_trash"], bool):
                raise WriteForbidden("update_block: in_trash must be a boolean")
            if body["in_trash"]:
                raise WriteForbidden("update_block: in_trash=true refused; blocks are deleted only via "
                                     "trash_block() (allow_block_trash), pages only via update_page()")
        type_keys = [k for k in body if k != "in_trash"]
        if len(type_keys) > 1:
            raise WriteForbidden(f"update_block: only one block-type key allowed, got {sorted(type_keys)}")
        if type_keys:
            t = type_keys[0]
            if t in STRUCTURAL_TYPES:
                raise WriteForbidden(f"update_block: block type {t} is never written")
            if not isinstance(body[t], dict) or "children" in body[t]:
                raise WriteForbidden(f"update_block: {t} must be an object without children")
        return self._write("UPDATE", "PATCH", f"blocks/{bid}", body, {"target": bid})

    def restore_block(self, block_id) -> WriteResult:
        """PATCH blocks/{id} {"in_trash": false} -- the rollback of a block deletion. Live Notion
        restores the block but NOT at its original position (contract §7.6): the caller re-reads
        the parent's children and reports "已恢复但位置可能变化" / needs_manual."""
        bid = self._id(block_id, "block_id")
        return self._write("RESTORE_BLOCK", "PATCH", f"blocks/{bid}", {"in_trash": False}, {"target": bid})

    def append_children(self, parent_id, children, position=None) -> WriteResult:
        pid = self._id(parent_id, "parent_id")
        if not isinstance(children, list) or not children:
            raise WriteForbidden("append_children: children must be a non-empty list")
        if len(children) > MAX_CHILDREN:
            raise WriteForbidden(f"append_children: {len(children)} children > {MAX_CHILDREN}")
        self._check_new_blocks(children, "append_children")
        body = {"children": children}
        anchor = None
        if position is not None:
            body["position"] = self._position(position)
            anchor = (body["position"].get("after_block") or {}).get("id")
        if self.silent:
            body["notifications"] = {"mode": "silent"}
        return self._write("APPEND", "PATCH", f"blocks/{pid}/children", body,
                           {"parent": pid, "anchor": anchor, "n": len(children)})

    def trash_block(self, block_id) -> WriteResult:
        bid = self._id(block_id, "block_id")
        if not self.allow_block_trash:
            raise WriteForbidden("trash_block: block deletion is disabled (allow_block_trash=false)")
        return self._write("TRASH_BLOCK", "DELETE", f"blocks/{bid}", None, {"target": bid})

    def update_page(self, page_id, body) -> WriteResult:
        pid = self._id(page_id, "page_id")
        if not isinstance(body, dict) or not body:
            raise WriteForbidden("update_page: body must be a non-empty dict")
        bad = set(body) & FORBIDDEN_PAGE_KEYS
        if bad:
            raise WriteForbidden(f"update_page: forbidden keys {sorted(bad)}")
        extra = set(body) - PAGE_PATCH_KEYS
        if extra:
            raise WriteForbidden(f"update_page: keys not allowed: {sorted(extra)}")
        if "in_trash" in body and not isinstance(body["in_trash"], bool):
            raise WriteForbidden("update_page: in_trash must be a boolean")
        if "properties" in body and not isinstance(body["properties"], dict):
            raise WriteForbidden("update_page: properties must be an object")
        body = dict(body)
        if self.silent:
            body["notifications"] = {"mode": "silent"}
        return self._write("PAGE_PATCH", "PATCH", f"pages/{pid}", body, {"target": pid})

    def create_page(self, body) -> WriteResult:
        if not isinstance(body, dict):
            raise WriteForbidden("create_page: body must be a dict")
        extra = set(body) - PAGE_CREATE_KEYS
        if extra:
            raise WriteForbidden(f"create_page: keys not allowed: {sorted(extra)}")
        parent = body.get("parent") or {}
        ptype = parent.get("type") or next((k for k in ("page_id", "data_source_id") if k in parent), None)
        if ptype not in ("page_id", "data_source_id") or not norm_id(parent.get(ptype)):
            raise WriteForbidden("create_page: parent must be a page_id or data_source_id")
        children = body.get("children")
        if children is not None:
            if not isinstance(children, list) or len(children) > MAX_CHILDREN:
                raise WriteForbidden(f"create_page: children must be a list of <= {MAX_CHILDREN}")
            self._check_new_blocks(children, "create_page")
        body = dict(body)
        if self.silent:
            body["notifications"] = {"mode": "silent"}
        return self._write("CREATE_PAGE", "POST", "pages", body, {"parent": norm_id(parent.get(ptype))})

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _id(value, what):
        nid = norm_id(value)
        if nid is None:
            raise WriteForbidden(f"{what} is not a Notion id")
        return nid

    @staticmethod
    def _position(position):
        if not isinstance(position, dict):
            raise WriteForbidden("position must be an object")
        t = position.get("type")
        if t == "start" and set(position) == {"type"}:
            return {"type": "start"}
        if t == "after_block" and set(position) == {"type", "after_block"}:
            anchor = norm_id((position.get("after_block") or {}).get("id"))
            if anchor:
                return {"type": "after_block", "after_block": {"id": anchor}}
        raise WriteForbidden("position must be {'type':'start'} or {'type':'after_block','after_block':{'id':..}}")

    @staticmethod
    def _check_new_blocks(children, what):
        for t, _b, depth in _walk_blocks(children):
            if not t or t in STRUCTURAL_TYPES:
                raise WriteForbidden(f"{what}: block type {t!r} cannot be created by write-back")
            if depth > 3:
                raise WriteForbidden(f"{what}: nesting deeper than the API allows")

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json", "Connection": "close"}

    def _result(self, outcome, op, rel, attempts, paused, **kw):
        res = WriteResult(outcome=outcome, op=op, path=rel, attempts=attempts, paused_s=paused, **kw)
        if outcome != "ok":
            self.stats["errors"] += 1
        log.info("%s %s %s -> %s status=%s code=%s rid=%s", self.label, op, rel, outcome,
                 res.status, res.code, res.request_id)
        return res

    def _pause(self, seconds, reason):
        if self.gate is not None:
            self.gate.pause(seconds)
        if self.on_pause is not None:
            self.on_pause(seconds, reason)
        self.stats["retries"] += 1
        log.info("%s pause %.1fs (%s)", self.label, seconds, reason)
        self.sleep(seconds)

    def _drop_pool(self):
        """Never reuse a pooled keep-alive connection for a write: a connection the server already
        closed fails *after* the request was handed to the socket, which would turn a request that
        never arrived into an 'unknown' outcome. Closing the adapters' pools after every write makes
        each write open a fresh connection (the session stays usable)."""
        close = getattr(self.session, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def _write(self, op, method, rel, body, ctx=None) -> WriteResult:
        """``ctx`` describes what the request may legitimately commit (used to validate ids in a
        2xx body or a 503 retry guidance): ``target`` (UPDATE/RESTORE_BLOCK/TRASH_BLOCK/PAGE_PATCH),
        ``parent`` (+ ``anchor``, ``n`` children for APPEND; the parent for CREATE_PAGE)."""
        try:
            return self._write_once(op, method, rel, body, ctx or {})
        finally:
            self._drop_pool()

    def _write_once(self, op, method, rel, body, ctx) -> WriteResult:
        url = f"{self.base_url}/{rel}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        attempt, paused = 0, 0.0
        while True:
            attempt += 1
            self.limiter.acquire()
            self.stats["requests"] += 1
            if self.on_send is not None:
                self.on_send(method, rel, attempt)
            self._drop_pool()
            try:
                resp = self.session.request(method, url, data=payload, headers=self._headers(),
                                            timeout=self.timeout, allow_redirects=False)
            except requests.exceptions.ConnectTimeout as e:
                return self._result("not_sent", op, rel, attempt, paused, code="connect_timeout",
                                    message=type(e).__name__)
            except requests.exceptions.ConnectionError as e:
                if _is_connect_phase(e):
                    return self._result("not_sent", op, rel, attempt, paused, code="connect_error",
                                        message=type(e).__name__)
                return self._result("unknown", op, rel, attempt, paused, code="connection_lost",
                                    message=f"{type(e).__name__} after send; outcome unknown")
            except requests.exceptions.Timeout as e:
                return self._result("unknown", op, rel, attempt, paused, code="read_timeout",
                                    message=f"{type(e).__name__}; outcome unknown")
            except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                    requests.exceptions.InvalidSchema, requests.exceptions.InvalidHeader) as e:
                return self._result("not_sent", op, rel, attempt, paused, code="invalid_request",
                                    message=type(e).__name__)
            except requests.exceptions.RequestException as e:
                return self._result("unknown", op, rel, attempt, paused, code="request_error",
                                    message=f"{type(e).__name__}; outcome unknown")
            status = resp.status_code
            rid = resp.headers.get("x-request-id") or resp.headers.get("X-Notion-Request-Id")
            if self.on_response is not None:
                self.on_response(status, rid)
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if 200 <= status < 300:
                try:
                    data = resp.json() if resp.content else {}
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    return self._result("unknown", op, rel, attempt, paused, status=status, code="bad_response",
                                        message="2xx with an unparseable body; outcome unknown", request_id=rid)
                ids = self._ids_from_data(op, data, ctx)
                if ids is None:
                    # applied, but the response does not identify exactly the created object(s):
                    # never guess -- the caller reconciles by set difference like any unknown outcome
                    return self._result("unknown", op, rel, attempt, paused, status=status, code="ids_ambiguous",
                                        message="2xx whose body does not identify the created ids; reconcile",
                                        data=data, request_id=rid, retry_after=retry_after)
                return self._result("ok", op, rel, attempt, paused, status=status, data=data,
                                    committed_ids=ids, request_id=rid, retry_after=retry_after)
            if status < 400:
                return self._result("unknown", op, rel, attempt, paused, status=status, code="redirect",
                                    message="unexpected 3xx; outcome unknown", request_id=rid)
            try:
                err = resp.json()
                if not isinstance(err, dict):
                    raise ValueError
            except ValueError:
                err = {"code": "http_error", "message": (resp.text or "")[:300]}
            api_code = err.get("code") or "unknown"
            msg = str(err.get("message") or "")[:500]
            ad = err.get("additional_data") if isinstance(err.get("additional_data"), dict) else {}
            reason = ad.get("rate_limit_reason") or err.get("rate_limit_reason")
            common = dict(status=status, api_code=api_code, request_id=rid, retry_after=retry_after,
                          additional_data=ad or None)
            if status in self.RETRY_STATUS:
                if reason == "public_api_request_blocked":
                    return self._result("rejected", op, rel, attempt, paused, code=api_code,
                                        message=msg, **common)
                if attempt > self.max_429_retries or (retry_after or 0) > self.max_retry_after:
                    if retry_after and self.gate is not None:
                        self.gate.pause(retry_after)
                    return self._result("rejected", op, rel, attempt, paused, code=api_code,
                                        message=msg, **common)
                wait = retry_after if retry_after is not None else min(30.0, 2.0 ** (attempt - 1))
                self._pause(wait, f"{status} {api_code} {reason or ''}".strip())
                paused += wait
                continue
            if status == 503:
                outcome, code503, ids = self._classify_503(op, err, ctx)
                return self._result(outcome, op, rel, attempt, paused, code=code503 or api_code, message=msg,
                                    committed_ids=ids, verify_required=bool(ids), **common)
            if status >= 500:
                return self._result("unknown", op, rel, attempt, paused, code=api_code, message=msg, **common)
            code = api_code
            if status == 403 and api_code == "restricted_resource" and ad.get("block_limit") == "block_creation":
                code = "block_limit"
            elif status == 400 and api_code == "validation_error" and "notification" in msg.lower():
                code = "notifications_rejected"
                msg = ("Notion 拒绝了 notifications 参数（send_silent_notifications=true）："
                       f"{msg}；请把 config 的 send_silent_notifications 设为 false 后重新生成计划")
            return self._result("rejected", op, rel, attempt, paused, code=code, message=msg, **common)

    @staticmethod
    def _ids_from_data(op, data, ctx):
        """Created ids from a 2xx body, validated against what the request could create.
        Returns None when the body does not identify them exactly (caller -> 'ids_ambiguous')."""
        if op == "CREATE_PAGE":
            i = norm_id(data.get("id"))
            if i is None or data.get("object") != "page" or i == ctx.get("parent"):
                return None
            return [i]
        if op == "APPEND":
            results = data.get("results")
            if not isinstance(results, list):
                return None
            ids = [norm_id(r.get("id")) if isinstance(r, dict) else None for r in results]
            forbidden = {ctx.get("parent"), ctx.get("anchor")} - {None}
            n = ctx.get("n")
            if any(i is None for i in ids) or len(set(ids)) != len(ids) or forbidden & set(ids) or \
                    (n is not None and len(ids) != n):
                return None
            return ids
        return []

    @staticmethod
    def _guidance(err):
        """(committed_resource_id, committed_child_ids) of a 503, each None when absent, read from
        ``additional_data.retry_guidance`` and/or flat ``additional_data``. Raises ValueError when a
        value is malformed (not an id, empty/duplicate list) or the two nestings disagree."""
        ad = err.get("additional_data") if isinstance(err.get("additional_data"), dict) else {}
        rg = ad.get("retry_guidance") if isinstance(ad.get("retry_guidance"), dict) else {}
        found = []
        for src in (rg, ad):
            res = kids = None
            if "committed_resource_id" in src:
                res = norm_id(src["committed_resource_id"])
                if res is None:
                    raise ValueError("committed_resource_id is not an id")
            if "committed_child_ids" in src:
                v = src["committed_child_ids"]
                if not isinstance(v, list) or not v:
                    raise ValueError("committed_child_ids is not a non-empty list")
                kids = [norm_id(x) for x in v]
                if any(i is None for i in kids) or len(set(kids)) != len(kids):
                    raise ValueError("committed_child_ids has bad or duplicate ids")
            if res is not None or kids is not None:
                found.append((res, kids))
        if len(found) == 2 and found[0] != found[1]:
            raise ValueError("retry_guidance and additional_data disagree")
        return found[0] if found else (None, None)

    @classmethod
    def _classify_503(cls, op, err, ctx):
        """(outcome, code or None (= keep the API code), committed_ids) for a 503.

        'ok' (code 'committed', verify_required) only when the guidance has exactly the shape this
        operation can commit:
          CREATE_PAGE  committed_resource_id only, != the parent id;
          APPEND       committed_child_ids only, one per child sent, none = parent/anchor
                       (fewer ids -> 'unknown' 'partially_committed' with those ids);
          UPDATE / RESTORE_BLOCK / TRASH_BLOCK / PAGE_PATCH  committed_resource_id == the target.
        No guidance -> 'unknown' (API code); any other shape -> 'unknown' 'guidance_mismatch' with
        no ids (the named ids cannot be trusted as 'what this request created')."""
        try:
            res, kids = cls._guidance(err)
        except ValueError:
            return "unknown", "guidance_unparseable", []
        if res is None and kids is None:
            return "unknown", None, []
        mismatch = ("unknown", "guidance_mismatch", [])
        if op == "CREATE_PAGE":
            if kids is not None or res == ctx.get("parent"):
                return mismatch
            return "ok", "committed", [res]
        if op == "APPEND":
            forbidden = {ctx.get("parent"), ctx.get("anchor")} - {None}
            n = ctx.get("n")
            if res is not None or kids is None or forbidden & set(kids) or (n is not None and len(kids) > n):
                return mismatch
            if n is not None and len(kids) < n:
                return "unknown", "partially_committed", kids
            return "ok", "committed", kids
        if kids is None and res is not None and res == ctx.get("target"):
            return "ok", "committed", [res]
        return mismatch
