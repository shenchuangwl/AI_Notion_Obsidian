#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for tools/md2blocks.py (contract §5.8, §2 write-back inline policy).

Fast and synthetic (no snapshot, no network). md2blocks is imported from tools/; it imports the module named
export_obsidian, so the tests run against whichever export_obsidian is importable (tools/ by default, or one
pre-loaded into sys.modules by a runner).

Run:  python3 -I -B -m unittest discover -s tools/tests -p 'test_md2blocks.py' -v
"""
import copy
import itertools
import os
import sys
import unittest
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import md2blocks as M  # noqa: E402
import export_obsidian as EO  # noqa: E402

PAGE_ID = "11111111-1111-4111-8111-111111111111"
NOTION_URL = "https://www.notion.so/Page-" + PAGE_ID.replace("-", "")


def T(content, link=None, **ann):
    seg = M._text_seg(content, ann, link)
    if "color" in ann:
        seg["annotations"]["color"] = ann["color"]
    return seg


def E(expr):
    return M._eq_seg(expr)


def MENTION(mt="page", **extra):
    m = {"type": mt, mt: extra or {"id": PAGE_ID}}
    return {"type": "mention", "mention": m, "annotations": M._ann(), "plain_text": "x", "href": None}


def blk(t, rich=None, bid="b1", **kw):
    v = {"rich_text": list(rich or []), "color": "default"}
    v.update(kw)
    return {"object": "block", "id": bid, "type": t, t: v, "has_children": False}


def code_blk(text, language="python", **kw):
    return blk("code", [T(text)] if text else [], language=language, caption=[], **kw)


def row_blk(cells):
    return {"object": "block", "id": "r1", "type": "table_row", "table_row": {"cells": cells}, "has_children": False}


def rt(rich, table=False):
    return M._rt(rich, table)


def brief(rich):
    """[(content, 'biscu' flags, link)] of request- or render-shaped rich text."""
    out = []
    for s in rich:
        a = s.get("annotations") or {}
        fl = "".join(k[0] for k in M.ANN_KEYS if a.get(k))
        if s["type"] == "text":
            out.append((s["text"]["content"], fl, M._seg_link(s)))
        elif s["type"] == "equation":
            out.append(("$" + s["equation"]["expression"] + "$", fl, None))
        else:
            out.append((s["type"], fl, None))
    return out


def reread(block, prefix="", list_num=1):
    """rebuild_block on the block's own rendering -> (body, normalized, lines)."""
    lines = M.render_own_lines(None, block, prefix, list_num)
    body, norm = M.rebuild_block(None, block, lines, prefix)
    return body, norm, lines


class InlineExporterGrammar(unittest.TestCase):
    """Exporter output parses back exactly (fast path, normalized=False)."""

    def roundtrip(self, rich, table=False, mode=None):
        md = rt(rich, table)
        segs, norm = M.parse_inline(md, table=table, mode=mode or ("cell" if table else "inline"))
        self.assertFalse(norm, md)
        self.assertEqual(rt(segs, table), md)
        self.assertEqual(M.canon_rich_text(segs, "cell" if table else "inline"),
                         M.canon_rich_text(rich, "cell" if table else "inline"), md)
        return md, segs

    def test_every_annotation_combination(self):
        keys = ("bold", "italic", "strikethrough", "underline")
        contents = ["abc", "two words", " margin ", "（括号）", "x*y_z", "a\nb", " "]
        n = 0
        for code in (False, True):
            for r in range(len(keys) + 1):
                for combo in itertools.combinations(keys, r):
                    for c in contents:
                        if code and (c.startswith(" ") or c.endswith(" ")):
                            continue  # code span edge spaces are stripped by CommonMark: covered in Klass tests
                        ann = {k: True for k in combo}
                        if code:
                            ann["code"] = True
                        self.roundtrip([T("pre "), T(c, **ann), T(" post")])
                        n += 1
        self.assertGreater(n, 150)

    def test_shapes_the_exporter_emits(self):
        cases = {
            "**b**": [T("b", bold=True)], "*i*": [T("i", italic=True)], "~~s~~": [T("s", strikethrough=True)],
            "<u>u</u>": [T("u", underline=True)], "<em>**bi**</em>": [T("bi", bold=True, italic=True)],
            "<strong>（x）</strong>": [T("（x）", bold=True)],
            "<u><del><em>**all**</em></del></u>": [T("all", bold=True, italic=True, strikethrough=True, underline=True)],
            "<strong>`c`</strong>": [T("c", bold=True, code=True)], "`c`": [T("c", code=True)],
            "``a`b``": [T("a`b", code=True)], "`` `x ``": [T("`x", code=True)], "`l1`\n`l2`": [T("l1\nl2", code=True)],
            "<u> **a** </u>": [T(" a ", bold=True, underline=True)],
        }
        for md, rich in cases.items():
            self.assertEqual(rt(rich), md)
            got, norm = M.parse_inline(md)
            self.assertFalse(norm, md)
            self.assertEqual(M.canon_rich_text(got, "inline"), M.canon_rich_text(rich, "inline"), md)

    def test_escapes(self):
        for c in ["a\\b", "a`b", "a*b", "a_b", "[x]", "$5", "~", "a==b", "50%%", "<div>", "#tag", "a #b",
                  "1<2 & ^ > ! { } ( ) + - . :", "x=y", "%", "<3", "a # b", "##x", "\\#", "~/path", "a]b"]:
            md, _ = self.roundtrip([T(c)])
        self.assertEqual(rt([T("a_b*c")]), "a\\_b\\*c")

    def test_table_cell_escapes(self):
        self.roundtrip([T("a|b")], table=True)
        self.roundtrip([T("l1\nl2")], table=True)
        self.roundtrip([T("x|y", code=True)], table=True)
        self.roundtrip([E("a|b")], table=True)
        self.assertEqual(rt([T("a|b\nc")], True), "a\\|b<br>c")

    def test_links(self):
        self.roundtrip([T("see", "https://example.com/a")])
        self.roundtrip([T("sp", "https://example.com/a b(c)")])  # angle-bracket destination
        self.roundtrip([T("bold link", "https://example.com/", bold=True)])
        self.roundtrip([T(" **a** ", "https://e.com/x", bold=True)])  # margins inside the link (coalesce)
        self.roundtrip([T(" aaa", "https://e.com/#", bold=True), T(" 字字", bold=True), T("，字")])
        self.roundtrip([T("mail", "mailto:a@b.c")])
        self.roundtrip([T("raw https://example.com/a_b*c text")])
        self.roundtrip([T("x", "https://e.com/1"), T("y", "https://e.com/2")])

    def test_equations(self):
        self.assertEqual(rt([E("a$b")]), "$a\\$b$")
        self.roundtrip([T("e = "), E("mc^2"), T(" ok")])
        self.roundtrip([E("a$b")])
        self.roundtrip([E("x_1 + y^2")])

    def test_raw_url_choice_points(self):
        self.roundtrip([T("https://x.io/a"), T("b", bold=True)])
        self.roundtrip([T("see https://x.io/p?q=1 and `c`")])

    def test_empty(self):
        self.assertEqual(M.parse_inline(""), ([], False))


