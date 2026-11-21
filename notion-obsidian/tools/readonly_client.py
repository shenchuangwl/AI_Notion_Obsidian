#!/usr/bin/env python3
"""Read-only Notion client (WRITEBACK_SPEC v1.1 §2.2, §2.6; checklist R-010/R-011).

``ReadOnlyClient`` subclasses ``nmig.client.NotionClient`` (the library is imported, never
modified) and overrides ``request()``, through which every helper, ``paginate()`` and
snapshot's ``list_templates`` pass. Allowed:

* ``GET`` on any API path (no request body);
* ``POST search``, ``POST data_sources/{id}/query``, ``POST databases/{id}/query``.

Everything else -- PATCH, DELETE, PUT, other POSTs, multipart/form bodies, absolute URLs that do
not start with this client's ``base_url``, paths with ``..``/``//``/backslashes -- raises
``ReadOnlyViolation`` *before* the rate limiter or the HTTP session is touched, so nothing is
sent and no rate token is consumed. The write helpers inherited from ``NotionClient`` are also
overridden to raise immediately (defence in depth).

Reads keep nmig's retry policy (GET/search/query are safe to retry: network errors, timeouts,
409, 429, 500, 502, 503, 504) and additionally retry 529 (overloaded), which nmig does not:
``request()`` runs nmig's loop with ``READ_RETRY_STATUS`` (spec §11.2: 429/529 honour
``Retry-After``). When the limiter is a ``rate_gate.RateGate``, a 429 or 529 ``Retry-After`` also
sets the gate's global pause so other processes back off too.

``make_read_client()`` builds one: the token defaults to ``notion_tokens.load_read_token()``;
``rate`` is a per-process cap; ``gate`` (a ``RateGate``) is the cross-process limit.
"""
import json
import logging
import os
import re
import sys
import time
from urllib.parse import unquote

import requests

NMIG = "/data120/Obsidian/notion-migration"
if NMIG not in sys.path:
    sys.path.insert(0, NMIG)

from nmig.client import API, RETRY_STATUS, NotionAPIError, NotionClient, RateLimiter  # noqa: E402

DEFAULT_VERSION = "2026-03-11"
READ_RETRY_STATUS = frozenset(RETRY_STATUS) | {529}
_nlog = logging.getLogger("nmig.client")     # same logger as nmig, so log routing is unchanged

_UUID = r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
_RO_POST = (
    re.compile(r"search"),
    re.compile(rf"data_sources/{_UUID}/query"),
    re.compile(rf"databases/{_UUID}/query"),
)


class ReadOnlyViolation(Exception):
    """A write (or otherwise non-read) request was attempted through a read-only client."""


class _CapThenGate:
    """Limiter = per-process cap (nmig RateLimiter) followed by the shared cross-process gate."""

    def __init__(self, cap, gate):
        self.cap, self.gate = cap, gate

    def acquire(self):
        self.cap.acquire()
        self.gate.acquire()

    def pause(self, seconds):
        self.gate.pause(seconds)


