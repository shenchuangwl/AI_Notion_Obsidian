#!/usr/bin/env python3
"""Tests for refresh.py orchestration, trash_archive.py and capture_sidebar.py (IMPL_CONTRACT §4 row G, §5.11, §7.9,
§7.11; WRITEBACK_SPEC §6.0, §9, §14; speccheck 2.H / 2.P).

The external tools (capture_sidebar, snapshot, assets_fetch, export_obsidian, verify_vault, publish_vault) are replaced
by an in-process fake runner that writes the artefacts the real tools write (manifest/export_report/page_json, verify.json,
publish_plan.json with an embedded plan_sha256, journal), so every §14.3 condition can be driven exactly. Notion reads
(member count, new-root check) run a real ReadOnlyClient against tools/tests/fake_notion.py. The expected-deviations
file is validated with the REAL verifier (verify_vault.Deviations.bind via verify()) on D's fixture vault.
The human gate is exercised for refusal only, through the CLI in a subprocess.

Run: cd /data120/Obsidian/notion-obsidian-dev && python3 -I -B -m unittest discover -s tools/tests -p 'test_refresh_pause.py'
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
SANDBOX = os.path.dirname(TOOLS)
for p in (TOOLS, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import capture_sidebar  # noqa: E402
import refresh  # noqa: E402
import trash_archive  # noqa: E402
import wb_common  # noqa: E402
import wb_locks  # noqa: E402
import wb_state  # noqa: E402


def uid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def sha(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode("utf-8")).hexdigest()


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


H, A, B, C, D, X = (uid(i) for i in (1, 2, 3, 4, 5, 6))   # Home (root), A, B (leaves), C (has child D), X (root)
PARENT = {H: None, A: H, B: H, C: H, D: C, X: None}
TITLE = {H: "Home", A: "A", B: "B", C: "C", D: "D", X: "X"}
REL = {H: "General/Home/Home.md", A: "General/Home/A.md", B: "General/Home/B.md", C: "General/Home/C/C.md",
       D: "General/Home/C/D.md", X: "x_to_notion/X.md"}
TS = "2026-01-02T03:04:00.000Z"


def note(nid, body="Body.\n"):
    return f'---\nnotion_id: "{nid}"\nnotion_kind: page\nnotion_title: "{TITLE.get(nid, "?")}"\n---\n\n{body}'


def page_obj(pid, title, parent):
    par = {"type": "workspace", "workspace": True} if parent is None else {"type": "page_id", "page_id": parent}
    return {"object": "page", "id": pid, "in_trash": False, "parent": par, "last_edited_time": TS,
            "properties": {"title": {"id": "title", "type": "title", "title": [{"type": "text", "plain_text": title,
                                                                                "text": {"content": title}}]}}}


def make_snapshot(path, pages, finished=True, link_from=None):
    """nmig-shaped snapshot with `pages` {id: (parent, title)}; optional link_to_page block in `link_from`."""
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE objects(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT, data TEXT, fetched_at TEXT, PRIMARY KEY(id, kind));
    CREATE TABLE blocks(id TEXT PRIMARY KEY, page_id TEXT, parent_id TEXT, ord INT, type TEXT, has_children INT, data TEXT);
    CREATE INDEX blocks_page ON blocks(page_id);
    """)
    con.execute("INSERT INTO meta VALUES('started_at', '2026-11-17T00:00:00+00:00')")
    if finished:
        con.execute("INSERT INTO meta VALUES('finished_at', '2026-11-17T00:10:00+00:00')")
    n = 0
    for pid, (parent, title) in pages.items():
        con.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
                    (pid, "page", parent, None, 0, json.dumps(page_obj(pid, title, parent)), TS))
        n += 1
        con.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
                    (uid(1000 + n), pid, pid, 0, "paragraph", 0,
                     json.dumps({"id": uid(1000 + n), "type": "paragraph", "paragraph": {"rich_text": []}})))
        if parent is not None:
            con.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
                        (pid, parent, parent, 1, "child_page", 0, json.dumps({"id": pid, "type": "child_page",
                                                                              "child_page": {"title": title}})))
    for src, dst in (link_from or []):
        n += 1
        con.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
                    (uid(1000 + n), src, src, 5, "link_to_page", 0,
                     json.dumps({"type": "link_to_page", "link_to_page": {"type": "page_id", "page_id": dst}})))
    con.commit()
    con.close()
    roots = [{"id": pid, "title": t, "teamspace": "General" if pid != X else "x_to_notion",
              "teamspace_order": 1 if pid != X else 3, "root_order": 1, "kind": "page", "in_trash": False}
             for pid, (parent, t) in pages.items() if parent is None]
    jdump(roots, path + ".roots.json")
    return path


def parse_args(args):
    out, i = {}, 0
    while i < len(args):
        k = args[i]
        if i + 1 < len(args) and not str(args[i + 1]).startswith("--"):
            out[k] = args[i + 1]
            i += 2
        else:
            out[k] = True
            i += 1
    return out


def plan_sha256(plan):
    return sha(wb_common.canon_json({k: v for k, v in plan.items() if k != "plan_sha256"}))


class FakeTools:
    """Stands in for the tool scripts; behaviour is set per test through the harness attributes."""

    def __init__(self, h):
        self.h = h
        self.calls = []

    def __call__(self, script, args, timeout=None):
        a = parse_args([str(x) for x in args])
        self.calls.append((script, a))
        return getattr(self, script.replace(".py", ""))(a)

    def names(self):
        return [c[0] for c in self.calls]

    def args_of(self, script, n=0):
        return [c[1] for c in self.calls if c[0] == script][n]

    # -- tools
    def capture_sidebar(self, a):
        h = self.h
        jdump({"order_match": True, "roots": h.sidebar_roots, "teamspace_order_required": refresh.wb_scan.TEAMSPACES,
               "teamspace_order_observed": refresh.wb_scan.TEAMSPACES},
              os.path.join(h.P.evidence, "sidebar_20991231T2359.json"))
        return 0

    def snapshot(self, a):
        h = self.h
        shutil.copyfile(h.snap, a["--db"])
        roots = jload(a["--roots"])
        h.snapshot_roots = roots
        jdump([dict(r, kind="page", in_trash=False) for r in roots], a["--db"] + ".roots.json")
        return 0

    def assets_fetch(self, a):
        return 0

    def export_obsidian(self, a):
        h = self.h
        stg = a.get("--out") or os.path.join(h.P.staging, a["--run-id"])
        os.makedirs(os.path.join(stg, "vault"), exist_ok=True)
        for nid in jload(a["--link-targets"]) if "--link-targets" in a else ():
            rel = h.page_json()[nid]["rel_path"]
            path = os.path.join(stg, "vault", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(h.files[rel])
        h.seen_overrides = jload(a["--overrides"])
        jdump({}, os.path.join(stg, "manifest.json"))
        jdump({}, os.path.join(stg, "attachments.json"))
        with open(os.path.join(stg, "block_lines.jsonl"), "w"):
            pass
        rep = {"renderer_version": "1.2.1", "block_lines": dict(h.block_lines)}
        if h.export_overrides is not None:
            rep["overrides"] = h.export_overrides
        jdump(rep, os.path.join(stg, "export_report.json"))
        with open(os.path.join(stg, "page_json.jsonl"), "w", encoding="utf-8") as fh:
            for nid, row in h.page_json().items():
                fh.write(json.dumps(row) + "\n")
        return h.export_rc

    def verify_vault(self, a):
        h = self.h
        kind = "post" if os.path.abspath(a["--vault"]) == h.P.vault else "staging"
        h.verify_args[kind] = a
        sbc, rejected, details = h.verify_result(kind)
        checks = {c: {"status": s, "summary": "", "details": details.get(c, {})} for c, s in sbc.items()}
        out = {"status_by_check": sbc, "checks": checks,
               "registered_deviations": {"rejected_entries": rejected}}
        jdump(out, os.path.join(a["--out"], "verify.json"))
        return 0 if all(s != "FAIL" for s in sbc.values()) else 1

    def publish_vault(self, a):
        h = self.h
        rep = a["--report-dir"]
        holds = jload(a["--holds"]) if "--holds" in a else {}
        if "--apply" not in a:
            h.plan_holds.append(holds)
            actions, tomb = h.plan_fn(holds)
            plan = {"run_id": a["--run-id"], "vault": a["--vault"], "counts": {}, "tombstones": tomb,
                    "actions": actions}
            plan["plan_sha256"] = plan_sha256(plan)
            jdump(plan, os.path.join(rep, "publish_plan.json"))
            return 0
        plan = jload(a["--plan-file"])
        assert a["--plan-sha"] == plan["plan_sha256"], "plan sha must be the embedded plan_sha256"
        assert os.path.exists(a["--approval"]), a["--approval"]
        h.applied.append(a)
        if h.race_rels:
            with open(os.path.join(rep, "publish_journal.jsonl"), "w", encoding="utf-8") as fh:
                for rel in h.race_rels:
                    fh.write(json.dumps({"op": "SKIP_REPLACE_RACE", "rel": rel}) + "\n")
                    fh.write(json.dumps({"event": "stopped", "rel": rel, "op": "REPLACE",
                                         "race": "SKIP_REPLACE_RACE"}) + "\n")
        return h.apply_rc


class Harness(unittest.TestCase):
    """Temp WORK layout + vault + export.db + snapshot; run rounds with the fake tools."""
    cfg = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refresh_test_")
        work, vault = os.path.join(self.tmp, "work"), os.path.join(self.tmp, "vault")
        self.P = refresh.Paths(work, vault)
        for d in (self.P.state, self.P.reports, self.P.staging, self.P.snapshots, self.P.logs, self.P.evidence,
                  self.P.approvals, os.path.dirname(self.P.config)):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.P.approvals, refresh.STANDING_APPROVAL), "w", encoding="utf-8") as fh:
            fh.write("# standing approval (test)\n")
        self.write_cfg(self.cfg)
        self.files = {REL[n]: note(n) for n in REL}
        self.files["Notion_Obsidian/README.md"] = "# README\n"
        for rel, text in self.files.items():
            self.put(rel, text)
        con = sqlite3.connect(self.P.export_db)
        con.executescript("""
        CREATE TABLE objects_map(notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, name TEXT, parent_id TEXT,
          teamspace TEXT, ord INT, title TEXT, rendered_sha256 TEXT, published_sha256 TEXT, published_path TEXT,
          published_run TEXT, status TEXT, first_run TEXT, last_run TEXT);
        CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT, run_id TEXT,
          status TEXT);""")
        for nid, rel in REL.items():
            con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)", (rel, sha(self.files[rel]), nid, "page", "R0",
                                                                             "active"))
            con.execute("INSERT INTO objects_map(notion_id, kind, rel_path, parent_id, title, status) VALUES(?,?,?,?,?,?)",
                        (nid, "page", rel, PARENT[nid], TITLE[nid], "active"))
        con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                    ("Notion_Obsidian/README.md", sha("# README\n"), None, "generated", "R0", "active"))
        con.commit()
        con.close()
        self.pages = {n: (PARENT[n], TITLE[n]) for n in REL}
        self.snap = make_snapshot(os.path.join(self.P.snapshots, "snap_20991231T0000.db"), self.pages)
        # behaviour knobs
        self.sidebar_roots = [{"id": H, "title": "Home", "teamspace": "General", "teamspace_order": 1, "root_order": 1},
                              {"id": X, "title": "X", "teamspace": "x_to_notion", "teamspace_order": 3, "root_order": 1}]
        self.block_lines = {"pages": 6, "rows": 10}
        self.export_overrides = None
        self.export_rc = 0
        self.pj_override = {}
        self.verify_override = {}
        self.plan_extra = []
        self.plan_replace = None
        self.tomb = []
        self.apply_rc = 0
        self.race_rels = []
        self.applied, self.plan_holds, self.verify_args = [], [], {}
        self.tools = FakeTools(self)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture helpers
    def write_cfg(self, cfg):
        jdump(cfg, self.P.config)

    def put(self, rel, text):
        p = os.path.join(self.P.vault, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def rm(self, rel):
        os.remove(os.path.join(self.P.vault, *rel.split("/")))

    def page_json(self):
        rows = {}
        for nid, rel in REL.items():
            rows[nid] = {"notion_id": nid, "kind": "page", "rel_path": rel, "sha256": sha(self.files[rel]),
                         "page_json_sha": sha("pj-" + nid), "renderer_version": "1.2.1"}
        for nid, upd in self.pj_override.items():
            rows.setdefault(nid, {"notion_id": nid, "kind": "page"}).update(upd)
        return rows

    def verify_result(self, kind):
        sbc = {c: "PASS" for c in refresh.BLOCKING}
        sbc["C11"] = "WARN"
        o = self.verify_override.get(kind) or {}
        sbc.update(o.get("status", {}))
        return sbc, o.get("rejected", []), o.get("details", {})

    def plan_fn(self, holds):
        if self.plan_replace is not None:
            return self.plan_replace(holds), list(self.tomb)
        acts = []
        for nid, rel in REL.items():
            op = "SKIP_HELD" if nid in holds else "UNCHANGED"
            acts.append({"op": op, "rel": rel, "nid": nid, "kind": "page", "sha": "x"})
        acts += [a for a in self.plan_extra if a.get("nid") not in holds]
        return acts, list(self.tomb)

    def seed_base(self, rows):
        db = wb_state.connect(self.P.wb_db)
        for nid, pjs, rv in rows:
            db.execute("INSERT INTO base_history(notion_id, seq, rel_path, sha256, page_json_sha, renderer_version, "
                       "run_id, origin) VALUES(?,?,?,?,?,?,?,?)", (nid, 1, REL.get(nid), "0" * 64, pjs, rv, "R0", "REMOTE"))
        db.close()

    def seed_all_base(self, rv="1.2.1"):
        self.seed_base([(n, sha("pj-" + n), rv) for n in REL])

    def opts(self, run_id="R1", **kw):
        d = dict(mode="incremental", dry_run=False, use_snapshot=self.snap, sidebar=None, run_id=run_id)
        d.update(kw)
        return argparse.Namespace(**d)

    def run_round(self, run_id="R1", client_factory=None, **kw):
        self.tools.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc, st = refresh.run_round(self.P, self.opts(run_id, **kw), runner_factory=lambda P, log: self.tools,
                                       client_factory=client_factory or (lambda P: None))
        self.stdout = out.getvalue()
        return rc, st

    def rep(self, run_id, *parts):
        return self.P.report(run_id, *parts)

    def holds_file(self):
        return jload(self.P.holds) if os.path.exists(self.P.holds) else {}

    def vault_note(self, name):
        p = os.path.join(self.P.vault, "Notion_Obsidian", "运行记录", name)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as fh:
            return fh.read().decode("utf-8")

    def assert_generated_note_rules(self, text):
        """D report / WB 5.1a: notion_kind generated + generated_by, no notion_id, LF, one trailing newline, no wikilink."""
        self.assertTrue(text.startswith("---\nnotion_kind: generated\ngenerated_by: refresh.py\n"), text[:80])
        self.assertNotIn("notion_id:", text.split("---")[1])
        self.assertNotIn("\r", text)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        self.assertNotIn("[[", text)

    def assert_paused(self, rc, st, reason_prefix):
        self.assertEqual(st["result"], "paused_for_review", st.get("error"))
        self.assertEqual(rc, refresh.EXIT_PAUSED)
        self.assertTrue(any(r.startswith(reason_prefix) for r in st["pause_reasons"]), st["pause_reasons"])
        self.assertEqual(self.applied, [])                              # nothing applied
        self.assertTrue(os.path.exists(self.rep(st["run_id"], "publish_plan.paused.json")))
        pr = jload(self.P.paused_runs)
        self.assertEqual(pr["latest"], st["run_id"])
        self.assertEqual(pr["runs"][st["run_id"]]["status"], "paused")
        txt = self.vault_note(f"{st['run_id']}-已暂停.md")
        self.assertIsNotNone(txt)
        self.assert_generated_note_rules(txt)
        self.assertIn(f"--approve-run {st['run_id']}", txt)
        lp = jload(self.P.last_published_json) if os.path.exists(self.P.last_published_json) else {}
        self.assertNotEqual(lp.get("run_id"), st["run_id"])           # a paused round is not "last published"
        return txt


# ====================================================================== round basics
class TestRoundBasics(Harness):
    def test_use_snapshot_published_and_wiring(self):
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published"), st.get("error"))
        names = self.tools.names()
        self.assertEqual(names, ["export_obsidian.py", "verify_vault.py", "publish_vault.py", "publish_vault.py",
                                 "verify_vault.py"])                   # no sidebar capture, no crawl, no Notion
        ex = self.tools.args_of("export_obsidian.py")
        self.assertEqual(ex["--overrides"], self.rep("R1", "overrides.json"))
        self.assertTrue(ex["--immutable"])                               # hard rule 4: finished snapshot, immutable
        for kind in ("staging", "post"):
            va = self.verify_args[kind]
            self.assertTrue(va["--snapshot-immutable"])                 # L-05
            self.assertEqual(va["--expected-deviations"], self.rep("R1", "expected_deviations.json"))  # L-06
        plan_a, apply_a = self.tools.args_of("publish_vault.py", 0), self.tools.args_of("publish_vault.py", 1)
        for x in (plan_a, apply_a):
            self.assertEqual(x["--holds"], self.rep("R1", "holds.json"))
            self.assertEqual(x["--overrides"], self.rep("R1", "overrides.json"))
            self.assertEqual(x["--wb-db"], self.P.wb_db)
            self.assertTrue(x["--allow-other-vault"])
        self.assertNotIn("--apply", plan_a)
        self.assertEqual(apply_a["--plan-file"], self.rep("R1", "publish_plan.json"))
        self.assertTrue(apply_a["--approval"].endswith(refresh.STANDING_APPROVAL))
        for f in ("wb_scan.json", "overrides.json", "holds.json", "expected_deviations.json", "publish_plan.json",
                  "refresh_status.json", "run_record.md", "sidebar_from_snapshot.json"):
            self.assertTrue(os.path.exists(self.rep("R1", f)), f)
        with open(self.P.last_published_txt, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), self.snap)                # D-17
        self.assertEqual(jload(self.P.last_published_json)["run_id"], "R1")
        self.assertFalse(os.path.exists(self.P.latest_snapshot))       # --use-snapshot never moves the crawl base
        self.assertIsNone(self.vault_note("R1-未完成.md"))
        with open(os.path.join(self.P.logs, "refresh_history.jsonl"), encoding="utf-8") as fh:
            hist = [json.loads(x) for x in fh]
        self.assertEqual(hist[-1]["result"], "published")

    def test_missing_standing_approval_stops_before_any_remote_read_export_or_publish(self):
        os.unlink(os.path.join(self.P.approvals, refresh.STANDING_APPROVAL))
        for rid, snapshot in (("offline", self.snap), ("scheduled", None)):
            reads = []
            rc, status = self.run_round(rid, use_snapshot=snapshot, client_factory=lambda _: reads.append("read"))
            with self.subTest(rid=rid):
                self.assertEqual(rc, refresh.EXIT_FAIL)
                self.assertEqual(reads, [])
                self.assertEqual(self.tools.calls, [])
                self.assertFalse(os.path.exists(self.rep(rid, "publish_journal.jsonl")))
                note = self.vault_note(rid + "-未完成.md")
                self.assertIn(refresh.STANDING_APPROVAL, note)
                self.assertIn("从备份恢复", note)
                self.assertNotIn("部分笔记可能已写入", note)

    def test_plan_only_refresh_can_run_without_standing_approval(self):
        os.unlink(os.path.join(self.P.approvals, refresh.STANDING_APPROVAL))
        rc, status = self.run_round(dry_run=True)
        self.assertEqual(rc, refresh.EXIT_OK, status)
        self.assertEqual(status["result"], "planned")

    def test_use_snapshot_refuses_unfinished_snapshot(self):
        make_snapshot(self.snap, self.pages, finished=False)
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (1, "failed"))
        self.assertIn("finished_at", st["error"])
        self.assertEqual(self.tools.calls, [])                         # nothing exported, nothing published
        txt = self.vault_note("R1-未完成.md")
        self.assert_generated_note_rules(txt)

    def test_dry_run_writes_no_state_and_no_vault_note(self):
        self.write_cfg({"conflict_pause_threshold": 1, "mode": "plan"})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A, "kind": "page"}]
        shutil.copyfile(os.path.join(self.P.vault, *REL[B].split("/")), os.path.join(self.P.vault, "General", "Home", "B copy.md"))
        self.rm(REL[X])
        rc, st = self.run_round(dry_run=True)
        self.assertEqual((rc, st["result"]), (0, "planned"))
        self.assertTrue(st["would_pause"])
        self.assertEqual(self.applied, [])
        for p in (self.P.holds, self.P.wb_db, self.P.paused_runs, self.P.last_published_txt):
            self.assertFalse(os.path.exists(p), p)
        self.assertFalse(os.path.exists(os.path.join(self.P.vault, "Notion_Obsidian", "运行记录")))

    def test_lock_busy_is_skipped(self):
        with wb_locks.lock("refresh", state_dir=self.P.state):
            rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "skipped_lock_busy"))
        self.assertEqual(self.tools.calls, [])

    def test_failed_staging_verify_fails_the_round(self):
        self.verify_override["staging"] = {"status": {"C02": "FAIL"}}
        rc, st = self.run_round()
        self.assertEqual(st["result"], "failed")
        self.assertIn("blocking checks failed", st["error"])
        self.assertEqual(self.applied, [])
        self.assert_generated_note_rules(self.vault_note("R1-未完成.md"))

    def test_export_report_block_lines_selfcheck_pauses(self):
        self.block_lines = {"pages": 6, "untagged_lines": 3, "own_line_lost": 7}
        rc, st = self.run_round()
        self.assert_paused(rc, st, "export_block_lines_selfcheck")

    def test_own_line_lost_alone_is_informational(self):
        self.block_lines = {"pages": 6, "own_line_lost": 20}
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published")