class InlineUserTypedEquivalents(unittest.TestCase):
    """CommonMark equivalents are accepted, flagged normalized=True, and read with CommonMark semantics."""

    def check(self, md, want_md, want):
        segs, norm = M.parse_inline(md)
        self.assertTrue(norm, md)
        self.assertEqual(rt(segs), want_md, md)
        self.assertEqual(brief(M.request_rich(segs)), want, md)

    def test_equivalents(self):
        self.check("_it_", "*it*", [("it", "i", None)])
        self.check("__b__", "**b**", [("b", "b", None)])
        self.check("***bi***", "<em>**bi**</em>", [("bi", "bi", None)])
        self.check("**（x）**", "<strong>（x）</strong>", [("（x）", "b", None)])
        self.check("a_b", "a\\_b", [("a_b", "", None)])
        self.check("&amp; x", "& x", [("& x", "", None)])
        self.check("a &lt; b", "a < b", [("a < b", "", None)])
        self.check("x\\&y", "x&y", [("x&y", "", None)])
        self.check("<https://x.io>", "[https://x.io](https://x.io)", [("https://x.io", "", "https://x.io")])
        self.check("**a****b**", "**a\\*\\*\\*\\*b**", [("a****b", "b", None)])
        self.check("1 * 2", "1 \\* 2", [("1 * 2", "", None)])
        self.check("cost $5 and $6", "cost \\$5 and \\$6", [("cost $5 and $6", "", None)])

    def test_exporter_form_is_not_normalized(self):
        for md in ["plain **b** *i* ~~s~~ <u>u</u> `c` [t](https://x.io) $e$", "a\\_b", "\\#tag"]:
            self.assertFalse(M.parse_inline(md)[1], md)


class InlineRefusals(unittest.TestCase):
    CASES = [
        ("[[Page]]", "wikilink"), ("x [[T/Page A|Page A]]", "wikilink"), ("![[img.png]]", "embed"),
        ("![alt](https://x.io/a.png)", "image"), ("==hl==", "highlight"), ("a ==b c== d", "highlight"),
        ("#tag", "tag"), ("x #项目/子", "tag"), ("%%c%%", "comment"), ("a %% b", "comment"),
        ("a[^1]", "footnote"), ("^[inline]", "footnote"), ("<span>x</span>", "html"), ("a <b>x</b>", "html"),
        ("<!-- c -->", "comment"), ("x ^abc123", "block_id"), ("$$x$$", "display_math"),
        ("[t](" + NOTION_URL + ")", "notion_link"), ("[t](/abc-" + "0" * 32 + ")", "notion_link"),
        ("[t](/foo/bar)", "link_scheme"), ("[t](ftp://x)", "link_scheme"), ("[t](#anchor)", "anchor_link"),
        ('[t](https://x.io "T")', "link_title"), ("[](https://x.io)", "empty_link"), ("[$x$](https://x.io)", "math_link"),
        ("a<br>b", "html"),
    ]

    def test_refusals(self):
        for md, what in self.CASES:
            with self.assertRaises(M.Refused, msg=md) as cm:
                M.parse_inline(md)
            self.assertEqual(cm.exception.reason, "UNSUPPORTED_INLINE:" + what, md)
            self.assertTrue(cm.exception.hint, md)

    def test_long_link_url_refused(self):
        with self.assertRaises(M.Refused) as cm:
            M.parse_inline("[t](https://x.io/" + "a" * 2100 + ")")
        self.assertEqual(cm.exception.reason, "LIMIT:link_url")

    def test_ambiguous(self):
        # a raw URL ending in an escaped pipe inside a table cell: the exporter writes '|' inside a raw URL as
        # %7C, so the canonical rendering would read back as a different URL text
        with self.assertRaises(M.Refused) as cm:
            M.parse_inline("https://x.io/\\|", table=True)
        self.assertEqual(cm.exception.reason, "UNSUPPORTED_INLINE:ambiguous")
        self.assertTrue(cm.exception.hint)
        with self.assertRaises(M.Refused) as cm:
            M.rebuild_block(None, row_blk([[T("x")], [T("y")]]), ["| https://x.io/\\| | y |"], "")
        self.assertEqual(cm.exception.reason, "UNSUPPORTED_INLINE:ambiguous")

    def test_number_sign_not_a_tag(self):
        self.assertTrue(M.parse_inline("#123")[1])  # digits-only is not an Obsidian tag
        self.assertEqual(brief(M.parse_inline("https://x.io/(_a_)")[0]),
                         [("https://x.io/(", "", None), ("a", "i", None), (")", "", None)])

    def test_raw_url_reads_like_commonmark(self):
        # several Notion contents render to this text; the one CommonMark (Obsidian) shows is chosen
        segs, norm = M.parse_inline("https://x.io/*a*")
        self.assertEqual((brief(segs), norm), ([("https://x.io/", "", None), ("a", "i", None)], False))
        segs, norm = M.parse_inline("https://x.io/a**b**")
        self.assertEqual((brief(segs), norm), ([("https://x.io/a", "", None), ("b", "b", None)], False))


