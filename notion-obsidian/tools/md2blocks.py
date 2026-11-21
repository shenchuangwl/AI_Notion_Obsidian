#!/usr/bin/env python3
"""md2blocks: reverse parser for the Obsidian write-back (WRITEBACK_SPEC v1.1 §4.3, §4.4; contract §5.8).

Turns exporter-rendered Markdown (the lines that `export_obsidian.Exporter.render_block` wrote for a block,
possibly edited by the user) back into Notion block JSON / API request bodies.

Two readings of the same Markdown are computed and must agree:

* the **exporter grammar** (fast path): the exact inverse of `Exporter.rt()` / `esc_inline` / `wrap_marks` /
  `esc_line_start` / `render_block`; a parse is accepted only when re-rendering it with the real exporter gives
  the input back byte for byte (choice points are explored depth-first);
* the **CommonMark reading** (markdown-it-py 3.0.0 + strikethrough + tables + Obsidian `$math$`): what
  Obsidian shows.  Obsidian-only syntax (wikilinks, embeds, ==highlight==, #tags, %%comments%%, footnotes,
  callouts, block ids, foreign HTML …) is refused with `UNSUPPORTED_INLINE:<what>`.

If the user typed the exporter's canonical form, the exporter-grammar parse is returned (`normalized=False`).
If the user typed a CommonMark equivalent (`_x_`, `__b__`, unescaped `a_b`, `* item`, …) the CommonMark reading
is returned with `normalized=True`, but only after checking that (1) its exporter rendering reads back to the
same content in the exporter grammar, (2) that rendering means the same under CommonMark and (3) markdown-it
renders the user's text and the canonical text to identical HTML.  Anything else raises `Refused`.

R/X classification (`klass`) is round-trip based (D-12): static X rules (contract §2 write-back inline policy,
WRITEBACK_SPEC §4.3, D-11/D-24/D-30/D-31), then block JSON -> exporter Markdown -> block JSON must be equal on
the writable fields (canonical form of roundtrip.md §4.6, strict mode) and the exporter-grammar reading must
agree with the CommonMark reading (otherwise what Obsidian shows is not what Notion holds: `md_divergent`).

Rendering never changes the caller's exporter state: `report`, `lists`, `_list_num`, `_blank_count` (and
`blocks` when an overlay is needed) are swapped out for the duration of each call.  Without an exporter a
private renderer built from the same `export_obsidian` module is used (R-class content does not depend on
exporter state: no mentions, no links to Notion pages).

No I/O, no network.  Public API (contract §5.8, plus helpers):
    Refused, canon_rich_text, writable_fields, klass, parse_inline, rebuild_block, parse_new_blocks,
    split_for_api, static_reasons, canon_writable, render_own_lines, apply_update, snapshot_blocks,
    request_rich, block_count, nesting_depth, chunk_children, check_children_request
Refusal reasons: UNSUPPORTED_INLINE:<what>, UNSUPPORTED_BLOCK:<what>, UNSUPPORTED_REGION, TYPE_CHANGED,
STRUCTURE_CHANGED, REPARSE_MISMATCH, LIMIT:<what> (each with a Chinese hint for the preview).
"""
import contextlib
import copy
import json
import os
import re
import sys
from collections import ChainMap, Counter, defaultdict

try:
    import export_obsidian as EO
except ImportError:  # loaded by file path without tools/ on sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import export_obsidian as EO

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

# ---------------------------------------------------------------------------------------------- constants
RENDER_TS = "General"  # teamspace argument for rendering: only attachments use it (never R-class content)
ANN_KEYS = ("bold", "italic", "strikethrough", "underline", "code")
HEADINGS = ("heading_1", "heading_2", "heading_3", "heading_4")
LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do")
TEXT_TYPES = ("paragraph",) + HEADINGS + LIST_TYPES + ("quote", "callout")
R_TYPES = frozenset(TEXT_TYPES + ("code", "equation", "divider", "table_row"))
NEW_TYPES = frozenset(("paragraph", "heading_1", "heading_2", "heading_3") + LIST_TYPES
                      + ("quote", "code", "divider", "equation"))

# Notion API request limits (https://developers.notion.com/reference/request-limits, WRITEBACK_SPEC §4.4.3)
MAX_TEXT = 2000          # text.content and link url, UTF-16 code units
MAX_EQUATION = 1000      # equation.expression
MAX_SEGMENTS = 100       # rich_text array length
MAX_CHILDREN = 100       # children per append request
MAX_NEST = 2             # nesting levels below the top-level blocks of one request
MAX_ELEMENTS = 1000      # block elements per request
MAX_BYTES = 500_000      # request body size

# Notion API code.language enum
NOTION_LANGUAGES = frozenset("""abap|abc|agda|arduino|ascii art|assembly|bash|basic|bnf|c|c#|c++|clojure|coffeescript|coq
|css|dart|dhall|diff|docker|ebnf|elixir|elm|erlang|f#|flow|fortran|gherkin|glsl|go|graphql|groovy|haskell|hcl|html|idris
|java|javascript|json|julia|kotlin|latex|less|lisp|livescript|llvm ir|lua|makefile|markdown|markup|matlab|mathematica
|mermaid|nix|notion formula|objective-c|ocaml|pascal|perl|php|plain text|powershell|prolog|protobuf|purescript|python|r
|racket|reason|ruby|rust|sass|scala|scheme|scss|shell|smalltalk|solidity|sql|swift|toml|typescript|vb.net|verilog|vhdl
|visual basic|webassembly|xml|yaml|java/c/c++/c#""".replace("\n", "").split("|"))

_LANG_MAP = getattr(EO, "LANG_MAP", {"plain text": "", "c++": "cpp", "c#": "csharp", "f#": "fsharp",
                                     "objective-c": "objectivec", "vb.net": "vbnet", "java/c/c++/c#": "java"})


def _md_lang(lang):
    """Forward mapping of render_block (code fence info string)."""
    lang = (lang or "").lower()
    return _LANG_MAP.get(lang, lang).replace(" ", "")


_LANG_INV = {}
for _l in sorted(NOTION_LANGUAGES):
    _k = _md_lang(_l)
    if _k not in _LANG_INV or _l == _k:
        _LANG_INV[_k] = _l
_LANG_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "shell", "zsh": "shell",
                 "yml": "yaml", "md": "markdown", "rb": "ruby", "rs": "rust", "kt": "kotlin", "golang": "go",
                 "text": "plain text", "txt": "plain text", "plaintext": "plain text", "c++": "c++",
                 "dockerfile": "docker", "tex": "latex", "ps1": "powershell", "proto": "protobuf"}

HINTS = {
    "wikilink": "Notion 中不能新建 [[维基链接]]；请在 Notion 中插入页面提及/链接，或改用 [文字](https://…) 外部链接",
    "embed": "![[嵌入]] 无法写回 Notion；请在 Notion 中插入附件/嵌入",
    "image": "图片 ![](…) 无法写回 Notion；请在 Notion 中插入图片",
    "highlight": "==高亮== 在 Notion 中是颜色，写回不支持；请在 Notion 中设置颜色或去掉 ==",
    "tag": "#标签 是 Obsidian 语法，Notion 中只是文字；如要保留文字请写成 \\#标签",
    "comment": "%%注释%% 是 Obsidian 语法，写回会变成正文；请删除或改写",
    "footnote": "脚注 [^…] / ^[…] 无法写回 Notion",
    "callout": "> [!…] 标注块无法新建/改类型；请在 Notion 中创建标注块",
    "html": "仅支持导出器使用的 <u> <strong> <em> <del>（表格内 <br>）；其他 HTML 无法写回",
    "block_id": "行尾 ^块ID 是 Obsidian 语法，写回会变成正文；请删除",
    "display_math": "行内 $$…$$ 无法写回；请把公式块单独成段（上下各一行 $$）",
    "math_link": "链接文字里不能放公式",
    "link_title": "链接标题 [文字](url \"标题\") 在 Notion 中没有对应字段；请去掉标题",
    "link_scheme": "只支持 http(s):// 与 mailto: 链接（相对链接/其他协议可能被 Notion 拒绝）",
    "notion_link": "指向 Notion 页面的链接在导出中变成 [[维基链接]]，该块写回后会变为只读；请在 Notion 中插入页面链接",
    "anchor_link": "#锚点链接无法写回",
    "empty_link": "空链接文字 [](url) 无法写回",
    "reference_definition": "链接引用定义 [x]: url 无法写回；请改用 [文字](url)",
    "ambiguous": "这段 Markdown 在 Obsidian 与 Notion 导出格式中含义不同（如 URL 中的 * _ $、相邻的 ** 标记、&实体;）；请改写或在 Notion 中修改",
}
HINT_MANUAL = "需在 Notion 中手工调整（块类型/层级/结构变化不会自动删除重建）"
HINT_NORMALIZED = "写回后本地文件会被规范化为 Notion 渲染格式"


class Refused(Exception):
    """A write-back item that must not be executed. `reason` is machine readable, `hint` is for the preview."""

    def __init__(self, reason, hint=""):
        super().__init__(f"{reason}: {hint}" if hint else reason)
        self.reason = reason
        self.hint = hint


def _unsupported(what, extra=""):
    return Refused(f"UNSUPPORTED_INLINE:{what}", HINTS.get(what, "") + (f"（{extra}）" if extra else ""))


# ---------------------------------------------------------------------------------------------- segments
def _ann(src=None, code=None):
    a = {k: bool((src or {}).get(k)) for k in ANN_KEYS}
    if code is not None:
        a["code"] = bool(code)
    a["color"] = "default"
    return a


def _text_seg(content, ann=None, link=None):
    """Render-compatible text segment (plain_text/href included because the exporter reads them)."""
    return {"type": "text", "text": {"content": content, "link": {"url": link} if link else None},
            "annotations": _ann(ann), "plain_text": content, "href": link}


def _eq_seg(expr, ann=None):
    return {"type": "equation", "equation": {"expression": expr}, "annotations": _ann(ann),
            "plain_text": expr, "href": None}


def _seg_link(seg):
    return ((seg.get("text") or {}).get("link") or {}).get("url") or seg.get("href") or None


def _seg_text(seg):
    return (seg.get("text") or {}).get("content", seg.get("plain_text", ""))


def _plain(rich):
    return "".join(_seg_text(s) if s.get("type") == "text" else (s.get("plain_text") or "") for s in rich or [])


def _internal(rich):
    """Request-shaped or API-shaped rich text -> render-compatible segments (adds plain_text/href)."""
    out = []
    for s in rich or []:
        s2 = copy.deepcopy(s)
        if s2.get("type") == "text":
            s2.setdefault("text", {}).setdefault("link", None)
            s2["plain_text"] = s2["text"].get("content", "")
            s2["href"] = _seg_link(s2)
        elif s2.get("type") == "equation":
            s2["plain_text"] = (s2.get("equation") or {}).get("expression", "")
            s2.setdefault("href", None)
        s2["annotations"] = dict(_ann(s2.get("annotations")), color=(s2.get("annotations") or {}).get("color") or "default")
        out.append(s2)
    return out


def request_rich(rich):
    """Rich text in Notion request shape: read-only fields (plain_text, href) dropped, annotations complete,
    empty text segments dropped. Reused fragments keep their annotations (incl. colour) and link verbatim."""
    out = []
    for s in rich or []:
        a = s.get("annotations") or {}
        ann = {k: bool(a.get(k)) for k in ANN_KEYS}
        ann["color"] = a.get("color") or "default"
        t = s.get("type")
        if t == "text":
            c = _seg_text(s)
            if c == "":
                continue
            txt = {"content": c}
            u = _seg_link(s)
            if u:
                txt["link"] = {"url": u}
            out.append({"type": "text", "text": txt, "annotations": ann})
        elif t == "equation":
            out.append({"type": "equation", "equation": {"expression": (s.get("equation") or {}).get("expression", "")},
                        "annotations": ann})
        elif t == "mention":
            out.append({"type": "mention", "mention": copy.deepcopy(s.get("mention")), "annotations": ann})
        else:
            raise Refused("UNSUPPORTED_INLINE:segment_type", str(t))
    return out


