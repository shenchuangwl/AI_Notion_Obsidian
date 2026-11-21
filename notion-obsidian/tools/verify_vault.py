#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_vault.py - independent verifier for the Notion -> Obsidian export (SPEC v1.2).

Expectations are derived ONLY from:
  * WORK/docs/SPEC.md v1.2 (the contract, encoded by hand in this file; v1.2 = section 13:
    database page templates under `<db folder>/_templates/`, canonical `.base` layout accepted
    next to the legacy `note.`-prefixed one, C10 fails on ANY missing text block),
    WORK/docs/custom-sort.md (custom-sort 3.2.0 parser rules, C05) and
    WORK/docs/bases.md (.base structure, C03),
  * the snapshot SQLite DB. A non-empty WAL is read with mode=ro and its existing
    index; unfinished rollback journals require recovery before verification.
    Absent or empty journals allow mode=ro&immutable=1. All expectation data
    are read inside one transaction,
  * the sidebar evidence JSON (teamspace order + root order),
  * optionally the snapshot's roots.json (root kind / in_trash).
This module deliberately does not import or read the exporter, so that exporter
bugs and verifier bugs do not share a cause. It never writes into the vault or
the snapshot; all output goes to --out.

Usage:
  python3 -I verify_vault.py --snapshot SNAP.db --sidebar sidebar_TS.json \
      --vault VAULT_OR_STAGING_ROOT --out REPORT_DIR \
      [--roots-kinds SNAP.db.roots.json] [--as-of ISO_UTC] [--negative-tests] \
      [--expected-deviations expected_deviations.json] [--ids IDS_FILE]

Outputs: OUT/verify.json, OUT/verify.md; with --negative-tests also
OUT/neg/<case>/vault (mutated copies), OUT/neg/<case>/verify.json and
OUT/negative_tests.json.

Check statuses: PASS / FAIL / WARN. Only C11 (credential-like strings, SPEC
section 12) can be WARN; WARN never makes `overall` FAIL.