class BlockRebuild(unittest.TestCase):
    """rebuild_block for every R type: unchanged lines round-trip, edits produce the expected body."""

    def test_unchanged_roundtrip_every_type_and_prefix(self):
        blocks = [
            blk("paragraph", [T("hello "), T("world", bold=True), T("\n- not a list\n1. no\n  lead\n\tTab")]),
            blk("paragraph", [T("line1\nline2", code=True)]),
            blk("heading_1", [T("H1 "), T("x", italic=True)]), blk("heading_2", [T("H2")]),
            blk("heading_3", [T("H3")]), blk("heading_4", [T("H4")]),
            blk("bulleted_list_item", [T("item\nsecond line")]), blk("bulleted_list_item", [T("- dash")]),
            blk("numbered_list_item", [T("num")]), blk("to_do", [T("open")], checked=False),
            blk("to_do", [T("done")], checked=True), blk("quote", [T("q1\n\nq3")]),
            blk("callout", [T("body")], icon={"type": "emoji", "emoji": "💡"}),
            blk("callout", [T("no icon")], icon={"type": "external", "external": {"url": "https://x.io/i.png"}}),
            code_blk("print(1)\n\n  indented"), code_blk("```\nfence", "markdown"), code_blk("x", "c++"),
            code_blk("", "plain text"),
            {"object": "block", "id": "e", "type": "equation", "equation": {"expression": "a\n\nb"}, "has_children": False},
            {"object": "block", "id": "d", "type": "divider", "divider": {}, "has_children": False},
            row_blk([[T("a")], [T("b|c")], []]),
            blk("paragraph", [T("x" * 4500)]),
            blk("bulleted_list_item", []), blk("quote", []), blk("paragraph", []),
        ]
        for b in blocks:
            self.assertEqual(M.klass(None, b), ("R", None), b["type"])
            for prefix in ("", "> ", "    ", ">     "):
                for num in ((1, 12) if b["type"] == "numbered_list_item" else (1,)):
                    body, norm, lines = reread(b, prefix, num)
                    self.assertFalse(norm, (b["type"], prefix))
                    after = M.apply_update(b, body)
                    self.assertEqual(M.render_own_lines(None, after, prefix, num), lines, (b["type"], prefix))
                    self.assertEqual(M.canon_writable(after, True), M.canon_writable(b, True), (b["type"], prefix))

    def test_paragraph_edit_and_normalize(self):
        p = blk("paragraph", [T("hello "), T("world", bold=True)])
        body, norm = M.rebuild_block(None, p, ["hello **world** again"], "")
        self.assertFalse(norm)
        self.assertEqual(brief(body["paragraph"]["rich_text"]), [("hello ", "", None), ("world", "b", None), (" again", "", None)])
        body, norm = M.rebuild_block(None, p, ["hello __world__ _again_"], "")
        self.assertTrue(norm)
        self.assertEqual(M.canon_rich_text(body["paragraph"]["rich_text"]),
                         M.canon_rich_text([T("hello "), T("world", bold=True), T(" "), T("again", italic=True)]))

    def test_heading(self):
        h = blk("heading_2", [T("Title")])
        body, norm = M.rebuild_block(None, h, ["## Title *new*"], "")
        self.assertFalse(norm)
        self.assertEqual(M.canon_rich_text(body["heading_2"]["rich_text"]),
                         M.canon_rich_text([T("Title "), T("new", italic=True)]))

    def test_setext_heading_is_normalized(self):
        h1 = blk("heading_1", [T("Title")])
        body, norm = M.rebuild_block(None, h1, ["Title *x*", "====="], "")
        self.assertTrue(norm)
        self.assertEqual(M.canon_rich_text(body["heading_1"]["rich_text"]),
                         M.canon_rich_text([T("Title "), T("x", italic=True)]))

    def test_lists(self):
        td = blk("to_do", [T("task")], checked=False)
        body, norm = M.rebuild_block(None, td, ["- [x] task"], "")
        self.assertEqual((body["to_do"]["checked"], norm), (True, False))
        body, norm = M.rebuild_block(None, td, ["- [X] task"], "")
        self.assertEqual((body["to_do"]["checked"], norm), (True, True))
        body, norm = M.rebuild_block(None, td, ["    - [ ] task 2"], "    ")
        self.assertFalse(body["to_do"]["checked"])
        nl = blk("numbered_list_item", [T("one")])
        body, norm = M.rebuild_block(None, nl, ["7. one two"], "")
        self.assertFalse(norm)
        self.assertEqual(brief(body["numbered_list_item"]["rich_text"]), [("one", "", None), (" two", "", None)])
        body, norm = M.rebuild_block(None, nl, ["7) one"], "")
        self.assertTrue(norm)
        bl = blk("bulleted_list_item", [T("a")])
        body, norm = M.rebuild_block(None, bl, ["* a", "  more"], "")
        self.assertTrue(norm)
        self.assertEqual(brief(body["bulleted_list_item"]["rich_text"]), [("a", "", None), ("\nmore", "", None)])

    def test_quote_and_callout(self):
        q = blk("quote", [T("q1\nq2")])
        self.assertEqual(M.render_own_lines(None, q, "> "), ["> > q1", "> > q2"])
        body, norm = M.rebuild_block(None, q, ["> > q1", "> > q2 more"], "> ")
        self.assertEqual(M._plain(body["quote"]["rich_text"]), "q1\nq2 more")
        c = blk("callout", [T("body")], icon={"type": "emoji", "emoji": "💡"})
        body, norm = M.rebuild_block(None, c, ["> [!note] 💡", "> body changed", ">", "> p2"], "")
        self.assertEqual(M._plain(body["callout"]["rich_text"]), "body changed\n\np2")
        self.assertNotIn("icon", body["callout"])
        body, norm = M.rebuild_block(None, c, ["> [!note] 💡"], "")
        self.assertEqual(body["callout"]["rich_text"], [])

    def test_code_equation_divider_row(self):
        code = code_blk("print(1)")
        body, norm = M.rebuild_block(None, code, ["```python", "print(2)", "```"], "")
        self.assertEqual((body["code"]["language"], M._plain(body["code"]["rich_text"]), norm), ("python", "print(2)", False))
        body, norm = M.rebuild_block(None, code, ["```bash", "echo 2", "```"], "")
        self.assertEqual(body["code"]["language"], "bash")
        body, norm = M.rebuild_block(None, code, ["```cpp", "int x;", "```"], "")
        self.assertEqual(body["code"]["language"], "c++")
        body, norm = M.rebuild_block(None, code, ["```", "plain", "```"], "")
        self.assertEqual(body["code"]["language"], "plain text")
        body, norm = M.rebuild_block(None, code, ["~~~python", "print(3)", "~~~"], "")
        self.assertTrue(norm)
        big = "x" * 4100
        body, norm = M.rebuild_block(None, code, ["```python", big, "```"], "")
        self.assertEqual([len(s["text"]["content"]) for s in body["code"]["rich_text"]], [2000, 2000, 100])
        eq = {"object": "block", "id": "e", "type": "equation", "equation": {"expression": "E=mc^2"}, "has_children": False}
        self.assertEqual(M.rebuild_block(None, eq, ["$$", "E=mc^3", "$$"], ""), ({"equation": {"expression": "E=mc^3"}}, False))
        dv = {"object": "block", "id": "d", "type": "divider", "divider": {}, "has_children": False}
        self.assertEqual(M.rebuild_block(None, dv, ["---"], ""), ({"divider": {}}, False))
        self.assertEqual(M.rebuild_block(None, dv, ["***"], ""), ({"divider": {}}, True))
        row = row_blk([[T("a")], [T("b|c")], []])
        self.assertEqual(M.render_own_lines(None, row), ["| a | b\\|c |   |"])
        body, norm = M.rebuild_block(None, row, ["| a | b\\|c new | **z** |"], "")
        self.assertEqual([M._plain(c) for c in body["table_row"]["cells"]], ["a", "b|c new", "z"])
        body, norm = M.rebuild_block(None, row, ["| a | b\\|c |   |", "| --- | --- | --- |"], "")
        self.assertFalse(norm)
        body, norm = M.rebuild_block(None, row, ["| _a_ | b\\|c |   |"], "")
        self.assertTrue(norm)

    def test_refusals(self):
        p = blk("paragraph", [T("hello")])
        h = blk("heading_2", [T("Title")])
        td = blk("to_do", [T("task")], checked=False)
        nl = blk("numbered_list_item", [T("one")])
        c = blk("callout", [T("body")], icon={"type": "emoji", "emoji": "💡"})
        q = blk("quote", [T("q")])
        code = code_blk("x")
        row = row_blk([[T("a")], [T("b")]])
        cases = [
            (p, ["- hello"], "", "TYPE_CHANGED"), (p, ["# hello"], "", "TYPE_CHANGED"),
            (p, ["hello", "---"], "", "TYPE_CHANGED"), (h, ["### Title"], "", "TYPE_CHANGED"),
            (h, ["Title", "====="], "", "TYPE_CHANGED"), (td, ["- task"], "", "TYPE_CHANGED"),
            (nl, ["- one"], "", "TYPE_CHANGED"), (nl, ["1. one", "2. two"], "", "TYPE_CHANGED"),
            (nl, ["1. one"], "    ", "STRUCTURE_CHANGED"), (c, ["> [!tip] 💡", "> body"], "", "TYPE_CHANGED"),
            (q, ["> [!tip] x"], "", "UNSUPPORTED_INLINE:callout"), (p, ["hello [[x]]"], "", "UNSUPPORTED_INLINE:wikilink"),
            (p, ["hello ==x=="], "", "UNSUPPORTED_INLINE:highlight"), (p, ["hello #tag"], "", "UNSUPPORTED_INLINE:tag"),
            (p, ["[^1]: note"], "", "UNSUPPORTED_INLINE:footnote"), (p, ["[a]: https://x.io"], "", "UNSUPPORTED_INLINE:reference_definition"),
            (p, ["<div>x</div>"], "", "UNSUPPORTED_INLINE:html"), (p, ["![[a.png]]"], "", "UNSUPPORTED_INLINE:embed"),
            (code, ["```py", "x", "```"], "", "UNSUPPORTED_BLOCK:code_language:py"),
            (row, ["| a | b | c |"], "", "STRUCTURE_CHANGED"), (row, ["| a | b |", "| --- |"], "", "STRUCTURE_CHANGED"),
            (p, [], "", "STRUCTURE_CHANGED"), (p, ["hello", "<!-- -->"], "", "UNSUPPORTED_INLINE:comment"),
            (p, ["hello", "", "| a | b |", "| --- | --- |"], "", "UNSUPPORTED_BLOCK:table"),
            (q, ["> q", "> > nested"], "", "TYPE_CHANGED"), (code, ["```python", "x"], "", "REPARSE_MISMATCH"),
        ]
        for b, lines, prefix, reason in cases:
            with self.assertRaises(M.Refused, msg=(lines, reason)) as cm:
                M.rebuild_block(None, b, lines, prefix)
            self.assertEqual(cm.exception.reason, reason, lines)

    def test_x_block_is_not_rebuilt(self):
        for b in (blk("paragraph", [T("a", color="red")]), blk("paragraph", [MENTION()]),
                  blk("toggle", [T("t")]), blk("paragraph", [T("t", NOTION_URL)])):
            with self.assertRaises(M.Refused) as cm:
                M.rebuild_block(None, b, ["anything"], "")
            self.assertEqual(cm.exception.reason, "UNSUPPORTED_REGION")

    def test_fragment_reuse_keeps_link_fragment_verbatim(self):
        old = blk("paragraph", [T("see "), T("docs", "https://x.io", bold=True), T(" now")])
        old["paragraph"]["rich_text"][1]["annotations"]["color"] = "default"
        body, norm = M.rebuild_block(None, old, ["see [**docs**](https://x.io) now please"], "")
        req = body["paragraph"]["rich_text"]
        self.assertFalse(norm)
        self.assertEqual(req[1], M.request_rich([old["paragraph"]["rich_text"][1]])[0])
        self.assertEqual(req[0], M.request_rich([old["paragraph"]["rich_text"][0]])[0])
        self.assertEqual(M._plain(req), "see docs now please")
        # whitespace-only bold segment (invisible in Markdown) survives an edit elsewhere
        old2 = blk("paragraph", [T("a"), T(" ", bold=True), T("b")])
        body, _ = M.rebuild_block(None, old2, ["a b c"], "")
        self.assertEqual(brief(body["paragraph"]["rich_text"])[:2], [("a", "", None), (" ", "b", None)])

    def test_no_side_effects_on_exporter(self):
        ex = EO.Exporter.__new__(EO.Exporter)
        M._shim()  # make sure the module shim exists
        for k, v in vars(M._shim()).items():
            setattr(ex, k, copy.copy(v))
        ex.report = defaultdict(Counter, {"x": Counter({"y": 1})})
        ex.lists = defaultdict(list, {"l": [1]})
        ex._list_num, ex._blank_count = 7, 3
        p = blk("numbered_list_item", [T("a", color="default")])
        M.klass(ex, p)
        M.rebuild_block(ex, p, ["4. a b"], "")
        M.parse_new_blocks(ex, ["- x", "- y", "", "1. z"], "")
        self.assertEqual((dict(ex.report), dict(ex.lists), ex._list_num, ex._blank_count),
                         ({"x": Counter({"y": 1})}, {"l": [1]}, 7, 3))