# ====================================================================== §14.3 conditions
class TestPauseConditions(Harness):
    def test_conflicts_at_threshold_pause_the_round(self):
        self.write_cfg({"conflict_pause_threshold": 2})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}, {"op": "CONFLICT_BOTH", "rel": REL[B], "nid": B}]
        rc, st = self.run_round()
        txt = self.assert_paused(rc, st, "conflicts:")
        self.assertIn(f"`{REL[A]}`", txt)                              # objects as inline-code paths
        # threshold - 1 -> no pause, object-level only
        self.plan_extra = self.plan_extra[:1]
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published_with_holds", st.get("pause_reasons"))
        dev = jload(self.rep("R2", "expected_deviations.json"))
        self.assertIn({"notion_id": A, "subtree": False, "paths": [REL[A]]}, dev["paused"])

    def test_conflict_on_page_with_children_pauses(self):
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[C], "nid": C}]
        rc, st = self.run_round()
        self.assert_paused(rc, st, "conflict_on_page_with_children")

    def test_leaf_conflict_is_object_level_and_registered_as_paused(self):
        with open(os.path.join(self.P.vault, *REL[A].split("/")), "a", encoding="utf-8") as fh:
            fh.write("local edit\n")
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}]
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published_with_holds"))
        self.assertEqual(len(self.applied), 1)
        dev = jload(self.rep("R1", "expected_deviations.json"))
        # the kept local file is stale vs Notion exactly like a held note (fix round, finding 1): paused, no subtree
        self.assertIn({"notion_id": A, "subtree": False, "paths": [REL[A]]}, dev["paused"])
        self.assertNotIn(A, {m["notion_id"] for m in dev["local_modified"]})
        self.assertNotIn(A, self.holds_file())                          # F's conflict op: no auto_sync_hold entry

    def test_conflict_moved_edited_registers_the_kept_old_file(self):
        """F keeps the edited file at `frm` and does not write `rel`: the deviation names the kept file (finding 1)."""
        old = "General/Home/A old.md"
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, *old.split("/")))
        con = sqlite3.connect(self.P.export_db)
        con.execute("UPDATE published_files SET rel_path=? WHERE notion_id=?", (old, A))
        con.commit()
        con.close()
        with open(os.path.join(self.P.vault, *old.split("/")), "a", encoding="utf-8") as fh:
            fh.write("local edit\n")
        self.plan_replace = lambda holds: [{"op": "CONFLICT_MOVED_EDITED", "rel": REL[A], "frm": old, "from": old,
                                            "nid": A, "kind": "page"}]
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published_with_holds"), st.get("error"))
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertIn({"notion_id": A, "subtree": False, "paths": [old, REL[A]]}, dev["paused"])
        self.assertEqual(dev["local_modified"], [])
        self.assertEqual(st["conflicts"], [{"rel": REL[A], "notion_id": A, "op": "CONFLICT_MOVED_EDITED",
                                            "dedup": False, "kept": old}])

    def test_mapping_anomaly_duplicate_is_object_level_hold(self):
        shutil.copyfile(os.path.join(self.P.vault, *REL[C].split("/")),
                        os.path.join(self.P.vault, "General", "Home", "C", "C copy.md"))
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published_with_holds"), st.get("error"))
        hs = self.holds_file()
        self.assertEqual(hs[C]["source"], "scan")
        self.assertTrue(hs[C]["auto_release"])
        sent = self.plan_holds[-1]
        self.assertIn(C, sent)
        self.assertIn(D, sent)                                         # subtree expanded for the publisher
        dev = jload(self.rep("R1", "expected_deviations.json"))
        ent = [p for p in dev["paused"] if p["notion_id"] == C][0]
        self.assertEqual(ent["paths"], ["General/Home/C/C copy.md", REL[C]])
        self.assertIn(C, st["holds"]["this_round"])
        # fixed by the user -> released automatically next round
        self.rm("General/Home/C/C copy.md")
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published")
        self.assertNotIn(C, self.holds_file())

    def test_sync_conflict_copy_holds_the_original_this_round(self):
        shutil.copyfile(os.path.join(self.P.vault, *REL[A].split("/")),
                        os.path.join(self.P.vault, "General", "Home", "A (Conflicted copy 2026-11-17 1010).md"))
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds")
        self.assertIn(A, self.plan_holds[-1])
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertEqual(dev["sync_conflict_copies"], [{"path": "General/Home/A (Conflicted copy 2026-11-17 1010).md",
                                                        "notion_id": A}])

    def test_source_mass_trash_pauses_and_archives(self):
        many = {uid(200 + i): (H, f"Gone {i}") for i in range(10)}
        prev = make_snapshot(os.path.join(self.P.snapshots, "snap_20991230T0000.db"), dict(self.pages, **many))
        with open(self.P.last_published_txt, "w", encoding="utf-8") as fh:
            fh.write(prev + "\n")
        rc, st = self.run_round()
        self.assert_paused(rc, st, "source_mass_trash")
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT count(*) FROM trash_archive").fetchone()[0], 10)
        db.close()
        self.assertEqual(jload(self.rep("R1", "trash_archive.json"))["count_for_pause"], 10)
        # threshold - 1 -> no pause
        prev = make_snapshot(prev, dict(self.pages, **dict(list(many.items())[:9])))
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))

    def test_source_change_ratio_pauses(self):
        self.seed_all_base()
        self.pj_override = {A: {"page_json_sha": sha("changed")}}       # 1 of 6 pages = 16.7% >= 10%
        rc, st = self.run_round()
        self.assert_paused(rc, st, "source_mass_change")
        self.assertEqual(st["source_change"]["pages_changed"], 1)

    def test_source_change_below_ratio_publishes(self):
        self.write_cfg({"source_change_pause_ratio": 0.5})
        self.seed_all_base()
        self.pj_override = {A: {"page_json_sha": sha("changed")}}
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published")

    def test_renderer_version_change_pauses_d15(self):
        self.seed_all_base(rv="1.2.0")
        rc, st = self.run_round()
        self.assert_paused(rc, st, "renderer_version_changed")

    def test_local_mass_delete_mode_plan_pauses_without_hold(self):
        self.write_cfg({"mode": "plan", "max_local_deletes": 1})
        self.seed_all_base()
        self.rm(REL[A])
        self.rm(REL[B])
        rc, st = self.run_round()
        self.assert_paused(rc, st, "local_mass_delete")
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT count(*) FROM pending_deletes").fetchone()[0], 0)  # neither restore nor hold
        db.close()

    def test_local_delete_mode_off_restores(self):
        self.write_cfg({"mode": "off", "max_local_deletes": 0})
        self.seed_all_base()
        self.rm(REL[A])
        self.rm(REL[B])
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published")
        self.assertEqual(sorted(r["notion_id"] for r in st["deletions"]["restore"]), sorted([A, B]))
        self.assertNotIn(A, self.plan_holds[-1])
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT count(*) FROM pending_deletes").fetchone()[0], 0)
        db.close()

    def test_local_delete_mode_plan_within_threshold_is_held(self):
        self.write_cfg({"mode": "plan"})
        self.seed_all_base()
        self.rm(REL[A])
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds")
        db = sqlite3.connect(self.P.wb_db)
        rows = db.execute("SELECT notion_id, rel_path, status, ttl_until FROM pending_deletes").fetchall()
        db.close()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [(A, REL[A], "hold")])
        self.assertIn(A, self.plan_holds[-1])
        self.assertEqual(self.plan_holds[-1][A]["source"], "pending_delete")
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertIn({"notion_id": A, "subtree": True, "paths": [REL[A]]}, dev["hold_ids"])
        # next round: still held (already pending, not counted as new)
        rc, st = self.run_round("R2")
        self.assertIn(A, self.plan_holds[-1])
        self.assertEqual(st["deletions"]["new_missing"], 0)
        # the note reappears -> restored, no longer held
        self.put(REL[A], self.files[REL[A]])
        rc, st = self.run_round("R3")
        self.assertNotIn(A, self.plan_holds[-1])
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT status FROM pending_deletes").fetchone()[0], "restored")
        db.close()

    def test_local_delete_mode_plan_notion_changed_or_partial_restores(self):
        self.write_cfg({"mode": "plan"})
        self.seed_all_base()
        self.pj_override = {A: {"page_json_sha": sha("edited in notion")}}
        self.write_cfg({"mode": "plan", "source_change_pause_ratio": 1.0})
        self.rm(REL[A])          # Notion changed since B -> CONFLICT_DEL_EDIT -> restore
        self.rm(REL[C])          # folder note gone but its child D stays -> PARTIAL_DELETE -> restore
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))
        rules = {r["notion_id"]: r["rule"] for r in st["deletions"]["restore"]}
        self.assertIn("CONFLICT_DEL_EDIT", rules[A])
        self.assertIn("PARTIAL_DELETE", rules[C])
        self.assertNotIn(A, self.plan_holds[-1])

    def test_missing_note_of_an_object_gone_from_notion_is_not_a_local_delete(self):
        self.write_cfg({"mode": "plan", "max_local_deletes": 0})
        self.seed_all_base()
        self.pj_override = {}
        rows = self.page_json()
        rows.pop(B)
        self.page_json = lambda: rows                                    # B no longer exported (deleted in Notion)
        self.rm(REL[B])
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))
        self.assertEqual(st["deletions"]["missing_not_exported"], [REL[B]])
        self.assertEqual(st["deletions"]["new_missing"], 0)

    def test_rejected_override_is_a_hold(self):
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "A2.md"))
        self.export_overrides = {"given": 1, "applied": [], "rejected": [{"notion_id": A, "local_rel": "General/Home/A2.md",
                                                                          "reason": "name_clash"}]}
        rc, st = self.run_round()
        self.assertEqual(self.seen_overrides, {A: "General/Home/A2.md"})   # the exporter got the override
        self.assertEqual(self.holds_file()[A]["reason"], "override_rejected:name_clash")
        self.assertIn(A, self.plan_holds[-1])
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertEqual(dev["name_overrides"], [])                     # a rejected override is not a name difference

    def test_same_folder_rename_becomes_persistent_override(self):
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "A2.md"))
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published")
        self.assertEqual(self.seen_overrides, {A: "General/Home/A2.md"})
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertEqual(dev["name_overrides"], [{"notion_id": A, "old_rel": REL[A], "local_rel": "General/Home/A2.md"}])
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT notion_id, local_rel, status FROM name_overrides").fetchall(),
                         [(A, "General/Home/A2.md", "open")])
        db.close()
        # after the publisher adopted it (published path = local_rel) the override keeps being passed on
        con = sqlite3.connect(self.P.export_db)
        con.execute("UPDATE published_files SET rel_path='General/Home/A2.md' WHERE notion_id=?", (A,))
        con.commit()
        con.close()
        rc, st = self.run_round("R2")
        self.assertEqual(self.seen_overrides, {A: "General/Home/A2.md"})

    def test_plan_writing_a_held_object_pauses(self):
        shutil.copyfile(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "Ax.md"))
        self.plan_replace = lambda holds: [{"op": "REPLACE", "rel": REL[A], "nid": A, "kind": "page"}]
        rc, st = self.run_round()
        self.assert_paused(rc, st, "plan_writes_held_object")

    def test_plan_anomaly_ops_become_object_holds(self):
        self.plan_extra = [{"op": "CONFLICT_UNTRACKED", "rel": REL[B], "nid": B}]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds")
        self.assertEqual(self.holds_file()[B]["source"], "plan")
        self.assertEqual(len(self.plan_holds), 2)                      # re-planned with the new hold
        self.assertIn(B, self.plan_holds[1])

    def test_publisher_own_skip_held_becomes_object_hold(self):
        self.plan_replace = lambda holds: [{"op": "SKIP_HELD", "rel": REL[B], "nid": B, "reason": "duplicate_id_elsewhere"}]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds")
        self.assertEqual(self.holds_file()[B]["reason"], "plan_skip_held:duplicate_id_elsewhere")
        self.assertEqual(len(self.plan_holds), 2)                      # re-planned once, then stable
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertIn(B, {p["notion_id"] for p in dev["paused"]})

    def test_base_history_import_failure_is_a_warning(self):
        self.apply_rc = 5
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published"))
        self.assertIn("base_history_import_failed", st["warnings"])
        self.assertTrue(os.path.exists(self.P.last_published_txt))

    def test_staging_rejected_entries_pause(self):
        self.verify_override["staging"] = {"rejected": [{"category": "local_new", "path": "x.md", "reason": "has_notion_id"}]}
        rc, st = self.run_round()
        self.assert_paused(rc, st, "expected_deviations_rejected_entries")

    def test_post_verify_failure_locatable_holds_object(self):
        self.verify_override["post"] = {"status": {"C02": "FAIL", "C06": "FAIL"},
                                        "details": {"C02": {"duplicates": [{"id": B, "paths": [REL[B]]}]},
                                                    "C06": {"unresolved": [{"source": REL[A], "target": "nope"}]}}}
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (1, "published_post_verify_failed"))
        hs = self.holds_file()
        self.assertEqual(set(hs), {A, B})
        self.assertEqual(hs[B]["source"], "post_verify")
        self.assertFalse(hs[B]["auto_release"])
        self.assertFalse(os.path.exists(self.P.pause_next))
        self.assert_generated_note_rules(self.vault_note("R1-未完成.md"))
        self.assertTrue(os.path.exists(self.P.last_published_txt))     # it was applied
        # next round: the held objects are skipped (not written) and registered as paused deviations
        self.verify_override = {}
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published_with_holds")
        self.assertTrue({A, B} <= set(self.plan_holds[-1]))
        dev = jload(self.rep("R2", "expected_deviations.json"))
        self.assertEqual({p["notion_id"] for p in dev["paused"]}, {A, B})

    def test_post_verify_failure_unlocatable_pauses_next_round(self):
        self.verify_override["post"] = {"status": {"C05": "FAIL"},
                                        "details": {"C05": {"problems": [{"section": "General", "issue": "order"}]}}}
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_post_verify_failed")
        self.assertTrue(os.path.exists(self.P.pause_next))
        self.verify_override = {}
        self.applied.clear()
        rc, st = self.run_round("R2")
        self.assert_paused(rc, st, "previous_post_verify_unlocatable")

    def test_partial_publish_note_directs_user_to_journal_and_fresh_round(self):
        self.apply_rc = 4
        self.race_rels = [REL[B]]
        _, status = self.run_round("RaceNote")
        self.assertTrue(status["partially_applied"])
        note = self.vault_note("RaceNote-已暂停.md")
        self.assertNotIn("--approve-run RaceNote", note)
        self.assertIn("reports/RaceNote/publish_journal.jsonl", note)
        self.assertIn("下一轮", note)
        self.assertIn(REL[B], note)

    def test_apply_race_pauses_and_holds_raced_object(self):
        self.apply_rc = 4
        self.race_rels = [REL[B]]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "paused_for_review")
        self.assertTrue(st["partially_applied"])
        self.assertEqual(self.holds_file()[B]["source"], "race")
        self.assertEqual(st["raced_ids"], [B])
        note = self.vault_note("R1-已暂停.md")
        self.assert_generated_note_rules(note)
        self.assertIn("本轮已部分写入，剩余写入已停止", note)
        self.assertIn("执行期间检测到库内文件竞态", note)
        self.assertIn(REL[B], note)
        self.assertIn("中止时的发布计划", note)