WRITEBACK_SPEC v1.1 additions (v1.3 of this tool):
  --expected-deviations JSON  local deviations registered by refresh's read-only vault scan
      (WRITEBACK_SPEC 14.0 / 7.4; keys local_new, hold_ids, name_overrides, sync_conflict_copies,
      optional paused, local_modified). An issue CAUSED BY a listed object (exact path / id match, a
      held id's subtree) is moved out of the failing list into details["registered_deviations(info)"]
      and counted as `registered_deviations` (INFO); the check status is computed from what remains,
      so an unlisted issue still FAILs. Statuses stay PASS / FAIL / WARN. Name overrides that are in
      effect turn C03 `name_not_derived_from_title` into INFO ("命名差异").
  --ids FILE   one notion_id per line ('#' comments): C03 / C04 / C06 / C08 / C10 are restricted to
      those objects (WRITEBACK_SPEC 12 W6); C01 / C02 / C05 / C07 / C09 / C11 still run on the whole vault.
  Tool-generated notes under Notion_Obsidian/ (WRITEBACK_SPEC 5.1a: 写回预览/*.md, 运行记录/写回-*.md,
  运行记录/*-已暂停.md, 运行记录/*-未完成.md) are accepted when they carry `notion_kind: generated`,
  no notion_id and no wikilinks / embeds (C08 otherwise); their sortspec sections are syntax-checked
  by C05 like every Notion_Obsidian section.

Negative tests (--negative-tests): 9 injected fault classes, each must make its
target check FAIL (see NEG_CASES; 09 deletes ONE text block line from the page
with the most text blocks and expects C10 to FAIL).

Exit status: 0 = overall PASS (and, with --negative-tests, every injected
fault detected); 1 = otherwise; 2 = bad input / refused.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sqlite3
import sys
import unicodedata
import urllib.parse
from collections import Counter, OrderedDict, defaultdict

_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:  # python3 -I omits the script directory
    sys.path.insert(0, _TOOLS)
from wb_common import open_snapshot_reader, snapshot_sidecars_with_data, snapshot_signature  # noqa: E402

try:  # PyYAML is optional; a minimal frontmatter parser is used otherwise.
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    _yaml = None

VERSION = "1.3"

# ---------------------------------------------------------------- SPEC constants
SPEC_TEAMSPACES = [
    "General",
    "Quantitative Investing",
    "x_to_notion",
    "Godcreate Business",
    "Godcreate AI工作空间",
]
TASK_DIR = "Notion_Obsidian"
SORTSPEC_REL = TASK_DIR + "/sortspec.md"
ATT = "_attachments"
# SPEC v1.2 section 13: a database's page templates live in `<db folder>/_templates/` (name reserved)
TEMPLATES = "_templates"
NOTE_KINDS = ("page", "row", "database", "generated", "template")
REQUIRED_FIELDS = (
    "notion_id",
    "notion_kind",
    "notion_url",
    "notion_title",
    "notion_parent_id",
    "notion_teamspace",
    "notion_order",
    "notion_created_time",
    "notion_last_edited_time",
)
RESERVED_PROP_KEYS = ("aliases", "tags", "cssclasses")


def row_prop_keys(pname):
    """Frontmatter keys acceptable for row property `pname` (SPEC 4: prefix `prop_` when it collides
    with notion_*, aliases, tags, cssclasses). Case-only variants such as `Tags`, and `sorting-spec`
    (custom-sort scans folder notes, docs/custom-sort.md S1), may be written either way."""
    if pname.startswith("notion_") or pname in RESERVED_PROP_KEYS:
        return ["prop_" + pname]
    cf = pname.casefold()
    if cf.startswith("notion_") or cf in RESERVED_PROP_KEYS or pname == "sorting-spec":
        return [pname, "prop_" + pname]
    return [pname]


TEXT_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "heading_4",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "toggle",
    "code",
    "table_row",
)
CHILD_BLOCK_TYPES = ("child_page", "child_database")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOTION_URL_RE = re.compile(r"^https://app\.notion\.com/p/([0-9a-f]{32})$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
SHA8_NAME_RE = re.compile(r"__([0-9a-f]{8})(?:\.[^./]+)?$")
FORBIDDEN_NAME_CHARS = set('\\/:*?"<>|#^[]')
SANITIZE_MAP = {
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜",
    ">": "＞", "|": "｜", "#": "＃", "^": "＾", "[": "［", "]": "］",
}
# SPEC v1.1 section 3 sanitize step 1: after NFC, before step 2
SANITIZE_PRE = (("...", "…"), ("%", "％"))
TITLE_MAX_BYTES = 120
# a truncated name keeps at least this many bytes before the "…" (120 minus the
# widest UTF-8 char and the 3-byte "…", minus slack for a stripped trailing space)
TRUNC_MIN_PREFIX_BYTES = 110
# SPEC v1.2 section 13: C10 no longer uses a 99% page threshold - any missing text block is a FAIL
C10_PREFIX = 40
LIST_CAP_JSON = 5000
LIST_CAP_MD = 40
SENSITIVE_PATTERNS = (
    ("notion_token_like", re.compile(rb"(?<![A-Za-z0-9_])(?:ntn|secret)_[A-Za-z0-9]{20,}")),
    ("private_key_block", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
)

CHECK_TITLES = OrderedDict(
    [
        ("C01", "库顶层：5 个团队协作区文件夹 + Notion_Obsidian，无其他非隐藏项"),
        ("C02", "身份集合：notion_id 缺失 / 多余 / 重复（含数据库模板，按 kind 统计）"),
        ("C03", "层级：位置（含 _templates）、folder note 结构、notion_parent_id / teamspace / kind / 文件名、.base"),
        ("C04", "顺序：notion_order 与父笔记正文中子对象链接顺序、数据库说明笔记“模板”小节链接顺序"),
        ("C05", "排序配置：custom-sort 3.2.0 语法（致命错误）+ 库根/团队协作区/对象文件夹/_templates 逐节顺序"),
        ("C06", "内部链接：[[...]] / ![[...]] 均可解析"),
        ("C07", "附件：存在、sha8 一致、托管文件全部被引用"),
        ("C08", "frontmatter：可解析、必需字段齐全、取值与快照一致、文件格式"),
        ("C09", "文件名：禁用字符、NFC+casefold 冲突、组件字节长度、路径长度统计"),
        ("C10", "正文完整性：每个含文本块前 40 字符均须命中（任一未命中即 FAIL，另报覆盖率）"),
        ("C11", "敏感串（警告，不阻断）：ntn_/secret_ 长串、私钥块，只报路径/行号/类型"),
    ]
)


class InputError(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def canon_id(value):
    """Return the canonical hyphenated lower-case Notion id, or None."""
    if not isinstance(value, str):
        return None
    h = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(h) != 32:
        return None
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def nfc(s):
    return unicodedata.normalize("NFC", s)


def ckey(s):
    """Comparison key from SPEC section 3 rule 4: NFC + casefold."""
    return nfc(nfc(s).casefold())


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def stem_of(rel):
    base = rel.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base.rsplit(".", 1)[0]


def dname(rel):
    return posixpath.dirname(rel)


def bname(rel):
    return posixpath.basename(rel)


def rich_plain(rt):
    if not isinstance(rt, list):
        return ""
    return "".join((x.get("plain_text") or "") for x in rt if isinstance(x, dict))


def object_title(kind, data):
    if kind == "database":
        return rich_plain(data.get("title"))
    props = data.get("properties")
    if isinstance(props, dict):
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "title":
                return rich_plain(v.get("title"))
    return ""


def is_trashed(data):
    return bool(data.get("in_trash") or data.get("archived") or data.get("is_archived"))


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(s):
    s = EMAIL_RE.sub("<email>", s)
    s = re.sub(r"(?:ntn|secret)_[A-Za-z0-9]{8,}", "<token>", s)
    return s


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_NOTION_URL_PATH_RE = re.compile(r"^https?://(?:www\.)?(?:notion\.so|app\.notion\.com)/([^?#\s]*)", re.I)
_HEX_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")


def notion_url_id(url):
    """Object id of a SPEC-6 Notion URL (last 32-hex / uuid in the path; query and #anchor ignored)."""
    if not isinstance(url, str):
        return None
    m = _NOTION_URL_PATH_RE.match(url.strip())
    if not m:
        return None
    hexes = _HEX_ID_RE.findall(m.group(1))
    return canon_id(hexes[-1]) if hexes else None


# ---------------------------------------------------------------- sanitize (SPEC 3)
def _sanitize_variants(title):
    """All readings of the SPEC v1.1 section-3 `sanitize` steps 1-2 we accept.

    Fixed by the SPEC: NFC first, then '...' -> '…' and '%' -> '％' (step 1), then the
    step-2 replacements. Left open by the SPEC (so every combination is accepted
    instead of guessing one): whether whitespace means ASCII or Unicode whitespace,
    whether \\v \\f U+0085 U+2028 U+2029 count as line breaks (-> space) or as control
    characters (-> removed), and whether strip / trailing-dot removal is iterated.
    """
    out = []
    raw = nfc(title or "")
    for a, b in SANITIZE_PRE:
        raw = raw.replace(a, b)
    for unicode_ws in (True, False):
        for wide_nl in (False, True):
            for iterate in (False, True):
                s = "".join(SANITIZE_MAP.get(ch, ch) for ch in raw)
                s = re.sub(r"[\n\r\t\v\f\x85  ]" if wide_nl else r"[\n\r\t]", " ", s)
                s = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")
                s = re.sub(r"\s+" if unicode_ws else r"[ \t\n\r\f\v]+", " ", s)
                s = s.strip()
                s = s.rstrip(".")
                if iterate:
                    prev = None
                    while prev != s:
                        prev = s
                        s = s.strip().rstrip(".")
                if s.startswith("."):
                    s = "．" + s[1:]
                if not s:
                    s = "Untitled"
                s = nfc(s)
                if s not in out:
                    out.append(s)
    return out


def sanitize(title):
    return _sanitize_variants(title)[0]


def name_matches(stem, title, oid):
    """True when `stem` is a SPEC-conformant name for an object with this title/id."""
    id8 = oid.replace("-", "")[:8]
    suffix = f" ({id8})"
    s = nfc(stem)
    cands = [s]
    while cands[-1].endswith(suffix):
        cands.append(cands[-1][: -len(suffix)])
    bases = _sanitize_variants(title)
    for k, c in enumerate(cands):
        for b in bases:
            if c == b:
                return True
            # SPEC 3 rule 3: a truncated name is "<prefix>… (<id8>)" - the id suffix is mandatory
            if k >= 1 and c.endswith("…"):
                pre = c[:-1]
                if (
                    len(b.encode("utf-8")) > TITLE_MAX_BYTES
                    and TRUNC_MIN_PREFIX_BYTES <= len(pre.encode("utf-8")) <= TITLE_MAX_BYTES
                    and b.startswith(pre)
                ):
                    return True
    return False


# ---------------------------------------------------------------- YAML / frontmatter
class MiniYAMLError(ValueError):
    pass


_DQ_ESC = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v",
           "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
           "N": "\x85", "_": "\xa0", "L": " ", "P": " "}


def _unescape_dq(s):
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            raise MiniYAMLError("unescaped quote inside double-quoted scalar")
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(s):
            raise MiniYAMLError("dangling backslash")
        e = s[i + 1]
        if e in _DQ_ESC:
            out.append(_DQ_ESC[e])
            i += 2
        elif e in "xuU":
            n = {"x": 2, "u": 4, "U": 8}[e]
            hexs = s[i + 2: i + 2 + n]
            if len(hexs) != n or not re.fullmatch(r"[0-9a-fA-F]+", hexs):
                raise MiniYAMLError("bad escape")
            out.append(chr(int(hexs, 16)))
            i += 2 + n
        else:
            raise MiniYAMLError(f"unknown escape \\{e}")
    return "".join(out)


def _split_flow(s):
    parts, cur, q, i = [], [], None, 0
    while i < len(s):
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _mini_scalar(tok):
    t = tok.strip()
    if t.startswith('"'):
        if len(t) < 2 or not t.endswith('"'):
            raise MiniYAMLError("unterminated double-quoted scalar")
        return _unescape_dq(t[1:-1])
    if t.startswith("'"):
        if len(t) < 2 or not t.endswith("'"):
            raise MiniYAMLError("unterminated single-quoted scalar")
        return t[1:-1].replace("''", "'")
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1]
        return [_mini_scalar(x) for x in _split_flow(inner)] if inner.strip() else []
    if t.startswith("{"):
        raise MiniYAMLError("flow mappings are not supported by the fallback parser")
    k = t.find(" #")
    if k >= 0:
        t = t[:k].rstrip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", t):
        return float(t)
    return t


def _split_key(line):
    if line.startswith('"') or line.startswith("'"):
        q = line[0]
        i = 1
        while i < len(line):
            if q == '"' and line[i] == "\\":
                i += 2
                continue
            if line[i] == q:
                if q == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        key = _mini_scalar(line[: i + 1])
        rest = line[i + 1:].lstrip()
        if not rest.startswith(":"):
            raise MiniYAMLError("expected ':' after quoted key")
        return key, rest[1:]
    m = re.match(r"^([^:]+?)\s*:(?:\s(.*)|)$", line)
    if not m:
        raise MiniYAMLError(f"cannot parse line: {line[:60]!r}")
    return m.group(1), (m.group(2) or "")


_MINI_MAP_ITEM_RE = re.compile(r"^(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^\s\"'\[\]{}#&*!|>%@`,-][^:#]*?)\s*:(?:\s|$)")


def _mini_list(lines):
    """Block sequence (lines share the dash column): scalar items, or mapping items such as
    `- type: table` followed by more-indented `name: x` lines (used by .base views / sort)."""
    ind = len(lines[0]) - len(lines[0].lstrip(" "))
    groups = []
    for b in lines:
        bi = len(b) - len(b.lstrip(" "))
        bs = b.strip()
        if bi == ind and (bs.startswith("- ") or bs == "-"):
            groups.append([bs[1:].lstrip(" ")])
        elif bi < ind or not groups:
            raise MiniYAMLError(f"bad sequence indentation: {bs[:60]!r}")
        else:
            groups[-1].append(b[ind + 2:] if len(b) >= ind + 2 and not b[:ind + 2].strip() else bs)
    items = []
    for g in groups:
        first = g[0]
        if first and _MINI_MAP_ITEM_RE.match(first):
            items.append(mini_yaml_load("\n".join(g)))
        elif len(g) > 1 and first == "":
            sub = [x for x in g[1:] if x.strip()]
            items.append(_mini_list(sub) if sub and sub[0].lstrip().startswith("-") else
                         mini_yaml_load("\n".join(g[1:])))
        else:
            val = _mini_scalar(first)
            for cont in g[1:]:
                if cont.strip():
                    val = (str(val) if val is not None else "") + " " + cont.strip()
            items.append(val)
    return items


def mini_yaml_load(text):
    """Very small YAML subset: top-level mapping of scalars, block/flow lists,
    one nested mapping level and block scalars. Enough for SPEC frontmatter."""
    lines = text.split("\n")
    out = {}
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            raise MiniYAMLError(f"unexpected indentation at line {i + 1}")
        key, rest = _split_key(line)
        rest_s = rest.strip()
        i += 1
        block = []
        while i < n and (not lines[i].strip() or lines[i][0] in " \t" or
                         (lines[i].startswith("- ") or lines[i] == "-")):
            if not lines[i].strip() and i + 1 < n and lines[i + 1] and lines[i + 1][0] not in " \t-":
                block.append(lines[i])
                i += 1
                break
            block.append(lines[i])
            i += 1
        while block and not block[-1].strip():
            block.pop()
        if rest_s[:1] in ("|", ">") and re.fullmatch(r"[|>][-+]?", rest_s):
            nonblank = [b for b in block if b.strip()]
            ind = min((len(b) - len(b.lstrip(" ")) for b in nonblank), default=0)
            body = [b[ind:] if b.strip() else "" for b in block]
            if rest_s[0] == ">":
                val = " ".join(x for x in body if x)
            else:
                val = "\n".join(body)
            if not rest_s.endswith("-") and val:
                val += "\n"
            out[key] = val
        elif rest_s == "":
            nonblank = [b for b in block if b.strip()]
            if not nonblank:
                out[key] = None
            elif nonblank[0].lstrip().startswith("-"):
                out[key] = _mini_list(nonblank)
            else:
                ind = min(len(b) - len(b.lstrip(" ")) for b in nonblank)
                out[key] = mini_yaml_load("\n".join(b[ind:] for b in nonblank))
        else:
            out[key] = _mini_scalar(rest_s)
    return out


def load_yaml(text):
    """Return (mapping, error)."""
    try:
        if _yaml is not None:
            data = _yaml.safe_load(text) if text.strip() else {}
        else:
            data = mini_yaml_load(text)
    except Exception as exc:  # noqa: BLE001 - parser errors of either backend
        msg = str(exc).strip().splitlines()
        return None, (msg[0] if msg else exc.__class__.__name__)[:300]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, "frontmatter is not a mapping"
    return data, None


@dataclasses.dataclass
class FrontMatter:
    present: bool
    data: dict | None
    error: str | None
    body: str
    body_line0: int  # 1-based line number of the first body line


def parse_frontmatter(text):
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return FrontMatter(False, None, None, text, 1)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") in ("---", "..."):
            fm_text = "\n".join(x.rstrip("\r") for x in lines[1:i])
            data, err = load_yaml(fm_text)
            return FrontMatter(True, data, err, "\n".join(lines[i + 1:]), i + 2)
    return FrontMatter(True, None, "unterminated frontmatter", text, 1)


# ---------------------------------------------------------------- markdown helpers
def _mask_inline_code(line):
    res, i, n = [], 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            res.append(line[i: i + 2])
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            run = j - i
            k, found = j, -1
            while k < n:
                if line[k] == "`":
                    m = k
                    while m < n and line[m] == "`":
                        m += 1
                    if m - k == run:
                        found = k
                        break
                    k = m
                else:
                    k += 1
            if found >= 0:
                res.append(" " * (found + run - i))
                i = found + run
            else:
                res.append(line[i:j])
                i = j
            continue
        res.append(ch)
        i += 1
    return "".join(res)


_QUOTE_PREFIX_RE = re.compile(r"^(?:[ \t]*>)*[ \t]*")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def _mask_comments(lines):
    """Blank Obsidian %%comments%% (they may span lines; an unclosed %% runs to the end).
    Expects lines whose code has already been masked, so %% inside code is not seen."""
    res, inside = [], False
    for line in lines:
        buf = list(line)
        i, n = 0, len(line)
        while i < n:
            if line.startswith("%%", i):
                buf[i] = buf[i + 1] = " "
                inside = not inside
                i += 2
                continue
            if inside:
                buf[i] = " "
            i += 1
        res.append("".join(buf))
    return res


def mask_code_lines(body, comments=True):
    """Body lines as Obsidian parses them for links: fenced code blocks and $$ display-math
    blocks blanked, inline code masked and (by default) %%comments%% blanked. Obsidian does
    not create links inside any of these."""
    out = []
    fence = None
    in_math = False
    for line in body.split("\n"):
        stripped = _QUOTE_PREFIX_RE.sub("", line, count=1)
        if fence is not None:
            m = re.match(r"^(`{3,}|~{3,})[ \t]*$", stripped)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            out.append("")
            continue
        if in_math:
            if stripped.rstrip().endswith("$$"):
                in_math = False
            out.append("")
            continue
        m = _FENCE_RE.match(stripped)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            out.append("")
            continue
        st = stripped.strip()
        if st.startswith("$$"):
            # SPEC 5 equation -> "$$\n...\n$$"; a one-line "$$...$$" is display math too
            if not (len(st) > 2 and st[2:].rstrip().endswith("$$")):
                in_math = True
            out.append("")
            continue
        out.append(_mask_inline_code(line))
    return _mask_comments(out) if comments else out


def remove_comments(body):
    """`body` with %%comment%% regions blanked but code text kept (for text-coverage checks)."""
    plain = mask_code_lines(body, comments=False)
    nocom = _mask_comments(plain)
    out = []
    for orig, a, b in zip(body.split("\n"), plain, nocom):
        if a == b or len(a) != len(orig):
            out.append(orig)
            continue
        out.append("".join(" " if (ca != " " and cb == " ") else co for co, ca, cb in zip(orig, a, b)))
    return "\n".join(out)


WIKI_RE = re.compile(r"(?<!\\)(!?)\[\[([^\]\n]*)\]\]")
_WIKI_AT_RE = re.compile(r"\[\[([^\]\n]*)\]\]")


def _odd_backslashes_before(s, i):
    n = 0
    while i - n - 1 >= 0 and s[i - n - 1] == "\\":
        n += 1
    return n % 2 == 1


def find_wikilinks(line):
    """[(embed, inner, start, end)] for the wikilinks Obsidian sees on one (masked) line.
    `[[` preceded by an odd number of backslashes is escaped; `\\\\[[x]]` (escaped backslash)
    is a real link; `\\![[x]]` is a plain link, not an embed."""
    out, pos = [], 0
    while True:
        k = line.find("[[", pos)
        if k < 0:
            return out
        if _odd_backslashes_before(line, k):
            pos = k + 1
            continue
        m = _WIKI_AT_RE.match(line, k)
        if not m:
            pos = k + 1
            continue
        embed = k > 0 and line[k - 1] == "!" and not _odd_backslashes_before(line, k - 1)
        out.append((embed, m.group(1), k - 1 if embed else k, m.end()))
        pos = m.end()
LINE_PREFIX_RE = re.compile(r"^(?:[ \t]*>[ \t]?)*[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)?")
MDLINK_RE = re.compile(r"(!?)\[((?:\\.|[^\]\\\n])*)\]\((?:<[^>\n]*>|[^)\s]*)(?:[ \t]+\"[^\"\n]*\")?\)")
NOTION_URL_IN_TEXT_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|app\.notion\.com)/[^\s)>\]|\"']*")
# inline HTML formatting a renderer may use instead of markdown markers (e.g. <strong> where `**`
# would not close next to CJK punctuation); stripped on BOTH sides of the C10 comparison
_STRIP_SEQ_RE = re.compile(r"<br\s*/?>|</?(?:u|strong|b|em|i|s|del|strike|mark|sup|sub|code|kbd|span|font)"
                           r"(?:\s[^<>]*)?>", re.I)
_STRIP_CHARS = set("\\*_~`$|#>[]=")


_FW_EQUIV = str.maketrans({"｜": "|", "［": "[", "］": "]"})  # SPEC 6 swaps these in link display text


def norm_text(s):
    s = nfc(_STRIP_SEQ_RE.sub("", s)).translate(_FW_EQUIV)
    return "".join(ch for ch in s if not ch.isspace() and ch not in _STRIP_CHARS)


def _wiki_visible(m):
    if m.group(1):
        return ""
    inner = m.group(2)
    if "|" in inner:
        return inner.split("|", 1)[1].replace("｜", "|").replace("］", "]")
    tgt = inner.split("#", 1)[0]
    return stem_of(tgt) if tgt else inner


def visible_text(body):
    s = WIKI_RE.sub(_wiki_visible, body)
    s = MDLINK_RE.sub(lambda m: "" if m.group(1) else m.group(2), s)
    return html.unescape(s)


# ---------------------------------------------------------------- expectation model
@dataclasses.dataclass
class ExpObj:
    id: str
    kind: str  # page | row | database | template
    title: str
    parent: str | None
    order: int
    teamspace: str
    is_root: bool
    depth: int
    created_time: object = None
    last_edited_time: object = None
    icon: object = None
    cover: object = None
    properties: dict | None = None
    children: list = dataclasses.field(default_factory=list)
    gap_children: list = dataclasses.field(default_factory=list)
    order_unknown: bool = False  # database whose row order source (_row_ids) is unavailable
    # SPEC v1.2 section 13: a database's page templates (data_source._template_ids order). They are
    # NOT children (not rows, not in the row list / .base / index); they live in <db folder>/_templates/
    templates: list = dataclasses.field(default_factory=list)

    @property
    def folder_expected(self):
        return self.kind == "database" or bool(self.children)

    @property
    def structure_flexible(self):
        return self.kind != "database" and not self.children and bool(self.gap_children)


def snapshot_is_live(path):
    """Whether snapshot data may need normal journal handling."""
    return bool(snapshot_sidecars_with_data(path))


def open_ro(path, force_immutable=False):
    """Return the read-only connection and SQLite open mode recorded in the verification report."""
    return open_snapshot_reader(path, require_finished=force_immutable, error_type=InputError,
                                isolation_level=None)



class Expectation:
    """Everything the vault should contain, derived from snapshot + sidebar."""

    def __init__(self, snapshot, sidebar, roots_kinds=None, as_of=None, snapshot_immutable=False):
        self.snapshot_path = os.path.realpath(snapshot)
        self.sidebar_path = os.path.abspath(sidebar)
        self.roots_kinds_path = os.path.abspath(roots_kinds) if roots_kinds else None
        self.as_of = as_of
        self.warnings = []
        if not os.path.isfile(self.snapshot_path):
            raise InputError(f"snapshot not found: {self.snapshot_path}")
        if not os.path.isfile(self.sidebar_path):
            raise InputError(f"sidebar not found: {self.sidebar_path}")
        self._load_sidebar()
        self._load_roots_kinds()
        self._page_texts = None
        self._block_refs = None
        sig0 = snapshot_signature(self.snapshot_path)
        # every snapshot read happens in ONE read transaction on ONE connection, so the
        # expectation is a consistent point-in-time view even while the crawler keeps writing
        self._con, self.open_mode = open_ro(self.snapshot_path, force_immutable=snapshot_immutable)
        try:
            self._con.execute("BEGIN")
            self._load_snapshot()
            self._build()
            self._load_files()
            self._load_block_refs()
            self.page_texts()
        except sqlite3.DatabaseError as exc:
            raise InputError(f"snapshot unreadable ({self.open_mode}): {exc}") from exc
        finally:
            try:
                self._con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._con.close()
            self._con = None
        if self.open_mode == "mode=ro&immutable=1" and snapshot_signature(self.snapshot_path) != sig0:
            # immutable=1 takes no lock: a write during the read could have produced a torn expectation
            raise InputError(f"{self.snapshot_path}: the snapshot or its -wal/-journal changed while it was being read "
                             "(a writer is active); verify a closed snapshot or a copy")

    # -- inputs ---------------------------------------------------------------
    def _load_sidebar(self):
        with open(self.sidebar_path, encoding="utf-8") as fh:
            sb = json.load(fh)
        roots = sb.get("roots") if isinstance(sb, dict) else sb
        if not isinstance(roots, list) or not roots:
            raise InputError("sidebar JSON has no 'roots' list")
        ts = None
        if isinstance(sb, dict):
            ts = sb.get("teamspace_order_required") or sb.get("teamspace_order_observed")
        if not ts:
            seen = OrderedDict()
            for r in sorted(roots, key=lambda r: int(r.get("teamspace_order") or 0)):
                seen.setdefault(r.get("teamspace"), None)
            ts = list(seen)
        self.teamspaces = list(ts)
        if self.teamspaces != SPEC_TEAMSPACES:
            self.warnings.append(
                f"sidebar teamspace list {self.teamspaces} differs from SPEC section 2 {SPEC_TEAMSPACES}")
        rows, seen_ids = [], set()
        for r in roots:
            rid = canon_id(r.get("id"))
            if not rid:
                self.warnings.append(f"sidebar root without a valid id: {r.get('title')!r}")
                continue
            if rid in seen_ids:
                self.warnings.append(f"sidebar lists root {rid} twice")
                continue
            seen_ids.add(rid)
            tsn = r.get("teamspace")
            if tsn not in self.teamspaces:
                self.warnings.append(f"sidebar root {rid} has unknown teamspace {tsn!r}")
            tso = r.get("teamspace_order")
            if tso is None and tsn in self.teamspaces:
                tso = self.teamspaces.index(tsn) + 1
            if tsn in self.teamspaces and int(tso) != self.teamspaces.index(tsn) + 1:
                self.warnings.append(f"sidebar root {rid}: teamspace_order {tso} disagrees with teamspace list")
            rows.append({"id": rid, "title": r.get("title") or "", "teamspace": tsn,
                         "teamspace_order": int(tso or 0), "root_order": int(r.get("root_order") or 0)})
        rows.sort(key=lambda r: (r["teamspace_order"], r["root_order"]))
        for tsn in self.teamspaces:
            ords = [r["root_order"] for r in rows if r["teamspace"] == tsn]
            if ords != list(range(1, len(ords) + 1)):
                self.warnings.append(f"sidebar root_order for {tsn!r} is not 1..n: {ords}")
        self.sidebar_roots = rows
        self.sidebar_sha256 = sha256_file(self.sidebar_path)

    def _load_roots_kinds(self):
        self.roots_kinds = {}
        if not self.roots_kinds_path:
            return
        with open(self.roots_kinds_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("roots") or []
        for r in data:
            rid = canon_id(r.get("id"))
            if rid:
                self.roots_kinds[rid] = {"kind": r.get("kind"), "in_trash": bool(r.get("in_trash"))}
        sb_ids = {r["id"] for r in self.sidebar_roots}
        if set(self.roots_kinds) != sb_ids:
            self.warnings.append(
                "roots-kinds ids differ from sidebar ids: only_in_roots_kinds="
                f"{sorted(set(self.roots_kinds) - sb_ids)} only_in_sidebar={sorted(sb_ids - set(self.roots_kinds))}")

    def _load_snapshot(self):
        if self.open_mode == "mode=ro":
            self.warnings.append(
                "snapshot has a non-empty WAL: opened with mode=ro. This read transaction includes "
                "data committed when verification started")
        con = self._con
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        for t in ("objects", "blocks", "files"):
            if t not in tables:
                raise InputError(f"snapshot lacks table {t!r}")
        self.objects = {}
        self.data_sources = OrderedDict()
        self.fetched_at = {}
        other = Counter()
        bad_json = 0
        ocols = {r[1] for r in con.execute("pragma table_info(objects)")}
        fsel = "fetched_at" if "fetched_at" in ocols else "null"
        for oid, kind, parent_id, data, fat in con.execute(
                f"select id, kind, parent_id, data, {fsel} from objects order by rowid"):
            cid = canon_id(oid)
            if not cid:
                continue
            if fat and kind in ("page", "database"):
                self.fetched_at[cid] = fat
            try:
                d = json.loads(data) if data else {}
            except ValueError:
                d, bad_json = {}, bad_json + 1
            if kind == "data_source":
                rids = [canon_id(x) for x in (d.get("_row_ids") or [])]
                # SPEC v1.2 section 13: `_template_ids` (API order); a missing key means "no templates"
                tids = [canon_id(x) for x in (d.get("_template_ids") or [])]
                self.data_sources[cid] = {"parent": canon_id(parent_id), "data": d,
                                          "row_ids": [x for x in rids if x],
                                          "has_row_ids": "_row_ids" in d,
                                          "template_ids": [x for x in tids if x],
                                          "has_template_ids": "_template_ids" in d}
            elif kind in ("page", "database"):
                if cid in self.objects and self.objects[cid][0] == "database":
                    continue
                self.objects[cid] = (kind, canon_id(parent_id), d)
            else:
                other[kind] += 1
        self.template_id_set = {t for v in self.data_sources.values() for t in v["template_ids"]}
        if bad_json:
            self.warnings.append(f"{bad_json} snapshot objects have unparsable JSON")
        if other:
            self.warnings.append(f"snapshot objects of unknown kinds ignored: {dict(other)}")
        try:
            rows = con.execute(
                "select id, page_id, parent_id, ord, type, json_extract(data,'$.in_trash'), "
                "json_extract(data,'$.archived'), rowid from blocks").fetchall()
            rows = [(r[0], r[1], r[2], r[3], r[4], bool(r[5]) or bool(r[6]), r[7]) for r in rows]
        except sqlite3.OperationalError:
            rows = []
            for r in con.execute("select id, page_id, parent_id, ord, type, data, rowid from blocks"):
                try:
                    d = json.loads(r[5]) if r[5] else {}
                except ValueError:
                    d = {}
                rows.append((r[0], r[1], r[2], r[3], r[4], is_trashed(d), r[6]))
        self.block_children = defaultdict(list)
        self.block_page = {}
        self.block_trashed = set()
        sort_key = {}
        for bid, pid, par, ordv, typ, tr, rid in rows:
            bid, pid, par = canon_id(bid), canon_id(pid), canon_id(par)
            self.block_children[(pid, par)].append((ordv if ordv is not None else 0, bid or "", typ, tr))
            sort_key[bid or ""] = rid
            if bid:
                self.block_page[bid] = pid
                if tr:
                    self.block_trashed.add(bid)
        # API children order = `ord`; ties / NULL ord fall back to insertion (= fetch) order,
        # never to the block id
        for v in self.block_children.values():
            v.sort(key=lambda t: (t[0], sort_key.get(t[1], 0)))
        self.block_count = len(rows)
        self.work_status = {}
        if "work" in tables:
            ws = Counter()
            for kind, status, cnt in con.execute("select kind, status, count(*) from work group by 1, 2"):
                ws[f"{kind}:{status}"] = cnt
            self.work_status = dict(sorted(ws.items()))
            not_done = sum(c for k, c in ws.items() if not k.endswith(":done"))
            if not_done:
                self.warnings.append(
                    f"snapshot work queue has {not_done} items not in status 'done' "
                    f"({self.work_status}); the snapshot may be incomplete")
        self.meta = {}
        if "meta" in tables:
            for k, v in con.execute("select key, value from meta"):
                if k != "roots":
                    self.meta[k] = v

    # -- model ----------------------------------------------------------------
    def _make(self, oid, kind, data, parent, order, teamspace, is_root, depth):
        props = None
        if kind == "row" and isinstance(data.get("properties"), dict):
            props = {k: (v.get("type") if isinstance(v, dict) else None)
                     for k, v in data["properties"].items()}
        e = ExpObj(id=oid, kind=kind, title=object_title(kind, data), parent=parent, order=order,
                   teamspace=teamspace, is_root=is_root, depth=depth,
                   created_time=data.get("created_time"), last_edited_time=data.get("last_edited_time"),
                   icon=data.get("icon"), cover=data.get("cover"), properties=props)
        self.objs[oid] = e
        return e

    def _block_child_ids(self, page_id):
        out = []
        top = page_id if (page_id, page_id) in self.block_children else None

        def walk(parent, depth):
            if depth > 200:
                return
            for _o, bid, typ, tr in self.block_children.get((page_id, parent), ()):
                if tr:
                    continue
                if typ in CHILD_BLOCK_TYPES:
                    out.append(bid)
                    continue
                walk(bid, depth + 1)

        walk(top, 0)
        return out

    def _rows_by_object_parent(self, db_id, ds_id=None):
        """Fallback when a row order source is missing: snapshot pages whose own parent is the
        data source (or, without ds_id, the database). Order is snapshot rowid order (unknown)."""
        out = []
        for oid, (kind_s, par, d) in self.objects.items():
            if kind_s != "page" or oid in self.template_id_set:  # templates share the parent type, not rows
                continue
            p = d.get("parent") if isinstance(d.get("parent"), dict) else {}
            if p.get("type") != "data_source_id":
                continue
            if ds_id is not None:
                if canon_id(p.get("data_source_id")) == ds_id:
                    out.append(oid)
            elif canon_id(p.get("database_id")) == db_id or par == db_id:
                out.append(oid)
        return out

    def _db_ds_order(self, db_id):
        """Data source ids of a database in the order the database lists them (fallback: snapshot
        data sources whose parent is the database, in rowid order)."""
        data = self.objects[db_id][2]
        ds_ids = [canon_id(x.get("id")) for x in (data.get("data_sources") or []) if isinstance(x, dict)]
        ds_ids = [x for x in ds_ids if x]
        if not ds_ids:
            ds_ids = [k for k, v in self.data_sources.items() if v["parent"] == db_id]
        return ds_ids

    def _db_templates(self, db_id):
        """SPEC v1.2 section 13: template page ids of a database = `_template_ids` of each (non-trashed)
        data source in API order, data sources in database order. A missing `_template_ids` = none."""
        out = []
        for ds in self._db_ds_order(db_id):
            v = self.data_sources.get(ds)
            if v is None or is_trashed(v["data"]):
                continue
            for t in v["template_ids"]:
                if t not in out:
                    out.append(t)
        return out

    def _db_rows(self, db_id):
        """-> (row ids in query order, order_unknown). SPEC 3: a database's children are the rows
        of each of its data sources in query order (snapshot: data_source._row_ids), data sources
        in the order the database lists them."""
        ds_ids = self._db_ds_order(db_id)
        rows, unknown = [], False
        if not ds_ids:
            fb = self._rows_by_object_parent(db_id)
            self.missing_data_sources.append({"database": db_id, "data_source": None,
                                              "reason": "database lists no data_sources and none in snapshot",
                                              "fallback_rows": len(fb)})
            return fb, True
        for ds in ds_ids:
            v = self.data_sources.get(ds)
            if v is None:
                fb = self._rows_by_object_parent(db_id, ds)
                self.missing_data_sources.append({"database": db_id, "data_source": ds,
                                                  "reason": "data_source not in snapshot", "fallback_rows": len(fb)})
                rows.extend(fb)
                unknown = True
                continue
            if is_trashed(v["data"]):
                continue
            if not v["has_row_ids"]:
                fb = self._rows_by_object_parent(db_id, ds)
                self.missing_data_sources.append({"database": db_id, "data_source": ds, "reason": "no _row_ids",
                                                  "fallback_rows": len(fb)})
                rows.extend(fb)
                unknown = True
                continue
            rows.extend(v["row_ids"])
        return rows, unknown

    def _db_ds_ids(self, db_id):
        data = self.objects[db_id][2]
        ids = {canon_id(x.get("id")) for x in (data.get("data_sources") or []) if isinstance(x, dict)}
        ids |= {k for k, v in self.data_sources.items() if v["parent"] == db_id}
        ids.discard(None)
        return ids

    def api_parent_ok(self, container, d, row):
        """SPEC v1.1 section 3: the object's own API `parent` decides. A child_page/child_database
        target is a child of page `container` only when parent is page_id == container, or block_id
        of a block inside `container`; a row is a child of database `container` only when parent is
        data_source_id in the database's data sources, or database_id == container.
        -> (is_child, compact parent description)."""
        p = d.get("parent") if isinstance(d.get("parent"), dict) else None
        if p is None:
            self._no_api_parent.append(container)
            return True, None  # cannot judge; SPEC rule needs the field (counted in warnings)
        t = p.get("type")
        desc = {"type": t, "id": canon_id(p.get(t)) if isinstance(p.get(t), str) else p.get(t)}
        if row:
            if canon_id(p.get("data_source_id")) in self._db_ds_ids(container):
                return True, desc
            return canon_id(p.get("database_id")) == container, desc
        if t == "page_id":
            return canon_id(p.get("page_id")) == container, desc
        if t == "block_id":
            return self.block_page.get(canon_id(p.get("block_id"))) == container, desc
        return False, desc

    _VIA = {"row": "data_source", "template": "template", None: "block"}

    def _expand(self, e):
        if e.kind == "database":
            rows, e.order_unknown = self._db_rows(e.id)
            cands = [(cid, "row") for cid in rows]
            # SPEC v1.2 section 13: templates are the database's template sub-objects (own sequence,
            # notion_order = template number), expanded after the rows
            cands += [(cid, "template") for cid in self._db_templates(e.id)]
        else:
            cands = [(cid, None) for cid in self._block_child_ids(e.id)]
        for cid, forced in cands:
            via = self._VIA[forced]
            o = self.objects.get(cid)
            if o is not None:
                # templates are pages whose API parent is the data source, exactly like rows
                is_child, pdesc = self.api_parent_ok(e.id, o[2], forced in ("row", "template"))
                if not is_child:
                    # a reference (e.g. an import-log page's leftover child_page blocks pointing at
                    # roots): no change to the hierarchy, not part of the child sequence / notion_order
                    self.references.append({"id": cid, "from": e.id, "via": via,
                                            "api_parent": pdesc, "title": object_title(o[0], o[2])})
                    continue
            if cid in self.objs:
                self.multi_ref.append({"id": cid, "parent": e.id, "first_parent": self.objs[cid].parent,
                                       "via": via})
                continue
            if o is None:
                if forced != "template":
                    e.gap_children.append(cid)
                if cid not in self._gap_ids:
                    self._gap_ids.add(cid)
                    self.gaps.append({"id": cid, "parent": e.id, "teamspace": e.teamspace, "via": via})
                continue
            kind_s, _p, d = o
            if is_trashed(d):
                if cid not in self._trashed_ids:
                    self._trashed_ids.add(cid)
                    self.trashed.append({"id": cid, "parent": e.id, "title": object_title(kind_s, d),
                                         "via": via})
                continue
            kind = forced or ("database" if kind_s == "database" else "page")
            seq = e.templates if forced == "template" else e.children
            child = self._make(cid, kind, d, e.id, len(seq) + 1, e.teamspace, False, e.depth + 1)
            seq.append(cid)
            self._expand(child)

    def _build(self):
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
        self.objs = OrderedDict()
        self.gaps, self.trashed, self.multi_ref, self.missing_data_sources = [], [], [], []
        self.references, self._no_api_parent = [], []
        self._gap_ids, self._trashed_ids = set(), set()
        for r in self.sidebar_roots:
            rid = r["id"]
            rk = self.roots_kinds.get(rid)
            o = self.objects.get(rid)
            if rk is not None and rk.get("in_trash"):
                self._trashed_ids.add(rid)
                self.trashed.append({"id": rid, "parent": None, "title": r["title"]})
                continue
            if o is None:
                self._gap_ids.add(rid)
                self.gaps.append({"id": rid, "parent": None, "teamspace": r["teamspace"], "via": "sidebar"})
                continue
            kind_s, _p, d = o
            if is_trashed(d):
                self._trashed_ids.add(rid)
                self.trashed.append({"id": rid, "parent": None, "title": r["title"]})
                continue
            kind = "database" if kind_s == "database" else "page"
            if rk and rk.get("kind") and rk["kind"] != kind:
                self.warnings.append(f"root {rid}: roots-kinds says {rk['kind']!r}, snapshot says {kind!r}")
            if rid in self.objs:
                self.warnings.append(f"root {rid} listed twice; kept first")
                continue
            # SPEC 3: all sidebar roots are registered BEFORE any subtree is expanded, and stay roots
            # even when some page carries a child_page block pointing at them (reference / multi_ref)
            self._make(rid, kind, d, None, r["root_order"], r["teamspace"], True, 0)
        for e in [x for x in self.objs.values() if x.is_root]:
            self._expand(e)
        for m in self.multi_ref:
            m["target_is_root"] = bool(self.objs.get(m["id"]) and self.objs[m["id"]].is_root)
        for m in self.references:
            t = self.objs.get(m["id"])
            m["target_is_root"] = bool(t and t.is_root)
            m["target_in_scope"] = t is not None
            m["target_parent"] = t.parent if t else None
        self._reference_targets = {m["id"] for m in self.references}
        if self._no_api_parent:
            self.warnings.append(f"{len(self._no_api_parent)} child candidates have no API `parent` in the "
                                 "snapshot; they were accepted as children (SPEC 3 v1.1 cannot be applied)")
        # snapshot objects not reached from the 31 roots
        self.unreachable = []
        for oid, (kind_s, parent, d) in self.objects.items():
            if oid in self.objs or oid in self._trashed_ids or oid in self._gap_ids:
                continue
            reason = "trashed" if is_trashed(d) else "not_reachable"
            in_scope_anc = None
            if reason == "not_reachable":
                p, hops = parent, 0
                while p and hops < 100:
                    if p in self.block_page and p not in self.objects:  # block-parented object
                        p = self.block_page[p]
                        hops += 1
                        continue
                    if p in self._trashed_ids:
                        reason = "under_trashed_ancestor"
                        break
                    if p in self.objs and in_scope_anc is None:
                        in_scope_anc = p
                    po = self.objects.get(p)
                    if po is None:
                        break
                    if is_trashed(po[2]):
                        reason = "under_trashed_ancestor"
                        break
                    p, hops = po[1], hops + 1
                if reason == "not_reachable" and in_scope_anc is not None:
                    # its parent chain enters the exported tree, yet no child_page/child_database block
                    # or data-source _row_ids reaches it: a silent omission unless explained
                    reason = "orphan_under_in_scope_ancestor"
            u = {"id": oid, "kind": kind_s, "parent_id": parent, "reason": reason,
                 "in_scope_ancestor": in_scope_anc, "title": object_title(kind_s, d)}
            if oid in self._reference_targets:
                # only reached through a block whose page is not its API parent (SPEC 3 v1.1 reference)
                u["referenced_from"] = sorted({m["from"] for m in self.references if m["id"] == oid})
            self.unreachable.append(u)
        self.orphans = [u for u in self.unreachable if u["reason"] == "orphan_under_in_scope_ancestor"]
        self._snapshot_warnings()

    def _snapshot_warnings(self):
        arch = [i for i, (_k, _p, d) in self.objects.items()
                if d.get("is_archived") and not (d.get("in_trash") or d.get("archived"))]
        if arch:
            self.warnings.append(
                f"{len(arch)} snapshot objects have is_archived=true but in_trash/archived false; SPEC 3 names only "
                f"in_trash/archived, this verifier treats them as trashed (first: {arch[:3]})")
        # containers whose children were never fetched: a child_page nested in them would be invisible
        parents_with_kids = {par for (_pg, par) in self.block_children}
        self.unfetched_containers = []
        con = self._con
        for bid, pid, typ in con.execute(
                "select id, page_id, type from blocks where has_children = 1 and type not in "
                "('child_page', 'child_database')"):
            cb, cp = canon_id(bid), canon_id(pid)
            if cp in self.objs and cb not in parents_with_kids and cb not in self.block_trashed:
                self.unfetched_containers.append({"block": cb, "page": cp, "type": typ})
        if self.unfetched_containers:
            self.warnings.append(
                f"{len(self.unfetched_containers)} in-scope blocks have has_children=1 but no children in the "
                f"snapshot (nested content / child pages may be missing): {self.unfetched_containers[:3]}")

    def _load_files(self):
        con = self._con
        rows = con.execute(
            "select key, owner_id, owner_kind, field, name, size, sha256, status from files").fetchall()
        self.files = []
        for key, owner, okind, field, name, size, sha, status in rows:
            oid = canon_id(owner)
            trashed = False
            if okind == "block":
                page = self.block_page.get(oid)
                trashed = oid in self.block_trashed
            else:
                page = oid
            e = self.objs.get(page) if page else None
            self.files.append({
                "key": key, "owner_id": oid, "owner_kind": okind, "field": field, "name": name,
                "size": size, "sha256": (sha or "").lower(), "status": status, "page": page,
                "in_scope": bool(e) and not trashed, "teamspace": e.teamspace if e else None})

    # -- references to child objects (C04) --------------------------------------
    _REF_TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "heading_4", "bulleted_list_item",
                       "numbered_list_item", "to_do", "quote", "callout", "toggle")
    _REF_CAPTION_TYPES = ("image", "video", "audio", "pdf", "file", "bookmark", "embed", "code")

    @staticmethod
    def _rich_refs(segs, optional):
        out = []
        for s in segs or ():
            if not isinstance(s, dict):
                continue
            if s.get("type") == "mention":
                m = s.get("mention") or {}
                t = m.get("type")
                if t in ("page", "database"):
                    cid = canon_id((m.get(t) or {}).get("id"))
                    if cid:
                        out.append((cid, optional))
                    continue
                if t in ("link_mention", "link_preview"):
                    cid = notion_url_id((m.get(t) or {}).get("href") or (m.get(t) or {}).get("url") or s.get("href"))
                    if cid:
                        out.append((cid, True))
                    continue
            link = ((s.get("text") or {}).get("link") or {}).get("url") if isinstance(s.get("text"), dict) else None
            cid = notion_url_id(s.get("href") or link)
            if cid:
                out.append((cid, optional))  # SPEC 6: in-scope Notion URLs are rewritten to wikilinks
        return out

    def _load_block_refs(self):
        """block id -> [(target object id, optional)] for links the SPEC renderer emits for that block
        (link_to_page, page/database mentions, rich-text Notion URLs; captions / bookmark URLs are
        `optional` because SPEC 5 and 6 do not say whether they become wikilinks), plus
        synced_block -> original block id."""
        if getattr(self, "_block_refs", None) is not None:
            return
        refs, synced = {}, {}
        con = self._con
        q = ("select id, type, data from blocks where type in ('link_to_page', 'synced_block') "
             "or data like '%\"mention\"%' or data like '%notion.so/%' or data like '%app.notion.com/%'")
        for bid, typ, data in con.execute(q):
            bid = canon_id(bid)
            if not bid or bid in self.block_trashed:
                continue
            try:
                d = json.loads(data) if data else {}
            except ValueError:
                continue
            body = d.get(typ) if isinstance(d.get(typ), dict) else {}
            out = []
            if typ == "link_to_page":
                t = body.get("type")
                cid = canon_id(body.get(t)) if t in ("page_id", "database_id") else None
                if cid:
                    out.append((cid, False))
            elif typ == "synced_block":
                src = canon_id(((body.get("synced_from") or {}).get("block_id")))
                if src:
                    synced[bid] = src
            elif typ == "table_row":
                out = self._rich_refs([x for c in (body.get("cells") or []) for x in (c or [])], False)
            elif typ in self._REF_TEXT_TYPES:
                out = self._rich_refs(body.get("rich_text"), False)
            if typ in self._REF_CAPTION_TYPES:
                out += self._rich_refs(body.get("caption"), True)
            if typ in ("bookmark", "embed", "link_preview"):
                cid = notion_url_id(body.get("url"))
                if cid:
                    out.append((cid, True))
            if out:
                refs[bid] = out
        self._block_refs, self._synced_from = refs, synced

    def child_link_ranks(self, pid):
        """child id -> (definite, optional): how many links to that child the SPEC renderer emits in
        the parent note BEFORE the child's own child_page/child_database link (body order). The
        child's own link is then occurrence number `definite` .. `definite + optional` (0-based)."""
        e = self.objs[pid]
        kids = set(e.children)
        if not kids:
            return {}
        if e.kind == "database":
            d = self.objects[pid][2]
            opt = Counter(c for c, _o in self._rich_refs(d.get("description"), True) if c in kids)
            return {c: (0, opt[c]) for c in kids}
        self._load_block_refs()
        cnt_def, cnt_opt, ranks = Counter(), Counter(), {}

        def walk(page, parent, depth, own):
            if depth > 200:
                return
            for _o, bid, typ, tr in self.block_children.get((page, parent), ()):
                if tr:
                    continue
                if typ in CHILD_BLOCK_TYPES:
                    if own and bid in kids and bid not in ranks:
                        ranks[bid] = (cnt_def[bid], cnt_opt[bid])
                    elif bid in kids:
                        cnt_def[bid] += 1  # another block rendering a link to the same object
                    continue
                for cid, opt in self._block_refs.get(bid, ()):
                    if cid in kids:
                        (cnt_opt if opt else cnt_def)[cid] += 1
                src = self._synced_from.get(bid)
                if src and src in self.block_page and depth < 50:
                    walk(self.block_page[src], src, depth + 1, False)  # SPEC 5: references render the original
                walk(page, bid, depth + 1, own)

        walk(pid, pid if (pid, pid) in self.block_children else None, 0, True)
        return ranks

    def db_properties(self, db_id):
        """Ordered property name -> type over the database's data sources."""
        data = self.objects[db_id][2]
        ds_ids = [canon_id(x.get("id")) for x in (data.get("data_sources") or []) if isinstance(x, dict)]
        ds_ids = [x for x in ds_ids if x] or [k for k, v in self.data_sources.items() if v["parent"] == db_id]
        props = OrderedDict()
        for ds in ds_ids:
            v = self.data_sources.get(ds)
            for name, p in ((v or {}).get("data", {}).get("properties") or {}).items():
                props.setdefault(name, p.get("type") if isinstance(p, dict) else None)
        if not props and isinstance(data.get("properties"), dict):
            for name, p in data["properties"].items():
                props.setdefault(name, p.get("type") if isinstance(p, dict) else None)
        return props

    def page_texts(self):
        """page id -> [(block id, type, 40-char key, redacted display snippet)]"""
        if self._page_texts is not None:
            return self._page_texts
        res = defaultdict(list)
        con = self._con
        q = "select id, page_id, type, data from blocks where type in (%s)" % ",".join(
            "?" * len(TEXT_BLOCK_TYPES))
        for bid, pid, typ, data in con.execute(q, TEXT_BLOCK_TYPES):
            pid, bid = canon_id(pid), canon_id(bid)
            e = self.objs.get(pid)
            if e is None or e.kind == "database" or bid in self.block_trashed:
                continue
            try:
                d = json.loads(data) if data else {}
            except ValueError:
                continue
            body = d.get(typ) or {}
            if typ == "table_row":
                segs = [x for cell in (body.get("cells") or []) for x in (cell or [])]
            else:
                segs = body.get("rich_text") or []
            plain = rich_plain(segs)
            if not plain.strip():
                continue
            key = norm_text(plain)[:C10_PREFIX]
            if not key:
                continue
            disp = "".join(
                "@user" if (isinstance(x, dict) and x.get("type") == "mention"
                            and (x.get("mention") or {}).get("type") == "user")
                else (x.get("plain_text") or "") for x in segs if isinstance(x, dict))
            # SPEC 5/6 leave the display text of mentions open: a link_mention may show its title
            # instead of the URL, a page/database mention the target's current title. Accept those too.
            alt = "".join(self._mention_display(x) for x in segs if isinstance(x, dict))
            alt_key = norm_text(alt)[:C10_PREFIX]
            alts = (alt_key,) if alt_key and alt_key != key else ()
            res[pid].append((bid, typ, key, redact(norm_text(disp)[:C10_PREFIX]), alts))
        self._page_texts = res
        return res

    def _mention_display(self, seg):
        if seg.get("type") != "mention":
            return seg.get("plain_text") or ""
        m = seg.get("mention") or {}
        t = m.get("type")
        body = m.get(t) if isinstance(m.get(t), dict) else {}
        if t in ("link_mention", "link_preview") and body.get("title"):
            return body["title"]
        if t in ("page", "database"):
            o = self.objs.get(canon_id(body.get("id")))
            if o is not None and o.title:
                return o.title
        if t == "date" and body.get("start"):
            # SPEC v1.1 §5.1: date mention shown as "YYYY-MM-DD HH:MM (+offset)", range joined with " → "
            def _one(x):
                if not x or "T" not in x:
                    return x or ""
                day, rest = x.split("T", 1)
                mm = re.search(r"([+-]\d\d:\d\d)$", rest)
                return f"{day} {rest[:5]}" + (f" ({mm.group(1)})" if mm else "")
            return _one(body.get("start")) + (" → " + _one(body["end"]) if body.get("end") else "")
        return seg.get("plain_text") or ""

    def summary(self):
        kinds = Counter(e.kind for e in self.objs.values())
        per_ts = OrderedDict((t, Counter()) for t in self.teamspaces)
        for e in self.objs.values():
            per_ts.setdefault(e.teamspace, Counter())[e.kind] += 1
        return {
            "sidebar_roots": len(self.sidebar_roots),
            "teamspaces": self.teamspaces,
            "expected_objects": len(self.objs),
            "expected_by_kind": dict(sorted(kinds.items())),
            "databases_with_templates": sum(1 for e in self.objs.values() if e.templates),
            "snapshot_data_sources_with_template_ids": sum(1 for v in self.data_sources.values()
                                                           if v["has_template_ids"]),
            "expected_by_teamspace": {k: dict(sorted(v.items())) for k, v in per_ts.items()},
            "snapshot_gaps_reachable_but_not_in_snapshot": len(self.gaps),
            "trashed_excluded": len(self.trashed),
            "multi_referenced_children": len(self.multi_ref),
            "references_not_children(api_parent_elsewhere)": len(self.references),
            "references_to_roots": sum(1 for m in self.references if m["target_is_root"]),
            "snapshot_open_mode": self.open_mode,
            "missing_data_sources": len(self.missing_data_sources),
            "databases_with_unknown_row_order": sum(1 for e in self.objs.values() if e.order_unknown),
            "orphans_under_in_scope_ancestor": len(self.orphans),
            "blocks_with_unfetched_children": len(self.unfetched_containers),
            "snapshot_objects_not_reachable": Counter(u["reason"] for u in self.unreachable),
            "snapshot_objects": len(self.objects),
            "snapshot_data_sources": len(self.data_sources),
            "snapshot_blocks": self.block_count,
            "snapshot_files": dict(Counter(f["status"] for f in self.files)),
            "snapshot_files_ok_in_scope": sum(1 for f in self.files if f["status"] == "ok" and f["in_scope"]),
            "snapshot_work_status": self.work_status,
        }


# ---------------------------------------------------------------- vault model
@dataclasses.dataclass
class Link:
    src: str
    raw: str
    target: str
    alias: str | None
    embed: bool
    line: int
    col: int
    standalone: bool
    status: str
    resolved: str | None
    subpath: str | None = None  # text after the first '#' (heading / ^block / base view)
    where: str = "body"  # "body" or "frontmatter:<key>"


@dataclasses.dataclass
class Note:
    rel: str
    text: str | None
    decode_error: str | None
    has_crlf: bool
    has_bom: bool
    fm: FrontMatter
    in_task_dir: bool
    in_attachments: bool
    links: list = dataclasses.field(default_factory=list)
    fm_links: list = dataclasses.field(default_factory=list)  # whole-value "[[...]]" frontmatter strings

    @property
    def data(self):
        return self.fm.data if self.fm.data is not None else None


def _is_attachment_path(rel):
    """Any `_attachments` component (C07 uses this to find misplaced attachments)."""
    return ATT in rel.split("/")[:-1]


def _is_canonical_attachment(rel):
    """SPEC 7 location `<teamspace>/_attachments/<file>`: only .md files there are exempt from the
    note checks. A `_attachments` folder anywhere else (e.g. a page titled "_attachments") must not
    hide notes from C02/C03/C08."""
    parts = rel.split("/")
    return len(parts) == 3 and parts[0] in SPEC_TEAMSPACES and parts[1] == ATT


class Vault:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise InputError(f"vault root is not a directory: {self.root}")
        self.top, self.hidden_top = [], []
        for name in sorted(os.listdir(self.root)):
            full = os.path.join(self.root, name)
            if name.startswith("."):
                self.hidden_top.append(name)
                continue
            self.top.append((name, os.path.isdir(full) and not os.path.islink(full)))
        self.files, self.dirs = [], []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = sorted(d for d in dns if not d.startswith("."))
            rel_dp = os.path.relpath(dp, self.root)
            rel_dp = "" if rel_dp == "." else rel_dp.replace(os.sep, "/")
            for d in dns:
                self.dirs.append(posixpath.join(rel_dp, d) if rel_dp else d)
            for f in sorted(fns):
                if f.startswith("."):
                    continue
                self.files.append(posixpath.join(rel_dp, f) if rel_dp else f)
        self.file_set = set(self.files)
        self.notes = OrderedDict()
        for rel in self.files:
            if rel.endswith(".md"):
                self.notes[rel] = self._load_note(rel)
        self._hash_cache = {}

    def abspath(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def _load_note(self, rel):
        with open(self.abspath(rel), "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
            err = None
        except UnicodeDecodeError as exc:
            text, err = raw.decode("utf-8", errors="replace"), str(exc)[:200]
        has_bom = text.startswith("﻿")
        fm = parse_frontmatter(text[1:] if has_bom else text)
        return Note(rel=rel, text=text, decode_error=err, has_crlf="\r\n" in text, has_bom=has_bom, fm=fm,
                    in_task_dir=rel.startswith(TASK_DIR + "/"), in_attachments=_is_canonical_attachment(rel))

    def sha256(self, rel):
        if rel not in self._hash_cache:
            self._hash_cache[rel] = sha256_file(self.abspath(rel))
        return self._hash_cache[rel]


class Resolver:
    """Obsidian-style link resolution: vault path, relative path, unique suffix /
    unique file name; case-insensitive fallback reported separately."""

    def __init__(self, files):
        self.files = set(files)
        self.ci = defaultdict(list)
        self.by_base = defaultdict(list)
        self.by_base_ci = defaultdict(list)
        for f in files:
            self.ci[ckey(f)].append(f)
            b = f.rsplit("/", 1)[-1]
            self.by_base[b].append(f)
            self.by_base_ci[ckey(b)].append(f)

    def resolve(self, target, src):
        status, resolved, _hits = self.resolve_hits(target, src)
        return status, resolved

    def resolve_hits(self, target, src):
        """-> (status, resolved, candidates): `candidates` lists the files an `ambiguous` result was
        ambiguous between (empty otherwise). Same steps and results as `resolve`."""
        if target == "":
            return "ok", src, []
        t = target.strip()
        if t.startswith("./") or t.startswith("../"):
            p = posixpath.normpath(posixpath.join(posixpath.dirname(src), t))
            if p.startswith(".."):
                return "unresolved", None, []
            for c in (p, p + ".md"):
                if c in self.files:
                    return "ok", c, []
            for c in (p, p + ".md"):
                hits = self.ci.get(ckey(c), [])
                if len(hits) == 1:
                    return "ok_ci", hits[0], []
            return "unresolved", None, []
        t = t.lstrip("/")
        cands = (t, t + ".md")
        for c in cands:
            if c in self.files:
                return "ok", c, []
        for c in cands:
            b = c.rsplit("/", 1)[-1]
            hits = [f for f in self.by_base.get(b, ()) if "/" not in c or f.endswith("/" + c)]
            if len(hits) == 1:
                return "ok", hits[0], []
            if len(hits) > 1:
                return "ambiguous", None, hits
        for c in cands:
            hits = self.ci.get(ckey(c), [])
            if len(hits) == 1:
                return "ok_ci", hits[0], []
            b = c.rsplit("/", 1)[-1]
            hits = [f for f in self.by_base_ci.get(ckey(b), ()) if "/" not in c or ckey(f).endswith("/" + ckey(c))]
            if len(hits) == 1:
                return "ok_ci", hits[0], []
            if len(hits) > 1:
                return "ambiguous", None, hits
        return "unresolved", None, []


def _split_inner(inner):
    if "|" in inner:
        tgt, alias = inner.split("|", 1)
    else:
        tgt, alias = inner, None
    if tgt.endswith("\\"):  # "[[target\|alias]]" inside a table
        tgt = tgt[:-1]
    path_part, _h, sub = tgt.partition("#")
    return path_part.strip(), (sub.strip() if _h else None), alias


def extract_links(note, resolver):
    links = []
    lines = mask_code_lines(note.fm.body)
    for li, line in enumerate(lines):
        if "[[" not in line:
            continue
        rest = line[LINE_PREFIX_RE.match(line).end():].strip()
        for embed, inner, start, end in find_wikilinks(line):
            path_part, sub, alias = _split_inner(inner)
            status, resolved = resolver.resolve(path_part, note.rel)
            links.append(Link(src=note.rel, raw=inner, target=path_part, alias=alias, embed=embed,
                              line=note.fm.body_line0 + li, col=start, standalone=(rest == line[start:end]),
                              status=status, resolved=resolved, subpath=sub))
    return links


_FM_LINK_RE = re.compile(r"^\s*(!?)\[\[([^\]\n]*)\]\]\s*$")


def _fm_strings(val, key):
    if isinstance(val, str):
        yield key, val
    elif isinstance(val, list):
        for k, x in enumerate(val):
            yield from _fm_strings(x, f"{key}.{k}")
    elif isinstance(val, dict):
        for k, x in val.items():
            yield from _fm_strings(x, f"{key}.{k}")


def extract_fm_links(note, resolver):
    """Frontmatter values that are a single wikilink ("[[...]]"): Obsidian indexes and renders
    these as links (properties). Partial strings are not treated as links."""
    links = []
    for key, val in (note.fm.data or {}).items():
        if key == "sorting-spec":
            continue
        for kp, s in _fm_strings(val, str(key)):
            m = _FM_LINK_RE.match(s)
            if not m:
                continue
            path_part, sub, alias = _split_inner(m.group(2))
            status, resolved = resolver.resolve(path_part, note.rel)
            links.append(Link(src=note.rel, raw=m.group(2), target=path_part, alias=alias, embed=bool(m.group(1)),
                              line=1, col=0, standalone=False, status=status, resolved=resolved, subpath=sub,
                              where="frontmatter:" + kp))
    return links


_HEADING_LINE_RE = re.compile(r"^(?:[ \t]*>[ \t]?)*[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9-]+)[ \t]*$")
_HEADING_JUNK_RE = re.compile(r"[#|^:%\[\]\\]")


def norm_heading(s):
    """Lenient heading-subpath key (Obsidian strips # | ^ : % [ ] from heading links; case-insensitive)."""
    return " ".join(_HEADING_JUNK_RE.sub(" ", nfc(s)).casefold().split())


def note_anchors(text_body):
    heads, blocks = set(), set()
    for line in mask_code_lines(text_body):
        m = _HEADING_LINE_RE.match(line)
        if m:
            heads.add(norm_heading(m.group(2)))
        b = _BLOCK_ID_RE.search(line)
        if b:
            blocks.add(b.group(1))
    return heads, blocks


# ---------------------------------------------------------------- WRITEBACK_SPEC additions
# 5.1a: tool-generated notes the write-back / refresh tools add under Notion_Obsidian/ (accepted when they carry
# `notion_kind: generated`, no notion_id and no wikilink / embed; checked in C08)
GENERATED_FILE_PATTERNS = (
    # (kind, folder, name prefix, name suffix)
    ("writeback_preview", TASK_DIR + "/写回预览", "", ".md"),
    ("writeback_runlog", TASK_DIR + "/运行记录", "写回-", ".md"),
    ("paused_runlog", TASK_DIR + "/运行记录", "", "-已暂停.md"),
    ("unfinished_runlog", TASK_DIR + "/运行记录", "", "-未完成.md"),
)


def generated_file_kind(rel):
    """WRITEBACK_SPEC 5.1a generated-file kind of a vault path, or None."""
    r = nfc(rel)
    d, b = dname(r), bname(r)
    for kind, folder, pre, suf in GENERATED_FILE_PATTERNS:
        if d == folder and b.startswith(pre) and b.endswith(suf) and len(b) > len(pre) + len(suf):
            return kind
    return None


# --expected-deviations (WRITEBACK_SPEC 14.0 / 7.4.4). Categories and the issues they may turn into INFO:
#   local_new           (path)  user-created file without notion_id: C01 extra top-level item made only of listed
#                               files, C02 md_without_notion_id, C06 links FROM it, C07 its own attachment-name issues
#                               and links from it, C08 every issue of it, C09 its name (and a folder holding only
#                               listed files), NFC/case collisions it causes.
#   sync_conflict_copy  (path)  Obsidian Sync "(Conflicted copy …)" file: C02 duplicates where every path but ONE is a
#                               listed copy, md_without_notion_id, C06 links from it, C08 / C09 issues of it.
#                               Ctx.P never picks a listed copy.
#   hold / paused       (id, subtree by default)  objects the publisher does not write this round: every issue that
#                               is located at such an object (C02 missing / extra, C03, C04, C05 index / listing of
#                               its vault name, C06 links from it and unresolved links naming it (a SPEC name of its
#                               title, or a listed `paths` entry), C07 its files, C10) and, in C08, only staleness
#                               (notion_title / time / aliases / icon / cover / row property): format and YAML
#                               damage of a held note keeps failing. C02 duplicates of an id only when the id's OWN
#                               entry lists every one of its notes (bar listed Sync copies) among its validated
#                               paths (WB 14.3 映射异常: the scan's mapping_duplicate hold lists them all).
#   name_override       (id)    local same-folder rename in effect at `local_rel` (the note of that id IS at
#                               local_rel): C03 name_not_derived_from_title, and C08 aliases_missing_original_title
#                               when the title-derived name equals the title (L-11: aliases follow the title-derived
#                               name, WRITEBACK_SPEC 7.4.3 / D-22), listed as 命名差异. The exporter
#                               rewrites links / sortspec / index to local_rel, so C04 / C05 / C06 issues stay FAILs.
#   local_modified      (id / path; alias local_only)  local edit kept in the vault (LOCAL_ONLY / CONFLICT_*):
#                               C04 order / child links of its body, C06 links from it, C07 its references,
#                               C08 notion_title / time / icon / cover mismatches, C10 its missing blocks.
# Never downgraded: C02 duplicates of a name_override id (unless held as above: a note the hold entry does not list,
# e.g. a re-ADDed old_rel, keeps failing), missing_not_in_snapshot, row_source_unavailable, orphans,
# generated_outside_task_dir; C03 issues of non-held objects other than the override name; C05 syntax / section
# errors; C09 object_name_collisions; C11. Unknown top-level keys are ignored (reported); a malformed known key is
# an input error (exit 2).
# Entries are checked against the vault scan before use (Deviations.bind); an entry that would excuse something it
# does not describe is REJECTED (reported in `rejected_entries`, never used), so its issues keep failing:
#   local_new           the path (or any file under a listed folder) has a notion_id, is a generated note, a
#                       database .base, an exporter-named attachment (`__<sha8>` under _attachments), lies in
#                       Notion_Obsidian/, or is a teamspace folder;
#   sync_conflict_copy  the name does not match SYNC_CONFLICT_RE, or the file's notion_id differs from the listed one;
#   hold / paused path  an existing folder, or an existing file that is not a note of a held object;
#   local_modified      the listed path exists but has no notion_id or belongs to another id.
DEV_CHECKS = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10")
DEV_KEYS = ("local_new", "hold_ids", "hold", "paused", "name_overrides", "sync_conflict_copies",
            "local_modified", "local_only")
DEV_META_KEYS = ("schema", "run_id", "generated_by", "generated_at", "created_at", "vault", "notes", "comment",
                 "counts", "snapshot")
HOLD_CATS = ("hold", "paused")
# C08 issues a held (not re-published, stale) note may show; anything else (format, YAML, fields) keeps failing
HOLD_C08_ISSUES = ("notion_title_mismatch", "time_mismatch", "aliases_missing_original_title", "row_property_missing")
HOLD_C08_PREFIXES = ("notion_icon_", "notion_cover_")
SCOPED_CHECKS = ("C03", "C04", "C06", "C08", "C10")
# Obsidian Sync conflict copies (contract 5.1, same pattern as wb_common.SYNC_CONFLICT_RE; not imported: independent)
SYNC_CONFLICT_RE = re.compile(r" \(Conflicted copy [^)]*\)\.md$")
_ID8_SUFFIX_RE = re.compile(r" \(([0-9a-f]{8})\)$")


def _dev_rel(value, where):
    """Validated vault-relative path from the deviations file (NFC, '/'-separated, no '.'/'..'/empty parts)."""
    if not isinstance(value, str) or value.rstrip("/") == "":
        raise InputError(f"--expected-deviations {where}: a path must be a non-empty string")
    if value.startswith("/") or os.path.isabs(value) or re.match(r"^[A-Za-z]:", value):
        raise InputError(f"--expected-deviations {where}: path must be vault-relative: {value!r}")
    value = value.rstrip("/")  # a listed folder may be written "Inbox/"
    if any(p in ("", ".", "..") for p in value.split("/")):
        raise InputError(f"--expected-deviations {where}: path must not contain empty, '.' or '..' parts: {value!r}")
    return nfc(value)


def _dev_id(value, where):
    i = canon_id(value)
    if i is None:
        raise InputError(f"--expected-deviations {where}: not a Notion id: {str(value)[:80]!r}")
    return i


def _dev_items(data, key):
    val = data.get(key)
    if val is None:
        return []
    if isinstance(val, dict):  # {id: {...}} / {id: "local_rel"} forms
        return [(k, v) for k, v in val.items()]
    if isinstance(val, list):
        return [(None, v) for v in val]
    raise InputError(f"--expected-deviations: '{key}' must be a list or an object")


class Deviations:
    """Parsed --expected-deviations file. `bind(ctx)` resolves ids / paths against one vault scan; checks then
    ask `by_id` / `by_path` / `link_target` / `name_key` and `record` what they turned into INFO."""

    def __init__(self, path, data, sha):
        if not isinstance(data, dict):
            raise InputError("--expected-deviations must be a JSON object")
        self.path, self.sha256 = os.path.abspath(path), sha
        self.local_new = OrderedDict()       # nfc rel -> True
        self.sync_copies = OrderedDict()     # nfc rel -> id | None
        self.staged_folder_notes = {}     # post-publish placement of the unheld children of a retained note
        self.hold = OrderedDict()            # id -> {"cat", "subtree", "paths"}
        self.name_overrides = OrderedDict()  # id -> {"old_rel", "local_rel"}
        self.local_modified = OrderedDict()  # key (id or nfc rel) -> {"id", "path"}
        self.rejected_parse = []             # [(category, entry dict, reason)]: well-formed but never used
        self.ignored_keys = sorted(k for k in data if k not in DEV_KEYS and k not in DEV_META_KEYS)
        for k, v in _dev_items(data, "local_new"):
            if k is not None:  # {rel: ...}
                p = k
            else:
                p = v.get("path", v.get("rel")) if isinstance(v, dict) else v
            self.local_new[_dev_rel(p, "local_new")] = True
        for k, v in _dev_items(data, "sync_conflict_copies"):
            if k is not None:  # {rel: notion_id | null | {...}}
                rel, nid = k, (v.get("notion_id", v.get("id")) if isinstance(v, dict) else v)
            elif isinstance(v, dict):
                rel, nid = v.get("path", v.get("rel")), v.get("notion_id", v.get("id"))
            else:
                rel, nid = v, None
            rel = _dev_rel(rel, "sync_conflict_copies")
            nid = _dev_id(nid, "sync_conflict_copies") if nid is not None else None
            if not SYNC_CONFLICT_RE.search(bname(rel)):
                # only an Obsidian Sync conflict name may be a sync copy (a plain copy is a real duplicate: only
                # its object's own hold entry listing every note registers it, see check_c02)
                self.rejected_parse.append(("sync_conflict_copy", {"path": rel, "notion_id": nid},
                                            "name_is_not_a_sync_conflict_copy"))
                continue
            self.sync_copies[rel] = nid
        for key, cat in (("hold_ids", "hold"), ("hold", "hold"), ("paused", "paused")):
            for k, v in _dev_items(data, key):
                if isinstance(v, dict):
                    nid = _dev_id(v.get("notion_id", v.get("id", k)), key)
                    if "staged_folder_note" in v:
                        self.staged_folder_notes[nid] = _dev_rel(v["staged_folder_note"], "staged_folder_note")
                    sub = v.get("subtree", True)
                    if not isinstance(sub, bool):
                        raise InputError(f"--expected-deviations {key}: 'subtree' must be true/false")
                    paths = v.get("paths") or ([v["path"]] if isinstance(v.get("path"), str) else [])
                    if not isinstance(paths, list):
                        raise InputError(f"--expected-deviations {key}: 'paths' must be a list")
                    paths = [_dev_rel(p, key) for p in paths]
                else:
                    nid, sub, paths = _dev_id(v if k is None else k, key), True, []
                h = self.hold.setdefault(nid, {"cat": cat, "subtree": sub, "paths": []})
                h["subtree"] = h["subtree"] or sub
                h["paths"] += [p for p in paths if p not in h["paths"]]
        for k, v in _dev_items(data, "name_overrides"):
            if isinstance(v, dict):
                nid = _dev_id(v.get("notion_id", v.get("id", k)), "name_overrides")
                local, old = v.get("local_rel"), v.get("old_rel")
            elif k is not None:
                nid, local, old = _dev_id(k, "name_overrides"), v, None
            else:
                raise InputError("--expected-deviations name_overrides: entries must be objects "
                                 "{notion_id, old_rel, local_rel}")
            if not isinstance(local, str) or not local.endswith(".md"):
                raise InputError(f"--expected-deviations name_overrides: local_rel must be a .md path ({nid})")
            self.name_overrides[nid] = {"local_rel": _dev_rel(local, "name_overrides"),
                                        "old_rel": _dev_rel(old, "name_overrides") if old is not None else None}
        bare_id = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
        for key in ("local_modified", "local_only"):
            for k, v in _dev_items(data, key):
                if isinstance(v, dict):
                    nid = v.get("notion_id", v.get("id", k))
                    p = v.get("path", v.get("rel"))
                elif k is not None:  # {notion_id: path}
                    nid, p = k, v
                elif isinstance(v, str) and bare_id.match(v):
                    nid, p = v, None
                else:
                    nid, p = None, v
                nid = _dev_id(nid, key) if nid is not None else None
                p = _dev_rel(p, key) if p is not None else None
                if nid is None and p is None:
                    raise InputError(f"--expected-deviations {key}: an entry needs notion_id or path")
                self.local_modified.setdefault(nid or p, {"id": nid, "path": p})
        # entries validated against one vault scan (bind); until then everything parsed counts as listed
        self.local_new_ok, self.sync_ok = list(self.local_new), list(self.sync_copies)
        self.hold_paths_ok = {i: list(h["paths"]) for i, h in self.hold.items()}
        self.local_modified_ok = OrderedDict(self.local_modified)
        self.rejected_bind = []
        self.bound = False

    def _validate(self, ctx):
        """Reject entries that would excuse issues of something they do not describe (fail closed: a wrong or stale
        deviations file must never hide a failure of an exported note). Needs self.held."""
        v = ctx.v
        files, dirs = defaultdict(list), {nfc(d) for d in v.dirs}
        for f in v.files:
            files[nfc(f)].append(f)
        rejected = []

        def exported(f):  # why the vault file f (raw path) is not a user-created file, or None
            if f in ctx.path_id:
                return "has_notion_id"
            n = v.notes.get(f)
            fmd = n.fm.data if n is not None and isinstance(n.fm.data, dict) else {}
            if fmd.get("notion_id") not in (None, ""):
                return "has_notion_id_field"
            if fmd.get("notion_kind") == "generated":
                return "generated_note"
            if f in ctx.base_id:
                return "database_base_file"
            if _is_attachment_path(f) and SHA8_NAME_RE.search(bname(f)):
                return "exporter_attachment_name"
            return None

        def local_new_reason(rel):
            if rel == TASK_DIR or rel.startswith(TASK_DIR + "/"):
                return "tool_file_in_task_dir"
            if rel in SPEC_TEAMSPACES:
                return "teamspace_folder"
            for f in files.get(rel, ()):
                why = exported(f)
                if why:
                    return why
            if rel in dirs:
                for f in v.files:
                    if nfc(f).startswith(rel + "/"):
                        why = exported(f)
                        if why:
                            return f"folder_holds_{why}: {f}"
            return None  # a user file, or absent (then simply unmatched)

        self.local_new_ok = []
        for rel in self.local_new:
            why = local_new_reason(rel)
            if why:
                rejected.append(("local_new", {"path": rel}, why))
            else:
                self.local_new_ok.append(rel)
        self.sync_ok = []
        for rel, nid in self.sync_copies.items():
            owners = {ctx.path_id.get(f) for f in files.get(rel, ())}
            if nid is not None and owners and owners != {nid}:
                rejected.append(("sync_conflict_copy", {"path": rel, "notion_id": nid},
                                 "listed_notion_id_differs_from_file"))
            else:
                self.sync_ok.append(rel)
        self.hold_paths_ok = {}
        for hid, h in self.hold.items():
            self.hold_paths_ok[hid] = []
            for p in h["paths"]:
                fs = files.get(p, [])
                if p in dirs:
                    why = "path_is_an_existing_folder"
                elif fs and not all(ctx.path_id.get(f) in self.held for f in fs):
                    why = "path_is_not_a_note_of_a_held_object"
                else:
                    why = None  # absent (an old name: link matching only) or a held object's own note
                if why:
                    rejected.append((h["cat"], {"notion_id": hid, "path": p}, why))
                else:
                    self.hold_paths_ok[hid].append(p)
        self.local_modified_ok = OrderedDict()
        for key, lm in self.local_modified.items():
            why = None
            if lm["path"] is not None:
                fs = files.get(lm["path"], [])
                owners = {ctx.path_id.get(f) for f in fs}
                if lm["path"] in dirs:
                    why = "path_is_a_folder"
                elif None in owners:
                    why = "path_has_no_notion_id"
                elif lm["id"] and owners and owners != {lm["id"]}:
                    why = "path_belongs_to_another_notion_id"
            if why:
                rejected.append(("local_modified", {"notion_id": lm["id"], "path": lm["path"]}, why))
            else:
                self.local_modified_ok[key] = lm
        # A retained folder note can stay behind while its children move with Notion. Accept the
        # staged folder only for that explicit hold and check its name and ancestry against the
        # snapshot. C03 and C05 still test the actual locations and order of every unheld child.
        for nid, rel in sorted(self.staged_folder_notes.items(), key=lambda item: item[1].count("/")):
            obj = ctx.exp.objs.get(nid)
            actual = [p for p in ctx.ids.get(nid, ()) if nfc(p) not in self.sync_ok]
            folder = dname(rel)
            if (len(actual) == 1 and obj and (obj.folder_expected or obj.structure_flexible)
                    and rel.endswith(".md") and ctx.is_folder_path(rel)
                    and name_matches(stem_of(rel), obj.title, nid)
                    and dname(folder) == ctx.container_of(nid)
                    and not files.get(rel) and rel not in dirs):
                ctx.layout_paths[nid] = rel
            else:
                rejected.append((self.hold[nid]["cat"], {"notion_id": nid, "path": rel},
                                 "invalid_staged_folder_note"))
        self.rejected_bind = rejected

    # -- binding to one vault scan
    def bind(self, ctx):
        exp = ctx.exp
        self._objs, self._ctx = exp.objs, ctx
        self.info = OrderedDict()   # check -> OrderedDict(list name -> [items])
        self.used = Counter()       # (check, category) -> n
        self.used_entries = set()
        self.held = OrderedDict()   # id -> (cat, entry key)
        for hid, h in self.hold.items():
            ek = (h["cat"], hid)
            todo = [hid]
            while todo:
                i = todo.pop()
                if i in self.held:
                    continue
                self.held[i] = (h["cat"], ek)
                e = exp.objs.get(i)
                if e is not None and h["subtree"]:
                    todo.extend(reversed(list(e.children) + list(e.templates)))
        self._validate(ctx)  # from here on only the validated views (*_ok) are used
        self.id_hits = defaultdict(dict)
        for i, (cat, ek) in self.held.items():
            self.id_hits[i].setdefault(cat, (cat, (ek,)))
        for key, lm in self.local_modified_ok.items():
            if lm["id"]:
                self.id_hits[lm["id"]].setdefault("local_modified", ("local_modified", (("local_modified", key),)))
        self.path_hits = defaultdict(dict)

        def addp(rel, cat, ek):
            self.path_hits[nfc(rel)].setdefault(cat, (cat, (ek,)))

        for rel in self.local_new_ok:
            addp(rel, "local_new", ("local_new", rel))
        for rel in self.sync_ok:
            addp(rel, "sync_conflict_copy", ("sync_conflict_copy", rel))
        for i, (cat, ek) in self.held.items():
            for p in ctx.ids.get(i, ()):
                addp(p, cat, ek)
        for hid, h in self.hold.items():
            for p in self.hold_paths_ok[hid]:
                addp(p, h["cat"], (h["cat"], hid))
        for key, lm in self.local_modified_ok.items():
            ek = ("local_modified", key)
            if lm["path"]:
                addp(lm["path"], "local_modified", ek)
            for p in (ctx.ids.get(lm["id"], ()) if lm["id"] else ()):
                addp(p, "local_modified", ek)
        # unresolved link targets: explicitly listed paths of held objects (exact, with or without .md) and their
        # stems. NOT a name override's old_rel: the exporter rewrites every link / sortspec / index entry to
        # local_rel (WRITEBACK_SPEC 7.4.3), so a link still naming old_rel is a real defect.
        self.old_targets, self.old_stems = {}, defaultdict(list)
        olds = [(p, (h["cat"], ((h["cat"], hid),))) for hid, h in self.hold.items() for p in self.hold_paths_ok[hid]]
        for rel, hit in olds:
            self.old_targets.setdefault(rel, hit)
            if rel.endswith(".md"):
                self.old_targets.setdefault(rel[:-3], hit)
            self.old_stems[stem_of(rel)].append(hit)
        # held objects by any SPEC-conformant name (an unresolved link to a held note's current Notion name)
        self.held_names, self.held_id8 = defaultdict(set), defaultdict(set)
        for i in self.held:
            e = exp.objs.get(i)
            if e is None:
                continue
            for var in _sanitize_variants(e.title):
                self.held_names[var].add(i)
            self.held_id8[i.replace("-", "")[:8]].add(i)
        # C05 listing keys (folder label, entry name) of held objects' vault files (a stale held note keeps its old
        # name while the regenerated sortspec lists the current one). Not name overrides (see old_targets).
        self.name_keys = {}
        for i, (cat, ek) in self.held.items():
            for p in ctx.ids.get(i, ()):
                p = nfc(p)
                self.name_keys.setdefault((dname(p) or "/", bname(p)), (cat, (ek,)))
                if "/" in dname(p) and stem_of(p) == bname(dname(p)):
                    self.name_keys.setdefault((dname(dname(p)) or "/", bname(dname(p))), (cat, (ek,)))
        self.bound = True

    # -- lookups (each returns a hit = (category, entry keys) or None)
    def by_id(self, i, cats):
        h = self.id_hits.get(i) if i else None
        if not h:
            return None
        return next((h[c] for c in cats if c in h), None)

    def by_path(self, rel, cats):
        h = self.path_hits.get(nfc(rel)) if isinstance(rel, str) else None
        if not h:
            return None
        return next((h[c] for c in cats if c in h), None)

    def _fits_container(self, t, stem, i):
        """A link with a folder part must point into the folder object i would live in (when that is known)."""
        if "/" not in t:
            return True
        try:
            cont = self._ctx.container_of(i)
        except KeyError:
            return True
        if cont is None:  # parent note missing too (held subtree, or C02 reports it)
            return True
        cont, d = nfc(cont), dname(t)
        return d == cont or (dname(d) == cont and bname(d) == stem)

    def link_target(self, target):
        """Hit for an UNRESOLVED link target that names a held object (a listed path, or a SPEC name of its
        current title in the folder it belongs to)."""
        t = nfc((target or "").strip().lstrip("/"))
        if not t:
            return None
        hit = self.old_targets.get(t)
        if hit:
            return hit
        stem = stem_of(t) if t.endswith(".md") else bname(t)
        if "/" not in t and self.old_stems.get(stem):
            return self.old_stems[stem][0]
        cands, base, id8s = set(), stem, set()
        cands |= self.held_names.get(base, set())
        while True:
            m = _ID8_SUFFIX_RE.search(base)
            if not m:
                break
            id8s.add(m.group(1))
            base = base[: m.start()]
            cands |= self.held_names.get(base, set())
        for x in id8s:
            cands |= self.held_id8.get(x, set())
        for i in sorted(cands):
            e = self._objs.get(i)
            if e is not None and name_matches(stem, e.title, i) and self._fits_container(t, stem, i):
                return self.held[i][0], (self.held[i][1],)
        return None

    def name_key(self, folder_label, name):
        return self.name_keys.get((nfc(folder_label or "/"), nfc(name))) if isinstance(name, str) else None

    def record(self, check, list_name, item, hit):
        cat, eks = hit
        info_item = dict(item, deviation=cat) if isinstance(item, dict) else {"item": item, "deviation": cat}
        self.info.setdefault(check, OrderedDict()).setdefault(list_name, []).append(info_item)
        self.used[(check, cat)] += 1
        self.used_entries.update(eks)

    def split(self, check, list_name, items, match):
        """Items `match` maps to a hit go to INFO; the rest (returned) keep failing the check."""
        if not items:
            return items
        keep = []
        for it in items:
            hit = match(it)
            if hit:
                self.record(check, list_name, it, hit)
            else:
                keep.append(it)
        return keep

    # -- reporting
    def entries(self):
        """Accepted (not rejected) entries: (category, entry dict, entry key)."""
        out = []
        out += [("local_new", {"path": p}, ("local_new", p)) for p in self.local_new_ok]
        out += [("sync_conflict_copy", {"path": p, "notion_id": self.sync_copies[p]}, ("sync_conflict_copy", p))
                for p in self.sync_ok]
        out += [(h["cat"], {"notion_id": i, "subtree": h["subtree"], "paths": self.hold_paths_ok[i]}, (h["cat"], i))
                for i, h in self.hold.items()]
        out += [("name_override", dict({"notion_id": i}, **ov), ("name_override", i))
                for i, ov in self.name_overrides.items()]
        out += [("local_modified", {"notion_id": lm["id"], "path": lm["path"]}, ("local_modified", k))
                for k, lm in self.local_modified_ok.items()]
        return out

    def rejected(self):
        return [dict(d, category=cat, reason=why) for cat, d, why in self.rejected_parse + self.rejected_bind]

    def report(self):
        by_check = OrderedDict((c, sum(len(v) for v in self.info.get(c, {}).values())) for c in DEV_CHECKS)
        by_cat = Counter()
        for (_c, cat), n in self.used.items():
            by_cat[cat] += n
        naming = [{"id": it.get("id"), "path": it.get("path"), "found": it.get("found"),
                   "expected": it.get("expected"), "old_rel": (self.name_overrides.get(it.get("id")) or {}).get("old_rel")}
                  for it in self.info.get("C03", {}).get("issues", []) if it.get("deviation") == "name_override"]
        ents = self.entries()
        return OrderedDict([
            ("file", self.path), ("sha256", self.sha256),
            ("total", sum(by_check.values())), ("by_check", by_check), ("by_category", dict(sorted(by_cat.items()))),
            ("entries", dict(Counter(cat for cat, _d, _k in ents))),
            ("held_ids_with_subtree", len(self.held)),
            ("name_overrides", naming),
            ("unmatched_entries(info)", [dict(d, category=cat) for cat, d, k in ents if k not in self.used_entries]),
            ("rejected_entries", self.rejected()),
            ("ignored_keys", self.ignored_keys),
            ("rule", "issues caused by a listed object are INFO (not counted in the check status); unlisted issues "
                     "keep failing (WRITEBACK_SPEC 14.0 / 7.4.4); an entry that does not describe what it names "
                     "(rejected_entries) is never used, so the issues it names keep failing"),
        ])


def load_expected_deviations(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise InputError(f"--expected-deviations unreadable: {exc}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"--expected-deviations is not UTF-8 JSON: {str(exc)[:200]}") from None
    return Deviations(path, data, hashlib.sha256(raw).hexdigest())


class Scope:
    """--ids: the objects C03 / C04 / C06 / C08 / C10 are restricted to (WRITEBACK_SPEC 12 W6)."""

    def __init__(self, path, ids, sha):
        self.path, self.sha256, self.ids = os.path.abspath(path), sha, set(ids)
        self.order = list(ids)
        self.parents, self.paths = set(), set()
        self.checked, self.trashed_absent, self.unverifiable = [], [], []
        self.not_in_expectation, self.without_note = [], []

    def bind(self, ctx):
        """Fail closed: every listed id must be checkable (an exported object with a note in the vault) or be
        verified as gone (trashed in the snapshot AND no note in the vault, e.g. a page trashed by write-back).
        Anything else (a block / data-source id, an object the refetch missed, a written page whose note is
        missing, a trashed object whose note is still there) is `unverifiable` and FAILs C03."""
        exp = ctx.exp
        self.parents = {exp.objs[i].parent for i in self.ids if i in exp.objs and exp.objs[i].parent}
        self.paths = {p for i in self.ids for p in ctx.ids.get(i, ())}
        self.not_in_expectation = [i for i in self.order if i not in exp.objs]
        self.without_note = [i for i in self.order if i not in ctx.ids]
        self.checked, self.trashed_absent, self.unverifiable = [], [], []
        for i in self.order:
            in_exp, has_note = i in exp.objs, i in ctx.ids
            if in_exp and has_note:
                self.checked.append(i)
                continue
            o = exp.objects.get(i)
            trashed = i in exp._trashed_ids or (o is not None and is_trashed(o[2]))
            if trashed and not in_exp and not has_note:
                self.trashed_absent.append(i)
                continue
            if in_exp:
                why = "expected_object_has_no_note_in_vault"
            elif has_note:
                why = "note_in_vault_for_trashed_object" if trashed else "note_in_vault_for_object_not_expected"
            elif i in exp._gap_ids:
                why = "snapshot_gap(object_not_fetched)"
            elif i in exp.data_sources:
                why = "data_source_id(list_its_database_or_rows)"
            elif o is not None:
                why = f"not_an_exported_object(snapshot_kind={o[0]})"
            elif i in exp.block_page:
                why = f"block_id(list_its_page:{exp.block_page[i]})"
            else:
                why = "unknown_to_snapshot(not_refetched?)"
            self.unverifiable.append({"id": i, "reason": why, "paths": sorted(ctx.ids.get(i, ()))})

    def report(self):
        return OrderedDict([
            ("file", self.path), ("sha256", self.sha256), ("ids", len(self.ids)), ("listed", self.order),
            ("counts", {"checked": len(self.checked), "trashed_absent": len(self.trashed_absent),
                        "unverifiable": len(self.unverifiable)}),
            ("ids_checked", self.checked),
            ("ids_trashed_in_snapshot_and_absent(info)", self.trashed_absent),
            ("ids_unverifiable", self.unverifiable),
            ("ids_not_in_expectation", self.not_in_expectation),
            ("ids_without_note_in_vault", self.without_note),
            ("notes_in_scope", len(self.paths)),
            ("checks_scoped", list(SCOPED_CHECKS)),
            ("checks_full_vault", [c for c in CHECK_TITLES if c not in SCOPED_CHECKS]),
            ("rule", "each listed id must be an exported object with a note in the vault (checked by C03/C04/C06/"
                     "C08/C10) or trashed in the snapshot with no note in the vault (info); any other id is "
                     "unverifiable and FAILs C03 (scope_ids_unverifiable)"),
        ])


def load_ids(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"--ids unreadable: {exc}") from None
    ids = []
    for n, line in enumerate(text.split("\n"), 1):
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        i = canon_id(s)
        if i is None:
            raise InputError(f"--ids line {n}: not a Notion id: {s[:80]!r}")
        if i not in ids:
            ids.append(i)
    if not ids:
        raise InputError("--ids lists no notion_id (an empty scope would check nothing)")
    return Scope(path, ids, hashlib.sha256(raw).hexdigest())


class Ctx:
    """Vault scan joined with the expectation model."""

    def __init__(self, exp, vault, dev=None, scope=None):
        self.exp, self.v = exp, vault
        self.dev, self.scope = dev, scope
        self.ids = defaultdict(list)
        self.unidentified = []
        self.generated_outside_task_dir = []
        for rel, n in vault.notes.items():
            if n.in_task_dir or n.in_attachments:
                continue
            fm = n.fm.data or {}
            if fm.get("notion_kind") == "generated":
                # SPEC 4/9: only Notion_Obsidian/ holds generated notes; elsewhere "generated" would
                # hide a note from every identity / structure / frontmatter check
                self.generated_outside_task_dir.append(rel)
                continue
            nid = canon_id(fm.get("notion_id"))
            if not nid:
                self.unidentified.append(rel)
                continue
            self.ids[nid].append(rel)
        # canonical path per id; for duplicated ids prefer the copy whose name is
        # derived from the title, so one stray copy does not cascade into C03/C05
        self.P = {}
        for i, ps in self.ids.items():
            ps = sorted(ps)
            e = exp.objs.get(i)
            if len(ps) > 1 and e is not None:
                good = [p for p in ps if name_matches(
                    bname(dname(p)) if ("/" in dname(p) and stem_of(p) == bname(dname(p))) else stem_of(p),
                    e.title, i)]
                ps = good + [p for p in ps if p not in good]
            if dev is not None and len(ps) > 1:
                # registered deviations: the override's local_rel is THE note of its id, a listed Sync
                # conflict copy never is (stable sort keeps the order above otherwise)
                pref = (dev.name_overrides.get(i) or {}).get("local_rel")
                ps = sorted(ps, key=lambda p: (nfc(p) != pref, dev.sync_copies.get(nfc(p), "-") in (None, i)))
            self.P[i] = ps[0]
        # Actual note paths drive content and identity checks. Layout paths are identical except
        # for held folder notes whose children were published at the verified Notion location.
        self.layout_paths = dict(self.P)
        self.path_id = {rel: i for i, ps in self.ids.items() for rel in ps}
        self.base_id = {}
        for i, rel in self.P.items():
            e = exp.objs.get(i)
            kind = e.kind if e else (vault.notes[rel].fm.data or {}).get("notion_kind")
            if kind == "database":
                self.base_id[posixpath.join(dname(rel), stem_of(rel) + ".base")] = i
        self.resolver = Resolver(vault.files)
        for n in vault.notes.values():
            if not n.in_attachments:
                n.links = extract_links(n, self.resolver)
                n.fm_links = extract_fm_links(n, self.resolver)
        self._anchor_cache = {}
        if dev is not None:
            dev.bind(self)
        if scope is not None:
            scope.bind(self)

    def anchors(self, rel):
        """(normalized headings, block ids) of a note, or ({view names}, None) for a .base file."""
        if rel not in self._anchor_cache:
            if rel.endswith(".base"):
                views = set()
                try:
                    with open(self.v.abspath(rel), encoding="utf-8") as fh:
                        data, _err = load_yaml(fh.read())
                    for vw in ((data or {}).get("views") or []):
                        if isinstance(vw, dict) and isinstance(vw.get("name"), str):
                            views.add(vw["name"])
                except (OSError, UnicodeDecodeError):
                    pass
                self._anchor_cache[rel] = (views, None)
            elif rel in self.v.notes:
                self._anchor_cache[rel] = note_anchors(self.v.notes[rel].fm.body)
            else:
                self._anchor_cache[rel] = None
        return self._anchor_cache[rel]

    def subpath_ok(self, ln):
        """None = nothing to check; True/False = the #subpath resolves inside the target."""
        if not ln.subpath or ln.status not in ("ok", "ok_ci"):
            return None
        a = self.anchors(ln.resolved)
        if a is None:
            return None  # attachments (e.g. pdf#page=2) are not checked
        if a[1] is None:  # .base: embed view name must match exactly
            return ln.subpath in a[0]
        heads, blocks = a
        sub = ln.subpath
        if sub.startswith("^"):
            return sub[1:] in blocks
        last = [x for x in sub.split("#") if x.strip()]
        return bool(last) and norm_heading(last[-1]) in heads

    def target_id(self, path):
        if path is None:
            return None
        return self.path_id.get(path) or self.base_id.get(path)

    @staticmethod
    def is_folder_path(rel):
        # object folders always live inside a teamspace folder, so a folder note's
        # directory has at least two components ("<teamspace>/<name>/<name>.md")
        return "/" in dname(rel) and stem_of(rel) == bname(dname(rel))

    def is_folder_note(self, i):
        return self.is_folder_path(self.P[i])

    def vault_name(self, i):
        rel = self.P[i]
        return bname(dname(rel)) if self.is_folder_note(i) else stem_of(rel)

    def children_folder(self, i):
        rel = self.layout_paths[i]
        return dname(rel) if self.is_folder_path(rel) else posixpath.join(dname(rel), stem_of(rel))

    def container_of(self, i):
        """Folder that must hold object i (its note, or its folder): the teamspace for a root,
        `<db folder>/_templates` for a template (SPEC v1.2 section 13), else the parent's children
        folder. None when the parent note is missing."""
        e = self.exp.objs[i]
        if e.is_root:
            return e.teamspace
        if e.parent not in self.P:
            return None
        base = self.children_folder(e.parent)
        return posixpath.join(base, TEMPLATES) if e.kind == "template" else base


# ---------------------------------------------------------------- checks
def _res(ok, summary, details):
    return {"status": "PASS" if ok else "FAIL", "summary": summary, "details": details}


def check_c01(ctx):
    names = {n: is_dir for n, is_dir in ctx.v.top}
    expected = SPEC_TEAMSPACES + [TASK_DIR]
    missing = [n for n in expected if n not in names]
    not_dir = [n for n in expected if n in names and not names[n]]
    extra = [n for n in names if n not in expected]
    near = [n for n in extra if any(ckey(n) == ckey(e) for e in expected)]
    if ctx.dev is not None:
        dev = ctx.dev

        def m_extra(n):
            # a top-level item made only of listed user files (a file, or a folder whose every file is listed)
            files = [f for f in ctx.v.files if f == n or f.startswith(n + "/")]
            hits = [dev.by_path(f, ("local_new", "sync_conflict_copy")) for f in files]
            if not files or not all(hits):
                return None
            return hits[0][0], tuple(k for h in hits for k in h[1])

        extra = dev.split("C01", "extra", extra, m_extra)
    ok = not (missing or not_dir or extra)
    return _res(ok, f"top-level non-hidden items={len(names)} missing={len(missing)} extra={len(extra)} "
                    f"not_dir={len(not_dir)}",
                {"top_level": [n for n, _ in ctx.v.top], "hidden_top_level_ignored": ctx.v.hidden_top,
                 "missing": missing, "extra": extra, "not_a_directory": not_dir,
                 "extra_differing_only_by_case_or_normalization": near})


def check_c02(ctx):
    exp = ctx.exp
    expected = set(exp.objs)
    present = set(ctx.ids)
    gap_ids = {g["id"] for g in exp.gaps}
    missing = []
    for i in exp.objs:
        if i in present:
            continue
        e = exp.objs[i]
        item = {"id": i, "kind": e.kind, "title": e.title, "teamspace": e.teamspace, "parent": e.parent,
                "fetched_at": exp.fetched_at.get(i)}
        # classification only (never changes the status): helps separate "the snapshot grew after the
        # export" from "the exporter dropped an object it had"
        if e.parent is not None and e.parent not in present:
            item["cause_hint"] = "parent_also_missing"
        elif exp.as_of and item["fetched_at"] and item["fetched_at"] > exp.as_of:
            item["cause_hint"] = "fetched_after_as_of(snapshot_newer_than_export)"
        elif e.parent is not None:
            pnote = ctx.v.notes.get(ctx.P.get(e.parent))
            hexid = i.replace("-", "")
            mention = pnote is not None and (hexid in pnote.text or i in pnote.text)
            item["cause_hint"] = ("parent_note_mentions_child_id_only(rendered_as_notion_url?)" if mention else
                                  "parent_note_has_no_link_or_id_of_child")
        missing.append(item)
    missing_gap = [g for g in exp.gaps if g["id"] not in present]
    extra = []
    for i in sorted(present - expected - gap_ids):
        o = exp.objects.get(i)
        if o is None:
            why = "unknown_to_snapshot"
        elif i in exp._trashed_ids or is_trashed(o[2]):
            why = "trashed_in_snapshot"
        elif i in exp._reference_targets:
            why = "reference_only(api_parent_elsewhere,SPEC3_v1.1)"
        else:
            why = "in_snapshot_but_not_reachable_from_roots"
        extra.append({"id": i, "paths": ctx.ids[i], "reason": why})
    dups = [{"id": i, "paths": sorted(ps)} for i, ps in sorted(ctx.ids.items()) if len(ps) > 1]
    kinds_expected = Counter(e.kind for e in exp.objs.values())
    kinds_present = Counter(exp.objs[i].kind for i in present & expected)
    kinds_missing = Counter(m["kind"] for m in missing)
    # completeness of the expectation itself: rows whose order source is missing, and snapshot
    # objects whose parent chain enters the exported tree but which no child block / _row_ids reach
    # a database whose API object lists NO data sources and which no snapshot row claims as parent
    # has zero expected rows: that expectation is complete (warning only). A listed data source that
    # is absent from the snapshot or lacks _row_ids leaves the row set unknown: FAIL.
    row_src = [r for r in exp.missing_data_sources if r.get("data_source") is not None or r.get("fallback_rows")]
    row_src_info = [r for r in exp.missing_data_sources if r not in row_src]
    orphans = [dict(o, present_in_vault=o["id"] in present) for o in exp.orphans]
    gen_out = ctx.generated_outside_task_dir
    unidentified = ctx.unidentified
    if ctx.dev is not None:
        dev = ctx.dev

        def m_dup(d):
            # a Sync conflict copy duplicates its original: every path but ONE must be a listed copy (of this id)
            listed = [(p, dev.by_path(p, ("sync_conflict_copy",))) for p in d["paths"]]
            listed = [(p, h) for p, h in listed if h and dev.sync_copies.get(nfc(p)) in (None, d["id"])]
            sync_keys = tuple(k for _p, h in listed for k in h[1])
            if len(listed) == len(d["paths"]) - 1:
                return "sync_conflict_copy", sync_keys
            # WB 14.3 映射异常 (one notion_id in several notes, e.g. Obsidian "Make a copy" keeps the frontmatter):
            # the scan holds the object and lists every note it found in the object's OWN hold entry. Only that
            # entry, with every remaining (non-Sync-copy) path among its validated paths, registers the duplicate:
            # a hold without paths, an ancestor's subtree entry, or any note the entry does not list (one that
            # appeared after the scan, e.g. a publisher re-ADD of old_rel) keeps the duplicate a FAIL.
            own = dev.hold.get(d["id"])
            rest = {nfc(p) for p in d["paths"]} - {nfc(p) for p, _h in listed}
            if own is None or d["id"] not in dev.held or not rest <= set(dev.hold_paths_ok.get(d["id"], ())):
                return None
            return own["cat"], ((own["cat"], d["id"]),) + sync_keys

        missing = dev.split("C02", "missing", missing, lambda it: dev.by_id(it["id"], HOLD_CATS))
        extra = dev.split("C02", "extra", extra, lambda it: dev.by_id(it["id"], HOLD_CATS))
        dups = dev.split("C02", "duplicates", dups, m_dup)
        unidentified = dev.split("C02", "md_without_notion_id", unidentified,
                                 lambda rel: dev.by_path(rel, ("local_new", "sync_conflict_copy")))
        kinds_missing = Counter(m["kind"] for m in missing)
    ok = not (missing or missing_gap or extra or dups or unidentified or row_src or orphans or gen_out)
    return _res(ok,
                f"expected={len(expected)} present={len(present & expected)} missing={len(missing)} "
                f"missing_not_in_snapshot={len(missing_gap)} extra={len(extra)} duplicated_ids={len(dups)} "
                f"md_without_notion_id={len(unidentified)} row_source_unavailable={len(row_src)} "
                f"orphans_under_in_scope_ancestor={len(orphans)} generated_outside_task_dir={len(gen_out)} "
                f"templates_expected={kinds_expected.get('template', 0)} "
                f"templates_present={kinds_present.get('template', 0)}",
                {"expected_by_kind": dict(sorted(kinds_expected.items())),
                 "present_by_kind": dict(sorted(kinds_present.items())),
                 "missing_by_kind": dict(sorted(kinds_missing.items())),
                 "missing_cause_hints(info)": dict(Counter(m.get("cause_hint") for m in missing)),
                 "missing": missing,
                 "missing_not_in_snapshot": missing_gap,
                 "gap_ids_present_in_vault": sorted(present & gap_ids),
                 "extra": extra, "duplicates": dups,
                 "md_without_notion_id": unidentified,
                 "row_source_unavailable": row_src,
                 "database_without_data_sources(warning)": row_src_info,
                 "orphans_under_in_scope_ancestor": orphans,
                 "generated_outside_task_dir": gen_out})


_BASE_STR_LIT = r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
_BASE_INFOLDER_RE = re.compile(r"file\.inFolder\(\s*" + _BASE_STR_LIT + r"\s*\)")


def _base_prop_ref(name):
    """Expression forms that read frontmatter key `name` in a Bases filter."""
    return (r"(?:note\.(?:" + re.escape(name) + r")\b|note\[\s*[\"']" + re.escape(name) + r"[\"']\s*\]|"
            r"(?<![\w.\]])" + re.escape(name) + r"\b)")


def _base_lit(tok):
    try:
        if tok.startswith('"'):
            return json.loads(tok)
        return json.loads('"' + tok[1:-1].replace("\\'", "'").replace('"', '\\"') + '"')
    except ValueError:
        return None


def _base_filter_strings(node, out, path=()):
    """Collect filter statements that are AND-ed (skip anything under `or` / `not`); returns False on a
    structure Bases rejects (object with != 1 key, key not and/or/not, value not a list)."""
    if node is None:
        return True
    if isinstance(node, str):
        if not any(k in ("or", "not") for k in path):
            out.append(node)
        return True
    if isinstance(node, dict):
        if len(node) != 1 or next(iter(node)) not in ("and", "or", "not"):
            return False
        k, val = next(iter(node.items()))
        if not isinstance(val, list):
            return False
        return all(_base_filter_strings(x, out, path + (k,)) for x in val)
    return False


def _base_prop_id(p):
    """Bases property-id normalisation (order / sort keys): bare names mean note.<name>."""
    if not isinstance(p, str):
        return None
    if p.startswith(("note.", "formula.", "file.")):
        return p
    return "file.file" if p == "file" else "note." + p


def base_normalize(data):
    """Semantic form of a parsed .base (SPEC v1.2 section 13): property ids in `order`, `sort[].property`
    (legacy key `column` accepted), `groupBy.property` and the `properties` keys are normalised with the
    Bases rule (bare name == `note.<name>`), so the canonical v1.2 layout (bare names, written back by
    Obsidian 1.15.1) and the legacy one (`note.` prefix) compare equal. Layout differences (sequence
    indentation, quoting) never reach this function because the text is parsed first."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if isinstance(data.get("properties"), dict):
        out["properties"] = {(_base_prop_id(k) if isinstance(k, str) else k): v
                             for k, v in data["properties"].items()}
    if isinstance(data.get("views"), list):
        views = []
        for vw in data["views"]:
            if isinstance(vw, dict):
                vw = dict(vw)
                if isinstance(vw.get("order"), list):
                    vw["order"] = [_base_prop_id(x) if isinstance(x, str) else x for x in vw["order"]]
                if isinstance(vw.get("sort"), list):
                    srt = []
                    for s in vw["sort"]:
                        if isinstance(s, dict):
                            s = dict(s)
                            if s.get("column") and not s.get("property"):
                                s["property"] = s.pop("column")
                            if isinstance(s.get("property"), str):
                                s["property"] = _base_prop_id(s["property"])
                        srt.append(s)
                    vw["sort"] = srt
                if isinstance(vw.get("groupBy"), dict) and isinstance(vw["groupBy"].get("property"), str):
                    vw["groupBy"] = dict(vw["groupBy"], property=_base_prop_id(vw["groupBy"]["property"]))
            views.append(vw)
        out["views"] = views
    return out


def bases_equivalent(text_a, text_b):
    """True when two .base texts parse to the same semantic form (base_normalize)."""
    a, ea = load_yaml(text_a)
    b, eb = load_yaml(text_b)
    if ea or eb:
        return False
    return base_normalize(a) == base_normalize(b)


_SEQ_UNDER_KEY_RE = re.compile(r"^( *)[^\s#-][^\n]*:[ \t]*\n\1- ", re.M)


def base_layout(btxt, data):
    """Informational: 'canonical_v1.2' (bare ids in order/sort, sequences indented under their key),
    'legacy' (note.-prefixed ids and/or unindented sequences) or 'mixed'. Never a failure by itself."""
    ids = []
    for vw in (data.get("views") or []) if isinstance(data, dict) else []:
        if isinstance(vw, dict):
            ids += [x for x in (vw.get("order") or []) if isinstance(x, str) and x != "file.name"]
            ids += [s.get("property") or s.get("column") for s in (vw.get("sort") or []) if isinstance(s, dict)]
    ids = [x for x in ids if isinstance(x, str)]
    prefixed = sum(1 for x in ids if x.startswith("note."))
    unindented = bool(_SEQ_UNDER_KEY_RE.search(btxt))
    if not prefixed and not unindented:
        return "canonical_v1.2"
    if prefixed == len(ids) and unindented:
        return "legacy"
    return "mixed"


def base_issues(btxt, folder, db_id, props):
    """SPEC 8 / v1.2 section 13 checks of a .base file -> list of issue names. The comparison is
    semantic (base_normalize): canonical v1.2 and legacy layouts are both accepted."""
    data, err = load_yaml(btxt)
    if err or not isinstance(data, dict):
        return ["base_yaml_error"]
    data = base_normalize(data)
    issues = []
    glob = []
    if "filters" not in data or not _base_filter_strings(data.get("filters"), glob):
        issues.append("base_filters_missing_or_invalid")
    views = data.get("views") if isinstance(data.get("views"), list) else []
    tables = [vw for vw in views if isinstance(vw, dict) and vw.get("type") == "table"]
    if not tables:
        return issues + ["base_no_table_view"]
    kind_rx = re.compile(_base_prop_ref("notion_kind") + r"\s*==\s*([\"'])row\1")
    par_rx = re.compile(_base_prop_ref("notion_parent_id") + r"\s*==\s*([\"'])" + re.escape(db_id) + r"\1")
    want_cols = []
    for name, typ in props.items():
        if typ == "title":
            continue
        keys = row_prop_keys(name)
        want_cols.append((name, {("note." + k).casefold() for k in keys}))
    best = None
    for vw in tables:
        vi = []
        strs = list(glob)
        if "filters" in vw and not _base_filter_strings(vw.get("filters"), strs):
            vi.append("base_view_filters_invalid")
        joined = "\n".join(strs)
        folders = [_base_lit(m.group(1)) for m in _BASE_INFOLDER_RE.finditer(joined)]
        # Bases strips '/' and NFC-normalises the argument, then prefix-matches the raw file path
        if not any(isinstance(f, str) and nfc(f.strip("/")) == folder for f in folders):
            vi.append("base_filter_missing_inFolder_db_folder")
        if not kind_rx.search(joined):
            vi.append("base_filter_missing_notion_kind_row")
        if not par_rx.search(joined):
            vi.append("base_filter_missing_notion_parent_id_db")
        sorts = []
        for s in (vw.get("sort") if isinstance(vw.get("sort"), list) else []):
            if isinstance(s, dict):
                p = s.get("property") or s.get("column")
                if isinstance(p, str) and s.get("direction") in ("ASC", "DESC"):  # others silently dropped
                    sorts.append((_base_prop_id(p).casefold(), s["direction"]))
        if not sorts or sorts[0] != ("note.notion_order", "ASC"):
            vi.append("base_view_not_sorted_by_notion_order_asc")
        order = vw.get("order")
        if not isinstance(order, list):
            vi.append("base_view_order_missing")
        else:
            cols = {(_base_prop_id(x) or "").casefold() for x in order}
            if "file.name" not in cols:
                vi.append("base_view_order_missing_file_name")
            if any(not (ks & cols) for _n, ks in want_cols):
                vi.append("base_view_order_missing_property")
        if best is None or len(vi) < len(best):
            best = vi
    return issues + best


def check_c03(ctx):
    exp, v = ctx.exp, ctx.v
    issues = []
    layouts = Counter()
    dir_set = set(v.dirs)
    tkey = ckey(TEMPLATES)
    scope = ctx.scope
    for i, e in exp.objs.items():
        if i not in ctx.P:
            continue
        if scope is not None and i not in scope.ids:
            continue
        rel = ctx.P[i]
        d, st = dname(rel), stem_of(rel)
        container = ctx.container_of(i)
        if container is None:
            issues.append({"id": i, "path": rel, "issue": "parent_note_missing", "parent": e.parent})
        if e.kind == "row" and ckey(ctx.vault_name(i)) == tkey:
            # SPEC v1.2 section 13: `_templates` is reserved in the database folder name space
            issues.append({"id": i, "path": rel, "issue": "reserved_name__templates_used_by_row",
                           "expected": f"{TEMPLATES} ({i.replace('-', '')[:8]})"})
        if container is not None:
            folder_ok = d != "" and dname(d) == container and st == bname(d)
            leaf_ok = d == container
            if e.folder_expected:
                ok = folder_ok
                want = f"{container}/<name>/<name>.md"
            elif e.structure_flexible:
                ok = folder_ok or leaf_ok
                want = f"{container}/<name>.md or {container}/<name>/<name>.md"
            else:
                ok = leaf_ok
                want = f"{container}/<name>.md"
            if not ok:
                item = {"id": i, "path": rel, "issue": "wrong_location", "expected": want, "parent": e.parent,
                        "kind": e.kind, "has_children": bool(e.children)}
                if e.folder_expected and e.kind != "database" and leaf_ok:
                    # leaf on disk although the snapshot lists children: if none of them is in the vault
                    # the export most likely predates them (classification only)
                    item["children_in_vault"] = sum(1 for c in e.children if c in ctx.P)
                    if item["children_in_vault"] == 0:
                        item["cause_hint"] = "no_expected_child_in_vault(snapshot_newer_than_export?)"
                issues.append(item)
        if e.kind == "database":
            base = posixpath.join(d, st + ".base")
            if base not in v.file_set:
                issues.append({"id": i, "path": rel, "issue": "base_file_missing", "expected": base})
            else:
                try:
                    with open(v.abspath(base), encoding="utf-8") as fh:
                        btxt = fh.read()
                    for iss in base_issues(btxt, d, i, exp.db_properties(i)):
                        issues.append({"id": i, "path": base, "issue": iss})
                    bdata, berr = load_yaml(btxt)
                    if not berr:
                        layouts[base_layout(btxt, bdata)] += 1
                except (OSError, UnicodeDecodeError) as exc:
                    issues.append({"id": i, "path": base, "issue": "base_unreadable", "detail": str(exc)[:200]})
                # SPEC 8: the database note embeds its own view
                if not any(ln.embed and ln.status in ("ok", "ok_ci") and ln.resolved == base
                           for ln in v.notes[rel].links):
                    issues.append({"id": i, "path": rel, "issue": "database_note_does_not_embed_its_base",
                                   "expected": f"![[{base}]]"})
            # SPEC v1.2 section 13: only a database WITH templates has a `_templates` folder
            tfold = posixpath.join(d, TEMPLATES)
            if not e.templates and ctx.is_folder_note(i) and tfold in dir_set:
                issues.append({"id": i, "path": tfold, "issue": "templates_folder_without_templates",
                               "entries": sorted(x for x in v.files if x.startswith(tfold + "/"))[:20]})
        fm = v.notes[rel].fm.data or {}
        fpar = fm.get("notion_parent_id")
        if (canon_id(fpar) if fpar is not None else None) != e.parent or (fpar is not None and not isinstance(fpar, str)):
            issues.append({"id": i, "path": rel, "issue": "notion_parent_id_mismatch", "found": str(fpar),
                           "expected": e.parent})
        if fm.get("notion_teamspace") != e.teamspace:
            issues.append({"id": i, "path": rel, "issue": "notion_teamspace_mismatch",
                           "found": str(fm.get("notion_teamspace")), "expected": e.teamspace})
        if fm.get("notion_kind") != e.kind:
            issues.append({"id": i, "path": rel, "issue": "notion_kind_mismatch",
                           "found": str(fm.get("notion_kind")), "expected": e.kind})
        if not name_matches(ctx.vault_name(i), e.title, i):
            issues.append({"id": i, "path": rel, "issue": "name_not_derived_from_title",
                           "found": ctx.vault_name(i), "expected": sanitize(e.title)})
    if ctx.dev is not None:
        dev = ctx.dev

        def m_c03(it):
            i = it.get("id")
            if it["issue"] == "name_not_derived_from_title":
                ov = dev.name_overrides.get(i)
                if ov is not None:
                    if nfc(ctx.P.get(i, "")) == ov["local_rel"]:
                        return "name_override", (("name_override", i),)  # 命名差异 (WRITEBACK_SPEC 7.4.4)
                    it["name_override_listed_local_rel"] = ov["local_rel"]  # listed, but not where it is
            hit = dev.by_id(i, HOLD_CATS)
            if hit is None and it["issue"] == "parent_note_missing":
                hit = dev.by_id(it.get("parent"), HOLD_CATS)
            return hit

        issues = dev.split("C03", "issues", issues, m_c03)
    in_scope = (lambda i: True) if scope is None else (lambda i: i in scope.ids)
    cnt = Counter(x["issue"] for x in issues)
    details = {"issue_counts": dict(cnt), "issues": issues,
               "base_layouts(info)": dict(sorted(layouts.items())),
               "base_rule": "semantic comparison (SPEC v1.2 section 13): canonical bare ids / indented sequences "
                            "and legacy note.-prefixed ids / unindented sequences are equivalent"}
    summary = (f"checked={sum(1 for i in exp.objs if i in ctx.P and in_scope(i))} issues={len(issues)} "
               f"{dict(cnt)} templates_checked="
               f"{sum(1 for i, e in exp.objs.items() if e.kind == 'template' and i in ctx.P and in_scope(i))}")
    unverifiable = []
    if scope is not None:
        # --ids fails closed: a listed id that is neither checked nor verified as trashed-and-absent
        unverifiable = scope.unverifiable
        details["scope_ids_unverifiable"] = unverifiable
        summary += f" scope_ids_unverifiable={len(unverifiable)}"
    return _res(not issues and not unverifiable, summary, details)


def check_c04(ctx):
    """notion_order values, and the position of each child's own link in the parent body.

    A child may also be linked from elsewhere in the parent (page mention, link_to_page, Notion URL)
    and those lines look exactly like a child_page link. The snapshot tells how many such links the
    SPEC renderer emits BEFORE the child's own block, so the child's own link is identified as that
    occurrence instead of "the first standalone link" (which misreads e.g. a mention list placed
    above the child pages)."""
    exp, v = ctx.exp, ctx.v
    scope = ctx.scope
    order_mismatch, missing_links, out_of_order, db_render, rank_short = [], [], [], [], []
    for i, e in exp.objs.items():
        if i not in ctx.P:
            continue
        if scope is not None and i not in scope.ids:
            continue
        if e.kind == "row" and exp.objs.get(e.parent) is not None and exp.objs[e.parent].order_unknown:
            continue  # row order source missing: reported by C02, the order cannot be checked
        val = (v.notes[ctx.P[i]].fm.data or {}).get("notion_order")
        if isinstance(val, bool) or not isinstance(val, int) or val != e.order:
            item = {"id": i, "path": ctx.P[i], "found": str(val), "expected": e.order, "parent": e.parent,
                    "kind": e.kind}
            if e.parent is not None:
                # templates have their own sequence (SPEC v1.2 section 13: notion_order = template number)
                sibs = exp.objs[e.parent].templates if e.kind == "template" else exp.objs[e.parent].children
                rank = 1 + sum(1 for c in sibs[: e.order - 1] if c in ctx.P)
                if val == rank and rank != e.order:
                    # equals its rank among the siblings that ARE in the vault: consistent with an export
                    # made before the missing siblings existed in the snapshot (classification only)
                    item["cause_hint"] = "matches_rank_among_siblings_present_in_vault"
            order_mismatch.append(item)
    parents_checked = 0
    for pid, e in exp.objs.items():
        if not e.children or pid not in ctx.P:
            continue
        if scope is not None and pid not in scope.ids and pid not in scope.parents:
            continue  # order is relational: a scoped object's parent body is checked as a whole
        parents_checked += 1
        note = v.notes[ctx.P[pid]]
        childset = set(e.children)
        occ = defaultdict(list)
        for k, ln in enumerate(note.links):
            cid = ctx.target_id(ln.resolved) if ln.status in ("ok", "ok_ci") else None
            if cid in childset:
                occ[cid].append(k)
        ranks = exp.child_link_ranks(pid)
        prev, prev_cid = -1, None
        for cid in e.children:
            if cid not in ctx.P:
                continue
            pos_list = occ.get(cid)
            if not pos_list:
                missing_links.append({"parent": pid, "parent_path": ctx.P[pid], "child": cid,
                                      "child_path": ctx.P[cid]})
                continue
            if exp.objs[cid].kind == "database" and e.kind != "database":
                # SPEC 5 child_database: "![[<db folder>/<name>.base]]" then "[[<db note>|<title>]]"
                base = posixpath.join(dname(ctx.P[cid]), stem_of(ctx.P[cid]) + ".base")
                has_embed = any(note.links[k].embed and note.links[k].resolved == base for k in pos_list)
                has_note = any(not note.links[k].embed and note.links[k].resolved == ctx.P[cid] for k in pos_list)
                if not (has_embed and has_note):
                    db_render.append({"parent_path": ctx.P[pid], "database": cid, "base_embed": has_embed,
                                      "note_link": has_note})
            if e.order_unknown:
                continue
            d, o = ranks.get(cid, (0, 0))
            cands = pos_list[d: d + o + 1]
            if not cands:
                rank_short.append({"parent_path": ctx.P[pid], "child": cid, "links_found": len(pos_list),
                                   "links_expected_before_own": d})
                cands = pos_list
            pick = next((p for p in cands if p > prev), None)
            if pick is None:
                pick = cands[0]
                out_of_order.append({"parent": pid, "parent_path": ctx.P[pid],
                                     "expected_before": ctx.P[prev_cid], "expected_after": ctx.P[cid],
                                     "line_before": note.links[prev].line, "line_after": note.links[pick].line})
            prev, prev_cid = pick, cid
    # SPEC v1.2 section 13: the database note lists its templates in a "模板" section, in template order
    tmpl_issues, tmpl_dbs = [], 0
    for pid, e in exp.objs.items():
        if e.kind != "database" or not e.templates or pid not in ctx.P:
            continue
        if scope is not None and pid not in scope.ids and not any(t in scope.ids for t in e.templates):
            continue
        present = [t for t in e.templates if t in ctx.P]
        if not present:
            continue  # all template notes missing: C02
        tmpl_dbs += 1
        note = v.notes[ctx.P[pid]]
        sec = template_section(note)
        if sec is None:
            tmpl_issues.append({"database": pid, "path": ctx.P[pid], "issue": "template_section_missing",
                                "expected": "a heading '模板' followed by links to the templates in order"})
            continue
        lo, hi, head = sec
        tset = set(present)
        first = {}
        for k, ln in enumerate(note.links):
            if not (lo < ln.line < hi) or ln.status not in ("ok", "ok_ci"):
                continue
            t = ctx.target_id(ln.resolved)
            if t in tset:
                first.setdefault(t, k)
        for t in present:
            if t not in first:
                tmpl_issues.append({"database": pid, "path": ctx.P[pid], "issue": "template_link_missing_in_section",
                                    "template": t, "template_path": ctx.P[t], "section": head})
        linked = [t for t in present if t in first]
        for a, b in zip(linked, linked[1:]):
            if first[b] <= first[a]:
                tmpl_issues.append({"database": pid, "path": ctx.P[pid], "issue": "template_links_out_of_order",
                                    "expected_before": ctx.P[a], "expected_after": ctx.P[b],
                                    "line_before": note.links[first[a]].line, "line_after": note.links[first[b]].line})
    if ctx.dev is not None:
        dev = ctx.dev
        body_cats = HOLD_CATS + ("local_modified",)  # a held (stale) or locally edited body / frontmatter

        def first_hit(*hits):
            return next((h for h in hits if h), None)

        order_mismatch = dev.split("C04", "notion_order_mismatch", order_mismatch,
                                   lambda it: dev.by_id(it["id"], body_cats))
        missing_links = dev.split("C04", "child_link_missing", missing_links, lambda it: first_hit(
            dev.by_id(it["parent"], body_cats), dev.by_id(it["child"], HOLD_CATS)))
        out_of_order = dev.split("C04", "child_links_out_of_order", out_of_order, lambda it: first_hit(
            dev.by_id(it["parent"], body_cats), dev.by_path(it["expected_before"], HOLD_CATS),
            dev.by_path(it["expected_after"], HOLD_CATS)))
        db_render = dev.split("C04", "child_database_render_incomplete", db_render, lambda it: first_hit(
            dev.by_path(it["parent_path"], body_cats), dev.by_id(it["database"], HOLD_CATS)))
        tmpl_issues = dev.split("C04", "template_section_issues", tmpl_issues, lambda it: first_hit(
            dev.by_id(it["database"], body_cats), dev.by_id(it.get("template"), HOLD_CATS)))
    ok = not (order_mismatch or missing_links or out_of_order or db_render or tmpl_issues)
    return _res(ok, f"notion_order_mismatch={len(order_mismatch)} parents_checked={parents_checked} "
                    f"child_link_missing={len(missing_links)} child_links_out_of_order={len(out_of_order)} "
                    f"child_database_render_incomplete={len(db_render)} "
                    f"template_sections_checked={tmpl_dbs} template_section_issues={len(tmpl_issues)} "
                    f"fewer_links_than_snapshot_predicts(warning)={len(rank_short)}",
                {"notion_order_mismatch": order_mismatch, "child_link_missing": missing_links,
                 "child_links_out_of_order": out_of_order, "child_database_render_incomplete": db_render,
                 "template_section_issues": tmpl_issues,
                 "fewer_links_than_snapshot_predicts(warning)": rank_short})


def template_section(note):
    """-> (first_line, end_line, heading) of the database note's "模板" section in absolute file line
    numbers (links strictly between them belong to it), or None. Preference: a heading that is exactly
    "模板"; else one containing "模板" or starting with "template". The section runs to the next heading
    of the same or a higher level."""
    lines = mask_code_lines(note.fm.body)
    heads = []
    for li, line in enumerate(lines):
        m = _HEADING_LINE_RE.match(line)
        if m:
            heads.append((li, len(m.group(1)), m.group(2).strip()))
    pick = next((h for h in heads if nfc(h[2]) == "模板"), None)
    if pick is None:
        pick = next((h for h in heads if "模板" in nfc(h[2]) or h[2].casefold().startswith("template")), None)
    if pick is None:
        return None
    li, lvl, text = pick
    end = next((h[0] for h in heads if h[0] > li and h[1] <= lvl), len(lines))
    return note.fm.body_line0 + li, note.fm.body_line0 + end, text


# ---- custom-sort 3.2.0 model (docs/custom-sort.md sections 3-5; plugin source rules quoted there)
CS_ATTR_WORDS = ("target-folder:", "::::", "<", ">", "\\<", "\\>", "order-asc:", "order-desc:", "sorting:",
                 "order-asc", "order-desc", "asc", "desc")
CS_PREFIX_TOKENS = sorted([
    ("/folders:files.", "type", "both_ext"), ("/folders:files", "type", "both"), ("/--hide:", "flag", "hide"),
    ("/:files.", "type", "files_ext"), ("/folders", "type", "folders"), ("/:files", "type", "files"),
    ("/!!!", "flag", 3), ("/!!", "flag", 2), ("/!", "flag", 1), ("/%.", "type", "both_ext"),
    ("/:.", "type", "files_ext"), ("--%", "type", "both"), ("/%", "type", "both"), ("/+", "flag", "combine"),
    ("/:", "type", "files"), ("/", "type", "folders"), ("%", "type", "both"),
], key=lambda t: -len(t[0]))
CS_SPECIAL = ("with-metadata:", "bookmarked:", "with-icon:")
_CS_TRAILING_ORDER_RE = re.compile(r"\s+[<>]\s+\S.*$")


def cs_unlistable(name):
    """Names custom-sort cannot list exactly (wildcard '...', or '%' / '% ' / '--% ' starts)."""
    return "..." in name or name == "%" or name.startswith(("% ", "--% "))


def cs_parse_item(s):
    """One trimmed group line -> {kind: exact|wildcard|outsiders|catchall|special|error, mtype, name, ...}."""
    if s == "%":
        return {"kind": "outsiders", "mtype": "both", "prio": 0, "hide": False}
    rest, mtype, prio, hide = s, None, 0, False
    while True:
        for tok, cat, val in CS_PREFIX_TOKENS:
            if rest == tok or rest.startswith(tok + " "):
                if cat == "type":
                    if mtype is not None:
                        return {"kind": "error", "error": "TooManyGroupTypePrefixes", "line": s}
                    mtype = val
                    hide = hide or tok == "--%"
                elif val == "hide":
                    hide = True
                elif isinstance(val, int):
                    prio = max(prio, val)
                rest = rest[len(tok):].strip()
                break
        else:
            break
    prefixed = mtype is not None
    mtype = mtype or "both"
    rest = _CS_TRAILING_ORDER_RE.sub("", rest).strip()
    g = {"mtype": mtype, "prio": prio, "hide": hide, "prefixed": prefixed}
    if rest == "":
        return dict(g, kind="catchall")
    if rest.count("...") > 1:
        return {"kind": "error", "error": "SyntaxErrorInGroupSpec (two '...')", "line": s}
    if "..." in rest:
        pre, suf = rest.split("...", 1)
        return dict(g, kind="wildcard", pre=pre, suf=suf, name=rest)
    if rest.startswith(CS_SPECIAL):
        return dict(g, kind="special", name=rest)
    return dict(g, kind="exact", name=rest)


def _cs_target(raw, spec_folder):
    """target-folder value -> (path, wildcard) ; wildcard in (None, '...', '*')."""
    t = raw.strip()
    if t == "/":
        return "", None
    wild = None
    for suf, w in (("/...", "..."), ("/*", "*")):
        if t.endswith(suf):
            t, wild = t[: -len(suf)], w
            break
    if t == "." or t == "":
        t = spec_folder if t == "." else ""
    elif t.startswith("./"):
        t = posixpath.join(spec_folder, t[2:])
    return t, wild


CS_TARGET_WORDS = ("target-folder:", "::::")


def _cs_new_section(sections, targets=(), raw_targets=(), spec_folder=TASK_DIR):
    sec = {"targets": [], "wild": {}, "items": [], "groups": [], "errors": [], "raw_targets": [],
           "attributes": [], "notes": []}
    for t in targets:
        sec["targets"].append(t)
        sec["wild"][t] = None
    sec["raw_targets"].extend(raw_targets)
    sections.append(sec)
    return sec


def parse_sorting_spec(spec, spec_folder=TASK_DIR):
    """custom-sort 3.2.0 sorting-spec text -> list of sections
    {targets: [path], wild: {path: '...'|'*'|None}, items: [(name, line)], groups: [group], errors: [...]}.

    Line handling follows the plugin's parser loop (docs/custom-sort.md section 5):
      1. blank lines and lines starting with '//' are skipped;
      2. attribute = first token (up to the first space, case-insensitive) is one of
         target-folder: :::: < \\< > \\> order-asc: order-desc: sorting: order-asc order-desc asc desc,
         followed by a non-empty value. target-folder lines must not be indented
         (TargetFolderNestedSpec). Consecutive target-folder lines share one section; a
         target-folder line after any other line starts a new section;
      3. otherwise, a line whose lower-cased text STARTS WITH any attribute word ("Description x",
         "ascii", "target-folder:X", "<x") is NoSpaceBetweenAttributeAndValue /
         MissingAttributeValue;
      4. otherwise it is a sorting group (type prefixes /folders, /:files., %, ... see cs_parse_item).
    Every error in steps 2-4 makes the plugin reject the WHOLE configuration and suspend itself.
    Group lines before the first target-folder belong to the spec's own folder."""
    sections, cur, prev_target = [], None, False
    for idx, raw in enumerate(spec.split("\n")):
        s = raw.strip()
        if not s or s.startswith("//"):
            continue
        r = raw.lstrip()
        nest = len(raw) - len(r)
        sp = r.find(" ")
        tok = r[:sp].lower() if sp >= 0 else None
        if tok in CS_ATTR_WORDS:
            val = r[sp:].strip()
            if cur is None and tok not in CS_TARGET_WORDS:
                cur = _cs_new_section(sections, [spec_folder], ["(implicit)"])
            if not val:
                (cur or _cs_new_section(sections, [], ["(none)"]))["errors"].append(
                    {"line": idx + 1, "error": "MissingAttributeValue", "text": s[:80]})
                prev_target = False
                continue
            if tok in CS_TARGET_WORDS:
                if nest > 0:
                    (cur or _cs_new_section(sections, [], ["(none)"]))["errors"].append(
                        {"line": idx + 1, "error": "TargetFolderNestedSpec (indented target-folder)", "text": s[:80]})
                    prev_target = False
                    continue
                if cur is None or not prev_target:
                    cur = _cs_new_section(sections)
                path, wild = _cs_target(val, spec_folder)
                cur["targets"].append(path)
                cur["wild"][path] = wild
                cur["raw_targets"].append(val)
                prev_target = True
                continue
            # order attribute (folder-level, or group-level when indented): valid syntax, but its value
            # is not validated here; the SPEC generator never writes one
            cur["attributes"].append({"line": idx + 1, "text": s[:80]})
            prev_target = False
            continue
        prev_target = False
        if cur is None:
            cur = _cs_new_section(sections, [spec_folder], ["(implicit)"])
        low = r.lower()
        if low.startswith(CS_ATTR_WORDS):
            # bare "Description ..." / "ASCII ..." / "asc" / "target-folder:X": the plugin's
            # checkForRiskyAttrSyntaxError rejects the line and with it the whole spec
            cur["errors"].append({"line": idx + 1, "text": s[:80],
                                  "error": "line starts with an attribute word (asc/desc/order-*/</>/target-folder:)"
                                           " without being a valid attribute"})
            continue
        g = cs_parse_item(s)
        if g["kind"] == "error":
            cur["errors"].append({"line": idx + 1, "error": g["error"], "text": s[:80]})
            continue
        g["line"] = idx + 1
        name = g.get("name")
        if name is not None:
            if name.startswith("%"):
                # SPEC v1.1 maps '%' to '％'; '%' / '% x' / '--% x' are also plugin errors
                cur["errors"].append({"line": idx + 1, "text": s[:80], "error": "name starts with '%'",
                                      "issue": "name_starts_with_percent"})
            if "\\" in name:
                cur["errors"].append({"line": idx + 1, "text": s[:80], "issue": "backslash_in_item",
                                      "error": "'\\' starts custom-sort regex/sorting symbols (SPEC maps it to '＼')"})
            if name.lower().startswith(("asc", "desc", "order-")) and g.get("prefixed"):
                cur["notes"].append({"line": idx + 1, "note": "name starts with asc/desc but has a type prefix "
                                                              "(accepted by the plugin)"})
        cur["groups"].append(g)
        if g["kind"] in ("exact", "wildcard", "special"):
            cur["items"].append((g["name"], idx))
    return sections


def _cs_match(g, name, is_dir):
    mt = g["mtype"]
    if is_dir:
        if mt not in ("folders", "both", "both_ext"):
            return False
        key = name
    else:
        if mt == "folders":
            return False
        key = name if mt in ("files_ext", "both_ext") else (name.rsplit(".", 1)[0] if "." in name else name)
    if g["kind"] == "catchall":
        return True
    if g["kind"] == "exact":
        return key == g["name"]  # plugin: strict ===, no case folding, no Unicode normalisation
    if g["kind"] == "wildcard":
        return len(key) >= len(g["pre"]) + len(g["suf"]) and key.startswith(g["pre"]) and key.endswith(g["suf"])
    return False


def cs_assign(section, entries):
    """entries [(name, is_dir)] -> {(name, is_dir): group index} (len(groups) = implicit outsiders)."""
    groups = section["groups"]
    outs = next((k for k, g in enumerate(groups) if g["kind"] == "outsiders"), len(groups))
    named = sorted((k for k, g in enumerate(groups) if g["kind"] in ("exact", "wildcard")),
                   key=lambda k: -groups[k]["prio"])
    catch = [k for k, g in enumerate(groups) if g["kind"] == "catchall"]
    res = {}
    for ent in entries:
        gi = next((k for k in named if _cs_match(groups[k], *ent)), None)
        if gi is None:
            gi = next((k for k in catch if _cs_match(groups[k], *ent)), outs)
        res[ent] = gi
    return res


def _norm_target(t):
    """Normalised target-folder path (kept for the negative-test mutator)."""
    return _cs_target(t.strip().strip("\"'"), TASK_DIR)[0]


def _strip_item(s):
    """Group line -> listed name ('' when the line lists no single name)."""
    g = cs_parse_item(s.strip())
    return g.get("name", "") if g["kind"] in ("exact", "wildcard", "special") else ""


def _order_compare(expected, items):
    pos = {}
    for k, (name, _l) in enumerate(items):
        pos.setdefault(name, k)
    missing = [x for x in expected if x not in pos]
    seq = [(x, pos[x]) for x in expected if x in pos]
    inv = [{"expected_before": a, "expected_after": b} for (a, pa), (b, pb) in zip(seq, seq[1:]) if pb <= pa]
    return missing, inv


INDEX_REL = TASK_DIR + "/Godcreate 目录索引.md"


def _check_index(ctx):
    """SPEC 9: Notion_Obsidian/Godcreate 目录索引.md (kind=generated) = complete tree of links in order."""
    exp, v = ctx.exp, ctx.v
    note = v.notes.get(INDEX_REL)
    if note is None:
        return [{"folder": INDEX_REL, "issue": "index_note_missing"}], {}
    probs = []
    if (note.fm.data or {}).get("notion_kind") != "generated":
        probs.append({"folder": INDEX_REL, "issue": "index_note_not_kind_generated"})
    first = {}
    for k, ln in enumerate(note.links):
        cid = ctx.target_id(ln.resolved) if ln.status in ("ok", "ok_ci") else None
        if cid in exp.objs:
            first.setdefault(cid, k)
    pre = []

    def dfs(i):
        pre.append(i)
        for c in exp.objs[i].children:
            dfs(c)

    for ts in exp.teamspaces:
        for r in exp.sidebar_roots:
            if r["teamspace"] == ts and r["id"] in exp.objs:
                dfs(r["id"])
    present = [i for i in pre if i in ctx.P]
    missing = [i for i in present if i not in first]
    linked = [i for i in present if i in first]
    inv = 0
    for a, b in zip(linked, linked[1:]):
        if first[b] <= first[a]:
            inv += 1
            if inv <= 20:
                probs.append({"folder": INDEX_REL, "issue": "index_order",
                              "expected_before": ctx.P[a], "expected_after": ctx.P[b]})
    for i in missing[:50]:
        probs.append({"folder": INDEX_REL, "issue": "index_missing_object", "id": i, "path": ctx.P[i]})
    return probs, {"objects_expected": len(present), "linked": len(present) - len(missing),
                   "missing": len(missing), "order_inversions": inv}


def check_c05(ctx):
    """SPEC 9 file-list order through custom-sort, modelled on the plugin's own matching rules:
    groups are matched by type prefix (/folders = folders only; /:files. = full file name;
    /:files, bare and % = basename), names compare with strict ===, the first matching group wins,
    unmatched items go to '%' (or an implicit trailing outsiders group), display order = group
    order. Lines the plugin rejects (bare names starting with asc/desc/</>..., two '...',
    duplicated target-folder) disable the WHOLE spec and are failures here."""
    exp, v = ctx.exp, ctx.v
    note = v.notes.get(SORTSPEC_REL)
    if note is None:
        return _res(False, f"{SORTSPEC_REL} missing", {"error": "sortspec_missing"})
    fm = note.fm.data
    if fm is None or "sorting-spec" not in fm:
        return _res(False, "sorting-spec frontmatter missing or unparsable",
                    {"error": "sorting_spec_missing", "yaml_error": note.fm.error})
    spec = fm["sorting-spec"]
    if not isinstance(spec, str):
        # the plugin only parses a string value (typeof === "string"); a list/map is silently ignored
        return _res(False, "sorting-spec is not a string (use a literal block `sorting-spec: |`)",
                    {"error": "sorting_spec_not_string", "type": type(spec).__name__})
    sections = parse_sorting_spec(spec, dname(SORTSPEC_REL))
    problems, warnings, checked = [], [], []
    for s in sections:
        for e in s["errors"]:
            problems.append(dict({"folder": ",".join(s["raw_targets"]), "issue": "spec_error_disables_plugin"}, **e))
        for a in s["attributes"]:
            warnings.append({"folder": ",".join(s["raw_targets"]), "issue": "order_attribute_not_validated", **a})
        if len(s["targets"]) > 1:
            # consecutive target-folder lines share ONE rule list (a target-folder without its own rules
            # is merged into the next one); the SPEC generator writes one section per folder
            problems.append({"folder": ",".join(s["raw_targets"]), "issue": "targets_share_one_section"})
    exact_t, wild_t = {}, []
    for s in sections:
        for t, raw in zip(s["targets"], s["raw_targets"]):
            w = s["wild"].get(t)
            key = (t, w)
            if raw.startswith("/") and raw != "/":
                problems.append({"folder": raw, "issue": "target_folder_leading_slash_never_matches"})
            if key in exact_t or (w is not None and any(k == key for k, _s in wild_t)):
                problems.append({"folder": raw, "issue": "spec_error_disables_plugin",
                                 "error": "duplicate target-folder"})
                continue
            if w is None:
                exact_t[key] = s
            else:
                wild_t.append((key, s))

    def section_for(folder):
        if (folder, None) in exact_t:
            return exact_t[(folder, None)]
        best, depth = None, -1
        for (t, w), s in wild_t:
            if folder == t or (w == "*" and folder.startswith(t + "/" if t else "")) or \
                    (w == "..." and dname(folder) == t):
                d = t.count("/") + (1 if t else 0)
                if d > depth:
                    best, depth = s, d
        return best

    entries_in = defaultdict(set)
    for d_ in v.dirs:
        entries_in[dname(d_)].add((bname(d_), True))
    for f in v.files:
        entries_in[dname(f)].add((bname(f), False))

    def compare(folder, expected, required):
        sec = section_for(folder)
        label = folder or "/"
        if sec is None:
            if required:
                problems.append({"folder": label, "issue": "section_missing"})
            return False
        ents = set(entries_in.get(folder, set())) | set(expected)
        assign = cs_assign(sec, ents)
        groups = sec["groups"]
        prev, prev_name = -1, None
        for name, is_dir in expected:
            if cs_unlistable(name):
                # SPEC v1.1 sanitize removes '...' and '%', so a conformant name is always listable
                problems.append({"folder": label, "issue": "name_not_listable_in_custom_sort", "name": name})
                continue
            gi = assign[(name, is_dir)]
            g = groups[gi] if gi < len(groups) else None
            if g is None or g["kind"] != "exact":
                problems.append({"folder": label, "issue": "not_listed", "name": name, "is_folder": is_dir,
                                 "falls_into": g["kind"] if g else "implicit_outsiders"})
                continue
            if gi <= prev:
                problems.append({"folder": label, "issue": "order", "expected_before": prev_name,
                                 "expected_after": name})
            prev, prev_name = gi, name
        if (ATT, True) in entries_in.get(folder, set()):
            ga = assign[(ATT, True)]
            after = sorted(n for (n, d), gi in assign.items() if (n, d) != (ATT, True) and gi >= ga)
            if after:
                problems.append({"folder": label, "issue": "_attachments_not_last", "displayed_with_or_after": after[:10]})
        checked.append({"folder": label, "expected": [n for n, _d in expected][:200],
                        "groups": [g.get("name", g["kind"]) for g in groups][:200]})
        return True

    layout = ctx.layout_paths
    compare("", [(t, True) for t in exp.teamspaces] + [(TASK_DIR, True)], True)
    for ts in exp.teamspaces:
        expected = []
        for r in exp.sidebar_roots:
            if r["teamspace"] != ts or r["id"] not in exp.objs:
                continue
            rid = r["id"]
            if rid in layout:
                fold = ctx.is_folder_path(layout[rid])
                expected.append((stem_of(layout[rid]) if fold else bname(layout[rid]), fold))
            else:
                fold = exp.objs[rid].folder_expected
                nm = sanitize(exp.objs[rid].title)
                expected.append((nm if fold else nm + ".md", fold))
        compare(ts, expected, True)
    # SPEC 9: one section per object folder: folder note (.md) first, a database's .base next, then
    # the children in Notion order (folders as /folders, notes as /:files. <name>.md)
    deep_checked, deep_missing = 0, 0
    for i, e in exp.objs.items():
        if i not in layout or not ctx.is_folder_path(layout[i]):
            continue
        folder = dname(layout[i])
        expected = [(bname(layout[i]), False)]
        if e.kind == "database":
            base = stem_of(layout[i]) + ".base"
            if posixpath.join(folder, base) in v.file_set:
                expected.append((base, False))
        for c in e.children:
            if c not in layout:
                continue
            if ctx.is_folder_path(layout[c]) and dname(dname(layout[c])) == folder:
                expected.append((stem_of(layout[c]), True))
            elif dname(layout[c]) == folder:
                expected.append((bname(layout[c]), False))
        if compare(folder, expected, True):
            deep_checked += 1
        else:
            deep_missing += 1
    # SPEC v1.2 section 13: a database with templates writes `/folders _templates` AFTER '%' in its own
    # section, and `<db folder>/_templates` gets its own section listing the templates in template order
    tmpl_checked = 0
    for i, e in exp.objs.items():
        if e.kind != "database" or not e.templates or i not in layout or not ctx.is_folder_path(layout[i]):
            continue
        folder = dname(layout[i])
        sec = section_for(folder)
        if sec is not None:
            ents = set(entries_in.get(folder, set())) | {(TEMPLATES, True)}
            assign = cs_assign(sec, ents)
            groups = sec["groups"]
            gi = assign[(TEMPLATES, True)]
            g = groups[gi] if gi < len(groups) else None
            outs = next((k for k, x in enumerate(groups) if x["kind"] == "outsiders"), None)
            if g is None or g["kind"] != "exact" or g["mtype"] != "folders":
                problems.append({"folder": folder, "issue": "templates_folder_not_listed",
                                 "expected": f"/folders {TEMPLATES} after %",
                                 "falls_into": (g.get("name") or g["kind"]) if g else "implicit_outsiders"})
            elif outs is None or gi < outs:
                problems.append({"folder": folder, "issue": "templates_folder_not_after_outsiders",
                                 "expected": f"%, then /folders {TEMPLATES}"})
            else:
                after = sorted(n for (n, d_), gx in assign.items() if (n, d_) != (TEMPLATES, True) and gx >= gi)
                if after:
                    problems.append({"folder": folder, "issue": "templates_folder_not_last",
                                     "displayed_with_or_after": after[:10]})
        # (a missing database-folder section is already reported by the loop above)
        tfold = posixpath.join(folder, TEMPLATES)
        expected = []
        for t in e.templates:
            if t not in layout:
                continue
            if ctx.is_folder_path(layout[t]) and dname(dname(layout[t])) == tfold:
                expected.append((stem_of(layout[t]), True))
            elif dname(layout[t]) == tfold:
                expected.append((bname(layout[t]), False))
        if compare(tfold, expected, True):
            tmpl_checked += 1
    # the plugin also parses `sorting-spec` STRING values in every sortspec.md / sortspec.md.md and in every
    # folder note (basename == parent folder name); their sections join the vault-wide spec (duplicate
    # target-folder -> whole spec rejected). Other notes are not scanned.
    for rel, n in v.notes.items():
        if rel == SORTSPEC_REL or not isinstance(n.fm.data, dict) or "sorting-spec" not in n.fm.data:
            continue
        b = bname(rel)
        scanned = b in ("sortspec.md", "sortspec.md.md") or (dname(rel) and stem_of(rel) == bname(dname(rel)))
        if scanned and isinstance(n.fm.data["sorting-spec"], str):
            problems.append({"folder": dname(rel) or "/", "issue": "foreign_sorting_spec_parsed_by_plugin",
                             "path": rel})
        else:
            warnings.append({"folder": dname(rel) or "/", "issue": "sorting_spec_key_in_note_not_scanned_by_plugin",
                             "path": rel})
    idx_probs, idx_stats = _check_index(ctx)
    problems.extend(idx_probs)
    if ctx.dev is not None:
        dev = ctx.dev

        def m_c05(it):
            iss = it.get("issue")
            if iss == "not_listed":
                return dev.name_key(it["folder"], it["name"])
            if iss == "order":
                return dev.name_key(it["folder"], it["expected_before"]) or dev.name_key(it["folder"], it["expected_after"])
            if iss == "index_missing_object":
                return dev.by_id(it.get("id"), HOLD_CATS)
            if iss == "index_order":
                return dev.by_path(it["expected_before"], HOLD_CATS) or dev.by_path(it["expected_after"], HOLD_CATS)
            return None  # syntax / section / sortspec-file problems are never registered deviations

        problems = dev.split("C05", "problems", problems, m_c05)
    return _res(not problems,
                f"sections={len(sections)} problems={len(problems)} deeper_sections_checked={deep_checked} "
                f"object_folders_without_section={deep_missing} templates_sections_checked={tmpl_checked} "
                f"index={idx_stats}",
                {"problems": problems, "warnings": warnings, "compared": checked[:20],
                 "object_folders_without_section": deep_missing, "templates_sections_checked": tmpl_checked,
                 "index": idx_stats})


_MD_INTERNAL_RE = re.compile(r"(?<![\\\]])(!?)\[(?:\\.|[^\]\\\n])*\]\((<[^>\n]*>|[^)\s]*)(?:[ \t]+\"[^\"\n]*\")?\)")


def check_c06(ctx):
    exp, v = ctx.exp, ctx.v
    total, st = 0, Counter()
    unresolved, ambiguous, ci = [], [], []
    bad_embed_sub, bad_link_sub, md_unres = [], [], []
    url_hits = []
    subpaths = 0
    scope = ctx.scope
    for rel, n in v.notes.items():
        if n.in_attachments:
            continue
        src_in = scope is None or rel in scope.paths
        for ln in list(n.links) + list(n.fm_links):
            if not src_in and not (ln.status in ("ok", "ok_ci") and ctx.target_id(ln.resolved) in scope.ids):
                continue  # --ids: links FROM scoped notes, and links resolving INTO them (subpath checks)
            total += 1
            st[ln.status] += 1
            item = {"source": rel, "line": ln.line, "target": ln.target, "embed": ln.embed}
            if ln.where != "body":
                item["where"] = ln.where
            if ln.status == "unresolved":
                unresolved.append(item)
            elif ln.status == "ambiguous":
                ambiguous.append(item)
            elif ln.status == "ok_ci":
                ci.append(dict(item, resolved=ln.resolved))
            if ln.subpath:
                subpaths += 1
                sok = ctx.subpath_ok(ln)
                if sok is False:
                    # an embed with a missing heading / ^block / base view renders an error box;
                    # a plain link still opens the note (SPEC 6 says Notion #block anchors are dropped)
                    (bad_embed_sub if ln.embed else bad_link_sub).append(dict(item, subpath=ln.subpath))
        if not src_in:
            continue
        masked = mask_code_lines(n.fm.body)
        for li, line in enumerate(masked):
            # a rewritten link may keep the original URL as its display text ("[[path|https://...]]"):
            # only URLs outside wikilinks count as "not rewritten"
            scan = line
            for _e, _inner, s0, s1 in find_wikilinks(line):
                scan = scan[:s0] + " " * (s1 - s0) + scan[s1:]
            for m in NOTION_URL_IN_TEXT_RE.finditer(scan):
                path = m.group(0).split("?", 1)[0].split("#", 1)[0]
                hexes = re.findall(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                   path.lower())
                cid = canon_id(hexes[-1]) if hexes else None
                if cid and cid in exp.objs:
                    url_hits.append({"source": rel, "line": n.fm.body_line0 + li, "id": cid})
            if "](" in line:
                for m in _MD_INTERNAL_RE.finditer(line):
                    url = m.group(2)[1:-1] if m.group(2).startswith("<") else m.group(2)
                    if not url or url.startswith("#") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
                        continue
                    p = urllib.parse.unquote(url.split("#", 1)[0])
                    if ctx.resolver.resolve(p, rel)[0] not in ("ok", "ok_ci"):
                        md_unres.append({"source": rel, "line": n.fm.body_line0 + li, "target": url[:200]})
    if ctx.dev is not None:
        dev = ctx.dev
        src_cats = ("local_new", "sync_conflict_copy") + HOLD_CATS + ("local_modified",)

        def m_unres(it):
            return dev.by_path(it["source"], src_cats) or dev.link_target(it["target"])

        def m_amb(it):
            # only links FROM a listed note: a listed Sync copy is named " (Conflicted copy …).md", so it never
            # shares a basename with the note it copies and cannot be what makes a link ambiguous
            return dev.by_path(it["source"], src_cats)

        def m_sub(it):
            hit = dev.by_path(it["source"], src_cats)
            if hit:
                return hit
            _st, res = ctx.resolver.resolve(it["target"], it["source"])
            return dev.by_id(ctx.target_id(res), HOLD_CATS + ("local_modified",))

        unresolved = dev.split("C06", "unresolved", unresolved, m_unres)
        ambiguous = dev.split("C06", "ambiguous", ambiguous, m_amb)
        bad_embed_sub = dev.split("C06", "embed_subpath_unresolved", bad_embed_sub, m_sub)
    ok = not (unresolved or ambiguous or bad_embed_sub)
    return _res(ok, f"wikilinks={total} ok={st['ok']} case_insensitive_only={st['ok_ci']} "
                    f"unresolved={len(unresolved)} ambiguous={len(ambiguous)} "
                    f"embed_subpath_unresolved={len(bad_embed_sub)} "
                    f"link_subpath_unresolved(warning)={len(bad_link_sub)} with_subpath={subpaths} "
                    f"markdown_links_without_scheme_unresolved(warning)={len(md_unres)} "
                    f"in_scope_notion_urls_not_rewritten(warning)={len(url_hits)}",
                {"status_counts": dict(st), "unresolved": unresolved, "ambiguous": ambiguous,
                 "embed_subpath_unresolved": bad_embed_sub,
                 "link_subpath_unresolved(warning)": bad_link_sub,
                 "resolved_case_insensitively(warning)": ci,
                 "markdown_links_without_scheme_unresolved(warning)": md_unres,
                 "in_scope_notion_urls_not_rewritten(warning)": url_hits,
                 "scope": "body wikilinks outside code / $$math$$ / %%comments%%, plus frontmatter values that "
                          "are a single [[link]]"})


def check_c07(ctx):
    exp, v = ctx.exp, ctx.v
    att_files = [f for f in v.files if _is_attachment_path(f)]
    att_set = set(att_files)
    misplaced, bad_name, sha_mismatch = [], [], []
    hashes = {}
    for f in att_files:
        parts = f.split("/")
        if not (len(parts) == 3 and parts[0] in SPEC_TEAMSPACES and parts[1] == ATT):
            misplaced.append(f)
        h = v.sha256(f)
        hashes[f] = h
        m = SHA8_NAME_RE.search(bname(f))
        if not m:
            bad_name.append(f)
        elif m.group(1) != h[:8]:
            sha_mismatch.append({"path": f, "name_sha8": m.group(1), "content_sha8": h[:8]})
    by_ts_hash = defaultdict(list)
    for f, h in hashes.items():
        by_ts_hash[(f.split("/")[0], h)].append(f)
    dup_content = [{"teamspace": t, "sha8": h[:8], "paths": ps} for (t, h), ps in by_ts_hash.items() if len(ps) > 1]
    referenced = defaultdict(list)  # attachment path -> [source notes]
    missing_refs, cross_ts = [], []
    for rel, n in v.notes.items():
        if n.in_attachments:
            continue
        src_ts = rel.split("/")[0] if rel.split("/")[0] in SPEC_TEAMSPACES else None
        refs = []
        for ln in list(n.links) + list(n.fm_links):  # links in code / %%comments%% are not references
            if ln.status in ("ok", "ok_ci") and ln.resolved in att_set:
                refs.append((ln.resolved, ln.line))
            elif ATT in ln.target.split("/") or ln.target.startswith(ATT):
                if ln.status not in ("ok", "ok_ci"):
                    missing_refs.append({"source": rel, "line": ln.line, "target": ln.target})
        fm = n.fm.data or {}
        for key in ("notion_icon", "notion_cover"):
            val = fm.get(key)
            if isinstance(val, str) and val.startswith("attachment:"):
                p = val[len("attachment:"):].strip().lstrip("/")
                if p in v.file_set:
                    refs.append((p, key))
                else:
                    missing_refs.append({"source": rel, "line": key, "target": p})
        for p, where in refs:
            referenced[p].append(rel)
            if src_ts and p.split("/")[0] != src_ts and not n.in_task_dir:
                cross_ts.append({"source": rel, "where": where, "attachment": p})
    ref_hashes_by_ts = defaultdict(set)
    for p in referenced:
        if p in hashes:
            ref_hashes_by_ts[p.split("/")[0]].add(hashes[p])
    all_ref_hashes = set().union(*ref_hashes_by_ts.values()) if ref_hashes_by_ts else set()
    unref_hashes = {hashes[f] for f in att_files if f not in referenced}
    not_referenced = []
    in_scope_ok = [f for f in exp.files if f["status"] == "ok" and f["in_scope"]]
    for f in in_scope_ok:
        if f["sha256"] in ref_hashes_by_ts.get(f["teamspace"], set()):
            continue
        if f["sha256"] in all_ref_hashes:
            why = "referenced_only_in_other_teamspace"
        elif f["sha256"] in unref_hashes:
            why = "file_present_but_not_referenced"
        else:
            why = "not_in_vault"
        not_referenced.append({"key": f["key"], "page": f["page"], "teamspace": f["teamspace"],
                               "name": f["name"], "sha8": f["sha256"][:8], "reason": why})
    orphans = [f for f in att_files if f not in referenced]
    if ctx.dev is not None:
        dev = ctx.dev
        src_cats = ("local_new", "sync_conflict_copy") + HOLD_CATS + ("local_modified",)

        def m_dup_content(it):
            hits = [dev.by_path(p, ("local_new",)) for p in it["paths"]]
            listed = [h for h in hits if h]
            if len(listed) != len(hits) - 1:
                return None
            return "local_new", tuple(k for h in listed for k in h[1])

        missing_refs = dev.split("C07", "missing_references", missing_refs,
                                 lambda it: dev.by_path(it["source"], src_cats))
        cross_ts = dev.split("C07", "cross_teamspace_reference", cross_ts,
                             lambda it: dev.by_path(it["source"], src_cats))
        misplaced = dev.split("C07", "misplaced", misplaced, lambda p: dev.by_path(p, ("local_new",)))
        bad_name = dev.split("C07", "bad_name", bad_name, lambda p: dev.by_path(p, ("local_new",)))
        sha_mismatch = dev.split("C07", "sha8_mismatch", sha_mismatch, lambda it: dev.by_path(it["path"], ("local_new",)))
        dup_content = dev.split("C07", "duplicate_content_in_teamspace", dup_content, m_dup_content)
        not_referenced = dev.split("C07", "snapshot_files_not_referenced", not_referenced,
                                   lambda it: dev.by_id(it["page"], HOLD_CATS + ("local_modified",)))
    ok = not (misplaced or bad_name or sha_mismatch or dup_content or missing_refs or cross_ts or not_referenced)
    not_ok_rows = [f for f in exp.files if f["status"] != "ok" and f["in_scope"]]
    return _res(ok,
                f"attachment_files={len(att_files)} referenced={len(referenced)} missing_refs={len(missing_refs)} "
                f"sha_mismatch={len(sha_mismatch)} bad_name={len(bad_name)} misplaced={len(misplaced)} "
                f"dup_content={len(dup_content)} cross_teamspace={len(cross_ts)} "
                f"snapshot_ok_files_in_scope={len(in_scope_ok)} not_referenced={len(not_referenced)} "
                f"orphans(warning)={len(orphans)}",
                {"missing_references": missing_refs, "sha8_mismatch": sha_mismatch, "bad_name": bad_name,
                 "misplaced": misplaced, "duplicate_content_in_teamspace": dup_content,
                 "cross_teamspace_reference": cross_ts, "snapshot_files_not_referenced": not_referenced,
                 "orphan_attachments(warning)": orphans,
                 "snapshot_files_not_ok_in_scope(info)": [
                     {"key": f["key"], "page": f["page"], "status": f["status"]} for f in not_ok_rows]})


def _icon_issue(fm_val, snap, downloaded=False):
    if not isinstance(snap, dict) or not snap:
        return None if fm_val is None else "present_but_snapshot_has_none"
    t = snap.get("type")
    if t == "emoji":
        return None if fm_val == snap.get("emoji") else "emoji_mismatch"
    if t == "external":
        return None if fm_val == (snap.get("external") or {}).get("url") else "external_url_mismatch"
    if t == "file":
        if isinstance(fm_val, str) and fm_val.startswith("attachment:"):
            return None
        # SPEC 7: a Notion-hosted file whose download succeeded must be a vault attachment (the
        # signed URL expires); the original URL is acceptable only when the download failed
        if downloaded:
            return "file_icon_not_attachment"
        return None if isinstance(fm_val, str) and fm_val.startswith("http") else "file_icon_not_attachment"
    return None


def check_c08(ctx):
    exp, v = ctx.exp, ctx.v
    issues, alias_warn = [], []
    checked = 0
    # page-level files (icon / cover) whose bytes were downloaded (files.status == ok)
    downloaded = {(f["owner_id"], (f["field"] or "").lower()) for f in exp.files
                  if f["owner_kind"] != "block" and f["status"] == "ok"}

    def add(rel, issue, **kw):
        issues.append(dict({"path": rel, "issue": issue}, **kw))

    scope = ctx.scope
    generated = 0
    for rel, n in v.notes.items():
        if n.in_attachments:
            continue
        if scope is not None and rel not in scope.paths:
            continue
        checked += 1
        if n.decode_error:
            add(rel, "not_utf8", detail=n.decode_error)
        if n.has_bom:
            add(rel, "utf8_bom")
        if n.has_crlf:
            add(rel, "crlf_line_endings")
        if not n.text.endswith("\n") or n.text.endswith("\n\n"):
            add(rel, "not_single_trailing_newline")
        if n.fm.error:
            add(rel, "yaml_error", detail=n.fm.error)
            continue
        fm = n.fm.data
        gk = generated_file_kind(rel) if n.in_task_dir else None
        if gk is not None:
            # WRITEBACK_SPEC 5.1a: notion_kind: generated, no notion_id, note paths as inline code (no wikilink /
            # embed, which a later local rename or delete would turn into a C06 failure)
            generated += 1
            gfm = fm if n.fm.present and isinstance(fm, dict) else {}
            if gfm.get("notion_kind") != "generated":
                add(rel, "generated_file_not_kind_generated", kind=gk, found=str(gfm.get("notion_kind")))
            if "notion_id" in gfm:
                add(rel, "generated_file_has_notion_id", kind=gk)
            wl = list(n.links) + list(n.fm_links)
            if wl:
                add(rel, "generated_file_contains_wikilink", kind=gk, count=len(wl), first_line=wl[0].line)
            if "generated_by" not in gfm:
                alias_warn.append({"path": rel, "issue": "generated_file_without_generated_by", "kind": gk})
            continue
        if not n.fm.present:
            if not n.in_task_dir:
                add(rel, "no_frontmatter")
            continue
        kind = fm.get("notion_kind")
        if kind == "generated":
            continue
        if n.in_task_dir and "notion_kind" not in fm:
            continue
        for f in REQUIRED_FIELDS:
            if f not in fm:
                add(rel, "missing_field", field=f)
        nid = fm.get("notion_id")
        if "notion_id" in fm and not (isinstance(nid, str) and UUID_RE.match(nid)):
            add(rel, "bad_notion_id", found=str(nid))
        if "notion_kind" in fm and kind not in NOTE_KINDS:
            add(rel, "bad_notion_kind", found=str(kind))
        url = fm.get("notion_url")
        if "notion_url" in fm:
            m = NOTION_URL_RE.match(url) if isinstance(url, str) else None
            if not m:
                add(rel, "bad_notion_url", found=str(url))
            elif canon_id(m.group(1)) != canon_id(nid):
                add(rel, "notion_url_id_mismatch", found=str(url))
        if "notion_title" in fm and not isinstance(fm.get("notion_title"), (str, type(None))):
            add(rel, "notion_title_not_string")
        par = fm.get("notion_parent_id")
        if par is not None and not (isinstance(par, str) and UUID_RE.match(par)):
            add(rel, "bad_notion_parent_id", found=str(par))
        if "notion_teamspace" in fm and fm.get("notion_teamspace") not in SPEC_TEAMSPACES:
            add(rel, "bad_notion_teamspace", found=str(fm.get("notion_teamspace")))
        o = fm.get("notion_order")
        if "notion_order" in fm and (isinstance(o, bool) or not isinstance(o, int) or o < 1):
            add(rel, "bad_notion_order", found=str(o))
        for f in ("notion_created_time", "notion_last_edited_time"):
            if f in fm:
                val = fm.get(f)
                if not isinstance(val, str):
                    add(rel, "time_not_quoted_string", field=f, found=str(val))
                elif not ISO_RE.match(val):
                    add(rel, "time_not_iso", field=f, found=val)
        e = exp.objs.get(canon_id(nid)) if isinstance(nid, str) else None
        if e is None:
            continue
        ftitle = fm.get("notion_title")
        if (ftitle or "") != (e.title or ""):
            add(rel, "notion_title_mismatch", found=str(ftitle), expected=e.title)
        for f, want in (("notion_created_time", e.created_time), ("notion_last_edited_time", e.last_edited_time)):
            if f in fm and isinstance(fm.get(f), str) and want is not None and fm.get(f) != want:
                add(rel, "time_mismatch", field=f, found=fm.get(f), expected=want)
        # SPEC 4 aliases: "only when the name after sanitize != original title". The name is the one
        # actually written (sanitize steps 1-4: replacement, truncation "…", dedupe " (<id8>)"),
        # so compare the on-disk name, not just the step-1/2 replacement.
        al = fm.get("aliases")
        al_list = al if isinstance(al, list) else ([al] if isinstance(al, str) else [])
        if not n.in_task_dir and e.title:
            if nfc(stem_of(rel)) != nfc(e.title):
                if e.title not in al_list:
                    add(rel, "aliases_missing_original_title", expected=e.title, name=stem_of(rel), id=e.id)
            elif al_list:
                alias_warn.append({"path": rel, "issue": "aliases_present_although_name_equals_title"})
        for f, snap in (("notion_icon", e.icon), ("notion_cover", e.cover)):
            if f == "notion_cover" and isinstance(snap, dict) and snap.get("type") == "emoji":
                continue
            why = _icon_issue(fm.get(f), snap, (e.id, f[len("notion_"):]) in downloaded)
            if why:
                add(rel, f + "_" + why, found=str(fm.get(f))[:200])
        if e.kind == "row" and e.properties:
            for pname, ptype in e.properties.items():
                if ptype == "title":
                    continue
                keys = row_prop_keys(pname)
                if not any(k in fm for k in keys):
                    add(rel, "row_property_missing", property=pname, expected_key=" or ".join(keys))
    if ctx.dev is not None:
        dev = ctx.dev
        lm_issues = ("notion_title_mismatch", "time_mismatch")

        def m_c08(it):
            iss = it["issue"]
            if iss.startswith("generated_file_"):
                return None
            hit = dev.by_path(it["path"], ("local_new", "sync_conflict_copy"))
            if hit:
                return hit
            if iss == "aliases_missing_original_title":
                # L-11 (lead decision 2026-11-18): WRITEBACK_SPEC 7.4.3 / D-22 / SPEC 4 -- aliases of an override
                # object follow the title-derived name, not the override name. A listed override at its local_rel
                # whose title-derived name equals the title therefore has no alias: 命名差异, not damage.
                i = canon_id(it.get("id"))
                ov = dev.name_overrides.get(i) if i else None
                if (ov is not None and nfc(it["path"]) == ov["local_rel"]
                        and nfc(sanitize(it.get("expected") or "")) == nfc(it.get("expected") or "")):
                    return "name_override", (("name_override", i),)
            if iss in HOLD_C08_ISSUES or iss.startswith(HOLD_C08_PREFIXES):  # a held note is stale, not damaged
                hit = dev.by_path(it["path"], HOLD_CATS)
                if hit:
                    return hit
            if iss in lm_issues or iss.startswith(("notion_icon_", "notion_cover_")):
                return dev.by_path(it["path"], ("local_modified",))
            return None

        issues = dev.split("C08", "issues", issues, m_c08)
    cnt = Counter(x["issue"] for x in issues)
    details = {"issue_counts": dict(cnt), "issues": issues, "warnings": alias_warn,
               "yaml_backend": "PyYAML" if _yaml else "mini"}
    if generated:
        details["generated_files_checked(WRITEBACK_SPEC_5.1a)"] = generated
    return _res(not issues, f"md_checked={checked} issues={len(issues)} {dict(cnt)} "
                            f"warnings={len(alias_warn)}" + (f" generated_files={generated}" if generated else ""),
                details)


def _component_issues(name, is_dir):
    try:
        b = name.encode("utf-8")
    except UnicodeEncodeError:
        return ["invalid_utf8_name"]
    out = []
    if any(ch in FORBIDDEN_NAME_CHARS for ch in name):
        out.append("forbidden_char")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        out.append("control_char")
    # SPEC v1.1 sanitize step 1: names are NFC (custom-sort and Bases inFolder compare code points)
    # and contain neither '...' (custom-sort wildcard) nor '%' (custom-sort group prefix)
    if name != nfc(name):
        out.append("not_nfc")
    if "%" in name:
        out.append("percent_char")
    if "..." in name:
        out.append("three_dots")
    stem = name
    if not is_dir:
        if name.endswith(".md"):
            stem = name[:-3]
        elif "." in name:
            stem = name.rsplit(".", 1)[0]
    # ASCII whitespace only: the SPEC does not say whether U+3000 etc. count as
    # "空白", and C03's name check accepts both readings, so C09 must not be stricter
    if name != name.strip(" \t") or stem != stem.strip(" \t"):
        out.append("leading_or_trailing_space")
    if stem.endswith("."):
        out.append("trailing_dot")
    if re.search(r"[ \t]{2,}", name):
        out.append("consecutive_whitespace")
    if len(b) > 255:
        out.append("component_over_255_bytes")
    return out


def check_c09(ctx):
    exp, v = ctx.exp, ctx.v
    bad = []
    entries = [(d, True) for d in v.dirs] + [(f, False) for f in v.files]
    per_dir = defaultdict(lambda: defaultdict(list))
    for rel, is_dir in entries:
        name = bname(rel)
        for iss in _component_issues(name, is_dir):
            bad.append({"path": rel, "issue": iss})
        try:
            per_dir[dname(rel)][ckey(name)].append(rel + ("/" if is_dir else ""))
        except UnicodeEncodeError:
            pass
    collisions = [{"dir": d or "/", "entries": sorted(ps)} for d, m in per_dir.items() for ps in m.values()
                  if len(ps) > 1]
    obj_keys = defaultdict(lambda: defaultdict(set))
    for i in ctx.P:
        rel = ctx.P[i]
        owner = dname(dname(rel)) if ctx.is_folder_note(i) else dname(rel)
        obj_keys[owner][ckey(ctx.vault_name(i))].add(i)
    obj_collisions = [{"dir": d or "/", "key": k, "ids": sorted(ids)} for d, m in obj_keys.items()
                      for k, ids in m.items() if len(ids) > 1]
    lens = sorted(((len(rel), rel) for rel, _ in entries), reverse=True)
    over200 = [r for n, r in lens if n > 200]
    over260 = [r for n, r in lens if n > 260]
    if ctx.dev is not None:
        dev = ctx.dev
        user_cats = ("local_new", "sync_conflict_copy")
        dir_set = set(v.dirs)

        def m_entry(rel):
            # a listed user file, or a folder that holds only listed user files and no identified note
            hit = dev.by_path(rel, user_cats)
            if hit or rel not in dir_set:
                return hit
            files = [f for f in v.files if f.startswith(rel + "/")]
            hits = [dev.by_path(f, user_cats) for f in files]
            if not files or not all(hits) or any(f in ctx.path_id for f in files):
                return None
            return hits[0][0], tuple(k for h in hits for k in h[1])

        def m_collision(it):
            hits = [m_entry(e.rstrip("/")) for e in it["entries"]]
            listed = [h for h in hits if h]
            if not listed or len(hits) - len(listed) > 1:  # at most one unlisted entry (the exported one)
                return None
            return listed[0][0], tuple(k for h in listed for k in h[1])

        bad = dev.split("C09", "name_issues", bad, lambda it: m_entry(it["path"]))
        collisions = dev.split("C09", "nfc_casefold_collisions", collisions, m_collision)
    ok = not (bad or collisions or obj_collisions)
    return _res(ok, f"entries={len(entries)} name_issues={len(bad)} case_collisions={len(collisions)} "
                    f"object_name_collisions={len(obj_collisions)} paths>200={len(over200)} "
                    f"paths>260={len(over260)} max_rel_path_len={lens[0][0] if lens else 0}",
                {"name_issues": bad, "nfc_casefold_collisions": collisions,
                 "object_name_collisions": obj_collisions,
                 "path_len_over_200(report)": over200, "path_len_over_260(report)": over260,
                 "longest_paths": [{"len": n, "path": r} for n, r in lens[:10]]})


def c10_missing(items, conv, raw):
    """Text blocks of one page that the note body does not contain -> [(item, reason)].

    item = (block id, type, 40-char key, display snippet, alt keys). A block is found when its key
    (or an alternative mention-display key) occurs in the visible text (`conv`) or in the raw body
    (`raw`). Blocks sharing the same key need as many non-overlapping occurrences as there are such
    blocks, so deleting one of two identical paragraphs is detected too (greedy left-to-right counting
    finds the maximum number of disjoint occurrences, so this never under-counts real copies)."""
    groups = OrderedDict()
    for it in items:
        groups.setdefault(it[2], []).append(it)
    miss = []
    for key, grp in groups.items():
        occ = max(conv.count(key), raw.count(key))
        if occ >= len(grp):
            continue
        avail = occ
        alts = []
        for it in grp:
            for a in (it[4] if len(it) > 4 else ()):
                if a and a not in alts:
                    alts.append(a)
        for a in alts:
            ca = max(conv.count(a), raw.count(a))
            # overlapping keys (one contains the other): one rendering may be counted by both
            avail = max(avail, ca) if (a in key or key in a) else avail + ca
        short = len(grp) - avail
        if short <= 0:
            continue
        reason = "not_found" if avail == 0 else f"fewer_occurrences_than_blocks({avail}<{len(grp)})"
        miss += [(it, reason) for it in grp[len(grp) - short:]]
    return miss


def check_c10(ctx):
    """SPEC v1.2 section 13: every text block's 40-char key must be found; ONE miss fails the check
    (the 99% page threshold of v1.1 is gone). Coverage is still reported. A page whose note is missing
    counts all its text blocks as missing (its text is not in the vault)."""
    exp, v = ctx.exp, ctx.v
    texts = exp.page_texts()
    total = matched = 0
    bad_pages, per_page, missing_notes = [], [], []
    scope = ctx.scope
    for pid, items in texts.items():
        if scope is not None and pid not in scope.ids:
            continue
        n = len(items)
        total += n
        if pid not in ctx.P:
            missing_notes.append({"id": pid, "blocks": n, "kind": exp.objs[pid].kind if pid in exp.objs else None})
            continue
        body = remove_comments(v.notes[ctx.P[pid]].fm.body)  # text inside %%comments%% is not visible
        conv = norm_text(visible_text(body))
        raw = norm_text(body)
        miss = c10_missing(items, conv, raw)
        m = n - len(miss)
        matched += m
        cov = m / n if n else 1.0
        per_page.append({"id": pid, "path": ctx.P[pid], "blocks": n, "covered": m, "coverage": round(cov, 4)})
        if miss:
            bad_pages.append({"id": pid, "path": ctx.P[pid], "blocks": n, "covered": m, "coverage": round(cov, 4),
                              "missing_blocks": [{"block": it[0], "type": it[1], "prefix": it[3], "reason": why}
                                                 for it, why in miss[:20]],
                              "missing_count": len(miss)})
    overall = matched / total if total else 1.0
    per_page.sort(key=lambda x: (x["coverage"], x["path"]))
    missing_total = total - matched
    pages_with_text = len(per_page) + len(missing_notes)
    if ctx.dev is not None:
        dev = ctx.dev
        missing_notes = dev.split("C10", "pages_note_missing", missing_notes, lambda it: dev.by_id(it["id"], HOLD_CATS))
        bad_pages = dev.split("C10", "pages_with_missing_blocks", bad_pages,
                              lambda it: dev.by_id(it["id"], HOLD_CATS + ("local_modified",)))
    return _res(not bad_pages and not missing_notes,
                f"pages_with_text={pages_with_text} text_blocks={total} covered={matched} "
                f"missing_blocks={missing_total} overall_coverage={overall:.4%} "
                f"pages_with_missing_blocks={len(bad_pages)} pages_note_missing={len(missing_notes)} "
                f"(rule: any missing block = FAIL)",
                {"overall_coverage": round(overall, 6), "text_blocks": total, "covered": matched,
                 "missing_blocks": missing_total,
                 "pages_with_missing_blocks": bad_pages,
                 "pages_note_missing": missing_notes, "per_page": per_page,
                 "rule": "SPEC v1.2 section 13: any text block not found -> FAIL (no 99% threshold)",
                 "method": "first 40 chars of block plain text (whitespace, backslashes and markdown marker "
                           "characters removed) searched in the note body, counted per identical key; "
                           "heuristic, not a proof of equality"})


def check_c11(ctx):
    v = ctx.v
    hits, skipped = [], []
    paths = list(v.files)
    obs = os.path.join(v.root, ".obsidian")
    if os.path.isdir(obs):
        for dp, _dns, fns in os.walk(obs):
            for f in fns:
                paths.append(os.path.relpath(os.path.join(dp, f), v.root).replace(os.sep, "/"))
    for rel in paths:
        full = v.abspath(rel)
        try:
            if os.path.getsize(full) > 256 * 1024 * 1024:
                skipped.append(rel)
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            skipped.append(f"{rel} ({exc.__class__.__name__})")
            continue
        for kind, rx in SENSITIVE_PATTERNS:
            for m in rx.finditer(data):
                hits.append({"path": rel, "line": data.count(b"\n", 0, m.start()) + 1, "kind": kind,
                             "length": m.end() - m.start()})
    # SPEC v1.1 section 12: credential-like strings in the user's own pages are migrated as-is; C11 only
    # reports where they are (path / line / kind, never the value) and is a WARNING, not a blocker
    r = _res(True, f"files_scanned={len(paths) - len(skipped)} hits={len(hits)} skipped={len(skipped)}"
                   + (" (WARN: user decides whether to rotate / edit the source pages)" if hits else ""),
             {"hits(values_never_printed)": hits, "skipped": skipped,
              "policy": "SPEC 12 v1.1: report only, not blocking; values are never written to the report"})
    if hits or skipped:
        r["status"] = "WARN"
    return r


CHECKS = OrderedDict([
    ("C01", check_c01), ("C02", check_c02), ("C03", check_c03), ("C04", check_c04), ("C05", check_c05),
    ("C06", check_c06), ("C07", check_c07), ("C08", check_c08), ("C09", check_c09), ("C10", check_c10),
    ("C11", check_c11),
])


def run_checks(exp, vault_root, dev=None, scope=None):
    vault = Vault(vault_root)
    ctx = Ctx(exp, vault, dev=dev, scope=scope)
    results = OrderedDict()
    for cid, fn in CHECKS.items():
        try:
            r = fn(ctx)
        except Exception as exc:  # a crashing check is a FAIL, never a silent PASS
            r = _res(False, f"check crashed: {exc.__class__.__name__}: {str(exc)[:300]}", {"crash": True})
        r["title"] = CHECK_TITLES[cid]
        if dev is not None and cid in DEV_CHECKS:
            info = dev.info.get(cid, {})
            n = sum(len(x) for x in info.values())
            r["registered_deviations"] = n
            r["details"]["registered_deviations(info)"] = info
            r["summary"] += f" registered_deviations(info)={n}"
        if scope is not None and cid in SCOPED_CHECKS:
            r["scope"] = f"--ids ({len(scope.ids)} ids)"
            r["summary"] += f" scope=--ids({len(scope.ids)})"
        results[cid] = r
    return ctx, results


# ---------------------------------------------------------------- reports
def _cap(obj, cap):
    if isinstance(obj, list):
        if len(obj) > cap:
            return [_cap(x, cap) for x in obj[:cap]] + [f"... {len(obj) - cap} more"]
        return [_cap(x, cap) for x in obj]
    if isinstance(obj, dict):
        return {k: _cap(x, cap) for k, x in obj.items()}
    return obj


def overall_status(results):
    return "PASS" if all(r["status"] in ("PASS", "WARN") for r in results.values()) else "FAIL"


def build_report(exp, vault_root, results, extra=None):
    rep = OrderedDict()
    rep["tool"] = {"name": "verify_vault.py", "version": VERSION, "generated_at": now_iso(),
                   "yaml_backend": "PyYAML" if _yaml else "mini"}
    rep["inputs"] = {"snapshot": exp.snapshot_path, "sidebar": exp.sidebar_path,
                     "sidebar_sha256": exp.sidebar_sha256, "roots_kinds": exp.roots_kinds_path,
                     "vault": os.path.abspath(vault_root), "snapshot_meta": exp.meta,
                     "snapshot_open_mode": exp.open_mode, "as_of": exp.as_of}
    # WARN (only C11 can be WARN) never fails the run; any FAIL does
    rep["overall"] = overall_status(results)
    rep["status_by_check"] = {k: r["status"] for k, r in results.items()}
    rep["checks_warn"] = [k for k, r in results.items() if r["status"] == "WARN"]
    rep["expectation"] = exp.summary()
    rep["warnings"] = exp.warnings
    rep["expectation_details"] = {
        "snapshot_gaps": exp.gaps, "trashed_excluded": exp.trashed, "multi_referenced": exp.multi_ref,
        "references_not_children": exp.references,
        "missing_data_sources": exp.missing_data_sources,
        "snapshot_objects_not_reachable": exp.unreachable,
    }
    rep["checks"] = results
    if extra:
        rep.update(extra)
    return _cap(rep, LIST_CAP_JSON)


def _md_list(lines, items, cap=LIST_CAP_MD):
    for x in items[:cap]:
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        lines.append(f"  - {s}")
    if len(items) > cap:
        lines.append(f"  - ... {len(items) - cap} more (see verify.json)")


def render_md(rep):
    L = ["# Vault verification report", ""]
    t, i = rep["tool"], rep["inputs"]
    L += [f"- Tool: `{t['name']}` v{t['version']} (YAML backend: {t['yaml_backend']})",
          f"- Generated: {t['generated_at']}",
          f"- Vault: `{i['vault']}`",
          f"- Snapshot: `{i['snapshot']}`",
          f"- Sidebar: `{i['sidebar']}` (sha256 `{i['sidebar_sha256'][:16]}…`)",
          f"- Roots kinds: `{i['roots_kinds']}`",
          f"- Snapshot opened with: `{i.get('snapshot_open_mode')}`",
          f"- **Overall: {rep['overall']}**"
          + (f" (WARN, not blocking: {', '.join(rep['checks_warn'])})" if rep.get("checks_warn") else ""), ""]
    L += ["## Results", "", "| Check | Status | Summary |", "|---|---|---|"]
    for cid, r in rep["checks"].items():
        summ = r["summary"].replace("|", "\\|")
        L.append(f"| {cid} {r['title']} | **{r['status']}** | {summ} |")
    if "registered_deviations" in rep:
        d = rep["registered_deviations"]
        L += ["", "## 已登记的本地差异 (INFO, --expected-deviations)", "",
              f"- File: `{d['file']}` (sha256 `{d['sha256'][:16]}…`)",
              f"- Registered deviations (INFO, not counted in check status): **{d['total']}**",
              f"- By check: `{json.dumps(d['by_check'], ensure_ascii=False)}`",
              f"- By category: `{json.dumps(d['by_category'], ensure_ascii=False)}`",
              f"- Entries in file: `{json.dumps(d['entries'], ensure_ascii=False)}`"
              + (f"; ignored keys: `{', '.join(d['ignored_keys'])}`" if d.get("ignored_keys") else "")]
        L.append(f"- 命名差异 (name overrides in effect): {len(d['name_overrides'])}")
        _md_list(L, d["name_overrides"], 15)
        L.append(f"- Entries that matched no issue (info): {len(d['unmatched_entries(info)'])}")
        _md_list(L, d["unmatched_entries(info)"], 15)
        L.append(f"- Rejected entries (not used: the issues they name keep failing): {len(d['rejected_entries'])}")
        _md_list(L, d["rejected_entries"], 15)
    if "scope" in rep:
        s = rep["scope"]
        L += ["", "## Scope (--ids)", "",
              f"- File: `{s['file']}`; ids: {s['ids']}; notes in scope: {s['notes_in_scope']}",
              f"- Scoped checks: {', '.join(s['checks_scoped'])}; whole vault: {', '.join(s['checks_full_vault'])}",
              f"- ids checked: {s['counts']['checked']}; trashed in the snapshot and absent (info): "
              f"{s['counts']['trashed_absent']}; **unverifiable (C03 FAIL): {s['counts']['unverifiable']}**"]
        _md_list(L, s["ids_unverifiable"], 15)
    L += ["", "## Expectation (derived from snapshot + sidebar)", ""]
    for k, v in rep["expectation"].items():
        L.append(f"- {k}: `{json.dumps(v, ensure_ascii=False)}`")
    if rep["warnings"]:
        L += ["", "## Warnings", ""]
        _md_list(L, rep["warnings"])
    L += ["", "## Details", ""]
    for cid, r in rep["checks"].items():
        L += [f"### {cid} {r['title']} — {r['status']}", "", r["summary"], ""]
        for k, val in r["details"].items():
            if isinstance(val, list):
                if k == "per_page":
                    continue
                L.append(f"- {k}: {len(val)}")
                _md_list(L, val, 15)
            else:
                L.append(f"- {k}: `{json.dumps(val, ensure_ascii=False)[:500]}`")
        L.append("")
    if "negative_tests" in rep:
        L += ["## Negative tests", ""]
        for c in rep["negative_tests"]["cases"]:
            L.append(f"- {c['case']}: expect {c['target_check']} FAIL -> {c.get('target_status')} "
                     f"({'ok' if c['ok'] else 'NOT DETECTED'}) {c.get('skipped') or ''}")
        L.append("")
    return "\n".join(L) + "\n"


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- negative tests
class Skip(Exception):
    pass


def _leaf_candidates(ctx, non_root=True):
    out = []
    for i, e in ctx.exp.objs.items():
        if e.kind in ("page", "row") and not e.children and not e.gap_children and len(ctx.ids.get(i, [])) == 1:
            if non_root and e.is_root:
                continue
            out.append((ctx.P[i], i, e))
    return sorted(out, key=lambda t: t[0])


def _pick_leaf(ctx):
    c = _leaf_candidates(ctx, True) or _leaf_candidates(ctx, False)
    if not c:
        raise Skip("no single-copy leaf note available")
    return c[0]


def _mut_delete_note(ctx, root):
    rel, i, _e = _pick_leaf(ctx)
    os.remove(os.path.join(root, *rel.split("/")))
    return {"deleted": rel, "id": i}


def _fm_line_swap(text_a, text_b, key):
    rx = re.compile(r"^" + re.escape(key) + r":.*$", re.M)

    def fm_span(t):
        if not t.startswith("---\n"):
            return None
        end = t.find("\n---", 3)
        return (0, end) if end > 0 else None

    sa, sb = fm_span(text_a), fm_span(text_b)
    if not sa or not sb:
        raise Skip("frontmatter not found")
    ma, mb = rx.search(text_a, 0, sa[1]), rx.search(text_b, 0, sb[1])
    if not ma or not mb:
        raise Skip(f"{key} line not found")
    if ma.group(0) == mb.group(0):
        raise Skip(f"{key} lines are identical")
    return (text_a[:ma.start()] + mb.group(0) + text_a[ma.end():],
            text_b[:mb.start()] + ma.group(0) + text_b[mb.end():])


def _mut_swap_order(ctx, root):
    for pid, e in ctx.exp.objs.items():
        kids = [c for c in e.children if len(ctx.ids.get(c, [])) == 1]
        if len(kids) < 2:
            continue
        a, b = ctx.P[kids[0]], ctx.P[kids[1]]
        pa, pb = os.path.join(root, *a.split("/")), os.path.join(root, *b.split("/"))
        with open(pa, encoding="utf-8") as fh:
            ta = fh.read()
        with open(pb, encoding="utf-8") as fh:
            tb = fh.read()
        try:
            na, nb = _fm_line_swap(ta, tb, "notion_order")
        except Skip:
            continue
        with open(pa, "w", encoding="utf-8") as fh:
            fh.write(na)
        with open(pb, "w", encoding="utf-8") as fh:
            fh.write(nb)
        return {"parent": pid, "swapped": [a, b]}
    raise Skip("no parent with two single-copy children")


def _mut_move_wrong_parent(ctx, root):
    cands = _leaf_candidates(ctx, True)
    for rel, i, e in cands:
        if dname(rel) == e.teamspace:
            continue
        dest = posixpath.join(e.teamspace, bname(rel))
        if os.path.exists(os.path.join(root, *dest.split("/"))):
            continue
        os.replace(os.path.join(root, *rel.split("/")), os.path.join(root, *dest.split("/")))
        return {"moved": rel, "to": dest, "id": i}
    folders = sorted(ctx.children_folder(i) for i, e in ctx.exp.objs.items() if e.folder_expected and i in ctx.P)
    for rel, i, e in _leaf_candidates(ctx, False):
        for f in folders:
            if f == dname(rel):
                continue
            dest = posixpath.join(f, bname(rel))
            if os.path.isdir(os.path.join(root, *f.split("/"))) and not os.path.exists(
                    os.path.join(root, *dest.split("/"))):
                os.replace(os.path.join(root, *rel.split("/")), os.path.join(root, *dest.split("/")))
                return {"moved": rel, "to": dest, "id": i}
    raise Skip("no leaf note could be moved to a wrong folder")


def _mut_duplicate_note(ctx, root):
    rel, i, _e = _pick_leaf(ctx)
    dest = posixpath.join(dname(rel), stem_of(rel) + " (neg-dup).md")
    shutil.copy2(os.path.join(root, *rel.split("/")), os.path.join(root, *dest.split("/")))
    return {"copied": rel, "to": dest, "id": i}


def _mut_break_link(ctx, root):
    for rel in sorted(ctx.v.notes):
        n = ctx.v.notes[rel]
        if n.in_attachments:
            continue
        for ln in n.links:
            if ln.status != "ok" or ln.embed or ln.target == "" or (ATT in ln.target.split("/")):
                continue
            p = os.path.join(root, *rel.split("/"))
            with open(p, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            old = "[[" + ln.raw + "]]"
            k = ln.line - 1
            if k >= len(lines) or old not in lines[k]:
                continue
            lines[k] = lines[k].replace(old, "[[__neg_missing_target__/nowhere|broken]]", 1)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            return {"note": rel, "line": ln.line, "old_target": ln.target}
    raise Skip("no resolvable non-embed wikilink found")


def _mut_delete_attachment(ctx, root):
    refs = set()
    for n in ctx.v.notes.values():
        for ln in n.links:
            if ln.status == "ok" and ln.resolved and _is_attachment_path(ln.resolved):
                refs.add(ln.resolved)
    if not refs:
        raise Skip("no referenced attachment in vault")
    rel = sorted(refs)[0]
    os.remove(os.path.join(root, *rel.split("/")))
    return {"deleted": rel}


def _mut_swap_sortspec(ctx, root):
    p = os.path.join(root, *SORTSPEC_REL.split("/"))
    if not os.path.isfile(p):
        raise Skip("sortspec.md missing")
    with open(p, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    in_root, found = False, {}
    want = ctx.exp.teamspaces[:2]
    for k, raw in enumerate(lines):
        s = raw.strip()
        m = re.match(r"^target-folder\s*:\s*(.*)$", s)
        if m:
            in_root = _norm_target(m.group(1)) == ""
            continue
        if in_root and _strip_item(s) in want and _strip_item(s) not in found:
            found[_strip_item(s)] = k
    if len(found) < 2:
        raise Skip("root section with the first two teamspaces not found")
    a, b = found[want[0]], found[want[1]]
    lines[a], lines[b] = lines[b], lines[a]
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"swapped_lines": [a + 1, b + 1], "teamspaces": want}


def _mut_sortspec_fatal_line(ctx, root):
    """Insert a bare 'Description …' group line: custom-sort rejects the whole spec (asc/desc prefix)."""
    p = os.path.join(root, *SORTSPEC_REL.split("/"))
    if not os.path.isfile(p):
        raise Skip("sortspec.md missing")
    with open(p, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for k, raw in enumerate(lines):
        if raw.strip().lower().startswith("target-folder:"):
            ind = raw[: len(raw) - len(raw.lstrip())]
            lines.insert(k + 1, ind + "Description neg-test")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            return {"inserted_after_line": k + 1, "text": "Description neg-test"}
    raise Skip("no target-folder line in sortspec.md")


def _mut_delete_one_text_block(ctx, root):
    """SPEC v1.2 section 13 (C10 strict): delete the single body line that holds ONE text block of the page
    with the most text blocks. The block's key must occur exactly once in the note and on exactly one
    line that holds no other block key, no wikilink, no table row and no code / comment, so exactly one
    block disappears and nothing else (links, order) changes. Candidates are tried from the middle of
    the page outwards; pages are tried in descending block count."""
    texts = ctx.exp.page_texts()
    pages = sorted(((len(items), pid) for pid, items in texts.items()
                    if pid in ctx.P and len(ctx.ids.get(pid, [])) == 1), key=lambda t: (-t[0], ctx.P[t[1]]))
    for n, pid in pages[:50]:
        rel = ctx.P[pid]
        p = os.path.join(root, *rel.split("/"))
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        fm = parse_frontmatter(text)
        if fm.error or not fm.present:
            continue
        body = remove_comments(fm.body)
        conv, raw = norm_text(visible_text(body)), norm_text(body)
        blines = fm.body.split("\n")
        masked = mask_code_lines(fm.body)
        line_norm = [(norm_text(visible_text(ln)), norm_text(ln)) for ln in blines]
        items = texts[pid]
        keys = [it[2] for it in items]
        key_count = Counter(keys)
        mid = len(items) // 2
        order = sorted(range(len(items)), key=lambda k: (abs(k - mid), k))
        for k in order:
            it = items[k]
            key = it[2]
            if key_count[key] != 1 or conv.count(key) != 1 or raw.count(key) != 1:
                continue
            hits = [li for li, (c, r) in enumerate(line_norm) if key in c or key in r]
            if len(hits) != 1:
                continue
            li = hits[0]
            line = blines[li]
            st = line.strip()
            if not st or "[[" in line or st.startswith("|") or masked[li].strip() == "" or masked[li] != line:
                continue
            c, r = line_norm[li]
            if any(o != key and (o in c or o in r) for o in key_count):
                continue
            new = blines[:li] + blines[li + 1:]
            if 0 < li < len(new) and new[li - 1].strip() == "" and new[li].strip() == "":
                del new[li]  # keep at most one blank line between blocks
            head = text[: len(text) - len(fm.body)]
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(head + "\n".join(new))
            return {"note": rel, "page": pid, "page_text_blocks": n, "deleted_body_line": li + 1,
                    "deleted_file_line": fm.body_line0 + li, "block": it[0], "block_type": it[1]}
    raise Skip("no page with a uniquely identifiable single-line text block")


NEG_CASES = [
    ("01_delete_note", "C02", _mut_delete_note),
    ("02_swap_sibling_order", "C04", _mut_swap_order),
    ("03_move_note_to_wrong_parent", "C03", _mut_move_wrong_parent),
    ("04_duplicate_notion_id", "C02", _mut_duplicate_note),
    ("05_break_internal_link", "C06", _mut_break_link),
    ("06_delete_attachment", "C07", _mut_delete_attachment),
    ("07_swap_teamspaces_in_sortspec", "C05", _mut_swap_sortspec),
    ("08_sortspec_fatal_desc_line", "C05", _mut_sortspec_fatal_line),
    ("09_delete_one_text_block_in_largest_page", "C10", _mut_delete_one_text_block),
]


def _inside(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def run_negative_tests(exp, vault_root, out_dir, base_ctx, base_results, cleanup=False):
    neg_root = os.path.join(out_dir, "neg")
    os.makedirs(neg_root, exist_ok=True)
    cases = []
    for name, target, fn in NEG_CASES:
        case_dir = os.path.join(neg_root, name)
        if not _inside(case_dir, neg_root):
            raise InputError("refusing to touch a path outside OUT/neg")
        if os.path.exists(case_dir):
            shutil.rmtree(case_dir)
        os.makedirs(case_dir)
        copy = os.path.join(case_dir, "vault")
        shutil.copytree(vault_root, copy, symlinks=True)
        rec = {"case": name, "target_check": target, "copy": copy,
               "baseline_target_status": base_results[target]["status"]}
        try:
            rec["mutation"] = fn(base_ctx, copy)
        except Skip as exc:
            rec.update(skipped=str(exc), ok=False, target_status=None, conclusive=False)
            cases.append(rec)
            continue
        _ctx, res = run_checks(exp, copy)
        rec["target_status"] = res[target]["status"]
        rec["target_summary"] = res[target]["summary"]
        rec["statuses"] = {k: r["status"] for k, r in res.items()}
        rec["ok"] = res[target]["status"] == "FAIL"
        rec["conclusive"] = rec["baseline_target_status"] == "PASS"
        write_json(os.path.join(case_dir, "verify.json"), build_report(exp, copy, res))
        if cleanup:
            shutil.rmtree(copy)
            rec["copy"] = None
        cases.append(rec)
    summary = {"generated_at": now_iso(), "vault": os.path.abspath(vault_root),
               "all_detected": all(c["ok"] for c in cases),
               "all_conclusive": all(c.get("conclusive") for c in cases), "cases": cases}
    write_json(os.path.join(out_dir, "negative_tests.json"), summary)
    return summary


# ---------------------------------------------------------------- main
def verify(snapshot, sidebar, vault, out, roots_kinds=None, negative_tests=False, neg_cleanup=False, as_of=None,
           expected_deviations=None, ids=None, snapshot_immutable=False):
    vault_abs, out_abs = os.path.realpath(vault), os.path.realpath(out)
    if _inside(out_abs, vault_abs):
        raise InputError("--out must not be inside the vault (reports would pollute the vault scan)")
    if _inside(vault_abs, os.path.join(out_abs, "neg")):
        raise InputError("--vault must not be inside OUT/neg")
    if out_abs == os.path.dirname(os.path.realpath(snapshot)):
        raise InputError("--out must not be the snapshot directory (the verifier never writes next to it)")
    if negative_tests and (expected_deviations or ids):
        # the mutators pick objects vault-wide: inside a scope / on a listed object a fault would be "undetected"
        raise InputError("--negative-tests cannot be combined with --expected-deviations or --ids")
    dev = load_expected_deviations(expected_deviations) if expected_deviations else None
    scope = load_ids(ids) if ids else None
    exp = Expectation(snapshot, sidebar, roots_kinds, as_of=as_of, snapshot_immutable=snapshot_immutable)
    os.makedirs(out_abs, exist_ok=True)
    ctx, results = run_checks(exp, vault_abs, dev=dev, scope=scope)
    extra = None
    if dev is not None or scope is not None:
        extra = OrderedDict()
        if dev is not None:
            extra["registered_deviations"] = dev.report()
        if scope is not None:
            extra["scope"] = scope.report()
    neg = None
    if negative_tests:
        neg = run_negative_tests(exp, vault_abs, out_abs, ctx, results, neg_cleanup)
        extra = {"negative_tests": {"all_detected": neg["all_detected"], "all_conclusive": neg["all_conclusive"],
                                    "cases": [{k: c.get(k) for k in ("case", "target_check", "target_status",
                                                                     "baseline_target_status", "ok", "conclusive",
                                                                     "skipped", "mutation")}
                                              for c in neg["cases"]]}}
    rep = build_report(exp, vault_abs, results, extra)
    if dev is not None:
        rep["inputs"]["expected_deviations"] = {"path": dev.path, "sha256": dev.sha256}
    if scope is not None:
        rep["inputs"]["ids"] = {"path": scope.path, "sha256": scope.sha256, "count": len(scope.ids)}
    write_json(os.path.join(out_abs, "verify.json"), rep)
    write_text(os.path.join(out_abs, "verify.md"), render_md(rep))
    ok = rep["overall"] == "PASS" and (neg is None or neg["all_detected"])
    return ok, rep, neg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Independent verifier for the Notion -> Obsidian vault (SPEC v1.2).")
    ap.add_argument("--snapshot", required=True,
                    help="snapshot SQLite DB (read-only; committed data in a non-empty WAL is included)")
    ap.add_argument("--sidebar", required=True, help="sidebar evidence JSON (teamspace + root order)")
    ap.add_argument("--vault", required=True, help="vault or staging vault root (read-only)")
    ap.add_argument("--out", required=True, help="report directory (must be outside the vault)")
    ap.add_argument("--roots-kinds", help="optional <snap.db>.roots.json with root kind / in_trash")
    ap.add_argument("--negative-tests", action="store_true",
                    help="also inject known faults into copies under OUT/neg and confirm detection")
    ap.add_argument("--neg-cleanup", action="store_true",
                    help="with --negative-tests: delete the mutated vault copies afterwards (keeps verify.json)")
    ap.add_argument("--as-of", help="optional ISO UTC time the vault was exported (e.g. 2026-11-16T19:37:39+00:00); "
                                    "missing objects fetched later are labelled as snapshot growth in C02 "
                                    "(classification only, statuses unchanged)")
    ap.add_argument("--expected-deviations", metavar="JSON",
                    help="expected_deviations.json from refresh's read-only vault scan (WRITEBACK_SPEC 14.0): issues "
                         "caused by listed objects (local_new, hold_ids, name_overrides, sync_conflict_copies; optional "
                         "paused, local_modified) become INFO (registered_deviations); unlisted issues still FAIL")
    ap.add_argument("--ids", metavar="FILE",
                    help="one notion_id per line: C03/C04/C06/C08/C10 only check these objects (WRITEBACK_SPEC W6); "
                         "C01/C02/C05/C07/C09/C11 still check the whole vault")
    ap.add_argument("--snapshot-immutable", action="store_true",
                    help="verify a finished snapshot with immutable=1. A non-empty -wal/-journal, or a change during "
                         "the read, is an input error (exit 2)")
    a = ap.parse_args(argv)
    try:
        ok, rep, neg = verify(a.snapshot, a.sidebar, a.vault, a.out, a.roots_kinds, a.negative_tests,
                              a.neg_cleanup, a.as_of, expected_deviations=a.expected_deviations, ids=a.ids,
                              snapshot_immutable=a.snapshot_immutable)
    except InputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for cid, r in rep["checks"].items():
        print(f"{cid} {r['status']}: {r['summary']}")
    if "registered_deviations" in rep:
        d = rep["registered_deviations"]
        print(f"REGISTERED_DEVIATIONS(info) total={d['total']} by_check={json.dumps(d['by_check'])} "
              f"unmatched_entries={len(d['unmatched_entries(info)'])} rejected_entries={len(d['rejected_entries'])}")
    if "scope" in rep:
        s = rep["scope"]
        print(f"SCOPE(--ids) listed={s['ids']} checked={s['counts']['checked']} "
              f"trashed_absent(info)={s['counts']['trashed_absent']} unverifiable={s['counts']['unverifiable']} "
              f"notes_in_scope={s['notes_in_scope']}")
    if neg is not None:
        for c in neg["cases"]:
            print(f"NEG {c['case']}: target {c['target_check']} -> {c.get('target_status')} "
                  f"{'detected' if c['ok'] else 'NOT DETECTED'}{' (skipped: ' + c['skipped'] + ')' if c.get('skipped') else ''}")
    print(f"OVERALL {rep['overall']}" + ("" if neg is None else f"; negative tests all detected={neg['all_detected']}"))
    print(f"report: {os.path.join(os.path.realpath(a.out), 'verify.json')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