class NewBlocks(unittest.TestCase):
    def test_exporter_rendering_parses_exactly(self):
        lines = ["# T", "", "para *x* and `c`", "", "- a", "    - b", "        - c", "- d", "", "<!-- -->", "",
                 "- [x] done", "- [ ] open", "", "1. n1", "2. n2", "", "> q1", ">", "> q2", "", "```python", "x=1",
                 "```", "", "---", "", "$$", "a+b", "$$", "", "## H2", "", "### H3"]
        blocks, norm = M.parse_new_blocks(None, lines, "")
        self.assertFalse(norm)
        self.assertEqual([b["type"] for b in blocks],
                         ["heading_1", "paragraph", "bulleted_list_item", "bulleted_list_item", "to_do", "to_do",
                          "numbered_list_item", "numbered_list_item", "quote", "code", "divider", "equation",
                          "heading_2", "heading_3"])
        a = blocks[2]["bulleted_list_item"]
        self.assertEqual(a["children"][0]["bulleted_list_item"]["children"][0]["type"], "bulleted_list_item")
        self.assertEqual((blocks[4]["to_do"]["checked"], blocks[5]["to_do"]["checked"]), (True, False))
        self.assertEqual(M._plain(blocks[8]["quote"]["rich_text"]), "q1\n\nq2")
        self.assertEqual(blocks[9]["code"]["language"], "python")
        self.assertEqual(blocks[11]["equation"]["expression"], "a+b")
        for b in blocks:  # request shape: no read-only fields
            for s in b[b["type"]].get("rich_text") or []:
                self.assertNotIn("plain_text", s)
                self.assertNotIn("href", s)

    def test_prefix_and_numbering(self):
        blocks, norm = M.parse_new_blocks(None, ["> new line", ">", "> other"], "> ")
        self.assertEqual(([b["type"] for b in blocks], norm), (["paragraph", "paragraph"], False))
        blocks, norm = M.parse_new_blocks(None, ["", "    3. a", "    4. b", ""], "    ")
        self.assertEqual(([b["type"] for b in blocks], norm), (["numbered_list_item"] * 2, False))
        self.assertEqual(M.parse_new_blocks(None, ["", "  "], ""), ([], False))

    def test_normalized(self):
        blocks, norm = M.parse_new_blocks(None, ["* a", "* b", "", "", "text _it_", "", "Setext", "===", "",
                                                 "    indented code", "", "1) x"], "")
        self.assertTrue(norm)
        self.assertEqual([b["type"] for b in blocks],
                         ["bulleted_list_item", "bulleted_list_item", "paragraph", "heading_1", "code", "numbered_list_item"])
        self.assertEqual(blocks[4]["code"]["language"], "plain text")
        blocks, norm = M.parse_new_blocks(None, ["- a", "", "    para", "", "    ```python", "    x", "    ```"], "")
        self.assertEqual(M._plain(blocks[0]["bulleted_list_item"]["rich_text"]), "a\n\npara")
        self.assertEqual(blocks[0]["bulleted_list_item"]["children"][0]["type"], "code")

    def test_refusals(self):
        cases = [
            (["- a", "    - b", "        - c", "            - d"], "UNSUPPORTED_BLOCK:nesting_depth"),
            (["| a | b |", "| --- | --- |", "| 1 | 2 |"], "UNSUPPORTED_BLOCK:table"),
            (["#### h4"], "UNSUPPORTED_BLOCK:heading_4"), (["> [!tip] x"], "UNSUPPORTED_INLINE:callout"),
            (["> q", "> - item"], "UNSUPPORTED_BLOCK:quote_children"), (["- a", "- [ ] b"], "REPARSE_MISMATCH"),
            (["<div>", "x", "</div>"], "UNSUPPORTED_INLINE:html"), (["text [[Link]]"], "UNSUPPORTED_INLINE:wikilink"),
            (["![[x.pdf]]"], "UNSUPPORTED_INLINE:embed"), (["```js", "x", "```"], "UNSUPPORTED_BLOCK:code_language:js"),
            (["a", "", "[x]: https://x.io"], "UNSUPPORTED_INLINE:reference_definition"),
            (["Two", "lines", "==="], "REPARSE_MISMATCH"),
            (["%%", "hidden", "%%"], "UNSUPPORTED_INLINE:comment"), (["  x"], "STRUCTURE_CHANGED"),
        ]
        for lines, reason in cases:
            with self.assertRaises(M.Refused, msg=lines) as cm:
                M.parse_new_blocks(None, lines, "    " if lines == ["  x"] else "")
            self.assertEqual(cm.exception.reason, reason, lines)

    def test_snapshot_overlay(self):
        blocks, _ = M.parse_new_blocks(None, ["- a", "    - b"], "")
        overlay, ids = M.snapshot_blocks(blocks, "parent")
        self.assertEqual(len(ids), 2)
        self.assertTrue(overlay["parent"][0]["has_children"])
        self.assertEqual(overlay[ids[0]][0]["type"], "bulleted_list_item")