def _merge(segs):
    """Merge adjacent text segments with identical annotations+link; drop empty text."""
    out = []
    for s in segs:
        if s.get("type") == "text" and _seg_text(s) == "":
            continue
        if (out and s.get("type") == "text" and out[-1].get("type") == "text"
                and _ann(out[-1]["annotations"]) == _ann(s["annotations"])
                and (out[-1]["annotations"].get("color") or "default") == (s["annotations"].get("color") or "default")
                and _seg_link(out[-1]) == _seg_link(s)):
            c = _seg_text(out[-1]) + _seg_text(s)
            out[-1] = dict(out[-1], text=dict(out[-1]["text"], content=c), plain_text=c)
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------------------------- canonical form
_EQUIV = {"ws_ann", "nl_code", "merge", "link_strip"}
_MODE_RULES = {"inline": set(), "text": {"lead_ws", "trail_ws"}, "list": {"lead_ws"}, "heading": {"trail_ws"},
               "cell": {"lead_strip", "trail_ws"}}
_VISIBLE_EXTRA = {"trail_ws", "edge_nl", "nl_lead", "lead_strip"}  # markdown-it strips paragraph edges (incl. NBSP)
MODE_OF = {"paragraph": "text", "quote": "text", "callout": "text", "bulleted_list_item": "list",
           "numbered_list_item": "list", "to_do": "list", "heading_1": "heading", "heading_2": "heading",
           "heading_3": "heading", "heading_4": "heading", "table_row": "cell"}


def _rules(mode, how):
    if how == "merge":
        return {"merge"}
    r = set(_EQUIV) | _MODE_RULES.get(mode or "inline", set())
    if how == "visible":
        r |= _VISIBLE_EXTRA
        if mode == "heading":
            r.add("lead_strip")
    return r


_MENTION_BY_ID = ("page", "database", "user", "custom_emoji")


def _canon_obj(x):
    """JSON value with None-valued keys dropped (an absent and a null `end`/`time_zone` are the same)."""
    if isinstance(x, dict):
        return {k: _canon_obj(v) for k, v in sorted(x.items()) if v is not None}
    if isinstance(x, list):
        return [_canon_obj(v) for v in x]
    return x


def _mention_canon(m):
    """(type, canonical value) of a mention: objects with an identity (page, database, user, custom_emoji)
    by id (dashes removed, lower case; user names/avatars are not content); link_mention by the fields the
    exporter renders (href/url, title); everything else (date start/end/time_zone, link_preview url,
    template_mention, unknown types) by the whole object."""
    mt = m.get("type")
    inner = m.get(mt)
    if mt in _MENTION_BY_ID and isinstance(inner, dict):
        return mt, {"id": str(inner.get("id") or "").replace("-", "").lower()}
    if mt == "link_mention" and isinstance(inner, dict):
        return mt, _canon_obj({k: inner.get(k) for k in ("href", "url", "title")})
    return mt, _canon_obj(inner)


def _canon_runs(rich, rules, mode="inline"):
    """Per-character canonical form (roundtrip.md §4.6). Returns a list of runs
    (kind, value, key, link) where kind 'c' = text, 'm' = mention atom, 'e' = equation atom."""
    chars = []
    for si, s in enumerate(rich or []):
        a = s.get("annotations") or {}
        key = tuple(bool(a.get(k)) for k in ANN_KEYS) + (a.get("color") or "default",)
        t = s.get("type")
        tag = 0 if "merge" in rules else si
        if t == "text":
            link = _seg_link(s)
            if link and "link_strip" in rules:
                link = link.strip()
            for ch in _seg_text(s):
                k2 = key
                if "ws_ann" in rules and ch.isspace():
                    k2 = (False, False, False, False, key[4] and not (ch == "\n" and "nl_code" in rules), key[5])
                elif "nl_code" in rules and ch == "\n":
                    k2 = key[:4] + (False, key[5])
                chars.append(("c", ch, k2, link, tag))
        elif t == "mention":
            mt, inner = _mention_canon(s.get("mention") or {})
            chars.append(("m", (mt, json.dumps(inner, sort_keys=True, ensure_ascii=False)), key, None, tag))
        elif t == "equation":
            chars.append(("e", (s.get("equation") or {}).get("expression", ""), key, None, tag))
        else:
            chars.append(("?", json.dumps(s, sort_keys=True), key, None, tag))

    def plain_ws(c):
        return c[0] == "c" and c[1].isspace() and c[1] != "\n" and not c[2][4] and c[3] is None

    plain_key = (False,) * 5 + ("default",)
    if "nl_lead" in rules:  # CommonMark drops spaces/tabs that start a continuation line (newline rule)
        out = []
        for c in chars:
            if c[0] == "c" and c[1] in " \t" and plain_ws(c) and out and out[-1][0] == "c" and out[-1][1] == "\n":
                continue
            out.append(c)
        chars = out
    if "lead_ws" in rules:
        out, at_start, run = [], True, []

        def flush():
            w = len("".join(" " if x[1] == " " else x[1] for x in run).expandtabs(4))
            out.extend([("c", " ", plain_key, None, run[0][4])] * w)
            run.clear()

        for c in chars:
            if at_start and c[0] == "c" and c[1] in " \t " and plain_ws(c):
                run.append(c)
                continue
            if run:
                flush()
            at_start = c[0] == "c" and c[1] == "\n"
            out.append(c)
        if run:
            flush()
        chars = out
    if "lead_strip" in rules:
        while chars and plain_ws(chars[0]):
            chars.pop(0)
    if "trail_ws" in rules:
        out = []
        for c in chars:
            if c[0] == "c" and c[1] == "\n":
                while out and plain_ws(out[-1]):
                    out.pop()
            out.append(c)
        while out and plain_ws(out[-1]):
            out.pop()
        chars = out
    if "edge_nl" in rules:
        def nl(c):
            return c[0] == "c" and c[1] == "\n" and c[3] is None
        while chars and nl(chars[0]):
            chars.pop(0)
        while chars and nl(chars[-1]):
            chars.pop()
    runs = []
    for c in chars:
        if (c[0] == "c" and runs and runs[-1][0] == "c" and runs[-1][2] == c[2] and runs[-1][3] == c[3]
                and runs[-1][4] == c[4]):
            runs[-1][1] += c[1]
        else:
            runs.append([c[0], c[1], c[2], c[3], c[4]])
    return [tuple(r[:4]) if "merge" in rules else tuple(r) for r in runs]


def canon_rich_text(rich, mode=None):
    """D-12 canonical form of a rich_text array: adjacent text segments with identical (annotations, link)
    merged, empty text dropped, annotation defaults filled, mentions canonicalised by _mention_canon (identity
    id for page/database/user, rendered fields for link_mention, the whole object otherwise: a different date
    or URL is a different mention), plain_text/href dropped.  With `mode` ("inline" | "text" | "list" |
    "heading" | "cell") the round-trip equivalence rules of
    roundtrip.md §4.6 (strict) are applied as well: whitespace carries no bold/italic/strike/underline,
    '\\n' carries no code, link URL stripped, plus per-mode leading/trailing whitespace rules."""
    rules = {"merge"} if mode is None else _rules(mode, "equiv")
    out = []
    for kind, val, key, link in _canon_runs(rich, rules, mode or "inline"):
        ann = dict(zip(ANN_KEYS, key[:5]))
        ann["color"] = key[5]
        if kind == "c":
            out.append({"type": "text", "text": {"content": val, "link": {"url": link} if link else None},
                        "annotations": ann})
        elif kind == "m":
            out.append({"type": "mention", "mention": {"type": val[0], val[0]: json.loads(val[1])}, "annotations": ann})
        elif kind == "e":
            out.append({"type": "equation", "equation": {"expression": val}, "annotations": ann})
        else:
            out.append({"type": "unknown", "raw": val, "annotations": ann})
    return out


def _rich_eq(a, b, mode, how):
    r = _rules(mode, how)
    return _canon_runs(a, r, mode) == _canon_runs(b, r, mode)


# ---------------------------------------------------------------------------------------------- writable fields
def writable_fields(block):
    """The fields write-back may set for this block type (WRITEBACK_SPEC §2.4): rich_text (+checked for
    to_do, +language for code), expression (equation), cells (table_row), nothing for divider. Colour, icon,
    caption, is_toggleable, children are never written. Non-R types -> {}."""
    t = block.get("type")
    v = block.get(t) if isinstance(block.get(t), dict) else {}
    if t in TEXT_TYPES:
        f = {"rich_text": v.get("rich_text") or []}
        if t == "to_do":
            f["checked"] = bool(v.get("checked"))
        return f
    if t == "code":
        return {"rich_text": v.get("rich_text") or [], "language": v.get("language") or "plain text"}
    if t == "equation":
        return {"expression": v.get("expression") or ""}
    if t == "table_row":
        return {"cells": v.get("cells") or []}
    return {}


def canon_writable(block, equiv=False):
    """Canonical JSON of writable fields (D-12 merge; with equiv=True the strict round-trip equivalence)."""
    t = block.get("type")
    f = writable_fields(block)
    mode = MODE_OF.get(t, "inline")
    out = {}
    if t == "code":
        return {"code_text": _plain(f["rich_text"]), "language": f["language"]}
    if "rich_text" in f:
        out["rich_text"] = canon_rich_text(f["rich_text"], mode if equiv else None)
    if "checked" in f:
        out["checked"] = f["checked"]
    if "expression" in f:
        out["expression"] = f["expression"]
    if "cells" in f:
        out["cells"] = [canon_rich_text(c, "cell" if equiv else None) for c in f["cells"]]
    return out


def _fields_eq(t, a, b, how):
    """Compare two field dicts (internal shape) of block type t. how: 'equiv' | 'visible'."""
    if t == "code":
        return a.get("code_text") == b.get("code_text") and a.get("language") == b.get("language")
    if t == "equation":
        return a.get("expression") == b.get("expression")
    if t == "divider":
        return True
    if t == "table_row":
        ca, cb = a.get("cells") or [], b.get("cells") or []
        return len(ca) == len(cb) and all(_rich_eq(x, y, "cell", how) for x, y in zip(ca, cb))
    if t == "to_do" and bool(a.get("checked")) != bool(b.get("checked")):
        return False
    return _rich_eq(a.get("rich_text") or [], b.get("rich_text") or [], MODE_OF.get(t, "inline"), how)


def _fields_of_block(block):
    t = block.get("type")
    f = writable_fields(block)
    if t == "code":
        return {"code_text": _plain(f["rich_text"]), "language": f["language"]}
    return dict(f)


# ---------------------------------------------------------------------------------------------- rendering
_SHIM = None


def _shim():
    """Private renderer: an Exporter built without __init__ (which would open state/export.db read-write)."""
    global _SHIM
    if _SHIM is None:
        s = EO.Exporter.__new__(EO.Exporter)
        init = getattr(s, "_init_common", None)  # exporter >= RENDERER_VERSION 1.2.1 (planning support)
        if init is not None:
            try:
                init(None, False, planning=True)
            except Exception:  # different signature: fall back to the attributes set below
                pass
        s.report = defaultdict(Counter)
        s.lists = defaultdict(list)
        s.nodes, s.users, s.obj, s.blocks, s.files, s.prev = {}, {}, {}, {}, {}, {}
        s.folder_owner, s.tpl_folder_owner = {}, {}
        for k in ("manifest", "attachments", "att_by_sha", "att_map"):
            if not isinstance(getattr(s, k, None), dict):
                setattr(s, k, {})
        s._list_num, s._blank_count, s._synced_stack = 1, 0, set()
        s.roots, s.root_ids, s.ts_roots, s.sidebar = [], set(), {}, {"roots": []}
        s.run_id, s.out, s.vault, s.state, s.roots_kinds = "md2blocks", None, None, None, None
        s._rec = None
        s._sandbox = True
        _SHIM = s
    return _SHIM


_SWAP = ("report", "lists", "_list_num", "_blank_count", "_rec", "_sandbox", "blocks")


@contextlib.contextmanager
def _sandbox(ex, overlay=None):
    """Swap the exporter's mutable render state for the duration of a render call (no block_lines
    recording, throwaway counters, lookup-only attachments)."""
    ex = ex if ex is not None else _shim()
    saved = {k: getattr(ex, k) for k in _SWAP if hasattr(ex, k)}
    ex.report = defaultdict(Counter)
    ex.lists = defaultdict(list)
    if "_rec" in saved:
        ex._rec = None
    if "_sandbox" in saved:
        ex._sandbox = True
    if overlay is not None:
        ex.blocks = ChainMap(overlay, saved.get("blocks") or {})
    try:
        yield ex
    finally:
        for k in _SWAP:
            if k in saved:
                setattr(ex, k, saved[k])


