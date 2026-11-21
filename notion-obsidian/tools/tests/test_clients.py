#!/usr/bin/env python3
"""Tests for the A1 client layer: notion_tokens, rate_gate, readonly_client, writeback_client,
the FakeNotion server (tools/tests/fake_notion.py) and the snapshot/assets_fetch client wiring.

No network except the local fake on 127.0.0.1; token files are temp files only.
Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_clients.py' -v
"""
import ast
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fake_notion  # noqa: E402
import notion_tokens  # noqa: E402
import rate_gate  # noqa: E402
import readonly_client  # noqa: E402
import writeback_client  # noqa: E402
from fake_notion import FakeNotion  # noqa: E402
from nmig.client import NotionAPIError  # noqa: E402
from notion_tokens import TokenError, load_read_token, load_writeback_token  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import ReadOnlyClient, ReadOnlyViolation, make_read_client  # noqa: E402
from writeback_client import WriteForbidden, WritebackClient, WriteResult  # noqa: E402

QUIET = ("nmig.client", "writeback_client")
_saved_levels = {}


def setUpModule():
    for name in QUIET:
        lg = logging.getLogger(name)
        _saved_levels[name] = lg.level
        lg.setLevel(logging.CRITICAL)


def tearDownModule():
    for name, lvl in _saved_levels.items():
        logging.getLogger(name).setLevel(lvl)


