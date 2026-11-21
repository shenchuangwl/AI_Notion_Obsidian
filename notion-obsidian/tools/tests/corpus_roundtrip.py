#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Corpus round-trip regression for tools/md2blocks.py (contract §5.8; reproduces roundtrip.md §5).

Read-only: the snapshot and export.db are opened with `?mode=ro&immutable=1`; nothing is written except the optional
--out JSON report. No network. Run explicitly (it is not part of the unittest suite):

    python3 -I -B tools/tests/corpus_roundtrip.py --snapshot snapshots/snap_20261117T1859.db \
        [--export-db state/export.db] [--edit-sample 3000] [--norm-sample 3000] [--limit N] [--out report.json]

Measures
  A  inline: for every non-empty rich_text / caption / table-cell array of every statically-R block:
     Exporter.rt() -> md2blocks.parse_inline -> rt identical, canonical equal, not normalized
  B  block: klass() on every block; for every R block render_block (children cut) -> rebuild_block ->
     re-render identical, writable fields canonical-equal (strict equivalence and plain D-12 merge),
     normalized == False; plus the same under a quote / list prefix for a sample
  E  edits: for a sample of R text blocks, append a word to the last own line -> rebuild_block must succeed,
     not normalized, and the request re-renders to exactly the edited lines (fragment reuse included)
  N  normalized edits: for a sample of R text blocks and table rows (rotating prefixes "", "> ", "    "),
     append " a_b" (CommonMark-only: forces the normalized path) to the last own line / one cell, and the
     exporter-canonical " a\\_b" to the same place: the first must be normalized, the second exact (or itself
     normalized when the edit made an exporter escape such as '- \\--' unnecessary), and the
     two results must be canonical-equal on the writable fields (D-12 merge, per-character annotations and
     links) -> the normalized path keeps every untouched character (edge whitespace, blank lines, other
     cells) exactly like the exporter-grammar path
Pass criterion (CORPUS OK): every A array identical+equal (no refusal, no normalization, no mismatch), every
B block, prefix case, E edit and N edit ok.
Examples in the report are anonymised (letters -> a, digits -> 0, CJK -> 字).
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.append(TOOLS)  # appended: an export_obsidian already earlier on sys.path wins (frozen-copy runs)
import export_obsidian as EO  # noqa: E402
import md2blocks as M  # noqa: E402

WORK = os.path.dirname(TOOLS)
X_CONTAINERS = {"column_list", "column", "synced_block"}


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def build_exporter(snap, export_db):
    wal = snap + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise SystemExit(f"ABORT: {wal} not empty (immutable open would miss frames)")
    if hasattr(EO.Exporter, "for_planning"):
        tmp = tempfile.mkdtemp(prefix="corpus_rt_")
        att = os.path.join(tmp, "attachments.json")
        with open(att, "w", encoding="utf-8") as f:
            f.write("{}")  # attachments are irrelevant for R-class blocks; skips the attachment render pass
        return EO.Exporter.for_planning(snap, export_db, attachments_json=att)
    # older exporter: build without __init__ (it opens export.db read-write)
    from collections import Counter as _C, defaultdict as _d
    ex = EO.Exporter.__new__(EO.Exporter)
    ex.roots_kinds = None
    ex.run_id = "corpus"
    ex.out = ex.vault = None
    ex.snap = ro(snap)
    ex.snap_path = snap
    ex.sidebar = {"roots": json.loads(dict(ex.snap.execute("select key,value from meta"))["roots"])}
    st = ro(export_db)
    ex.state = None
    ex.prev = {r[0]: {"name": r[1], "parent": r[2], "title": r[3], "kind": r[4]}
               for r in st.execute("SELECT notion_id,name,parent_id,title,kind FROM objects_map")}
    st.close()
    ex.report = _d(_C)
    ex.folder_owner, ex.tpl_folder_owner = {}, {}
    ex._list_num, ex._blank_count, ex._synced_stack = 1, 0, set()
    ex.lists = _d(list)
    ex.load()
    ex.manifest, ex.attachments, ex.att_by_sha = {}, {}, {}
    ex.build_tree()
    ex.assign_paths()
    return ex


def anon(s, n=90):
    s = re.sub(r"[A-Za-z]", "a", s)
    s = re.sub(r"[0-9]", "0", s)
    s = re.sub(r"[一-鿿぀-ヿ가-힯]", "字", s)
    s = re.sub(r"a{4,}", "aaa", s)
    s = re.sub(r"字{4,}", "字字字", s)
    return s if len(s) <= n else s[:n] + "…"