# ====================================================================== approve / discard / gate
class TestHumanDecisions(Harness):
    def pause_one(self):
        self.write_cfg({"conflict_pause_threshold": 1})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "paused_for_review")
        self.applied.clear()

    def gate_ok(self):
        return contextlib.ExitStack()

    def patched(self, yes=True):
        st = contextlib.ExitStack()
        import wb_gate
        st.enter_context(mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"))
        st.enter_context(mock.patch.object(wb_gate, "confirm_yes_no", return_value=yes))
        st.enter_context(contextlib.redirect_stdout(io.StringIO()))
        st.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return st

    def test_cli_gate_refuses_without_tty(self):
        """The CLI decision commands refuse in this agent session (env markers) and, with a clean env, on pipes."""
        before = sorted(os.listdir(os.path.join(SANDBOX, "state")))
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS") and not k.startswith("CLAUDE_CODE_")}
        for env in (dict(os.environ, CLAUDECODE="1"), clean):
            for args in (["--approve-run", "20991231T0000-T1"], ["--discard-run", "20991231T0000-T1"],
                         ["--release-hold", uid(5)]):
                r = subprocess.run([sys.executable, "-I", "-B", os.path.join(TOOLS, "refresh.py")] + args,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                   timeout=60)
                self.assertEqual(r.returncode, refresh.EXIT_GATE, (args, r.stderr.decode()[-500:]))
                self.assertIn("拒绝", r.stderr.decode())
        self.assertEqual(sorted(os.listdir(os.path.join(SANDBOX, "state"))), before)   # no lock/state file created

    def test_approve_replans_and_publishes_same_plan(self):
        self.pause_one()
        with self.patched():
            rc = refresh.approve_run(self.P, "R1", runner_factory=lambda P, log: self.tools)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.applied), 1)
        self.assertTrue(self.applied[0]["--approval"].endswith("approve-run-R1.approval.md"))
        self.assertEqual(self.applied[0]["--report-dir"], self.rep("R1", "approve"))
        self.assertEqual(jload(self.P.paused_runs)["runs"]["R1"]["status"], "approved")
        self.assertEqual(jload(self.P.last_published_json)["run_id"], "R1")

    def test_approve_refuses_a_different_plan_and_non_latest(self):
        self.pause_one()
        self.plan_extra.append({"op": "REPLACE", "rel": REL[B], "nid": B})        # the world changed
        with self.patched():
            rc = refresh.approve_run(self.P, "R1", runner_factory=lambda P, log: self.tools)
        self.assertEqual(rc, refresh.EXIT_FAIL)
        self.assertEqual(self.applied, [])
        self.assertEqual(jload(self.P.paused_runs)["runs"]["R1"]["status"], "paused")
        with self.patched():
            self.assertEqual(refresh.approve_run(self.P, "R0", runner_factory=lambda P, log: self.tools), 1)

    def test_approve_refused_after_a_later_publish(self):
        self.pause_one()
        jdump({"run_id": "R9", "snapshot": self.snap}, self.P.last_published_json)
        with self.patched():
            rc = refresh.approve_run(self.P, "R1", runner_factory=lambda P, log: self.tools)
        self.assertEqual(rc, 1)
        self.assertEqual(jload(self.P.paused_runs)["runs"]["R1"]["status"], "superseded")

    def test_approve_answer_no_changes_nothing(self):
        self.pause_one()
        with self.patched(yes=False):
            rc = refresh.approve_run(self.P, "R1", runner_factory=lambda P, log: self.tools)
        self.assertEqual(rc, 1)
        self.assertEqual(self.applied, [])
        self.assertEqual(jload(self.P.paused_runs)["runs"]["R1"]["status"], "paused")

    def test_discard_and_release_hold(self):
        self.pause_one()
        with self.patched():
            self.assertEqual(refresh.discard_run(self.P, "R1"), 0)
        pr = jload(self.P.paused_runs)
        self.assertEqual(pr["runs"]["R1"]["status"], "discarded")
        self.assertIsNone(pr["latest"])
        refresh.save_holds(self.P, {B: {"reason": "post_verify_C02", "source": "post_verify", "auto_release": False}})
        with self.patched():
            self.assertEqual(refresh.release_hold(self.P, B), 0)
        self.assertEqual(self.holds_file(), {})


# ====================================================================== prune
class TestPrune(Harness):
    def test_prune_keeps_paused_evidence_and_drops_old_side_outputs(self):
        runs = [f"2099010{i}T0000-T1" for i in range(1, 7)]
        for r in runs:
            d = os.path.join(self.P.staging, r)
            os.makedirs(os.path.join(d, "vault"))
            for f in ("block_lines.jsonl", "page_json.jsonl", "attachments.json", "manifest.json"):
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("{}\n")
        snaps = []
        for i in range(20):
            s = os.path.join(self.P.snapshots, f"snap_2099{i + 10:02d}01T0000.db")
            with open(s, "w") as fh:
                fh.write("x")
            snaps.append(s)
        paused_run, paused_snap = runs[0], snaps[0]
        jdump({"latest": paused_run, "runs": {paused_run: {"status": "paused", "snapshot": paused_snap},
                                              runs[1]: {"status": "discarded", "snapshot": snaps[1]}}},
              self.P.paused_runs)
        jdump({"run_id": runs[2], "snapshot": snaps[2]}, self.P.last_published_json)
        with open(self.P.last_published_txt, "w") as fh:
            fh.write(snaps[2] + "\n")
        with open(self.P.latest_snapshot, "w") as fh:
            fh.write(snaps[3])
        log = io.StringIO()
        refresh.prune(log, self.P)
        st = lambda r, f: os.path.exists(os.path.join(self.P.staging, r, f))  # noqa: E731
        self.assertTrue(st(paused_run, "vault") and st(paused_run, "block_lines.jsonl") and st(paused_run, "page_json.jsonl"))
        self.assertFalse(st(runs[1], "vault") or st(runs[1], "block_lines.jsonl") or st(runs[1], "page_json.jsonl"))
        self.assertFalse(st(runs[2], "vault") or st(runs[2], "block_lines.jsonl"))
        self.assertTrue(st(runs[2], "page_json.jsonl"))                  # last published run: D-15 fallback baseline
        for r in runs:
            self.assertTrue(st(r, "attachments.json") and st(r, "manifest.json"))   # planner inputs kept
        for r in runs[-3:]:
            self.assertTrue(st(r, "vault") and st(r, "block_lines.jsonl"))
        left = set(os.listdir(self.P.snapshots))
        for s in snaps[:4]:                                              # paused, (discarded->pruned), last published, latest
            self.assertEqual(os.path.basename(s) in left, s != snaps[1], s)
        self.assertEqual({s for s in snaps if os.path.basename(s) in left},
                         set(snaps[-4:]) | {paused_snap, snaps[2], snaps[3]})

    def test_incremental_and_trash_archive_keep_their_distinct_previous_snapshots(self):
        # The fake capture tool copies its input into snapshots/. Keep that fixture outside the retention set.
        new_content = os.path.join(self.tmp, "fixture-next.db")
        os.rename(self.snap, new_content)
        self.snap = new_content
        paths = [os.path.join(self.P.snapshots, f"snap_200001{day:02d}T0000.db") for day in range(1, 11)]
        paused, incremental, published = paths[:3]
        vanished = uid(501)
        for path in paths:
            pages = dict(self.pages)
            if path == published:
                pages[vanished] = (H, "archived from last publication")
            make_snapshot(path, pages)
        jdump({"latest": "held", "runs": {"held": {"status": "paused", "snapshot": paused}}}, self.P.paused_runs)
        refresh.write_text(self.P.latest_snapshot, incremental)
        refresh.write_text(self.P.last_published_txt, published)
        refresh.prune(io.StringIO(), self.P)
        with self.subTest(stage="four ordinary snapshots plus three old exemptions"):
            self.assertEqual({path for path in paths if os.path.isfile(path)}, set(paths[-4:] + paths[:3]))

        rc, status = self.run_round("next-incremental", use_snapshot=None)
        self.assertEqual((rc, status["result"]), (refresh.EXIT_OK, "published"))
        self.assertEqual(self.tools.args_of("snapshot.py")["--incremental-from"], incremental)
        self.assertEqual(status["source_change"]["compared_with"], published)
        with contextlib.closing(sqlite3.connect(self.P.wb_db)) as db:
            self.assertEqual(db.execute("SELECT notion_id,last_snapshot FROM trash_archive").fetchall(),
                             [(vanished, published)])
        self.assertEqual(refresh.latest_complete_snapshot(self.P), status["snapshot"])
        self.assertEqual(refresh.last_published_snapshot(self.P), status["snapshot"])
        # run_round itself prunes after advancing the two pointers. The paused run still pins its snapshot.
        newest = sorted(paths + [status["snapshot"]])[-4:]
        self.assertEqual({path for path in paths + [status["snapshot"]] if os.path.isfile(path)},
                         set(newest) | {paused, status["snapshot"]})

    def test_superseded_pauses_are_bounded(self):
        runs = {f"2099010{i}T0000-T1": {"status": "superseded", "snapshot": None} for i in range(1, 7)}
        runs["2099010{}T0000-T1".format(7)] = {"status": "paused", "snapshot": None}
        jdump({"latest": "20990107T0000-T1", "runs": runs}, self.P.paused_runs)
        self.assertEqual(refresh.paused_exempt_runs(self.P),
                         {"20990107T0000-T1", "20990106T0000-T1", "20990105T0000-T1", "20990104T0000-T1"})

    def test_newer_pause_supersedes_older_pause(self):
        self.write_cfg({"conflict_pause_threshold": 1})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}]
        self.run_round("20990101T0000-T1")
        self.run_round("20990102T0000-T1")
        pr = jload(self.P.paused_runs)
        self.assertEqual(pr["latest"], "20990102T0000-T1")
        self.assertEqual(pr["runs"]["20990101T0000-T1"]["status"], "superseded")
        self.assertEqual(pr["runs"]["20990102T0000-T1"]["status"], "paused")

    def test_prune_calls_wb_refetch_retention_and_keeps_referenced(self):
        wb = os.path.join(self.P.snapshots, "wb")
        os.makedirs(wb)
        old_ref, old_free = os.path.join(wb, "snap_A.db"), os.path.join(wb, "snap_B.db")
        for p in (old_ref, old_free):
            with open(p, "w") as fh:
                fh.write("x")
            os.utime(p, (1, 1))                                          # ancient
        with open(self.P.last_published_txt, "w") as fh:
            fh.write(old_ref + "\n")
        refresh.prune(io.StringIO(), self.P)
        self.assertTrue(os.path.exists(old_ref))
        self.assertFalse(os.path.exists(old_free))

    def test_paused_round_evidence_survives_later_rounds(self):
        self.write_cfg({"conflict_pause_threshold": 1})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}]
        self.run_round("2099010100-T1")
        self.write_cfg({})
        self.plan_extra = []
        for i in range(2, 7):
            rid = f"20990101{i:02d}-T1"
            rc, st = self.run_round(rid)
            os.makedirs(os.path.join(self.P.staging, rid, "vault"), exist_ok=True)
        self.assertTrue(os.path.isdir(os.path.join(self.P.staging, "2099010100-T1", "vault")))
        self.assertEqual(jload(self.P.paused_runs)["runs"]["2099010100-T1"]["status"], "superseded")
        self.assertFalse(os.path.isdir(os.path.join(self.P.staging, "2099010102-T1", "vault")))
        # discarding releases the evidence
        import wb_gate
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/t"), \
                mock.patch.object(wb_gate, "confirm_yes_no", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh.discard_run(self.P, "2099010100-T1"), 0)
        refresh.prune(io.StringIO(), self.P)
        self.assertFalse(os.path.isdir(os.path.join(self.P.staging, "2099010100-T1", "vault")))