class Klass(unittest.TestCase):
    def test_x_reasons(self):
        cases = [
            (blk("toggle", [T("t")]), "type:toggle"), ({"type": "image", "image": {}, "id": "i"}, "type:image"),
            ({"type": "table", "table": {}, "id": "t"}, "type:table"),
            (blk("paragraph", [T("a", color="red")]), "color"),
            (blk("paragraph", [T("a", color="gray_background")]), "color"),
            (blk("paragraph", [MENTION()]), "mention_page"), (blk("paragraph", [MENTION("user", id="u")]), "mention_user"),
            (blk("paragraph", [MENTION("date", start="2026-01-01")]), "mention_date"),
            (blk("paragraph", [MENTION("link_mention", href="https://x.io")]), "mention_link_mention"),
            (blk("paragraph", [T("t", NOTION_URL)]), "link_to_notion"),
            (blk("paragraph", [T("t", "/" + PAGE_ID.replace("-", ""))]), "link_to_notion"),
            (blk("paragraph", [T("t", "#abc")]), "anchor_link"), (blk("paragraph", [T("t", "/local/path")]), "relative_link"),
            (blk("code", [T("x")], language="python", caption=[T("cap")]), "caption"),
            (blk("code", [T("x", bold=True)], language="python", caption=[]), "code_annotated"),
            (blk("code", [T("x")], language="Python3", caption=[]), "code_language"),
            (blk("heading_2", [T("a\nb")]), "heading_newline"),
            (dict(blk("paragraph", [T("p")]), has_children=True), "paragraph_with_children"),
            (dict(blk("heading_1", [T("h")], is_toggleable=True), has_children=True), "toggle_heading_with_children"),
            (blk("paragraph", [T("\n")]), "roundtrip"),
            (blk("paragraph", [T("", "https://x.io")]), "roundtrip"),
            (blk("paragraph", [T("&amp;")]), "md_divergent"), (blk("paragraph", [T(" pad ", code=True)]), "md_divergent"),
            (blk("paragraph", [T("x", bold=True), T("y", bold=True)]), "md_divergent"),
            (blk("paragraph", [T("see https://x.io/*a*")]), "roundtrip"),  # reads back as italic "a"
            (blk("heading_2", [T("a #")]), "md_divergent"), (blk("paragraph", [T("end ^abc")]), "md_divergent"),
            (blk("paragraph", [T("<https://x.io>")]), "md_divergent"),
        ]
        for b, why in cases:
            k, r = M.klass(None, b)
            self.assertEqual(k, "X", (b, why))
            self.assertTrue(r == why or r.startswith(why + ":"), (r, why))

    def test_r_cases(self):
        for b in (blk("paragraph", [T("a", color="default")], color="blue"),
                  blk("callout", [T("x")], icon={"type": "emoji", "emoji": "⚠️"}, color="gray_background"),
                  dict(blk("bulleted_list_item", [T("has kids")]), has_children=True),
                  dict(blk("heading_1", [T("h")], is_toggleable=True), has_children=False),
                  blk("paragraph", [T("x", "https://e.com/x#frag")]), blk("paragraph", [T("x", "mailto:a@b.c")])):
            self.assertEqual(M.klass(None, b), ("R", None), b)

    def test_children_argument(self):
        p = blk("paragraph", [T("p")])
        self.assertEqual(M.klass(None, p, [blk("paragraph", [T("kid")])])[1], "paragraph_with_children")
        self.assertEqual(M.klass(None, p, []), ("R", None))