def seg_anon(rich):
    out = []
    for g in rich or []:
        a = g.get("annotations") or {}
        fl = "".join(k[0] for k in M.ANN_KEYS if a.get(k))
        if g.get("type") == "text":
            out.append(f"T[{fl}]{'L' if M._seg_link(g) else ''}:{anon(M._seg_text(g), 40)!r}")
        else:
            out.append(f"{(g.get('type') or '?')[0].upper()}[{fl}]")
    return out[:6]


def rich_arrays(b):
    t = b.get("type")
    v = b.get(t) if isinstance(b.get(t), dict) else {}
    out = []
    if isinstance(v.get("rich_text"), list) and t != "code":
        out.append((v["rich_text"], False, M.MODE_OF.get(t, "inline")))
    if isinstance(v.get("caption"), list) and v["caption"]:
        out.append((v["caption"], False, "inline"))
    if t == "table_row":
        out.extend((c, True, "cell") for c in v.get("cells") or [])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--export-db", default=os.path.join(WORK, "state", "export.db"))
    ap.add_argument("--limit", type=int, default=0, help="random sample of blocks (0 = all)")
    ap.add_argument("--edit-sample", type=int, default=3000)
    ap.add_argument("--norm-sample", type=int, default=3000)
    ap.add_argument("--prefix-sample", type=int, default=3000)
    ap.add_argument("--no-inline", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    t0 = time.time()
    ex = build_exporter(os.path.abspath(args.snapshot), os.path.abspath(args.export_db))
    t_build = time.time() - t0

    bjson, parent_of = {}, {}
    for parent, lst in ex.blocks.items():
        for b in lst:
            bjson[b["id"]] = b
            parent_of[b["id"]] = parent

    def ancestors(bid):
        out = []
        p = parent_of.get(bid)
        while p in bjson:
            out.append(bjson[p].get("type"))
            p = parent_of.get(p)
        return out

    ids = sorted(bjson)
    random.seed(7)
    if args.limit:
        ids = random.sample(ids, min(args.limit, len(ids)))

    res = {"exporter": EO.__file__, "renderer_version": getattr(EO, "RENDERER_VERSION", None),
           "snapshot": args.snapshot, "blocks_total": len(bjson), "blocks_considered": len(ids)}
    by_type = Counter()
    kl = Counter()
    x_reason = Counter()
    x_reason_rtype = Counter()
    type_r = defaultdict(Counter)
    A, A_ex = Counter(), defaultdict(list)
    B, B_ex = Counter(), defaultdict(list)
    r_ids = []
    klass_of = {}

    def add_ex(store, k, item, lim=3):
        if len(store[k]) < lim:
            store[k].append(item)

    for bid in ids:
        b = bjson[bid]
        t = b.get("type")
        by_type[t] += 1
        anc = ancestors(bid)
        if any(a in X_CONTAINERS for a in anc) or t in X_CONTAINERS:
            k, why = "X", "inside_column_or_synced" if t not in X_CONTAINERS else f"type:{t}"
        else:
            k, why = M.klass(ex, b, None)
        klass_of[bid] = k
        kl[k] += 1
        type_r[t][k] += 1
        if k == "X":
            x_reason[why.split(":")[0] if why.startswith(("roundtrip", "md_divergent", "render_error")) else why] += 1
            if t in M.R_TYPES and not why.startswith(("type:", "inside_")):
                x_reason_rtype[why] += 1
                if why.startswith(("roundtrip", "md_divergent")):
                    try:
                        lines = M.render_own_lines(ex, b, "", 1)
                    except Exception:
                        lines = []
                    add_ex(B_ex, "X:" + why + ":" + t, {"lines": [anon(x) for x in lines[:3]],
                                                          "orig": seg_anon((b.get(t) or {}).get("rich_text"))})
            continue
        r_ids.append(bid)
        # ---- level A (inline) for R blocks' arrays
        if not args.no_inline:
            for arr, table, mode in rich_arrays(b):
                if not arr:
                    continue
                A["arrays"] += 1
                md = M._rt(arr, table)
                try:
                    segs, norm = M.parse_inline(md, table=table, mode=mode)
                except M.Refused as e:
                    A["refused:" + e.reason] += 1
                    add_ex(A_ex, "refused:" + e.reason, {"md": anon(md), "orig": seg_anon(arr)})
                    continue
                if norm:
                    A["normalized"] += 1
                    add_ex(A_ex, "normalized", {"md": anon(md), "orig": seg_anon(arr)})
                    continue
                same = M._rt(segs, table) == md
                eq = M._rich_eq(arr, segs, "cell" if table else "inline", "equiv")
                A["rerender_identical"] += same
                A["canon_equal"] += eq
                A["identical_and_equal"] += same and eq
                if not (same and eq):
                    add_ex(A_ex, "mismatch", {"md": anon(md), "orig": seg_anon(arr), "parsed": seg_anon(segs)})
        # ---- level B (block) round trip through rebuild_block
        B["r_blocks"] += 1
        lines = M.render_own_lines(ex, b, "", 1)
        try:
            body, norm = M.rebuild_block(ex, b, lines, "")
        except M.Refused as e:
            B["rebuild_refused:" + e.reason] += 1
            add_ex(B_ex, "rebuild_refused:" + e.reason, {"type": t, "lines": [anon(x) for x in lines[:3]]})
            continue
        b2 = M.apply_update(b, body)
        same = M.render_own_lines(ex, b2, "", 1) == lines
        eq = M.canon_writable(b2, equiv=True) == M.canon_writable(b, equiv=True)
        eq_merge = M.canon_writable(b2) == M.canon_writable(b)
        B["rerender_identical"] += same
        B["canon_equal"] += eq
        B["d12_merge_equal"] += eq_merge
        B["normalized"] += norm
        B["all_ok"] += same and eq and not norm
        type_r[t]["roundtrip_ok"] += same and eq and not norm
        if not (same and eq and not norm):
            add_ex(B_ex, "mismatch:" + t, {"lines": [anon(x) for x in lines[:3]],
                                           "orig": seg_anon((b.get(t) or {}).get("rich_text"))})
    t_main = time.time() - t0

    # ---- prefixes: the same blocks rendered inside a quote / list continuation / nested quote list
    rnd = random.Random(11)
    P = Counter()
    sample = rnd.sample(r_ids, min(args.prefix_sample, len(r_ids)))
    for bid in sample:
        b = bjson[bid]
        for prefix in ("> ", "    ", ">     "):
            lines = M.render_own_lines(ex, b, prefix, 1)
            try:
                body, norm = M.rebuild_block(ex, b, lines, prefix)
                ok = (not norm and M.render_own_lines(ex, M.apply_update(b, body), prefix, 1) == lines
                      and M.canon_writable(M.apply_update(b, body), True) == M.canon_writable(b, True))
            except M.Refused as e:
                ok = False
                P["refused:" + e.reason] += 1
            P["cases"] += 1
            P["ok"] += ok
    # ---- edits
    E, E_ex = Counter(), defaultdict(list)
    text_r = [i for i in r_ids if bjson[i]["type"] in M.TEXT_TYPES]
    for bid in rnd.sample(text_r, min(args.edit_sample, len(text_r))):
        b = bjson[bid]
        lines = M.render_own_lines(ex, b, "", 1)
        if not lines or not lines[-1].strip():
            E["skipped_blank_last_line"] += 1
            continue
        k = len(lines) - 1
        if len(lines) >= 3 and lines[0].startswith("```"):  # fenced paragraph: edit inside the fence
            k = len(lines) - 2
        edited = lines[:k] + [lines[k] + " 改动x"] + lines[k + 1:]
        E["cases"] += 1
        try:
            body, norm = M.rebuild_block(ex, b, edited, "")
        except M.Refused as e:
            E["refused:" + e.reason] += 1
            add_ex(E_ex, "refused:" + e.reason, {"type": b["type"], "lines": [anon(x) for x in edited[-2:]]})
            continue
        b2 = M.apply_update(b, body)
        got = M.render_own_lines(ex, b2, "", 1)
        if norm:
            # an edit that made an exporter escape unnecessary (e.g. '\---' + text): the canonical rendering
            # must itself be exact and stable
            E["normalized"] += 1
            try:
                ok = not M.rebuild_block(ex, b2, got, "")[1]
            except M.Refused:
                ok = False
            E["normalized_stable"] += ok
        else:
            ok = got == edited
        E["ok"] += ok
        if not ok:
            add_ex(E_ex, "mismatch", {"type": b["type"], "want": [anon(x) for x in edited[-2:]],
                                      "got": [anon(x) for x in got[-2:]]})
        # fragment reuse: unchanged leading segments are re-sent verbatim
        t = b["type"]
        orig = [s for s in (b.get(t) or {}).get("rich_text") or [] if M._seg_text(s) != ""]
        req = body[t].get("rich_text") or []
        if orig and req and len(orig) > 1:
            o0 = M.request_rich(orig[:1])[0]
            E["reuse_first_segment_verbatim"] += req[0] == o0
            E["reuse_checked"] += 1

    # ---- normalized edits: CommonMark-only ' a_b' vs exporter-canonical ' a\_b' at the same place
    N, N_ex = Counter(), defaultdict(list)
    norm_pool = [i for i in r_ids if bjson[i]["type"] in M.TEXT_TYPES or bjson[i]["type"] == "table_row"]
    for j, bid in enumerate(rnd.sample(norm_pool, min(args.norm_sample, len(norm_pool)))):
        b = bjson[bid]
        t = b["type"]
        prefix = ("", "> ", "    ")[j % 3]
        lines = M.render_own_lines(ex, b, prefix, 1)
        if t == "table_row":
            cells = M._split_row(lines[0][len(prefix):])
            if not cells:
                N["skipped"] += 1
                continue
            ci = rnd.randrange(len(cells))

            def row_with(word, cells=cells, ci=ci):
                cs = list(cells)
                cs[ci] = cs[ci] + word
                return [prefix + "| " + " | ".join(c if c else " " for c in cs) + " |"]
            v_norm, v_fast = row_with(" a_b"), row_with(" a\\_b")
        else:
            if not lines or not lines[-1][len(prefix):].strip() or (len(lines) >= 3 and lines[0][len(prefix):].startswith("```")):
                N["skipped"] += 1  # blank last line / fenced paragraph (code text is verbatim: nothing to normalize)
                continue
            if t == "callout" and len(lines) == 1:
                N["skipped"] += 1  # header-only callout: the header line is not text
                continue
            v_norm = lines[:-1] + [lines[-1] + " a_b"]
            v_fast = lines[:-1] + [lines[-1] + " a\\_b"]
        N["cases"] += 1
        N["cases_" + t] += 1
        try:
            body_n, norm_n = M.rebuild_block(ex, b, v_norm, prefix)
            body_f, norm_f = M.rebuild_block(ex, b, v_fast, prefix)
        except M.Refused as e:
            N["refused:" + e.reason] += 1
            add_ex(N_ex, "refused:" + e.reason, {"type": t, "prefix": prefix, "lines": [anon(x) for x in v_norm[-2:]]})
            continue
        b_n, b_f = M.apply_update(b, body_n), M.apply_update(b, body_f)
        exact_f = M.render_own_lines(ex, b_f, prefix, 1) == v_fast
        same = M.canon_writable(b_n) == M.canon_writable(b_f)
        N["normalized"] += norm_n
        N["fast_exact"] += (not norm_f) and exact_f
        N["fast_normalized"] += norm_f  # the appended word made an exporter escape unnecessary (e.g. '- \\--')
        N["same_as_fast"] += same
        ok = norm_n and same and (norm_f or exact_f)
        N["ok"] += ok
        if not ok:
            add_ex(N_ex, "mismatch:" + t, {"prefix": prefix, "norm": norm_n, "fast_norm": norm_f, "fast_exact": exact_f,
                                           "lines": [anon(x) for x in v_norm[:3]],
                                           "got": seg_anon(M.writable_fields(b_n).get("rich_text") or []),
                                           "want": seg_anon(M.writable_fields(b_f).get("rich_text") or [])})

    # ---- notes fully R (page/row/template notes; child_page/child_database lines counted as X or ignored)
    notes = [oid for oid, n in ex.nodes.items() if n.get("kind") != "database"]
    full_r = full_r_ign = 0
    if not args.limit:
        for oid in notes:
            ok = ok2 = True
            stack = list(ex.blocks.get(oid, []))
            while stack:
                b = stack.pop()
                t = b.get("type")
                k = klass_of.get(b["id"], "X")
                if t != "table" and k != "R":
                    ok = False
                    if t not in ("child_page", "child_database"):
                        ok2 = False
                if t != "child_page" and b.get("has_children"):
                    stack.extend(ex.blocks.get(b["id"], []))
            full_r += ok
            full_r_ign += ok2
    res.update({
        "seconds_build": round(t_build, 1), "seconds_main": round(t_main, 1),
        "seconds_total": round(time.time() - t0, 1),
        "klass": dict(kl), "x_reasons": dict(x_reason.most_common()),
        "x_reasons_r_types_content": dict(x_reason_rtype.most_common()),
        "r_type_blocks": sum(by_type[t] for t in M.R_TYPES),
        "by_type": {t: dict(type_r[t], total=by_type[t]) for t in sorted(by_type, key=lambda x: -by_type[x])},
        "A_inline": dict(A), "B_block": dict(B), "prefix_check": dict(P), "edit_check": dict(E),
        "normalized_edit_check": dict(N),
        "notes": len(notes), "notes_fully_R": full_r if not args.limit else None,
        "notes_fully_R_ignoring_child_links": full_r_ign if not args.limit else None,
        "examples": {"A": A_ex, "B": B_ex, "E": E_ex, "N": N_ex},
    })
    txt = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)
    summary = {k: v for k, v in res.items() if k not in ("examples", "by_type")}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    bad = ((A["arrays"] - A["identical_and_equal"]) + (B["r_blocks"] - B["all_ok"]) + (P["cases"] - P["ok"])
           + (E["cases"] - E["ok"]) + (N["cases"] - N["ok"]))
    print("CORPUS", "OK" if bad == 0 else f"FAIL ({bad} failing cases)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