def _rt(rich, table=False, ex=None):
    with _sandbox(ex) as r:
        return r.rt(rich, RENDER_TS, table)


def render_own_lines(exporter, block, prefix="", list_num=1):
    """The block's own lines as render_block writes them (children cut), without side effects.
    table_row is rendered through a one-row table (row line only)."""
    t = block.get("type")
    b = dict(block)
    b["has_children"] = False
    if t == "table_row":
        cells = (b.get("table_row") or {}).get("cells") or []
        tid = "md2blocks:table"
        tb = {"object": "block", "id": tid, "type": "table", "has_children": True,
              "table": {"table_width": max(len(cells), 1), "has_column_header": True, "has_row_header": False}}
        with _sandbox(exporter, {tid: [b]}) as r:
            lines = r.render_block(tb, RENDER_TS, prefix)
        return [str(lines[0])]
    with _sandbox(exporter) as r:
        r._list_num = list_num
        return [str(x) for x in r.render_block(b, RENDER_TS, prefix)]


def _table_sep(width, prefix=""):
    return prefix + "|" + "|".join([" --- "] * width) + "|"


# ---------------------------------------------------------------------------------------------- fast inline (exporter grammar)
_ESC = set("\\`*_[]$~=%<#")
_URL_RE = re.compile(r"https?://[^\s<>\[\]()]+")
_PLAIN_RE = re.compile(r"(?:(?!https?://)[^\\`*\[\]$~<|])+")
_TAGS = {"strong": "bold", "em": "italic", "del": "strikethrough", "u": "underline"}
_OPEN_TAG = re.compile(r"<(strong|em|del|u)>")


class _Fail(Exception):
    pass


def _coalesce(sub):
    """A container (<u>…</u>, [label](url)) wraps exactly one segment's rendering, but wrap_marks() hoisted
    that segment's whitespace margins out of its ** / * / ~~: put the margins back into the core segment."""
    if len(sub) < 2 or any(g["type"] != "text" for g in sub):
        return sub
    codes = [g for g in sub if g["annotations"].get("code")]
    if codes and len(codes) < len(sub) and all(g["annotations"].get("code") or set(_seg_text(g)) == {"\n"} for g in sub):
        # one multi-line code segment renders as `l1`\n`l2` inside its <strong>/<em>/<del>/<u>/link wrapper
        a0 = codes[0]["annotations"]
        if all(g["annotations"] == a0 and _seg_link(g) == _seg_link(codes[0]) for g in codes):
            return [_text_seg("".join(_seg_text(g) for g in sub), a0, _seg_link(codes[0]))]
        return sub
    core = [g for g in sub if _seg_text(g).strip() or g["annotations"].get("code")]
    if len(core) != 1:
        return sub
    c = core[0]
    return [_text_seg("".join(_seg_text(g) for g in sub), c["annotations"], _seg_link(c))]


class _FastInline:
    """Recursive-descent inverse of Exporter.rt() (proto/rtparse.py, without mentions/wikilinks/autolinks)."""

    def __init__(self, s, table, choices):
        self.s, self.table, self.choices = s, table, choices
        self.ptr = 0
        self.arity = []

    def choose(self, n):
        k = self.choices[self.ptr] if self.ptr < len(self.choices) else 0
        self.arity.append(n)
        self.ptr += 1
        return k

    def seq(self, i, ann, link, term):
        s = self.s
        segs, buf = [], []

        def flush():
            if buf:
                segs.append(_text_seg("".join(buf), ann, link))
                buf.clear()

        while True:
            if term is not None and s.startswith(term, i):
                flush()
                return i, segs
            if i >= len(s):
                if term is None:
                    flush()
                    return i, segs
                raise _Fail("missing " + term)
            c = s[i]
            if c == "\\":
                nx = s[i + 1:i + 2]
                if nx and (nx in _ESC or (self.table and nx == "|")):
                    buf.append(nx)
                    i += 2
                else:
                    buf.append("\\")
                    i += 1
                continue
            if c == "`":
                j, seg = self.code_span(i, ann, link)
                if seg is None:
                    buf.append(s[i:j])
                else:
                    flush()
                    segs.append(seg)
                i = j
                continue
            if c == "$":
                flush()
                i, seg = self.equation(i, ann)
                segs.append(seg)
                continue
            if c == "[":
                if s.startswith("[[", i):
                    raise _Fail("wikilink")
                flush()
                i, sub = self.link(i, ann)
                segs.extend(sub)
                continue
            if c == "<":
                m = _OPEN_TAG.match(s, i)
                if m:
                    flush()
                    tag = m.group(1)
                    a2 = dict(ann)
                    a2[_TAGS[tag]] = True
                    j, sub = self.seq(m.end(), a2, link, f"</{tag}>")
                    segs.extend(_coalesce(sub))
                    i = j + len(tag) + 3
                    continue
                if self.table and s.startswith("<br>", i):
                    buf.append("\n")
                    i += 4
                    continue
                buf.append("<")
                i += 1
                continue
            if c == "*":
                flush()
                if s.startswith("**", i):
                    j, sub = self.seq(i + 2, dict(ann, bold=True), link, "**")
                    segs.extend(sub)
                    i = j + 2
                else:
                    j, sub = self.seq(i + 1, dict(ann, italic=True), link, "*")
                    segs.extend(sub)
                    i = j + 1
                continue
            if c == "~":
                if s.startswith("~~", i):
                    flush()
                    j, sub = self.seq(i + 2, dict(ann, strikethrough=True), link, "~~")
                    segs.extend(sub)
                    i = j + 2
                else:
                    buf.append("~")
                    i += 1
                continue
            if c in "]|":
                buf.append(c)
                i += 1
                continue
            m = _URL_RE.match(s, i)
            if m:
                end = self.url_end(m, term)
                buf.append(s[i:end])
                i = end
                continue
            m = _PLAIN_RE.match(s, i)
            if m:
                buf.append(m.group(0))
                i = m.end()
                continue
            buf.append(c)
            i += 1

    def url_end(self, m, term):
        u, st = m.group(0), m.start()
        d = len(u)
        bt = u.find("`")
        if bt > 0:
            d = min(d, bt)
        if term in ("**", "*", "~~"):
            k = u.find(term)
            if k > 0:
                d = min(d, k)
        k = u.find("\\")
        if k > 0:
            d = min(d, k)
        cands = [d] + [k for k in range(len(u), 7, -1) if k != d and (k == len(u) or u[k] in "*~`\\$")]
        idx = self.choose(len(cands))
        if idx >= len(cands):
            raise _Fail("url choice")
        return st + cands[idx]

    def code_span(self, i, ann, link):
        s = self.s
        n = len(re.compile(r"`+").match(s, i).group(0))
        j = i + n
        m2 = re.compile(r"(?<!`)`{%d}(?!`)" % n).search(s, j)
        if not m2:
            return j, None
        inner = s[j:m2.start()]
        if self.table:
            inner = inner.replace("\\|", "|")
        if len(inner) >= 2 and inner[0] == " " and inner[-1] == " " and (inner[1] == "`" or inner[-2] == "`"):
            inner = inner[1:-1]
        return m2.end(), _text_seg(inner, dict(ann, code=True), link)

    def equation(self, i, ann):
        s = self.s
        j = i + 1
        out = []
        while j < len(s):
            c = s[j]
            if c == "\\" and s[j + 1:j + 2] == "$":
                if self.choose(2) == 0:
                    out.append("$")
                    j += 2
                    continue
                out.append("\\")
                j += 1
                continue
            if self.table and c == "\\" and s[j + 1:j + 2] == "|":
                out.append("|")
                j += 2
                continue
            if c == "$":
                return j + 1, _eq_seg("".join(out))
            out.append(c)
            j += 1
        raise _Fail("unclosed $")

    def link(self, i, ann):
        s = self.s
        j, sub = self.seq(i + 1, ann, "\0pending", "](")
        k = j + 2
        if s.startswith("<", k):
            e = s.find(">", k)
            if e < 0 or not s.startswith(")", e + 1):
                raise _Fail("bad <url>")
            url, end = s[k + 1:e], e + 2
        else:
            e = s.find(")", k)
            if e < 0:
                raise _Fail("bad url")
            url, end = s[k:e], e + 1
        if not sub:
            raise _Fail("empty link label")
        for g in sub:
            if g["type"] != "text":
                raise _Fail("equation inside link label")
            g["text"]["link"] = {"url": url}
            g["href"] = url
        return end, _coalesce(sub)


def _fast_inline(md, table=False, budget=256, accept=None):
    """Exporter-grammar parse of one rich_text rendering: a parse whose Exporter.rt() equals `md` byte for
    byte, else None. Several Notion contents can render to the same Markdown (e.g. a raw URL followed by a
    bold segment, A11): with `accept` (a predicate, normally "agrees with the CommonMark reading") the first
    exact parse satisfying it is preferred; otherwise the first exact parse is returned."""
    if md == "":
        return []
    choices = []
    first = None
    for _ in range(budget):
        p = _FastInline(md, table, choices)
        try:
            _, segs = p.seq(0, {}, None, None)
            if _rt(segs, table) == md:
                if accept is None or accept(segs):
                    return segs
                if first is None:
                    first = segs
        except (_Fail, RecursionError):
            pass
        ar = p.arity[:]
        ch = (choices + [0] * len(ar))[:len(ar)]
        k = len(ar) - 1
        while k >= 0:
            if ch[k] + 1 < ar[k]:
                ch[k] += 1
                choices = ch[:k + 1]
                break
            k -= 1
        else:
            return first
    return first


# ---------------------------------------------------------------------------------------------- line-start escapes
_LS_TOK = re.compile(r"^(\s*)\\(>|\||#{1,6}(?=\s|$)|[+\-](?=\s|$)|=+\s*$|-+\s*$)")
_LS_NUM = re.compile(r"^(\s*)(\d{1,9})\\([.)])(?=\s|$)")
_THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")


def _unesc_line(ln):
    """Inverse of export_obsidian.esc_line_start (leading NBSP run = spaces unless it precedes an escape)."""
    m = re.match("^ +", ln)
    if m:
        rest = ln[m.end():]
        if _LS_TOK.match(rest) or _LS_NUM.match(rest):
            return m.group(0) + _unesc_line(rest)
        return " " * len(m.group(0)) + rest
    m = _LS_NUM.match(ln)
    if m:
        return m.group(1) + m.group(2) + m.group(3) + ln[m.end():]
    m = _LS_TOK.match(ln)
    if m:
        return m.group(1) + ln[len(m.group(1)) + 1:]
    return ln


def _split_row(line):
    """'| a | b |' -> ['a', 'b'] (cell sources, one pad space removed each side) or None."""
    if not (line.startswith("|") and line.endswith("|")) or len(line) < 2:
        return None
    parts, cur, i = [], [], 1
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i:i + 2])
            i += 2
            continue
        if c == "|":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        return None
    cells = []
    for p in parts:
        if len(p) < 2 or not (p.startswith(" ") and p.endswith(" ")):
            return None
        inner = p[1:-1]
        cells.append("" if inner == " " else inner)
    return cells


# ---------------------------------------------------------------------------------------------- CommonMark reading
def _math_inline_rule(state, silent):
    src, pos, mx = state.src, state.pos, state.posMax
    if src[pos] != "$":
        return False
    if pos + 1 < mx and src[pos + 1] == "$":
        if not silent:
            tok = state.push("md2b_math2", "", 0)
            tok.content = "$$"
        state.pos = pos + 2
        return True
    if pos + 1 >= mx or src[pos + 1].isspace():
        return False
    j = pos + 1
    while j < mx:
        c = src[j]
        if c == "\\" and j + 1 < mx and src[j + 1] == "$":
            j += 2
            continue
        if c == "$" and not src[j - 1].isspace():
            if not silent:
                tok = state.push("md2b_math", "", 0)
                tok.content = src[pos + 1:j].replace("\\$", "$")
            state.pos = j + 1
            return True
        j += 1
    return False