# ====================================================================== Notion reads (fake server)
class TestNotionReads(Harness):
    def setUp(self):
        super().setUp()
        import logging
        lg = logging.getLogger("nmig.client")
        old = lg.level
        lg.setLevel(logging.ERROR)                                       # expected 403/404 warnings: keep output clean
        self.addCleanup(lg.setLevel, old)

    def fake(self, caps=("read", "user_info"), persons=2):
        from fake_notion import FakeNotion
        f = FakeNotion()
        f.add_token("ro", caps=caps, roots=(H, X))
        for i in range(persons):
            f.add_user(f"person{i}", type="person")
        f.add_user("bot", type="bot")
        f.add_page("Home", id=H)
        f.add_page("X", id=X)
        f.add_page("Brand new root", id=uid(900))                       # not shared with the read token
        base = f.start()
        self.addCleanup(f.stop)
        from readonly_client import make_read_client
        return f, (lambda P: make_read_client(base_url=base, token=f.secret("ro"), rate=None, sleep=lambda s: None,
                                              max_retries=0))

    def test_member_count_persons_only(self):
        f, cf = self.fake(persons=2)
        res = refresh.count_members(cf(self.P))
        self.assertEqual(res, {"count": 2, "status": "ok", "error": None})
        st = refresh.member_status({"workspace_members": 1, "notion_plan": "free"}, res)
        self.assertTrue(st["warn_block_limit"])
        self.assertEqual(st["creation_state"], "blocked_assumed")
        self.assertFalse(refresh.member_status({"workspace_members": 1, "notion_plan": "free"},
                                               {"count": 1, "status": "ok"})["warn_block_limit"])
        self.assertNotIn("person0", json.dumps(st))                      # never names

    def test_member_count_403_is_unknown_and_warns(self):
        f, cf = self.fake(caps=("read",))
        res = refresh.count_members(cf(self.P))
        self.assertEqual((res["count"], res["status"]), (None, "unknown"))
        self.assertIn("403", res["error"])
        self.assertTrue(refresh.member_status({"workspace_members": 1, "notion_plan": "free"}, res)["warn_block_limit"])

    def test_full_round_new_root_unshared_and_member_warning(self):
        f, cf = self.fake(persons=2)
        self.sidebar_roots = self.sidebar_roots + [{"id": uid(900), "title": "Brand new root", "teamspace": "General",
                                                    "teamspace_order": 1, "root_order": 2}]
        rc, st = self.run_round(use_snapshot=None, client_factory=cf)
        self.assertEqual(st["result"], "published", st.get("error"))
        self.assertEqual(self.tools.names()[:3], ["capture_sidebar.py", "snapshot.py", "assets_fetch.py"])
        self.assertEqual([r["id"] for r in st["new_roots_unshared"]], [uid(900)])
        self.assertNotIn(uid(900), [r["id"] for r in self.snapshot_roots])     # crawl does not die on the 404
        self.assertIn("warn_block_limit", st["warnings"])
        self.assertIn("new_roots_unshared", st["warnings"])
        with open(self.rep(st["run_id"], "run_record.md"), encoding="utf-8") as fh:
            rec = fh.read()
        self.assertIn("新顶层页面未共享给读连接", rec)
        self.assertIn("连接", rec)
        self.assertIn("Godcreate", rec)
        self.assertIn("工作区成员数告警", rec)
        self.assertIn("警告", self.stdout)
        self.assertTrue(os.path.exists(self.P.latest_snapshot))
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT members, state, source FROM creation_state").fetchall(),
                         [(2, "blocked_assumed", "refresh")])
        db.close()
        # writes went nowhere: the fake saw GETs only
        self.assertTrue(all(r["method"] == "GET" for r in f.requests))


