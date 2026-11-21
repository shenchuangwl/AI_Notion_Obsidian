#!/usr/bin/env python3
"""Render a Notion snapshot (nmig rest_inventory SQLite) into an Obsidian vault tree.

Contract: ../docs/SPEC.md.  Deterministic: the same snapshot + the same state DB give
byte-identical output (no run timestamps are written into notes).

Usage:
  python3 -I export_obsidian.py --snapshot <snap.db> --sidebar <sidebar.json> \
      --run-id <run_id> [--state <export.db>] [--out <staging_dir>] \
      [--overrides <overrides.json>] [--no-block-lines]

Writes <out>/vault/** , <out>/manifest.json , <out>/export_report.json and updates the
objects_map table of the state DB (assigned names/paths, rendered sha256).
It never writes into the formal vault (that is publish_vault.py's job).

Renderer 1.2.1 side outputs (outside vault/, not listed in manifest.json; write-back D-09):
  <out>/block_lines.jsonl  one row per rendered block / gap line run of every page|row|template note
                           (physical 1-based line numbers incl. frontmatter; see _Recorder)
  <out>/page_json.jsonl    {notion_id, kind, rel_path, sha256, page_json_sha, renderer_version} per note
  <out>/attachments.json   {file_key: rel | null} attachment map actually used by this run
Recording never changes a single output byte (rendered lines are tagged with a str subclass).
--overrides {notion_id: local_rel}: same-folder local file names (WRITEBACK_SPEC §7.4); rejected ones are
listed in export_report.json["overrides"].
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
from urllib.parse import unquote
from collections import ChainMap, Counter, defaultdict

import yaml

_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:  # `python3 -I` does not put the script directory on sys.path
    sys.path.insert(0, _TOOLS)
from wb_common import open_snapshot_reader, snapshot_signature  # noqa: E402
from wb_norm import canon_block, page_blocks_dfs, page_json_sha, page_json_sha_canon  # noqa: E402

# Bump whenever ANY rendered byte can change (write-back refuses notes rendered by another version).
# 1.2.1 = byte-identical to the 2026-11-17 09:49 renderer + block_lines recording + name overrides.
RENDERER_VERSION = "1.2.1"
# Versions whose output must stay byte-identical to the production renderer (contract 1.9 / 5.7, L-03). Generated-
# file changes that alter bytes are prepared for the NEXT version and switch on with the bump (a lead decision):
# README_LINES_NEXT (R-048) and the Notion_Obsidian sortspec sections (R-122, WB 5.1a; sortspec.TASK_SECTIONS).
BYTE_FROZEN_RENDERERS = ("1.2.1",)


def renderer_bytes_frozen(version=None):
    """True while the renderer version (default: the current RENDERER_VERSION) must reproduce the 1.2.1 bytes."""
    return (RENDERER_VERSION if version is None else version) in BYTE_FROZEN_RENDERERS

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEAMSPACES = ["General", "Quantitative Investing", "x_to_notion", "Godcreate Business", "Godcreate AI工作空间"]
TASK_DIR = "Notion_Obsidian"
ATT_DIR = "_attachments"
RESERVED_FM = {"aliases", "tags", "cssclasses", "cssclass", "alias", "tag"}
FILE_BLOCKS = ("image", "video", "audio", "pdf", "file")
EMBEDDABLE = ("image", "video", "audio", "pdf")
NOTION_HOSTED = re.compile(r"^https://(prod-files-secure\.s3\.[a-z0-9-]+\.amazonaws\.com|s3\.[a-z0-9-]+\.amazonaws\.com/secure\.notion-static\.com|file\.notion\.so)/")
HEX32 = re.compile(r"([0-9a-fA-F]{32})")
HEXDASH = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
LANG_MAP = {"plain text": "", "c++": "cpp", "c#": "csharp", "f#": "fsharp", "objective-c": "objectivec",
            "vb.net": "vbnet", "shell": "shell", "markup": "markup", "java/c/c++/c#": "java"}
README_LINES = [
    "# Notion_Obsidian：本库说明",
    "",
    "本库由 Notion 工作区 Godcreate 的 5 个团队协作区单向导出生成（General、Quantitative Investing、x_to_notion、Godcreate Business、Godcreate AI工作空间），通过 Obsidian Sync 与远程库 “Godcreate” 同步。",
    "",
    "- 每篇迁移笔记的 frontmatter 带 `notion_id`、`notion_url`、`notion_parent_id`、`notion_order` 等，可追溯到 Notion 原页面。",
    "- 有子页面的页面是“文件夹 + 同名笔记”；数据库是“文件夹 + 说明笔记 + .base 视图 + 记录”，数据库页面模板在 `_templates/`。",
    "- 文件列表顺序由社区插件 Custom File Explorer sorting 按 `Notion_Obsidian/sortspec.md` 呈现；`Godcreate 目录索引` 也按 Notion 顺序列出全部页面。",
    "- 每日 03:30（增量）与每周日 04:30（全量）自动从 Notion 更新；在库内改过的迁移笔记不会被覆盖，Notion 的新版本会放到 `Notion_Obsidian/conflicts/<运行编号>/`；Notion 中删除的页面不会自动从库内删除。",
    "- 每轮更新的结果见 `Notion_Obsidian/运行记录/`。",
    "- 工具、快照、备份与报告在库外：`/data120/Obsidian/notion-obsidian/`（说明见执行方案文档）。",
    "- 误删找回：Obsidian 设置→同步→已删除文件 / 版本历史；文件恢复核心插件；库外备份 `backup/` 与 `tools/rollback.py`。",
    "",
    "本文件由导出器生成（notion_kind: generated），每轮会被重新生成，请勿手改。",
]
# R-048 (D-01, U-4): README of the next renderer version. Deployed schedule: incremental timer `*-*-* 00/6:30:00`,
# full timer `Sun *-*-* 05:30:00` (both RandomizedDelaySec=15min); O->N write-back is manual with approval; the one
# operations document (written by the lead) is OPS_DOC. Not used while renderer_bytes_frozen() (byte identity).
OPS_DOC = "/data120/Obsidian/notion_obsidian+obsidian_notion 同步相通执行文档.md"
README_LINES_NEXT = [
    "# Notion_Obsidian：本库说明",
    "",
    "本库由 Notion 工作区 Godcreate 的 5 个团队协作区导出生成（General、Quantitative Investing、x_to_notion、Godcreate Business、Godcreate AI工作空间），通过 Obsidian Sync 与远程库 “Godcreate” 同步。",
    "",
    "- 每篇迁移笔记的 frontmatter 带 `notion_id`、`notion_url`、`notion_parent_id`、`notion_order` 等，可追溯到 Notion 原页面。",
    "- 有子页面的页面是“文件夹 + 同名笔记”；数据库是“文件夹 + 说明笔记 + .base 视图 + 记录”，数据库页面模板在 `_templates/`。",
    "- 文件列表顺序由社区插件 Custom File Explorer sorting 按 `Notion_Obsidian/sortspec.md` 呈现；`Godcreate 目录索引` 也按 Notion 顺序列出全部页面。",
    "- Notion → Obsidian 自动更新：每 6 小时增量一次（约 00:30、06:30、12:30、18:30），每周日约 05:30 全量一次。只在库内改过的迁移笔记保留本地版本、不被覆盖；Notion 与库内都改过时库内文件不动，Notion 的新版本放到 `Notion_Obsidian/conflicts/<运行编号>/`；Notion 中删除的页面不会自动从库内删除。",
    "- Obsidian → Notion 只在人工确认后写回：在终端生成写回计划、审阅并确认后的修改才会写入 Notion；计划的只读副本在 `Notion_Obsidian/写回预览/`。",
    "- 每轮更新与写回的结果见 `Notion_Obsidian/运行记录/`（最新在上）。",
    f"- 工具、快照、备份与报告在库外：`/data120/Obsidian/notion-obsidian/`；操作说明见唯一操作文档 `{OPS_DOC}`。",
    "- 误删找回：Obsidian 设置→同步→已删除文件 / 版本历史；文件恢复核心插件；库外备份 `backup/` 与 `tools/rollback.py`。",
    "",
    "本文件由导出器生成（notion_kind: generated），每轮会被重新生成，请勿手改。",
]


def readme_lines():
    """Lines of Notion_Obsidian/README.md for the current renderer version."""
    return README_LINES if renderer_bytes_frozen() else README_LINES_NEXT


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects_map(
  notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, name TEXT, parent_id TEXT, teamspace TEXT,
  ord INT, title TEXT, rendered_sha256 TEXT, published_sha256 TEXT, published_path TEXT,
  published_run TEXT, status TEXT, first_run TEXT, last_run TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, kind TEXT, snapshot TEXT, started TEXT, finished TEXT, stats TEXT);
"""


# ---------------------------------------------------------------- helpers
def norm_id(s):
    if not s:
        return None
    s = s.strip().lower().replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", s):
        return None
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


def nid32(i):
    return i.replace("-", "")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


_FW = {"\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜",
       "#": "＃", "^": "＾", "[": "［", "]": "］"}