def _math_block_rule(state, startLine, endLine, silent):
    if state.sCount[startLine] - state.blkIndent >= 4:
        return False
    pos = state.bMarks[startLine] + state.tShift[startLine]
    line = state.src[pos:state.eMarks[startLine]]
    if not line.startswith("$$"):
        return False
    rest = line[2:].strip()
    if rest:
        if len(rest) >= 2 and rest.endswith("$$"):
            content, nxt = rest[:-2], startLine + 1
        else:
            return False
    else:
        k = startLine + 1
        found = False
        while k < endLine:
            p = state.bMarks[k] + state.tShift[k]
            e = state.eMarks[k]
            if p < e and state.sCount[k] < state.blkIndent:
                break
            if state.src[p:e].strip() == "$$":
                found = True
                break
            k += 1
        if not found:
            return False
        content = state.getLines(startLine + 1, k, state.blkIndent, False)
        nxt = k + 1
    if silent:
        return True
    tok = state.push("md2b_math_block", "math", 0)
    tok.block = True
    tok.content = content
    tok.markup = "$$"
    tok.map = [startLine, nxt]
    state.line = nxt
    return True


def _record_lead(orig):
    """Wrap markdown-it's paragraph rule: it strips the paragraph content with Python str.strip(), which also
    removes NBSP / U+3000 (the exporter writes leading spaces of a text line as NBSP). Record what was stripped
    from the front in the inline token's meta['md2b_lead'] so the reading can restore it (_para_lead)."""
    def rule(state, startLine, endLine, silent):
        n = len(state.tokens)
        ok = orig(state, startLine, endLine, silent)
        if ok and not silent:
            for tok in state.tokens[n:]:
                if tok.type == "inline" and tok.map:
                    raw = state.getLines(tok.map[0], tok.map[1], state.blkIndent, False)
                    tok.meta["md2b_lead"] = raw[:len(raw) - len(raw.lstrip())]
        return ok
    return rule


def _make_md():
    md = MarkdownIt("commonmark", {"breaks": True, "html": True})
    md.enable(["strikethrough", "table"])
    md.disable("text_join")
    para = [r for r in md.block.ruler.__rules__ if r.name == "paragraph"][0]
    md.block.ruler.at("paragraph", _record_lead(para.fn), {"alt": list(para.alt)})
    md.inline.ruler.before("escape", "md2b_math", _math_inline_rule)
    md.block.ruler.before("fence", "md2b_math_block", _math_block_rule,
                          {"alt": ["paragraph", "reference", "blockquote", "list"]})
    md.normalizeLink = lambda url: url
    md.normalizeLinkText = lambda url: url
    md.validateLink = lambda url: True
    md.add_render_rule("text_special", lambda self, t, i, o, e: escapeHtml(t[i].content))
    md.add_render_rule("md2b_math", lambda self, t, i, o, e: "<eq>" + escapeHtml(t[i].content) + "</eq>")
    md.add_render_rule("md2b_math2", lambda self, t, i, o, e: "<eq2/>")
    md.add_render_rule("md2b_math_block", lambda self, t, i, o, e: "<eqn>" + escapeHtml(t[i].content) + "</eqn>\n")
    return md


_MD = _make_md()

_HTML_TOGGLE = {"<u>": ("underline", 1), "</u>": ("underline", -1), "<strong>": ("bold", 1),
                "</strong>": ("bold", -1), "<em>": ("italic", 1), "</em>": ("italic", -1),
                "<del>": ("strikethrough", 1), "</del>": ("strikethrough", -1)}
_BR = re.compile(r"^<br\s*/?>$", re.I)
_TAG_RE = re.compile(r"(?:^|(?<=\s))#([\w/\-]+)")
_HL_RE = re.compile(r"==(?=[^\s=])[\s\S]*?(?<=[^\s=])==")
_FN_RE = re.compile(r"\[\^[^\]\s]*\]|\^\[")
_BLOCKID_RE = re.compile(r"(?:^|\s)\^[A-Za-z0-9-]+\s*$")
_FN_DEF = re.compile(r"^\s{0,3}(?:>\s*)*\[\^[^\]]+\]:")
_NOTION_HOST = re.compile(r"^https?://([a-z0-9-]+\.)*(notion\.so|notion\.site|notion\.com)(/|$|\?|#)", re.I)
_NOTION_REL = re.compile(r"^/(?:p/)?(?:[^/]*-)?[0-9a-fA-F]{32}(?:[?#].*)?$")


def _scan_obsidian(children):
    """Refuse Obsidian-only inline syntax in unescaped text (escaped chars / code / math are masked)."""
    flat = []
    for tok in children:
        if tok.type == "text":
            flat.append(tok.content)
        elif tok.type == "text_special":
            flat.append("\x00")
        elif tok.type in ("softbreak", "hardbreak"):
            flat.append("\n")
        elif tok.type in ("code_inline", "md2b_math"):
            flat.append("\x01")
    s = "".join(flat)
    if "![[" in s:
        raise _unsupported("embed")
    if "[[" in s:
        raise _unsupported("wikilink")
    if "%%" in s:
        raise _unsupported("comment")
    if _HL_RE.search(s):
        raise _unsupported("highlight")
    if _FN_RE.search(s):
        raise _unsupported("footnote")
    for m in _TAG_RE.finditer(s):
        if not m.group(1).isdigit():
            raise _unsupported("tag", "#" + m.group(1)[:30])
    if _BLOCKID_RE.search(s):
        raise _unsupported("block_id")


def _is_notion_url(u):
    u = (u or "").strip()
    return bool(_NOTION_HOST.match(u) or _NOTION_REL.match(u))


def _check_url(u):
    u = (u or "").strip()
    if _is_notion_url(u):
        raise _unsupported("notion_link")
    if u.startswith("#"):
        raise _unsupported("anchor_link")
    if not re.match(r"(?i)^(https?://|mailto:)\S", u):
        raise _unsupported("link_scheme", u[:60])
    if _u16(u) > MAX_TEXT:
        raise Refused("LIMIT:link_url", f"链接 URL 超过 {MAX_TEXT} 字符，Notion API 不接受")


def _line_start_nbsp(children, i, c):
    """Text token `c` (children[i]) starts a source line of a text/list block with a NBSP run: read it the way
    the exporter grammar does (_unesc_line): spaces, unless the rest of the source line is an escaped
    line-start token (`\\-`, `1\\.`, `\\#` …), in which case the NBSP run is literal text."""
    m = re.match("^\u00a0+", c)
    parts = [c[m.end():]]
    for tok in children[i + 1:]:
        if tok.type in ("softbreak", "hardbreak"):
            break
        parts.append(tok.content if tok.type == "text" else
                     (tok.markup or tok.content) if tok.type == "text_special" else "\x01")
    rest = "".join(parts)
    if _LS_TOK.match(rest) or _LS_NUM.match(rest):
        return c
    return " " * m.end() + c[m.end():]


def _cm_inline(children, table=False, mode="inline"):
    """markdown-it inline tokens -> rich text (CommonMark semantics); raises Refused on unsupported syntax."""
    _scan_obsidian(children)
    segs = []
    cnt = Counter()
    html_stack = []
    link = None
    link_start = 0

    def ann(code=False):
        a = {k: cnt[k] > 0 for k in ("bold", "italic", "strikethrough", "underline")}
        a["code"] = code
        return a

    nbsp_lead = mode in ("text", "list")  # esc_line_start wrote leading spaces of each text line as NBSP
    at_ls = True  # at the start of a source line
    for i, tok in enumerate(children):
        ty = tok.type
        was_ls, at_ls = at_ls, False
        if ty in ("text", "text_special"):
            c = tok.content
            if nbsp_lead and was_ls and ty == "text" and link is None and c.startswith("\u00a0"):
                c = _line_start_nbsp(children, i, c)
            if c:
                segs.append(_text_seg(c, ann(), link))
            else:
                at_ls = was_ls
        elif ty in ("softbreak", "hardbreak"):
            segs.append(_text_seg("\n", ann(), link))
            at_ls = True
        elif ty == "code_inline":
            segs.append(_text_seg(tok.content, ann(True), link))
        elif ty in ("strong_open", "strong_close"):
            cnt["bold"] += 1 if ty.endswith("open") else -1
        elif ty in ("em_open", "em_close"):
            cnt["italic"] += 1 if ty.endswith("open") else -1
        elif ty in ("s_open", "s_close"):
            cnt["strikethrough"] += 1 if ty.endswith("open") else -1
        elif ty == "link_open":
            if tok.attrGet("title"):
                raise _unsupported("link_title")
            href = tok.attrGet("href") or ""
            _check_url(href)
            link = href.strip()
            link_start = len(segs)
        elif ty == "link_close":
            if len(segs) == link_start:
                raise _unsupported("empty_link")
            link = None
        elif ty == "image":
            raise _unsupported("image")
        elif ty == "html_inline":
            h = tok.content.strip()
            if table and _BR.match(h):
                segs.append(_text_seg("\n", ann(), link))
                continue
            key = h.lower()
            if key not in _HTML_TOGGLE:
                raise _unsupported("comment" if h.startswith("<!--") else "html", h[:40])
            name, d = _HTML_TOGGLE[key]
            if d > 0:
                html_stack.append(key)
                cnt[name] += 1
            else:
                if not html_stack or html_stack[-1] != key.replace("/", ""):
                    raise _unsupported("html", "标签不配对")
                html_stack.pop()
                cnt[name] -= 1
        elif ty == "md2b_math":
            if link:
                raise _unsupported("math_link")
            segs.append(_eq_seg(tok.content))
        elif ty == "md2b_math2":
            raise _unsupported("display_math")
        else:
            raise _unsupported(ty)
    if html_stack:
        raise _unsupported("html", "标签未闭合")
    return _merge(segs)


def _tree(tokens):
    root = []
    stack = [root]
    for tok in tokens:
        if tok.nesting == 1:
            node = {"tok": tok, "kids": []}
            stack[-1].append(node)
            stack.append(node["kids"])
        elif tok.nesting == -1:
            stack.pop()
        else:
            stack[-1].append({"tok": tok, "kids": []})
    return root


_NEWLINE_RE = re.compile(r"\r\n?|\n")


def _src_lines(lines):
    """The source lines as markdown-it numbers them in token maps (it normalises \\r\\n and \\r to \\n and NUL
    to U+FFFD before splitting)."""
    return _NEWLINE_RE.split("\n".join(lines).replace("\0", "\ufffd"))


def _parse_md(lines):
    if any(_FN_DEF.match(ln) for ln in lines):
        raise _unsupported("footnote")
    env = {}
    tokens = _MD.parse("\n".join(lines), env)
    if env.get("references"):
        raise _unsupported("reference_definition")
    return _tree(tokens)


def _html(lines, inline=False):
    """HTML used for the 'renders identically' check (lists made tight, ordered-list start ignored,
    `<!-- -->` separators dropped, task-box case folded)."""
    env = {}
    src = "\n".join(lines) if isinstance(lines, list) else lines
    tokens = _MD.parseInline(src, env) if inline else _MD.parse(src, env)
    out, stack = [], []
    for tok in tokens:
        if tok.type == "html_block" and tok.content.strip() == "<!-- -->":
            continue
        if tok.type == "ordered_list_open" and tok.attrs:
            tok.attrs.pop("start", None)
        if tok.type in ("paragraph_open", "paragraph_close") and stack and stack[-1] == "list_item_open":
            tok.hidden = True
        if tok.nesting == 1 and tok.type != "paragraph_open":
            stack.append(tok.type)
        elif tok.nesting == -1 and tok.type != "paragraph_close" and stack:
            stack.pop()
        out.append(tok)
    html = _MD.renderer.render(out, _MD.options, env)
    return re.sub(r"(<li>(?:<p>)?)\[X\]", r"\1[x]", html)


def _check_node_kinds(nodes, allow_separator=True):
    """Refuse HTML blocks (except the exporter's `<!-- -->` list separator between new blocks) and tables."""
    for n in nodes:
        ty = n["tok"].type
        if ty == "html_block":
            if n["tok"].content.strip() != "<!-- -->" or not allow_separator:
                raise _unsupported("comment" if n["tok"].content.lstrip().startswith("<!--") else "html",
                                   n["tok"].content.strip()[:40])
        elif ty == "table_open":
            raise Refused("UNSUPPORTED_BLOCK:table", "表格只能修改已有行的单元格文字，不能新建/增删行列")


def _para_lead(inline):
    """The leading whitespace markdown-it stripped from a paragraph (meta['md2b_lead'], see _record_lead), read
    the way the exporter grammar reads a text line start: spaces/tabs are CommonMark indentation (the exporter
    never writes them at a text-line start, esc_line_start turns them into NBSP) and stay dropped; from the
    first other whitespace character on (NBSP, U+3000, …) the run is content, a leading NBSP run reading back
    as spaces exactly as in _unesc_line (kept literal before an escaped line-start token)."""
    lead = (inline.meta or {}).get("md2b_lead") or ""
    lead = lead[lead.rfind("\n") + 1:].lstrip(" \t")
    if not lead:
        return ""
    u = _unesc_line(lead + inline.content.split("\n", 1)[0])
    return u[:len(u) - len(u.lstrip())]


