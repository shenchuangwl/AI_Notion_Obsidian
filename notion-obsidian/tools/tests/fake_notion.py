#!/usr/bin/env python3
"""FakeNotion -- a realistic local HTTP fake of the Notion REST API for tests (contract §5.6).

Runs a threaded ``http.server`` on 127.0.0.1 (random port). Point any client at it with
``base_url=fake.start()`` (returns ``http://127.0.0.1:<port>/v1``). Never talks to the network.

Quick start
-----------
::

    fake = FakeNotion(tokens={
        "ro": {"caps": {"read", "user_info"}, "roots": {"*"}},          # bearer secret = "ro"
        "wb": {"caps": {"read", "update", "insert"}, "roots": {page}},  # page-access roots
    })
    base = fake.start()
    root = fake.add_page("Root")                                 # workspace-level page
    p = fake.add_page("Child", parent=root)                      # child_page block in Root
    ids = fake.add_blocks(p, [{"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "hi"}}]}},
                              {"type": "toggle", "toggle": {"rich_text": [], "children": [...]}}])
    db, ds = fake.add_database(p, "Tasks", {"Name": "title", "Tag": {"type": "select", "options": ["a"]}})
    row = fake.add_row(ds, "Row 1", {"Tag": {"select": {"name": "a"}}})
    tpl = fake.add_template(ds, "Template", is_default=True)
    fake.grant("wb", p)          # tokens can be changed at any time: grant/revoke/set_caps
    ...
    fake.stop()

Tokens
------
``tokens = {name: {"caps": set, "roots": set, "secret": str?, "bot_name": str?}}``. The bearer
secret defaults to the name. ``caps`` ⊆ {read, update, insert, read_comments, insert_comments,
user_info} ("all" = every capability). ``roots`` is a set of page/database ids the integration was
shared with; access is inherited by every descendant (child pages, blocks, inline databases,
data sources, rows, templates). ``"*"`` = whole workspace. Default when ``tokens`` is None:
``ro`` (read + user_info, all roots) and ``wb`` (read/update/insert, all roots).
Each token has its own bot user (``fake.bot_id(name)``) which becomes ``created_by`` /
``last_edited_by`` of everything it writes, and is returned by ``GET users/me``.

Errors follow the API: unknown token 401 ``unauthorized``; missing ``Notion-Version`` 400
``missing_version``; missing capability 403 ``restricted_resource``; object not shared or not
existing 404 ``object_not_found`` (``no_access="403"`` makes "not shared" a 403
``restricted_resource`` instead); bad ids / bodies 400 ``validation_error``; unknown routes 400
``invalid_request_url``; ``archived`` in a body 400 (Notion-Version 2026-03-11 only has ``in_trash``).

Realistic normalisation
-----------------------
* rich text: ``type`` inferred, ``annotations`` completed (bold/italic/strikethrough/underline/code
  false, color "default"), ``plain_text`` and ``href`` computed (mentions get the target title);
  a request item with text content > 2000 chars, > 100 items, unknown colors -> 400. On every API
  write, ADJACENT text segments with identical annotations and link are MERGED like live Notion
  (contract §7.2: ["same ", "same"] -> ["same same"]; plain 2000 + plain 500 -> one segment of
  2500), so read-backs can hold segments > 2000 chars. Seeded data (``add_*``) is stored unmerged,
  exactly as given.
* trash: ``DELETE blocks/{id}`` / ``in_trash: true`` hides an object from children, query and
  search; GET still returns it with ``in_trash: true``. A block restored with ``in_trash: false``
  does NOT return to its old slot (contract §7.6): it moves to the end of its parent's children
  (``FakeNotion(restore_position="end"|"start"|"original")``; "end" is the default -- note that a
  block that was already last therefore looks restored in place). A page below a trashed ancestor
  reports ``in_trash: true`` (contract §7.7) and false again once the ancestor is restored.
* blocks: ``object, id, parent, created_time, last_edited_time`` (minute precision,
  ``...:00.000Z``), ``created_by/last_edited_by`` (the writing token's bot), ``has_children``
  (computed, trashed children ignored), ``in_trash``; per-type defaults (``color``, to_do
  ``checked``, heading ``is_toggleable``, code ``caption``/``language`` (validated)).
  Page and database ids also appear as ``child_page`` / ``child_database`` blocks in their parent.
* pages: ``properties`` (rows follow the data-source schema with property ids; select options
  are auto-created like the real API, status options are not), ``parent``
  (``page_id``/``block_id``/``data_source_id``+``database_id``/``workspace``), ``in_trash``,
  ``is_locked``, ``url``, top-level ``request_id``; relation/people > 25 items are truncated with
  ``has_more: true`` (use ``GET pages/{id}/properties/{prop}``).
* pagination everywhere: ``page_size`` 1..100 (else 400), ``start_cursor`` = id of the next item.
* touching a block also bumps the owning page's ``last_edited_time``/``last_edited_by``.

Endpoints
---------
``GET users/me``, ``GET users`` (needs user_info), ``GET users/{id}``, ``GET|PATCH pages/{id}``,
``POST pages``, ``GET pages/{id}/properties/{prop}``, ``GET|PATCH|DELETE blocks/{id}``,
``GET|PATCH blocks/{id}/children`` (``position`` ``start``/``end``/``after_block``, legacy
``after``; ≤100 children, ≤2 nesting levels), ``POST search``, ``GET databases/{id}``,
``POST databases/{id}/query``, ``GET data_sources/{id}``, ``POST data_sources/{id}/query``
(filters: ``timestamp`` created_time/last_edited_time, ``and``/``or``, ``property`` equals for
title/rich_text/select/status/checkbox/number; sorts on timestamps; anything else -> 400),
``GET data_sources/{id}/templates``, ``GET views?database_id=``, ``GET comments?block_id=``
(needs read_comments).

Block limit
-----------
``fake.set_block_limit(n)``: the workspace may create ``n`` more blocks (a new page counts 1 plus
its children, recursively); a request that needs more gets 403 ``restricted_resource`` with
``additional_data.block_limit = "block_creation"`` and changes nothing. ``None`` = unlimited.

Fault injection
---------------
``fake.inject(method, path_regex, status=None, body=None, times=1, delay=None, after_commit=False,
*, retry_after=None, rate_limit_reason=None, guidance="retry_guidance", block_limit=False,
message=None, headers=None, drop=False, token=None)`` -> ``Fault`` (``.hits``).
``path_regex`` is searched in the relative path (``pages``, ``blocks/<id>/children``...).
Faults are tried in injection order; ``times=None`` = forever.

* ``after_commit=False``: the request is NOT processed; after ``delay`` the fault response is sent
  (``status`` None = process normally after the delay).
* ``after_commit=True``: the request IS processed (state changes), then after ``delay`` the fault
  response replaces the real one (``status`` None = the real response, just late -> use a
  delay longer than the client timeout to simulate "committed, then timed out").
* ``drop=True``: close the connection without any response (client sees a connection error).
* default bodies per status: 429 ``rate_limited`` (+ ``Retry-After`` = ``retry_after`` or 1,
  ``additional_data.rate_limit_reason``), 409 ``conflict_error``, 500, 502, 503
  ``service_unavailable``, 504, 529, 403 ``restricted_resource`` (``block_limit=True`` adds the
  block-limit marker), 400 ``validation_error``, 404 ``object_not_found``.
* 503 with ``after_commit=True``: the body carries the ids that were committed --
  ``committed_resource_id`` (POST pages) or ``committed_child_ids`` (append; for other writes use a
  ``body`` callable, e.g. ``committed_resource_id`` = ``ctx["committed_ids"][0]``) -- under
  ``additional_data.retry_guidance`` (``guidance="retry_guidance"``), directly under
  ``additional_data`` (``guidance="flat"``) or not at all (``guidance=None``).
* ``body`` may be a dict or ``callable(ctx) -> dict`` with ``ctx = {"method", "path", "result",
  "committed_ids"}``.
* ``fake.reject_notifications = True`` makes any body with ``notifications`` a 400.

Inspection
----------
``fake.page(id)``, ``fake.block(id)``, ``fake.children(id, include_trashed=False)``,
``fake.child_ids(id)``, ``fake.rows(ds)``, ``fake.database(id)``, ``fake.data_source(id)``,
``fake.requests`` (list of dicts: seq, t, method, path, query, body, token, version, status,
fault, committed), ``fake.writes()``, ``fake.count(method=None, path_regex=None)``,
``fake.clear_requests()``, ``fake.bot_id(token)``, ``fake.blocks_created``.
Use ``fake.now = lambda: datetime(...)`` to control timestamps.
"""
import copy
import datetime as _dt
import http.server
import json
import re
import socket
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

API_PREFIX = "/v1/"
ALL_CAPS = frozenset({"read", "update", "insert", "read_comments", "insert_comments", "user_info"})
ANN_DEFAULT = {"bold": False, "italic": False, "strikethrough": False, "underline": False,
               "code": False, "color": "default"}