def para(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def plain(block):
    t = block["type"]
    return "".join(x["plain_text"] for x in block[t].get("rich_text", []))


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# ====================================================================== tokens
class TokenLoaderTests(unittest.TestCase):
    SECRET = "ntn_TESTTOKEN_abc123XYZ"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a1tok_")
        self.cfg = os.path.join(self.tmp, "notion-obsidian")
        os.mkdir(self.cfg)
        os.chmod(self.cfg, 0o700)
        self.env = os.path.join(self.cfg, "writeback.env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text, mode=0o600, path=None):
        path = path or self.env
        if os.path.lexists(path):
            os.unlink(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, mode)
        return path

    def assertRefused(self, fn, *a):
        with self.assertRaises(TokenError) as cm:
            fn(*a)
        self.assertNotIn(self.SECRET, str(cm.exception))
        self.assertNotIn("abc123", str(cm.exception))
        return str(cm.exception)

    def test_writeback_ok(self):
        self._write(f"# comment\nOTHER=1\nNOTION_WRITEBACK_TOKEN={self.SECRET}\n")
        self.assertEqual(load_writeback_token(self.env), self.SECRET)

    def test_writeback_export_and_quotes(self):
        self._write(f'export NOTION_WRITEBACK_TOKEN="{self.SECRET}"\n')
        self.assertEqual(load_writeback_token(self.env), self.SECRET)

    def test_file_mode_not_0600_refused(self):
        for mode in (0o640, 0o644, 0o400, 0o660, 0o700):
            self._write(f"NOTION_WRITEBACK_TOKEN={self.SECRET}\n", mode=mode)
            msg = self.assertRefused(load_writeback_token, self.env)
            self.assertIn(oct(mode), msg)

    def test_dir_mode_not_0700_refused(self):
        self._write(f"NOTION_WRITEBACK_TOKEN={self.SECRET}\n")
        for mode in (0o750, 0o755, 0o770):
            os.chmod(self.cfg, mode)
            self.assertRefused(load_writeback_token, self.env)
        os.chmod(self.cfg, 0o700)
        self.assertEqual(load_writeback_token(self.env), self.SECRET)

    def test_symlink_refused(self):
        real = self._write(f"NOTION_WRITEBACK_TOKEN={self.SECRET}\n", path=os.path.join(self.cfg, "real.env"))
        link = os.path.join(self.cfg, "link.env")
        os.symlink(real, link)
        self.assertRefused(load_writeback_token, link)

    def test_symlinked_dir_refused(self):
        self._write(f"NOTION_WRITEBACK_TOKEN={self.SECRET}\n")
        ldir = os.path.join(self.tmp, "linkdir")
        os.symlink(self.cfg, ldir)
        self.assertRefused(load_writeback_token, os.path.join(ldir, "writeback.env"))

    def test_missing_or_empty_refused(self):
        self.assertRefused(load_writeback_token, self.env)          # no file
        self._write("OTHER=1\n")
        self.assertRefused(load_writeback_token, self.env)          # no variable
        self._write("NOTION_WRITEBACK_TOKEN=\n")
        self.assertRefused(load_writeback_token, self.env)          # empty
        self._write("NOTION_WRITEBACK_TOKEN=ntn_a b\n")
        self.assertRefused(load_writeback_token, self.env)          # whitespace inside

    def test_writeback_env_var_is_ignored(self):
        with mock.patch.dict(os.environ, {"NOTION_WRITEBACK_TOKEN": self.SECRET}):
            self.assertRefused(load_writeback_token, self.env)      # file missing -> refused despite env

    def test_read_token_from_explicit_path_ignores_env(self):
        p = self._write("NOTION_TARGET_TOKEN=ntn_readtoken\n", mode=0o644, path=os.path.join(self.tmp, "r.env"))
        with mock.patch.dict(os.environ, {"NOTION_TARGET_TOKEN": "ntn_fromenv"}):
            self.assertEqual(load_read_token(p), "ntn_readtoken")

    def test_read_token_env_wins_without_path(self):
        no_files = mock.patch.object(notion_tokens, "_read_regular_file",
                                     side_effect=AssertionError("must not read any file"))
        with mock.patch.dict(os.environ, {"NOTION_TARGET_TOKEN": "ntn_fromenv"}), no_files:
            self.assertEqual(load_read_token(), "ntn_fromenv")      # the real ~/.config file is never read

    def test_read_token_missing_refused(self):
        p = self._write("X=1\n", mode=0o600, path=os.path.join(self.tmp, "r.env"))
        self.assertRefused(load_read_token, p)

    def test_defaults_and_mask(self):
        self.assertEqual(notion_tokens.WRITEBACK_ENV_FILE, "~/.config/notion-obsidian/writeback.env")
        self.assertEqual(notion_tokens.READ_ENV_FILE, "~/.config/notion-migrate/.env")
        m = notion_tokens.mask(self.SECRET)
        self.assertNotIn("abc123", m)
        self.assertIn(str(len(self.SECRET)), m)


# ====================================================================== rate gate
class RateGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a1gate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state", "rate_gate.json")
        self.clock = FakeClock()

    def gate(self, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("sleep", self.clock.sleep)
        return RateGate(self.path, **kw)

    def test_burst_then_steady_rate(self):
        g = self.gate(total_rps=2.0, burst=3)
        t0 = self.clock.t
        for _ in range(3):
            g.acquire()
        self.assertEqual(self.clock.t, t0)                      # burst is free
        for _ in range(4):
            g.acquire()
        self.assertAlmostEqual(self.clock.t - t0, 2.0, places=6)   # 4 more at 2/s
        self.assertEqual(g.stats["acquired"], 7)

    def test_state_shared_between_instances(self):
        a, b = self.gate(total_rps=1.0, burst=2), self.gate(total_rps=1.0, burst=2)
        t0 = self.clock.t
        a.acquire()
        b.acquire()
        a.acquire()        # bucket of 2 shared: third token needs 1 s
        self.assertAlmostEqual(self.clock.t - t0, 1.0, places=6)

    def test_pause_blocks_every_instance_and_only_extends(self):
        a, b = self.gate(total_rps=10.0), self.gate(total_rps=10.0)
        t0 = self.clock.t
        a.pause(5)
        a.pause(2)                                             # never shortens
        self.assertAlmostEqual(b.state()["paused_for"], 5.0, places=6)
        b.acquire()
        self.assertGreaterEqual(self.clock.t - t0, 5.0)
        self.assertLess(self.clock.t - t0, 5.0 + 0.2)          # restarts at the steady rate

    def test_pause_clamped_and_garbage_ignored(self):
        g = self.gate()
        g.pause(10 ** 9)
        self.assertLessEqual(g.state()["paused_for"], RateGate.MAX_PAUSE)
        g2 = RateGate(os.path.join(self.tmp, "g2.json"), clock=self.clock, sleep=self.clock.sleep)
        for bad in (None, "x", float("nan"), float("inf"), -1, 0):
            g2.pause(bad)
        self.assertEqual(g2.state()["paused_for"], 0.0)

    def test_per_process_cap(self):
        g = self.gate(total_rps=10.0, burst=3, cap_rps=1.0)
        t0 = self.clock.t
        for _ in range(5):
            g.acquire()
        self.assertAlmostEqual(self.clock.t - t0, 2.0, places=6)    # (5 - 3 burst) / 1 rps
        self.assertEqual(self.gate(total_rps=2.5, cap_rps=9).cap_rps, 2.5)   # cap never above total

    def test_corrupt_state_and_clock_jump(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            fh.write("{not json")
        g = self.gate(total_rps=2.0, burst=3)
        t0 = self.clock.t
        g.acquire()                                           # corrupt -> empty bucket (conservative)
        self.assertAlmostEqual(self.clock.t - t0, 0.5, places=6)
        self.clock.t -= 3600                                  # wall clock jumps back
        g.acquire()
        self.assertLess(self.clock.t - (t0 - 3600), 2.0)
        with open(self.path, encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertEqual(set(st), {"v", "tokens", "last", "pause_until"})

    def test_damaged_state_file_is_conservative_and_keeps_known_pause(self):
        g = self.gate(total_rps=2.0, burst=3)
        t0 = self.clock.t
        g.pause(300)
        with open(self.path, "r+b") as fh:          # crash-truncated: existing but EMPTY file
            fh.truncate(0)
        other = self.gate(total_rps=2.0, burst=3)   # a process that never saw the pause
        other.acquire()
        self.assertAlmostEqual(self.clock.t - t0, 0.5, places=6)   # empty bucket, not a free burst of 3
        g.acquire()                                  # the instance that set the pause still honours it
        self.assertGreaterEqual(self.clock.t - t0, 300.0)
        self.assertGreaterEqual(other.state()["pause_until"], t0 + 300.0)   # and wrote it back

    def test_stale_tail_after_crash_between_write_and_truncate(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        now = self.clock.t
        head = json.dumps({"v": 1, "tokens": 3, "last": now, "pause_until": now + 50}, sort_keys=True)
        with open(self.path, "w") as fh:            # new JSON + the tail of a longer old one
            fh.write(head + '0.123456, "pause_until": 1.0}')
        g = self.gate(total_rps=2.0, burst=3)
        g.acquire()
        self.assertGreaterEqual(self.clock.t - now, 50.0)          # the pause survived
        with open(self.path, encoding="utf-8") as fh:
            json.load(fh)                                           # rewritten cleanly (no tail)

    def test_state_file_private_and_default_path(self):
        self.gate().acquire()
        self.assertEqual(os.stat(self.path).st_mode & 0o077, 0)
        self.assertTrue(rate_gate.DEFAULT_PATH.endswith(os.path.join("state", "rate_gate.json")))
        self.assertEqual(rate_gate.DEFAULT_PATH, os.path.join(os.path.dirname(TOOLS), "state", "rate_gate.json"))
        self.assertEqual(rate_gate.DEFAULT_TOTAL_RPS, 2.5)
        with self.assertRaises(ValueError):
            RateGate(self.path, total_rps=0)


class RateGateTwoProcessTests(unittest.TestCase):
    """Two real processes share one state file: the combined rate must hold."""

    CHILD = r"""
import json, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from rate_gate import RateGate
g = RateGate(sys.argv[2], total_rps=float(sys.argv[3]), burst=1)
start, n = float(sys.argv[4]), int(sys.argv[5])
while time.time() < start:
    time.sleep(0.002)
out = []
for _ in range(n):
    g.acquire()
    out.append(time.time())
print(json.dumps(out))
"""

    def test_combined_rate_across_two_processes(self):
        tmp = tempfile.mkdtemp(prefix="a1mp_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "rate_gate.json")
        rate, n = 10.0, 8
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-I", "-B", "-c", self.CHILD, TOOLS, path, str(rate),
                                   repr(start), str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        outs = []
        for p in procs:
            so, se = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, se.decode()[-500:])
            outs.append(json.loads(so))
        ts = sorted(outs[0] + outs[1])
        self.assertEqual([len(o) for o in outs], [n, n])
        # 16 tokens, burst 1, 10/s -> at least 1.5 s overall (small scheduling tolerance); each
        # process alone would finish in 0.7 s, so this span proves the bucket was shared
        self.assertGreaterEqual(ts[-1] - ts[0], (2 * n - 1) / rate - 0.08)
        for i in range(len(ts) - 5):                     # no window ever exceeds the combined rate
            self.assertGreaterEqual(ts[i + 5] - ts[i], (5 - 1) / rate - 0.05)
        # NOTE: no per-process span/fairness assertion -- flock is not fair, so one process may
        # legitimately take most tokens first (that assertion made this test flaky).

    def test_pause_set_by_other_process(self):
        tmp = tempfile.mkdtemp(prefix="a1mp_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "rate_gate.json")
        code = ("import sys; sys.dont_write_bytecode=True; sys.path.insert(0, sys.argv[1]);"
                "from rate_gate import RateGate; RateGate(sys.argv[2]).pause(1.0)")
        subprocess.run([sys.executable, "-I", "-B", "-c", code, TOOLS, path], check=True, timeout=30)
        t0 = time.time()
        RateGate(path).acquire()
        self.assertGreaterEqual(time.time() - t0, 0.8)


# ====================================================================== fake server fixture
class FakeCase(unittest.TestCase):
    TOKENS = {
        "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "ntn_RO_SECRET_0001"},
        "wb": {"caps": {"read", "update", "insert"}, "roots": {"*"}, "secret": "ntn_WB_SECRET_0002"},
    }

    def setUp(self):
        self.fake = FakeNotion(tokens=self.TOKENS)
        self.base = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.mkdtemp(prefix="a1fake_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.alice = self.fake.add_user("Alice")
        self.fake.add_user("Bob")
        self.root = self.fake.add_page("Root")
        self.page = self.fake.add_page("Page", parent=self.root)
        self.b = self.fake.add_blocks(self.page, [para("one"), para("two"), para("three")])
        self.db, self.ds = self.fake.add_database(self.page, "Tasks", {
            "Name": "title", "Tag": {"type": "select", "options": ["a", "b"]}, "Rel": "relation",
            "Done": "checkbox"})
        self.row = self.fake.add_row(self.ds, "Row 1", {"Tag": {"select": {"name": "a"}},
                                                         "Rel": {"relation": [{"id": self.root}] * 30}})
        self.tpl = self.fake.add_template(self.ds, "Tpl", is_default=True, children=[para("from template")])
        self.fake.clear_requests()

    def gate(self, name="g.json", **kw):
        return RateGate(os.path.join(self.tmp, name), total_rps=kw.pop("total_rps", 200), burst=kw.pop("burst", 20),
                        **kw)

    def ro(self, **kw):
        kw.setdefault("gate", self.gate())
        kw.setdefault("sleep", lambda s: None)
        return make_read_client(base_url=self.base, token="ntn_RO_SECRET_0001", rate=kw.pop("rate", 200), **kw)

    def wb(self, **kw):
        kw.setdefault("gate", self.gate())
        kw.setdefault("timeout", 5)
        return WritebackClient("ntn_WB_SECRET_0002", base_url=self.base, **kw)


# ====================================================================== fake server itself
class FakeNotionTests(FakeCase):
    def test_shapes_and_normalisation(self):
        b = self.fake.block(self.b[0])
        rt = b["paragraph"]["rich_text"][0]
        self.assertEqual(rt["annotations"], fake_notion.ANN_DEFAULT)
        self.assertEqual((rt["plain_text"], rt["href"]), ("one", None))
        self.assertEqual(b["parent"], {"type": "page_id", "page_id": self.page})
        self.assertTrue(b["last_edited_time"].endswith(":00.000Z"))
        self.assertFalse(b["in_trash"])
        self.assertNotIn("archived", b)
        row = self.fake.page(self.row)
        self.assertEqual(row["parent"], {"type": "data_source_id", "data_source_id": self.ds, "database_id": self.db})
        self.assertEqual(row["properties"]["Name"]["id"], "title")
        kinds = [c["type"] for c in self.fake.children(self.page)]
        self.assertEqual(kinds, ["paragraph", "paragraph", "paragraph", "child_database"])
        self.assertEqual([c["type"] for c in self.fake.children(self.root)], ["child_page"])

    def test_auth_version_capability_access(self):
        import requests
        s = requests.Session()
        self.addCleanup(s.close)
        url = f"{self.base}/pages/{self.page}"
        r = s.get(url, headers={"Authorization": "Bearer nope", "Notion-Version": "2026-03-11"})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "unauthorized"))
        r = s.get(url, headers={"Authorization": "Bearer ntn_RO_SECRET_0001"})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "missing_version"))
        h = {"Authorization": "Bearer ntn_RO_SECRET_0001", "Notion-Version": "2026-03-11"}
        r = s.patch(url, headers=h, json={"in_trash": True})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "restricted_resource"))
        self.fake.add_token("narrow", caps={"read"}, roots={self.page})
        hn = {"Authorization": "Bearer narrow", "Notion-Version": "2026-03-11"}
        self.assertEqual(s.get(f"{self.base}/blocks/{self.b[1]}", headers=hn).status_code, 200)   # inherited
        self.assertEqual(s.get(f"{self.base}/pages/{self.row}", headers=hn).status_code, 200)     # db row below
        r = s.get(f"{self.base}/pages/{self.root}", headers=hn)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "object_not_found"))
        r = s.post(f"{self.base}/search", headers=hn, json={})
        ids = {x["id"] for x in r.json()["results"]}
        self.assertIn(self.page, ids)
        self.assertNotIn(self.root, ids)
        r = s.patch(url, headers=h, json={"archived": True})
        self.assertEqual(r.status_code, 403)               # capability is checked first
        hw = {"Authorization": "Bearer ntn_WB_SECRET_0002", "Notion-Version": "2026-03-11"}
        r = s.patch(url, headers=hw, json={"archived": True})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "validation_error"))
        r = s.get(f"{self.base}/nope", headers=h)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_request_url"))
        log = self.fake.requests
        self.assertTrue(all(e["token"] in (None, "ro", "narrow", "wb") for e in log))

    def test_pagination_and_limits(self):
        self.fake.add_blocks(self.root, [para(f"p{i}") for i in range(150)])
        c = self.ro()
        kids = c.list_children(self.root)
        self.assertEqual(len(kids), 151)
        self.assertEqual(self.fake.count("GET", r"/children$"), 2)
        with self.assertRaises(NotionAPIError) as cm:
            c.request("GET", f"blocks/{self.root}/children", params={"page_size": 101})
        self.assertEqual(cm.exception.status, 400)

    def test_block_limit_mode(self):
        self.fake.set_block_limit(2)
        w = self.wb()
        r = w.append_children(self.page, [para("x"), para("y"), para("z")])
        self.assertEqual((r.outcome, r.code), ("rejected", "block_limit"))
        self.assertEqual(len(self.fake.child_ids(self.page)), 4)      # nothing created
        r = w.append_children(self.page, [para("x"), para("y")])
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(self.fake.block_limit_remaining, 0)

    def test_rich_text_merged_like_live_notion(self):
        """Contract §7.2 / probe P3: adjacent text segments with identical annotations+link merge on
        write, with no length limit; the 2000-char limit applies to request items only."""
        w = self.wb()

        def seg(t, link=None, **ann):
            s = {"type": "text", "text": {"content": t}, "annotations": ann}
            if link:
                s["text"]["link"] = {"url": link}
            return s

        def p(*segs):
            return {"type": "paragraph", "paragraph": {"rich_text": list(segs)}}

        mention = {"type": "mention", "mention": {"type": "page", "page": {"id": self.root}}}
        r = w.append_children(self.page, [
            p(seg("same "), seg("same"), seg(" ann", bold=True), seg("ann2", bold=True)),
            p(seg("A" * 2000), seg("B" * 500)),
            p(seg("edge ", bold=True), seg(" space")),
            p(seg("x", "https://e.example/a"), seg("y", "https://e.example/a"), seg("z", "https://e.example/b"),
              seg("w")),
            p(seg("m"), mention, mention, seg("n")),
        ])
        self.assertEqual(r.outcome, "ok")
        rb = [b["paragraph"]["rich_text"] for b in self.ro().list_children(self.page)[-5:]]
        self.assertEqual([x["plain_text"] for x in rb[0]], ["same same", " annann2"])
        self.assertEqual([x["annotations"]["bold"] for x in rb[0]], [False, True])
        self.assertEqual([len(x["text"]["content"]) for x in rb[1]], [2500])          # one segment > 2000
        self.assertEqual(rb[1][0]["plain_text"], "A" * 2000 + "B" * 500)
        self.assertEqual([x["plain_text"] for x in rb[2]], ["edge ", " space"])        # edge spaces kept
        self.assertEqual([(x["plain_text"], x["href"]) for x in rb[3]],
                         [("xy", "https://e.example/a"), ("z", "https://e.example/b"), ("w", None)])
        self.assertEqual([x["type"] for x in rb[4]], ["text", "mention", "mention", "text"])
        r = w.append_children(self.page, [p(seg("C" * 2001))])                          # P3b
        self.assertEqual((r.outcome, r.status, r.api_code), ("rejected", 400, "validation_error"))
        big = self.fake.child_ids(self.page)[-4]                                        # the 2500-char block
        self.assertEqual(w.update_block(big, {"paragraph": {"color": "red"}}).outcome, "ok")   # stored text kept
        self.assertEqual(len(self.fake.block(big)["paragraph"]["rich_text"][0]["text"]["content"]), 2500)
        self.assertEqual(w.update_block(big, {"paragraph": {"rich_text": [seg("u"), seg("v")]}}).outcome, "ok")
        self.assertEqual([x["plain_text"] for x in self.fake.block(big)["paragraph"]["rich_text"]], ["uv"])
        w.update_page(self.row, {"properties": {"Name": {"title": [seg("t1"), seg("t2")]}}})
        self.assertEqual([x["plain_text"] for x in self.fake.page(self.row)["properties"]["Name"]["title"]],
                         ["t1t2"])
        (seeded,) = self.fake.add_blocks(self.page, [p(seg("a"), seg("b"))])           # seeding: stored as given
        self.assertEqual(len(self.fake.block(seeded)["paragraph"]["rich_text"]), 2)

    def test_child_page_reports_trashed_ancestor(self):
        """Contract §7.7 / probe P8: GET on a page below a trashed page reports in_trash true, and false
        again after the ancestor is restored; the page's own flag is not changed."""
        w = self.wb()
        child = self.fake.add_page("Child", parent=self.page)
        grand = self.fake.add_page("Grand", parent=child)
        self.assertEqual(w.update_page(self.page, {"in_trash": True}).outcome, "ok")
        self.assertTrue(w.get_page(child)["in_trash"])
        self.assertTrue(w.get_page(grand)["in_trash"])
        self.assertTrue(w.get_block(child)["in_trash"])                  # the child_page block view too
        self.assertTrue(self.fake.page(self.row)["in_trash"])            # row of a db inside the page
        self.assertFalse(self.fake.pages[child]["in_trash"])             # own flag untouched
        self.assertEqual(w.update_page(self.page, {"in_trash": False}).outcome, "ok")
        self.assertFalse(w.get_page(child)["in_trash"])
        self.assertFalse(w.get_page(grand)["in_trash"])
        self.assertFalse(w.get_block(child)["in_trash"])

    def test_templates_views_comments_properties(self):
        c = self.ro()
        res = c.request("GET", f"data_sources/{self.ds}/templates")
        self.assertEqual(res["templates"], [{"id": self.tpl, "name": "Tpl", "is_default": True}])
        self.assertEqual(len(c.list_views(self.db)), 1)
        with self.assertRaises(NotionAPIError) as cm:
            c.list_comments(self.page)
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "restricted_resource"))
        row = c.retrieve_page(self.row)
        rel = row["properties"]["Rel"]
        self.assertTrue(rel["has_more"])
        self.assertEqual(len(rel["relation"]), 25)
        full = c.retrieve_page_property(self.row, rel["id"])
        self.assertEqual(len(full["results"]), 30)
        tag = c.retrieve_page_property(self.row, row["properties"]["Tag"]["id"])
        self.assertEqual(tag["select"]["name"], "a")
        rows = c.query_data_source(self.ds)
        self.assertEqual([r["id"] for r in rows], [self.row])        # templates are not rows
        t0 = rows[0]["created_time"]
        q = c.query_data_source(self.ds, {"filter": {"timestamp": "created_time", "created_time": {"after": t0}}})
        self.assertEqual(q, [])