def _paras_rich(nodes, mode, span=None):
    """Consecutive paragraph nodes -> one rich_text, joined by '\\n' * (blank lines + 1).
    Edge whitespace that markdown-it strips but the exporter grammar reads as content is restored, so an edit
    elsewhere in the block never drops it: each paragraph's leading NBSP/U+3000 run (_para_lead) and, with
    `span` = (first line, end line) of the block's text in the parsed source, blank lines before the first and
    after the last paragraph ('\\n' each, as the exporter renders a leading/trailing newline)."""
    out = []
    prev_end = None
    for n in nodes:
        tok = n["tok"]
        if tok.type != "paragraph_open":
            return None
        inline = n["kids"][0]["tok"]
        rich = _cm_inline(inline.children or [], False, mode if mode in ("text", "list") else "inline")
        if prev_end is not None:
            out.append(_text_seg("\n" * (tok.map[0] - prev_end + 1)))
        elif span is not None and tok.map[0] > span[0]:
            out.append(_text_seg("\n" * (tok.map[0] - span[0])))
        if mode in ("text", "list"):
            out.append(_text_seg(_para_lead(inline)))
        out.extend(rich)
        prev_end = tok.map[1]
    if span is not None and span[1] is not None and prev_end is not None and span[1] > prev_end:
        out.append(_text_seg("\n" * (span[1] - prev_end)))
    return _merge(out)


def _heading_lead(line, lvl):
    """Leading whitespace of an ATX heading's text as the exporter grammar reads it ('#'*lvl + ' ' + rt(text):
    every character after the one separator space is content); markdown-it strips it. Plain, outside markup."""
    s = (line or "").lstrip(" \t")
    mk = "#" * lvl + " "
    if not s.startswith(mk):
        return []
    rest = s[len(mk):]
    if not rest.strip():
        return []
    w = rest[:len(rest) - len(rest.lstrip())]
    return [_text_seg(w)] if w else []


def _cell_edges(src, rich):
    """markdown-it strips a table cell (str.strip()); the exporter grammar keeps everything between the single
    pad spaces ('| ' cell ' |'): restore the cell's leading/trailing whitespace (rt() puts it outside markup)."""
    if not src:
        return rich
    if not src.strip():
        return [_text_seg(src)] + rich
    lead = src[:len(src) - len(src.lstrip())]
    trail = src[len(src.rstrip()):]
    return ([_text_seg(lead)] if lead else []) + rich + ([_text_seg(trail)] if trail else [])


def _child_like(line):
    """True if this line, read on its own, starts a block (list/task item, quote, heading, fence, rule, $$ …).
    Inside a list item's paragraph such a line is text for CommonMark only because it is indented 4+ columns
    past the item's content (or cannot interrupt a paragraph), while Obsidian may show it nested and the
    exporter renders a child exactly like that (to_do children sit 6 spaces deep). Exporter text never has
    one: esc_line_start escapes every line-start token."""
    s = line.lstrip(" \t")
    if not s:
        return False
    toks = _MD.parse(s, {})
    return bool(toks) and toks[0].type != "paragraph_open"


HINT_NESTING = ("列表项内缩进的这一行以列表/任务/引用/标题等标记开头：Obsidian 可能显示为子项，导出格式读作正文；"
                "子项请在 Notion 中添加，若是正文请在行首标记前加 \\")


def _check_item_lines(item_node, paras, src, reason):
    """Refuse a list item whose paragraph continuation lines look like nested blocks (see _child_like)."""
    if src is None:
        return
    first = item_node["tok"].map[0] if item_node["tok"].map else None
    for p in paras:
        a, b = p["tok"].map
        for ln in range(a, b):
            if ln != first and ln < len(src) and _child_like(src[ln]):
                raise Refused(reason, HINT_NESTING)


def _lang_from_info(info):
    word = (info or "").strip().split()[0] if (info or "").strip() else ""
    if word in _LANG_INV:
        return _LANG_INV[word]
    alias = _LANG_ALIASES.get(word.lower())
    raise Refused(f"UNSUPPORTED_BLOCK:code_language:{word[:30]}",
                  "Notion 不支持该代码语言名" + (f"；请写成 ```{_md_lang(alias)}" if alias else "（请使用 Notion 的语言名）"))


_CHECKBOX = re.compile(r"^\[([ xX])\](?: |$)")


def _split_checkbox(inline_children):
    """If the first inline token is text starting with '[ ] ' / '[x] ', return (checked, children without it)."""
    if not inline_children or inline_children[0].type != "text":
        return None, inline_children
    m = _CHECKBOX.match(inline_children[0].content)
    if not m:
        return None, inline_children
    first = copy.copy(inline_children[0])
    first.content = inline_children[0].content[m.end():]
    return m.group(1) in "xX", [first] + list(inline_children[1:])


def _list_item_fields(item_node, t_expected, mode="list", src=None):
    """One list_item node whose children are paragraphs only -> (rich, checked). `src`: the parsed source
    lines (for the nested-looking-line check and the leading/trailing blank-line restore)."""
    kids = item_node["kids"]
    if any(k["tok"].type != "paragraph_open" for k in kids):
        raise Refused("TYPE_CHANGED", "列表项内出现了子块/其他结构；" + HINT_MANUAL)
    _check_item_lines(item_node, kids, src, "TYPE_CHANGED")
    checked = None
    if kids:
        inline = kids[0]["kids"][0]["tok"]
        checked, ch2 = _split_checkbox(inline.children or [])
        if checked is not None:
            inline = copy.copy(inline)
            inline.children = ch2
            kids = [dict(kids[0], kids=[{"tok": inline, "kids": []}])] + kids[1:]
    if (t_expected == "to_do") != (checked is not None):
        raise Refused("TYPE_CHANGED", "任务项与普通列表项之间的转换" + HINT_MANUAL)
    span = (item_node["tok"].map[0], len(src)) if src is not None and item_node["tok"].map else None
    rich = _paras_rich(kids, mode, span) if kids else []
    return rich, checked


def _cm_fields(t, body, old_block=None):
    """CommonMark reading of one block's own lines (prefix stripped; callout header removed) -> fields."""
    if t == "table_row":
        cells_src = _split_row(body[0]) if body else None
        if cells_src is None:
            raise Refused("STRUCTURE_CHANGED", "表格行格式被改动；" + HINT_MANUAL)
        width = len(cells_src)
        nodes = _parse_md([body[0], _table_sep(width)])
        _ths = []

        def walk(ns):
            for n in ns:
                if n["tok"].type in ("th_open", "td_open"):
                    _ths.append(n)
                else:
                    walk(n["kids"])
        if len(nodes) != 1 or nodes[0]["tok"].type != "table_open":
            raise Refused("STRUCTURE_CHANGED", "表格行格式被改动；" + HINT_MANUAL)
        walk(nodes)
        if len(_ths) != width:
            raise Refused("STRUCTURE_CHANGED", "表格行格式被改动；" + HINT_MANUAL)
        cells = []
        for th, src in zip(_ths, cells_src):
            ch = th["kids"][0]["tok"].children if th["kids"] else []
            cells.append(_merge(_cell_edges(src, _cm_inline(ch or [], True, "cell"))))
        return {"cells": cells}
    nodes = _parse_md(body)
    _check_node_kinds(nodes, allow_separator=False)
    kinds = [n["tok"].type for n in nodes]
    src = _src_lines(body)
    if t == "paragraph":
        if not nodes:
            return {"rich_text": []}
        if kinds == ["fence"] and not nodes[0]["tok"].info.strip():
            c = nodes[0]["tok"].content
            c = c[:-1] if c.endswith("\n") else c
            return {"rich_text": [_text_seg(c, {"code": True})]}
        rich = _paras_rich(nodes, "text", (0, len(src)))
        if rich is None:
            raise Refused("TYPE_CHANGED", HINT_MANUAL)
        return {"rich_text": rich}
    if t in HEADINGS:
        lvl = int(t[-1])
        if kinds != ["heading_open"] or nodes[0]["tok"].tag != f"h{lvl}":
            if not nodes and not any(x.strip() for x in body):
                return {"rich_text": []}
            raise Refused("TYPE_CHANGED", "标题级别/类型变化" + HINT_MANUAL)
        inline = nodes[0]["kids"][0]["tok"]
        hl = nodes[0]["tok"]
        lead = (_heading_lead(src[hl.map[0]], lvl)
                if hl.markup.startswith("#") and hl.map and hl.map[0] < len(src) else [])
        return {"rich_text": _merge(lead + _cm_inline(inline.children or [], False, "heading"))}
    if t in LIST_TYPES:
        want = "ordered_list_open" if t == "numbered_list_item" else "bullet_list_open"
        if kinds != [want] or len(nodes[0]["kids"]) != 1:
            raise Refused("TYPE_CHANGED", "列表类型/条目数变化" + HINT_MANUAL)
        rich, checked = _list_item_fields(nodes[0]["kids"][0], t, src=src)
        f = {"rich_text": rich}
        if t == "to_do":
            f["checked"] = checked
        if t == "numbered_list_item":
            f["number"] = int(nodes[0]["tok"].attrGet("start") or 1)
        return f
    if t in ("quote", "callout"):
        if t == "callout" and not body:
            return {"rich_text": []}
        if kinds != ["blockquote_open"]:
            raise Refused("TYPE_CHANGED", HINT_MANUAL)
        q = nodes[0]["kids"]
        if q and q[0]["tok"].type == "paragraph_open":
            first = q[0]["kids"][0]["tok"].children or []
            if first and first[0].type == "text" and first[0].content.startswith("[!"):
                raise _unsupported("callout")
        if not q:
            return {"rich_text": []}
        rich = _paras_rich(q, "text", (0, len(src)))
        if rich is None:
            raise Refused("TYPE_CHANGED", "引用内出现了子块/其他结构；" + HINT_MANUAL)
        return {"rich_text": rich}
    if t == "code":
        if kinds == ["fence"]:
            tok = nodes[0]["tok"]
            lang = _lang_from_info(tok.info)
        elif kinds == ["code_block"]:
            tok = nodes[0]["tok"]
            lang = "plain text"
        else:
            raise Refused("TYPE_CHANGED", HINT_MANUAL)
        c = tok.content[:-1] if tok.content.endswith("\n") else tok.content
        return {"code_text": c, "language": lang}
    if t == "equation":
        if kinds != ["md2b_math_block"]:
            raise Refused("TYPE_CHANGED", HINT_MANUAL)
        return {"expression": nodes[0]["tok"].content}
    if t == "divider":
        if kinds != ["hr"]:
            raise Refused("TYPE_CHANGED", HINT_MANUAL)
        return {}
    raise Refused("UNSUPPORTED_REGION", f"type:{t}")


# ---------------------------------------------------------------------------------------------- block level (exporter grammar)
def _strip_prefix(lines, prefix):
    pr = prefix.rstrip()
    out = []
    for ln in lines:
        if ln.startswith(prefix):
            out.append(ln[len(prefix):])
        elif ln.rstrip() == pr:
            out.append("")
        else:
            raise Refused("STRUCTURE_CHANGED", "行首缩进/引用前缀被改动；" + HINT_MANUAL)
    return out


def _fast_text(lines, table=False, accept=None):
    return _fast_inline("\n".join(_unesc_line(ln) for ln in lines), table, accept=accept)


def _accept(prefer, key, mode, idx=None):
    """Predicate 'agrees with the CommonMark reading' for _fast_inline (None when there is no reading)."""
    if not prefer or key not in prefer:
        return None
    want = prefer[key] if idx is None else (prefer[key][idx] if idx < len(prefer[key]) else None)
    if want is None:
        return None
    return lambda segs: _rich_eq(segs, want, mode, "visible")