def sanitize(title, oid, max_bytes=120):
    t = unicodedata.normalize("NFC", title or "")
    t = t.replace("...", "…").replace("%", "％")  # custom-sort: '...' is a wildcard, leading '%' unlistable
    t = "".join(_FW.get(ch, ch) for ch in t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cc" or ch in "\t\n\r")
    t = re.sub(r"[\t\r\n]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.rstrip(".").strip()
    if t.startswith("."):
        t = "．" + t[1:]
    if not t:
        t = "Untitled"
    if len(t.encode("utf-8")) > max_bytes:
        out = ""
        for ch in t:
            if len((out + ch).encode("utf-8")) > max_bytes - 3:
                break
            out += ch
        t = out.rstrip() + "…" + f" ({nid32(oid)[:8]})"
    return t


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fold(name):
    return unicodedata.normalize("NFC", name).casefold()


def plain(rich):
    return "".join(r.get("plain_text", "") for r in (rich or []))


def title_of(obj, kind):
    if kind == "database" or kind == "data_source":
        return plain(obj.get("title"))
    for p in (obj.get("properties") or {}).values():
        if isinstance(p, dict) and p.get("type") == "title":
            return plain(p.get("title"))
    return ""


def in_trash(obj):
    return bool(obj and (obj.get("in_trash") or obj.get("archived") or obj.get("is_archived")))


def extract_notion_id(url):
    """Return the page/database id referenced by a Notion URL, or None."""
    if not url or not re.match(r"^https?://([a-z0-9-]+\.)?(notion\.so|notion\.site|notion\.com)/", url):
        return None
    m = re.search(r"[?&]p=([0-9a-fA-F-]{32,36})", url)
    if m and norm_id(m.group(1)):
        return norm_id(m.group(1))
    path = url.split("?")[0].split("#")[0]
    hits = HEXDASH.findall(path) or HEX32.findall(path)
    return norm_id(hits[-1]) if hits else None


# ---------------------------------------------------------------- markdown escaping
_INLINE_ESC = re.compile(r"([\\`*_\[\]$~])")


_URL_RE = re.compile(r"https?://[^\s<>\[\]()]+")


def esc_inline(s, table=False):
    """Minimal escaping of plain text; URL substrings are kept raw (Obsidian autolinks them)."""
    if not s:
        return ""
    out, pos = [], 0
    for m in _URL_RE.finditer(s):
        out.append(_esc_plain(s[pos:m.start()], table))
        u = m.group(0)
        out.append(u.replace("|", "%7C") if table else u)
        pos = m.end()
    out.append(_esc_plain(s[pos:], table))
    return "".join(out)


def _esc_plain(s, table=False):
    if not s:
        return ""
    s = _INLINE_ESC.sub(r"\\\1", s)
    s = s.replace("==", r"\=\=").replace("%%", r"\%\%")
    s = re.sub(r"<(?=[A-Za-z/!?%])", r"\\<", s)
    s = re.sub(r"(^|(?<=\s))#(?=[^\s#])", r"\\#", s)
    if table:
        s = s.replace("|", r"\|")
    return s


_LINE_START = re.compile(r"^(\s*)(>|\||#{1,6}(?=\s|$)|[+\-*](?=\s|$)|\d{1,9}[.)](?=\s|$)|=+\s*$|-+\s*$|_{3,}\s*$|\*{3,}\s*$)")
_THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")


def esc_line_start(line):
    m0 = re.match(r"^[ \t]+", line)
    if m0:  # leading spaces would turn into indented code: keep them visible as NBSP
        return "\u00a0" * len(m0.group(0).expandtabs(4)) + line[m0.end():]
    m = _LINE_START.match(line)
    if not m:
        return line
    lead, tok = m.group(1), m.group(2)
    if re.fullmatch(r"\d+[.)]", tok):
        return lead + tok[:-1] + "\\" + tok[-1] + line[m.end():]
    return lead + "\\" + line[len(lead):]


def wikilink_text(s):
    s = (s or "").replace("\n", " ").replace("|", "｜").replace("[", "［").replace("]", "］")
    return s.strip() or "link"


def url_for_md(u, table=False):
    u = (u or "").strip()
    if table:
        u = u.replace("|", "%7C")
    if any(c in u for c in " ()<>"):
        return "<" + u.replace("<", "%3C").replace(">", "%3E") + ">"
    return u


_TAGS = {"**": "strong", "*": "em", "~~": "del"}


def _blankish(line):
    return line.replace(">", "").strip() == ""


def fmt_date(d):
    """Notion date mention -> readable text (start [→ end]); time zone kept only as offset."""
    def one(x):
        if not x:
            return ""
        if "T" in x:
            day, rest = x.split("T", 1)
            hm = rest[:5]
            off = ""
            m = re.search(r"([+-]\d\d:\d\d|Z)$", rest)
            if m and m.group(1) != "Z":
                off = f" ({m.group(1)})"
            return f"{day} {hm}{off}"
        return x
    if not d:
        return ""
    s = one(d.get("start"))
    if d.get("end"):
        s += " → " + one(d.get("end"))
    return s


def sniff_ext(path, ctype=None):
    try:
        head = open(path, "rb").read(16)
    except OSError:
        head = b""
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    if ctype:
        import mimetypes
        e = mimetypes.guess_extension(ctype.split(";")[0].strip())
        if e:
            return e.lstrip(".")
    return ""


def _is_punct(ch):
    return unicodedata.category(ch)[0] in "PS"


def wrap_marks(s, mark):
    if not s.strip():
        return s
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    core = s.strip()
    # CommonMark flanking rules break `**（x）**` next to CJK letters: use HTML tags then
    if _is_punct(core[0]) or _is_punct(core[-1]):
        tag = _TAGS[mark]
        return f"{lead}<{tag}>{core}</{tag}>{trail}"
    return f"{lead}{mark}{core}{mark}{trail}"


class _IndentDumper(yaml.SafeDumper):
    """Indent block sequences under mappings, as Obsidian writes .base files."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


# ---------------------------------------------------------------- block_lines recording
class RenderRefused(Exception):
    """A sandbox (planning) render needed something the persisted run state cannot reproduce exactly."""


# WRITEBACK_SPEC §4.3: the only block types that can ever be written back ("R?" = candidate; the planner
# confirms R with md2blocks round trips).  Everything else is X with x_reason "type:<t>".
R_TYPES = frozenset(("paragraph", "heading_1", "heading_2", "heading_3", "heading_4", "bulleted_list_item",
                     "numbered_list_item", "to_do", "quote", "code", "equation", "divider", "table_row", "callout"))
BLOCK_LINE_KEYS = ("notion_id", "base_sha", "renderer_version", "block_id", "owner_page_id", "parent_block_id",
                   "ord", "depth", "type", "own_start", "own_end", "line_start", "line_end", "prefix", "zero_line",
                   "klass", "x_reason", "json_sha", "seq")


class _Ln(str):
    """A rendered line tagged with its origin.  It IS a str (equality, join, strip, slicing results are
    plain str), so tagging changes no output byte.  Attributes: key (block occurrence key | None),
    role ('own' | 'sep' | 'gap'), c (creation counter), merged_into (line it was merged into)."""


class _Recorder:
    """Per-page block <-> line recorder (exporter.md §4.2).

    Lines are tagged when created: a block's own lines ('own'), structural separator lines a block emits
    between its own lines and its children ('sep'), and lines render_blocks inserts between sibling chunks
    ('gap').  finalize() maps the surviving lines (after dump_note's leading/trailing pops) to physical
    1-based file line numbers and produces block_lines rows."""

    def __init__(self, ex, page_id):
        self.ex = ex
        self.page = page_id
        self.stack = []        # occurrence keys of the blocks being rendered
        self.meta = {}         # key -> dict
        self.order = []        # keys in pre-order
        self.occ = Counter()
        self.created = []      # every tagged line, in creation order
        self.n = 0

    def _new(self, s, key, role):
        x = _Ln(s)
        x.key, x.role, x.c, x.merged_into = key, role, self.n, None
        self.n += 1
        self.created.append(x)
        return x

    def enter(self, b, parent_id, idx, prefix):
        bid = b.get("id") or None
        if bid is None:
            # overlay block the planner wants to create (no Notion id yet): a unique occurrence key that can never
            # equal a block id, another new block or the top-level marker None; its row keeps block_id null
            key, dup = ("new", self.n), False
        else:
            self.occ[bid] += 1
            dup = self.occ[bid] > 1
            key = (bid, self.occ[bid]) if dup else bid
        rparent = self.stack[-1] if self.stack else None
        pm = self.meta.get(rparent) if rparent is not None else None
        snap_row = self.ex.block_row.get(bid) if bid is not None else None
        owner = snap_row[0] if snap_row else (pm["owner"] if pm else self.page)  # overlay blocks: inherit
        self.meta[key] = {"block": b, "block_id": bid, "type": b.get("type"),
                          "parent_block_id": None if parent_id == self.page else parent_id,
                          "ord": idx, "depth": len(self.stack), "prefix": prefix, "owner": owner,
                          "rparent": rparent, "synced": bool(pm and (pm["synced"] or pm["type"] == "synced_block")),
                          "dup": dup, "c": self.n, "anchor": None}
        self.n += 1
        self.order.append(key)
        self.stack.append(key)
        return key

    def leave(self):
        self.stack.pop()

    def own(self, chunk, key):
        return [x if type(x) is _Ln else self._new(x, key, "own") for x in chunk]

    def leaf(self, b, parent_id, idx, prefix, line):
        key = self.enter(b, parent_id, idx, prefix)
        self.leave()
        return self._new(line, key, "own")

    def sep(self, s):
        return self._new(s, self.stack[-1], "sep") if self.stack else self.gap(s, self.page, "")

    def gap(self, s, parent_id, prefix):
        x = self._new(s, None, "gap")
        x.gparent = None if parent_id == self.page else parent_id
        x.gdepth = len(self.stack)
        x.gprefix = prefix
        return x

    def set_anchor(self, key, line):
        if key in self.meta:
            self.meta[key]["anchor"] = line

    # ------------------------------------------------------------ rows
    def finalize(self, kept, lead, trail, first, base_sha):
        """kept = body elements after dump_note's pops; lead/trail = popped elements; first = 1-based
        file line of kept[0].  Returns block_lines rows sorted by position, seq 0..n-1.

        Zero-line anchors always name a line that exists in the file: the blank line before the body
        (first - 1) or, when the note has no body at all, the closing frontmatter fence (first - 2).
        A block that keeps some lines but lost one of its own lines (a leading blank line merged into the
        previous blank line, or a blank line trimmed at the note's start/end) is X "own_line_lost": rendering
        it alone would not reproduce its recorded own lines (R-042), so it must never be rebuilt from them."""
        stats = self.ex.report["block_lines"]
        pos = {}
        ln = first
        for x in kept:
            k = x.count("\n") + 1
            pos[id(x)] = (ln, ln + k - 1)
            ln += k
        head = first - 1 if kept else first - 2     # last file line before the body; exists in every note
        last = ln - 1 if kept else head
        lead_ids = {id(x) for x in lead}
        trail_ids = {id(x) for x in trail}

        def resolve(x):
            hops = 0
            while x is not None and hops < 100000:
                i = id(x)
                if i in pos:
                    return pos[i][1]
                if i in lead_ids:
                    return head
                if i in trail_ids:
                    return last
                x = getattr(x, "merged_into", None)
                hops += 1
            return None

        own, span, gaps = {}, {}, []
        line_key = {}
        for x in kept:
            a, b = pos[id(x)]
            if type(x) is not _Ln:
                stats["untagged_lines"] += 1
                gaps.append((a, b, -1, "untagged", None, None, ""))
                continue
            line_key[(a, b)] = x
            # kept lines arrive in increasing line order: first hit = start, last hit = end
            if x.role == "own":
                o = own.get(x.key)
                if o is None:
                    own[x.key] = [a, b]
                else:
                    o[1] = b
            if x.role != "gap":
                sp = span.get(x.key)
                if sp is None:
                    span[x.key] = [a, b]
                else:
                    sp[1] = b
            if x.role == "sep":
                m = self.meta[x.key]
                gaps.append((a, b, x.c, "sep", m["block_id"], m["depth"], m["prefix"]))
            elif x.role == "gap":
                gaps.append((a, b, x.c, "gap", x.gparent, x.gdepth, x.gprefix))
        first_lost = {}
        own_lost = set()     # keys that lost at least one of their own lines (merged or popped)
        for x in self.created:
            if x.key is not None and id(x) not in pos:
                if x.key not in first_lost:
                    first_lost[x.key] = x
                if x.role == "own":
                    own_lost.add(x.key)
        kids = defaultdict(list)
        pre = {}
        for i, k in enumerate(self.order):
            pre[k] = i
            kids[self.meta[k]["rparent"]].append(k)
        # per physical line: pre-order index of the owning block (sep lines count for their block), for the
        # self-checks below; gap/untagged lines are neutral (+inf for min, -1 for max)
        big = len(self.order) + 1
        nl = (last if kept else first) + 2
        kmin, kmax, kown = [big] * nl, [-1] * nl, [-1] * nl
        for (a, b), x in line_key.items():
            if x.key is None:
                continue
            q = pre[x.key]
            for li in range(a, b + 1):
                kmin[li] = kmax[li] = q
                if x.role == "own":
                    kown[li] = q
        sub, sub_lost, size = {}, {}, {}
        for k in reversed(self.order):
            rng = list(span[k]) if k in span else None
            lost = first_lost.get(k)
            sz = 1
            for ch in kids.get(k, ()):
                sz += size[ch]
                r = sub[ch]
                if r:
                    rng = [min(rng[0], r[0]), max(rng[1], r[1])] if rng else list(r)
                cl = sub_lost[ch]
                if cl is not None and (lost is None or cl.c < lost.c):
                    lost = cl
            sub[k], sub_lost[k], size[k] = rng, lost, sz
        rows = []
        for k in self.order:
            m = self.meta[k]
            rng = sub[k]
            if rng:
                ls, le, zero = rng[0], rng[1], 0
                # contiguity self-check: every keyed line inside the subtree range belongs to the subtree
                if min(kmin[ls:le + 1]) < pre[k] or max(kmax[ls:le + 1]) >= pre[k] + size[k]:
                    stats["contiguity_violations"] += 1
            else:
                anc = resolve(sub_lost[k]) if sub_lost[k] is not None else None
                if anc is None and m["anchor"] is not None:
                    anc = resolve(m["anchor"])
                if anc is None:
                    pr = sub.get(m["rparent"]) if m["rparent"] is not None else None
                    anc = pr[0] if pr else head
                ls = le = anc
                zero = 1
                stats["zero_line_blocks"] += 1
            o = own.get(k)
            if zero:
                os_, oe = ls, le
            elif o:
                os_, oe = o
                seg = kown[os_:oe + 1]
                if min(seg) != pre[k] or max(seg) != pre[k]:
                    stats["own_noncontiguous"] += 1
            else:
                os_ = oe = None
            kl, xr = self.ex.structural_klass(m["block"], self.page, m["owner"], m["synced"], m["dup"])
            if not zero and k in own_lost:
                stats["own_line_lost"] += 1
                if kl == "R?":
                    kl, xr = "X", "own_line_lost"
            rows.append(((ls, m["c"]), {
                "notion_id": self.page, "base_sha": base_sha, "renderer_version": RENDERER_VERSION,
                "block_id": m["block_id"], "owner_page_id": m["owner"], "parent_block_id": m["parent_block_id"],
                "ord": m["ord"], "depth": m["depth"], "type": m["type"], "own_start": os_, "own_end": oe,
                "line_start": ls, "line_end": le, "prefix": m["prefix"], "zero_line": zero, "klass": kl,
                "x_reason": xr, "json_sha": self.ex.block_sha(m["block"])}))
        gaps.sort(key=lambda g: (g[0], g[2]))
        merged = []
        for g in gaps:
            if merged and tuple(merged[-1][3:]) == g[3:] and merged[-1][1] + 1 == g[0]:
                merged[-1][1] = g[1]
            else:
                merged.append(list(g))
        for a, b, c, role, parent, depth, prefix in merged:
            rows.append(((a, c), {
                "notion_id": self.page, "base_sha": base_sha, "renderer_version": RENDERER_VERSION,
                "block_id": None, "owner_page_id": None, "parent_block_id": parent, "ord": None, "depth": depth,
                "type": "gap", "own_start": a, "own_end": b, "line_start": a, "line_end": b, "prefix": prefix,
                "zero_line": 0, "klass": None, "x_reason": role, "json_sha": None}))
        rows.sort(key=lambda r: r[0])
        out = []
        for i, (_, r) in enumerate(rows):
            r["seq"] = i
            out.append(r)
        stats["pages"] += 1
        stats["rows"] += len(out)
        stats["gap_rows"] += len(merged)
        stats["block_rows"] += len(self.order)
        return out


# ---------------------------------------------------------------- exporter
class Exporter:
    def __init__(self, snap_path, sidebar_path, run_id, state_path, out_dir, immutable=False, roots_kinds=None,
                 overrides=None, link_targets=None, record_block_lines=True):
        self.roots_kinds = roots_kinds
        self.run_id = run_id
        self.out = out_dir
        self.vault = os.path.join(out_dir, "vault")
        self._open_snapshot(snap_path, immutable)
        self.sidebar = _read_json(sidebar_path)
        self.state = sqlite3.connect(state_path)
        self.state.executescript(STATE_SCHEMA)
        self.prev = {r[0]: {"name": r[1], "parent": r[2], "title": r[3], "kind": r[4]}
                     for r in self.state.execute("SELECT notion_id,name,parent_id,title,kind FROM objects_map")}
        self._init_common(overrides, record_block_lines, planning=False, link_targets=link_targets)
        self._load_checked_snapshot(SystemExit)

    def _open_snapshot(self, snap_path, immutable, error_type=SystemExit):
        before = snapshot_signature(snap_path)
        self.snap, mode = open_snapshot_reader(snap_path, require_finished=immutable, error_type=error_type)
        self._snapshot_signature = before if mode == "mode=ro&immutable=1" else None
        self.snap_path = snap_path

    def _load_checked_snapshot(self, error_type):
        self.snap.execute("BEGIN")
        try:
            self.load()
        finally:
            self.snap.rollback()
        if self._snapshot_signature is not None and snapshot_signature(self.snap_path) != self._snapshot_signature:
            raise error_type(f"{self.snap_path}: snapshot changed while it was being read; retry with a finished copy")

    def _init_common(self, overrides, record_block_lines, planning, link_targets=None):
        self.report = defaultdict(Counter)
        self.folder_owner = {}
        self.tpl_folder_owner = {}
        self._list_num = 1
        self._blank_count = 0
        self._synced_stack = set()
        self.lists = defaultdict(list)
        self.record_block_lines = record_block_lines
        self._planning = planning
        self._rec = None             # _Recorder while a recorded render is in progress
        self._sandbox = False        # planning render: lookup-only attachments, throwaway counters
        self._att_fixed = False      # attachment map loaded from a previous run (attachments.json)
        self.att_map = {}            # file key -> rel | None, as resolved by this exporter
        self.manifest, self.attachments, self.att_by_sha = {}, {}, {}
        self._canon_cache = None     # id(block) -> (block, canonical json) while run() renders one note
        self.overrides, self.override_applied, self.override_rejected = {}, [], []
        self.link_targets = {}
        for k, v in (link_targets or {}).items():
            nid = norm_id(k) if isinstance(k, str) else None
            if nid and isinstance(v, str) and v.endswith(".md") and "/" not in v[:1] and "\\" not in v:
                self.link_targets[nid] = v
        self.overrides_given = len(overrides or {})
        for k, v in (overrides or {}).items():
            nid = norm_id(k) if isinstance(k, str) else None
            if nid is None:
                self.override_rejected.append({"notion_id": k, "local_rel": v, "reason": "bad_id"})
            else:
                self.overrides[nid] = v

    @classmethod
    def for_planning(cls, snapshot, export_db, overrides=None, attachments_json=None, sidebar=None, roots_kinds=None,
                     link_targets=None):
        """Read-only exporter for write-back planning: finished snapshot, objects_map read-only, nodes and
        paths built exactly as a normal run (same names via objects_map), no writes anywhere.

        attachments_json: the run's ``attachments.json`` ({file_key: rel|null}); when None, a full in-memory
        render pass in run order rebuilds the map (about the time of one export's render phase).
        sidebar / roots_kinds: only needed when ``<snapshot>.roots.json`` does not exist."""
        ex = cls.__new__(cls)
        ex.roots_kinds = roots_kinds or (snapshot + ".roots.json")
        ex.run_id = None
        ex.out = ex.vault = None
        ex.state = None
        ex._open_snapshot(snapshot, immutable=True, error_type=RuntimeError)
        if sidebar:
            ex.sidebar = _read_json(sidebar)
        else:
            ex.sidebar = {"roots": None}
        kinds_path = ex.roots_kinds
        if not os.path.exists(kinds_path) and not sidebar:
            raise ValueError(f"no roots: {kinds_path} missing and no sidebar given")
        if not os.path.isfile(export_db):
            raise FileNotFoundError(export_db)
        wal = export_db + "-wal"
        flags = "mode=ro" if (os.path.exists(wal) and os.path.getsize(wal) > 0) else "mode=ro&immutable=1"
        st = sqlite3.connect(f"file:{export_db}?{flags}", uri=True)
        try:
            ex.prev = {r[0]: {"name": r[1], "parent": r[2], "title": r[3], "kind": r[4]}
                       for r in st.execute("SELECT notion_id,name,parent_id,title,kind FROM objects_map")}
        except sqlite3.OperationalError as e:  # names would not match the published run: refuse
            raise ValueError(f"{export_db}: no usable objects_map ({e})") from None
        finally:
            st.close()
        ex._init_common(overrides, record_block_lines=True, planning=True, link_targets=link_targets)
        ex._load_checked_snapshot(RuntimeError)
        ex.build_tree()
        ex.assign_paths()
        ex._check_overrides_in_tree()
        if attachments_json:
            ex.att_map = _read_json(attachments_json)
        else:
            saved = ex.report, ex.lists
            ex.report, ex.lists = defaultdict(Counter), defaultdict(list)
            try:
                for oid in ex.run_order():
                    n = ex.nodes[oid]
                    if n["kind"] == "database":
                        ex.database_note(n)
                    else:
                        ex.render_page(n)
            finally:
                ex.report, ex.lists = saved
        ex._att_fixed = True
        return ex

    # ------------------------------------------------------------ loading
    def load(self):
        self.obj = {}
        for oid, kind, parent, data in self.snap.execute("SELECT id,kind,parent_id,data FROM objects"):
            self.obj[(oid, kind)] = (json.loads(data), parent)
        self.blocks = defaultdict(list)  # parent_id -> [block json ordered]
        self.block_row = {}              # block id -> (owning page_id, parent_id, ord) as stored in the snapshot
        rows = self.snap.execute("SELECT id,page_id,parent_id,ord,type,has_children,data FROM blocks ORDER BY parent_id, ord")
        for bid, pid, parent, ordv, typ, hc, data in rows:
            self.blocks[parent].append(json.loads(data))
            self.block_row[bid] = (pid, parent, ordv)
        self.files = {}
        for key, name, size, sha, status, err in self.snap.execute("SELECT key,name,size,sha256,status,error FROM files"):
            self.files[key] = {"name": name, "size": size, "sha": sha, "status": status, "ctype": err if status == "ok" else None}
        kinds_path = self.roots_kinds or (self.snap_path + ".roots.json")
        self.roots = _read_json(kinds_path) if os.path.exists(kinds_path) else self.sidebar["roots"]
        self.users = {}
        for (oid, kind), (d, _) in self.obj.items():
            if kind == "page":
                for p in (d.get("properties") or {}).values():
                    if isinstance(p, dict):
                        for u in (p.get("people") or []) + [p.get("created_by") or {}, p.get("last_edited_by") or {}]:
                            if isinstance(u, dict) and u.get("id") and u.get("name"):
                                self.users[u["id"]] = u["name"]

    def get(self, oid, kind):
        v = self.obj.get((oid, kind))
        return v[0] if v else None

    # ------------------------------------------------------------ tree
    def page_children(self, pid):
        """child pages / databases in content order (nested containers included)."""
        out = []

        def walk(parent):
            for b in self.blocks.get(parent, []):
                t = b.get("type")
                if t == "child_page":
                    out.append((b["id"], "page"))
                elif t == "child_database":
                    out.append((b["id"], "database"))
                elif b.get("has_children"):
                    walk(b["id"])
        walk(pid)
        return out

    def db_children(self, did):
        db = self.get(did, "database") or {}
        out = []
        for ref in db.get("data_sources", []):
            ds = self.get(ref["id"], "data_source")
            if not ds:
                self.report["missing"]["data_source"] += 1
                continue
            for rid in ds.get("_row_ids", []):
                out.append((rid, "row"))
        return out

    def db_templates(self, did):
        db = self.get(did, "database") or {}
        out = []
        for ref in db.get("data_sources", []):
            ds = self.get(ref["id"], "data_source") or {}
            out += [norm_id(t) for t in ds.get("_template_ids", []) if norm_id(t)]
        return out

    def build_tree(self):
        """Assign every in-scope object a node: kind, parent, order, teamspace, title, children."""
        self.nodes = {}
        self.ts_roots = {t: [] for t in TEAMSPACES}
        roots = sorted(self.roots, key=lambda r: (r["teamspace_order"], r["root_order"]))
        self.root_ids = {norm_id(r["id"]) for r in roots if not r.get("in_trash")}
        for r in roots:
            rid, kind = norm_id(r["id"]), r.get("kind", "page")
            if r.get("in_trash"):
                self.lists["trashed_roots"].append(rid)
                continue
            if self._add(rid, kind, None, r["root_order"], r["teamspace"]):
                self.ts_roots[r["teamspace"]].append(rid)
            else:
                self.lists["root_not_exported"].append({"id": rid, "title": r.get("title"), "teamspace": r["teamspace"]})

    def _add(self, oid, kind, parent, order, ts):
        if oid in self.nodes:
            self.lists["duplicate_reference"].append({"id": oid, "first_parent": self.nodes[oid]["parent"], "again_under": parent})
            return False
        data = self.get(oid, "database" if kind == "database" else "page")
        if data is None:
            self.lists["missing_object"].append({"id": oid, "kind": kind, "parent": parent})
            return False
        if in_trash(data):
            self.lists["trashed"].append(oid)
            return False
        node = {"id": oid, "kind": kind, "parent": parent, "order": order, "ts": ts, "data": data,
                "title": title_of(data, "database" if kind == "database" else "page"), "children": []}
        self.nodes[oid] = node
        kids = self.db_children(oid) if kind == "database" else self.page_children(oid)
        n = 0
        for cid, ck in kids:
            if not self._is_canonical_child(cid, ck, oid, data):
                continue
            if self._add(cid, ck, oid, n + 1, ts):
                n += 1
                node["children"].append(cid)
        if kind == "database":  # SPEC v1.2: data-source page templates -> <db>/_templates/
            node["templates"] = []
            k = 0
            for tid in self.db_templates(oid):
                if self._is_canonical_child(tid, "template", oid, data) and self._add(tid, "template", oid, k + 1, ts):
                    k += 1
                    node["templates"].append(tid)
        return True

    def _is_canonical_child(self, cid, ck, oid, pdata):
        """A child_page/child_database block (or a query row) is a child only when the child's own
        API `parent` points at this object; otherwise it is a reference (rendered as a link)."""
        if cid in self.root_ids:
            self.lists["reference_to_root"].append({"id": cid, "referenced_from": oid})
            return False
        cdata = self.get(cid, "database" if ck == "database" else "page")
        if cdata is None:
            return True  # recorded as missing by _add
        par = cdata.get("parent") or {}
        pt = par.get("type")
        target = norm_id(par.get(pt)) if pt in ("page_id", "database_id", "data_source_id", "block_id") else None
        if ck in ("row", "template"):
            dss = {norm_id(x["id"]) for x in (pdata.get("data_sources") or [])}
            ok = (pt == "data_source_id" and target in dss) or (pt == "database_id" and target == oid)
        elif pt == "block_id":
            ok = True  # page nested inside a block of the parent (e.g. column); block ids are not tracked here
        else:
            ok = target == oid
        if not ok:
            self.lists["reference_not_parent"].append({"id": cid, "referenced_from": oid, "api_parent": par})
        return ok

    # ------------------------------------------------------------ paths
    def assign_paths(self):
        for ts in TEAMSPACES:
            self._assign_dir(ts, self.ts_roots[ts], reserved=set())

    def _override_name(self, oid, dir_rel):
        """(name, None) when the local file name override for oid is acceptable in dir_rel, else (None, reason).
        Only plain notes (no folder) may be renamed, inside the SAME folder, to a name that sanitize() keeps
        unchanged (so links, sortspec and custom-sort wildcards stay valid)."""
        rel = self.overrides[oid]
        n = self.nodes[oid]
        if not isinstance(rel, str) or not rel.endswith(".md"):
            return None, "not_md"
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
            return None, "bad_path"
        if n["kind"] == "database" or n["children"]:  # folder notes / folders: UNSUPPORTED_RENAME (§7.4.6)
            return None, "folder_note"
        if "/".join(parts[:-1]) != dir_rel:
            return None, "different_folder"
        name = parts[-1][:-3]
        if not name or sanitize(name, oid) != name:
            return None, "unsafe_name"
        return name, None

    def _override_reject(self, oid, reason):
        self.override_rejected.append({"notion_id": oid, "local_rel": self.overrides.get(oid), "reason": reason})
        self.report["names"]["override_rejected"] += 1

    def _check_overrides_in_tree(self):
        for oid in self.overrides:
            if oid not in self.nodes:
                self._override_reject(oid, "not_in_tree")

    def _assign_dir(self, dir_rel, ids, reserved):
        taken = {fold(x) for x in reserved}
        taken.add(fold(ATT_DIR))
        chosen = {}
        # pass 1: keep previous names (stability) when title and parent are unchanged
        for oid in ids:
            n = self.nodes[oid]
            p = self.prev.get(oid)
            if p and p["parent"] == n["parent"] and p["title"] == n["title"] and p["name"]:
                if fold(p["name"]) not in taken:
                    chosen[oid] = p["name"]
                    taken.add(fold(p["name"]))
        # same-folder local name overrides (WRITEBACK_SPEC §7.4.3, R-046): an accepted override name takes part in
        # the same-folder dedupe BEFORE pass 2, so a sibling named in pass 2 (a new page, a changed title) gets the
        # dedupe suffix instead of displacing the user's renamed file.  An override is void (reported) only when its
        # name clashes with a reserved name, a sibling's stable pass-1 name or an earlier override of this folder.
        # Pass-1 names never change (no collateral rename of established notes); the overridden object's own
        # title-derived name stays reserved too (aliases, D-22, and a clean return when the override is cleared).
        # Without overrides every name is exactly what it was before (byte identity).
        ov, ov_owner = {}, {}
        if self.overrides:
            for oid in ids:
                if oid not in self.overrides:
                    continue
                oname, why = self._override_name(oid, dir_rel)
                if not why:
                    f = fold(oname)
                    own = fold(chosen[oid]) if oid in chosen else None
                    if f in ov_owner or (f in taken and f != own):
                        why = "name_clash"
                if why:
                    self._override_reject(oid, why)
                else:
                    ov[oid] = oname
                    ov_owner[fold(oname)] = oid

        def busy(name, oid):
            f = fold(name)
            return f in taken or ov_owner.get(f, oid) != oid

        # pass 2: first come, first served in Notion order
        for oid in ids:
            if oid in chosen:
                continue
            n = self.nodes[oid]
            base = sanitize(n["title"], oid)
            name = base
            if busy(name, oid):
                name = f"{base} ({nid32(oid)[:8]})"
                k = 12
                while busy(name, oid):
                    name = f"{base} ({nid32(oid)[:k]})"
                    k += 4
                self.report["names"]["dedup_suffix"] += 1
            if not (n["title"] or "").strip():
                self.report["names"]["empty_title"] += 1
            chosen[oid] = name
            taken.add(fold(name))
        for oid in ids:
            n = self.nodes[oid]
            name = chosen[oid]
            n["name"] = name
            if oid in ov:
                # D-22: objects_map.name / aliases keep the computed (title-derived) name; the path uses the override
                n["natural_name"] = name
                n["name"] = name = ov[oid]
                self.override_applied.append({"notion_id": oid, "local_rel": f"{dir_rel}/{name}.md",
                                              "computed_rel": f"{dir_rel}/{n['natural_name']}.md"})
            if n["kind"] == "database" or n["children"]:
                folder = f"{dir_rel}/{name}"
                n["folder"] = folder
                self.folder_owner[folder] = oid
                n["note"] = f"{folder}/{name}.md"
                if n["kind"] == "database":
                    n["base"] = f"{folder}/{name}.base"
                self._assign_dir(folder, n["children"], reserved={name, "_templates"} if n["kind"] == "database" else {name})
                if n.get("templates"):
                    tf = f"{folder}/_templates"
                    self.tpl_folder_owner[tf] = oid
                    self._assign_dir(tf, n["templates"], reserved=set())
            else:
                n["folder"] = None
                n["note"] = f"{dir_rel}/{name}.md"

    def link_target(self, oid):
        n = self.nodes.get(oid)
        if not n:
            return None
        target = self.link_targets.get(oid)
        return (target[:-3] if target.endswith(".md") else target) if target else n["note"][:-3]

    def link_note(self, oid):
        """The note path used by generated links and indexes, without changing the exported object path."""
        n = self.nodes.get(oid)
        if not n:
            return None
        return self.link_targets.get(oid) or n["note"]

    def link_index(self):
        """notion_id -> {"note": vault-relative note path, "title": current title, "base": .base path | None}
        (override paths included), i.e. exactly what links/mentions/child blocks render to."""
        return {oid: {"note": self.link_note(oid), "title": n["title"], "base": n.get("base")} for oid, n in self.nodes.items()}

    def run_order(self):
        """Pre-order of all exported objects (teamspace order, sidebar roots, children, then templates):
        the render order of run(); attachment de-duplication depends on it."""
        order = []

        def walk(oid):
            order.append(oid)
            for c in self.nodes[oid]["children"]:
                walk(c)
            for t in self.nodes[oid].get("templates", []):
                walk(t)
        for ts in TEAMSPACES:
            for r in self.ts_roots[ts]:
                if r in self.nodes:
                    walk(r)
        return order

    # ------------------------------------------------------------ attachments
    def attachment(self, ts, key, fallback_name=None):
        if self._sandbox or (self._att_fixed and key in self.att_map):
            # planning: reproduce exactly what the run resolved (rel paths depend on render order)
            if key not in self.att_map:
                raise RenderRefused(f"attachment {key} is not in the run's attachment map")
            rel = self.att_map[key]
            if rel is None:
                self.report["attachments"]["unavailable"] += 1
            return rel
        rel = self._attachment(ts, key, fallback_name)
        self.att_map[key] = rel
        return rel

    def _attachment(self, ts, key, fallback_name=None):
        f = self.files.get(key)
        if not f or f["status"] != "ok" or not f["sha"]:
            self.report["attachments"]["unavailable"] += 1
            self.lists["attachment_unavailable"].append({"key": key, "status": f and f["status"], "name": fallback_name})
            return None
        sha = f["sha"]
        src = os.path.join(WORK, "blobs", sha)
        if not os.path.exists(src):
            self.report["attachments"]["blob_missing"] += 1
            self.lists["attachment_unavailable"].append({"key": key, "status": "blob_missing"})
            return None
        name = unquote(f["name"] or fallback_name or "file")
        stem, ext = os.path.splitext(name)
        ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:10].lower()
        if not ext:
            ext = sniff_ext(src, f.get("ctype"))
            if ext:
                self.report["attachments"]["ext_from_content"] += 1
        stem = sanitize(stem, sha + "0" * 32, max_bytes=80)
        rel = f"{ts}/{ATT_DIR}/{stem}__{sha[:8]}" + (f".{ext}" if ext else "")
        prev = self.att_by_sha.get((ts, sha))
        if prev:
            return prev
        self.att_by_sha[(ts, sha)] = rel
        self.attachments[rel] = src
        self.report["attachments"]["placed"] += 1
        return rel

    # ------------------------------------------------------------ rich text
    def rt(self, rich, ts, table=False):
        out = []
        for r in rich or []:
            typ = r.get("type")
            ann = r.get("annotations") or {}
            href = r.get("href")
            if typ == "equation":
                eq = "$" + (r.get("equation") or {}).get("expression", "").replace("$", r"\$") + "$"
                out.append(eq.replace("|", r"\|") if table else eq)
                continue
            if typ == "mention":
                out.append(self.mention(r, ts, table))
                continue
            text = (r.get("text") or {}).get("content", r.get("plain_text", ""))
            link = ((r.get("text") or {}).get("link") or {}).get("url") or href
            if ann.get("code"):
                segs = []
                for seg in text.split("\n"):  # a code span cannot cross a line break / blank line
                    if not seg:
                        segs.append("")
                        continue
                    ticks = max([len(x) for x in re.findall(r"`+", seg)] + [0]) + 1
                    fence = "`" * ticks
                    pad = " " if seg.startswith("`") or seg.endswith("`") else ""
                    segs.append(f"{fence}{pad}{seg}{pad}{fence}")
                s = "\n".join(segs)
                if "\n" in text:
                    self.report["inline"]["multiline_code_split"] += 1
                if table:
                    s = s.replace("|", r"\|")
                # emphasis around a code span: HTML tags (`**` next to a backtick fails flanking rules)
                for key, tag in (("bold", "strong"), ("italic", "em"), ("strikethrough", "del"), ("underline", "u")):
                    if ann.get(key) and s.strip():
                        s = f"<{tag}>{s}</{tag}>"
            else:
                s = esc_inline(text, table)
                if ann.get("bold"):
                    s = wrap_marks(s, "**")
                if ann.get("italic"):
                    s = wrap_marks(s, "*")
                if ann.get("strikethrough"):
                    s = wrap_marks(s, "~~")
                if ann.get("underline") and s.strip():
                    s = f"<u>{s}</u>"
            if ann.get("color") and ann.get("color") != "default":
                self.report["inline"]["color_dropped"] += 1
            if link:
                s = self.linkify(s, text, link, ts, table)
            out.append(s)
        res = "".join(out)
        if table:
            res = res.replace("\n", "<br>")
        return res

    def wl(self, tgt, alias, table=False):
        return f"[[{tgt}\\|{wikilink_text(alias)}]]" if table else f"[[{tgt}|{wikilink_text(alias)}]]"

    def linkify(self, rendered, text, url, ts, table=False):
        tid = extract_notion_id(url)
        if url.startswith("/") and not tid:
            path = url.split("?")[0].split("#")[0]
            hx = HEX32.findall(path)
            # Notion-internal relative links look like /<id> , /<title>-<id> or /p/<id>; local paths do not
            if hx and re.fullmatch(r"/(?:p/)?(?:[^/]*-)?[0-9a-fA-F]{32}", path):
                tid = norm_id(hx[-1])
                url = "https://app.notion.com" + url
            else:
                self.report["links"]["relative_non_notion_kept"] += 1
        if tid:
            tgt = self.link_target(tid)
            if tgt:
                self.report["links"]["internal_rewritten"] += 1
                if "#" in url:
                    self.report["links"]["block_anchor_dropped"] += 1
                return self.wl(tgt, text, table)
            self.report["links"]["notion_out_of_scope"] += 1
            self.lists["link_out_of_scope"].append({"url": url[:200], "target": tid})
        else:
            self.report["links"]["external"] += 1
        return f"[{rendered}]({url_for_md(url, table)})" if rendered else f"<{url_for_md(url, table).strip('<>')}>"

    def mention(self, r, ts, table):
        m = r.get("mention") or {}
        mt = m.get("type")
        pt = r.get("plain_text", "")
        if mt in ("page", "database"):
            tid = norm_id((m.get(mt) or {}).get("id"))
            tgt = self.link_target(tid)
            if tgt:
                self.report["links"]["mention_rewritten"] += 1
                # Notion shows the target's current title, not the stored plain_text
                return self.wl(tgt, self.nodes[tid]["title"] or pt, table)
            self.report["links"]["mention_out_of_scope"] += 1
            self.lists["link_out_of_scope"].append({"mention": mt, "target": tid, "text": pt[:80]})
            snap_title = None
            for k in ("page", "database"):
                o = self.get(tid, k) if tid else None
                if o:
                    snap_title = title_of(o, k)
                    break
            label = snap_title or pt or "Notion 页面"
            return f"[{esc_inline(label, table)}](https://app.notion.com/p/{nid32(tid) if tid else ''})"
        if mt == "user":
            u = m.get("user") or {}
            return "@" + esc_inline(u.get("name") or self.users.get(u.get("id")) or pt.lstrip("@") or "user", table)
        if mt == "date":
            return esc_inline(fmt_date(m.get("date")) or pt, table)
        if mt in ("link_preview", "link_mention"):
            url = (m.get(mt) or {}).get("url") or r.get("href")
            label = (m.get(mt) or {}).get("title") or pt or url
            return self.linkify(esc_inline(label, table), label, url, ts, table) if url else esc_inline(pt, table)
        self.report["inline"][f"mention_{mt}"] += 1
        return esc_inline(pt, table)

    # ------------------------------------------------------------ blocks
    def render_blocks(self, parent_id, ts, prefix=""):
        """Blocks separated by one blank line; never more than one consecutive blank line is
        produced here, so no later global blank-line collapsing is needed (code stays verbatim)."""
        lines = []
        prev_list = None
        num = 0
        blank = prefix.rstrip()
        rec = self._rec
        for idx, b in enumerate(self.blocks.get(parent_id, [])):
            t = b.get("type")
            self.report["blocks"][t] += 1
            v = b.get(t)
            if isinstance(v, dict) and v.get("color") not in (None, "default"):
                self.report["layout"]["block_color_dropped"] += 1
            if t == "numbered_list_item":
                start = (v or {}).get("list_start_index") if isinstance(v, dict) else None
                num = (num + 1) if prev_list == "numbered_list_item" else (start or 1)
                self._list_num = num
            if rec is None:
                chunk = self.render_block(b, ts, prefix)
            else:
                key = rec.enter(b, parent_id, idx, prefix)
                try:
                    chunk = self.render_block(b, ts, prefix)
                finally:
                    rec.leave()
                chunk = rec.own(chunk, key)
            if not chunk:
                if rec is not None:
                    rec.set_anchor(key, lines[-1] if lines else None)
                continue
            is_list = t in ("bulleted_list_item", "numbered_list_item", "to_do")
            if lines:
                if is_list and prev_list == t:
                    pass
                else:
                    if is_list and prev_list in ("bulleted_list_item", "to_do") and t in ("bulleted_list_item", "to_do"):
                        # same '-' marker: an HTML comment keeps the two lists apart
                        if not _blankish(lines[-1]):
                            lines.append(blank if rec is None else rec.gap(blank, parent_id, prefix))
                        lines.append(prefix + "<!-- -->" if rec is None else rec.gap(prefix + "<!-- -->", parent_id, prefix))
                    if not _blankish(lines[-1]) and not _blankish(chunk[0]):
                        lines.append(blank if rec is None else rec.gap(blank, parent_id, prefix))
                    elif _blankish(lines[-1]) and _blankish(chunk[0]):
                        if rec is not None:
                            chunk[0].merged_into = lines[-1]
                        chunk = chunk[1:]
                        if not chunk:
                            continue
            lines.extend(chunk)
            prev_list = t if is_list else None
        return lines

    def _sep(self, s):
        """Structural separator line emitted by the block being rendered (between its own lines and its
        children).  Identity function unless block_lines are being recorded."""
        return s if self._rec is None else self._rec.sep(s)

    def _text_lines(self, text, prefix, first_prefix=None, escape_start=True):
        out = []
        for i, ln in enumerate(text.split("\n")):
            ln2 = esc_line_start(ln) if escape_start else ln
            out.append(((first_prefix if (i == 0 and first_prefix is not None) else prefix) + ln2).rstrip()
                       if ln2 else (first_prefix if (i == 0 and first_prefix is not None) else prefix).rstrip())
        return out

    def _children(self, b, ts, prefix):
        if not b.get("has_children"):
            return []
        sub = self.render_blocks(b["id"], ts, prefix)
        return sub

    def render_block(self, b, ts, prefix):
        t = b.get("type")
        v = b.get(t) or {}
        caption = self.rt(v.get("caption"), ts) if isinstance(v, dict) else ""

        if t == "paragraph":
            rich = v.get("rich_text") or []
            if (len(rich) == 1 and rich[0].get("type") == "text" and (rich[0].get("annotations") or {}).get("code")
                    and "\n" in rich[0].get("plain_text", "") and not rich[0].get("href")):
                code = rich[0]["plain_text"]
                ticks = max([len(x) for x in re.findall(r"`{3,}", code)] + [2]) + 1
                lines = [prefix + "`" * ticks] + [(prefix + ln) if ln else prefix.rstrip() for ln in code.split("\n")] + [prefix + "`" * ticks]
                self.report["layout"]["multiline_inline_code_as_fence"] += 1
            else:
                txt = self.rt(rich, ts)
                if not txt.strip():
                    self.report["blocks"]["blank_paragraph"] += 1
                    self._blank_count += 1
                    lines = [prefix.rstrip()]
                else:
                    lines = self._text_lines(txt, prefix)
            kids = self._children(b, ts, prefix)
            if kids:
                self.report["layout"]["paragraph_children_flattened"] += 1
                lines += [self._sep(prefix.rstrip())] + kids + [self._sep(prefix.rstrip()), self._sep(prefix + "<!-- -->")]
            return lines
        if t in ("heading_1", "heading_2", "heading_3", "heading_4"):
            lvl = int(t[-1])
            txt = self.rt(v.get("rich_text"), ts).replace("\n", " ")
            if not txt.strip():
                self.report["layout"]["empty_heading"] += 1
                lines = [prefix.rstrip()]
            else:
                lines = [f"{prefix}{'#' * lvl} {txt}".rstrip()]
            kids = self._children(b, ts, prefix)
            if kids:
                lines += [self._sep(prefix.rstrip())] + kids
            return lines
        if t in ("bulleted_list_item", "numbered_list_item", "to_do"):
            if t == "numbered_list_item":
                # explicit sequence numbers: Live Preview shows source numbers literally
                marker = f"{self._list_num}. "
            else:
                marker = "- " if t == "bulleted_list_item" else ("- [x] " if v.get("checked") else "- [ ] ")
            txt = self.rt(v.get("rich_text"), ts)
            cont = prefix + " " * max(4, len(marker))
            lines = []
            for i, ln in enumerate(txt.split("\n")):
                if i == 0:
                    first = esc_line_start(ln)
                    if _THEMATIC.match((marker + first).strip()):
                        first = "\\" + first
                        self.report["layout"]["list_text_thematic_escaped"] += 1
                    lines.append(prefix + marker + first)
                else:
                    lines.append((cont + esc_line_start(ln)) if ln else cont.rstrip())
            if not txt:
                lines = [(prefix + marker).rstrip()]
            kids = self._children(b, ts, cont)
            if kids:
                kid_blocks = self.blocks.get(b["id"], [])
                if kid_blocks and kid_blocks[0].get("type") not in ("bulleted_list_item", "numbered_list_item", "to_do"):
                    lines.append(self._sep(cont.rstrip()))
                lines += kids
            return lines
        if t in ("quote", "callout", "toggle"):
            qp = prefix + "> "
            txt = self.rt(v.get("rich_text"), ts)
            if t == "quote":
                lines = self._text_lines(txt, qp) if txt else [qp.rstrip()]
            else:
                icon = ""
                ic = v.get("icon") or {}
                if ic.get("type") == "emoji":
                    icon = ic.get("emoji", "") + " "
                if t == "toggle":
                    first, *rest = (txt.split("\n") if txt else [""])
                    lines = [f"{qp}[!toggle]- {first}".rstrip()]
                    for ln in rest:
                        lines.append((qp + esc_line_start(ln)).rstrip())
                else:  # callout: Notion shows icon + body; Obsidian title = icon, body below
                    if ic and ic.get("type") != "emoji":
                        self.report["layout"]["callout_non_emoji_icon_dropped"] += 1
                    lines = [f"{qp}[!note] {icon}".rstrip()]
                    if txt:
                        lines += self._text_lines(txt, qp)
            kids = self._children(b, ts, qp)
            if kids:
                lines += [self._sep(qp.rstrip())] + kids
            return lines
        if t == "code":
            code = plain(v.get("rich_text"))
            lang = (v.get("language") or "").lower()
            lang = LANG_MAP.get(lang, lang).replace(" ", "")
            ticks = max([len(x) for x in re.findall(r"`{3,}", code)] + [2]) + 1
            fence = "`" * ticks
            lines = [prefix + fence + lang]
            for ln in code.split("\n"):
                lines.append((prefix + ln) if ln else prefix.rstrip())
            lines.append(prefix + fence)
            if caption.strip():
                lines += [prefix.rstrip(), prefix + "*" + caption.strip() + "*"]
            return lines
        if t == "equation":
            expr = v.get("expression", "")
            return [prefix + "$$"] + [prefix + ln for ln in expr.split("\n")] + [prefix + "$$"]
        if t == "divider":
            return [prefix + "---"]
        if t == "table":
            kids_all = self.blocks.get(b["id"], [])
            rows = [r for r in kids_all if r.get("type") == "table_row"]
            self.report["blocks"]["table_row"] += len(rows)
            if not rows:
                return [prefix + "%% notion:empty-table %%"]
            width = max(v.get("table_width") or 0, max(len(r["table_row"]["cells"]) for r in rows))
            if not v.get("has_column_header"):
                self.report["layout"]["table_without_header_row"] += 1
            rec = self._rec
            ords = {id(r): j for j, r in enumerate(kids_all)} if rec is not None else None
            out = []
            for i, r in enumerate(rows):
                cells = [self.rt(c, ts, table=True) for c in r["table_row"]["cells"]]
                cells += [""] * (width - len(cells))
                row_line = prefix + "| " + " | ".join(c if c else " " for c in cells) + " |"
                # recording: each row line is the table_row's own line; the delimiter is the table's own line
                out.append(row_line if rec is None else rec.leaf(r, b["id"], ords[id(r)], prefix, row_line))
                if i == 0:
                    out.append(prefix + "|" + "|".join([" --- "] * width) + "|")
            return out
        if t in FILE_BLOCKS:
            kind = v.get("type")
            name = v.get("name") or ""
            if kind == "file":
                rel = self.attachment(ts, f"{b['id']}:{t}", name or None)
                if rel:
                    line = f"![[{rel}]]" if t in EMBEDDABLE else f"[[{rel}|{wikilink_text(name or os.path.basename(rel))}]]"
                    self.report["attachments"][f"ref_{t}"] += 1
                else:
                    line = f"> [!warning] 附件未能下载：{esc_inline(name or t)}（Notion 块 {b['id']}）"
            elif kind == "external":
                url = (v.get("external") or {}).get("url", "")
                if not url.strip():
                    line = f"%% notion:{t} 外部链接为空 block {b['id']} %%"
                else:
                    line = f"![]({url_for_md(url)})" if t in ("image", "video") else f"[{esc_inline(name or url)}]({url_for_md(url)})"
                self.report["attachments"][f"external_{t}"] += 1
            else:
                line = f"%% notion:{t} type={kind} block {b['id']} %%"
                self.report["attachments"][f"other_{kind}"] += 1
            lines = [prefix + line]
            if caption.strip():
                lines.append(prefix + caption.strip())
            return lines
        if t in ("bookmark", "embed", "link_preview"):
            url = v.get("url", "")
            if NOTION_HOSTED.match(url or ""):
                key = f"{b['id']}:embed"
                rel = self.attachment(ts, key, url.split("?")[0].rsplit("/", 1)[-1])
                if rel:
                    self.report["attachments"]["ref_embed_file"] += 1
                    return [prefix + f"[[{rel}|{wikilink_text(os.path.basename(rel))}]]"] + ([prefix + caption.strip()] if caption.strip() else [])
                return [prefix + f"> [!warning] Notion 托管文件未能下载（{t}，Notion 块 {b['id']}）"]
            label = caption.strip() or esc_inline(url)
            return [prefix + self.linkify(label, caption or url, url, ts)] if url else [prefix + f"%% notion:{t} empty %%"]
        if t == "child_page":
            tgt = self.link_target(b["id"])
            title = (v or {}).get("title", "")
            if tgt:
                return [prefix + f"[[{tgt}|{wikilink_text(title or self.nodes[b['id']]['title'])}]]"]
            return [prefix + f"%% notion:child_page 未导出 {b['id']} {esc_inline(title)} %%"]
        if t == "child_database":
            n = self.nodes.get(b["id"])
            if n:
                return [prefix + f"![[{n['base']}]]", prefix.rstrip(), prefix + f"[[{self.link_target(b['id'])}|{wikilink_text(n['title'])}]]"]
            return [prefix + f"%% notion:child_database 未导出 {b['id']} {esc_inline(v.get('title', ''))} %%"]
        if t == "link_to_page":
            lt = v.get("type")
            tid = norm_id(v.get(lt)) if lt in ("page_id", "database_id") else None
            tgt = self.link_target(tid)
            if tgt:
                self.report["links"]["link_to_page_rewritten"] += 1
                return [prefix + f"[[{tgt}|{wikilink_text(self.nodes[tid]['title'])}]]"]
            self.lists["link_out_of_scope"].append({"link_to_page": tid})
            return [prefix + f"[Notion 页面](https://app.notion.com/p/{nid32(tid) if tid else ''})"]
        if t == "synced_block":
            kids = self.render_blocks(b["id"], ts, prefix)
            src = ((v or {}).get("synced_from") or {}).get("block_id")
            if not kids and src and src not in self._synced_stack:
                self._synced_stack.add(src)
                kids = self.render_blocks(src, ts, prefix)
                self._synced_stack.discard(src)
                if kids:
                    self.report["layout"]["synced_reference_resolved_from_source"] += 1
            if not kids and not src and not b.get("has_children"):
                # an original synced block that is simply empty in Notion (verified live in acceptance round 2)
                self.report["layout"]["synced_block_empty"] += 1
                return [prefix + "%% notion:synced_block 空 %%"]
            if not kids:
                self.report["layout"]["synced_block_unreadable"] += 1
                return [prefix + f"> [!warning] 同步块内容无法读取（源块 {src or b['id']} 不在快照中）"]
            return kids
        if t == "column_list":
            out = []
            rec = self._rec
            for i, col in enumerate(self.blocks.get(b["id"], [])):
                self.report["blocks"]["column"] += 1
                if i:
                    out += [self._sep(prefix.rstrip()), self._sep(prefix + "%% column %%"), self._sep(prefix.rstrip())]
                if rec is None:
                    out += self.render_blocks(col["id"], ts, prefix)
                else:  # columns never reach render_block: record them as containers without own lines
                    ck = rec.enter(col, b["id"], i, prefix)
                    try:
                        sub = self.render_blocks(col["id"], ts, prefix)
                    finally:
                        rec.leave()
                    if not sub:
                        rec.set_anchor(ck, out[-1] if out else None)
                    out += sub
            return out or [prefix + "%% notion:column_list empty %%"]
        if t in ("table_of_contents", "breadcrumb"):
            return [prefix + f"%% notion:{t} %%"]
        # template / unsupported / unknown
        self.report["unsupported"][t if t != "unsupported" else f"unsupported:{(v or {}).get('block_type')}"] += 1
        lines = [prefix + f"%% notion:unsupported {t if t != 'unsupported' else (v or {}).get('block_type')} {b['id']} %%"]
        kids = self._children(b, ts, prefix)
        return lines + ([self._sep(prefix.rstrip())] + kids if kids else [])

    # ------------------------------------------------------------ properties
    def prop_value(self, p, ts):
        t = p.get("type")
        v = p.get(t)
        if t in ("title",):
            return None
        if t == "rich_text":
            return plain(v)
        if t in ("number", "checkbox", "url", "email", "phone_number", "created_time", "last_edited_time"):
            return v
        if t in ("select", "status"):
            return (v or {}).get("name")
        if t == "multi_select":
            return [x.get("name") for x in v or []]
        if t == "date":
            return (v or {}).get("start") if v else None
        if t == "people":
            return [x.get("name") or self.users.get(x.get("id")) or x.get("id") for x in v or []]
        if t in ("created_by", "last_edited_by"):
            return (v or {}).get("name") or self.users.get((v or {}).get("id")) or (v or {}).get("id")
        if t == "files":
            out = []
            for i, f in enumerate(v or []):
                if f.get("type") == "external":
                    out.append((f.get("external") or {}).get("url"))
            return out
        if t == "relation":
            out = []
            for x in v or []:
                tid = norm_id(x.get("id"))
                tgt = self.link_target(tid)
                out.append(f"[[{tgt}|{wikilink_text(self.nodes[tid]['title'])}]]" if tgt else f"https://app.notion.com/p/{nid32(tid) if tid else ''}")
            return out
        if t == "formula":
            ft = (v or {}).get("type")
            fv = (v or {}).get(ft)
            return fv.get("start") if isinstance(fv, dict) else fv
        if t == "rollup":
            rt_ = (v or {}).get("type")
            rv = (v or {}).get(rt_)
            if rt_ == "array":
                return [self.prop_value(x, ts) for x in rv or []]
            return rv.get("start") if isinstance(rv, dict) else rv
        if t == "unique_id":
            return f"{v.get('prefix')}-{v.get('number')}" if v and v.get("prefix") else (v or {}).get("number")
        if t == "verification":
            return (v or {}).get("state")
        if t == "button":
            return None
        self.report["properties"][f"other_{t}"] += 1
        return json.dumps(v, ensure_ascii=False) if v is not None else None

    def row_properties(self, node):
        props = {}
        data = node["data"]
        for name, p in (data.get("properties") or {}).items():
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            self.report["properties"][t] += 1
            if t == "files":
                files = []
                for i, f in enumerate(p.get("files") or []):
                    if f.get("type") == "file":
                        rel = self.attachment(node["ts"], f"{node['id']}:prop:{name}:{i}", f.get("name"))
                        files.append(f"[[{rel}]]" if rel else f"(未下载) {f.get('name')}")
                    elif f.get("type") == "external":
                        files.append((f.get("external") or {}).get("url"))
                val = files
            else:
                val = self.prop_value(p, node["ts"])
            if t == "title":
                continue
            key = name
            if key.startswith("notion_") or key.lower() in RESERVED_FM:
                key = "prop_" + key
            props[key] = val
            if t == "date" and p.get("date") and p["date"].get("end"):
                props[f"{key}（结束）"] = p["date"]["end"]
        return props

    # ------------------------------------------------------------ notes
    def icon_cover(self, node, field):
        d = node["data"].get(field) or {}
        if not d:
            return None
        if d.get("type") == "emoji":
            return d.get("emoji")
        if d.get("type") == "external":
            return (d.get("external") or {}).get("url")
        if d.get("type") == "file":
            rel = self.attachment(node["ts"], f"{node['id']}:{field}", None)
            return f"attachment:{rel}" if rel else None
        if d.get("type") == "custom_emoji":
            return (d.get("custom_emoji") or {}).get("name")
        return None

    def frontmatter(self, node, kind):
        d = node["data"]
        fm = {"notion_id": node["id"], "notion_kind": kind,
              "notion_title": node["title"],
              "notion_url": f"https://app.notion.com/p/{nid32(node['id'])}",
              "notion_parent_id": node["parent"], "notion_teamspace": node["ts"], "notion_order": node["order"],
              "notion_created_time": d.get("created_time"), "notion_last_edited_time": d.get("last_edited_time")}
        ic = self.icon_cover(node, "icon")
        if ic:
            fm["notion_icon"] = ic
        cv = self.icon_cover(node, "cover")
        if cv:
            fm["notion_cover"] = cv
        # aliases follow the title-derived name even when a local name override is in effect (§7.4.3)
        if (node.get("natural_name") or node["name"]) != node["title"] and node["title"].strip():
            fm["aliases"] = [node["title"]]
        if kind in ("row", "template"):
            fm.update(self.row_properties(node))
        return fm

    def layout_note(self, fm, body_lines):
        """The only place whole-note text is assembled.  Returns (text, first_body_line, kept, lead, trail):
        kept = body elements actually written (file line first_body_line onward), lead/trail = popped ones."""
        y = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=100000, default_flow_style=False)
        lines = list(body_lines)
        lead, trail = [], []
        while lines and _blankish(lines[0]) and not lines[0].strip():
            lead.append(lines.pop(0))
        while lines and not lines[-1].strip():
            trail.append(lines.pop())
        body = "\n".join(lines)
        text = "---\n" + y + "---\n" + ("\n" + body + "\n" if body.strip() else "")
        return text, y.count("\n") + 4, lines, lead, trail

    def dump_note(self, fm, body_lines):
        return self.layout_note(fm, body_lines)[0]

    def page_note(self, node):
        return self.render_page(node)[0]

    def render_page(self, node_or_id, blocks=None, record=False, sandbox=False):
        """Render one note.  Returns (text, block_lines rows | None).

        blocks:  overlay {parent_id: [block_json, ...]} applied over the snapshot children (ChainMap);
        record:  also return block_lines rows (base_sha = sha256 of the returned text);
        sandbox: no side effects: counters/lists swapped for throwaway ones, attachments lookup-only from the
                 run's map (an attachment the run did not resolve raises RenderRefused).
        A database node renders its database note and returns rows None.
        Overlay blocks may lack an "id" (blocks to be created): allowed for childless R types (WB §4.3); their
        block_lines rows have block_id null and owner = the enclosing block's owner (or the page)."""
        node = node_or_id if isinstance(node_or_id, dict) else self.nodes[norm_id(node_or_id) or node_or_id]
        if blocks:
            self._check_overlay(blocks)
        saved_blocks = self.blocks
        saved_state = (self.report, self.lists, self._sandbox, self._rec, self._list_num, self._blank_count,
                       self._synced_stack)
        if blocks:
            self.blocks = ChainMap(dict(blocks), saved_blocks)
        if sandbox:
            self.report, self.lists, self._sandbox = defaultdict(Counter), defaultdict(list), True
        try:
            if node["kind"] == "database":
                self._rec = None
                return self.database_note(node), None
            kind = node["kind"] if node["kind"] in ("row", "template") else "page"
            node["note_kind"] = kind
            self._blank_count = 0
            self._synced_stack = set()
            rec = self._rec = _Recorder(self, node["id"]) if record else None
            body = self.render_blocks(node["id"], node["ts"])
            fm = self.frontmatter(node, kind)
            if self._blank_count:
                fm["notion_blank_blocks"] = self._blank_count
            text, first, kept, lead, trail = self.layout_note(fm, body)
            rows = None
            if rec is not None:
                self._rec = None
                rows = rec.finalize(kept, lead, trail, first, sha256_bytes(text.encode("utf-8")))
            return text, rows
        finally:
            self.blocks = saved_blocks
            (self.report, self.lists, self._sandbox, self._rec, self._list_num, self._blank_count,
             self._synced_stack) = saved_state

    @staticmethod
    def _check_overlay(blocks):
        """RenderRefused for overlay blocks the renderer cannot place: not a typed dict, or without an id while
        the renderer needs one (children are looked up by parent id; non-R types reference their id)."""
        for parent, lst in blocks.items():
            for b in lst:
                if not isinstance(b, dict) or not isinstance(b.get("type"), str):
                    raise RenderRefused(f"overlay block under {parent} is not a typed block object")
                if not b.get("id") and (b.get("has_children") or b["type"] not in R_TYPES):
                    raise RenderRefused(f"overlay block of type {b['type']} under {parent} needs an id"
                                        + (" (it has children)" if b.get("has_children") else ""))

    # ------------------------------------------------------------ block classes / hashes (write-back)
    def block_canon(self, b):
        """wb_norm.canon_block(b); cached per note during run() (blocks are never mutated there)."""
        cache = self._canon_cache
        if cache is None:
            return canon_block(b)
        hit = cache.get(id(b))
        if hit is None or hit[0] is not b:
            hit = cache[id(b)] = (b, canon_block(b))
        return hit[1]

    def block_sha(self, b):
        """block_lines.json_sha = wb_norm.block_json_sha(block)."""
        return sha256_bytes(self.block_canon(b).encode("utf-8"))

    @staticmethod
    def _is_notion_url(url):
        if extract_notion_id(url):
            return True
        if url.startswith("/"):
            path = url.split("?")[0].split("#")[0]
            return bool(HEX32.findall(path) and re.fullmatch(r"/(?:p/)?(?:[^/]*-)?[0-9a-fA-F]{32}", path))
        return False

    def _rich_reason(self, rich):
        for r in rich or []:
            if not isinstance(r, dict):
                continue
            if (r.get("annotations") or {}).get("color") not in (None, "default"):
                return "color"
            if r.get("type") == "mention":
                return "mention_" + str((r.get("mention") or {}).get("type"))
            url = ((r.get("text") or {}).get("link") or {}).get("url") or r.get("href")
            if url and self._is_notion_url(url):
                return "notion_link"          # incl. Notion URLs with a #block fragment
            if url and url.strip().startswith("#"):
                return "anchor_link"          # contract §2: '#anchor' links are read-only (same test as md2blocks)
        return None

    def structural_klass(self, b, page_id, owner_page_id=None, in_synced=False, duplicate=False):
        """('X', reason) when a structural rule makes the block read-only, else ('R?', None).
        Rules: WRITEBACK_SPEC §4.3 X list, D-11 (whole synced subtree), D-30/D-31 (captions), contract §2
        inline policy (mentions, coloured segments, links to Notion pages, '#anchor' links).  The planner confirms
        'R?' with the md2blocks round trip (D-12).  block_lines rows add one layout rule on top of this:
        'own_line_lost' (see _Recorder.finalize)."""
        t = b.get("type")
        v = b.get(t) if isinstance(b.get(t), dict) else {}
        if in_synced or duplicate or t == "synced_block":
            return "X", "synced"
        if owner_page_id is not None and owner_page_id != page_id:
            return "X", "owner"
        if t not in R_TYPES:
            return "X", f"type:{t}"
        if t.startswith("heading_") and v.get("is_toggleable") and b.get("has_children"):
            return "X", "toggleable_heading"
        if plain(v.get("caption")).strip():
            return "X", "caption"
        if t == "table_row":
            reason = None
            for cell in v.get("cells") or []:
                reason = self._rich_reason(cell)
                if reason:
                    break
        else:
            rich = v.get("rich_text") or []
            reason = self._rich_reason(rich)
            if not reason and t == "paragraph" and (
                    len(rich) == 1 and rich[0].get("type") == "text" and (rich[0].get("annotations") or {}).get("code")
                    and "\n" in rich[0].get("plain_text", "") and not rich[0].get("href")):
                reason = "code_fence_paragraph"
        return ("X", reason) if reason else ("R?", None)

    def note_page_json_sha(self, node):
        """page_json_sha (D-10) of a node from the snapshot (databases: object fields only, no blocks)."""
        if node["kind"] == "database":
            return page_json_sha(node["data"], [])
        # == wb_norm.page_json_sha(node["data"], page_blocks_dfs(...)); the DFS already excludes synced children
        return page_json_sha_canon(node["data"], [self.block_canon(b) for b in page_blocks_dfs(self.blocks.get, node["id"])])

    def schema_of(self, node):
        db = node["data"]
        props = []
        for ref in db.get("data_sources", []):
            ds = self.get(ref["id"], "data_source") or {}
            for name, p in (ds.get("properties") or {}).items():
                t = p.get("type")
                opts = ""
                if t in ("select", "multi_select", "status"):
                    opts = "、".join(o.get("name", "") for o in (p.get(t) or {}).get("options", []))
                elif t == "relation":
                    opts = "→ " + str((p.get("relation") or {}).get("data_source_id") or (p.get("relation") or {}).get("database_id"))
                elif t == "formula":
                    opts = (p.get("formula") or {}).get("expression", "")[:200]
                props.append((name, t, opts))
        return props

    def database_note(self, node):
        db = node["data"]
        fm = self.frontmatter(node, "database")
        fm["notion_data_sources"] = [r["id"] for r in db.get("data_sources", [])]
        lines = []
        desc = self.rt(db.get("description"), node["ts"])
        if desc.strip():
            lines += self._text_lines(desc, "") + [""]
        lines += [f"![[{node['base']}]]", "", "## 记录（按 Notion 查询顺序）", ""]
        for i, cid in enumerate(node["children"], 1):
            c = self.nodes[cid]
            lines.append(f"{i}. [[{self.link_target(cid)}|{wikilink_text(c['title'] or 'Untitled')}]]")
        if not node["children"]:
            lines.append("（无记录）")
        if node.get("templates"):
            lines += ["", "## 模板（Notion 数据库页面模板，不计入记录）", ""]
            for i, tid in enumerate(node["templates"], 1):
                t = self.nodes[tid]
                lines.append(f"{i}. [[{self.link_target(tid)}|{wikilink_text(t['title'] or 'Untitled')}]]")
        lines += ["", "## 属性定义（静态快照）", "", "| 属性 | 类型 | 选项 / 定义 |", "| --- | --- | --- |"]
        for name, t, opts in self.schema_of(node):
            lines.append(f"| {esc_inline(name, True)} | {t} | {esc_inline(opts, True) or ' '} |")
        views = db.get("_views")
        lines += ["", "## Notion 视图（只读记录，Obsidian 中不能原样复现）", ""]
        if isinstance(views, list) and views:
            for v in views:
                lines.append(f"- {esc_inline(v.get('name') or v.get('id') or '视图')}（{v.get('type', '?')}）")
        else:
            lines.append(f"- 未取得视图信息（{db.get('_views_error') or 'API 未返回'}）")
        return self.dump_note(fm, lines)

    def base_file(self, node):
        # verified against Obsidian 1.15.1 (docs/bases.md): property IDs in order/sort/properties are
        # `note.<raw name>` (no quoting); filter expressions use JSON-escaped string literals.
        cols, props = ["file.name"], {"file.name": {"displayName": "名称"}}
        seen = set()
        # SPEC v1.2: Obsidian's own serialisation (bare names in order/sort, indented sequences)
        for name, t, _ in self.schema_of(node):
            if t in ("title", "button"):
                continue
            key = name if not (name.startswith("notion_") or name.lower() in RESERVED_FM) else "prop_" + name
            if key in seen:
                continue
            seen.add(key)
            cols.append(key)
        cols.append("notion_order")
        props["note.notion_order"] = {"displayName": "Notion 顺序"}
        doc = {"filters": {"and": [f"file.inFolder({json.dumps(unicodedata.normalize('NFC', node['folder']), ensure_ascii=False)})",
                                   'file.ext == "md"',
                                   'note.notion_kind == "row"',
                                   f'note.notion_parent_id == "{node["id"]}"']},
               "properties": props,
               "views": [{"type": "table", "name": "Notion 顺序", "order": cols,
                          "sort": [{"property": "notion_order", "direction": "ASC"}]}]}
        return yaml.dump(doc, Dumper=_IndentDumper, allow_unicode=True, sort_keys=False, width=100000)

    # ------------------------------------------------------------ output
    def write(self, rel, content, kind, oid=None):
        path = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(path, "wb") as fh:
            fh.write(data)
        self.manifest[rel] = {"sha256": sha256_bytes(data), "size": len(data), "kind": kind, "notion_id": oid}

    def run(self):
        if self._planning:
            raise RuntimeError("a planning exporter (Exporter.for_planning) never writes; use a normal Exporter")
        if os.path.exists(self.vault):
            raise SystemExit(f"ABORT: staging dir already exists: {self.vault}")
        os.makedirs(self.vault)
        self.manifest = {}
        self.attachments = {}
        self.att_by_sha = {}
        self.att_map = {}
        self.build_tree()
        self.assign_paths()
        self._check_overrides_in_tree()
        for ts in TEAMSPACES:
            os.makedirs(os.path.join(self.vault, ts), exist_ok=True)
        order = self.run_order()
        # side outputs (D-09): outside vault/, not in manifest.json; written to *.tmp and renamed at the end
        bl_path = os.path.join(self.out, "block_lines.jsonl")
        pj_path = os.path.join(self.out, "page_json.jsonl")
        att_path = os.path.join(self.out, "attachments.json")
        for p in (bl_path, pj_path, att_path):
            if os.path.exists(p):
                os.remove(p)  # never leave a previous attempt's side output next to this run's vault
        bl = open(bl_path + ".tmp", "w", encoding="utf-8") if self.record_block_lines else None
        page_rows = []
        self._canon_cache = {}
        try:
            for oid in order:
                self._canon_cache.clear()
                n = self.nodes[oid]
                if n["kind"] == "database":
                    self.write(n["note"], self.database_note(n), "database", oid)
                    self.write(n["base"], self.base_file(n), "base", oid)
                else:
                    text, rows = self.render_page(n, record=self.record_block_lines)
                    self.write(n["note"], text, n.get("note_kind") or "page", oid)
                    if bl is not None:
                        sha = self.manifest[n["note"]]["sha256"]
                        for r in rows:
                            if r["base_sha"] != sha:  # cannot happen: rows are hashed from the same text
                                raise RuntimeError(f"block_lines base_sha mismatch for {n['note']}")
                            bl.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
                page_rows.append({"notion_id": oid, "kind": self.manifest[n["note"]]["kind"], "rel_path": n["note"],
                                  "sha256": self.manifest[n["note"]]["sha256"],
                                  "page_json_sha": self.note_page_json_sha(n), "renderer_version": RENDERER_VERSION})
        finally:
            self._canon_cache = None
            if bl is not None:
                bl.close()
        for rel, src in sorted(self.attachments.items()):
            with open(src, "rb") as fh:
                data = fh.read()
            self.write(rel, data, "attachment")
        self.write_task_dir(order)
        self.save_state(order)
        with open(pj_path + ".tmp", "w", encoding="utf-8") as fh:
            for r in page_rows:
                fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        with open(att_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.att_map, fh, ensure_ascii=False, indent=0, sort_keys=True)
        if bl is not None:
            os.replace(bl_path + ".tmp", bl_path)
        os.replace(pj_path + ".tmp", pj_path)
        os.replace(att_path + ".tmp", att_path)
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(self.manifest, fh, ensure_ascii=False, indent=0, sort_keys=True)
        rep = {k: dict(v) for k, v in self.report.items()}
        if self.record_block_lines:
            rep.setdefault("block_lines", {})  # also record a successful empty mapping in a database-only export
        rep["counts"] = dict(Counter(n["kind"] for n in self.nodes.values()))
        rep["counts"]["notes_total"] = sum(1 for m in self.manifest.values() if m["kind"] in ("page", "row", "database", "template"))
        rep["counts"]["attachments"] = sum(1 for m in self.manifest.values() if m["kind"] == "attachment")
        rep["lists"] = {k: v[:500] for k, v in self.lists.items()}
        rep["list_sizes"] = {k: len(v) for k, v in self.lists.items()}
        rep["renderer_version"] = RENDERER_VERSION
        rep["side_outputs"] = (["block_lines.jsonl"] if self.record_block_lines else []) + ["page_json.jsonl", "attachments.json"]
        if self.overrides or self.override_rejected:
            rep["overrides"] = {"given": self.overrides_given, "applied": self.override_applied,
                                "rejected": self.override_rejected}
        with open(os.path.join(self.out, "export_report.json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
        return rep

    # ------------------------------------------------------------ task dir / ordering
    def write_task_dir(self, order):
        from sortspec import build_sortspec  # local module (custom-sort syntax)
        lines = ["# Godcreate 目录索引", "",
                 "按 Notion 侧栏顺序（团队协作区、根页面）与正文/查询顺序（子页面、数据库记录）生成。由导出器生成（notion_kind: generated），请勿手改。", ""]
        for ts in TEAMSPACES:
            lines.append(f"## {ts}")
            lines.append("")

            def walk(oid, depth):
                n = self.nodes[oid]
                tag = "（数据库）" if n["kind"] == "database" else ""
                lines.append("    " * depth + f"- [[{self.link_target(oid)}|{wikilink_text(n['title'] or 'Untitled')}]]{tag}")
                for c in n["children"]:
                    walk(c, depth + 1)
                for t in n.get("templates", []):
                    tn = self.nodes[t]
                    lines.append("    " * (depth + 1) + f"- [[{self.link_target(t)}|{wikilink_text(tn['title'] or 'Untitled')}]]（模板）")
            for r in self.ts_roots[ts]:
                if r in self.nodes:
                    walk(r, 0)
            lines.append("")
        fm = {"notion_kind": "generated", "generated_by": "export_obsidian.py"}
        self.write(f"{TASK_DIR}/Godcreate 目录索引.md", self.dump_note(fm, lines), "generated")
        # mapping list
        mlines = ["# Notion ID ↔ 库内路径映射清单", "", "| # | Notion ID | 类型 | 团队协作区 | 库内路径 |", "| --- | --- | --- | --- | --- |"]
        for i, oid in enumerate(order, 1):
            n = self.nodes[oid]
            mlines.append(f"| {i} | `{oid}` | {n['kind']} | {n['ts']} | `{self.link_note(oid)}` |")
        self.write(f"{TASK_DIR}/映射清单.md", self.dump_note(fm, mlines), "generated")
        # sortspec (+ the Notion_Obsidian sections and the new README only past the byte-frozen 1.2.1)
        spec_text = build_sortspec(self, task_sections=not renderer_bytes_frozen())
        self.write(f"{TASK_DIR}/sortspec.md", spec_text, "generated")
        self.write(f"{TASK_DIR}/README.md", self.dump_note(fm, readme_lines()), "generated")

    def folder_children(self, folder):
        """Ordered child entries of a vault folder: list of (name, is_folder, ext)."""
        if folder == "":
            return [(ts, True, None) for ts in TEAMSPACES] + [(TASK_DIR, True, None)]
        if folder in TEAMSPACES:
            ids = self.ts_roots[folder]
            owner = None
        elif folder in self.tpl_folder_owner:
            ids = self.nodes[self.tpl_folder_owner[folder]].get("templates", [])
            owner = None
        else:
            owner = self.folder_owner.get(folder)
            ids = self.nodes[owner]["children"] if owner else []
        entries = []
        if owner:
            n = self.nodes[owner]
            entries.append((n["name"], False, "md"))
            if n["kind"] == "database":
                entries.append((n["name"], False, "base"))
        for oid in ids:
            n = self.nodes.get(oid)
            if not n:
                continue
            entries.append((n["name"], bool(n.get("folder")), None if n.get("folder") else "md"))
        return entries

    # ------------------------------------------------------------ state
    def save_state(self, order):
        cur = self.state.cursor()
        # R-035: an object trashed through write-back keeps that status even if it reappears in a snapshot
        trashed_wb = {r[0] for r in cur.execute("SELECT notion_id FROM objects_map WHERE status='trashed_via_writeback'")}
        for oid in order:
            n = self.nodes[oid]
            k = n["kind"]
            sha = self.manifest[n["note"]]["sha256"]
            if oid in trashed_wb:
                self.lists["trashed_via_writeback_reappeared"].append(oid)
            # D-22: rel_path = the actual (override) path, name = the title-derived name
            cur.execute("""INSERT INTO objects_map(notion_id,kind,rel_path,name,parent_id,teamspace,ord,title,rendered_sha256,status,first_run,last_run)
                           VALUES(?,?,?,?,?,?,?,?,?, 'active', ?, ?)
                           ON CONFLICT(notion_id) DO UPDATE SET kind=excluded.kind, rel_path=excluded.rel_path, name=excluded.name,
                           parent_id=excluded.parent_id, teamspace=excluded.teamspace, ord=excluded.ord, title=excluded.title,
                           rendered_sha256=excluded.rendered_sha256,
                           status=CASE WHEN objects_map.status='trashed_via_writeback' THEN 'trashed_via_writeback' ELSE 'active' END,
                           last_run=excluded.last_run""",
                        (oid, k, n["note"], n.get("natural_name") or n["name"], n["parent"], n["ts"], n["order"], n["title"],
                         sha, self.run_id, self.run_id))
        live = set(order)
        for (oid,) in cur.execute("SELECT notion_id FROM objects_map WHERE status='active'").fetchall():
            if oid not in live:
                cur.execute("UPDATE objects_map SET status='source_missing', last_run=? WHERE notion_id=?", (self.run_id, oid))
                self.lists["source_missing_since_last_run"].append(oid)
        self.state.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--sidebar", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--state", default=os.path.join(WORK, "state", "export.db"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--immutable", action="store_true", help="require a finished snapshot with no outstanding WAL or rollback-journal data")
    ap.add_argument("--roots-kinds", default=None, help="override <snapshot>.roots.json")
    ap.add_argument("--overrides", default=None,
                    help="JSON {notion_id: local_rel}: same-folder local note names (WRITEBACK_SPEC §7.4); "
                         "others are ignored and listed in export_report.json['overrides']['rejected']")
    ap.add_argument("--link-targets", default=None,
                    help="JSON {notion_id: kept_rel.md}: redirect generated links and indexes while a moved edit is held")
    ap.add_argument("--no-block-lines", action="store_true", help="do not record <out>/block_lines.jsonl")
    a = ap.parse_args()
    overrides = None
    if a.overrides:
        overrides = _read_json(a.overrides)
        if not isinstance(overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()):
            raise SystemExit(f"ABORT: {a.overrides} must be a JSON object {{notion_id: local_rel}}")
    link_targets = None
    if a.link_targets:
        link_targets = _read_json(a.link_targets)
        if not isinstance(link_targets, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                         for k, v in link_targets.items()):
            raise SystemExit(f"ABORT: {a.link_targets} must be a JSON object {{notion_id: kept_rel.md}}")
    out = a.out or os.path.join(WORK, "staging", a.run_id)
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ex = Exporter(a.snapshot, a.sidebar, a.run_id, a.state, out, immutable=a.immutable, roots_kinds=a.roots_kinds,
                  overrides=overrides, link_targets=link_targets, record_block_lines=not a.no_block_lines)
    rep = ex.run()
    print(json.dumps({"counts": rep["counts"], "list_sizes": rep["list_sizes"], "unsupported": rep.get("unsupported", {}),
                      "links": rep.get("links", {}), "attachments": rep.get("attachments", {}),
                      "renderer_version": RENDERER_VERSION, "block_lines": rep.get("block_lines", {}),
                      "overrides": {k: len(v) for k, v in rep.get("overrides", {}).items() if isinstance(v, list)}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
