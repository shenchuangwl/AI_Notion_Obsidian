#!/usr/bin/env python3
"""Full-corpus regression for the exporter (module B, IMPL_CONTRACT §5.7).  Run explicitly (slow, ~5 min):

  cd /data120/Obsidian/notion-obsidian-dev
  python3 -I -B tools/tests/regress_export_bytes.py [--run-id dev-B-regress] [--tmp DIR] [--reuse] [--keep-ovr]

What it does (all reads of production paths are read-only; all writes go to <sandbox>/staging and --tmp):
 1. exports the sandbox snapshot copy with the production sidebar evidence into staging/<run-id> (state = a COPY
    of the sandbox state/export.db) and compares every file's sha256 with the production manifest
    staging/20261117T1859-T1/manifest.json (expect 100 % identical, manifest.json byte-identical);
 2. checks block_lines.jsonl invariants against the staged files and the snapshot: own ranges + gap rows
    partition every note body, no untagged line, contiguous subtrees, every snapshot block of each note
    accounted for, foreign-owner / synced rows are X;
 3. Exporter.for_planning + render_page(sandbox=True, record=True) reproduces every note's bytes and its
    block_lines rows, without side effects; also without attachments.json (rebuilt map identical);
 3b. R-042: every 'R?' block re-renders alone (recorded prefix + list number) to its own lines; every
    'own_line_lost' X block does not;
 4. overlay edit demo (synthetic edit + inserted numbered item + an appended block without id);
 5. name overrides: in-memory planning checks + a full export with --overrides into staging/<run-id>-ovr
    compared file by file with run 1 (file at local_rel, links/sortspec/index/mapping use it, aliases unchanged,
    cross-folder and folder-note overrides rejected).
Prints a JSON summary; exit code 0 only if every check passes.  No note text is printed (ids and counts only;
the overlay demo prints only its synthetic lines).
"""
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
SANDBOX = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import export_obsidian as exp  # noqa: E402
from sortspec import build_sortspec  # noqa: E402

PROD = "/data120/Obsidian/notion-obsidian"
SNAP = os.path.join(SANDBOX, "snapshots", "snap_20261117T1859.db")
SIDEBAR = os.path.join(PROD, "evidence", "sidebar_20261117T0417.json")
PROD_MANIFEST = os.path.join(PROD, "staging", "20261117T1859-T1", "manifest.json")
STATE_SRC = os.path.join(SANDBOX, "state", "export.db")
NOTE_KINDS = ("page", "row", "template")


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_export(run_id, state_copy, overrides=None):
    out = os.path.join(SANDBOX, "staging", run_id)
    cmd = [sys.executable, "-I", "-B", os.path.join(TOOLS, "export_obsidian.py"), "--snapshot", SNAP, "--sidebar", SIDEBAR,
           "--run-id", run_id, "--state", state_copy, "--out", out, "--immutable"]
    if overrides:
        cmd += ["--overrides", overrides]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"export failed rc={p.returncode}: {p.stderr[-2000:]}")
    return out, round(time.time() - t, 1)


def body_bounds(text):
    """(first_body_line, last_line) 1-based; first > last when the note has no body."""
    lines = text.split("\n")
    assert lines[0] == "---"
    j = lines.index("---", 1)                      # closing frontmatter fence (0-based)
    last = len(lines) - 1                          # text ends with "\n"
    first = j + 3
    return first, last


# ------------------------------------------------------------------ 1. bytes
def check_bytes(out):
    prod = json.load(open(PROD_MANIFEST, encoding="utf-8"))
    dev = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
    same = sum(1 for k, v in prod.items() if k in dev and dev[k]["sha256"] == v["sha256"])
    diff = sorted(k for k, v in prod.items() if k in dev and dev[k]["sha256"] != v["sha256"])
    meta_diff = sorted(k for k, v in prod.items() if k in dev and dev[k] != v)
    # the files on disk really have the manifest's sha
    disk_bad = [k for k, v in dev.items() if sha_file(os.path.join(out, "vault", k)) != v["sha256"]]
    on_disk = set()
    for root, _, files in os.walk(os.path.join(out, "vault")):
        for f in files:
            on_disk.add(os.path.relpath(os.path.join(root, f), os.path.join(out, "vault")).replace(os.sep, "/"))
    res = {"prod_files": len(prod), "dev_files": len(dev), "identical": same, "different": len(diff),
           "only_prod": len(set(prod) - set(dev)), "only_dev": len(set(dev) - set(prod)),
           "manifest_entry_diff": len(meta_diff), "disk_sha_mismatch": len(disk_bad),
           "files_not_in_manifest": len(on_disk - set(dev)),
           "manifest_json_byte_identical": sha_file(PROD_MANIFEST) == sha_file(os.path.join(out, "manifest.json")),
           "examples": diff[:5]}
    res["ok"] = (same == len(prod) == len(dev) and not meta_diff and not disk_bad and not (on_disk - set(dev))
                 and res["manifest_json_byte_identical"])
    return res, dev