class Canonical(unittest.TestCase):
    def test_merge_and_defaults(self):
        a = [T("a", bold=True), T("b", bold=True), T(""), {"type": "text", "text": {"content": "c"}}]
        self.assertEqual(M.canon_rich_text(a), [
            {"type": "text", "text": {"content": "ab", "link": None}, "annotations": dict(M._ann({"bold": True}))},
            {"type": "text", "text": {"content": "c", "link": None}, "annotations": M._ann()}])

    def test_mentions_and_equations(self):
        m1 = MENTION()
        m2 = copy.deepcopy(m1)
        m2["mention"]["page"]["id"] = PAGE_ID.replace("-", "").upper()
        m2["plain_text"] = "other"
        self.assertEqual(M.canon_rich_text([m1]), M.canon_rich_text([m2]))
        self.assertEqual(M.canon_rich_text([E("x")])[0]["equation"], {"expression": "x"})

    def test_equivalence_rules(self):
        self.assertNotEqual(M.canon_rich_text([T(" a ", bold=True)]), M.canon_rich_text([T(" "), T("a", bold=True), T(" ")]))
        self.assertEqual(M.canon_rich_text([T(" a ", bold=True)], "inline"),
                         M.canon_rich_text([T(" "), T("a", bold=True), T(" ")], "inline"))
        self.assertEqual(M.canon_rich_text([T("a \nb ")], "text"), M.canon_rich_text([T("a\nb")], "text"))
        self.assertEqual(M.canon_rich_text([T("\tx")], "text"), M.canon_rich_text([T("    x")], "text"))
        self.assertEqual(M.canon_rich_text([T("a\nb", code=True)], "inline"),
                         M.canon_rich_text([T("a", code=True), T("\n"), T("b", code=True)], "inline"))

    def test_writable_fields(self):
        self.assertEqual(M.writable_fields(blk("to_do", [T("x")], checked=True))["checked"], True)
        self.assertEqual(set(M.writable_fields(code_blk("x"))), {"rich_text", "language"})
        self.assertEqual(M.writable_fields({"type": "divider", "divider": {}}), {})
        self.assertEqual(M.writable_fields({"type": "image", "image": {}}), {})
        self.assertEqual(set(M.writable_fields(blk("callout", [T("x")], icon={"type": "emoji", "emoji": "💡"}))), {"rich_text"})


class Limits(unittest.TestCase):
    def test_split_long_text(self):
        segs = M.split_for_api([T("x" * 5000, bold=True)])
        self.assertEqual([len(M._seg_text(s)) for s in segs], [2000, 2000, 1000])
        self.assertTrue(all(s["annotations"]["bold"] for s in segs))
        p = blk("paragraph", [T("x" * 5000)])
        body, norm, lines = reread(p)
        self.assertEqual(len(body["paragraph"]["rich_text"]), 3)
        self.assertEqual(M.render_own_lines(None, M.apply_update(p, body)), lines)

    def test_render_safe_split(self):
        for text in ("a" * 1999 + "==" + "b" * 100, "x" * 1990 + " https://example.com/" + "p" * 50 + " end",
                     "y" * 1999 + " #tag" + "z" * 10, "w" * 1999 + "%%" + "v" * 5, "u" * 1999 + "<" + "div>"):
            segs = M.split_for_api([T(text)])
            self.assertTrue(all(M._u16(M._seg_text(s)) <= 2000 for s in segs))
            self.assertEqual("".join(rt([s]) for s in segs), rt([T(text)]), text[1990:])
            p = blk("paragraph", [T(text)])
            self.assertEqual(M.klass(None, p), ("R", None))
            body, norm, lines = reread(p)
            self.assertEqual(M.render_own_lines(None, M.apply_update(p, body)), lines)

    def test_long_formatted_segment_is_x(self):
        self.assertEqual(M.klass(None, blk("paragraph", [T("x" * 2100, bold=True)])), ("X", "limit:split"))
        self.assertEqual(M.klass(None, blk("paragraph", [T("a\n" + "x" * 2100, code=True)])), ("X", "limit:split"))
        p = blk("paragraph", [T("short", bold=True)])
        with self.assertRaises(M.Refused) as cm:
            M.rebuild_block(None, p, ["**" + "x" * 2100 + "**"], "")
        self.assertEqual(cm.exception.reason, "LIMIT:split")
        with self.assertRaises(M.Refused) as cm:
            M.parse_new_blocks(None, ["**" + "x" * 2100 + "**"], "")
        self.assertEqual(cm.exception.reason, "LIMIT:split")

    def test_split_utf16(self):
        s = "😀" * 1500  # 3,000 UTF-16 units
        segs = M.split_for_api([T(s)])
        self.assertEqual([M._u16(M._seg_text(x)) for x in segs], [2000, 1000])
        self.assertEqual("".join(M._seg_text(x) for x in segs), s)

    def test_segment_limit(self):
        rich = [T("a", bold=(i % 2 == 0)) for i in range(101)]
        with self.assertRaises(M.Refused) as cm:
            M.split_for_api(rich)
        self.assertEqual(cm.exception.reason, "LIMIT:segments")
        self.assertEqual(len(M.split_for_api([T("a") for _ in range(150)])), 1)  # identical neighbours merge

    def test_equation_and_url_limits(self):
        with self.assertRaises(M.Refused):
            M.split_for_api([E("x" * 1001)])
        with self.assertRaises(M.Refused):
            M.split_for_api([T("t", "https://x.io/" + "a" * 2000)])
        eq = {"object": "block", "id": "e", "type": "equation", "equation": {"expression": "x"}, "has_children": False}
        with self.assertRaises(M.Refused) as cm:
            M.rebuild_block(None, eq, ["$$", "y" * 1001, "$$"], "")
        self.assertEqual(cm.exception.reason, "LIMIT:equation")

    def test_request_limits(self):
        def para(i):
            return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(i)}}]}}
        batches = M.chunk_children([para(i) for i in range(250)])
        self.assertEqual([len(b) for b in batches], [100, 100, 50])
        nested = {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [], "children": [
            {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [], "children": [
                {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [], "children": [para(1)]}}]}}]}}
        self.assertEqual(M.nesting_depth([nested]), 3)
        self.assertEqual(M.block_count([nested]), 4)
        with self.assertRaises(M.Refused) as cm:
            M.check_children_request([nested])
        self.assertEqual(cm.exception.reason, "LIMIT:nesting")
        big = [{"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [], "children": [para(j) for j in range(99)]}}
               for _ in range(11)]
        self.assertEqual([len(b) for b in M.chunk_children(big)], [10, 1])  # 1,000 elements per request
        huge = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "x" * 2000}}] * 100}}] * 3
        self.assertEqual([len(b) for b in M.chunk_children(huge)], [2, 1])  # 500 KB per request


NBSP = " "
IDEO = "　"
DIVIDER = {"object": "block", "id": "k", "type": "divider", "divider": {}, "has_children": False}


def chars(rich):
    """Per-character (char, annotation flags, link) of request- or render-shaped rich text."""
    return [(c, "".join(k[0] for k in M.ANN_KEYS if (s.get("annotations") or {}).get(k)), M._seg_link(s))
            for s in rich if s["type"] == "text" for c in s["text"]["content"]]