def _fast_fields(t, body, prefer=None):
    """Exporter-grammar reading of a block's own lines (prefix stripped) -> fields, or None.
    `prefer`: the CommonMark reading of the same lines; among several exact parses the agreeing one wins."""
    if not body:
        return None
    acc = _accept(prefer, "rich_text", MODE_OF.get(t, "inline"))
    if t == "paragraph":
        if len(body) >= 3 and re.fullmatch(r"`{3,}", body[0]) and body[-1] == body[0]:
            return {"rich_text": [_text_seg("\n".join(body[1:-1]), {"code": True})]}
        if body == [""]:
            return {"rich_text": []}
        r = _fast_text(body, accept=acc)
        return None if r is None else {"rich_text": r}
    if t in HEADINGS:
        lvl = int(t[-1])
        if body == [""]:
            return {"rich_text": []}
        if len(body) != 1 or not body[0].startswith("#" * lvl + " "):
            return None
        r = _fast_inline(body[0][lvl + 1:], accept=acc)
        return None if r is None else {"rich_text": r}
    if t in LIST_TYPES:
        first = body[0]
        extra = {}
        if t == "numbered_list_item":
            m = re.match(r"^(\d{1,9})\. ", first) or re.fullmatch(r"(\d{1,9})\.", first)
            if not m:
                return None
            marker = m.group(1) + ". "
            extra["number"] = int(m.group(1))
        elif t == "to_do":
            m = re.match(r"^- \[( |x)\] ", first) or re.fullmatch(r"- \[( |x)\]", first)
            if not m:
                return None
            marker = f"- [{m.group(1)}] "
            extra["checked"] = m.group(1) == "x"
        else:
            if not (first.startswith("- ") or first == "-"):
                return None
            marker = "- "
        if first == marker.rstrip():
            if len(body) == 1:
                return dict(extra, rich_text=[])
            return None
        t0 = first[len(marker):]
        if t0.startswith("\\") and _THEMATIC.match((marker + t0[1:]).strip()):
            t0 = t0[1:]
        cont = " " * max(4, len(marker))
        text = [t0]
        for ln in body[1:]:
            if ln == "":
                text.append("")
            elif ln.startswith(cont):
                text.append(ln[len(cont):])
            else:
                return None
        r = _fast_text(text, accept=acc)
        return None if r is None else dict(extra, rich_text=r)
    if t in ("quote", "callout"):
        ls = body[1:] if t == "callout" else body
        text = []
        for ln in ls:
            if ln == ">":
                text.append("")
            elif ln.startswith("> "):
                text.append(ln[2:])
            else:
                return None
        if t == "quote" and text == [""]:
            return {"rich_text": []}
        if not text:
            return {"rich_text": []} if t == "callout" else None
        r = _fast_text(text, accept=acc)
        return None if r is None else {"rich_text": r}
    if t == "code":
        m = re.match(r"^`{3,}", body[0])
        if not m or len(body) < 2 or body[-1] != m.group(0):
            return None
        lang = body[0][m.end():]
        if lang not in _LANG_INV:
            return None
        return {"code_text": "\n".join(body[1:-1]), "language": _LANG_INV[lang]}
    if t == "equation":
        if len(body) < 2 or body[0] != "$$" or body[-1] != "$$":
            return None
        return {"expression": "\n".join(body[1:-1])}
    if t == "divider":
        return {} if body == ["---"] else None
    if t == "table_row":
        cells_src = _split_row(body[0])
        if cells_src is None:
            return None
        cells = []
        for i, c in enumerate(cells_src):
            r = _fast_inline(c, table=True, accept=_accept(prefer, "cells", "cell", i)) if c else []
            if r is None:
                return None
            cells.append(r)
        return {"cells": cells}
    return None


def _block_with(old_block, fields):
    """Snapshot-shaped block = old block with writable fields replaced (children cut)."""
    t = old_block["type"]
    b = copy.deepcopy(old_block)
    b["has_children"] = False
    v = b.setdefault(t, {})
    if "rich_text" in fields:
        v["rich_text"] = _internal(fields["rich_text"])
    if "checked" in fields:
        v["checked"] = bool(fields["checked"])
    if t == "code":
        v["rich_text"] = [_text_seg(fields["code_text"])] if fields["code_text"] else []
        v["language"] = fields["language"]
    if t == "equation":
        v["expression"] = fields["expression"]
    if t == "table_row":
        v["cells"] = [_internal(c) for c in fields["cells"]]
    return b


def _render_fields(ex, old_block, fields, prefix):
    return render_own_lines(ex, _block_with(old_block, fields), prefix, fields.get("number", 1))


# ---------------------------------------------------------------------------------------------- static classification
_STATIC_ORDER = ("type", "paragraph_with_children", "toggle_heading_with_children", "caption", "code_annotated",
                 "code_language", "color", "mention", "link_to_notion", "anchor_link", "relative_link",
                 "heading_newline", "limit")


def _rich_arrays(block):
    t = block.get("type")
    v = block.get(t) if isinstance(block.get(t), dict) else {}
    out = []
    if isinstance(v.get("rich_text"), list):
        out.append(v["rich_text"])
    if isinstance(v.get("caption"), list) and v["caption"]:
        out.append(v["caption"])
    if t == "table_row":
        out.extend(c for c in (v.get("cells") or []) if isinstance(c, list))
    return out


def static_reasons(block, children=None):
    """Static X reasons (type, structure visible on the block itself, inline content). Ancestor-based reasons
    (column/synced subtree, owner_page_id != notion_id, D-11) are the exporter's block_lines job."""
    t = block.get("type")
    v = block.get(t) if isinstance(block.get(t), dict) else {}
    rs = []
    if t not in R_TYPES:
        rs.append(f"type:{t}")
        return rs
    has_kids = bool(children) if children is not None else bool(block.get("has_children"))
    if t == "paragraph" and has_kids:
        rs.append("paragraph_with_children")
    if t in HEADINGS and v.get("is_toggleable") and has_kids:
        rs.append("toggle_heading_with_children")
    if t == "code":
        if v.get("caption"):
            rs.append("caption")
        for s in v.get("rich_text") or []:
            a = s.get("annotations") or {}
            if (s.get("type") != "text" or _seg_link(s) or any(a.get(k) for k in ANN_KEYS)
                    or (a.get("color") or "default") != "default"):
                rs.append("code_annotated")
                break
        if (v.get("language") or "plain text") not in NOTION_LANGUAGES:
            rs.append("code_language")
    for arr in ([] if t == "code" else _rich_arrays(block)):
        for s in arr:
            a = s.get("annotations") or {}
            if (a.get("color") or "default") != "default":
                rs.append("color")
            st = s.get("type")
            if st == "mention":
                rs.append("mention_" + str((s.get("mention") or {}).get("type")))
            elif st == "text":
                u = _seg_link(s)
                if u:
                    u2 = u.strip()
                    if _is_notion_url(u2):
                        rs.append("link_to_notion")
                    elif u2.startswith("#"):
                        rs.append("anchor_link")
                    elif not re.match(r"(?i)^(https?://|mailto:)\S", u2):
                        rs.append("relative_link")
                    elif _u16(u2) > MAX_TEXT:
                        rs.append("limit:link_url")
            elif st == "equation":
                if _u16((s.get("equation") or {}).get("expression", "")) > MAX_EQUATION:
                    rs.append("limit:equation")
            else:
                rs.append(f"segment_{st}")
    if t in HEADINGS and "\n" in _plain(v.get("rich_text")):
        rs.append("heading_newline")
    if t == "equation" and _u16(v.get("expression") or "") > MAX_EQUATION:
        rs.append("limit:equation")
    seen = []
    for r in rs:
        if r not in seen:
            seen.append(r)

    def prio(r):
        for i, p in enumerate(_STATIC_ORDER):
            if r.startswith(p):
                return i
        return len(_STATIC_ORDER)
    return sorted(seen, key=prio)


# ---------------------------------------------------------------------------------------------- klass
def _analyse(ex, old_block, own_lines, prefix):
    """Both readings of `own_lines` for block type of `old_block`:
    returns (fast_fields_or_None, cm_fields_or_None, cm_error_or_None)."""
    t = old_block["type"]
    body = _strip_prefix(own_lines, prefix)
    cm_body = body
    if t == "callout":
        header = render_own_lines(ex, _block_with(old_block, {"rich_text": []}), prefix)[0]
        if not own_lines or own_lines[0] != header:
            raise Refused("TYPE_CHANGED", "标注块的首行（类型/图标）被改动；图标与颜色只能在 Notion 中修改")
        cm_body = body[1:]
    if t == "table_row":
        width = len((old_block.get("table_row") or {}).get("cells") or [])
        if len(body) == 2:
            if body[1] != _table_sep(width).strip():
                raise Refused("STRUCTURE_CHANGED", "表格分隔行被改动；" + HINT_MANUAL)
            body = cm_body = body[:1]
            own_lines = own_lines[:1]
        if len(body) != 1:
            raise Refused("STRUCTURE_CHANGED", "表格行数变化；" + HINT_MANUAL)
        cells = _split_row(body[0])
        if cells is None or len(cells) != width:
            raise Refused("STRUCTURE_CHANGED", f"表格列数变化（应为 {width} 列）；" + HINT_MANUAL)
    try:
        cm, err = _cm_fields(t, cm_body, old_block), None
    except Refused as e:
        cm, err = None, e
    fast = _fast_fields(t, body, prefer=cm)
    if fast is not None:
        try:
            if _render_fields(ex, old_block, fast, prefix) != list(own_lines):
                fast = None
        except Exception:
            fast = None
    return fast, cm, err, body, cm_body


def klass(exporter, block, children=None):
    """("R", None) when write-back may UPDATE this block, else ("X", reason).
    R requires: no static X reason, and block JSON -> exporter Markdown -> block JSON equal on writable fields
    (strict canonical form, D-12, no fragment reuse) and the CommonMark reading of that Markdown agreeing."""
    rs = static_reasons(block, children)
    if rs:
        return "X", rs[0]
    t = block["type"]
    try:
        lines = render_own_lines(exporter, block, "", 1)
        fast, cm, err, _, _ = _analyse(exporter, block, lines, "")
    except Refused as e:
        return "X", "roundtrip:" + e.reason
    except Exception as e:  # renderer failure on odd JSON: never writable
        return "X", "render_error:" + type(e).__name__
    if fast is None:
        return "X", "roundtrip"
    if not _fields_eq(t, _fields_of_block(block), fast, "equiv"):
        return "X", "roundtrip"
    if cm is None:
        return "X", "md_divergent:" + (err.reason if err else "?")
    if not _fields_eq(t, fast, cm, "visible"):
        return "X", "md_divergent"
    if _has_long_segment(fast):  # the API split must not change the rendering
        try:
            _request_body(exporter, block, fast, "")
        except Refused as e:
            return "X", "limit:" + e.reason.split(":", 1)[-1].lower()
    return "R", None


# ---------------------------------------------------------------------------------------------- fragment reuse
def _atoms(rich, mode=None):
    """Per-character comparison keys for fragment reuse: whitespace ignores bold/italic/strike/underline; in
    text/list blocks a NBSP and a space in a line-start run are the same key (esc_line_start renders both as
    NBSP and the exporter grammar reads them back as spaces), so untouched NBSP text is re-sent as it was."""
    out = []
    lead_eq = mode in ("text", "list")
    at_start = True
    for si, s in enumerate(rich or []):
        if s.get("type") == "text":
            a = s.get("annotations") or {}
            key = tuple(bool(a.get(k)) for k in ANN_KEYS) + (a.get("color") or "default", _seg_link(s))
            plain = not key[4] and key[6] is None
            for ci, ch in enumerate(_seg_text(s)):
                if lead_eq and at_start and plain and ch in "  ":
                    k = (" ", "line_start")
                elif ch.isspace():
                    k = (ch, (False, False, False, False, key[4], key[5], key[6]))
                else:
                    k = (ch, key)
                at_start = ch == "\n" or (at_start and plain and ch in " \t ")
                out.append((k, si, ci))
        else:
            at_start = False
            out.append(((s.get("type"), json.dumps(_internal([s])[0].get(s.get("type")), sort_keys=True)), si, None))
    return out


def _regroup(picks, srcs):
    """picks: list of (source_index, seg_index, char_index|None) -> segments (slices of the sources)."""
    segs = []
    cur = None
    for src, si, ci in picks:
        seg = srcs[src][si]
        if ci is None:
            segs.append(copy.deepcopy(seg))
            cur = None
            continue
        if cur and cur[0] == src and cur[1] == si and cur[3] == ci:
            cur[2].append(_seg_text(seg)[ci])
            cur[3] = ci + 1
            continue
        cur = [src, si, [_seg_text(seg)[ci]], ci + 1]
        segs.append(cur)
    out = []
    for g in segs:
        if isinstance(g, list):
            base = copy.deepcopy(srcs[g[0]][g[1]])
            c = "".join(g[2])
            base["text"] = dict(base.get("text") or {}, content=c)
            base["plain_text"] = c
            out.append(base)
        else:
            out.append(g)
    return out