# ------------------------------------------------------------------ 2. block_lines invariants
def iter_rows_by_note(path):
    cur, rows = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["notion_id"] != cur:
                if cur is not None:
                    yield cur, rows
                cur, rows = r["notion_id"], []
            rows.append(r)
    if cur is not None:
        yield cur, rows


def check_block_lines(out, manifest):
    snap = sqlite3.connect(f"file:{SNAP}?mode=ro&immutable=1", uri=True)
    snap_blocks = defaultdict(dict)
    snap_pos = {}
    for bid, pid, typ, parent, ordv in snap.execute("SELECT id, page_id, type, parent_id, ord FROM blocks"):
        snap_blocks[pid][bid] = typ
        snap_pos[bid] = (parent, ordv)
    snap.close()
    notes = {}
    for line in open(os.path.join(out, "page_json.jsonl"), encoding="utf-8"):
        r = json.loads(line)
        notes[r["notion_id"]] = r
    st = Counter()
    problems = defaultdict(list)
    klass = Counter()
    xreason = Counter()
    missing_types = Counter()
    seen_notes = set()
    fully_r = 0
    snap_total_rendered = 0
    for nid, rows in iter_rows_by_note(os.path.join(out, "block_lines.jsonl")):
        if nid in seen_notes:
            problems["note_rows_not_contiguous_in_file"].append(nid)
        seen_notes.add(nid)
        pj = notes.get(nid)
        if not pj or pj["kind"] not in NOTE_KINDS:
            problems["rows_for_unknown_note"].append(nid)
            continue
        text = open(os.path.join(out, "vault", pj["rel_path"]), encoding="utf-8").read()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if any(r["base_sha"] != sha for r in rows) or sha != manifest[pj["rel_path"]]["sha256"]:
            problems["base_sha_mismatch"].append(nid)
        if [r["seq"] for r in rows] != list(range(len(rows))):
            problems["seq_not_dense"].append(nid)
        if any(r["renderer_version"] != exp.RENDERER_VERSION for r in rows):
            problems["renderer_version"].append(nid)
        first, last = body_bounds(text)
        st["notes"] += 1
        st["body_lines"] += max(0, last - first + 1)
        cover = Counter()
        own_of_line = {}
        blocks = [r for r in rows if r["type"] != "gap"]
        gaps = [r for r in rows if r["type"] == "gap"]
        st["block_rows"] += len(blocks)
        st["gap_rows"] += len(gaps)
        for i, r in enumerate(rows):
            if r["type"] == "gap":
                if r["x_reason"] == "untagged":
                    st["untagged_lines"] += r["line_end"] - r["line_start"] + 1
                for ln in range(r["own_start"], r["own_end"] + 1):
                    cover[ln] += 1
            elif r["zero_line"]:
                st["zero_line"] += 1
                # the anchor is an existing line: the blank line before the body, or the closing frontmatter
                # fence when the note has no body at all
                head = first - 1 if last >= first else last
                if last < first:
                    st["zero_line_in_bodyless_note"] += 1
                if not (r["own_start"] == r["own_end"] == r["line_start"] == r["line_end"]) or not (head <= r["line_start"] <= last):
                    problems["zero_line_anchor"].append((nid, r["block_id"]))
            else:
                if r["own_start"] is not None:
                    if not (r["line_start"] <= r["own_start"] <= r["own_end"] <= r["line_end"]):
                        problems["own_outside_range"].append((nid, r["block_id"]))
                    for ln in range(r["own_start"], r["own_end"] + 1):
                        cover[ln] += 1
                        own_of_line[ln] = i
                else:
                    st["blocks_without_own_lines"] += 1
        # partition of the body: every line exactly once, nothing outside
        holes = [ln for ln in range(first, last + 1) if cover[ln] == 0]
        dup = [ln for ln, c in cover.items() if c > 1]
        outside = [ln for ln in cover if ln < first or ln > last]
        if holes or dup or outside:
            problems["partition"].append((nid, len(holes), len(dup), len(outside)))
        # render tree from parent_block_id (orphans = synced-source children -> innermost containing synced_block)
        by_id = defaultdict(list)
        for i, r in enumerate(rows):
            if r["type"] != "gap":
                by_id[r["block_id"]].append(i)
        parent = {}
        for i, r in enumerate(rows):
            if r["type"] == "gap" or r["zero_line"]:
                continue
            cand = [j for j in by_id.get(r["parent_block_id"], []) if not rows[j]["zero_line"]
                    and rows[j]["line_start"] <= r["line_start"] and r["line_end"] <= rows[j]["line_end"]]
            if r["parent_block_id"] is None:
                parent[i] = None
            elif cand:
                parent[i] = cand[0]
            else:
                syn = [j for j, q in enumerate(rows) if q["type"] == "synced_block" and not q["zero_line"] and j != i
                       and q["line_start"] <= r["line_start"] and r["line_end"] <= q["line_end"]]
                if syn:
                    parent[i] = min(syn, key=lambda j: rows[j]["line_end"] - rows[j]["line_start"])
                    st["orphans_attached_to_synced"] += 1
                    if rows[i]["klass"] != "X":
                        problems["synced_child_not_x"].append((nid, r["block_id"]))
                else:
                    parent[i] = None
                    problems["no_parent"].append((nid, r["block_id"], r["type"]))

        def is_desc(j, i):
            hops = 0
            while j is not None and hops < 1000:
                if j == i:
                    return True
                j = parent.get(j)
                hops += 1
            return False
        for i, r in enumerate(rows):
            if r["type"] == "gap" or r["zero_line"]:
                continue
            p = parent.get(i)
            if p is not None and not (rows[p]["line_start"] <= r["line_start"] and r["line_end"] <= rows[p]["line_end"]):
                problems["child_outside_parent"].append((nid, r["block_id"]))
            for ln in range(r["line_start"], r["line_end"] + 1):
                j = own_of_line.get(ln)
                if j is not None and not is_desc(j, i):
                    problems["contiguity"].append((nid, r["block_id"]))
                    break
        # snapshot accounting
        sb = snap_blocks.get(nid, {})
        rendered = {r["block_id"] for r in blocks}
        snap_total_rendered += len(set(sb) & rendered)
        for bid in set(sb) - rendered:
            missing_types[sb[bid]] += 1
        foreign = [r for r in blocks if r["owner_page_id"] != nid]
        st["foreign_owner_rows"] += len(foreign)
        if any(r["klass"] != "X" for r in foreign):
            problems["foreign_owner_not_x"].append(nid)
        syn_ids = {r["block_id"] for r in blocks if r["type"] == "synced_block"}
        for r in blocks:   # ord / parent_block_id / owner come from the snapshot
            par, ordv = snap_pos.get(r["block_id"], (None, None))
            if r["ord"] != ordv or (r["parent_block_id"] or nid) != par:
                problems["ord_or_parent_differs_from_snapshot"].append((nid, r["block_id"]))
        for r in blocks:
            klass[r["klass"]] += 1
            xreason[r["x_reason"]] += 1
            if r["parent_block_id"] in syn_ids and r["klass"] != "X":
                problems["synced_child_not_x"].append((nid, r["block_id"]))
        if all(r["klass"] == "R?" for r in blocks):
            fully_r += 1
    expected_notes = {k for k, v in notes.items() if v["kind"] in NOTE_KINDS}
    no_rows = expected_notes - seen_notes
    # a note without rows must be a note without blocks
    for nid in no_rows:
        if snap_blocks.get(nid):
            problems["note_without_rows"].append(nid)
    snap_in_notes = sum(len(snap_blocks.get(n, {})) for n in expected_notes)
    res = {"stats": dict(st), "notes_with_rows": len(seen_notes), "notes_without_blocks": len(no_rows),
           "snapshot_blocks_of_exported_notes": snap_in_notes, "snapshot_blocks_rendered": snap_total_rendered,
           "snapshot_blocks_not_rendered_by_type": dict(missing_types),
           "snapshot_blocks_total": sum(len(v) for v in snap_blocks.values()),
           "klass": dict(klass), "x_reason_top": dict(Counter({str(k): v for k, v in xreason.items()}).most_common(25)),
           "notes_fully_R_candidate": fully_r,
           "problems": {k: {"count": len(v), "examples": v[:3]} for k, v in problems.items()}}
    res["ok"] = not problems and st["untagged_lines"] == 0 and snap_total_rendered == snap_in_notes
    return res