class NormalizedEditKeepsUntouchedText(unittest.TestCase):
    """Review fix round, finding 1: a CommonMark-only edit (normalized path) keeps the edge whitespace and
    blank lines of the untouched parts exactly like the exporter-grammar path (markdown-it strips paragraph,
    heading and table-cell edges with str.strip(), which also removes NBSP / U+3000)."""

    def both(self, b, fast_lines, norm_lines, prefix=""):
        """rebuild with an exporter-canonical edit and with the CommonMark-only equivalent: same result."""
        bf, nf = M.rebuild_block(None, b, fast_lines, prefix)
        bn, nn = M.rebuild_block(None, b, norm_lines, prefix)
        self.assertEqual((nf, nn), (False, True), norm_lines)
        an = M.apply_update(b, bn)
        self.assertEqual(M.canon_writable(an), M.canon_writable(M.apply_update(b, bf)), norm_lines)
        return an[b["type"]]

    def edit_last(self, b, prefix="", fast=" a\\_b", norm=" a_b"):
        L = M.render_own_lines(None, b, prefix)
        return self.both(b, L[:-1] + [L[-1] + fast], L[:-1] + [L[-1] + norm], prefix)

    def test_text_blocks_keep_leading_whitespace_and_blank_lines(self):
        cases = [  # (block, plain text expected after appending ' a_b' to the last own line)
            (blk("paragraph", [T("  第一行缩进\n第二行")]), "  第一行缩进\n第二行 a_b"),
            (blk("paragraph", [T(NBSP * 2 + "第一行\n第二行")]), NBSP * 2 + "第一行\n第二行 a_b"),
            (blk("paragraph", [T(IDEO * 2 + "第一行\n第二行")]), IDEO * 2 + "第一行\n第二行 a_b"),
            (blk("paragraph", [T("首段\n\n  次段\n" + IDEO + "三")]), "首段\n\n  次段\n" + IDEO + "三 a_b"),
            (blk("paragraph", [T("\ntext here")]), "\ntext here a_b"),
            (blk("paragraph", [T("\n\n" + NBSP + "x")]), "\n\n" + NBSP + "x a_b"),
            (blk("heading_2", [T(IDEO + "标题")]), IDEO + "标题 a_b"),
            (blk("heading_3", [T("  标题")]), "  标题 a_b"),
            (blk("heading_1", [T(NBSP + "标题")]), NBSP + "标题 a_b"),
            (blk("quote", [T("  引用")]), "  引用 a_b"),
            (blk("quote", [T("\n" + IDEO + "引用")]), "\n" + IDEO + "引用 a_b"),
            (blk("callout", [T("  正文\n二")], icon={"type": "emoji", "emoji": "💡"}), "  正文\n二 a_b"),
            (blk("bulleted_list_item", [T("  项")]), "  项 a_b"),
            (blk("bulleted_list_item", [T("\n项")]), "\n项 a_b"),
            (blk("bulleted_list_item", [T("项\n\n" + NBSP + "次段")]), "项\n\n" + NBSP + "次段 a_b"),
            (blk("numbered_list_item", [T(IDEO + "项")]), IDEO + "项 a_b"),
            (blk("to_do", [T("  任务\n续")], checked=True), "  任务\n续 a_b"),
        ]
        for b, want in cases:
            self.assertEqual(M.klass(None, b), ("R", None), b)
            for prefix in ("", "> ", "    "):
                v = self.edit_last(b, prefix)
                self.assertEqual(M._plain(v["rich_text"]), want, (b["type"], prefix, want))

    def test_trailing_blank_line(self):
        for t in ("paragraph", "quote", "bulleted_list_item"):
            b = blk(t, [T("text\n")])
            L = M.render_own_lines(None, b)
            v = self.both(b, [L[0] + " a\\_b"] + L[1:], [L[0] + " a_b"] + L[1:])
            self.assertEqual(M._plain(v["rich_text"]), "text a_b\n", t)

    def test_untouched_formatting_is_kept(self):
        p = blk("paragraph", [T("  "), T("粗", bold=True), T(" 与 "), T("链接", "https://x.io"), T("\n二")])
        L = M.render_own_lines(None, p)
        self.assertEqual(L, [NBSP * 2 + "**粗** 与 [链接](https://x.io)", "二"])
        v = self.both(p, [L[0], "二 *斜*"], [L[0].replace("**粗**", "__粗__"), "二 _斜_"])
        want = [T("  "), T("粗", bold=True), T(" 与 "), T("链接", "https://x.io"), T("\n二 "), T("斜", italic=True)]
        self.assertEqual(chars(v["rich_text"]), chars(want))

    def test_table_row_untouched_cells(self):
        row = row_blk([[T(IDEO * 2 + "缩进")], [T("b")], [T("x ")], [T("  ")], [T(NBSP + "y" + NBSP)]])
        L = M.render_own_lines(None, row)
        self.assertEqual(L, ["| " + IDEO * 2 + "缩进 | b | x  |    | " + NBSP + "y" + NBSP + " |"])
        v = self.both(row, [L[0].replace("| b |", "| *b* |")], [L[0].replace("| b |", "| _b_ |")])
        self.assertEqual([M._plain(c) for c in v["cells"]], [IDEO * 2 + "缩进", "b", "x ", "  ", NBSP + "y" + NBSP])
        self.assertEqual(brief(v["cells"][1]), [("b", "i", None)])
        # the edited cell itself: its exporter-grammar padding is content too
        v = self.both(row, [L[0].replace("| b |", "|  *b* |")], [L[0].replace("| b |", "|  _b_ |")])
        self.assertEqual(brief(v["cells"][1]), [(" ", "", None), ("b", "i", None)])

    def test_untouched_nbsp_data_is_resent_verbatim(self):
        # the exporter grammar reads a line-start NBSP run as spaces; fragment reuse keeps the stored NBSP
        p = blk("paragraph", [T(NBSP * 2 + "x\n" + NBSP + "y")])
        L = M.render_own_lines(None, p)
        for edit in ([L[0], L[1] + " z"], [L[0], L[1] + " a_b"]):
            body, _ = M.rebuild_block(None, p, edit, "")
            self.assertTrue(M._plain(body["paragraph"]["rich_text"]).startswith(NBSP * 2 + "x\n" + NBSP + "y "), edit)

    def test_new_blocks(self):
        for lines, t, want, norm in [
            (["## " + IDEO + "标题"], "heading_2", IDEO + "标题", False),
            (["#  标题"], "heading_1", " 标题", False),
            ([NBSP * 2 + "缩进"], "paragraph", "  缩进", False),
            ([IDEO + "缩进", NBSP + "续"], "paragraph", IDEO + "缩进\n 续", False),
            (["- " + NBSP * 2 + "项"], "bulleted_list_item", "  项", False),
            (["- ", "    项"], "bulleted_list_item", "\n项", False),
            ([">", "> " + IDEO + "引"], "quote", "\n" + IDEO + "引", False),
            # spaces/tabs typed at a paragraph start are CommonMark indentation (the exporter writes NBSP)
            (["  indented"], "paragraph", "indented", True),
        ]:
            req, n = M.parse_new_blocks(None, lines, "")
            self.assertEqual(([b["type"] for b in req], M._plain(req[0][t]["rich_text"]), n), ([t], want, norm), lines)

    def test_carriage_returns_and_nul(self):
        # markdown-it numbers lines after normalising \r / \r\n (and NUL): token maps must index those lines
        for lines in (["##1#~  |\r# seed text"], ["## " + IDEO + "x\r\ny"], ["- a\r      - b"], ["x\0y\r" + NBSP + "z"]):
            try:
                M.parse_new_blocks(None, lines, "")
            except M.Refused:
                pass
            for b in (blk("heading_2", [T("x")]), blk("paragraph", [T("x")]), blk("bulleted_list_item", [T("x")])):
                try:
                    M.rebuild_block(None, b, lines, "")
                except M.Refused:
                    pass