# ====================================================================== read-only client
class ReadOnlyClientTests(FakeCase):
    def test_reads(self):
        c = self.ro()
        self.assertIsInstance(c, ReadOnlyClient)
        self.assertEqual(c.me()["bot"]["workspace_id"], self.fake.workspace_id)
        self.assertEqual(c.retrieve_page(self.page)["id"], self.page)
        self.assertEqual([plain(b) for b in c.list_children(self.page)[:3]], ["one", "two", "three"])
        self.assertEqual(c.retrieve_database(self.db)["data_sources"][0]["id"], self.ds)
        self.assertEqual(c.retrieve_data_source(self.ds)["id"], self.ds)
        self.assertEqual(len(c.query_data_source(self.ds)), 1)
        self.assertEqual(len(c.request("POST", f"databases/{self.db}/query", body={})["results"]), 1)
        self.assertIn(self.page, {x["id"] for x in c.search("Page")})
        users = list(c.paginate("GET", "users"))
        self.assertEqual(sum(1 for u in users if u["type"] == "person"), 2)
        self.assertEqual(c.request("GET", f"{self.base}/blocks/{self.b[0]}")["id"], self.b[0])   # absolute URL
        self.assertTrue(self.fake.requests and all(e["token"] == "ro" for e in self.fake.requests))

    def test_refuses_writes_before_any_io(self):
        gate_path = os.path.join(self.tmp, "untouched.json")
        c = self.ro(gate=RateGate(gate_path))
        calls = []
        orig = c.session.request
        c.session.request = lambda *a, **k: calls.append(a) or orig(*a, **k)
        u = self.page
        bad = [
            ("PATCH", f"pages/{u}", {"body": {"in_trash": True}}),
            ("POST", "pages", {"body": {"parent": {"page_id": u}}}),
            ("POST", f"{self.base}/pages", {"body": {}}),
            ("POST", "https://api.notion.com/v1/search", {"body": {}}),           # foreign absolute URL
            ("GET", "https://evil.example/v1/users/me", {}),
            ("DELETE", f"blocks/{u}", {}),
            ("PATCH", f"blocks/{u}", {"body": {"paragraph": {}}}),
            ("PATCH", f"blocks/{u}/children", {"body": {"children": []}}),
            ("POST", f"data_sources/{self.ds}/query/../../pages", {"body": {}}),
            ("POST", f"data_sources/{self.ds}/query/x", {"body": {}}),
            ("POST", "data_sources/not-a-uuid/query", {"body": {}}),
            ("POST", f"search/../pages", {"body": {}}),
            ("POST", "comments", {"body": {}}),
            ("POST", "file_uploads", {"body": {}}),
            ("POST", "databases", {"body": {}}),
            ("PATCH", f"data_sources/{self.ds}", {"body": {}}),
            ("PUT", f"pages/{u}", {}),
            ("HEAD", f"pages/{u}", {}),
            ("GET", f"pages/{u}", {"body": {"x": 1}}),
            ("GET", f"pages/{u}/%2E%2E/x", {}),
            ("POST", "search", {"files": {"f": b"x"}}),
            ("POST", "search", {"data": {"a": "b"}}),
        ]
        for method, path, kw in bad:
            with self.subTest(method=method, path=path):
                with self.assertRaises(ReadOnlyViolation):
                    c.request(method, path, **kw)
        helpers = [
            lambda: c.create_page({"parent": {"page_id": u}}), lambda: c.update_page(u, {"in_trash": True}),
            lambda: c.append_children(u, [para("x")]), lambda: c.update_block(u, {}), lambda: c.trash_block(u),
            lambda: c.create_database({}), lambda: c.update_data_source(self.ds, {}),
            lambda: c.create_comment({}), lambda: c.upload_bytes("a.txt", b"x"),
        ]
        for fn in helpers:
            with self.assertRaises(ReadOnlyViolation):
                fn()
        self.assertEqual(calls, [])
        self.assertEqual(self.fake.requests, [])                  # the fake saw nothing at all
        self.assertFalse(os.path.exists(gate_path))               # no rate token was even taken
        self.assertEqual(c.stats["requests"], 0)

    def test_429_sets_shared_gate_pause(self):
        g = self.gate("p.json")
        c = self.ro(gate=g)
        self.fake.inject("GET", rf"^pages/{self.page}$", 429, retry_after=0.4)
        t0 = time.time()
        self.assertEqual(c.retrieve_page(self.page)["id"], self.page)
        self.assertGreaterEqual(time.time() - t0, 0.3)            # waited on the gate (client sleep is a no-op)
        self.assertEqual(g.stats["pauses_set"], 1)
        self.assertEqual(self.fake.count("GET", r"^pages/"), 2)

    def test_529_on_reads_retried_with_gate_pause(self):
        """nmig does not retry 529; reads through ReadOnlyClient (and WritebackClient reads) do, honouring
        Retry-After and setting the shared gate pause; other statuses keep nmig's policy."""
        g = self.gate("p529.json")
        c = self.ro(gate=g)
        self.fake.inject("GET", rf"^pages/{self.page}$", 529, retry_after=0.4)
        t0 = time.time()
        self.assertEqual(c.retrieve_page(self.page)["id"], self.page)
        self.assertGreaterEqual(time.time() - t0, 0.3)            # waited on the gate (client sleep is a no-op)
        self.assertEqual(g.stats["pauses_set"], 1)
        self.assertEqual(self.fake.count("GET", rf"^pages/{self.page}$"), 2)
        self.fake.inject("POST", r"^search$", 503)                # nmig policy unchanged: 503 retried on reads
        self.assertIn(self.page, {x["id"] for x in c.search("Page")})
        self.assertEqual(self.fake.count("POST", r"^search$"), 2)
        self.fake.inject("GET", rf"^pages/{self.root}$", 404)      # ...and 404 is not
        with self.assertRaises(NotionAPIError) as cm:
            c.retrieve_page(self.root)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.fake.count("GET", rf"^pages/{self.root}$"), 1)
        c2 = self.ro(max_retries=1)                                # exhausted -> NotionAPIError 529
        self.fake.inject("GET", rf"^blocks/{self.b[0]}$", 529, retry_after=0, times=None)
        with self.assertRaises(NotionAPIError) as cm:
            c2.retrieve_block(self.b[0])
        self.assertEqual(cm.exception.status, 529)
        self.assertEqual(self.fake.count("GET", rf"^blocks/{self.b[0]}$"), 2)
        self.fake.clear_faults()
        w = self.wb(sleep=lambda s: None)                          # write-back client reads too
        self.fake.inject("GET", rf"^pages/{self.row}$", 529, retry_after=0)
        self.assertEqual(w.get_page(self.row)["id"], self.row)
        self.assertEqual(self.fake.count("GET", rf"^pages/{self.row}$"), 2)
        self.assertEqual(self.fake.writes(), [])

    def test_make_read_client_loads_token_from_given_path(self):
        p = os.path.join(self.tmp, "ro.env")
        with open(p, "w") as fh:
            fh.write("NOTION_TARGET_TOKEN=ntn_RO_SECRET_0001\n")
        c = make_read_client(base_url=self.base, token_path=p, rate=100, gate=self.gate(), sleep=lambda s: None)
        self.assertEqual(c.retrieve_page(self.page)["id"], self.page)
        self.assertNotIn("SECRET", repr(c))
        self.assertEqual(c.version, "2026-03-11")

    def test_no_access_page_is_404(self):
        self.fake.add_token("narrow", caps={"read"}, roots={self.page}, secret="ntn_NARROW")
        c = make_read_client(base_url=self.base, token="ntn_NARROW", rate=100, sleep=lambda s: None)
        with self.assertRaises(NotionAPIError) as cm:
            c.retrieve_page(self.root)
        self.assertEqual(cm.exception.status, 404)


