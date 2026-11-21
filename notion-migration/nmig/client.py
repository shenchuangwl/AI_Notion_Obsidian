"""Minimal, robust Notion REST client.

- Notion-Version defaults to 2026-03-11 (configurable).
- Token-bucket rate limiting (default 2.5 req/s, Notion's documented average is 3 req/s).
- Retries: 429 (honours Retry-After), 409 conflict, 5xx, network errors/timeouts,
  exponential backoff with jitter.
- Never logs the token or request bodies (bodies may contain private content).
"""
import json
import logging
import random
import threading
import time

import requests

API = "https://api.notion.com/v1"
log = logging.getLogger("nmig.client")


class NotionAPIError(Exception):
    def __init__(self, status, code, message, request_id=None, path=None):
        super().__init__(f"{status} {code}: {message} (path={path}, request_id={request_id})")
        self.status, self.code, self.message = status, code, message
        self.request_id, self.path = request_id, path


class RateLimiter:
    def __init__(self, rate=2.5, burst=3):
        self.rate, self.capacity = float(rate), float(burst)
        self.tokens, self.last = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


RETRY_STATUS = {409, 429, 500, 502, 503, 504}


class NotionClient:
    def __init__(self, token, version="2026-03-11", label="notion", rate=2.5,
                 max_retries=8, timeout=90, session=None, base_url=API, sleep=time.sleep):
        if not token:
            raise ValueError(f"{label}: token 未配置")
        self._token = token
        self.version, self.label = version, label
        self.max_retries, self.timeout = max_retries, timeout
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.limiter = RateLimiter(rate)
        self.sleep = sleep
        self.stats = {"requests": 0, "retries": 0, "errors": 0}

    def __repr__(self):  # never expose the token
        return f"<NotionClient {self.label} version={self.version}>"

    def _headers(self, json_body=True):
        h = {"Authorization": f"Bearer {self._token}", "Notion-Version": self.version}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, params=None, body=None, files=None, data=None):
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
                    data=json.dumps(body) if (body is not None and files is None) else data,
                    files=files, headers=self._headers(json_body=files is None),
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
                log.debug("%s %s %s -> %s %.2fs", self.label, method, path, resp.status_code, dt)
                return resp.json() if resp.content else {}
            try:
                err = resp.json()
            except ValueError:
                err = {"code": "http_error", "message": resp.text[:300]}
            code, msg = err.get("code", "unknown"), err.get("message", "")
            if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"{resp.status_code} {code}", path)
                continue
            self.stats["errors"] += 1
            log.warning("%s %s %s -> %s %s %s rid=%s", self.label, method, path, resp.status_code, code, msg[:200], rid)
            raise NotionAPIError(resp.status_code, code, msg, rid, path)

    def _backoff(self, attempt, retry_after, why, path):
        self.stats["retries"] += 1
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 5.0
        else:
            delay = min(60.0, (2 ** (attempt - 1)) * 0.8) * (0.5 + random.random())
        log.info("%s retry #%d in %.1fs (%s) %s", self.label, attempt, delay, why, path)
        self.sleep(delay)

    # ---------- pagination ----------
    def paginate(self, method, path, body=None, params=None, page_size=100):
        cursor = None
        while True:
            if method == "GET":
                p = dict(params or {}, page_size=page_size)
                if cursor:
                    p["start_cursor"] = cursor
                res = self.request("GET", path, params=p)
            else:
                b = dict(body or {}, page_size=page_size)
                if cursor:
                    b["start_cursor"] = cursor
                res = self.request(method, path, params=params, body=b)
            for item in res.get("results", []):
                yield item
            if not res.get("has_more"):
                return
            cursor = res.get("next_cursor")

    # ---------- convenience ----------
    def me(self):
        return self.request("GET", "users/me")

    def retrieve_page(self, pid):
        return self.request("GET", f"pages/{pid}")

    def retrieve_page_property(self, pid, prop_id):
        """Paginated property item (needed for relation/people/rich_text > 25 items)."""
        items, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            res = self.request("GET", f"pages/{pid}/properties/{prop_id}", params=params)
            if res.get("object") != "list":
                return res
            items.extend(res.get("results", []))
            if not res.get("has_more"):
                res["results"] = items
                return res
            cursor = res.get("next_cursor")

    def list_children(self, block_id):
        return list(self.paginate("GET", f"blocks/{block_id}/children"))

    def retrieve_block(self, bid):
        return self.request("GET", f"blocks/{bid}")

    def retrieve_database(self, did):
        return self.request("GET", f"databases/{did}")

    def retrieve_data_source(self, dsid):
        return self.request("GET", f"data_sources/{dsid}")

    def query_data_source(self, dsid, body=None):
        return list(self.paginate("POST", f"data_sources/{dsid}/query", body=body or {}))

    def list_views(self, database_id):
        return list(self.paginate("GET", "views", params={"database_id": database_id}))

    def list_comments(self, block_id):
        return list(self.paginate("GET", "comments", params={"block_id": block_id}))

    def search(self, query="", object_type=None):
        body = {"query": query}
        if object_type:
            body["filter"] = {"property": "object", "value": object_type}
        return list(self.paginate("POST", "search", body=body))

    def create_page(self, body):
        return self.request("POST", "pages", body=body)

    def update_page(self, pid, body):
        return self.request("PATCH", f"pages/{pid}", body=body)

    def append_children(self, block_id, children, after=None):
        body = {"children": children}
        if after:
            body["position"] = {"type": "after_block", "after_block": {"id": after}}
        return self.request("PATCH", f"blocks/{block_id}/children", body=body)

    def update_block(self, bid, body):
        return self.request("PATCH", f"blocks/{bid}", body=body)

    def trash_block(self, bid):
        return self.request("DELETE", f"blocks/{bid}")

    def create_database(self, body):
        return self.request("POST", "databases", body=body)

    def update_data_source(self, dsid, body):
        return self.request("PATCH", f"data_sources/{dsid}", body=body)

    def create_comment(self, body):
        return self.request("POST", "comments", body=body)

    # ---------- file uploads ----------
    def upload_bytes(self, filename, content, content_type="application/octet-stream"):
        """Single-part (<=20MB) or multi-part upload; returns file_upload id."""
        size = len(content)
        part = 10 * 1024 * 1024
        if size <= 20 * 1024 * 1024:
            fu = self.request("POST", "file_uploads", body={"mode": "single_part", "filename": filename,
                                                            "content_type": content_type})
            self.request("POST", f"file_uploads/{fu['id']}/send",
                         files={"file": (filename, content, content_type)})
            return fu["id"]
        n = (size + part - 1) // part
        fu = self.request("POST", "file_uploads", body={"mode": "multi_part", "number_of_parts": n,
                                                        "filename": filename, "content_type": content_type})
        for i in range(n):
            self.request("POST", f"file_uploads/{fu['id']}/send",
                         files={"file": (filename, content[i * part:(i + 1) * part], content_type)},
                         data={"part_number": str(i + 1)})
        self.request("POST", f"file_uploads/{fu['id']}/complete", body={})
        return fu["id"]