_BASE_COLORS = ("default", "gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red")
COLORS = frozenset(_BASE_COLORS + tuple(c + "_background" for c in _BASE_COLORS[1:]) + ("default_background",))
CODE_LANGUAGES = frozenset("""abap|abc|agda|arduino|ascii art|assembly|bash|basic|bnf|c|c#|c++|clojure|coffeescript|coq|
css|dart|dhall|diff|docker|ebnf|elixir|elm|erlang|f#|flow|fortran|gherkin|glsl|go|graphql|groovy|haskell|hcl|html|idris|
java|javascript|json|julia|kotlin|latex|less|lisp|livescript|llvm ir|lua|makefile|markdown|markup|matlab|mathematica|
mermaid|nix|notion formula|objective-c|ocaml|pascal|perl|php|plain text|powershell|prolog|protobuf|purescript|python|
r|racket|reason|ruby|rust|sass|scala|scheme|scss|shell|smalltalk|solidity|sql|swift|toml|typescript|vb.net|verilog|
vhdl|visual basic|webassembly|xml|yaml|java/c/c++/c#""".replace("\n", "").split("|"))
TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
              "numbered_list_item", "quote", "to_do", "toggle", "callout")
MEDIA_TYPES = ("image", "file", "pdf", "video", "audio")
API_CREATABLE = frozenset(TEXT_TYPES + MEDIA_TYPES + (
    "code", "divider", "equation", "bookmark", "embed", "table", "table_row", "column_list", "column",
    "table_of_contents", "breadcrumb", "link_to_page", "synced_block"))
LIST_PROPS = ("title", "rich_text", "relation", "people")
READONLY_PROPS = frozenset({"formula", "rollup", "created_time", "created_by", "last_edited_time",
                            "last_edited_by", "unique_id", "verification", "button"})
_UUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
DROP = object()


def nid(value):
    """Dashed lowercase uuid or None."""
    if not isinstance(value, str) or not _UUID.match(value.strip()):
        return None
    h = value.strip().replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def new_id():
    return str(uuid.uuid4())