# ====================================================================== write-back client
class WritebackClientTests(FakeCase):
    def test_update_block_ok(self):
        w = self.wb()
        r = w.update_block(self.b[1], {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "TWO"}}]}})
        self.assertIsInstance(r, WriteResult)
        self.assertEqual((r.outcome, r.status, r.op, r.attempts), ("ok", 200, "UPDATE", 1))
        self.assertTrue(r.ok)
        blk = self.fake.block(self.b[1])
        self.assertEqual(plain(blk), "TWO")
        self.assertEqual(blk["last_edited_by"]["id"], self.fake.bot_id("wb"))
        self.assertEqual(self.fake.page(self.page)["last_edited_by"]["id"], self.fake.bot_id("wb"))
        (req,) = self.fake.writes()
        self.assertEqual(req["version"], "2026-03-11")
        self.assertNotIn("notifications", req["body"])           # Update a block has no such parameter

    def test_append_positions_and_silent(self):
        w = self.wb()
        r = w.append_children(self.page, [para("after-one")],
                              position={"type": "after_block", "after_block": {"id": self.b[0]}})
        self.assertEqual(r.outcome, "ok")
        r2 = w.append_children(self.page, [para("first"), para("second")], position={"type": "start"})
        r3 = w.append_children(self.page, [para("last")])
        kids = self.fake.children(self.page)
        self.assertEqual([plain(k) for k in kids if k["type"] == "paragraph"],
                         ["first", "second", "one", "after-one", "two", "three", "last"])
        self.assertEqual(r2.committed_ids, [kids[0]["id"], kids[1]["id"]])
        self.assertEqual(r3.committed_ids, [kids[-1]["id"]])
        bodies = [e["body"] for e in self.fake.writes()]
        self.assertTrue(all(b["notifications"] == {"mode": "silent"} for b in bodies))
        self.assertEqual(bodies[1]["position"], {"type": "start"})

    def test_silent_false_omits_notifications(self):
        w = self.wb(silent=False)
        w.append_children(self.page, [para("x")])
        w.update_page(self.page, {"properties": {"title": {"title": [{"text": {"content": "P2"}}]}}})
        self.assertTrue(all("notifications" not in e["body"] for e in self.fake.writes()))

    def test_create_page_and_row_update_page(self):
        w = self.wb()
        r = w.create_page({"parent": {"page_id": self.page},
                           "properties": {"title": {"title": [{"text": {"content": "New"}}]}},
                           "children": [para("body")]})
        self.assertEqual((r.outcome, r.op), ("ok", "CREATE_PAGE"))
        (pid,) = r.committed_ids
        self.assertEqual(self.fake.child_ids(self.page)[-1], pid)
        self.assertEqual(self.fake.page(pid)["created_by"]["id"], self.fake.bot_id("wb"))
        rr = w.create_page({"parent": {"type": "data_source_id", "data_source_id": self.ds},
                            "properties": {"Name": {"title": [{"text": {"content": "R2"}}]},
                                           "Tag": {"select": {"name": "b"}}}})
        self.assertEqual(rr.outcome, "ok")
        self.assertIn(rr.committed_ids[0], [x["id"] for x in self.fake.rows(self.ds)])
        up = w.update_page(self.row, {"properties": {"Done": {"checkbox": True}}})
        self.assertEqual(up.outcome, "ok")
        self.assertTrue(self.fake.page(self.row)["properties"]["Done"]["checkbox"])
        tr = w.update_page(pid, {"in_trash": True})
        self.assertEqual(tr.outcome, "ok")
        self.assertNotIn(pid, self.fake.child_ids(self.page))
        self.assertTrue(self.fake.page(pid)["in_trash"])
        back = w.update_page(pid, {"in_trash": False})
        self.assertEqual(back.outcome, "ok")
        self.assertIn(pid, self.fake.child_ids(self.page))

    def test_timeout_after_commit_is_unknown_and_never_resent(self):
        w = self.wb(timeout=0.5)
        self.fake.inject("PATCH", r"/children$", None, delay=2.0, after_commit=True)
        r = w.append_children(self.page, [para("once")])
        self.assertEqual((r.outcome, r.code, r.status), ("unknown", "read_timeout", None))
        self.assertEqual(self.fake.count("PATCH", r"/children$"), 1)
        self.assertEqual([plain(k) for k in self.fake.children(self.page)].count("once"), 1)
        self.fake.inject("POST", r"^pages$", None, delay=2.0, after_commit=True)
        r = w.create_page({"parent": {"page_id": self.page}, "properties": {"title": {"title": []}}})
        self.assertEqual(r.outcome, "unknown")
        self.assertEqual(self.fake.count("POST", r"^pages$"), 1)

    def test_dropped_connection_is_unknown(self):
        w = self.wb()
        self.fake.inject("PATCH", r"/children$", drop=True, after_commit=True)
        r = w.append_children(self.page, [para("dropped")])
        self.assertEqual((r.outcome, r.code), ("unknown", "connection_lost"))
        self.assertEqual(self.fake.count("PATCH", r"/children$"), 1)

    def test_connection_refused_is_not_sent(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        w = WritebackClient("ntn_WB_SECRET_0002", base_url=f"http://127.0.0.1:{port}/v1", timeout=1)
        r = w.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.code), ("not_sent", "connect_error"))

    def test_5xx_and_409_never_resent(self):
        w = self.wb()
        for st, outcome in ((500, "unknown"), (502, "unknown"), (504, "unknown"), (503, "unknown"),
                            (409, "rejected")):
            with self.subTest(status=st):
                self.fake.clear_requests()
                self.fake.inject("PATCH", r"/children$", st)
                r = w.append_children(self.page, [para(f"s{st}")])
                self.assertEqual((r.outcome, r.status, r.attempts), (outcome, st, 1))
                self.assertEqual(self.fake.count("PATCH"), 1)
                self.fake.inject("POST", r"^pages$", st)
                r = w.create_page({"parent": {"page_id": self.page}})
                self.assertEqual((r.outcome, r.attempts), (outcome, 1))
                self.assertEqual(self.fake.count("POST"), 1)
        self.assertEqual(len(self.fake.child_ids(self.page)), 4)   # nothing was ever applied

    def test_503_committed_ids(self):
        w = self.wb()
        before = set(self.fake.child_ids(self.page))
        self.fake.inject("PATCH", r"/children$", 503, after_commit=True)          # retry_guidance nesting
        r = w.append_children(self.page, [para("c1"), para("c2")])
        new = [i for i in self.fake.child_ids(self.page) if i not in before]
        self.assertEqual((r.outcome, r.status, r.code, r.verify_required), ("ok", 503, "committed", True))
        self.assertEqual(r.committed_ids, new)
        self.assertIsNone(r.data)
        self.assertEqual(self.fake.count("PATCH"), 1)
        self.fake.inject("POST", r"^pages$", 503, after_commit=True, guidance="flat")   # flat nesting
        r = w.create_page({"parent": {"page_id": self.page}})
        self.assertEqual((r.outcome, r.code), ("ok", "committed"))
        self.assertEqual(r.committed_ids, [self.fake.child_ids(self.page)[-1]])
        self.fake.inject("POST", r"^pages$", 503, after_commit=True, guidance=None)     # no guidance
        r = w.create_page({"parent": {"page_id": self.page}})
        self.assertEqual((r.outcome, r.committed_ids), ("unknown", []))
        self.fake.inject("PATCH", r"/children$", 503, after_commit=True,                # garbage guidance
                         body=lambda ctx: {"object": "error", "status": 503, "code": "service_unavailable",
                                           "message": "x", "additional_data": {
                                               "retry_guidance": {"committed_child_ids": ["nope", None]}}})
        r = w.append_children(self.page, [para("g")])
        self.assertEqual((r.outcome, r.committed_ids), ("unknown", []))
        self.fake.inject("PATCH", r"/children$", 503, after_commit=True,                # partial
                         body=lambda ctx: {"code": "service_unavailable", "message": "x", "additional_data": {
                             "retry_guidance": {"committed_child_ids": ctx["committed_ids"][:1]}}})
        r = w.append_children(self.page, [para("p1"), para("p2")])
        self.assertEqual((r.outcome, r.code, len(r.committed_ids)), ("unknown", "partially_committed", 1))
        self.assertEqual(self.fake.count("PATCH"), 3)
        self.assertEqual(self.fake.count("POST"), 2)

    def test_429_retry_after_honoured_with_gate_pause(self):
        pauses, sends = [], []
        g = self.gate("w.json")
        w = self.wb(gate=g, on_pause=lambda s, why: pauses.append((s, why)),
                    on_send=lambda m, rel, n: sends.append((m, rel, n)))
        self.fake.inject("PATCH", rf"^blocks/{self.b[0]}$", 429, retry_after=1)
        r = w.update_block(self.b[0], {"paragraph": {"rich_text": []}})
        self.assertEqual((r.outcome, r.attempts), ("ok", 2))
        self.assertGreaterEqual(r.paused_s, 1.0)
        log = [e for e in self.fake.requests if e["method"] == "PATCH"]
        self.assertEqual([e["status"] for e in log], [429, 200])
        self.assertGreaterEqual(log[1]["t"] - log[0]["t"], 0.95)
        self.assertEqual(pauses[0][0], 1.0)
        self.assertEqual(g.stats["pauses_set"], 1)
        self.assertEqual([n for _, _, n in sends], [1, 2])

    def test_529_retried_but_blocked_429_not(self):
        w = self.wb(sleep=lambda s: None)
        self.fake.inject("PATCH", r"/children$", 529, retry_after=0)
        r = w.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.attempts), ("ok", 2))
        self.fake.clear_requests()
        self.fake.inject("PATCH", r"/children$", 429, retry_after=0, rate_limit_reason="public_api_request_blocked")
        r = w.append_children(self.page, [para("y")])
        self.assertEqual((r.outcome, r.code, r.attempts), ("rejected", "rate_limited", 1))
        self.assertEqual(self.fake.count("PATCH"), 1)

    def test_429_exhausted_or_too_long(self):
        w = WritebackClient("ntn_WB_SECRET_0002", base_url=self.base, sleep=lambda s: None, max_429_retries=2,
                            rate=1000)
        self.fake.inject("PATCH", r"/children$", 429, retry_after=0, times=None)
        r = w.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.attempts, r.retry_after), ("rejected", 3, 0.0))
        self.fake.clear_faults()
        self.fake.clear_requests()
        self.fake.inject("PATCH", r"/children$", 429, retry_after=500)
        r = w.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.attempts, r.retry_after), ("rejected", 1, 500.0))

    def test_block_limit_vs_permission_403(self):
        w = self.wb()
        self.fake.set_block_limit(0)
        r = w.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.status, r.code, r.api_code), ("rejected", 403, "block_limit",
                                                                     "restricted_resource"))
        r = w.create_page({"parent": {"page_id": self.page}})
        self.assertEqual(r.code, "block_limit")
        self.fake.set_block_limit(None)
        self.fake.inject("PATCH", r"/children$", 403, block_limit=True)
        self.assertEqual(w.append_children(self.page, [para("x")]).code, "block_limit")
        ro_as_wb = WritebackClient("ntn_RO_SECRET_0001", base_url=self.base, rate=1000)   # no insert capability
        r = ro_as_wb.append_children(self.page, [para("x")])
        self.assertEqual((r.outcome, r.status, r.code), ("rejected", 403, "restricted_resource"))

    def test_notifications_rejected_is_surfaced_not_retried(self):
        self.fake.reject_notifications = True
        w = self.wb()
        r = w.create_page({"parent": {"page_id": self.page}})
        self.assertEqual((r.outcome, r.code, r.api_code), ("rejected", "notifications_rejected", "validation_error"))
        self.assertIn("send_silent_notifications", r.message)
        self.assertEqual(self.fake.count("POST"), 1)

    def test_policy_refusals_before_io(self):
        w = self.wb()
        u, b = self.page, self.b[0]
        cases = [
            lambda: w.trash_block(b),                                            # allow_block_trash false
            lambda: w.update_page(u, {"is_locked": True}),
            lambda: w.update_page(u, {"erase_content": True}),
            lambda: w.update_page(u, {"archived": True}),
            lambda: w.update_page(u, {"icon": None}),
            lambda: w.update_page(u, {"in_trash": "yes"}),
            lambda: w.update_block(b, {"archived": True}),
            lambda: w.update_block(b, {"child_page": {"title": "x"}}),
            lambda: w.update_block(b, {"synced_block": {}}),
            lambda: w.update_block(b, {"paragraph": {}, "quote": {}}),
            lambda: w.update_block(b, {"paragraph": {"children": []}}),
            lambda: w.update_block("not-an-id", {"paragraph": {}}),
            lambda: w.append_children(u, [para(str(i)) for i in range(101)]),
            lambda: w.append_children(u, []),
            lambda: w.append_children(u, [{"type": "child_page", "child_page": {"title": "x"}}]),
            lambda: w.append_children(u, [{"type": "column_list", "column_list": {}}]),
            lambda: w.append_children(u, [{"type": "toggle", "toggle": {"rich_text": [], "children": [
                {"type": "table_of_contents", "table_of_contents": {}}]}}]),
            lambda: w.append_children(u, [para("x")], position={"type": "after_block"}),
            lambda: w.append_children(u, [para("x")], position={"type": "end"}),
            lambda: w.append_children(u, [para("x")], position="start"),
            lambda: w.create_page({"parent": {"workspace": True}}),
            lambda: w.create_page({"parent": {"database_id": self.db}}),
            lambda: w.create_page({"parent": {"page_id": u}, "template": {"type": "default"}}),
            lambda: w.create_page({"parent": {"page_id": u}, "children": [{"type": "synced_block"}]}),
        ]
        for n, fn in enumerate(cases):
            with self.subTest(case=n):
                with self.assertRaises(WriteForbidden):
                    fn()
        self.assertEqual(self.fake.requests, [])

    def test_trash_block_when_allowed(self):
        w = self.wb(allow_block_trash=True)
        r = w.trash_block(self.b[2])
        self.assertEqual((r.outcome, r.op), ("ok", "TRASH_BLOCK"))
        self.assertNotIn(self.b[2], self.fake.child_ids(self.page))
        self.assertTrue(self.fake.block(self.b[2])["in_trash"])
        r = w.update_block(self.b[2], {"in_trash": False})                    # restore via update_block
        self.assertEqual(r.outcome, "ok")
        self.assertIn(self.b[2], self.fake.child_ids(self.page))

    def test_update_block_never_trashes(self):
        """update_block(.., {"in_trash": true}) would bypass allow_block_trash (and, on a child_page id,
        trash a page subtree): refused before any I/O whatever allow_block_trash says."""
        sub = self.fake.add_page("Sub", parent=self.page)
        self.fake.add_blocks(sub, [para("sub content")])
        self.fake.clear_requests()
        for allow in (False, True):
            w = self.wb(allow_block_trash=allow)
            for bid, body in ((self.b[1], {"in_trash": True}),
                              (self.b[1], {"paragraph": {"rich_text": []}, "in_trash": True}),
                              (sub, {"in_trash": True})):
                with self.subTest(allow=allow, body=body, child_page=bid == sub):
                    with self.assertRaises(WriteForbidden):
                        w.update_block(bid, body)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(self.fake.block(self.b[1])["in_trash"])
        self.assertFalse(self.fake.page(sub)["in_trash"])

    def test_restore_block_and_position_not_kept(self):
        """Contract §7.6: a restored block does not come back at its original position (fake default:
        end of the parent's children); restore_block() is the rollback path."""
        w = self.wb(allow_block_trash=True)

        def order():
            return [plain(k) for k in self.fake.children(self.page) if k["type"] == "paragraph"]

        self.assertEqual(order(), ["one", "two", "three"])
        self.assertEqual(w.trash_block(self.b[0]).outcome, "ok")
        self.assertEqual(order(), ["two", "three"])
        self.assertTrue(w.get_block(self.b[0])["in_trash"])                  # GET still returns it
        r = w.restore_block(self.b[0])
        self.assertEqual((r.outcome, r.op, r.status), ("ok", "RESTORE_BLOCK", 200))
        self.assertEqual(r.data["in_trash"], False)
        self.assertEqual(order(), ["two", "three", "one"])                    # NOT where it was
        self.assertEqual(self.fake.writes()[-1]["body"], {"in_trash": False})
        self.fake.restore_position = "original"                               # opt-in old behaviour
        w.trash_block(self.b[2])
        w.restore_block(self.b[2])
        self.assertEqual(order(), ["two", "three", "one"])
        with self.assertRaises(WriteForbidden):
            w.restore_block("nope")

    def test_503_guidance_must_match_the_operation(self):
        """A 503 becomes 'ok' only with the guidance shape the operation can commit; anything else is
        'unknown' with no trusted ids. Never resent."""
        w = self.wb()

        def err(**guidance):
            def body(ctx):
                g = {k: (v(ctx) if callable(v) else v) for k, v in guidance.items()}
                return {"object": "error", "status": 503, "code": "service_unavailable", "message": "x",
                        "additional_data": {"retry_guidance": g}}
            return body

        newpage = lambda ctx: ctx["committed_ids"][0]                          # noqa: E731
        create = {"parent": {"page_id": self.page}, "properties": {"title": {"title": []}},
                  "children": [para("pc")]}
        cases = [
            ("POST", r"^pages$", err(committed_child_ids=lambda c: self.fake.child_ids(c["committed_ids"][0])),
             lambda: w.create_page(create), ("unknown", "guidance_mismatch")),
            ("POST", r"^pages$", err(committed_resource_id=self.page),
             lambda: w.create_page(create), ("unknown", "guidance_mismatch")),
            ("POST", r"^pages$", err(committed_resource_id=newpage, committed_child_ids=[self.b[0]]),
             lambda: w.create_page(create), ("unknown", "guidance_mismatch")),
            ("PATCH", r"/children$", err(committed_resource_id=self.page),
             lambda: w.append_children(self.page, [para("y")]), ("unknown", "guidance_mismatch")),
            ("PATCH", r"/children$", err(committed_child_ids=[self.page]),
             lambda: w.append_children(self.page, [para("y")]), ("unknown", "guidance_mismatch")),
            ("PATCH", r"/children$", err(committed_child_ids=lambda c: c["committed_ids"] + [self.b[0]]),
             lambda: w.append_children(self.page, [para("y")]), ("unknown", "guidance_mismatch")),
            ("PATCH", r"/children$", err(committed_child_ids=[self.b[0]]),
             lambda: w.append_children(self.page, [para("y")],
                                       position={"type": "after_block", "after_block": {"id": self.b[0]}}),
             ("unknown", "guidance_mismatch")),
            ("PATCH", rf"^blocks/{self.b[1]}$", err(committed_resource_id=self.b[2]),
             lambda: w.update_block(self.b[1], {"paragraph": {"rich_text": []}}), ("unknown", "guidance_mismatch")),
            ("PATCH", rf"^blocks/{self.b[1]}$", err(committed_child_ids=[self.b[1]]),
             lambda: w.update_block(self.b[1], {"paragraph": {"rich_text": []}}), ("unknown", "guidance_mismatch")),
            ("PATCH", rf"^blocks/{self.b[1]}$", err(committed_resource_id=self.b[1]),
             lambda: w.update_block(self.b[1], {"paragraph": {"rich_text": []}}), ("ok", "committed")),
            ("PATCH", rf"^pages/{self.row}$", err(committed_resource_id=self.row),
             lambda: w.update_page(self.row, {"properties": {"Done": {"checkbox": True}}}), ("ok", "committed")),
        ]
        for n, (method, rx, body, call, expected) in enumerate(cases):
            with self.subTest(case=n):
                self.fake.clear_requests()
                self.fake.inject(method, rx, 503, after_commit=True, body=body)
                r = call()
                self.assertEqual((r.outcome, r.code, r.status), expected + (503,))
                self.assertEqual(self.fake.count(method), 1)                   # never resent
                if r.outcome == "ok":
                    self.assertTrue(r.verify_required)
                    self.assertEqual(len(r.committed_ids), 1)
                    self.assertIsNone(r.data)
                else:
                    self.assertEqual(r.committed_ids, [])
                    self.assertFalse(r.verify_required)
        # the two nestings disagree -> unparseable
        self.fake.inject("POST", r"^pages$", 503, after_commit=True, body=lambda ctx: {
            "code": "service_unavailable", "message": "x", "additional_data": {
                "committed_resource_id": ctx["committed_ids"][0],
                "retry_guidance": {"committed_resource_id": self.row}}})
        r = w.create_page(create)
        self.assertEqual((r.outcome, r.code, r.committed_ids), ("unknown", "guidance_unparseable", []))

    def test_2xx_that_does_not_identify_created_ids_is_unknown(self):
        w = self.wb()
        extra = self.fake.block(self.b[0])
        self.fake.inject("PATCH", r"/children$", 200, after_commit=True,       # results include an OLD block
                         body=lambda ctx: dict(ctx["result"], results=ctx["result"]["results"] + [extra]))
        r = w.append_children(self.page, [para("new1")])
        self.assertEqual((r.outcome, r.code, r.status, r.committed_ids), ("unknown", "ids_ambiguous", 200, []))
        self.assertIsNotNone(r.data)
        self.assertIn("new1", [plain(k) for k in self.fake.children(self.page) if k["type"] == "paragraph"])
        self.fake.inject("POST", r"^pages$", 200, after_commit=True,           # id == the parent
                         body=lambda ctx: dict(ctx["result"], id=self.page))
        r = w.create_page({"parent": {"page_id": self.page}})
        self.assertEqual((r.outcome, r.code, r.committed_ids), ("unknown", "ids_ambiguous", []))
        self.assertEqual(self.fake.count("PATCH") + self.fake.count("POST"), 2)

    def test_reads(self):
        self.fake.set_caps("wb", {"read", "update", "insert", "user_info"})
        w = self.wb()
        self.assertEqual(w.get_page(self.page)["id"], self.page)
        self.assertEqual(w.get_block(self.b[0])["id"], self.b[0])
        self.assertEqual(len(w.list_children_all(self.page)), 4)
        rel = w.get_page(self.row)["properties"]["Rel"]["id"]
        self.assertEqual(len(w.get_property_all(self.row, rel)["results"]), 30)
        self.assertIn(self.page, {x["id"] for x in w.search_all()})
        self.assertEqual(sum(1 for u in w.users_list_all() if u["type"] == "person"), 2)
        self.assertEqual(self.fake.writes(), [])

    def test_hooks_and_token_never_leaks(self):
        secret = "ntn_WB_SECRET_0002"
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        lg_names = ("writeback_client", "nmig.client", "")
        saved = {n: logging.getLogger(n).level for n in lg_names}
        for n in lg_names:
            logging.getLogger(n).setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        try:
            responses = []
            w = self.wb(on_response=lambda st, rid: responses.append((st, rid)), sleep=lambda s: None)
            results = [w.update_block(self.b[0], {"paragraph": {"rich_text": []}})]
            self.fake.inject("PATCH", r"/children$", 500)
            results.append(w.append_children(self.page, [para("x")]))
            self.fake.inject("POST", r"^pages$", 429, retry_after=0)
            results.append(w.create_page({"parent": {"page_id": self.page}}))
            try:
                w.get_page(self.root.replace("-", "")[:-1] + "0")
            except NotionAPIError:
                pass
        finally:
            logging.getLogger().removeHandler(handler)
            for n, lvl in saved.items():
                logging.getLogger(n).setLevel(lvl)
        text = buf.getvalue() + repr(w) + str(w) + "".join(repr(r) for r in results)
        self.assertTrue(buf.getvalue())                       # logging did happen
        self.assertNotIn(secret, text)
        self.assertNotIn("SECRET", text)
        self.assertEqual([s for s, _ in responses], [200, 500, 429, 200])
        self.assertTrue(all(rid for _, rid in responses))