# ====================================================================== expected deviations vs the real verifier
def _load_tvv():
    spec = importlib.util.spec_from_file_location("test_verify_vault_fixture", os.path.join(HERE, "test_verify_vault.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDeviationsBindRealVerifier(unittest.TestCase):
    def test_expected_deviations_pass_verifier_validation(self):
        tvv = _load_tvv()
        vv = tvv.vv
        import wb_scan
        tmp = tempfile.mkdtemp(prefix="refresh_dev_bind_")
        self.addCleanup(shutil.rmtree, tmp, True)
        snap, sidebar, roots, vault = (os.path.join(tmp, "snap", "snap.db"), os.path.join(tmp, "sidebar.json"),
                                       os.path.join(tmp, "snap.db.roots.json"), os.path.join(tmp, "vault"))
        os.makedirs(os.path.dirname(snap))
        tvv.build_snapshot(snap)
        tvv.build_sidebar(sidebar, roots)
        tvv.write_vault(vault)
        export_db = os.path.join(tmp, "export.db")
        con = sqlite3.connect(export_db)
        con.execute("CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT, "
                    "run_id TEXT, status TEXT)")
        for dp, _d, fns in os.walk(vault):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, vault).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    data = fh.read()
                nid, kind, _e = wb_scan.read_note_identity(data) if rel.endswith(".md") else (None, None, None)
                con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                            (rel, sha(data), nid, kind or ("attachment" if "_attachments" in rel else "generated"),
                             "R0", "active"))
        con.commit()
        con.close()
        ok0, rep0, _ = vv.verify(snap, sidebar, vault, os.path.join(tmp, "out0"), roots)
        self.assertTrue(ok0)

        def w(rel, data):
            p = os.path.join(vault, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

        # local changes: user note + user image + user folder at the root, a Sync conflict copy, a local edit
        w("Inbox/idea.md", "# my idea\n")
        w("Inbox/sketch.png", b"\x89PNGuser")
        w("scratch.md", "loose note\n")
        child = "General/Home/Child One.md"
        with open(os.path.join(vault, *child.split("/")), "rb") as fh:
            child_bytes = fh.read()
        w("General/Home/Child One (Conflicted copy 2026-11-17 1010).md", child_bytes)
        sub = "General/Tasks/Row B/Sub Page.md"
        with open(os.path.join(vault, *sub.split("/")), "a", encoding="utf-8") as fh:
            fh.write("\nlocal addition\n")
        scan = wb_scan.scan(vault, export_db, None, {})
        held = {nid: dict(e, source="scan") for nid, e in wb_scan.anomaly_ids(scan).items()}
        dev = refresh.build_expected_deviations("RX", scan, held, {}, {})
        devp = os.path.join(tmp, "expected_deviations.json")
        jdump(dev, devp)
        self.assertEqual(sorted(dev["local_new"]), ["Inbox", "Inbox/idea.md", "Inbox/sketch.png", "scratch.md"])
        ok, rep, _ = vv.verify(snap, sidebar, vault, os.path.join(tmp, "out1"), roots, expected_deviations=devp)
        rd = rep["registered_deviations"]
        self.assertEqual(rd["rejected_entries"], [], rd["rejected_entries"])
        st = {k: r["status"] for k, r in rep["checks"].items()}
        for c in ("C01", "C02", "C08", "C09"):
            self.assertEqual(st[c], "PASS", (c, rep["checks"][c]["summary"]))
        self.assertGreater(rd["total"], 0)
        # without the file the same vault fails (the entries really excuse something)
        ok2, rep2, _ = vv.verify(snap, sidebar, vault, os.path.join(tmp, "out2"), roots)
        self.assertFalse(ok2)


# ====================================================================== trash_archive
class TestTrashArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trash_archive_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_archive_rows_refs_dedupe_and_never_deletes(self):
        pages = {H: (None, "Home"), A: (H, "A"), B: (A, "B"), C: (H, "C")}
        prev = make_snapshot(os.path.join(self.tmp, "prev.db"), pages, link_from=[(C, B)])
        new = make_snapshot(os.path.join(self.tmp, "new.db"), {H: (None, "Home"), C: (H, "C")})
        shas = {p: wb_common.sha256_file(p) for p in (prev, new)}
        db = wb_state.connect(os.path.join(self.tmp, "wb.db"))
        res = trash_archive.archive(prev, new, db, run_id="R1", exclude_ids=[A])
        self.assertEqual(sorted(g["notion_id"] for g in res["disappeared"]), sorted([A, B]))
        self.assertEqual((res["archived"], res["count_for_pause"]), (2, 1))   # A excluded (trashed via write-back)
        row = db.execute("SELECT object_json, blocks_json, inbound_refs_json, reason, last_snapshot FROM trash_archive "
                         "WHERE notion_id=?", (B,)).fetchone()
        self.assertEqual(json.loads(row[0])["id"], B)
        self.assertEqual([b["type"] for b in json.loads(row[1])], ["paragraph"])
        refs = json.loads(row[2])["refs"]
        self.assertEqual(sorted((r["page_id"], r["type"]) for r in refs), sorted([(A, "child_page"), (C, "link_to_page")]))
        self.assertIn("disappeared_from_snapshot", row[3])
        res2 = trash_archive.archive(prev, new, db, run_id="R2")
        self.assertEqual((res2["archived"], res2["already_archived"]), (0, 2))
        db.close()
        for p, h in shas.items():
            self.assertEqual(wb_common.sha256_file(p), h)                 # snapshots untouched
        dry = trash_archive.archive(prev, new, None, dry_run=True)
        self.assertEqual(dry["archived"], 2)

    def test_refuses_snapshot_with_wal_data(self):
        prev = make_snapshot(os.path.join(self.tmp, "prev.db"), {H: (None, "Home")})
        new = make_snapshot(os.path.join(self.tmp, "new.db"), {H: (None, "Home")})
        with open(new + "-wal", "wb") as fh:
            fh.write(b"x" * 10)
        with self.assertRaises(trash_archive.SnapshotRefused):
            trash_archive.archive(prev, new, None, dry_run=True)


# ====================================================================== capture_sidebar
# Shape recorded by the live read-only capture of 2026-11-17 23:06 (session wb_dev_sidebar), sanitised: titles/ids
# replaced. All five teamspaces collapsed; the first aria-expanded control near each "添加到 <name>" was the settings
# menu button "团队协作区设置与成员..." (see the module report), the only rendered treeitems were 最近 / 私人 items.
LIVE_COLLAPSED = {
    "url": "https://app.notion.com/p/Team-Home-04c0728e322e411f8fb0cf4ee5f9f5df",
    "captured": "2026-11-17T16:06:42.295Z",
    "teamspaces": [{"name": n, "label": "团队协作区设置与成员...", "expanded": "false", "items": []}
                   for n in capture_sidebar.ORDER],
    "others": [{"depth": 0, "text": "Recent page", "href": "/p/Recent-" + "1" * 32 + "?pvs=28", "section": "最近"},
               {"depth": 0, "text": "Private import", "href": "/p/Priv-" + "2" * 32 + "?pvs=12", "section": "私人"}],
}


def _items(prefix, n, nested=False):
    out = []
    for i in range(n):
        out.append({"depth": 0, "text": f"{prefix} {i}", "href": f"/p/{prefix}-{(prefix[0].lower() * 2 + f'{i:x}') * 1}"})
    for i, it in enumerate(out):
        h = hashlib.md5(it["text"].encode()).hexdigest()
        it["href"] = f"/p/x-{h}?pvs=28" if i else f"/p/{h}?v={'f' * 32}&pvs=28"
    if nested:
        out.insert(1, {"depth": 1, "text": "child", "href": "/p/c-" + "c" * 32})
    return out


class TestCaptureSidebarParse(unittest.TestCase):
    def test_live_collapsed_shape_is_a_mismatch_not_empty_success(self):
        ev, problems = capture_sidebar.parse_capture(LIVE_COLLAPSED)
        self.assertEqual(ev["teamspace_order_observed"], capture_sidebar.ORDER)
        self.assertFalse(ev["order_match"])                               # caller keeps the previous evidence
        self.assertEqual(ev["roots"], [])
        self.assertEqual(len(problems), 5)
        self.assertEqual(ev["out_of_scope_top_level"], [{"id": capture_sidebar.dashed("2" * 32), "title": "Private import",
                                                         "section": "私人"}])

    def test_expanded_items_give_ordered_roots(self):
        extra = {n: _items(n.split()[0], k, nested=(k > 1)) for n, k in zip(capture_sidebar.ORDER, (5, 2, 3, 1, 4))}
        ev, problems = capture_sidebar.parse_capture(LIVE_COLLAPSED, extra)
        self.assertEqual(problems, [])
        self.assertTrue(ev["order_match"])
        self.assertEqual(len(ev["roots"]), 15)
        self.assertEqual(ev["roots_per_teamspace"], dict(zip(capture_sidebar.ORDER, (5, 2, 3, 1, 4))))
        r0 = ev["roots"][0]
        self.assertEqual((r0["teamspace"], r0["teamspace_order"], r0["root_order"]), ("General", 1, 1))
        self.assertEqual(r0["id"], capture_sidebar.dashed(hashlib.md5(b"General 0").hexdigest()))  # not the ?v= view id
        self.assertEqual([r["root_order"] for r in ev["roots"] if r["teamspace"] == "General"], [1, 2, 3, 4, 5])
        self.assertNotIn("child", [r["title"] for r in ev["roots"]])

    def test_wrong_order_or_missing_teamspace(self):
        s = json.loads(json.dumps(LIVE_COLLAPSED))
        s["teamspaces"][0], s["teamspaces"][1] = s["teamspaces"][1], s["teamspaces"][0]
        extra = {n: _items(n.split()[0], 1) for n in capture_sidebar.ORDER}
        ev, _ = capture_sidebar.parse_capture(s, extra)
        self.assertFalse(ev["order_match"])
        s["teamspaces"] = s["teamspaces"][1:]
        ev, problems = capture_sidebar.parse_capture(s, extra)
        self.assertFalse(ev["order_match"])

    def test_capture_flow_expands_one_at_a_time_and_restores(self):
        state = {n: "false" for n in capture_sidebar.ORDER}
        log = []

        class FakeBridge:
            def __call__(self, *a, timeout=None):
                log.append(a[0])
                return ""

            def eval_json(self, js):
                if "const rows = [];" in js:     # SCAN
                    ts = [{"name": n, "label": n[:1], "toggle_found": True, "expanded": state[n],
                           "items": _items(n.split()[0], 2) if state[n] == "true" else []} for n in capture_sidebar.ORDER]
                    log.append(("scan", tuple(k for k, v in state.items() if v == "true")))
                    return {"teamspaces": ts, "others": []}, "{}"
                name = json.loads(js.split("const NAME = ")[1].split(", WANT")[0])
                want = json.loads(js.split("WANT = ")[1].split(";")[0])
                before = state[name]
                if before != want:
                    state[name] = want
                log.append(("toggle", name, want))
                return {"found": True, "clicked": before != want, "before": before}, "{}"

        scan, extra, raws, actions = capture_sidebar.capture(FakeBridge(), settle=0, expand_wait=0)
        self.assertTrue(all(v == "false" for v in state.values()))        # user's collapsed state restored
        scans = [x for x in log if isinstance(x, tuple) and x[0] == "scan"]
        self.assertTrue(all(len(x[1]) <= 1 for x in scans))               # never more than one expanded
        ev, problems = capture_sidebar.parse_capture(scan, extra)
        self.assertEqual(problems, [])
        self.assertEqual(len(ev["roots"]), 10)
        self.assertEqual([a["action"] for a in actions], ["expand", "collapse"] * 5)

    def test_capture_restores_on_error(self):
        state = {n: "false" for n in capture_sidebar.ORDER}

        class Boom(Exception):
            pass

        class FakeBridge:
            n = 0

            def __call__(self, *a, timeout=None):
                return ""

            def eval_json(self, js):
                if "const rows = [];" in js:
                    FakeBridge.n += 1
                    if FakeBridge.n == 2:
                        raise Boom()
                    return {"teamspaces": [{"name": n, "toggle_found": True, "expanded": state[n], "items": []}
                                           for n in capture_sidebar.ORDER], "others": []}, "{}"
                name = json.loads(js.split("const NAME = ")[1].split(", WANT")[0])
                want = json.loads(js.split("WANT = ")[1].split(";")[0])
                before, state[name] = state[name], want
                return {"found": True, "clicked": before != want, "before": before}, "{}"

        with self.assertRaises(Boom):
            capture_sidebar.capture(FakeBridge(), settle=0, expand_wait=0)
        self.assertTrue(all(v == "false" for v in state.values()))


NODE_DOM = r"""
const readline = require('readline');
const CLICKS = [];
class El {
  constructor(tag, attrs, text) { this.tag = tag; this.attrs = Object.assign({}, attrs || {}); this._text = text || '';
    this.children = []; this.parentElement = null; this.onclick = null; }
  append(...cs) { for (const c of cs) { c.parentElement = this; this.children.push(c); } return this; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get innerText() { return [this._text, ...this.children.map(c => c.innerText)].filter(Boolean).join('\n'); }
  get tagName() { return this.tag.toUpperCase(); }
  get previousElementSibling() { const p = this.parentElement; if (!p) return null; const i = p.children.indexOf(this); return i > 0 ? p.children[i - 1] : null; }
  contains(x) { while (x) { if (x === this) return true; x = x.parentElement; } return false; }
  closest(sel) { let x = this; while (x) { if (matches(x, sel)) return x; x = x.parentElement; } return null; }
  all() { const out = []; const w = e => { for (const c of e.children) { out.push(c); w(c); } }; w(this); return out; }
  querySelectorAll(sel) { return this.all().filter(e => matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  click() { CLICKS.push(this.getAttribute('aria-label') || this._text); if (this.onclick) this.onclick(); }
  dispatchEvent(ev) { if (ev.type === 'click') this.click(); return true; }
}
function matches(e, sel) {
  const m = sel.match(/^([a-z]*)((?:\[[^\]]+\])*)$/); if (!m) throw new Error('selector ' + sel);
  if (m[1] && e.tag !== m[1]) return false;
  for (const a of m[2].matchAll(/\[([^=\]]+)(?:=([^\]]+))?\]/g)) {
    if (!e.hasAttribute(a[1])) return false;
    if (a[2] !== undefined && e.getAttribute(a[1]) !== a[2].replace(/^["']|["']$/g, '')) return false;
  }
  return true;
}
class MouseEvent { constructor(type) { this.type = type; } }
const body = new El('body');
const document = { body, querySelectorAll: s => body.querySelectorAll(s), querySelector: s => body.querySelector(s) };
const location = { href: 'https://app.notion.com/p/test' }; const window = {};
const nav = new El('nav'); body.append(nav);
function treeitem(text, id, kids) { const t = new El('div', {role: 'treeitem'}); t.append(new El('a', {href: '/p/' + text.replace(/ /g, '-') + '-' + id}, text)); for (const k of (kids || [])) t.append(k); return t; }
const hex = n => n.toString(16).padStart(32, '0');
nav.append(new El('div', {}, '最近'), treeitem('Recent one', hex(1)));
const tsRoot = new El('div'); nav.append(new El('div', {}, '团队协作区'), tsRoot);
const TS = %TS%; let k = 100;
// sibling: toggle next to 打开/添加到/settings (first probe); nested: the LIVE shape probed 2026-11-18 -- the header
// row itself is the toggle (no aria-label, text = icon letter / name) and contains 打开/添加到/settings, children in
// the row's wrapper; flat: nested toggle, children rendered loose in the 团队协作区 section after the row.
const LAYOUT = "%LAYOUT%".startsWith("%") ? "sibling" : "%LAYOUT%";
for (const [name, letter, n, mode] of TS) {
  const wrap = new El('div'), row = new El('div'), kids = new El('div');
  const settings = new El('div', {role: 'button', 'aria-expanded': 'false', 'aria-haspopup': 'dialog', 'aria-label': '团队协作区设置与成员...'});
  const open_ = new El('div', {role: 'button', 'aria-expanded': 'false', 'aria-haspopup': 'dialog', 'aria-label': '打开'});
  const add = new El('div', {role: 'button', 'aria-expanded': 'false', 'aria-haspopup': 'dialog', 'aria-label': '添加到 ' + name});
  let toggle;
  if (LAYOUT === 'sibling') {
    toggle = new El('div', {role: 'button', 'aria-expanded': 'false', 'aria-label': letter});
    row.append(toggle, open_, add, settings);
  } else {
    toggle = new El('div', {role: 'button', 'aria-expanded': 'false'}, letter);
    const inner = new El('div'); inner.append(new El('div', {}, letter === name ? '' : name), open_, add, settings);
    toggle.append(inner); row.append(toggle);
  }
  wrap.append(row);
  if (LAYOUT === 'flat') tsRoot.append(wrap, kids); else { wrap.append(kids); tsRoot.append(wrap); }
  const pages = []; for (let i = 0; i < n; i++) pages.push([name + ' root ' + i, hex(k++)]);
  toggle.onclick = () => { if (mode === 'noflip') return;
    const open = toggle.getAttribute('aria-expanded') !== 'true'; toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    kids.children = []; if (open && mode !== 'empty') for (const [t, id] of pages) kids.append(treeitem(t, id, [treeitem(t + ' child', hex(k++))])); };
  settings.onclick = () => settings.setAttribute('aria-expanded', settings.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
}
nav.append(new El('div', {}, '私人'), treeitem('Private one', hex(2)));
const rl = readline.createInterface({input: process.stdin});
rl.on('line', line => { const js = JSON.parse(line); let out;
  try { out = {ok: true, r: js === '__CLICKS__' ? JSON.stringify(CLICKS) : eval(js)}; } catch (e) { out = {ok: false, e: String(e)}; }
  process.stdout.write(JSON.stringify(out) + '\n'); });
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestCaptureSidebarJsOnMockDom(unittest.TestCase):
    """Runs the real SCAN_JS / TOGGLE_JS in node against a mock DOM shaped like the probed sidebar (toggle with
    an icon-letter label, 打开, 添加到 <name>, and the settings menu button that also carries aria-expanded)."""

    def node(self, ts, layout="sibling"):
        proc = subprocess.Popen(["node", "-e", NODE_DOM.replace("%TS%", json.dumps(ts, ensure_ascii=False))
                                 .replace("%LAYOUT%", layout)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def stop():
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.addCleanup(stop)

        def ev(js):
            proc.stdin.write(json.dumps(js) + "\n")
            proc.stdin.flush()
            out = json.loads(proc.stdout.readline())
            self.assertTrue(out["ok"], out.get("e"))
            return out["r"]

        class NodeBridge:
            def __call__(self, *a, timeout=None):
                return ""

            def eval_json(self, js):
                raw = ev(js)
                return json.loads(raw), raw
        return ev, NodeBridge()

    def test_live_layouts_nested_and_flat(self):
        """Lead fix 2026-11-18 (live probe): the header row itself is the toggle and wraps 打开/添加到/settings;
        children render in the row's wrapper (nested) or loose in the section (flat, document-order read)."""
        ts = [["General", "General", 3, "ok"], ["Quantitative Investing", "Q", 2, "ok"], ["x_to_notion", "X", 4, "ok"],
              ["Godcreate Business", "G", 1, "ok"], ["Godcreate AI工作空间", "Godcreate AI工作空间", 2, "ok"]]
        for layout, read in (("nested", "container"), ("flat", "document_order")):
            with self.subTest(layout=layout):
                ev, bridge = self.node(ts, layout)
                state0 = json.loads(ev(capture_sidebar._js(capture_sidebar.SCAN_JS)))
                self.assertTrue(all(t["toggle_found"] for t in state0["teamspaces"]), state0["teamspaces"])
                self.assertEqual([t["label"] for t in state0["teamspaces"]], [t[1] for t in ts])
                scan, extra, raws, actions = capture_sidebar.capture(bridge, settle=0, expand_wait=0)
                self.assertEqual(json.loads(ev("__CLICKS__")), [x for t in ts for x in (t[1], t[1])])
                ev_, problems = capture_sidebar.parse_capture(scan, extra)
                self.assertEqual(problems, [])
                self.assertTrue(ev_["order_match"])
                self.assertEqual(ev_["roots_per_teamspace"], {t[0]: t[2] for t in ts})
                self.assertEqual([r["title"] for r in ev_["roots"] if r["teamspace"] == "Godcreate AI工作空间"],
                                 ["Godcreate AI工作空间 root 0", "Godcreate AI工作空间 root 1"])   # 私人 never absorbed
                self.assertEqual([o["title"] for o in ev_["out_of_scope_top_level"]], ["Private one"])
                state = json.loads(ev(capture_sidebar._js(capture_sidebar.SCAN_JS)))
                self.assertEqual({t["expanded"] for t in state["teamspaces"]}, {"false"})   # restored
                ev(capture_sidebar._js(capture_sidebar.TOGGLE_JS, NAME="x_to_notion", WANT="true"))
                st = json.loads(ev(capture_sidebar._js(capture_sidebar.SCAN_JS)))
                xt = next(t for t in st["teamspaces"] if t["name"] == "x_to_notion")
                self.assertEqual((xt["read"], [i["depth"] for i in xt["items"]]), (read, [0, 1] * 4))

    def test_click_that_does_not_expand_aborts_without_more_clicks(self):
        """Fix round, finding 8: no synthetic-event fallback; a toggle that does not flip stops the capture."""
        ts = [["General", "General", 3, "ok"], ["Quantitative Investing", "Q", 2, "noflip"], ["x_to_notion", "X", 4, "ok"],
              ["Godcreate Business", "G", 1, "ok"], ["Godcreate AI工作空间", "Godcreate AI工作空间", 2, "ok"]]
        ev, bridge = self.node(ts)
        scan, extra, raws, actions = capture_sidebar.capture(bridge, settle=0, expand_wait=0)
        self.assertEqual(json.loads(ev("__CLICKS__")), ["General", "General", "Q"])   # Q clicked once, then nothing
        self.assertEqual([a["action"] for a in actions], ["expand", "collapse", "expand", "abort"])
        self.assertIn("aria-expanded", actions[-1]["reason"])
        _ev, problems = capture_sidebar.parse_capture(scan, extra)
        self.assertFalse(_ev["order_match"])

    def test_expand_without_roots_aborts_and_restores_only_that_toggle(self):
        ts = [["General", "General", 3, "empty"], ["Quantitative Investing", "Q", 2, "ok"], ["x_to_notion", "X", 4, "ok"],
              ["Godcreate Business", "G", 1, "ok"], ["Godcreate AI工作空间", "Godcreate AI工作空间", 2, "ok"]]
        ev, bridge = self.node(ts)
        scan, extra, raws, actions = capture_sidebar.capture(bridge, settle=0, expand_wait=0)
        self.assertEqual(json.loads(ev("__CLICKS__")), ["General", "General"])        # expand + restore, nothing else
        self.assertEqual([a["action"] for a in actions], ["expand", "abort", "collapse(cleanup)"])
        state = json.loads(ev(capture_sidebar._js(capture_sidebar.SCAN_JS)))
        self.assertEqual({t["expanded"] for t in state["teamspaces"]}, {"false"})

    def test_js_clicks_only_teamspace_toggles_and_reads_roots(self):
        ts = [["General", "General", 3], ["Quantitative Investing", "Q", 2], ["x_to_notion", "X", 4],
              ["Godcreate Business", "G", 1], ["Godcreate AI工作空间", "Godcreate AI工作空间", 2]]
        proc = subprocess.Popen(["node", "-e", NODE_DOM.replace("%TS%", json.dumps(ts, ensure_ascii=False))],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def stop():
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.addCleanup(stop)

        def ev(js):
            proc.stdin.write(json.dumps(js) + "\n")
            proc.stdin.flush()
            out = json.loads(proc.stdout.readline())
            self.assertTrue(out["ok"], out.get("e"))
            return out["r"]

        class NodeBridge:
            def __call__(self, *a, timeout=None):
                return ""

            def eval_json(self, js):
                raw = ev(js)
                return json.loads(raw), raw

        scan, extra, raws, actions = capture_sidebar.capture(NodeBridge(), settle=0, expand_wait=0)
        clicks = json.loads(ev("__CLICKS__"))
        self.assertEqual(clicks, [x for t in ts for x in (t[1], t[1])])   # each toggle: expand + collapse, nothing else
        ev_, problems = capture_sidebar.parse_capture(scan, extra)
        self.assertEqual(problems, [])
        self.assertTrue(ev_["order_match"])
        self.assertEqual(ev_["roots_per_teamspace"], {t[0]: t[2] for t in ts})
        self.assertEqual([r["title"] for r in ev_["roots"] if r["teamspace"] == "x_to_notion"],
                         [f"x_to_notion root {i}" for i in range(4)])
        self.assertEqual([o["title"] for o in ev_["out_of_scope_top_level"]], ["Private one"])
        state = json.loads(ev(capture_sidebar._js(capture_sidebar.SCAN_JS)))
        self.assertEqual({t["expanded"] for t in state["teamspaces"]}, {"false"})   # restored
        self.assertTrue(all(t["toggle_found"] for t in state["teamspaces"]))
        for js in (capture_sidebar._js(capture_sidebar.SCAN_JS),
                   capture_sidebar._js(capture_sidebar.TOGGLE_JS, NAME="General", WANT="true")):
            self.assertNotRegex(js, r"%[A-Z]+%")


class TestNoWriteClients(unittest.TestCase):
    def test_refresh_never_imports_write_paths(self):
        import ast
        for name in ("refresh.py", "trash_archive.py", "capture_sidebar.py", "wb_scan.py"):
            with open(os.path.join(TOOLS, name), encoding="utf-8") as fh:
                src = fh.read()
            mods = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    mods |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    mods.add(node.module or "")
            self.assertNotIn("writeback_client", mods, name)
            self.assertNotIn("notion_tokens", mods, name)
            for bad in ("writeback.env", "load_writeback_token", "WritebackClient"):
                self.assertNotIn(bad, src, name)



# ====================================================================== fix round (adversarial review findings)
class TestFixRoundConflicts(Harness):
    def test_standing_deduped_conflict_never_pauses_again(self):
        """Finding 2: a CONFLICT_BOTH on a folder note pauses once; after approval its copy exists (F: dedup=True)
        and later rounds publish with the conflict listed, never paused again."""
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[C], "nid": C, "kind": "page"}]
        rc, st = self.run_round("R1")
        self.assert_paused(rc, st, "conflict_on_page_with_children")
        import wb_gate
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/t"), \
                mock.patch.object(wb_gate, "confirm_yes_no", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(refresh.approve_run(self.P, "R1", runner_factory=lambda P, log: self.tools), 0)
        self.plan_extra = [dict(self.plan_extra[0], dedup=True, conflict_rel="Notion_Obsidian/conflicts/R1/x.md")]
        for rid in ("R2", "R3"):
            rc, st = self.run_round(rid)
            self.assertEqual((rc, st["result"]), (0, "published_with_holds"), st.get("pause_reasons"))
            self.assertEqual(st["conflicts"][0]["dedup"], True)
            with open(self.rep(rid, "run_record.md"), encoding="utf-8") as fh:
                self.assertIn("此前已写过冲突副本", fh.read())

    def test_threshold_counts_new_conflicts_only(self):
        self.write_cfg({"conflict_pause_threshold": 2})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A, "dedup": True},
                           {"op": "CONFLICT_BOTH", "rel": REL[B], "nid": B}]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds", st.get("pause_reasons"))
        self.plan_extra[0].pop("dedup")
        self.applied.clear()
        rc, st = self.run_round("R2")
        self.assert_paused(rc, st, "conflicts:")

    def test_conflict_on_page_whose_children_left_notion_is_leaf(self):
        """Finding 11: objects_map rows with status source_missing are not children."""
        con = sqlite3.connect(self.P.export_db)
        con.execute("UPDATE objects_map SET status='source_missing' WHERE notion_id=?", (D,))
        con.commit()
        con.close()
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[C], "nid": C}]
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds", st.get("pause_reasons"))

    def test_delete_of_page_whose_children_left_notion_is_not_partial(self):
        self.write_cfg({"mode": "plan"})
        self.seed_all_base()
        con = sqlite3.connect(self.P.export_db)
        con.execute("UPDATE objects_map SET status='source_missing' WHERE notion_id=?", (D,))
        con.commit()
        con.close()
        self.rm(REL[C])                                                  # D's tombstoned note stays in the vault
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds", st.get("pause_reasons"))
        self.assertEqual(st["deletions"]["restore"], [])
        self.assertEqual(st["deletions"]["pending"], [REL[C]])