class ReadOnlyClient(NotionClient):
    """NotionClient that can only read. See the module docstring for the allowlist."""

    def __repr__(self):  # never expose the token
        return f"<ReadOnlyClient {self.label} version={self.version}>"

    def _rel(self, path):
        if not isinstance(path, str) or not path:
            raise ReadOnlyViolation("empty or non-string path refused")
        if path.startswith("http"):
            pre = self.base_url + "/"
            if not path.startswith(pre):
                raise ReadOnlyViolation(f"foreign URL refused: {path.split('?')[0][:80]}")
            path = path[len(pre):]
        rel = path.lstrip("/").split("?", 1)[0].split("#", 1)[0]
        if "\\" in rel or "//" in rel or any(unquote(s) in (".", "..") for s in rel.split("/")):
            raise ReadOnlyViolation(f"suspicious path refused: {rel[:80]}")
        return rel

    def check(self, method, path, body=None, files=None, data=None):
        """Raise ReadOnlyViolation unless (method, path) is a permitted read. Returns the rel path."""
        m = (method or "").upper()
        rel = self._rel(path)
        if files is not None or data is not None:
            raise ReadOnlyViolation(f"{m} {rel[:80]}: multipart/form bodies refused")
        if m == "GET":
            if body is not None:
                raise ReadOnlyViolation(f"GET {rel[:80]} with a body refused")
            return rel
        if m == "POST" and any(rx.fullmatch(rel) for rx in _RO_POST):
            return rel
        raise ReadOnlyViolation(f"{m} {rel[:80]} refused by ReadOnlyClient")

    def request(self, method, path, params=None, body=None, files=None, data=None):
        self.check(method, path, body=body, files=files, data=data)   # raises BEFORE limiter/session
        return self._send_read(method, path, params, body)

    def _send_read(self, method, path, params, body):
        """nmig ``NotionClient.request()`` (same session call, logging, errors and backoff) for a
        request ``check()`` accepted (never files/form data), with ``READ_RETRY_STATUS`` = nmig's
        RETRY_STATUS + 529."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            self.stats["requests"] += 1
            t0 = time.monotonic()
            try:
                resp = self.session.request(
                    method, url, params=params,
                    data=json.dumps(body) if body is not None else None,
                    files=None, headers=self._headers(json_body=True),
                    timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > self.max_retries:
                    self.stats["errors"] += 1
                    raise NotionAPIError(0, "network_error", str(exc), path=path)
                self._backoff(attempt, None, f"network {type(exc).__name__}", path)
                continue
            dt = time.monotonic() - t0
            rid = resp.headers.get("x-request-id") or resp.headers.get("X-Notion-Request-Id")
            if resp.status_code < 400:
                _nlog.debug("%s %s %s -> %s %.2fs", self.label, method, path, resp.status_code, dt)
                return resp.json() if resp.content else {}
            try:
                err = resp.json()
            except ValueError:
                err = {"code": "http_error", "message": resp.text[:300]}
            code, msg = err.get("code", "unknown"), err.get("message", "")
            if resp.status_code in READ_RETRY_STATUS and attempt <= self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"{resp.status_code} {code}", path)
                continue
            self.stats["errors"] += 1
            _nlog.warning("%s %s %s -> %s %s %s rid=%s", self.label, method, path, resp.status_code, code,
                          msg[:200], rid)
            raise NotionAPIError(resp.status_code, code, msg, rid, path)

    def _backoff(self, attempt, retry_after, why, path):
        # a 429 with Retry-After pauses every process sharing the rate gate
        if retry_after and str(why).split(" ", 1)[0] in ("429", "529"):
            pause = getattr(self.limiter, "pause", None)
            if pause is not None:
                try:
                    pause(float(retry_after))
                except (TypeError, ValueError):
                    pass
        return super()._backoff(attempt, retry_after, why, path)

    # ---- write helpers: refuse immediately (never reach request()) ----
    def _refuse(self, what):
        raise ReadOnlyViolation(f"{what} refused by ReadOnlyClient")

    def create_page(self, body):
        self._refuse("create_page")

    def update_page(self, pid, body):
        self._refuse("update_page")

    def append_children(self, block_id, children, after=None):
        self._refuse("append_children")

    def update_block(self, bid, body):
        self._refuse("update_block")

    def trash_block(self, bid):
        self._refuse("trash_block")

    def create_database(self, body):
        self._refuse("create_database")

    def update_data_source(self, dsid, body):
        self._refuse("update_data_source")

    def create_comment(self, body):
        self._refuse("create_comment")

    def upload_bytes(self, filename, content, content_type="application/octet-stream"):
        self._refuse("upload_bytes")


def make_read_client(base_url=None, token=None, rate=2.5, gate=None, *, version=DEFAULT_VERSION,
                     label="godcreate-ro", session=None, sleep=None, timeout=90, max_retries=8,
                     token_path=None) -> ReadOnlyClient:
    """Build a ReadOnlyClient.

    token: None -> ``notion_tokens.load_read_token(token_path)``.
    rate: per-process cap in requests/s (None = no extra cap when a gate is given).
    gate: a ``rate_gate.RateGate``; when given, the client's limiter is ``cap -> gate``.
    """
    if token is None:
        tools = os.path.dirname(os.path.abspath(__file__))
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from notion_tokens import load_read_token
        token = load_read_token(token_path)
    kw = {"version": version or DEFAULT_VERSION, "label": label, "timeout": timeout,
          "max_retries": max_retries, "base_url": base_url or API, "session": session}
    if sleep is not None:
        kw["sleep"] = sleep
    client = ReadOnlyClient(token, rate=rate or 2.5, **kw)
    if gate is not None:
        client.limiter = _CapThenGate(RateLimiter(rate), gate) if rate else gate
    return client