class FakeError(Exception):
    def __init__(self, status, code, message, additional_data=None, headers=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message
        self.additional_data, self.headers = additional_data, headers or {}


def _bad(msg):
    return FakeError(400, "validation_error", msg)


class Fault:
    def __init__(self, method, path_regex, status, body, times, delay, after_commit, headers, drop, token,
                 retry_after, rate_limit_reason, guidance, block_limit, message):
        self.method = (method or "*").upper()
        self.rx = re.compile(path_regex)
        self.status, self.body, self.times, self.delay = status, body, times, delay
        self.after_commit, self.headers, self.drop, self.token = after_commit, dict(headers or {}), drop, token
        self.retry_after, self.rate_limit_reason = retry_after, rate_limit_reason
        self.guidance, self.block_limit, self.message = guidance, block_limit, message
        self.hits = 0

    def active(self):
        return self.times is None or self.hits < self.times

    def matches(self, method, rel, token):
        return self.active() and self.method in ("*", method) and bool(self.rx.search(rel)) and \
            (self.token is None or self.token == token)

    def response(self, method, rel, result, committed):
        """(status, body, headers) of this fault."""
        st = self.status
        headers = dict(self.headers)
        ctx = {"method": method, "path": rel, "result": copy.deepcopy(result), "committed_ids": list(committed)}
        if callable(self.body):
            body = self.body(ctx)
        elif self.body is not None:
            body = copy.deepcopy(self.body)
        else:
            code, msg = {
                400: ("validation_error", "Fake validation error."),
                403: ("restricted_resource", "Insufficient permissions for this endpoint."),
                404: ("object_not_found", "Could not find object."),
                409: ("conflict_error", "Conflict occurred while saving. Please try again."),
                429: ("rate_limited", "You have been rate limited. Please try again in a few minutes."),
                500: ("internal_server_error", "Unexpected error occurred."),
                502: ("bad_gateway", "Bad gateway."),
                503: ("service_unavailable", "Notion is unavailable, please try again later."),
                504: ("gateway_timeout", "Notion upstream server timed out."),
                529: ("service_unavailable", "Notion is overloaded, please try again later."),
            }.get(st, ("fake_error", "Injected fault."))
            body = {"object": "error", "status": st, "code": code, "message": self.message or msg}
            ad = {}
            if st in (429, 529) and self.rate_limit_reason:
                ad["rate_limit_reason"] = self.rate_limit_reason
            if st == 403 and self.block_limit:
                ad["block_limit"] = "block_creation"
                body["message"] = self.message or "This workspace has reached its block limit."
            key = None
            if method == "POST" and rel == "pages":
                key = "committed_resource_id"
            elif method == "PATCH" and rel.endswith("/children"):
                key = "committed_child_ids"
            if st == 503 and self.after_commit and committed and self.guidance and key:
                val = committed[0] if key == "committed_resource_id" else list(committed)
                if self.guidance == "flat":
                    ad[key] = val
                else:
                    ad["retry_guidance"] = {key: val}
            if ad:
                body["additional_data"] = ad
        if st in (429, 529) and "Retry-After" not in headers:
            headers["Retry-After"] = str(self.retry_after if self.retry_after is not None else 1)
        return st, body, headers


class FakeNotion:
    def __init__(self, tokens=None, *, workspace_id="c893af62-39e0-4e16-bfa8-2ca081395c81",
                 workspace_name="Fake Workspace", no_access="404", now=None, restore_position="end"):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.workspace_id, self.workspace_name = workspace_id, workspace_name
        self.no_access = str(no_access)
        if restore_position not in ("end", "start", "original"):
            raise ValueError("restore_position must be 'end', 'start' or 'original'")
        self.restore_position = restore_position
        self.now = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self.pages, self.blocks, self.databases, self.data_sources = {}, {}, {}, {}
        self.kids, self.up = {}, {}
        self.users = {}
        self.comments = {}
        self.views = {}
        self.tokens = {}
        self.faults = []
        self._log = []
        self._seq = 0
        self.block_limit_remaining = None
        self.blocks_created = 0
        self.reject_notifications = False
        self._server = self._thread = None
        self._conns = set()
        self._prop_counter = 0
        if tokens is None:
            tokens = {"ro": {"caps": {"read", "user_info"}, "roots": {"*"}},
                      "wb": {"caps": {"read", "update", "insert"}, "roots": {"*"}}}
        for name, spec in tokens.items():
            self.add_token(name, **spec)

    # ================================================================== server lifecycle
    def start(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 15

            def setup(self):
                super().setup()
                with fake._lock:
                    fake._conns.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with fake._lock:
                        fake._conns.discard(self.connection)

            def log_message(self, *a):  # quiet
                pass

            def _go(self):
                fake._handle(self)

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _go

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):  # client went away etc.: quiet
                pass

        self._stop.clear()
        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-notion", daemon=True)
        self._thread.start()
        return self.base_url

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            with self._lock:
                conns = list(self._conns)
            for c in conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._server.server_close()
            self._thread.join(5)
            self._server = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ================================================================== tokens
    def add_token(self, name, caps=("read",), roots=("*",), secret=None, bot_name=None, bot_id=None):
        caps = set(caps or ())
        if "all" in caps:
            caps = set(ALL_CAPS)
        unknown = caps - ALL_CAPS
        if unknown:
            raise ValueError(f"unknown capabilities {unknown}")
        bid = bot_id or new_id()
        self.users[bid] = {"object": "user", "id": bid, "type": "bot", "name": bot_name or f"bot-{name}",
                           "avatar_url": None, "bot": {"owner": {"type": "workspace", "workspace": True},
                                                       "workspace_name": self.workspace_name}}
        self.tokens[secret or name] = {"name": name, "caps": caps, "roots": self._roots(roots), "bot_id": bid}
        return secret or name

    @staticmethod
    def _roots(roots):
        return {r if r == "*" else nid(r) for r in (roots or ())}

    def _tok(self, name):
        for t in self.tokens.values():
            if t["name"] == name:
                return t
        raise KeyError(name)

    def secret(self, name):
        return next(s for s, t in self.tokens.items() if t["name"] == name)

    def bot_id(self, name):
        return self._tok(name)["bot_id"]

    def grant(self, name, *ids):
        with self._lock:
            self._tok(name)["roots"] |= self._roots(ids)

    def revoke(self, name, *ids):
        with self._lock:
            self._tok(name)["roots"] -= self._roots(ids)

    def set_caps(self, name, caps):
        with self._lock:
            caps = set(caps)
            self._tok(name)["caps"] = set(ALL_CAPS) if "all" in caps else caps

    # ================================================================== faults
    def inject(self, method, path_regex, status=None, body=None, times=1, delay=None, after_commit=False, *,
               headers=None, retry_after=None, rate_limit_reason=None, guidance="retry_guidance",
               block_limit=False, message=None, drop=False, token=None):
        f = Fault(method, path_regex, status, body, times, delay, after_commit, headers, drop, token,
                  retry_after, rate_limit_reason, guidance, block_limit, message)
        with self._lock:
            self.faults.append(f)
        return f

    def clear_faults(self):
        with self._lock:
            self.faults.clear()

    def set_block_limit(self, remaining=None):
        with self._lock:
            self.block_limit_remaining = remaining

    # ================================================================== request log
    @property
    def requests(self):
        with self._lock:
            return copy.deepcopy(self._log)

    def writes(self):
        out = []
        for r in self.requests:
            if r["method"] == "GET":
                continue
            if r["method"] == "POST" and re.fullmatch(r"search|(data_sources|databases)/[^/]+/query", r["path"]):
                continue
            out.append(r)
        return out

    def count(self, method=None, path_regex=None, token=None):
        rx = re.compile(path_regex) if path_regex else None
        return sum(1 for r in self.requests if (method is None or r["method"] == method.upper())
                   and (rx is None or rx.search(r["path"])) and (token is None or r["token"] == token))

    def clear_requests(self):
        with self._lock:
            self._log.clear()

    # ================================================================== time / helpers
    def _ts(self):
        t = self.now()
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        t = t.astimezone(_dt.timezone.utc).replace(second=0, microsecond=0)
        return t.strftime("%Y-%m-%dT%H:%M:00.000Z")

    @staticmethod
    def _user_ref(uid):
        return {"object": "user", "id": uid}

    def _kind(self, oid):
        if oid in self.pages:
            return "page"
        if oid in self.databases:
            return "database"
        if oid in self.data_sources:
            return "data_source"
        if oid in self.blocks:
            return "block"
        return None

    def _parent_ref(self, oid):
        p = self.up.get(oid)
        if p is None:
            return {"type": "workspace", "workspace": True}
        k = self._kind(p)
        if k == "page":
            return {"type": "page_id", "page_id": p}
        if k == "block":
            return {"type": "block_id", "block_id": p}
        if k == "data_source":
            return {"type": "data_source_id", "data_source_id": p, "database_id": self.data_sources[p]["database_id"]}
        if k == "database":
            return {"type": "database_id", "database_id": p}
        return {"type": "workspace", "workspace": True}

    def _owning_page(self, oid):
        x = self.up.get(oid)
        while x is not None and x not in self.pages:
            x = self.up.get(x)
        return x

    def _trashed_chain(self, oid):
        x = oid
        while x is not None:
            obj = self.pages.get(x) or self.blocks.get(x) or self.databases.get(x)
            if obj is not None and obj.get("in_trash"):
                return True
            x = self.up.get(x)
        return False

    def _accessible(self, tok, oid):
        roots = tok["roots"]
        if "*" in roots:
            return True
        x = oid
        while x is not None:
            if x in roots:
                return True
            x = self.up.get(x)
        return False

    def _not_found(self, oid, what="block"):
        return FakeError(404, "object_not_found",
                         f"Could not find {what} with ID: {oid}. Make sure the relevant pages and databases "
                         f"are shared with your integration.")

    def _need(self, tok, oid, what, kinds):
        """Existence + access check; returns normalised id."""
        i = nid(oid)
        if i is None:
            raise _bad(f"path failed validation: path.{what}_id should be a valid uuid, instead was `{oid}`.")
        if self._kind(i) not in kinds:
            raise self._not_found(i, what)
        if not self._accessible(tok, i):
            if self.no_access == "403":
                raise FakeError(403, "restricted_resource", "Insufficient permissions for this resource.")
            raise self._not_found(i, what)
        return i

    @staticmethod
    def _cap(tok, cap):
        if cap not in tok["caps"]:
            raise FakeError(403, "restricted_resource", "Insufficient permissions for this endpoint.")

    def _title_of(self, oid):
        if oid in self.pages:
            for p in self.pages[oid]["properties"].values():
                if p.get("type") == "title":
                    return "".join(x.get("plain_text", "") for x in p.get("title") or [])
            return ""
        if oid in self.databases:
            return "".join(x.get("plain_text", "") for x in self.databases[oid]["title"])
        if oid in self.data_sources:
            return self.data_sources[oid]["name"]
        return ""

    # ================================================================== normalisation
    @staticmethod
    def _merge_rt(items):
        """Live-measured (contract §7.2): Notion merges ADJACENT text segments with identical
        annotations and link into one segment (no length limit: 2000 + 500 -> one of 2500).
        Mentions and equations are never merged."""
        out = []
        for it in items:
            prev = out[-1] if out else None
            if prev is not None and prev["type"] == "text" and it["type"] == "text" and \
                    prev["annotations"] == it["annotations"] and prev["text"]["link"] == it["text"]["link"]:
                prev["text"]["content"] += it["text"]["content"]
                prev["plain_text"] += it["plain_text"]
                continue
            out.append(it)
        return out

    def _norm_rt(self, items, where="rich_text", merge=True):
        """Validate + normalise a rich_text array. Limits (≤100 items, ≤2000 chars per text
        content) apply to the REQUEST items; with ``merge`` (every API write) adjacent identical
        text segments are then merged like live Notion, so stored/read-back segments can exceed
        2000 chars. Seeding passes ``merge=False``: seeded data is stored as given."""
        if items is None:
            return []
        if not isinstance(items, list):
            raise _bad(f"{where} should be an array")
        if len(items) > 100:
            raise _bad(f"{where}.length should be ≤ `100`, instead was `{len(items)}`.")
        out = []
        for n, it in enumerate(items):
            if not isinstance(it, dict):
                raise _bad(f"{where}[{n}] should be an object")
            t = it.get("type") or next((k for k in ("text", "mention", "equation") if k in it), None)
            if t == "text":
                txt = it.get("text") or {}
                content = txt.get("content")
                if not isinstance(content, str):
                    raise _bad(f"{where}[{n}].text.content should be a string")
                if len(content) > 2000:
                    raise _bad(f"{where}[{n}].text.content.length should be ≤ `2000`, instead was `{len(content)}`.")
                link = txt.get("link")
                if link is not None:
                    if not isinstance(link, dict) or not isinstance(link.get("url"), str) or not link["url"]:
                        raise _bad(f"{where}[{n}].text.link.url should be a non-empty string")
                    link = {"url": link["url"]}
                obj = {"type": "text", "text": {"content": content, "link": link}}
                plain, href = content, (link or {}).get("url")
            elif t == "mention":
                m = dict(it.get("mention") or {})
                mt = m.get("type") or next((k for k in m if k != "type"), None)
                if mt not in ("user", "page", "database", "date", "link_preview", "template_mention",
                              "link_mention", "custom_emoji"):
                    raise _bad(f"{where}[{n}].mention type `{mt}` is not supported")
                m["type"] = mt
                href = None
                if mt == "user":
                    u = self.users.get(nid((m.get("user") or {}).get("id")) or "")
                    plain = "@" + (u["name"] if u else "Anonymous")
                elif mt in ("page", "database"):
                    target = nid((m.get(mt) or {}).get("id"))
                    if target is None:
                        raise _bad(f"{where}[{n}].mention.{mt}.id should be a valid uuid")
                    m[mt] = {"id": target}
                    plain = self._title_of(target) or "Untitled"
                    href = "https://www.notion.so/" + target.replace("-", "")
                elif mt == "date":
                    plain = str((m.get("date") or {}).get("start") or "")
                else:
                    plain = str((m.get(mt) or {}).get("url") or mt)
                    href = (m.get(mt) or {}).get("url")
                if "plain_text" in it and isinstance(it["plain_text"], str):
                    plain = it["plain_text"]
                obj = {"type": "mention", "mention": m}
            elif t == "equation":
                expr = (it.get("equation") or {}).get("expression")
                if not isinstance(expr, str):
                    raise _bad(f"{where}[{n}].equation.expression should be a string")
                obj = {"type": "equation", "equation": {"expression": expr}}
                plain, href = expr, None
            else:
                raise _bad(f"{where}[{n}] should define `text`, `mention` or `equation`")
            ann = dict(ANN_DEFAULT)
            for k, v in (it.get("annotations") or {}).items():
                if k not in ANN_DEFAULT:
                    raise _bad(f"{where}[{n}].annotations.{k} should be not present")
                if k == "color":
                    if v not in COLORS:
                        raise _bad(f"{where}[{n}].annotations.color `{v}` is not a valid color")
                elif not isinstance(v, bool):
                    raise _bad(f"{where}[{n}].annotations.{k} should be a boolean")
                ann[k] = v
            obj["annotations"] = ann
            obj["plain_text"] = plain
            obj["href"] = href
            out.append(obj)
        return self._merge_rt(out) if merge else out

    def _block_type(self, spec):
        if not isinstance(spec, dict):
            raise _bad("block should be an object")
        t = spec.get("type") or next((k for k in spec if k not in ("object", "id", "children", "has_children",
                                                                     "parent", "created_time", "last_edited_time",
                                                                     "created_by", "last_edited_by", "in_trash",
                                                                     "archived")), None)
        if not t:
            raise _bad("block type could not be determined")
        return t

    def _norm_content(self, t, content, where, seeding):
        c = copy.deepcopy(content or {})
        if not isinstance(c, dict):
            raise _bad(f"{where}.{t} should be an object")
        merge = not seeding
        if t in TEXT_TYPES or t == "code":
            c["rich_text"] = self._norm_rt(c.get("rich_text"), f"{where}.{t}.rich_text", merge)
        if t in TEXT_TYPES:
            color = c.setdefault("color", "default")
            if color not in COLORS:
                raise _bad(f"{where}.{t}.color `{color}` is not a valid color")
        if t == "to_do":
            c.setdefault("checked", False)
            if not isinstance(c["checked"], bool):
                raise _bad(f"{where}.to_do.checked should be a boolean")
        if t.startswith("heading_"):
            c.setdefault("is_toggleable", False)
        if t == "callout":
            c.setdefault("icon", {"type": "emoji", "emoji": "💡"})
        if t == "code":
            c["caption"] = self._norm_rt(c.get("caption"), f"{where}.code.caption", merge)
            c.setdefault("language", "plain text")
            if c["language"] not in CODE_LANGUAGES:
                raise _bad(f"{where}.code.language should be a valid language, instead was `{c['language']}`.")
        if t == "equation" and not isinstance(c.get("expression"), str):
            raise _bad(f"{where}.equation.expression should be a string")
        if t in MEDIA_TYPES + ("bookmark", "embed"):
            if "caption" in c or not seeding:
                c["caption"] = self._norm_rt(c.get("caption"), f"{where}.{t}.caption", merge)
        if t == "table_row":
            c["cells"] = [self._norm_rt(cell, f"{where}.table_row.cells", merge) for cell in c.get("cells") or []]
        return c

    def _count_blocks(self, specs, depth=1):
        n = 0
        for s in specs or []:
            t = self._block_type(s)
            n += 1
            inner = (s.get(t) or {}).get("children") if isinstance(s.get(t), dict) else None
            n += self._count_blocks(inner if inner is not None else s.get("children"), depth + 1)
        return n

    def _validate_specs(self, specs, where, depth=1):
        if not isinstance(specs, list):
            raise _bad(f"{where} should be an array")
        if len(specs) > 100:
            raise _bad(f"{where}.length should be ≤ `100`, instead was `{len(specs)}`.")
        if depth > 3:
            raise _bad(f"{where}: too many levels of nesting (at most two levels can be appended per request)")
        for n, s in enumerate(specs):
            t = self._block_type(s)
            if t not in API_CREATABLE:
                raise _bad(f"{where}[{n}].{t} is not a supported block type for creation")
            inner = (s.get(t) or {}).get("children") if isinstance(s.get(t), dict) else None
            if inner is None:
                inner = s.get("children")
            if inner:
                self._validate_specs(inner, f"{where}[{n}].{t}.children", depth + 1)

    def _charge_blocks(self, n):
        if self.block_limit_remaining is not None:
            if n > self.block_limit_remaining:
                raise FakeError(403, "restricted_resource", "This workspace has reached its block limit.",
                                additional_data={"block_limit": "block_creation"})
            self.block_limit_remaining -= n
        self.blocks_created += n

    def _insert_child(self, parent, cid, index=None):
        lst = self.kids.setdefault(parent, [])
        if index is None:
            lst.append(cid)
        else:
            lst.insert(index, cid)
        self.up[cid] = parent

    def _create_blocks(self, parent, specs, by, *, seeding, index=None, where="children"):
        """Create blocks (recursively) under parent; returns top-level ids in order."""
        ids = []
        ts = self._ts()
        for n, s in enumerate(specs or []):
            t = self._block_type(s)
            content = s.get(t) if isinstance(s.get(t), dict) else {}
            nested = content.get("children") if isinstance(content, dict) else None
            if nested is None:
                nested = s.get("children")
            content = {k: v for k, v in (content or {}).items() if k != "children"}
            if seeding and t == "child_page":
                pid = self.add_page((content or {}).get("title", "Untitled"), parent, id=s.get("id"),
                                    children=nested, _index=None if index is None else index + n)
                ids.append(pid)
                continue
            if seeding and t == "child_database":
                did, _ = self.add_database(parent, (content or {}).get("title", "Database"), id=s.get("id"),
                                           _index=None if index is None else index + n)
                ids.append(did)
                continue
            bid = (nid(s.get("id")) if seeding and s.get("id") else None) or new_id()
            if bid in self.blocks or bid in self.pages:
                raise ValueError(f"duplicate id {bid}")
            block = {"object": "block", "id": bid, "type": t,
                     t: self._norm_content(t, content, f"{where}[{n}]", seeding),
                     "created_time": s.get("created_time") if seeding and s.get("created_time") else ts,
                     "last_edited_time": s.get("last_edited_time") if seeding and s.get("last_edited_time") else ts,
                     "created_by": self._user_ref(by), "last_edited_by": self._user_ref(by),
                     "in_trash": bool(s.get("in_trash", False)) if seeding else False}
            self.blocks[bid] = block
            self._insert_child(parent, bid, None if index is None else index + n)
            ids.append(bid)
            if nested:
                self._create_blocks(bid, nested, by, seeding=seeding, where=f"{where}[{n}].{t}.children")
        return ids

    # ================================================================== schema / properties
    def _new_prop_id(self):
        self._prop_counter += 1
        return f"%3A{self._prop_counter:03d}"

    def _norm_schema(self, props):
        props = props or {"Name": "title"}
        out = {}
        for name, spec in props.items():
            if isinstance(spec, str):
                spec = {"type": spec}
            spec = dict(spec)
            t = spec.get("type")
            cfg = spec.get(t) if isinstance(spec.get(t), dict) else {}
            if t in ("select", "multi_select", "status"):
                opts = spec.get("options", cfg.get("options", []))
                cfg = {"options": [o if isinstance(o, dict) else {"name": o} for o in opts]}
                for o in cfg["options"]:
                    o.setdefault("id", new_id()[:8])
                    o.setdefault("color", "default")
            pid = spec.get("id") or ("title" if t == "title" else self._new_prop_id())
            out[name] = {"id": pid, "name": name, "type": t, t: cfg}
        if sum(1 for p in out.values() if p["type"] == "title") != 1:
            raise ValueError("a data source schema needs exactly one title property")
        return out

    def _empty_value(self, sprop, page):
        t = sprop["type"]
        v = {"title": [], "rich_text": [], "select": None, "multi_select": [], "status": None, "number": None,
             "checkbox": False, "date": None, "url": None, "email": None, "phone_number": None, "relation": [],
             "people": [], "files": [], "formula": {"type": "string", "string": None},
             "rollup": {"type": "array", "array": [], "function": "show_original"},
             "created_time": page.get("created_time"), "last_edited_time": page.get("last_edited_time"),
             "created_by": page.get("created_by"), "last_edited_by": page.get("last_edited_by"),
             "unique_id": {"prefix": None, "number": None}, "verification": None, "button": {}}.get(t)
        return {"id": sprop["id"], "type": t, t: copy.deepcopy(v)}

    def _norm_value(self, sprop, value, ds, *, seeding):
        t = sprop["type"]
        name = sprop.get("name", t)
        if not isinstance(value, dict):
            raise _bad(f"body.properties.{name} should be an object")
        if t in READONLY_PROPS and not seeding:
            raise _bad(f"{name} is a {t} property and cannot be updated.")
        if t not in value and not seeding:
            raise _bad(f"body.properties.{name}.{t} should be defined, instead was `undefined`.")
        v = value.get(t)
        if t in ("title", "rich_text"):
            v = self._norm_rt(v, f"body.properties.{name}.{t}", merge=not seeding)
        elif t in ("select", "status"):
            if v is not None:
                if not isinstance(v, dict) or not isinstance(v.get("name"), str) or (t == "select" and "," in v["name"]):
                    raise _bad(f"body.properties.{name}.{t}.name should be a string without commas")
                opts = (ds["properties"][name][t] if ds and name in ds["properties"] else {}).setdefault("options", [])
                hit = next((o for o in opts if o["name"] == v["name"]), None)
                if hit is None:
                    if t == "status" and not seeding:
                        raise _bad(f"Invalid status option. Status option \"{v['name']}\" does not exist.")
                    hit = {"id": new_id()[:8], "name": v["name"], "color": "default"}
                    opts.append(hit)
                v = dict(hit)
        elif t == "multi_select":
            res = []
            opts = (ds["properties"][name][t] if ds and name in ds["properties"] else {}).setdefault("options", [])
            for o in v or []:
                if not isinstance(o, dict) or not isinstance(o.get("name"), str) or "," in o["name"]:
                    raise _bad(f"body.properties.{name}.multi_select names should be strings without commas")
                hit = next((x for x in opts if x["name"] == o["name"]), None)
                if hit is None:
                    hit = {"id": new_id()[:8], "name": o["name"], "color": "default"}
                    opts.append(hit)
                res.append(dict(hit))
            v = res
        elif t == "number":
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise _bad(f"body.properties.{name}.number should be a number or null")
        elif t == "checkbox":
            if not isinstance(v, bool):
                raise _bad(f"body.properties.{name}.checkbox should be a boolean")
        elif t == "date":
            if v is not None and (not isinstance(v, dict) or not isinstance(v.get("start"), str)):
                raise _bad(f"body.properties.{name}.date.start should be a string")
            if v is not None:
                v = {"start": v["start"], "end": v.get("end"), "time_zone": v.get("time_zone")}
        elif t in ("url", "email", "phone_number"):
            if v is not None and not isinstance(v, str):
                raise _bad(f"body.properties.{name}.{t} should be a string or null")
        elif t == "relation":
            v = [{"id": nid(r.get("id")) or r.get("id")} for r in v or []]
        elif t in ("people", "files"):
            v = list(v or [])
        return {"id": sprop["id"], "type": t, t: copy.deepcopy(v)}

    def _page_schema(self, pid):
        ds_id = self.up.get(pid)
        if ds_id in self.data_sources:
            return self.data_sources[ds_id]
        return None

    def _set_properties(self, page, props, *, seeding):
        ds = self._page_schema(page["id"]) if page["id"] in self.up else None
        for key, value in (props or {}).items():
            if ds is not None:
                sname = key if key in ds["properties"] else next(
                    (n for n, p in ds["properties"].items() if p["id"] == key), None)
                if sname is None:
                    raise _bad(f"{key} is not a property that exists.")
                page["properties"][sname] = self._norm_value(ds["properties"][sname], value, ds, seeding=seeding)
            else:
                sname = next((n for n, p in page["properties"].items() if n == key or p.get("id") == key), None)
                if sname is None or page["properties"][sname]["type"] != "title":
                    raise _bad(f"{key} is not a property that exists. Pages that are not in a data source "
                               f"can only have a title property.")
                page["properties"][sname] = self._norm_value(
                    {"id": "title", "name": sname, "type": "title"}, value, None, seeding=seeding)

    # ================================================================== seeding API
    def add_user(self, name="User", *, type="person", id=None, email=None):
        uid = nid(id) if id else new_id()
        u = {"object": "user", "id": uid, "type": type, "name": name, "avatar_url": None}
        if type == "person":
            u["person"] = {"email": email} if email else {}
        else:
            u["bot"] = {}
        with self._lock:
            self.users[uid] = u
        return uid

    def _default_creator(self, created_by):
        if created_by:
            return nid(created_by) or created_by
        persons = [u for u in self.users.values() if u["type"] == "person"]
        return persons[0]["id"] if persons else next(iter(self.users.values()))["id"]

    def add_page(self, title="Untitled", parent=None, *, id=None, children=None, properties=None, icon=None,
                 cover=None, in_trash=False, is_locked=False, created_by=None, _index=None):
        """Seed a page. parent None = workspace level; a page/block id = child_page block appended there."""
        with self._lock:
            pid = nid(id) if id else new_id()
            if pid in self.pages or pid in self.blocks:
                raise ValueError(f"duplicate id {pid}")
            by = self._default_creator(created_by)
            ts = self._ts()
            page = {"object": "page", "id": pid, "created_time": ts, "last_edited_time": ts,
                    "created_by": self._user_ref(by), "last_edited_by": self._user_ref(by),
                    "cover": cover, "icon": icon, "in_trash": bool(in_trash), "is_locked": bool(is_locked),
                    "properties": {"title": {"id": "title", "type": "title",
                                             "title": self._norm_rt([{"text": {"content": title}}])}}}
            self.pages[pid] = page
            if parent is not None:
                p = nid(parent)
                if self._kind(p) not in ("page", "block"):
                    raise ValueError(f"unknown parent {parent}")
                self._insert_child(p, pid, _index)
            else:
                self.up[pid] = None
            if properties:
                self._set_properties(page, properties, seeding=True)
            if children:
                self._create_blocks(pid, children, by, seeding=True)
            return pid

    def add_blocks(self, parent_id, blocks, *, after=None, created_by=None):
        """Seed blocks under a page/block (nested children allowed, also child_page/child_database
        specs). ``after`` = id of an existing sibling to insert after. Returns top-level ids."""
        with self._lock:
            p = nid(parent_id)
            if self._kind(p) not in ("page", "block"):
                raise ValueError(f"unknown parent {parent_id}")
            index = None
            if after is not None:
                index = self.kids.get(p, []).index(nid(after)) + 1
            return self._create_blocks(p, blocks, self._default_creator(created_by), seeding=True, index=index)

    def add_database(self, parent_id, title="Database", properties=None, *, id=None, data_source_id=None,
                     inline=False, data_source=True, in_trash=False, created_by=None, _index=None):
        """Seed a database (child_database block in parent) and, by default, one data source.
        Returns (database_id, data_source_id_or_None)."""
        with self._lock:
            did = nid(id) if id else new_id()
            by = self._default_creator(created_by)
            ts = self._ts()
            self.databases[did] = {"object": "database", "id": did,
                                   "title": self._norm_rt([{"text": {"content": title}}]),
                                   "description": [], "is_inline": bool(inline), "in_trash": bool(in_trash),
                                   "is_locked": False, "icon": None, "cover": None,
                                   "created_time": ts, "last_edited_time": ts,
                                   "created_by": self._user_ref(by), "last_edited_by": self._user_ref(by),
                                   "data_sources": []}
            if parent_id is None:
                self.up[did] = None
            else:
                p = nid(parent_id)
                if self._kind(p) not in ("page", "block"):
                    raise ValueError(f"unknown parent {parent_id}")
                self._insert_child(p, did, _index)
            ds = self.add_data_source(did, title, properties, id=data_source_id) if data_source else None
            return did, ds

    def add_data_source(self, database_id, name=None, properties=None, *, id=None):
        with self._lock:
            did = nid(database_id)
            dsid = nid(id) if id else new_id()
            ts = self._ts()
            self.data_sources[dsid] = {"id": dsid, "name": name or self._title_of(did), "database_id": did,
                                       "properties": self._norm_schema(properties), "rows": [], "templates": [],
                                       "created_time": ts, "last_edited_time": ts}
            self.up[dsid] = did
            self.databases[did]["data_sources"].append({"id": dsid, "name": name or self._title_of(did)})
            self.views.setdefault(did, [{"object": "view", "id": new_id(), "name": "Default view",
                                         "type": "table", "database_id": did}])
            return dsid

    def _new_row(self, dsid, title, properties, id, children, in_trash, created_by):
        ds = self.data_sources[nid(dsid)]
        rid = nid(id) if id else new_id()
        by = self._default_creator(created_by)
        ts = self._ts()
        page = {"object": "page", "id": rid, "created_time": ts, "last_edited_time": ts,
                "created_by": self._user_ref(by), "last_edited_by": self._user_ref(by), "cover": None,
                "icon": None, "in_trash": bool(in_trash), "is_locked": False, "properties": {}}
        self.pages[rid] = page
        self.up[rid] = ds["id"]
        for name, sprop in ds["properties"].items():
            page["properties"][name] = self._empty_value(sprop, page)
        props = dict(properties or {})
        if title is not None:
            tname = next(n for n, p in ds["properties"].items() if p["type"] == "title")
            props[tname] = {"title": [{"text": {"content": title}}]}
        self._set_properties(page, props, seeding=True)
        if children:
            self._create_blocks(rid, children, by, seeding=True)
        return rid

    def add_row(self, data_source_id, title=None, properties=None, *, id=None, children=None, in_trash=False,
                created_by=None):
        with self._lock:
            rid = self._new_row(data_source_id, title, properties, id, children, in_trash, created_by)
            self.data_sources[nid(data_source_id)]["rows"].append(rid)
            return rid

    def add_template(self, data_source_id, name="Template", *, is_default=False, id=None, children=None):
        with self._lock:
            tid = self._new_row(data_source_id, name, None, id, children, False, None)
            self.data_sources[nid(data_source_id)]["templates"].append(
                {"id": tid, "name": name, "is_default": bool(is_default)})
            return tid

    def add_comment(self, block_id, text):
        with self._lock:
            cid = new_id()
            self.comments.setdefault(nid(block_id), []).append(
                {"object": "comment", "id": cid, "parent": {"type": "page_id", "page_id": nid(block_id)},
                 "rich_text": self._norm_rt([{"text": {"content": text}}]), "created_time": self._ts()})
            return cid

    # ================================================================== rendering
    def _page_in_trash(self, pid):
        """Live-measured (contract §7.7, probe P8): a page under a trashed ancestor reports
        ``in_trash: true``; restoring the ancestor makes it false again. The stored flag is the
        page's own and stays unchanged (only the rendering inherits)."""
        return bool(self.pages[pid].get("in_trash")) or self._trashed_chain(self.up.get(pid))

    def _render_page(self, pid, truncate=True):
        p = copy.deepcopy(self.pages[pid])
        p["in_trash"] = self._page_in_trash(pid)
        p["parent"] = self._parent_ref(pid)
        p["url"] = f"https://www.notion.so/{pid.replace('-', '')}"
        p["public_url"] = None
        if truncate:
            for prop in p["properties"].values():
                t = prop.get("type")
                lim = 25 if t in ("relation", "people") else 100 if t in ("title", "rich_text") else None
                if lim and isinstance(prop.get(t), list) and len(prop[t]) > lim:
                    prop[t] = prop[t][:lim]
                    prop["has_more"] = True
                elif t in ("relation", "people"):
                    prop["has_more"] = False
        return p

    def _visible_kids(self, oid, include_trashed=False):
        out = []
        for c in self.kids.get(oid, []):
            obj = self.pages.get(c) or self.databases.get(c) or self.blocks.get(c)
            if obj is not None and (include_trashed or not obj.get("in_trash")):
                out.append(c)
        return out

    def _render_block(self, bid):
        if bid in self.pages:
            pg = self.pages[bid]
            return {"object": "block", "id": bid, "parent": self._parent_ref(bid),
                    "created_time": pg["created_time"], "last_edited_time": pg["last_edited_time"],
                    "created_by": copy.deepcopy(pg["created_by"]), "last_edited_by": copy.deepcopy(pg["last_edited_by"]),
                    "has_children": bool(self._visible_kids(bid)), "in_trash": self._page_in_trash(bid),
                    "type": "child_page", "child_page": {"title": self._title_of(bid)}}
        if bid in self.databases:
            d = self.databases[bid]
            return {"object": "block", "id": bid, "parent": self._parent_ref(bid),
                    "created_time": d["created_time"], "last_edited_time": d["last_edited_time"],
                    "created_by": copy.deepcopy(d["created_by"]), "last_edited_by": copy.deepcopy(d["last_edited_by"]),
                    "has_children": False, "in_trash": d["in_trash"],
                    "type": "child_database", "child_database": {"title": self._title_of(bid)}}
        b = copy.deepcopy(self.blocks[bid])
        b["parent"] = self._parent_ref(bid)
        b["has_children"] = bool(self._visible_kids(bid))
        return b

    def _render_database(self, did):
        d = copy.deepcopy(self.databases[did])
        d["parent"] = self._parent_ref(did)
        d["url"] = f"https://www.notion.so/{did.replace('-', '')}"
        d["public_url"] = None
        return d

    def _render_ds(self, dsid):
        ds = self.data_sources[dsid]
        db = self.databases[ds["database_id"]]
        return {"object": "data_source", "id": dsid, "title": self._norm_rt([{"text": {"content": ds["name"]}}]),
                "description": [], "parent": {"type": "database_id", "database_id": ds["database_id"]},
                "database_parent": self._parent_ref(ds["database_id"]), "is_inline": db["is_inline"],
                "in_trash": db["in_trash"], "created_time": ds["created_time"],
                "last_edited_time": ds["last_edited_time"], "properties": copy.deepcopy(ds["properties"]),
                "icon": None, "cover": None, "url": f"https://www.notion.so/{dsid.replace('-', '')}"}

    # ================================================================== inspection (no access checks)
    def page(self, pid):
        with self._lock:
            return self._render_page(nid(pid), truncate=False)

    def block(self, bid):
        with self._lock:
            return self._render_block(nid(bid))

    def child_ids(self, parent_id, include_trashed=False):
        with self._lock:
            return list(self._visible_kids(nid(parent_id), include_trashed))

    def children(self, parent_id, include_trashed=False):
        with self._lock:
            return [self._render_block(c) for c in self._visible_kids(nid(parent_id), include_trashed)]

    def rows(self, data_source_id, include_trashed=False):
        with self._lock:
            ds = self.data_sources[nid(data_source_id)]
            return [self._render_page(r, truncate=False) for r in ds["rows"]
                    if include_trashed or not self.pages[r]["in_trash"]]

    def database(self, did):
        with self._lock:
            return self._render_database(nid(did))

    def data_source(self, dsid):
        with self._lock:
            return self._render_ds(nid(dsid))

    # ================================================================== HTTP plumbing
    def _handle(self, h):
        t0 = time.time()
        url = urlsplit(h.path)
        method = h.command.upper()
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length > 0 else b""
        auth = h.headers.get("Authorization") or ""
        secret = auth[7:] if auth.startswith("Bearer ") else None
        tok = self.tokens.get(secret) if secret else None
        rel = url.path[len(API_PREFIX):] if url.path.startswith(API_PREFIX) else None
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body, body_err = None, None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body_err = FakeError(400, "invalid_json", "Error parsing JSON body.")
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "t": t0, "method": method, "path": rel if rel is not None else url.path,
                     "query": query, "body": copy.deepcopy(body), "token": tok["name"] if tok else None,
                     "version": h.headers.get("Notion-Version"), "status": None, "fault": False,
                     "committed": False}
            self._log.append(entry)
            fault = None
            if rel is not None and tok is not None:
                fault = next((f for f in self.faults if f.matches(method, rel, tok["name"])), None)
                if fault is not None:
                    fault.hits += 1
                    entry["fault"] = True
        status, payload, headers = None, None, {}
        if fault is not None and not fault.after_commit:
            if fault.delay:
                self._stop.wait(fault.delay)
            if fault.drop:
                return self._drop(h, entry)
            if fault.status is not None:
                status, payload, headers = fault.response(method, rel, None, [])
        if status is None:
            try:
                if body_err is not None:
                    raise body_err
                with self._lock:
                    result, committed = self._dispatch(method, rel, query, body, tok, h.headers)
                    entry["committed"] = committed is not None
                status, payload = 200, result
            except FakeError as e:
                committed = None
                status = e.status
                payload = {"object": "error", "status": e.status, "code": e.code, "message": e.message}
                if e.additional_data:
                    payload["additional_data"] = e.additional_data
                headers = dict(e.headers)
            if fault is not None and fault.after_commit:
                if fault.delay:
                    self._stop.wait(fault.delay)
                if fault.drop:
                    return self._drop(h, entry)
                if fault.status is not None and status == 200:
                    status, payload, headers = fault.response(method, rel, payload, committed or [])
        if isinstance(payload, dict):
            payload.setdefault("request_id", new_id())
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self._lock:
            entry["status"] = status
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json; charset=utf-8")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("x-request-id", payload.get("request_id", "") if isinstance(payload, dict) else "")
            for k, v in headers.items():
                h.send_header(k, str(v))
            if h.close_connection:   # client sent "Connection: close": say so, like a real server
                h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(data)
            h.wfile.flush()
        except OSError:
            h.close_connection = True

    def _drop(self, h, entry):
        with self._lock:
            entry["status"] = "dropped"
        h.close_connection = True
        try:
            h.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _dispatch(self, method, rel, query, body, tok, headers):
        """Returns (result_json, committed_ids_or_None). Raises FakeError."""
        if tok is None:
            raise FakeError(401, "unauthorized", "API token is invalid.")
        if rel is None:
            raise FakeError(400, "invalid_request_url", "Invalid request URL.")
        ver = headers.get("Notion-Version")
        if not ver:
            raise FakeError(400, "missing_version",
                            "Notion-Version header failed validation: Notion-Version header should be defined.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ver):
            raise FakeError(400, "validation_error", f"Notion-Version `{ver}` is not a valid version.")
        if body is not None and not isinstance(body, dict):
            raise _bad("body should be an object")
        if body is not None and "notifications" in body and self.reject_notifications:
            raise _bad("body.notifications should be not present, instead was `{...}`.")
        segs = [unquote(s) for s in rel.split("/")]
        n = len(segs)
        if segs[0] == "users":
            if method == "GET" and n == 2 and segs[1] == "me":
                me = copy.deepcopy(self.users[tok["bot_id"]])
                me["bot"]["workspace_id"] = self.workspace_id
                me["bot"]["workspace_limits"] = {"max_file_upload_size_in_bytes": 5242880}
                return me, None
            if method == "GET" and n == 1:
                self._cap(tok, "user_info")
                return self._paginate(list(self.users.values()), query, "user"), None
            if method == "GET" and n == 2:
                self._cap(tok, "user_info")
                u = self.users.get(nid(segs[1]) or "")
                if not u:
                    raise self._not_found(segs[1], "user")
                return copy.deepcopy(u), None
        elif segs[0] == "pages":
            if method == "POST" and n == 1:
                return self._create_page(body or {}, tok)
            if n >= 2:
                if method == "GET" and n == 2:
                    self._cap(tok, "read")
                    pid = self._need(tok, segs[1], "page", ("page",))
                    return self._render_page(pid), None
                if method == "PATCH" and n == 2:
                    return self._patch_page(segs[1], body or {}, tok)
                if method == "GET" and n == 4 and segs[2] == "properties":
                    self._cap(tok, "read")
                    pid = self._need(tok, segs[1], "page", ("page",))
                    return self._property_item(pid, segs[3], query), None
        elif segs[0] == "blocks" and n >= 2:
            if n == 2 and method == "GET":
                self._cap(tok, "read")
                bid = self._need(tok, segs[1], "block", ("block", "page", "database"))
                return self._render_block(bid), None
            if n == 2 and method == "PATCH":
                return self._patch_block(segs[1], body or {}, tok)
            if n == 2 and method == "DELETE":
                return self._delete_block(segs[1], tok)
            if n == 3 and segs[2] == "children" and method == "GET":
                self._cap(tok, "read")
                bid = self._need(tok, segs[1], "block", ("block", "page"))
                items = [self._render_block(c) for c in self._visible_kids(bid)]
                return self._paginate(items, query, "block"), None
            if n == 3 and segs[2] == "children" and method == "PATCH":
                return self._append(segs[1], body or {}, tok)
        elif segs[0] == "search" and n == 1 and method == "POST":
            self._cap(tok, "read")
            return self._search(body or {}, tok), None
        elif segs[0] == "databases" and n >= 2:
            if n == 2 and method == "GET":
                self._cap(tok, "read")
                did = self._need(tok, segs[1], "database", ("database",))
                return self._render_database(did), None
            if n == 3 and segs[2] == "query" and method == "POST":
                self._cap(tok, "read")
                did = self._need(tok, segs[1], "database", ("database",))
                rows = []
                for d in self.databases[did]["data_sources"]:
                    rows += self._query_rows(d["id"], body or {})
                return self._paginate(rows, body or {}, "page_or_data_source"), None
        elif segs[0] == "data_sources" and n >= 2:
            if n == 2 and method == "GET":
                self._cap(tok, "read")
                dsid = self._need(tok, segs[1], "data_source", ("data_source",))
                return self._render_ds(dsid), None
            if n == 3 and segs[2] == "query" and method == "POST":
                self._cap(tok, "read")
                dsid = self._need(tok, segs[1], "data_source", ("data_source",))
                return self._paginate(self._query_rows(dsid, body or {}), body or {}, "page_or_data_source"), None
            if n == 3 and segs[2] == "templates" and method == "GET":
                self._cap(tok, "read")
                dsid = self._need(tok, segs[1], "data_source", ("data_source",))
                res = self._paginate([dict(t) for t in self.data_sources[dsid]["templates"]], query, "template")
                res["templates"] = res.pop("results")
                return res, None
        elif segs[0] == "views" and n == 1 and method == "GET":
            self._cap(tok, "read")
            did = self._need(tok, query.get("database_id"), "database", ("database",))
            return self._paginate(copy.deepcopy(self.views.get(did, [])), query, "view"), None
        elif segs[0] == "comments" and n == 1 and method == "GET":
            self._cap(tok, "read_comments")
            bid = self._need(tok, query.get("block_id"), "block", ("block", "page"))
            return self._paginate(copy.deepcopy(self.comments.get(bid, [])), query, "comment"), None
        raise FakeError(400, "invalid_request_url", "Invalid request URL.")

    # ================================================================== pagination / queries
    def _paginate(self, items, src, typ):
        ps = src.get("page_size", 100) if isinstance(src, dict) else 100
        try:
            ps = int(ps)
        except (TypeError, ValueError):
            raise _bad("page_size should be a number") from None
        if not 1 <= ps <= 100:
            raise _bad(f"page_size should be ≤ `100` and ≥ `1`, instead was `{ps}`.")
        start = 0
        cur = src.get("start_cursor") if isinstance(src, dict) else None
        if cur:
            ids = [it.get("id") for it in items]
            if cur not in ids:
                raise _bad("start_cursor should be a valid cursor")
            start = ids.index(cur)
        chunk = items[start:start + ps]
        more = start + ps < len(items)
        return {"object": "list", "results": chunk, "next_cursor": items[start + ps].get("id") if more else None,
                "has_more": more, "type": typ, typ: {}}

    def _property_item(self, pid, prop, query):
        page = self.pages[pid]
        key = unquote(prop)
        name = next((n for n, p in page["properties"].items() if unquote(p.get("id", "")) == key or n == key), None)
        if name is None:
            raise self._not_found(prop, "property")
        p = page["properties"][name]
        t = p["type"]
        if t not in LIST_PROPS:
            return {"object": "property_item", "id": p["id"], "type": t, t: copy.deepcopy(p.get(t))}
        values = p.get(t) or []
        try:
            ps = int(query.get("page_size", 100))
        except (TypeError, ValueError):
            raise _bad("page_size should be a number") from None
        if not 1 <= ps <= 100:
            raise _bad(f"page_size should be ≤ `100` and ≥ `1`, instead was `{ps}`.")
        cur = query.get("start_cursor")
        start = 0
        if cur:
            if not re.fullmatch(r"pi-\d+", cur) or int(cur[3:]) > len(values):
                raise _bad("start_cursor should be a valid cursor")
            start = int(cur[3:])
        chunk = values[start:start + ps]
        more = start + ps < len(values)
        return {"object": "list",
                "results": [{"object": "property_item", "id": p["id"], "type": t, t: copy.deepcopy(v)} for v in chunk],
                "next_cursor": f"pi-{start + ps}" if more else None, "has_more": more,
                "type": "property_item", "property_item": {"id": p["id"], "next_url": None, "type": t, t: {}}}

    def _search(self, body, tok):
        q = (body.get("query") or "").lower()
        flt = body.get("filter") or {}
        want = flt.get("value") if flt.get("property") == "object" else None
        if want not in (None, "page", "data_source", "database"):
            raise _bad("body.filter.value should be `page` or `data_source`")
        out = []
        tpl = {t["id"] for ds in self.data_sources.values() for t in ds["templates"]}
        if want in (None, "page"):
            for pid in self.pages:
                if pid in tpl or self._trashed_chain(pid) or not self._accessible(tok, pid):
                    continue
                if q in self._title_of(pid).lower():
                    out.append(self._render_page(pid))
        if want in (None, "data_source", "database"):
            for dsid, ds in self.data_sources.items():
                if self._trashed_chain(ds["database_id"]) or not self._accessible(tok, dsid):
                    continue
                if q in ds["name"].lower():
                    out.append(self._render_ds(dsid))
        return self._paginate(out, body, "page_or_data_source")

    def _ts_match(self, value, cond):
        for op, ref in cond.items():
            v, r = value[:16], str(ref)[:16]
            ok = {"equals": v == r, "before": v < r, "after": v > r, "on_or_before": v <= r,
                  "on_or_after": v >= r}.get(op)
            if ok is None:
                raise _bad(f"fake_notion: unsupported timestamp operator {op}")
            if not ok:
                return False
        return True

    def _row_match(self, page, flt):
        if not flt:
            return True
        if "and" in flt:
            return all(self._row_match(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self._row_match(page, f) for f in flt["or"])
        if "timestamp" in flt:
            ts = flt["timestamp"]
            if ts not in ("created_time", "last_edited_time"):
                raise _bad(f"fake_notion: unsupported timestamp {ts}")
            return self._ts_match(page[ts], flt.get(ts) or {})
        if "property" in flt:
            name = flt["property"]
            p = page["properties"].get(name)
            if p is None:
                raise _bad(f"Could not find property with name or id: {name}")
            t = p["type"]
            cond = flt.get(t) or {}
            if set(cond) - {"equals"}:
                raise _bad(f"fake_notion: unsupported property filter {cond}")
            v = p.get(t)
            if t in ("title", "rich_text"):
                v = "".join(x["plain_text"] for x in v or [])
            elif t in ("select", "status"):
                v = (v or {}).get("name")
            elif t not in ("checkbox", "number"):
                raise _bad(f"fake_notion: unsupported property filter type {t}")
            return v == cond.get("equals")
        raise _bad(f"fake_notion: unsupported filter {list(flt)}")

    def _query_rows(self, dsid, body):
        flt = body.get("filter")
        rows = [self.pages[r] for r in self.data_sources[dsid]["rows"] if not self.pages[r]["in_trash"]]
        rows = [p for p in rows if self._row_match(p, flt)]
        for s in reversed(body.get("sorts") or []):
            if s.get("timestamp") not in ("created_time", "last_edited_time"):
                raise _bad(f"fake_notion: unsupported sort {s}")
            rows.sort(key=lambda p: p[s["timestamp"]], reverse=s.get("direction") == "descending")
        return [self._render_page(p["id"]) for p in rows]

    # ================================================================== writes
    def _touch(self, oid, tok):
        ts = self._ts()
        ref = self._user_ref(tok["bot_id"])
        for x in (oid, self._owning_page(oid)):
            obj = self.pages.get(x) or self.blocks.get(x) if x else None
            if obj is not None:
                obj["last_edited_time"] = ts
                obj["last_edited_by"] = dict(ref)

    @staticmethod
    def _no_archived(body):
        if "archived" in body:
            raise _bad("body.archived should be not present (Notion-Version 2026-03-11: use in_trash).")

    def _patch_block(self, bid_raw, body, tok):
        self._cap(tok, "update")
        bid = self._need(tok, bid_raw, "block", ("block", "page", "database"))
        self._no_archived(body)
        if bid in self.pages or bid in self.databases:
            if set(body) - {"in_trash"}:
                raise _bad("child_page / child_database blocks can only be trashed or restored via this endpoint.")
            obj = self.pages.get(bid) or self.databases.get(bid)
            if "in_trash" in body:
                obj["in_trash"] = bool(body["in_trash"])
                self._touch(bid, tok)
            return self._render_block(bid), [bid]
        b = self.blocks[bid]
        if not body:
            raise _bad("body should define the block type object or in_trash")
        restoring = body.get("in_trash") is False
        if self._trashed_chain(bid) and not restoring:
            raise _bad("Can't edit block that is archived. You must unarchive the block before editing.")
        if "in_trash" in body and not isinstance(body["in_trash"], bool):
            raise _bad("body.in_trash should be a boolean")
        keys = [k for k in body if k != "in_trash"]
        for k in keys:
            if k != b["type"]:
                raise _bad(f"body.{k} should be not present (block {bid} is of type {b['type']}).")
            if not isinstance(body[k], dict):
                raise _bad(f"body.{k} should be an object")
            if "children" in body[k]:
                raise _bad(f"body.{k}.children should be not present")
            if b["type"] in ("child_page", "child_database", "synced_block", "column_list", "column",
                             "table_of_contents", "breadcrumb", "link_preview", "unsupported"):
                raise _bad(f"Block type {b['type']} is not supported for update.")
            t = b["type"]
            patch = copy.deepcopy(body[k])
            # stored rich text the request does not replace is kept as is (already normalised and
            # possibly merged beyond 2000 chars, which only REQUEST items may not exceed)
            keep = {kk: b[t][kk] for kk in ("rich_text", "caption", "cells") if kk in b[t] and kk not in patch}
            merged = {kk: vv for kk, vv in b[t].items() if kk not in keep}
            merged.update(patch)
            normed = self._norm_content(t, merged, "body", seeding=False)
            normed.update(copy.deepcopy(keep))
            b[t] = normed
        if "in_trash" in body:
            was_trashed = b["in_trash"]
            b["in_trash"] = body["in_trash"]
            if was_trashed and not b["in_trash"]:
                self._reposition_restored(bid)
        self._touch(bid, tok)
        return self._render_block(bid), [bid]

    def _reposition_restored(self, bid):
        """Live-measured (contract §7.6, probe P7): a block restored with ``in_trash: false`` does NOT
        come back at its original position. ``self.restore_position``: "end" (default: moved to the
        end of its parent's children), "start", or "original" (stays in its old slot)."""
        lst = self.kids.get(self.up.get(bid))
        if lst is None or bid not in lst or self.restore_position == "original":
            return
        lst.remove(bid)
        if self.restore_position == "start":
            lst.insert(0, bid)
        else:
            lst.append(bid)

    def _delete_block(self, bid_raw, tok):
        self._cap(tok, "update")
        bid = self._need(tok, bid_raw, "block", ("block", "page", "database"))
        obj = self.pages.get(bid) or self.databases.get(bid) or self.blocks.get(bid)
        obj["in_trash"] = True
        self._touch(bid, tok)
        return self._render_block(bid), [bid]

    def _append(self, pid_raw, body, tok):
        self._cap(tok, "insert")
        parent = self._need(tok, pid_raw, "block", ("block", "page"))
        self._no_archived(body)
        extra = set(body) - {"children", "position", "after", "notifications"}
        if extra:
            raise _bad(f"body.{sorted(extra)[0]} should be not present")
        if self._trashed_chain(parent):
            raise _bad("Can't edit block that is archived. You must unarchive the block before editing.")
        children = body.get("children")
        if not isinstance(children, list) or not children:
            raise _bad("body.children should be a non-empty array")
        self._validate_specs(children, "body.children")
        visible = self._visible_kids(parent)
        index = None
        pos = body.get("position")
        after = body.get("after")
        if pos is not None:
            if not isinstance(pos, dict) or pos.get("type") not in ("start", "end", "after_block"):
                raise _bad("body.position.type should be `start`, `end` or `after_block`")
            if pos["type"] == "start":
                index = 0
            elif pos["type"] == "after_block":
                after = (pos.get("after_block") or {}).get("id")
                if after is None:
                    raise _bad("body.position.after_block.id should be defined")
        if after is not None:
            a = nid(after)
            if a is None or a not in visible:
                raise _bad(f"after_block {after} is not a child of block {parent}")
            index = self.kids[parent].index(a) + 1
        self._charge_blocks(self._count_blocks(children))
        ids = self._create_blocks(parent, children, tok["bot_id"], seeding=False, index=index,
                                  where="body.children")
        self._touch(parent, tok)
        res = {"object": "list", "results": [self._render_block(i) for i in ids], "next_cursor": None,
               "has_more": False, "type": "block", "block": {}}
        return res, ids

    def _patch_page(self, pid_raw, body, tok):
        self._cap(tok, "update")
        pid = self._need(tok, pid_raw, "page", ("page",))
        self._no_archived(body)
        extra = set(body) - {"properties", "in_trash", "is_locked", "icon", "cover", "erase_content",
                             "notifications"}
        if extra:
            raise _bad(f"body.{sorted(extra)[0]} should be not present")
        page = self.pages[pid]
        restoring = body.get("in_trash") is False
        if self._trashed_chain(pid) and not restoring:
            raise _bad("Can't edit page that is archived. You must unarchive the page before editing.")
        staged = copy.deepcopy(page)
        if "properties" in body:
            if not isinstance(body["properties"], dict):
                raise _bad("body.properties should be an object")
            self._set_properties(staged, body["properties"], seeding=False)
        for k in ("in_trash", "is_locked"):
            if k in body:
                if not isinstance(body[k], bool):
                    raise _bad(f"body.{k} should be a boolean")
                staged[k] = body[k]
        for k in ("icon", "cover"):
            if k in body:
                staged[k] = copy.deepcopy(body[k])
        self.pages[pid] = staged
        if body.get("erase_content"):
            for c in self.kids.get(pid, []):
                obj = self.blocks.get(c) or self.pages.get(c) or self.databases.get(c)
                obj["in_trash"] = True
        self._touch(pid, tok)
        return self._render_page(pid), [pid]

    def _create_page(self, body, tok):
        self._cap(tok, "insert")
        self._no_archived(body)
        extra = set(body) - {"parent", "properties", "children", "icon", "cover", "template", "notifications"}
        if extra:
            raise _bad(f"body.{sorted(extra)[0]} should be not present")
        parent = body.get("parent")
        if not isinstance(parent, dict):
            raise _bad("body.parent should be defined, instead was `undefined`.")
        if parent.get("workspace"):
            raise _bad("body.parent: internal integrations cannot create workspace-level pages.")
        ptype = parent.get("type") or next((k for k in ("page_id", "data_source_id", "database_id") if k in parent),
                                           None)
        if ptype not in ("page_id", "data_source_id", "database_id"):
            raise _bad("body.parent.type should be `page_id`, `data_source_id` or `database_id`")
        kinds = {"page_id": ("page",), "data_source_id": ("data_source",), "database_id": ("database",)}[ptype]
        target = self._need(tok, parent.get(ptype), ptype[:-3], kinds)
        if ptype == "database_id":
            dss = self.databases[target]["data_sources"]
            if len(dss) != 1:
                raise _bad("body.parent.database_id: database has several data sources; use data_source_id")
            target, ptype = dss[0]["id"], "data_source_id"
        if self._trashed_chain(target if ptype == "page_id" else self.data_sources[target]["database_id"]):
            raise _bad("Can't create a page in an archived parent.")
        children = body.get("children") or []
        if children:
            self._validate_specs(children, "body.children")
        tpl_children = []
        tpl = body.get("template")
        if tpl:
            if ptype != "data_source_id" or not isinstance(tpl, dict):
                raise _bad("body.template is only supported for data source parents")
            tlist = self.data_sources[target]["templates"]
            if tpl.get("type") == "default":
                t = next((x for x in tlist if x["is_default"]), None)
            elif tpl.get("type") == "template_id":
                t = next((x for x in tlist if x["id"] == nid(tpl.get("template_id"))), None)
            else:
                t = None
            if t is None:
                raise _bad("body.template does not name an existing template")
            tpl_children = [self._spec_from_block(c) for c in self._visible_kids(t["id"])]
        self._charge_blocks(1 + self._count_blocks(children) + self._count_blocks(tpl_children))
        props = body.get("properties") or {}
        by = tok["bot_id"]
        ts = self._ts()
        pid = new_id()
        page = {"object": "page", "id": pid, "created_time": ts, "last_edited_time": ts,
                "created_by": self._user_ref(by), "last_edited_by": self._user_ref(by),
                "cover": copy.deepcopy(body.get("cover")), "icon": copy.deepcopy(body.get("icon")),
                "in_trash": False, "is_locked": False, "properties": {}}
        if ptype == "page_id":
            page["properties"] = {"title": {"id": "title", "type": "title", "title": []}}
            self.up[pid] = target
            try:
                self._set_properties(page, props, seeding=False)
            except FakeError:
                self.up.pop(pid, None)
                raise
            self.pages[pid] = page
            self._insert_child(target, pid)
            self._touch(target, tok)
        else:
            ds = self.data_sources[target]
            for name, sprop in ds["properties"].items():
                page["properties"][name] = self._empty_value(sprop, page)
            self.up[pid] = target
            try:
                self._set_properties(page, props, seeding=False)
            except FakeError:
                self.up.pop(pid, None)
                raise
            self.pages[pid] = page
            ds["rows"].append(pid)
        if tpl_children:
            self._create_blocks(pid, tpl_children, by, seeding=False)
        if children:
            self._create_blocks(pid, children, by, seeding=False, where="body.children")
        return self._render_page(pid), [pid]

    def _spec_from_block(self, bid):
        b = self._render_block(bid)
        t = b["type"]
        spec = {"type": t, t: copy.deepcopy(b[t])}
        kids = self._visible_kids(bid)
        if kids and bid in self.blocks:
            spec[t]["children"] = [self._spec_from_block(k) for k in kids]
        return spec