class TestFixRoundHolds(Harness):
    def test_sync_copy_of_root_note_holds_only_that_note(self):
        """Finding 6: WB 4.0 holds only the corresponding note (no subtree) for a Sync conflict copy."""
        shutil.copyfile(os.path.join(self.P.vault, *REL[H].split("/")),
                        os.path.join(self.P.vault, "General", "Home", "Home (Conflicted copy 2026-11-17 1010).md"))
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds")
        self.assertEqual(set(self.plan_holds[-1]), {H})                  # not A, B, C, D
        self.assertFalse(self.plan_holds[-1][H]["subtree"])
        self.assertFalse(self.holds_file()[H]["subtree"])
        dev = jload(self.rep("R1", "expected_deviations.json"))
        self.assertIn({"notion_id": H, "subtree": False, "paths": [REL[H]]}, dev["paused"])
        # a duplicate id still holds the subtree
        shutil.copyfile(os.path.join(self.P.vault, *REL[C].split("/")), os.path.join(self.P.vault, "General", "Home", "C", "C copy.md"))
        rc, st = self.run_round("R2")
        self.assertTrue(self.plan_holds[-1][C]["subtree"])
        self.assertIn(D, self.plan_holds[-1])

    def test_nfd_named_note_is_held_with_a_clear_reason(self):
        """Finding 7: a published name present only in another Unicode normal form is held, not silently equal."""
        import unicodedata
        nid = uid(7)
        rel = "General/Home/Café.md"
        text = note(nid)
        con = sqlite3.connect(self.P.export_db)
        con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)", (rel, sha(text), nid, "page", "R0", "active"))
        con.execute("INSERT INTO objects_map(notion_id, kind, rel_path, parent_id, title, status) VALUES(?,?,?,?,?,?)",
                    (nid, "page", rel, H, "Café", "active"))
        con.commit()
        con.close()
        self.put(unicodedata.normalize("NFD", rel), text)
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published_with_holds", st.get("error"))
        hs = self.holds_file()
        self.assertEqual(hs[nid]["reason"], "path_normalization_mismatch")
        self.assertFalse(hs[nid]["subtree"])
        self.assertIn(nid, self.plan_holds[-1])
        self.assertEqual(st["scan_lists"]["normalization_mismatch"][0]["published_rel"], rel)

    def test_new_rejected_override_is_not_stored_open_and_open_one_is_invalidated(self):
        """Finding 10 (WB 7.4.3): only honoured overrides are open in writeback.db."""
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "A2.md"))
        self.export_overrides = {"given": 1, "applied": [], "rejected": [{"notion_id": A, "local_rel": "General/Home/A2.md",
                                                                          "reason": "name_clash"}]}
        self.run_round()
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT count(*) FROM name_overrides").fetchone()[0], 0)
        db.close()
        self.assertEqual(self.holds_file()[A]["reason"], "override_rejected:name_clash")
        # accepted next round -> open; rejected later -> 'rejected' (失效), no open row left
        self.export_overrides = None
        self.run_round("R2")
        self.export_overrides = {"given": 1, "applied": [], "rejected": [{"notion_id": A, "local_rel": "General/Home/A2.md",
                                                                          "reason": "other_folder"}]}
        self.run_round("R3")
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(db.execute("SELECT local_rel, status FROM name_overrides").fetchall(),
                         [("General/Home/A2.md", "rejected")])
        db.close()
        with open(self.rep("R3", "run_record.md"), encoding="utf-8") as fh:
            self.assertIn("命名差异失效", fh.read())


class TestFixRoundPendingDeletes(Harness):
    def age_pending(self):
        db = sqlite3.connect(self.P.wb_db)
        db.execute("UPDATE pending_deletes SET ttl_until='2000-01-01T00:00:00+07:00'")
        db.commit()
        db.close()

    def rows(self):
        db = sqlite3.connect(self.P.wb_db)
        r = db.execute("SELECT notion_id, status FROM pending_deletes ORDER BY rowid").fetchall()
        db.close()
        return r

    def test_expired_pending_delete_is_restored_even_after_a_failed_round(self):
        """Finding 4 (WB 6.2): expiry is never re-armed as a new hold, also when the expiry round did not publish."""
        self.write_cfg({"mode": "plan", "max_local_deletes": 1})
        self.seed_all_base()
        self.rm(REL[A])
        rc, st = self.run_round("R1")
        self.assertEqual(self.rows(), [(A, "hold")])
        self.age_pending()
        self.verify_override["staging"] = {"status": {"C02": "FAIL"}}
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "failed")
        self.assertEqual(st["pending_deletes_update"]["expired"], [A])
        self.verify_override = {}
        self.rm(REL[B])                                                  # one genuinely new deletion
        rc, st = self.run_round("R3")
        self.assertEqual(st["deletions"]["new_missing"], 1)              # A is not new again
        rules = {r["notion_id"]: r["rule"] for r in st["deletions"]["restore"]}
        self.assertIn("6.2", rules[A])
        self.assertNotIn(A, self.plan_holds[-1])                         # published (restored) from Notion
        self.assertEqual([r for r in self.rows() if r[0] == A], [(A, "expired")])   # no re-armed hold row
        self.put(REL[A], self.files[REL[A]])                             # the restore landed
        self.run_round("R4")
        self.assertIn((A, "restored"), self.rows())

    def test_expiry_round_itself_lists_the_restore(self):
        self.write_cfg({"mode": "plan"})
        self.seed_all_base()
        self.rm(REL[A])
        self.run_round("R1")
        self.age_pending()
        rc, st = self.run_round("R2")
        self.assertIn(A, {r["notion_id"] for r in st["deletions"]["restore"]})
        self.assertNotIn(A, self.plan_holds[-1])

    def test_subtree_member_of_a_pending_delete_is_not_new(self):
        self.write_cfg({"mode": "plan", "max_local_deletes": 5})
        self.seed_all_base()
        self.rm(REL[D])
        self.rm(REL[C])
        rc, st = self.run_round("R1")
        self.assertEqual(st["deletions"]["new_missing"], 2)
        rc, st = self.run_round("R2")
        self.assertEqual(st["deletions"]["new_missing"], 0)              # D is inside C's pending subtree
        self.assertEqual([r for r in self.rows()], [(C, "hold")])


class TestFixRoundRendererAndRunIds(Harness):
    def approve(self, run_id):
        import wb_gate
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/t"), \
                mock.patch.object(wb_gate, "confirm_yes_no", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return refresh.approve_run(self.P, run_id, runner_factory=lambda P, log: self.tools)

    def test_approved_renderer_upgrade_is_accepted_once(self):
        """Finding 5: base rows of objects F did not re-seed keep the old version; no pause after approval."""
        self.seed_all_base(rv="1.2.0")
        rc, st = self.run_round("R1")
        self.assert_paused(rc, st, "renderer_version_changed")
        self.assertEqual(self.approve("R1"), 0)
        self.assertEqual(jload(self.P.last_published_json)["renderer_version"], ["1.2.1"])
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))
        self.assertEqual(st["source_change"]["renderer_version"]["baseline_source"], "last_published")
        rc, st = self.run_round("R3")
        self.assertEqual(st["result"], "published")

    def test_mixed_base_rows_do_not_pause_after_a_published_round(self):
        db = wb_state.connect(self.P.wb_db)
        for n in REL:
            db.execute("INSERT INTO base_history(notion_id, seq, rel_path, sha256, page_json_sha, renderer_version, "
                       "run_id, origin) VALUES(?,?,?,?,?,?,?,?)",
                       (n, 1, REL[n], "0" * 64, sha("pj-" + n), "1.2.0" if n == A else "1.2.1", "R0", "REMOTE"))
        db.close()
        rc, st = self.run_round("R1")                                    # newest base row is 1.2.1
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))

    def test_prune_orders_staging_by_time_not_name(self):
        """Finding 9: custom --run-id names (rehearsals) must not push the newest real runs out of KEEP_STAGING."""
        old = ["GSMOKE1", "GSMOKE2", "GSMOKE3"]
        new = ["20990101T0000-T1", "20990101T0600-T1", "20990101T1200-T1"]
        for i, r in enumerate(old + new):
            d = os.path.join(self.P.staging, r)
            os.makedirs(os.path.join(d, "vault"))
            for f in ("manifest.json", "block_lines.jsonl"):
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("{}\n")
            t = 1_000_000 + i * 1000
            os.utime(os.path.join(d, "manifest.json"), (t, t))
        refresh.prune(io.StringIO(), self.P)
        for r in new:
            self.assertTrue(os.path.isdir(os.path.join(self.P.staging, r, "vault")), r)
        for r in old:
            self.assertFalse(os.path.isdir(os.path.join(self.P.staging, r, "vault")), r)

    def test_publish_supersedes_a_custom_named_pause(self):
        self.write_cfg({"conflict_pause_threshold": 1})
        self.plan_extra = [{"op": "CONFLICT_BOTH", "rel": REL[A], "nid": A}]
        rc, st = self.run_round("ZZZ")
        self.assertEqual(st["result"], "paused_for_review")
        self.write_cfg({})
        self.plan_extra = []
        rc, st = self.run_round("AAA")
        self.assertEqual(st["result"], "published")
        self.assertEqual(jload(self.P.paused_runs)["runs"]["ZZZ"]["status"], "superseded")


class TestFixRoundOverrideAliases(Harness):
    def test_alias_only_failure_holds_the_override_object_and_publishes_the_rest(self):
        """Finding 3 (interim adapter until B emits aliases or D accepts them): the round is not failed."""
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "A2.md"))
        calls = {"n": 0}
        orig = self.verify_result

        def vr(kind):
            if kind == "staging":
                calls["n"] += 1
                dev = jload(self.rep("R1", "expected_deviations.json"))
                if A not in {p["notion_id"] for p in dev["paused"]}:
                    return ({**{c: "PASS" for c in refresh.BLOCKING}, "C08": "FAIL", "C11": "WARN"}, [],
                            {"C08": {"issues": [{"path": "General/Home/A2.md", "issue": "aliases_missing_original_title",
                                                 "expected": "A", "name": "A2"}]}})
            return orig(kind)
        self.verify_result = vr
        rc, st = self.run_round()
        self.assertEqual((rc, st["result"]), (0, "published_with_holds"), st.get("error"))
        self.assertEqual(calls["n"], 2)                                  # first gate + one re-run
        self.assertEqual(st["override_alias_holds"], [A])
        self.assertEqual(st["verify_first"]["C08"], "FAIL")
        self.assertTrue(all(st["verify"][c] == "PASS" for c in refresh.BLOCKING))
        h = self.holds_file()[A]
        self.assertEqual((h["reason"], h["source"], h["subtree"], h["auto_release"]),
                         ("override_aliases_missing", "export", False, True))
        self.assertIn(A, self.plan_holds[-1])
        self.assertIn("override_aliases_missing", st["warnings"])
        self.assertIsNone(self.vault_note("R1-未完成.md"))

    def test_any_other_failure_still_fails_the_round(self):
        os.rename(os.path.join(self.P.vault, *REL[A].split("/")), os.path.join(self.P.vault, "General", "Home", "A2.md"))
        cases = [
            ({"C08": "FAIL", "C06": "FAIL"}, {"C08": {"issues": [{"path": "General/Home/A2.md",
                                                                   "issue": "aliases_missing_original_title"}]}}),
            ({"C08": "FAIL"}, {"C08": {"issues": [{"path": REL[B], "issue": "aliases_missing_original_title"}]}}),
            ({"C08": "FAIL"}, {"C08": {"issues": [{"path": "General/Home/A2.md", "issue": "bad_notion_order"}]}}),
        ]
        for i, (status, details) in enumerate(cases):
            self.verify_override["staging"] = {"status": status, "details": details}
            rc, st = self.run_round(f"F{i}")
            self.assertEqual(st["result"], "failed", (i, st.get("result")))
            self.assertIn("blocking checks failed", st["error"])
            self.assertEqual(self.applied, [])


class TestFixRoundCaptureMain(unittest.TestCase):
    def test_aborted_capture_exits_2_and_records_it(self):
        tmp = tempfile.mkdtemp(prefix="capture_main_")
        self.addCleanup(shutil.rmtree, tmp, True)
        extra = {n: _items(n.split()[0], 1) for n in capture_sidebar.ORDER}
        actions = [{"teamspace": "General", "action": "expand", "result": {"found": True, "clicked": True}},
                   {"teamspace": "General", "action": "abort", "reason": "no root treeitem appeared"}]
        with mock.patch.object(capture_sidebar, "capture", return_value=(LIVE_COLLAPSED, extra, [], actions)), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = capture_sidebar.main(["--out-dir", tmp])
        self.assertEqual(rc, 2)
        ev = [f for f in os.listdir(tmp) if f.endswith(".json") and not f.endswith(".raw.json")]
        d = jload(os.path.join(tmp, ev[0]))
        self.assertFalse(d["order_match"])
        self.assertEqual(d["aborted"], [{"teamspace": "General", "reason": "no root treeitem appeared"}])

    def test_no_synthetic_event_fallback_in_code(self):
        self.assertNotIn("dispatchEvent", capture_sidebar.TOGGLE_JS)
        self.assertNotIn("HARD", capture_sidebar.TOGGLE_JS)


# ---------------------------------------------------------------------- real verifier (D's fixture)
_TVV = None


def tvv_module():
    global _TVV
    if _TVV is None:
        _TVV = _load_tvv()
    return _TVV


def _export_db_from_vault(vault, export_db, sha_override=None, rel_override=None):
    """published_files for every file of `vault` (as test_expected_deviations_pass_verifier_validation does)."""
    import wb_scan
    sha_override, rel_override = sha_override or {}, rel_override or {}
    con = sqlite3.connect(export_db)
    con.execute("CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT, "
                "run_id TEXT, status TEXT)")
    for dp, _d, fns in os.walk(vault):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, vault).replace(os.sep, "/")
            with open(full, "rb") as fh:
                data = fh.read()
            nid, kind, _e = wb_scan.read_note_identity(data) if rel.endswith(".md") else (None, None, None)
            con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                        (rel_override.get(rel, rel), sha_override.get(rel, sha(data)), nid,
                         kind or ("attachment" if "_attachments" in rel else "generated"), "R0", "active"))
    con.commit()
    con.close()