class ListItemNesting(unittest.TestCase):
    """Review fix round, finding 2: a continuation line inside a list item that starts like a block (the
    exporter renders a to_do's children 6 spaces deep, which CommonMark reads as paragraph text) is refused,
    never folded into the parent's text."""

    def render(self, parent, kids):
        with M._sandbox(None, {"md2b-test-root": [parent], parent["id"]: kids}) as r:
            return [str(x) for x in r.render_blocks("md2b-test-root", M.RENDER_TS, "")]

    def test_rendered_children_reproduce_or_refuse(self):
        kid_list = [blk("bulleted_list_item", [T("b")], bid="k"), blk("to_do", [T("b")], bid="k", checked=True),
                    blk("numbered_list_item", [T("b")], bid="k")]
        kid_other = [blk("paragraph", [T("b")], bid="k"), blk("quote", [T("b")], bid="k"),
                     blk("heading_2", [T("b")], bid="k"), dict(code_blk("b"), id="k"), DIVIDER]
        for pt in ("bulleted_list_item", "numbered_list_item", "to_do"):
            extra = {"checked": False} if pt == "to_do" else {}
            parent = dict(blk(pt, [T("a")], bid="p", **extra), has_children=True)
            for kid in kid_list + kid_other:
                lines = self.render(parent, [kid])
                try:
                    req, norm = M.parse_new_blocks(None, lines, "")
                except M.Refused as e:
                    self.assertIn(e.reason, ("UNSUPPORTED_BLOCK:ambiguous_nesting", "REPARSE_MISMATCH"), lines)
                    self.assertNotEqual(pt, "bulleted_list_item" if kid in kid_list else None, lines)
                    continue
                self.assertNotEqual(pt, "to_do", lines)  # CommonMark cannot show a to_do's 6-space children
                top = req[0][pt]
                if kid in kid_list:  # the child structure is reproduced exactly
                    self.assertEqual((len(req), norm, M._plain(top["rich_text"])), (1, False, "a"), lines)
                    self.assertEqual([k["type"] for k in top.get("children") or []], [kid["type"]], lines)
                else:  # a non-list child after a blank line reads as the item's own next paragraph
                    self.assertFalse(any("- b" in M._plain(b[b["type"]].get("rich_text") or []) for b in req), lines)

    def test_todo_child_lines_refused(self):
        td = blk("to_do", [T("a")], checked=False)
        for child in ("      - b", "      - [ ] b", "      1. b", "      > b", "      # b", "      ---", "      * b",
                      "      2024. b", "\t- b"):
            with self.assertRaises(M.Refused, msg=child) as cm:
                M.rebuild_block(None, td, ["- [ ] a", child], "")
            self.assertIn(cm.exception.reason, ("TYPE_CHANGED", "REPARSE_MISMATCH"), child)
            with self.assertRaises(M.Refused, msg=child) as cm:
                M.parse_new_blocks(None, ["- [ ] a", child], "")
            self.assertIn(cm.exception.reason, ("UNSUPPORTED_BLOCK:ambiguous_nesting", "REPARSE_MISMATCH"), child)
        with self.assertRaises(M.Refused) as cm:
            M.parse_new_blocks(None, ["- [ ] a", "      - b"], "")
        self.assertEqual(cm.exception.reason, "UNSUPPORTED_BLOCK:ambiguous_nesting")
        with self.assertRaises(M.Refused) as cm:
            M.rebuild_block(None, blk("bulleted_list_item", [T("a")]), ["- a", "      - b"], "")
        self.assertEqual(cm.exception.reason, "TYPE_CHANGED")

    def test_escaped_markers_are_text(self):
        td = blk("to_do", [T("a\n- b\n1. c\n> d\n# e\n---\n2024. f")], checked=False)
        self.assertEqual(M.klass(None, td), ("R", None))
        L = M.render_own_lines(None, td)
        self.assertEqual(L[1:3], ["      \\- b", "      1\\. c"])
        body, norm = M.rebuild_block(None, td, L[:-1] + [L[-1] + " g"], "")
        self.assertEqual((M._plain(body["to_do"]["rich_text"]), norm), ("a\n- b\n1. c\n> d\n# e\n---\n2024. f g", False))
        req, norm = M.parse_new_blocks(None, ["- [ ] a", "      \\- b"], "")
        self.assertEqual((M._plain(req[0]["to_do"]["rich_text"]), norm), ("a\n- b", False))


class MentionCanonical(unittest.TestCase):
    """Review fix round, finding 3: mentions without an id are canonicalised by their content."""

    def m(self, mt, inner):
        return {"type": "mention", "mention": {"type": mt, mt: inner}, "annotations": M._ann(), "plain_text": "p", "href": None}

    def test_content_matters(self):
        c = M.canon_rich_text
        self.assertNotEqual(c([T("due "), self.m("date", {"start": "2026-01-01", "end": None})]),
                            c([T("due "), self.m("date", {"start": "2027-12-31", "end": None})]))
        self.assertNotEqual(c([self.m("date", {"start": "2026-01-01"})]),
                            c([self.m("date", {"start": "2026-01-01", "end": "2026-01-02"})]))
        self.assertNotEqual(c([self.m("date", {"start": "2026-01-01T10:00", "time_zone": "Asia/Bangkok"})]),
                            c([self.m("date", {"start": "2026-01-01T10:00", "time_zone": "UTC"})]))
        self.assertEqual(c([self.m("date", {"start": "2026-01-01", "end": None, "time_zone": None})]),
                         c([self.m("date", {"start": "2026-01-01"})]))
        self.assertNotEqual(c([self.m("link_preview", {"url": "https://a.com"})]),
                            c([self.m("link_preview", {"url": "https://evil.com"})]))
        self.assertNotEqual(c([self.m("link_mention", {"href": "https://a.com", "title": "A"})]),
                            c([self.m("link_mention", {"href": "https://evil.com", "title": "A"})]))
        self.assertEqual(c([self.m("link_mention", {"href": "https://a.com", "title": "A", "description": "x"})]),
                         c([self.m("link_mention", {"href": "https://a.com", "title": "A", "description": "y"})]))
        self.assertNotEqual(c([self.m("template_mention", {"type": "template_mention_date", "template_mention_date": "today"})]),
                            c([self.m("template_mention", {"type": "template_mention_date", "template_mention_date": "now"})]))
        self.assertEqual(c([self.m("user", {"object": "user", "id": PAGE_ID, "name": "A"})]),
                         c([self.m("user", {"object": "user", "id": PAGE_ID.replace("-", ""), "name": "B"})]))
        pa = blk("paragraph", [T("due "), self.m("date", {"start": "2026-01-01"})])
        pb = blk("paragraph", [T("due "), self.m("date", {"start": "2027-12-31"})])
        self.assertNotEqual(M.canon_writable(pa), M.canon_writable(pb))
        self.assertNotEqual(M.canon_writable(pa, True), M.canon_writable(pb, True))


if __name__ == "__main__":
    unittest.main()