# ====================================================================== wiring in snapshot / assets_fetch
class WiringTests(unittest.TestCase):
    def _tree(self, name):
        with open(os.path.join(TOOLS, name), encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_no_plain_notionclient_and_gate_used(self):
        for name in ("snapshot.py", "assets_fetch.py"):
            with self.subTest(file=name):
                tree = self._tree(name)
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
                fnames = [getattr(c.func, "id", getattr(c.func, "attr", None)) for c in calls]
                self.assertNotIn("NotionClient", fnames)
                mk = [c for c in calls if getattr(c.func, "id", None) == "make_read_client"]
                self.assertEqual(len(mk), 1)
                kws = {k.arg: k.value for k in mk[0].keywords}
                self.assertEqual(getattr(kws["gate"].func, "id", None), "RateGate")
                self.assertEqual(ast.unparse(kws["rate"]), "a.rate")
                src = ast.unparse(tree)
                self.assertIn("RATE_GATE_TOTAL_RPS = 2.5", src)
                self.assertIn("os.path.join(WORK, 'state', 'rate_gate.json')", src)
                imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
                self.assertNotIn("NotionClient", imported)
                self.assertNotIn("writeback_client", src)
                self.assertNotIn("writeback.env", src)

    def test_cli_flags_unchanged(self):
        for name, flags in (("snapshot.py", ["--roots", "--db", "--workers", "--rate", "--no-comments",
                                             "--incremental-from", "--add-templates"]),
                            ("assets_fetch.py", ["--db", "--rate"])):
            out = subprocess.run([sys.executable, "-I", "-B", os.path.join(TOOLS, name), "--help"],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            for f in flags:
                self.assertIn(f, out.stdout)


class SnapshotCrawlAgainstFakeTests(FakeCase):
    """snapshot.py's crawler (BlobInventory + templates) runs through the ReadOnlyClient against the fake."""

    def test_inventory_crawl(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("snapshot_under_test", os.path.join(TOOLS, "snapshot.py"))
        snap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(snap)
        self.fake.add_blocks(self.b[0], [para("nested")])
        client = self.ro()
        roots = snap.detect_kinds(client, [{"id": self.root, "title": "Root"}])
        self.assertEqual(roots[0]["kind"], "page")
        store = snap.Store(os.path.join(self.tmp, "snap.db"))
        inv = snap.BlobInventory(client, store, workers=2, hash_files=False, comments=True)
        inv.seed(roots)
        inv.run()
        s = snap.summary(store)
        self.assertEqual(s["errors"], [])
        self.assertEqual(s["objects"], {"page": 4, "database": 1, "data_source": 1})   # root, page, row, template
        # Root: child_page; Page: 3 paragraphs + nested paragraph + child_database; template: 1 paragraph
        self.assertEqual(s["blocks"], 7)
        self.assertEqual(s["work"], {"done": 5})            # 2 pages, 1 database, 1 row, 1 template
        self.assertEqual(self.fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