class RealFixture(unittest.TestCase):
    CHILD_ONE = "General/Home/Child One.md"

    def setUp(self):
        self.t = tvv_module()
        self.vv = self.t.vv
        self.tmp = tempfile.mkdtemp(prefix="refresh_real_vv_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.snap = os.path.join(self.tmp, "snap", "snap.db")
        os.makedirs(os.path.dirname(self.snap))
        self.sidebar = os.path.join(self.tmp, "sidebar.json")
        self.roots = os.path.join(self.tmp, "snap.db.roots.json")
        self.t.build_snapshot(self.snap)
        self.t.build_sidebar(self.sidebar, self.roots)
        self.base_vault = os.path.join(self.tmp, "base_vault")
        self.t.write_vault(self.base_vault)

    def copy(self, name):
        d = os.path.join(self.tmp, name)
        shutil.copytree(self.base_vault, d, symlinks=True)
        return d

    def rd(self, v, rel):
        with open(os.path.join(v, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def wr(self, v, rel, text):
        p = os.path.join(v, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def edit(self, v, rel, old, new):
        t = self.rd(v, rel)
        self.assertIn(old, t)
        self.wr(v, rel, t.replace(old, new, 1))

    def verify(self, vault, dev_obj, name):
        devp = os.path.join(self.tmp, name + ".json")
        jdump(dev_obj, devp)
        out = os.path.join(self.tmp, "out_" + name)
        ok, rep, _ = self.vv.verify(self.snap, self.sidebar, vault, out, self.roots, expected_deviations=devp,
                                    snapshot_immutable=True)
        return ok, rep, refresh.read_verify(out)


class TestConflictDeviationsRealVerifier(RealFixture):
    def conflict_copy(self, v, notion_text, rel, run="RX"):
        """L-07 copy as publish_vault.conflict_content writes it (generated frontmatter, body unchanged)."""
        fm_end = notion_text.index("\n---\n", 4)
        keep = [ln for ln in notion_text[4:fm_end].split("\n")
                if ln.split(":", 1)[0] not in ("notion_id", "notion_url", "aliases", "notion_kind")]
        head = ["---", "notion_kind: generated", 'generated_by: "publish_vault conflict-copy"',
                f'conflict_of_notion_id: "{self.t.P2}"', f'conflict_run: "{run}"', f'conflict_vault_path: "{rel}"'] + keep
        stem = rel.rsplit("/", 1)[1][:-3]
        crel = f"Notion_Obsidian/conflicts/{run}/General/Home/{stem} (Notion 版本 {run}).md"
        self.wr(v, crel, "\n".join(head) + notion_text[fm_end:])

    def test_conflict_moved_edited_passes_post_verify(self):
        """Finding 1: Notion renamed Child Old -> Child One while the note was edited at Child Old.md."""
        old = "General/Home/Child Old.md"
        v = self.copy("post_moved")
        notion_text = self.rd(v, self.CHILD_ONE)
        os.replace(os.path.join(v, *self.CHILD_ONE.split("/")), os.path.join(v, *old.split("/")))
        self.edit(v, old, 'notion_title: "Child One"', 'notion_title: "Child Old"')
        published_old = self.rd(v, old)
        self.wr(v, old, published_old + "\nlocal edit kept by the publisher\n")
        self.conflict_copy(v, notion_text, self.CHILD_ONE)
        export_db = os.path.join(self.tmp, "export_moved.db")
        _export_db_from_vault(v, export_db, sha_override={old: sha(published_old)})
        import wb_scan
        scan = wb_scan.scan(v, export_db, None, {})
        self.assertEqual([m["rel"] for m in scan["local_modified"]], [old])
        plan = {"actions": [{"op": "CONFLICT_MOVED_EDITED", "rel": self.CHILD_ONE, "frm": old, "from": old,
                             "nid": self.t.P2, "kind": "page"}]}
        # before the fix: only the scan's local_modified entry -> the object and its parent fail
        dev0 = refresh.build_expected_deviations("RX", scan, {}, {}, {})
        ok0, rep0, _ = self.verify(v, dev0, "dev0")
        self.assertFalse(ok0)
        dev = refresh.build_expected_deviations("RX", scan, {}, {}, {}, (), (), refresh.conflict_kept_paths(plan))
        self.assertEqual(dev["paused"], [{"notion_id": self.t.P2, "subtree": False, "paths": [old, self.CHILD_ONE]}])
        ok, rep, v1 = self.verify(v, dev, "dev1")
        self.assertEqual(rep["registered_deviations"]["rejected_entries"], [])
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in refresh.BLOCKING if v1["_sbc"][c] != "PASS"})
        located, unloc = refresh.locate_failures(v1, v, {})
        self.assertEqual((located, unloc), ({}, []))

    def test_conflict_both_with_l07_copy_passes_post_verify(self):
        v = self.copy("post_both")
        notion_text = self.rd(v, self.CHILD_ONE)
        published = notion_text
        self.wr(v, self.CHILD_ONE, published + "\nlocal edit\n")
        self.conflict_copy(v, notion_text.replace("Some ", "Notion changed: "), self.CHILD_ONE)
        export_db = os.path.join(self.tmp, "export_both.db")
        _export_db_from_vault(v, export_db, sha_override={self.CHILD_ONE: sha(published)})
        import wb_scan
        scan = wb_scan.scan(v, export_db, None, {})
        plan = {"actions": [{"op": "CONFLICT_BOTH", "rel": self.CHILD_ONE, "nid": self.t.P2, "kind": "page"}]}
        dev = refresh.build_expected_deviations("RX", scan, {}, {}, {}, (), (), refresh.conflict_kept_paths(plan))
        ok, rep, v1 = self.verify(v, dev, "dev_both")
        self.assertEqual(rep["registered_deviations"]["rejected_entries"], [])
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in refresh.BLOCKING if v1["_sbc"][c] != "PASS"})


class TestOverrideAliasRealVerifier(RealFixture):
    MY = "General/Home/My Child.md"

    def staging_with_override(self, name):
        """The exporter's output for override {P2: My Child.md} today: links/index/sortspec rewritten, no aliases."""
        v = self.copy(name)
        os.replace(os.path.join(v, *self.CHILD_ONE.split("/")), os.path.join(v, *self.MY.split("/")))
        home = "General/Home/Home.md"
        self.edit(v, home, "[[General/Home/Child One|Child One]]\n\n> [!toggle]", "[[General/Home/My Child|Child One]]\n\n> [!toggle]")
        self.edit(v, home, "[[General/Home/Child One|Child One]] is linked", "[[General/Home/My Child|Child One]] is linked")
        self.edit(v, "Notion_Obsidian/Godcreate 目录索引.md", "[[General/Home/Child One|", "[[General/Home/My Child|")
        self.edit(v, "Notion_Obsidian/sortspec.md", "/:files. Child One.md", "/:files. My Child.md")
        return v

    def test_rename_staging_and_post_verify_with_the_adapter(self):
        """Finding 3: with today's exporter the only staging FAIL is C08 aliases on the override note; the adapter
        holds that one object and the whole-run gate passes with the real verifier (staging and post-publish)."""
        user_vault = self.copy("user_vault")
        os.replace(os.path.join(user_vault, *self.CHILD_ONE.split("/")), os.path.join(user_vault, *self.MY.split("/")))
        export_db = os.path.join(self.tmp, "export_ovr.db")
        _export_db_from_vault(self.base_vault, export_db)
        import wb_scan
        scan = wb_scan.scan(user_vault, export_db, None, {})
        overrides, new, _close = refresh.compute_overrides(scan)
        self.assertEqual(overrides, {self.t.P2: self.MY})
        applied = {self.t.P2: {"old_rel": self.CHILD_ONE, "local_rel": self.MY}}
        staging = self.staging_with_override("staging")
        ok0, rep0, v0 = self.verify(staging, refresh.build_expected_deviations("RX", scan, {}, {}, applied), "stg0")
        failed = [c for c in refresh.BLOCKING if v0["_sbc"][c] != "PASS"]
        # L-11: the listed override's missing alias is 命名差异 (C08 INFO), so staging passes without any hold
        self.assertEqual(failed, [])
        self.assertTrue(ok0)
        self.assertEqual(rep0["checks"]["C08"]["registered_deviations"], 1)
        self.assertEqual(rep0["registered_deviations"]["rejected_entries"], [])
        self.assertEqual(refresh.override_alias_failures(v0, failed, applied), {})
        # the adapter stays as a fallback: verifying WITHOUT the override listed fails C03 + C08 (never only C08),
        # so the adapter does not fire and the round would fail -- an unlisted rename is never silently accepted
        ok_u, _rep_u, v_u = self.verify(staging, refresh.build_expected_deviations("RX", scan, {}, {}, {}), "stg_unlisted")
        self.assertFalse(ok_u)
        failed_u = [c for c in refresh.BLOCKING if v_u["_sbc"][c] != "PASS"]
        self.assertIn("C03", failed_u)
        self.assertEqual(refresh.override_alias_failures(v_u, failed_u, {}), {})
        alias = {self.t.P2: self.MY}
        holds = {self.t.P2: {"reason": "override_aliases_missing", "subtree": False, "source": "export",
                             "paths": [self.MY, self.CHILD_ONE]}}
        dev = refresh.build_expected_deviations("RX", scan, holds, {}, applied)
        ok1, rep1, v1 = self.verify(staging, dev, "stg1")
        self.assertEqual(rep1["registered_deviations"]["rejected_entries"], [])
        self.assertTrue(ok1, {c: rep1["checks"][c]["summary"] for c in refresh.BLOCKING if v1["_sbc"][c] != "PASS"})
        # post-publish: everything but the held note comes from staging; the held note is the user's renamed file
        post = self.staging_with_override("post")
        shutil.copyfile(os.path.join(user_vault, *self.MY.split("/")), os.path.join(post, *self.MY.split("/")))
        ok2, rep2, v2 = self.verify(post, dev, "post")
        self.assertTrue(ok2, {c: rep2["checks"][c]["summary"] for c in refresh.BLOCKING if v2["_sbc"][c] != "PASS"})
        # a once-fixed exporter (aliases emitted) passes without any hold
        fixed = self.staging_with_override("fixed")
        self.edit(fixed, self.MY, "notion_created_time", 'aliases:\n  - "Child One"\nnotion_created_time')
        ok3, _rep3, v3 = self.verify(fixed, refresh.build_expected_deviations("RX", scan, {}, {}, applied), "fixed")
        self.assertTrue(ok3)


    def test_two_rename_rounds_publish_with_the_real_verifier(self):
        """Stage-0 ⑤ shape (rename -> 2 rounds -> C01-C10 PASS) through run_round: export/publish are stand-ins,
        both verifies are the REAL verifier on D's fixture; the round result is published_with_holds."""
        work, user_vault = os.path.join(self.tmp, "work"), self.copy("round_vault")
        os.replace(os.path.join(user_vault, *self.CHILD_ONE.split("/")), os.path.join(user_vault, *self.MY.split("/")))
        P = refresh.Paths(work, user_vault)
        for d in (P.state, P.reports, P.staging, P.snapshots, P.logs, P.evidence, P.approvals, os.path.dirname(P.config)):
            os.makedirs(d, exist_ok=True)
        jdump({}, P.config)
        with open(os.path.join(P.approvals, refresh.STANDING_APPROVAL), "w", encoding="utf-8") as fh:
            fh.write("# standing approval (test)\n")
        _export_db_from_vault(self.base_vault, P.export_db)
        snap = os.path.join(P.snapshots, "snap_20991231T0000.db")
        shutil.copyfile(self.snap, snap)
        con = sqlite3.connect(snap)                       # a temp copy of the fixture: mark it finished
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('finished_at', '2026-11-17T00:10:00+00:00')")
        con.commit()
        con.close()
        shutil.copyfile(self.roots, snap + ".roots.json")
        staging_src = self.staging_with_override("staging_src")
        t, P2, MY = self, self.t.P2, self.MY
        seen = {"verify": []}

        def runner(script, args, timeout=None):
            a = parse_args([str(x) for x in args])
            if script == "export_obsidian.py":
                stg = os.path.join(P.staging, a["--run-id"])
                shutil.copytree(staging_src, os.path.join(stg, "vault"))
                jdump({"renderer_version": "1.2.1", "block_lines": {"pages": 1},
                       "overrides": {"given": 1, "applied": [{"notion_id": P2, "local_rel": MY}], "rejected": []}},
                      os.path.join(stg, "export_report.json"))
                jdump({}, os.path.join(stg, "manifest.json"))
                return 0
            if script == "verify_vault.py":
                ok, _rep, _ = t.vv.verify(t.snap, t.sidebar, a["--vault"], a["--out"], t.roots,
                                          expected_deviations=a["--expected-deviations"], snapshot_immutable=True)
                seen["verify"].append((os.path.basename(a["--out"]), ok))
                return 0 if ok else 1
            if script == "publish_vault.py":
                holds = jload(a["--holds"])
                if "--apply" not in a:
                    acts = [{"op": "SKIP_HELD" if P2 in holds else "ADOPT_RENAME", "rel": MY, "nid": P2, "kind": "page"}]
                    plan = {"run_id": a["--run-id"], "actions": acts, "tombstones": []}
                    plan["plan_sha256"] = plan_sha256(plan)
                    jdump(plan, os.path.join(a["--report-dir"], "publish_plan.json"))
                    return 0
                stg = os.path.join(P.staging, a["--run-id"], "vault")   # F stand-in: everything but the held note
                for dp, _d, fns in os.walk(stg):
                    for fn in fns:
                        rel = os.path.relpath(os.path.join(dp, fn), stg).replace(os.sep, "/")
                        if rel == MY and P2 in holds:
                            continue
                        dst = os.path.join(P.vault, *rel.split("/"))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(os.path.join(dp, fn), dst)
                return 0
            raise AssertionError(script)
        runner.calls = []
        for rid in ("RN1", "RN2"):
            opts = argparse.Namespace(mode="incremental", dry_run=False, use_snapshot=snap, sidebar=None, run_id=rid)
            with contextlib.redirect_stdout(io.StringIO()):
                rc, st = refresh.run_round(P, opts, runner_factory=lambda P_, log: runner, client_factory=lambda P_: None)
            # L-11: no alias hold any more -> the rename is adopted and the round publishes cleanly
            self.assertEqual((rc, st["result"]), (0, "published"), (rid, st.get("error")))
            self.assertFalse(st.get("override_alias_holds"))
            self.assertTrue(all(st["verify"][c] == "PASS" for c in refresh.BLOCKING), st["verify"])
            self.assertTrue(all(st["post_publish_verify"][c] == "PASS" for c in refresh.BLOCKING), st["post_publish_verify"])
            self.assertFalse(os.path.exists(os.path.join(user_vault, "Notion_Obsidian", "运行记录", f"{rid}-未完成.md")))
        self.assertEqual([x[1] for x in seen["verify"]], [True, True] * 2)   # staging, post (x2); no retry (L-11)
        self.assertTrue(os.path.exists(os.path.join(user_vault, *MY.split("/"))))
        self.assertFalse(os.path.exists(os.path.join(user_vault, *self.CHILD_ONE.split("/"))))



# ====================================================================== wave-4 fix round 1 (n2o-pipeline)
class TestW4RunlogNotesAndSidebar(Harness):
    """r1-n2o-completeness-06/-07, r1-operability-02/-03, r1-spec-coverage-03, r1-contract-consistency-05: the
    warnings of a PUBLISHED round reach the vault run log (publish_vault --runlog-notes), and a failed sidebar
    capture is a warning (age, root count, consecutive count) instead of a silent fallback."""

    fake = TestNotionReads.fake            # helper only (inheriting would run TestNotionReads' tests twice)

    def setUp(self):
        super().setUp()
        import logging
        lg = logging.getLogger("nmig.client")
        old = lg.level
        lg.setLevel(logging.ERROR)
        self.addCleanup(lg.setLevel, old)

    def notes_of(self):
        a = self.applied[-1]
        self.assertIn("--runlog-notes", a)
        return jload(a["--runlog-notes"])

    def test_published_round_passes_new_root_and_member_notes_to_the_publisher(self):
        f, cf = self.fake(persons=2)
        self.sidebar_roots = self.sidebar_roots + [{"id": uid(900), "title": "Brand new root", "teamspace": "General",
                                                    "teamspace_order": 1, "root_order": 2}]
        rc, st = self.run_round(use_snapshot=None, client_factory=cf)
        self.assertEqual(st["result"], "published", st.get("error"))
        notes = self.notes_of()
        by = {n["key"].split(":")[0]: n for n in notes}
        self.assertEqual(sorted(by), ["new_roots_unshared", "warn_block_limit"])
        self.assertIn(uid(900), by["new_roots_unshared"]["key"])
        self.assertTrue(any(refresh.FIX_STEPS_NEW_ROOT in x for x in by["new_roots_unshared"]["lines"]))
        self.assertIn(refresh.BLOCK_LIMIT_WARNING, by["warn_block_limit"]["lines"])

    def failing_capture(self, rc_value=2):
        """Older good evidence (2 roots) and a capture that fails like production since 2026-11-17 18:59."""
        jdump({"order_match": True, "captured_at": "2026-11-17T04:17:00+07:00", "roots": self.sidebar_roots},
              os.path.join(self.P.evidence, "sidebar_20261117T0417.json"))

        def bad_capture(a):
            jdump({"order_match": False, "roots": []}, os.path.join(self.P.evidence, "sidebar_20991231T2359.json"))
            return rc_value
        self.tools.capture_sidebar = bad_capture

    def test_failed_sidebar_capture_warns_records_and_escalates(self):
        f, cf = self.fake(persons=1)
        self.failing_capture()
        for i in range(1, refresh.SIDEBAR_FAIL_ALERT + 1):
            rc, st = self.run_round(f"S{i}", use_snapshot=None, client_factory=cf)
            self.assertEqual(st["result"], "published", st.get("error"))
            self.assertFalse(st["sidebar_fresh"])
            self.assertIn("sidebar_capture_failed", st["warnings"])
            sc = st["sidebar_capture"]
            self.assertEqual((sc["evidence"], sc["roots"], sc["consecutive_failures"]),
                             ("sidebar_20261117T0417.json", 2, i))
            self.assertGreater(sc["evidence_age_hours"], 24)
            self.assertEqual(sc["escalated"], i >= refresh.SIDEBAR_FAIL_ALERT)
            self.assertIn("侧栏抓取失败", self.stdout)
            note = [n for n in self.notes_of() if n["key"].startswith("sidebar_capture_failed")]
            self.assertEqual(len(note), 1)
            self.assertIn("sidebar_20261117T0417.json", note[0]["key"])
            with open(self.rep(f"S{i}", "run_record.md"), encoding="utf-8") as fh:
                self.assertIn("侧栏抓取失败（新顶层页面不会被发现）", fh.read())
        # one good capture resets the counter
        self.tools.capture_sidebar = FakeTools.capture_sidebar.__get__(self.tools)
        rc, st = self.run_round("S9", use_snapshot=None, client_factory=cf)
        self.assertTrue(st["sidebar_fresh"])
        self.assertNotIn("sidebar_capture", st)
        self.assertFalse(os.path.exists(os.path.join(self.P.state, "sidebar_capture_failures.json")))

    def test_dry_run_capture_failure_writes_no_state(self):
        f, cf = self.fake(persons=1)
        self.failing_capture()
        rc, st = self.run_round("SD", use_snapshot=None, client_factory=cf, dry_run=True)
        self.assertEqual(st["result"], "planned", st.get("error"))
        self.assertIn("sidebar_capture_failed", st["warnings"])
        self.assertFalse(os.path.exists(os.path.join(self.P.state, "sidebar_capture_failures.json")))