# ------------------------------------------------------------------ 3. render_page == file bytes
def rows_digest(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return h.hexdigest()


def check_render_page(out, state_copy, manifest):
    file_digest = {}
    for nid, rows in iter_rows_by_note(os.path.join(out, "block_lines.jsonl")):
        file_digest[nid] = rows_digest(rows)
    t = time.time()
    ex = exp.Exporter.for_planning(SNAP, state_copy, attachments_json=os.path.join(out, "attachments.json"))
    t_build = round(time.time() - t, 1)
    before = (json.dumps({k: dict(v) for k, v in ex.report.items()}, sort_keys=True), json.dumps(ex.lists, sort_keys=True, default=str),
              json.dumps(ex.att_map, sort_keys=True), len(ex.att_by_sha), len(ex.attachments))
    same = diff = rows_same = rows_diff = 0
    bad = []
    t = time.time()
    for oid in ex.run_order():
        n = ex.nodes[oid]
        text, rows = ex.render_page(oid, record=True, sandbox=True)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == manifest[n["note"]]["sha256"]:
            same += 1
        else:
            diff += 1
            bad.append(oid)
        if n["kind"] != "database":
            if rows_digest(rows) == file_digest.get(oid, rows_digest([])):
                rows_same += 1
            else:
                rows_diff += 1
    t_render = round(time.time() - t, 1)
    after = (json.dumps({k: dict(v) for k, v in ex.report.items()}, sort_keys=True), json.dumps(ex.lists, sort_keys=True, default=str),
             json.dumps(ex.att_map, sort_keys=True), len(ex.att_by_sha), len(ex.attachments))
    # without attachments.json: the map is rebuilt by an in-memory pass
    ex2 = exp.Exporter.for_planning(SNAP, state_copy)
    rebuilt_equal = ex2.att_map == ex.att_map
    # page_json.jsonl == the reference D-10 definition (canonical dict form, not the exporter's composed form)
    import wb_norm
    pj_bad = 0
    with open(os.path.join(out, "page_json.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            n = ex.nodes[r["notion_id"]]
            blocks = [] if n["kind"] == "database" else wb_norm.page_blocks_dfs(ex.blocks.get, n["id"])
            ref = hashlib.sha256(wb_norm.canon_json({"page": wb_norm.json_norm({k: n["data"].get(k) for k in wb_norm.PAGE_KEYS}),
                                                     "blocks": [wb_norm.json_norm(b) for b in blocks]}).encode("utf-8")).hexdigest()
            pj_bad += ref != r["page_json_sha"] or r["sha256"] != manifest[r["rel_path"]]["sha256"]
    # link index matches the manifest paths
    li = ex.link_index()
    li_ok = all(manifest.get(v["note"], {}).get("notion_id") == k for k, v in li.items())
    res = {"notes": same + diff, "bytes_identical": same, "bytes_different": diff, "examples": bad[:5],
           "rows_identical": rows_same, "rows_different": rows_diff, "sandbox_no_side_effects": before == after,
           "attachments_map_rebuilt_equal": rebuilt_equal, "link_index_matches_manifest": li_ok,
           "page_json_sha_reference_mismatch": pj_bad,
           "for_planning_s": t_build, "render_all_s": t_render}
    res["ok"] = diff == 0 and rows_diff == 0 and before == after and rebuilt_equal and li_ok and pj_bad == 0
    return res, ex


# ------------------------------------------------------------------ 3b. R-042 per-block re-render
def render_alone(ex, b, prefix, own_lines, ts):
    """The block rendered on its own (children cut) with the recorded prefix and list number, no side effects."""
    b2 = dict(b)
    b2["has_children"] = False
    saved = (ex.report, ex.lists, ex._sandbox, ex._list_num, ex._rec)
    ex.report, ex.lists, ex._sandbox, ex._rec = defaultdict(Counter), defaultdict(list), True, None
    try:
        if b.get("type") == "numbered_list_item":
            m = re.match(re.escape(prefix) + r"(\d+)\.", own_lines[0] if own_lines else "")
            ex._list_num = int(m.group(1)) if m else 1
        return [str(x) for x in ex.render_block(b2, ts, prefix)]
    finally:
        ex.report, ex.lists, ex._sandbox, ex._list_num, ex._rec = saved


def check_r042(out, ex):
    """R-042: every non-zero-line 'R?' block with own lines re-renders ALONE (children cut, recorded prefix and
    list number) to exactly lines[own_start-1:own_end]; every 'own_line_lost' X block does NOT (that is why it
    is X).  table_row lines depend on the table width and are left to md2blocks (counted, not checked)."""
    by_id = {b["id"]: b for lst in ex.blocks.values() for b in lst}
    st = Counter()
    bad, lost_same = [], []
    for nid, rows in iter_rows_by_note(os.path.join(out, "block_lines.jsonl")):
        n = ex.nodes[nid]
        lines = open(os.path.join(out, "vault", n["note"]), encoding="utf-8").read().split("\n")
        for r in rows:
            if r["type"] == "gap":
                continue
            if r["x_reason"] in ("own_line_lost", "anchor_link"):
                st[r["x_reason"] + "_rows"] += 1
            if r["zero_line"] or r["own_start"] is None:
                if r["x_reason"] == "own_line_lost":
                    st["own_line_lost_without_own_lines"] += 1
                continue
            if not (r["klass"] == "R?" or r["x_reason"] == "own_line_lost"):
                continue
            if r["type"] == "table_row":
                st["table_row_not_checked"] += 1
                continue
            b = by_id[r["block_id"]]
            own = lines[r["own_start"] - 1:r["own_end"]]
            got = render_alone(ex, b, r["prefix"], own, n["ts"])
            kind = "container" if b.get("has_children") else "leaf"
            if r["klass"] == "R?":
                st[f"r_{kind}_checked"] += 1
                if got != own:
                    bad.append((nid, r["block_id"], r["type"]))
            else:
                st[f"own_line_lost_{kind}_checked"] += 1
                if got == own:
                    lost_same.append((nid, r["block_id"], r["type"]))
    res = {"stats": dict(st), "r_mismatch": len(bad), "r_mismatch_examples": bad[:5],
           "own_line_lost_but_rerender_equal": len(lost_same), "own_line_lost_examples": lost_same[:5]}
    res["ok"] = not bad and not lost_same and st["r_leaf_checked"] > 0
    return res


# ------------------------------------------------------------------ 4. overlay edit demo
def check_overlay(ex):
    pick = None
    for oid in ex.run_order():
        n = ex.nodes[oid]
        if n["kind"] == "database":
            continue
        bl = ex.blocks.get(oid, [])
        if not (6 <= len(bl) <= 40):
            continue
        ts = [b["type"] for b in bl]
        has_par = any(b["type"] == "paragraph" and exp.plain(b["paragraph"]["rich_text"]).strip()
                      and ex.structural_klass(b, oid)[0] == "R?" for b in bl)
        if has_par and any(ts[i:i + 3] == ["numbered_list_item"] * 3 for i in range(len(ts) - 2)):
            pick = oid
            break
    if not pick:
        return {"ok": False, "error": "no suitable page"}
    before, rows_b = ex.render_page(pick, record=True, sandbox=True)
    rep_before = json.dumps({k: dict(v) for k, v in ex.report.items()}, sort_keys=True)
    top = copy.deepcopy(ex.blocks[pick])
    edited = None
    for b in top:
        if b["type"] == "paragraph" and exp.plain(b["paragraph"]["rich_text"]).strip() and ex.structural_klass(b, pick)[0] == "R?":
            b["paragraph"]["rich_text"] = [{"type": "text", "text": {"content": "EDITED *text* [x]", "link": None},
                                            "annotations": {"bold": True}, "plain_text": "EDITED *text* [x]"}]
            edited = b["id"]
            break
    i = [b["type"] for b in top].index("numbered_list_item")
    new = copy.deepcopy(top[i])
    new["id"] = "00000000-0000-4000-8000-000000000001"
    new["has_children"] = False
    new["numbered_list_item"]["rich_text"] = [{"type": "text", "text": {"content": "inserted item", "link": None},
                                               "annotations": {}, "plain_text": "inserted item"}]
    top.insert(i + 1, new)
    # a block the planner would APPEND has no id yet: it must be recorded too (block_id null)
    top.append({"object": "block", "type": "paragraph", "has_children": False,
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": "appended new block", "link": None},
                                             "annotations": {}, "plain_text": "appended new block"}], "color": "default"}})
    after, rows_a = ex.render_page(pick, blocks={pick: top}, record=True, sandbox=True)
    again, _ = ex.render_page(pick, sandbox=True)
    import difflib
    bl, al = before.split("\n"), after.split("\n")
    plus = [x for x in difflib.unified_diff(bl, al, lineterm="", n=0) if x.startswith("+") and not x.startswith("+++")]
    minus = [x for x in difflib.unified_diff(bl, al, lineterm="", n=0) if x.startswith("-") and not x.startswith("---")]
    renum = sum(1 for x in plus if re.match(r"^\+\s*\d+\. ", x) and "inserted item" not in x)
    synthetic = [x for x in plus if "EDITED" in x or "inserted item" in x or "appended new block" in x]
    rb = {r["block_id"]: r for r in rows_b if r["block_id"]}
    ra = {r["block_id"]: r for r in rows_a if r["block_id"]}
    idless = [r for r in rows_a if r["type"] != "gap" and r["block_id"] is None]
    unchanged_sha = all(ra[k]["json_sha"] == rb[k]["json_sha"] for k in rb if k in ra and k != edited)
    ed_line = al[ra[edited]["own_start"] - 1] if edited in ra else None
    new_row = ra.get(new["id"])
    new_line = al[new_row["own_start"] - 1] if new_row else None
    res = {"page": pick, "diff_plus": len(plus), "diff_minus": len(minus), "renumbered_lines": renum,
           "synthetic_lines": synthetic, "edited_row_points_at_edit": bool(ed_line and "EDITED" in ed_line),
           "inserted_row_points_at_insert": bool(new_line and "inserted item" in new_line),
           "inserted_row_owner_is_page": bool(new_row and new_row["owner_page_id"] == pick),
           "idless_row_points_at_append": (len(idless) == 1 and al[idless[0]["own_start"] - 1] == "appended new block"
                                           and idless[0]["owner_page_id"] == pick and idless[0]["ord"] == len(top) - 1),
           "rows_partition_after_edit": not body_partition_problems(after, rows_a),
           "untouched_blocks_json_sha_unchanged": unchanged_sha,
           "edited_json_sha_changed": edited in ra and ra[edited]["json_sha"] != rb[edited]["json_sha"],
           "rerender_without_overlay_identical": again == before,
           "no_side_effects": rep_before == json.dumps({k: dict(v) for k, v in ex.report.items()}, sort_keys=True)}
    res["ok"] = all(v for k, v in res.items() if k not in ("page", "diff_plus", "diff_minus", "synthetic_lines")) \
        and len(synthetic) == 3 and renum >= 1
    return res