def _reuse(old_rich, new_rich, mode=None):
    """Keep the old fragments (annotations, link, boundaries) for the unchanged prefix/suffix (R-052)."""
    a, b = _atoms(old_rich, mode), _atoms(new_rich, mode)
    p = 0
    while p < len(a) and p < len(b) and a[p][0] == b[p][0]:
        p += 1
    s = 0
    while s < len(a) - p and s < len(b) - p and a[len(a) - 1 - s][0] == b[len(b) - 1 - s][0]:
        s += 1
    picks = [(0, x[1], x[2]) for x in a[:p]] + [(1, x[1], x[2]) for x in b[p:len(b) - s]] + \
            [(0, x[1], x[2]) for x in a[len(a) - s:]]
    return _regroup(picks, [_internal(old_rich), _internal(new_rich)])


# ---------------------------------------------------------------------------------------------- limits
def _u16(s):
    return len((s or "").encode("utf-16-le")) // 2


def _split_text(s, limit):
    out, cur, n = [], [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > limit and cur:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += w
    if cur:
        out.append("".join(cur))
    return out


def _cut_max(c, start, limit):
    """Largest end index such that c[start:end] fits `limit` UTF-16 units."""
    n, k = 0, start
    while k < len(c):
        w = 2 if ord(c[k]) > 0xFFFF else 1
        if n + w > limit:
            break
        n += w
        k += 1
    return k


def _split_render_safe(seg, limit, table):
    """Cut points for a long plain text segment chosen so that the exporter renders the pieces exactly like
    the whole (no cut inside a raw URL, between '=' '=', before a '#' after whitespace, …). Annotated or
    linked segments get one Markdown wrapper per piece whatever the cut: plain cuts (caller re-checks)."""
    c = _seg_text(seg)
    ann = seg.get("annotations") or {}
    if any(ann.get(k) for k in ANN_KEYS) or _seg_link(seg):
        return _split_text(c, limit)

    def r(x):
        return _rt([_text_seg(x)], table)

    pieces, start = [], 0
    while _u16(c[start:]) > limit:
        cut = _cut_max(c, start, limit)
        whole = r(c[start:])
        best = None
        for k in range(cut, max(start, cut - 400), -1):
            if r(c[start:k]) + r(c[k:]) == whole:
                best = k
                break
        best = best or cut
        pieces.append(c[start:best])
        start = best
    pieces.append(c[start:])
    return pieces


def split_for_api(rich, limit=MAX_TEXT, table=False, render_safe=True):
    """Split text segments longer than `limit` (UTF-16 units, at code-point boundaries) into several segments
    with the same annotations/link; refuse what cannot be split (link URL > limit, equation > 1,000) and
    arrays that still exceed 100 segments after merging identical neighbours. Shape is preserved.
    With render_safe (default) plain-text cut points are chosen so that the exporter's rendering is unchanged
    (code-block bodies pass render_safe=False: they are rendered verbatim anyway)."""
    out = []
    for s in rich or []:
        t = s.get("type")
        if t == "text":
            u = _seg_link(s)
            if u and _u16(u) > limit:
                raise Refused("LIMIT:link_url", f"链接 URL 超过 {limit} 字符，Notion API 不接受")
            c = _seg_text(s)
            if _u16(c) <= limit:
                out.append(s)
                continue
            for piece in (_split_render_safe(s, limit, table) if render_safe else _split_text(c, limit)):
                s2 = copy.deepcopy(s)
                s2["text"]["content"] = piece
                if "plain_text" in s2:
                    s2["plain_text"] = piece
                out.append(s2)
        elif t == "equation":
            if _u16((s.get("equation") or {}).get("expression", "")) > MAX_EQUATION:
                raise Refused("LIMIT:equation", f"公式超过 {MAX_EQUATION} 字符，Notion API 不接受")
            out.append(s)
        else:
            out.append(s)
    if len(out) > MAX_SEGMENTS:
        merged = []
        for s in out:
            if (merged and s.get("type") == "text" and merged[-1].get("type") == "text"
                    and (merged[-1].get("annotations") or {}) == (s.get("annotations") or {})
                    and _seg_link(merged[-1]) == _seg_link(s)
                    and _u16(_seg_text(merged[-1]) + _seg_text(s)) <= limit):
                m = copy.deepcopy(merged[-1])
                m["text"]["content"] = _seg_text(m) + _seg_text(s)
                if "plain_text" in m:
                    m["plain_text"] = m["text"]["content"]
                merged[-1] = m
            else:
                merged.append(s)
        if len(merged) > MAX_SEGMENTS:
            raise Refused("LIMIT:segments", f"一个块的格式片段超过 {MAX_SEGMENTS} 个，Notion API 不接受；请拆分段落")
        out = merged
    return out


def block_count(children):
    """Number of block elements in a children list (recursive)."""
    n = 0
    for b in children or []:
        n += 1
        t = b.get("type")
        n += block_count(((b.get(t) or {}) if isinstance(b.get(t), dict) else {}).get("children") or [])
    return n


def nesting_depth(children):
    """0 for a flat list, 1 if some block has children, 2 if some child has children, …"""
    d = 0
    for b in children or []:
        t = b.get("type")
        kids = ((b.get(t) or {}) if isinstance(b.get(t), dict) else {}).get("children") or []
        if kids:
            d = max(d, 1 + nesting_depth(kids))
    return d


def _json_size(obj):
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def check_children_request(children):
    """Raise Refused unless `children` fits one append request (≤100 top-level, ≤2 nesting levels,
    ≤1,000 elements, ≤500 KB)."""
    if len(children) > MAX_CHILDREN:
        raise Refused("LIMIT:children", f"一次请求最多 {MAX_CHILDREN} 个子块")
    if nesting_depth(children) > MAX_NEST:
        raise Refused("LIMIT:nesting", f"一次请求最多 {MAX_NEST} 层嵌套")
    if block_count(children) > MAX_ELEMENTS:
        raise Refused("LIMIT:elements", f"一次请求最多 {MAX_ELEMENTS} 个块元素")
    if _json_size({"children": children}) > MAX_BYTES:
        raise Refused("LIMIT:bytes", f"请求体超过 {MAX_BYTES} 字节")


def chunk_children(children):
    """Split a list of new top-level blocks into consecutive append requests that each satisfy
    check_children_request (order preserved). A single block that alone exceeds a limit -> Refused."""
    batches, cur = [], []
    for b in children:
        check_children_request([b])
        cand = cur + [b]
        try:
            check_children_request(cand)
            cur = cand
        except Refused:
            batches.append(cur)
            cur = [b]
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------------------------------------- inline API
def _validate_rich(rich):
    for s in rich or []:
        if s.get("type") == "text" and _seg_link(s):
            _check_url(_seg_link(s))
        if s.get("type") == "mention":
            raise _unsupported("wikilink")


def parse_inline(md, *, table=False, mode="inline", exporter=None):
    """One rich_text rendering (no block syntax) -> (rich_text, normalized).
    Exporter-canonical input -> (exporter-grammar parse, False); CommonMark-equivalent input -> (CommonMark
    reading, True) after the fixpoint/HTML checks; else Refused. Segments are render-compatible (they carry
    plain_text/href); use request_rich(split_for_api(...)) for a request body."""
    def cm_read(s):
        # a GFM table cell reaches the inline parser with every '\|' already turned into '|' (escapedSplit)
        src = s.replace("\\|", "|") if table else s
        toks = _MD.parseInline(src, {})
        return src, _cm_inline((toks[0].children if toks else []) or [], table, mode)

    src_u, cm = cm_read(md)

    def agrees(segs):
        return _rich_eq(segs, cm, mode, "visible")

    fast = _fast_inline(md, table, accept=agrees)
    if fast is not None and agrees(fast):
        _validate_rich(fast)
        return fast, False
    c = _rt(cm, table, exporter)
    f2 = _fast_inline(c, table, accept=agrees)
    if f2 is None or not _rich_eq(f2, cm, mode, "equiv"):
        raise _unsupported("ambiguous")
    src_c, cm2 = cm_read(c)
    if not _rich_eq(cm2, cm, mode, "visible") or _html(src_u, inline=True) != _html(src_c, inline=True):
        raise Refused("REPARSE_MISMATCH", "规范化后的 Markdown 与原文在 Obsidian 中显示不同")
    _validate_rich(cm)
    return cm, True


# ---------------------------------------------------------------------------------------------- UPDATE
def _update_body(t, old_block, fields):
    v = {}
    old_v = old_block.get(t) or {}
    if t in TEXT_TYPES:
        new = _internal(fields["rich_text"])
        reused = _reuse(old_v.get("rich_text") or [], new, MODE_OF.get(t))
        v["rich_text"] = request_rich(split_for_api(_merge_seams(reused)))
        if t == "to_do":
            v["checked"] = bool(fields["checked"])
    elif t == "code":
        txt = fields["code_text"]
        v["rich_text"] = request_rich(split_for_api([_text_seg(txt)], render_safe=False)) if txt else []
        v["language"] = fields["language"]
    elif t == "equation":
        if _u16(fields["expression"]) > MAX_EQUATION:
            raise Refused("LIMIT:equation", f"公式超过 {MAX_EQUATION} 字符，Notion API 不接受")
        v["expression"] = fields["expression"]
    elif t == "table_row":
        old_cells = old_v.get("cells") or []
        v["cells"] = [request_rich(split_for_api(_merge_seams(_reuse(oc, _internal(nc), "cell")), table=True))
                      for oc, nc in zip(old_cells, fields["cells"])]
    return {t: v}


def _merge_seams(segs):
    return [s for s in segs if not (s.get("type") == "text" and _seg_text(s) == "")]


def apply_update(old_block, body):
    """Block JSON after a PATCH with `body` (render-compatible; for the planner's re-render oracle)."""
    t = old_block["type"]
    b = copy.deepcopy(old_block)
    v = b.setdefault(t, {})
    for k, val in (body.get(t) or {}).items():
        if k == "rich_text":
            v[k] = _internal(val)
        elif k == "cells":
            v[k] = [_internal(c) for c in val]
        else:
            v[k] = copy.deepcopy(val)
    return b


def rebuild_block(exporter, old_block, own_lines, prefix):
    """UPDATE body (`{type: {...}}` for PATCH /v1/blocks/{id}) for an R block whose own lines changed.
    Same type only; returns (body, normalized). Raises Refused (UNSUPPORTED_REGION when the old block is X,
    TYPE_CHANGED / STRUCTURE_CHANGED, UNSUPPORTED_INLINE:<what>, REPARSE_MISMATCH, LIMIT:<what>)."""
    t = old_block.get("type")
    if t not in R_TYPES:
        raise Refused("UNSUPPORTED_REGION", f"type:{t}")
    k, why = klass(exporter, old_block)
    if k != "R":
        raise Refused("UNSUPPORTED_REGION", why or "")
    own_lines = [str(x) for x in own_lines]
    if not own_lines:
        raise Refused("STRUCTURE_CHANGED", "块的行被全部删除（删除块是单独的 B 项）")
    fast, cm, err, body, cm_body = _analyse(exporter, old_block, own_lines, prefix)
    if fast is not None and cm is not None and _fields_eq(t, fast, cm, "visible"):
        fields, normalized = fast, False
    elif cm is not None:
        if t == "numbered_list_item" and "number" not in cm:
            cm["number"] = 1
        c_lines = _render_fields(exporter, old_block, cm, prefix)
        try:
            f2, cm2, err2, c_body, c_cm_body = _analyse(exporter, old_block, c_lines, prefix)
        except Refused:
            raise _unsupported("ambiguous")
        if f2 is None or not _fields_eq(t, f2, cm, "equiv"):
            raise _unsupported("ambiguous")
        if cm2 is None or not _fields_eq(t, cm2, cm, "visible"):
            raise Refused("REPARSE_MISMATCH", "规范化后的 Markdown 与原文在 Obsidian 中显示不同")
        src_u = [body[0], _table_sep(len(cm["cells"]))] if t == "table_row" else cm_body
        src_c = [c_body[0], _table_sep(len(cm["cells"]))] if t == "table_row" else c_cm_body
        if _html(src_u) != _html(src_c):
            raise Refused("REPARSE_MISMATCH", "规范化后的 Markdown 与原文在 Obsidian 中显示不同")
        fields, normalized = cm, True
    elif err is not None:
        raise err
    else:
        raise Refused("REPARSE_MISMATCH", "无法解析改动后的行")
    for r in [fields.get("rich_text")] + list(fields.get("cells") or []):
        _validate_rich(r)
    return _request_body(exporter, old_block, fields, prefix), normalized


def _has_long_segment(fields):
    arrays = [fields.get("rich_text") or []] + list(fields.get("cells") or [])
    return any(s.get("type") == "text" and _u16(_seg_text(s)) > MAX_TEXT for a in arrays for s in a)


def _request_body(ex, old_block, fields, prefix):
    """PATCH body for `fields` (fragment reuse, API splitting); the body must render exactly like the fields,
    else reuse is dropped; if it still differs (a >2,000-char formatted segment whose pieces render
    differently) -> Refused LIMIT:split / REPARSE_MISMATCH."""
    t = old_block["type"]
    out = _update_body(t, old_block, fields)
    want = _render_fields(ex, old_block, fields, prefix)
    got = render_own_lines(ex, apply_update(old_block, out), prefix, fields.get("number", 1))
    if got == want:
        return out
    v = out[t]
    if "rich_text" in v and t in TEXT_TYPES:
        v["rich_text"] = request_rich(split_for_api(_internal(fields["rich_text"])))
    if "cells" in v:
        v["cells"] = [request_rich(split_for_api(_internal(c), table=True)) for c in fields["cells"]]
    got = render_own_lines(ex, apply_update(old_block, out), prefix, fields.get("number", 1))
    if got == want:
        return out
    if _has_long_segment(fields):
        raise Refused("LIMIT:split", f"超过 {MAX_TEXT} 字符的带格式片段按 Notion API 限制拆分后渲染会改变；请在 Notion 中修改")
    raise Refused("REPARSE_MISMATCH", "请求体重新渲染与解析结果不一致")


# ---------------------------------------------------------------------------------------------- APPEND / new blocks
def _new_block(t, fields, kids=None):
    return {"type": t, "fields": fields, "kids": kids or []}


def _cm_blocks(nodes, depth, src=None):
    """markdown-it block tree -> list of new blocks {type, fields, kids}. `src`: the parsed source lines
    (heading leading whitespace, nested-looking list lines)."""
    _check_node_kinds(nodes)
    out = []
    for n in nodes:
        tok = n["tok"]
        ty = tok.type
        if ty == "html_block":
            continue
        if ty == "paragraph_open":
            out.append(_new_block("paragraph", {"rich_text": _paras_rich([n], "text")}))
        elif ty == "heading_open":
            lvl = int(tok.tag[1:])
            if lvl > 3:
                raise Refused(f"UNSUPPORTED_BLOCK:heading_{lvl}", "新建标题只支持 # / ## / ###（Notion API heading_1–3）")
            inline = n["kids"][0]["tok"]
            lead = (_heading_lead(src[tok.map[0]], lvl)
                    if src is not None and tok.map and tok.map[0] < len(src) and tok.markup.startswith("#") else [])
            out.append(_new_block(f"heading_{lvl}",
                                  {"rich_text": _merge(lead + _cm_inline(inline.children or [], False, "heading"))}))
        elif ty in ("bullet_list_open", "ordered_list_open"):
            start = int(tok.attrGet("start") or 1) if ty == "ordered_list_open" else None
            for i, item in enumerate(n["kids"]):
                kids = item["kids"]
                paras = []
                for k in kids:
                    if k["tok"].type != "paragraph_open":
                        break
                    paras.append(k)
                rest = kids[len(paras):]
                _check_item_lines(item, paras, src, "UNSUPPORTED_BLOCK:ambiguous_nesting")
                checked = None
                if paras:
                    inline = paras[0]["kids"][0]["tok"]
                    if ty == "bullet_list_open":
                        checked, ch2 = _split_checkbox(inline.children or [])
                        if checked is not None:
                            inline = copy.copy(inline)
                            inline.children = ch2
                            paras = [dict(paras[0], kids=[{"tok": inline, "kids": []}])] + paras[1:]
                span = (item["tok"].map[0], None) if item["tok"].map else None
                rich = _paras_rich(paras, "list", span) if paras else []
                if ty == "ordered_list_open":
                    t, f = "numbered_list_item", {"rich_text": rich, "number": start + i}
                elif checked is not None:
                    t, f = "to_do", {"rich_text": rich, "checked": checked}
                else:
                    t, f = "bulleted_list_item", {"rich_text": rich}
                child_blocks = []
                if rest:
                    if depth + 1 > MAX_NEST:
                        raise Refused("UNSUPPORTED_BLOCK:nesting_depth",
                                      f"新内容嵌套超过 {MAX_NEST + 1} 层；请减少缩进层级或分次写回")
                    child_blocks = _cm_blocks(rest, depth + 1, src)
                out.append(_new_block(t, f, child_blocks))
        elif ty == "blockquote_open":
            q = n["kids"]
            if q and q[0]["tok"].type == "paragraph_open":
                first = q[0]["kids"][0]["tok"].children or []
                if first and first[0].type == "text" and first[0].content.startswith("[!"):
                    raise _unsupported("callout")
            rich = _paras_rich(q, "text", tuple(tok.map) if tok.map else None) if q else []
            if rich is None:
                raise Refused("UNSUPPORTED_BLOCK:quote_children", "新建引用块内只支持文字段落")
            out.append(_new_block("quote", {"rich_text": rich}))
        elif ty == "fence":
            c = tok.content[:-1] if tok.content.endswith("\n") else tok.content
            out.append(_new_block("code", {"code_text": c, "language": _lang_from_info(tok.info)}))
        elif ty == "code_block":
            c = tok.content[:-1] if tok.content.endswith("\n") else tok.content
            out.append(_new_block("code", {"code_text": c, "language": "plain text"}))
        elif ty == "hr":
            out.append(_new_block("divider", {}))
        elif ty == "md2b_math_block":
            out.append(_new_block("equation", {"expression": tok.content}))
        else:
            raise Refused(f"UNSUPPORTED_BLOCK:{ty}", "无法新建这种块")
    return out


def _snapshot_tree(blocks, parent, overlay, counter):
    lst = []
    for nb in blocks:
        counter[0] += 1
        bid = f"md2blocks-new-{counter[0]:05d}"
        b = _block_with({"object": "block", "id": bid, "type": nb["type"], nb["type"]: {}}, nb["fields"])
        b["has_children"] = bool(nb["kids"])
        lst.append(b)
        if nb["kids"]:
            _snapshot_tree(nb["kids"], bid, overlay, counter)
    overlay[parent] = lst
    return lst


def _render_new(ex, blocks, prefix):
    overlay = {}
    root = "md2blocks-new-root"
    _snapshot_tree(blocks, root, overlay, [0])
    with _sandbox(ex, overlay) as r:
        return [str(x) for x in r.render_blocks(root, RENDER_TS, prefix)]


_RENUM = re.compile(r"^((?:\s|>)*)\d{1,9}\. ")


def _renum(lines):
    return [_RENUM.sub(lambda m: m.group(1) + "1. ", ln) for ln in lines]


def _blocks_eq(a, b):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x["type"] != y["type"] or not _fields_eq(x["type"], x["fields"], y["fields"], "visible"):
            return False
        if not _blocks_eq(x["kids"], y["kids"]):
            return False
    return True


def _check_new_roundtrip(ex, blocks):
    """Every new block must read back (exporter grammar) to what will be written (else a later re-export
    would not match what Obsidian showed)."""
    for nb in blocks:
        t = nb["type"]
        b = _block_with({"object": "block", "id": "md2blocks-check", "type": t, t: {}}, nb["fields"])
        lines = render_own_lines(ex, b, "", nb["fields"].get("number", 1))
        fast = _fast_fields(t, lines, prefer=nb["fields"])
        if fast is None or render_own_lines(ex, _block_with(b, fast), "", fast.get("number", 1)) != lines \
                or not _fields_eq(t, fast, nb["fields"], "equiv"):
            raise _unsupported("ambiguous")
        _validate_rich(nb["fields"].get("rich_text"))
        _check_new_roundtrip(ex, nb["kids"])


def _req_block(nb):
    t = nb["type"]
    f = nb["fields"]
    v = {}
    if t in TEXT_TYPES:
        v["rich_text"] = request_rich(split_for_api(_internal(f["rich_text"])))
        if t == "to_do":
            v["checked"] = bool(f["checked"])
    elif t == "code":
        v["rich_text"] = request_rich(split_for_api([_text_seg(f["code_text"])], render_safe=False)) if f["code_text"] else []
        v["language"] = f["language"]
    elif t == "equation":
        if _u16(f["expression"]) > MAX_EQUATION:
            raise Refused("LIMIT:equation", f"公式超过 {MAX_EQUATION} 字符，Notion API 不接受")
        v["expression"] = f["expression"]
    if nb["kids"]:
        v["children"] = [_req_block(k) for k in nb["kids"]]
    return {"object": "block", "type": t, t: v}


def parse_new_blocks(exporter, lines, base_prefix):
    """New Markdown lines (APPEND / new page body, all lines carry `base_prefix`) -> (request blocks,
    normalized). Types: paragraph, heading_1..3, bulleted/numbered/to_do (≤ MAX_NEST nesting levels below the
    top), quote (text paragraphs only), code (+language), divider, equation. Raises Refused."""
    lines = [str(x) for x in lines]
    body = _strip_prefix(lines, base_prefix)
    lo, hi = 0, len(body)
    while lo < hi and not body[lo].strip():
        lo += 1
    while hi > lo and not body[hi - 1].strip():
        hi -= 1
    body = body[lo:hi]
    if not body:
        return [], False
    nodes = _parse_md(body)
    blocks = _cm_blocks(nodes, 0, _src_lines(body))
    if not blocks:
        raise Refused("REPARSE_MISMATCH", "没有可写入的内容")
    rendered = _render_new(exporter, blocks, base_prefix)
    exact = _renum(rendered) == _renum(lines[lo:hi])
    c_body = _strip_prefix(rendered, base_prefix)
    if not exact:
        if _html(body) != _html(c_body):
            raise Refused("REPARSE_MISMATCH", "规范化后的 Markdown 与原文在 Obsidian 中显示不同")
        blocks2 = _cm_blocks(_parse_md(c_body), 0, _src_lines(c_body))
        if not _blocks_eq(blocks, blocks2):
            raise Refused("REPARSE_MISMATCH", "规范化后的 Markdown 与原文在 Obsidian 中显示不同")
    _check_new_roundtrip(exporter, blocks)
    req = [_req_block(b) for b in blocks]
    for b in req:
        if nesting_depth([b]) > MAX_NEST:
            raise Refused("UNSUPPORTED_BLOCK:nesting_depth", f"新内容嵌套超过 {MAX_NEST + 1} 层")
    # the request itself (after API splitting) must render like the parsed blocks
    overlay, _ = snapshot_blocks(req, "md2blocks-req-root", "md2blocks-req")
    with _sandbox(exporter, overlay) as r:
        got = [str(x) for x in r.render_blocks("md2blocks-req-root", RENDER_TS, base_prefix)]
    if got != rendered:
        raise Refused("LIMIT:split", f"超过 {MAX_TEXT} 字符的带格式片段按 Notion API 限制拆分后渲染会改变；请拆短")
    return req, not exact


def snapshot_blocks(request_blocks, parent_id, id_prefix="new"):
    """Request-shaped new blocks -> ({parent_id: [snapshot-shaped blocks]}, flat id list), synthetic ids
    `<id_prefix>-<n>`; for the planner's render_page overlay."""
    overlay = {}
    ids = []

    def walk(lst, parent):
        out = []
        for rb in lst:
            t = rb["type"]
            bid = f"{id_prefix}-{len(ids) + 1:05d}"
            ids.append(bid)
            v = copy.deepcopy(rb.get(t) or {})
            kids = v.pop("children", None) or []
            if "rich_text" in v:
                v["rich_text"] = _internal(v["rich_text"])
            b = {"object": "block", "id": bid, "type": t, t: v, "has_children": bool(kids)}
            out.append(b)
            if kids:
                walk(kids, bid)
        overlay[parent] = out
    walk(request_blocks, parent_id)
    return overlay, ids