class TestW4PauseDetails(Harness):
    """r1-n2o-completeness-08, r1-user-journey-10 (WB 14.4: 原因、涉及对象的行内代码路径与处理命令)."""

    def test_source_mass_trash_note_names_the_vanished_pages(self):
        many = {uid(200 + i): (H, f"Gone {i}") for i in range(10)}
        prev = make_snapshot(os.path.join(self.P.snapshots, "snap_20991230T0000.db"), dict(self.pages, **many))
        with open(self.P.last_published_txt, "w", encoding="utf-8") as fh:
            fh.write(prev + "\n")
        rc, st = self.run_round()
        txt = self.assert_paused(rc, st, "source_mass_trash")
        self.assertIn("本轮在 Notion 中消失的页面", txt)
        for i in range(10):
            self.assertIn(f"`Gone {i}`", txt)
        self.assertIn(uid(205), txt)
        self.assertIn("放行后将写入库内的改动", txt)

    def test_local_mass_delete_note_lists_the_notes_and_the_hold_command(self):
        self.write_cfg({"mode": "plan", "max_local_deletes": 1})
        self.seed_all_base()
        self.rm(REL[A])
        self.rm(REL[B])
        rc, st = self.run_round()
        txt = self.assert_paused(rc, st, "local_mass_delete")
        self.assertIn(f"`{REL[A]}`", txt)
        self.assertIn(f"`{REL[B]}`", txt)
        self.assertIn(f"`{wb_common.cli_command('writeback', 'hold', '--run', 'R1', work=self.P.work, vault=self.P.vault)}`", txt)

    def test_rejected_entry_note_names_the_file_and_the_cause(self):
        self.verify_override["staging"] = {"rejected": [{"category": "local_new", "path": "x.md", "reason": "has_notion_id"}]}
        rc, st = self.run_round()
        txt = self.assert_paused(rc, st, "expected_deviations_rejected_entries")
        self.assertIn("`x.md`", txt)
        self.assertIn("同名", txt)

    def test_previous_unlocatable_note_names_the_item(self):
        self.verify_override["post"] = {"status": {"C05": "FAIL"},
                                        "details": {"C05": {"problems": [{"section": "General", "issue": "order"}]}}}
        self.run_round()
        self.verify_override = {}
        self.applied.clear()
        rc, st = self.run_round("R2")
        txt = self.assert_paused(rc, st, "previous_post_verify_unlocatable")
        self.assertIn("上一轮（R1）发布后复验失败", txt)
        self.assertIn("C05 problems", txt)
        self.assertIn('"issue": "order"', txt)
        # the failure note of R1 names the item too (and has a 处理 section)
        r1 = self.vault_note("R1-未完成.md")
        self.assertIn("复验失败但无法定位到对象的项目", r1)
        self.assertIn("## 处理", r1)

    def test_source_mass_change_note_names_the_changed_page_and_what_approve_writes(self):
        self.seed_all_base()
        self.pj_override = {A: {"page_json_sha": sha("changed")}}
        self.plan_replace = lambda holds: [{"op": "REPLACE", "rel": REL[A], "nid": A, "kind": "page", "sha": "s",
                                            "before": "b", "expect": "s"}] + \
            [{"op": "UNCHANGED", "rel": REL[n], "nid": n, "kind": "page", "sha": "x"} for n in REL if n != A]
        rc, st = self.run_round()
        txt = self.assert_paused(rc, st, "source_mass_change")
        self.assertIn("本轮在 Notion 中有改动的页面", txt)
        self.assertIn(f"REPLACE `{REL[A]}`", txt)


class TestW4ApproveRun(Harness):
    """r1-user-journey-05, r1-n2o-completeness-12, r1-operability-11."""
    pause_one = TestHumanDecisions.pause_one

    def approve(self, run_id="R1", yes=True):
        import wb_gate
        asked = []
        err = io.StringIO()
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"), \
                mock.patch.object(wb_gate, "confirm_yes_no", side_effect=lambda q: asked.append(q) or yes), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            rc = refresh.approve_run(self.P, run_id, runner_factory=lambda P, log: self.tools)
        return rc, asked, out.getvalue(), err.getvalue()

    def test_plan_difference_is_refused_before_the_question_with_a_diff_and_a_next_step(self):
        self.pause_one()
        self.plan_extra.append({"op": "REPLACE", "rel": REL[B], "nid": B})        # the world changed
        rc, asked, out, err = self.approve()
        self.assertEqual(rc, refresh.EXIT_FAIL)
        self.assertEqual(asked, [])                                              # refused BEFORE y/N
        self.assertIn(f"`{REL[B]}`：暂停时 UNCHANGED → 现在 REPLACE+UNCHANGED", err)
        self.assertIn("--discard-run R1", err)
        self.assertIn("下一轮", err)
        self.assertEqual(self.applied, [])

    def test_superseded_run_names_the_newer_pause(self):
        self.pause_one()
        rc, st = self.run_round("R2")
        self.assertEqual(st["result"], "paused_for_review")
        rc, asked, out, err = self.approve("R1")
        self.assertEqual(rc, refresh.EXIT_FAIL)
        self.assertIn("R2", err)
        self.assertIn("--approve-run R2", err)

    def test_approve_with_holds_is_published_with_holds(self):
        refresh.save_holds(self.P, {B: {"reason": "post_verify_C02", "source": "post_verify", "auto_release": False,
                                        "subtree": True}})
        self.pause_one()
        rc, asked, out, err = self.approve()
        self.assertEqual(rc, 0, err)
        st = jload(self.rep("R1", "approve", "refresh_status.json"))
        self.assertEqual(st["result"], "published_with_holds")
        self.assertTrue(asked)
        self.assertIn(f"`{REL[A]}`", out)                                      # the conflict is shown before y/N

    def test_approve_post_verify_failure_writes_the_vault_failure_note(self):
        self.pause_one()
        self.verify_override["post"] = {"status": {"C02": "FAIL"},
                                        "details": {"C02": {"duplicates": [{"id": B, "paths": [REL[B]]}]}}}
        rc, asked, out, err = self.approve()
        self.assertEqual(rc, refresh.EXIT_FAIL)
        st = jload(self.rep("R1", "approve", "refresh_status.json"))
        self.assertEqual(st["result"], "published_post_verify_failed")
        note = self.vault_note("R1-未完成.md")
        self.assertIsNotNone(note)
        self.assert_generated_note_rules(note)
        self.assertIn("## 处理", note)

    def test_rc5_is_published_with_a_warning(self):
        self.pause_one()
        self.apply_rc = 5
        rc, asked, out, err = self.approve()
        self.assertEqual(rc, 0, err)
        st = jload(self.rep("R1", "approve", "refresh_status.json"))
        self.assertIn(st["result"], ("published", "published_with_holds"))
        self.assertIn("base_history_import_failed", st["warnings"])
        self.assertEqual(jload(self.P.last_published_json)["run_id"], "R1")
        self.assertEqual(jload(self.P.paused_runs)["runs"]["R1"]["status"], "approved")

    def test_rc4_race_holds_the_raced_object_and_supersedes_the_run(self):
        self.pause_one()
        self.apply_rc = 4
        self.race_rels = [REL[B]]
        rc, asked, out, err = self.approve()
        self.assertEqual(rc, refresh.EXIT_FAIL)
        st = jload(self.rep("R1", "approve", "refresh_status.json"))
        self.assertEqual(st["raced_ids"], [B])
        self.assertTrue(st["partially_applied"])
        self.assertEqual(self.holds_file()[B]["reason"], "apply_race")
        e = jload(self.P.paused_runs)["runs"]["R1"]
        self.assertEqual(e["status"], "superseded")
        note = self.vault_note("R1-未完成.md")
        self.assertIsNotNone(note)
        self.assertIn("本轮已部分写入，剩余写入已停止", note)
        self.assertIn("执行期间检测到库内文件竞态", note)
        self.assertIn(REL[B], note)


class TestW4FailureNotesDryRunHelp(Harness):
    """r1-operability-07 / -08 / -09, r1-n2o-completeness-11."""

    def test_failure_note_has_a_chinese_reason_and_a_next_step(self):
        self.verify_override["staging"] = {"status": {"C03": "FAIL"}}
        rc, st = self.run_round()
        self.assertEqual(st["result"], "failed")
        note = self.vault_note("R1-未完成.md")
        self.assert_generated_note_rules(note)
        self.assertIn("暂存库校验未通过", note)
        self.assertIn("## 处理", note)
        self.assertIn("reports/R1/verify/verify.md", note)
        self.assertIn("blocking checks failed", note)                 # the raw message stays as 技术信息

    def test_failed_dry_run_writes_no_vault_note_and_prunes_nothing(self):
        self.verify_override["staging"] = {"status": {"C03": "FAIL"}}
        old = os.path.join(self.P.staging, "OLD", "vault")
        os.makedirs(old)
        for r in ("A1", "A2", "A3", "A4"):
            os.makedirs(os.path.join(self.P.staging, r, "vault"))
        rc, st = self.run_round(dry_run=True)
        self.assertEqual(st["result"], "failed")
        self.assertFalse(os.path.exists(os.path.join(self.P.vault, "Notion_Obsidian", "运行记录")))
        self.assertTrue(os.path.isdir(old))                              # prune did not run

    def test_child_tools_run_without_bytecode(self):
        self.assertEqual(refresh.PY[1:], ["-I", "-B"])
        calls = []

        class R(refresh.Runner):
            pass
        with open(os.path.join(self.tmp, "log.txt"), "w") as log, \
                mock.patch.object(refresh.subprocess, "run",
                                  side_effect=lambda argv, **kw: calls.append(argv) or mock.Mock(returncode=0)):
            R(self.P, log)("export_obsidian.py", ["--help"])
        self.assertEqual(calls[0][1:3], ["-I", "-B"])

    def test_help_is_bilingual_with_exit_codes_and_the_approval_flow(self):
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(TOOLS, "refresh.py"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        out = r.stdout.decode("utf-8")
        for s in ("退出码 / exit codes", "--approve-run", "--discard-run", "--release-hold", "writeback.py hold --run",
                  "已暂停.md", "incremental：", "示例 / examples"):
            self.assertIn(s, out)
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(TOOLS, "refresh.py"), "--sidebar", "x.json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("中止：--sidebar 只能与 --use-snapshot 一起使用", r.stderr.decode("utf-8"))


class TestW4Coverage(Harness):
    """r1-spec-coverage-04 / -05, r1-operability-13, r1-data-safety-04 (unit)."""

    def test_page_state_blocked_and_partial_create_are_held_with_subtree(self):
        for state in ("BLOCKED", "PARTIAL_CREATE"):
            with self.subTest(state=state):
                db = wb_state.connect(self.P.wb_db)
                db.execute("DELETE FROM page_state")
                db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                           (C, state, "test", "2026-11-20T00:00:00+07:00"))
                db.commit()
                db.close()
                rc, st = self.run_round(f"PS-{state}")
                self.assertEqual(st["result"], "published_with_holds", st.get("error"))
                held = self.plan_holds[-1]
                self.assertEqual(held[C]["reason"], f"page_state_{state}")
                self.assertEqual(held[D]["reason"], f"subtree_of:{C}")
                self.assertTrue(set(held) <= {C, D}, held)
                acts = {a["rel"]: a["op"] for a in jload(self.rep(f"PS-{state}", "publish_plan.json"))["actions"]}
                self.assertEqual((acts[REL[C]], acts[REL[D]], acts[REL[A]]), ("SKIP_HELD", "SKIP_HELD", "UNCHANGED"))

    def test_pages_trashed_via_writeback_never_count_for_the_source_trash_pause(self):
        many = {uid(200 + i): (H, f"Trashed {i}") for i in range(10)}
        prev = make_snapshot(os.path.join(self.P.snapshots, "snap_20991230T0000.db"), dict(self.pages, **many))
        with open(self.P.last_published_txt, "w", encoding="utf-8") as fh:
            fh.write(prev + "\n")
        con = sqlite3.connect(self.P.export_db)
        for nid in many:
            con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                        (f"General/Home/{nid}.md", "0" * 64, nid, "page", "R0", "trashed_via_writeback"))
            con.execute("INSERT INTO objects_map(notion_id, kind, rel_path, parent_id, title, status) VALUES(?,?,?,?,?,?)",
                        (nid, "page", f"General/Home/{nid}.md", H, "t", "trashed_via_writeback"))
        con.commit()
        con.close()
        rc, st = self.run_round()
        self.assertEqual(st["result"], "published", st.get("pause_reasons"))
        ta = jload(self.rep("R1", "trash_archive.json"))
        self.assertEqual(ta["count_for_pause"], 0)
        self.assertEqual(st["source_change"]["disappeared"], 10)

    def test_writeback_db_keeps_block_lines_of_the_two_newest_bases_only(self):
        db = wb_state.connect(self.P.wb_db)
        for seq, bsha in enumerate(("a" * 64, "b" * 64, "c" * 64), start=1):
            db.execute("INSERT INTO base_history(notion_id, seq, rel_path, sha256, page_json_sha, renderer_version, "
                       "run_id, origin) VALUES(?,?,?,?,?,?,?,?)", (A, seq, REL[A], bsha, "p", "1.2.1", f"R{seq}", "REMOTE"))
            db.execute("INSERT INTO block_lines(notion_id, base_sha, seq, block_id) VALUES(?,?,?,?)", (A, bsha, 0, "x"))
        db.execute("INSERT INTO block_lines(notion_id, base_sha, seq, block_id) VALUES(?,?,?,?)", (B, "d" * 64, 0, "y"))
        db.commit()
        db.close()
        out = refresh.maintain_writeback_db(self.P, io.StringIO())
        self.assertEqual(out["block_lines_deleted"], 1)
        db = sqlite3.connect(self.P.wb_db)
        self.assertEqual(sorted(r[0] for r in db.execute("SELECT base_sha FROM block_lines WHERE notion_id=?", (A,))),
                         ["b" * 64, "c" * 64])
        self.assertEqual(db.execute("SELECT count(*) FROM base_history").fetchone()[0], 3)   # chain kept (STALE_REVERT)
        self.assertEqual(db.execute("SELECT count(*) FROM block_lines WHERE notion_id=?", (B,)).fetchone()[0], 1)
        db.close()
        with wb_locks.lock("writeback", state_dir=self.P.state):          # a writeback.py command runs: skipped
            self.assertEqual(refresh.maintain_writeback_db(self.P, io.StringIO()), {"skipped": "writeback lock busy"})

    def test_write_json_survives_names_that_are_not_utf8(self):
        p = os.path.join(self.tmp, "x.json")
        refresh.write_json(p, {"local_new": ["General/\udcc4\udce3.md"]})
        self.assertEqual(jload(p), {"local_new": ["General/\udcc4\udce3.md"]})


if __name__ == "__main__":
    unittest.main()