def body_partition_problems(text, rows):
    first, last = body_bounds(text)
    cover = Counter()
    for r in rows:
        if r["type"] == "gap" or (not r["zero_line"] and r["own_start"] is not None):
            for ln in range(r["own_start"], r["own_end"] + 1):
                cover[ln] += 1
    return [ln for ln in range(first, last + 1) if cover[ln] != 1] + [ln for ln in cover if ln < first or ln > last]


# ------------------------------------------------------------------ 5. overrides
def pick_override_target(ex, manifest, out):
    """A leaf page (no children) whose parent note links to it, plus a page with children."""
    leaf = folder = None
    for oid in ex.run_order():
        n = ex.nodes[oid]
        if n["kind"] == "page" and not n["children"] and n["parent"] and ex.nodes[n["parent"]]["kind"] == "page" and leaf is None:
            ptxt = open(os.path.join(out, "vault", ex.nodes[n["parent"]]["note"]), encoding="utf-8").read()
            if f"[[{n['note'][:-3]}|" in ptxt:
                leaf = oid
        if n["kind"] == "page" and n["children"] and n["parent"] and folder is None:
            folder = oid
        if leaf and folder:
            break
    return leaf, folder


def check_overrides(out, state_copy, manifest, ex, tmp, run_id, keep_ovr):
    leaf, folder = pick_override_target(ex, manifest, out)
    ln = ex.nodes[leaf]
    d = os.path.dirname(ln["note"])
    new_name = f"本地改名 {leaf[:8]}"
    new_rel = f"{d}/{new_name}.md"
    other_dir = os.path.dirname(os.path.dirname(ln["note"])) or exp.TEAMSPACES[0]
    # a second leaf for the cross-folder test
    leaf2 = next(o for o in ex.run_order() if o != leaf and ex.nodes[o]["kind"] == "page" and not ex.nodes[o]["children"]
                 and os.path.dirname(ex.nodes[o]["note"]) != other_dir)
    fn = ex.nodes[folder]
    ovr = {leaf: new_rel,
           leaf2: f"{other_dir}/elsewhere.md",
           folder: f"{os.path.dirname(fn['note'])}/renamed folder note.md"}
    res = {"leaf": leaf, "leaf2_cross_folder": leaf2, "folder_note": folder}
    # (a) in memory
    ex2 = exp.Exporter.for_planning(SNAP, state_copy, overrides=ovr, attachments_json=os.path.join(out, "attachments.json"))
    rej = {r["notion_id"]: r["reason"] for r in ex2.override_rejected}
    res["mem_note_path"] = ex2.nodes[leaf]["note"] == new_rel
    res["mem_link_index"] = ex2.link_index()[leaf]["note"] == new_rel
    ptext, _ = ex2.render_page(ln["parent"], sandbox=True)
    res["mem_parent_links_new"] = f"[[{new_rel[:-3]}|" in ptext and f"[[{ln['note'][:-3]}|" not in ptext
    own, _ = ex2.render_page(leaf, sandbox=True)
    res["mem_note_bytes_unchanged"] = hashlib.sha256(own.encode("utf-8")).hexdigest() == manifest[ln["note"]]["sha256"]
    spec, spec0 = build_sortspec(ex2), build_sortspec(ex)
    old_line = f"  /:files. {ln['name']}.md\n"
    res["mem_sortspec"] = (spec.count(f"  /:files. {new_name}.md\n") == 1
                           and spec0.count(old_line) - spec.count(old_line) == 1)
    res["mem_rejected"] = rej
    res["mem_rejects_ok"] = rej.get(leaf2) == "different_folder" and rej.get(folder) == "folder_note" and leaf not in rej
    # (b) full export with --overrides
    ovr_path = os.path.join(tmp, "overrides.json")
    json.dump(ovr, open(ovr_path, "w", encoding="utf-8"), ensure_ascii=False)
    st2 = os.path.join(tmp, "export_ovr.db")
    shutil.copy2(STATE_SRC, st2)
    os.chmod(st2, 0o600)
    stale = os.path.join(SANDBOX, "staging", run_id + "-ovr")
    if os.path.isdir(stale):
        shutil.rmtree(stale)
    out2, secs = run_export(run_id + "-ovr", st2, ovr_path)
    res["ovr_export_s"] = secs
    m1 = manifest
    m2 = json.load(open(os.path.join(out2, "manifest.json"), encoding="utf-8"))
    only1 = sorted(set(m1) - set(m2))
    only2 = sorted(set(m2) - set(m1))
    changed = sorted(k for k in set(m1) & set(m2) if m1[k]["sha256"] != m2[k]["sha256"])
    linkers = set()
    for k in changed:
        if m1[k]["kind"] in ("page", "row", "template", "database"):
            txt = open(os.path.join(out2, "vault", k), encoding="utf-8").read()
            if f"[[{new_rel[:-3]}|" in txt or f"[[{new_rel[:-3]}\\|" in txt:
                linkers.add(k)
    gen = {k for k in changed if m1[k]["kind"] == "generated"}
    res["ovr_only_old"] = only1 == [ln["note"]]
    res["ovr_only_new"] = only2 == [new_rel]
    res["ovr_same_bytes_at_new_path"] = m2.get(new_rel, {}).get("sha256") == m1[ln["note"]]["sha256"]
    res["ovr_changed_files"] = len(changed)
    res["ovr_changed_are_linkers_or_generated"] = set(changed) == linkers | gen and bool(linkers)
    res["ovr_generated_changed"] = sorted(gen)
    gidx = open(os.path.join(out2, "vault", "Notion_Obsidian", "Godcreate 目录索引.md"), encoding="utf-8").read()
    gmap = open(os.path.join(out2, "vault", "Notion_Obsidian", "映射清单.md"), encoding="utf-8").read()
    gss = open(os.path.join(out2, "vault", "Notion_Obsidian", "sortspec.md"), encoding="utf-8").read()
    res["ovr_index_uses_new"] = f"[[{new_rel[:-3]}|" in gidx and f"[[{ln['note'][:-3]}|" not in gidx
    res["ovr_mapping_uses_new"] = f"`{new_rel}`" in gmap and f"`{ln['note']}`" not in gmap
    res["ovr_sortspec_uses_new"] = f"/:files. {new_name}.md" in gss
    rep = json.load(open(os.path.join(out2, "export_report.json"), encoding="utf-8"))
    o = rep.get("overrides", {})
    res["ovr_report"] = {"given": o.get("given"), "applied": len(o.get("applied", [])),
                         "rejected": {r["notion_id"]: r["reason"] for r in o.get("rejected", [])}}
    res["ovr_report_ok"] = (o.get("given") == 3 and [a["notion_id"] for a in o.get("applied", [])] == [leaf]
                            and res["ovr_report"]["rejected"] == {leaf2: "different_folder", folder: "folder_note"})
    db = sqlite3.connect(f"file:{st2}?mode=ro", uri=True)
    rel, name = db.execute("SELECT rel_path, name FROM objects_map WHERE notion_id=?", (leaf,)).fetchone()
    db.close()
    res["ovr_objects_map"] = rel == new_rel and name == ln["name"]
    # page_json / block_lines of the overridden note point at the new path, same sha
    pj = {json.loads(x)["notion_id"]: json.loads(x) for x in open(os.path.join(out2, "page_json.jsonl"), encoding="utf-8")}
    res["ovr_page_json"] = pj[leaf]["rel_path"] == new_rel and pj[leaf]["sha256"] == m1[ln["note"]]["sha256"]
    res["ok"] = all(v for k, v in res.items() if k.startswith(("mem_", "ovr_")) and isinstance(v, bool))
    if not keep_ovr:
        for p in ("vault", "block_lines.jsonl"):
            q = os.path.join(out2, p)
            if os.path.isdir(q):
                shutil.rmtree(q)
            elif os.path.exists(q):
                os.remove(q)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="dev-B-regress")
    ap.add_argument("--tmp", default=None, help="directory for state copies (default: tempfile)")
    ap.add_argument("--reuse", action="store_true", help="reuse an existing staging/<run-id> export")
    ap.add_argument("--keep-ovr", action="store_true", help="keep the override run's vault/ and block_lines")
    a = ap.parse_args()
    assert os.path.realpath(SANDBOX) != os.path.realpath(PROD), "must run from the sandbox"
    tmp = tempfile.mkdtemp(prefix="regress_export_", dir=a.tmp)
    summary = {"renderer_version": exp.RENDERER_VERSION, "snapshot": SNAP, "sidebar": SIDEBAR, "prod_manifest": PROD_MANIFEST}
    out = os.path.join(SANDBOX, "staging", a.run_id)
    state_copy = os.path.join(tmp, "export.db")
    if a.reuse and os.path.isdir(os.path.join(out, "vault")):
        shutil.copy2(STATE_SRC, state_copy)
        summary["export_s"] = None
    else:
        if os.path.exists(out):
            raise SystemExit(f"{out} exists; pass --reuse or another --run-id")
        shutil.copy2(STATE_SRC, state_copy)
        os.chmod(state_copy, 0o600)
        out, secs = run_export(a.run_id, state_copy)
        summary["export_s"] = secs
    summary["out"] = out
    t = time.time()
    summary["bytes"], manifest = check_bytes(out)
    print("bytes:", json.dumps(summary["bytes"], ensure_ascii=False), flush=True)
    summary["block_lines"] = check_block_lines(out, manifest)
    print("block_lines:", json.dumps(summary["block_lines"], ensure_ascii=False), flush=True)
    summary["render_page"], ex = check_render_page(out, state_copy, manifest)
    print("render_page:", json.dumps(summary["render_page"], ensure_ascii=False), flush=True)
    summary["r042"] = check_r042(out, ex)
    print("r042:", json.dumps(summary["r042"], ensure_ascii=False), flush=True)
    summary["overlay"] = check_overlay(ex)
    print("overlay:", json.dumps(summary["overlay"], ensure_ascii=False), flush=True)
    summary["overrides"] = check_overrides(out, state_copy, manifest, ex, tmp, a.run_id, a.keep_ovr)
    print("overrides:", json.dumps(summary["overrides"], ensure_ascii=False), flush=True)
    summary["checks_s"] = round(time.time() - t, 1)
    summary["ok"] = all(summary[k]["ok"] for k in ("bytes", "block_lines", "render_page", "r042", "overlay", "overrides"))
    json.dump(summary, open(os.path.join(out, "regress_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(tmp, ignore_errors=True)
    print("OVERALL", "PASS" if summary["ok"] else "FAIL", flush=True)
    sys.exit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
