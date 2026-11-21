#!/usr/bin/env python3
"""Tests for tools/wb_scan.py (IMPL_CONTRACT §5.11; WRITEBACK_SPEC §4.0, §6.0, §7.4; speccheck 2.F R-060..R-068).

Fixtures are small vaults + export.db (+ writeback.db) built in tempfile.mkdtemp(): rename, move, new, modified,
missing, duplicate notion_id, Obsidian Sync conflicted copy, generated file changed, broken frontmatter, plus the
read-only guarantee (no write anywhere, nothing created) and a read-only smoke scan of the sandbox vault copy.

Run: cd /data120/Obsidian/notion-obsidian-dev && python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_scan.py'
"""
import ast
import builtins
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import wb_scan  # noqa: E402
import wb_state  # noqa: E402

SANDBOX = os.path.dirname(TOOLS)


def uid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


P_HOME, P_CHILD, P_OTHER, P_SUB, P_LEAF, P_ROOT2, DB1, ROW1, TPL1 = (uid(i) for i in range(1, 10))
PNG = b"\x89PNG\r\n\x1a\nscan-fixture"
ATT = "General/_attachments/pic__" + hashlib.sha256(PNG).hexdigest()[:8] + ".png"


def note(nid, kind, title, body="Body text.\n", extra=""):
    return (f'---\nnotion_id: "{nid}"\nnotion_kind: {kind}\nnotion_title: "{title}"\n{extra}---\n\n{body}')


FILES = {
    "General/Home/Home.md": (P_HOME, "page", note(P_HOME, "page", "Home", "[[General/Home/Child|Child]]\n")),
    "General/Home/Child.md": (P_CHILD, "page", note(P_CHILD, "page", "Child")),
    "General/Home/Other.md": (P_OTHER, "page", note(P_OTHER, "page", "Other")),
    "General/Home/Sub/Sub.md": (P_SUB, "page", note(P_SUB, "page", "Sub")),
    "General/Home/Sub/Leaf.md": (P_LEAF, "page", note(P_LEAF, "page", "Leaf")),
    "General/DB/DB.md": (DB1, "database", note(DB1, "database", "DB")),
    "General/DB/DB.base": (DB1, "base", "filters: x\n"),
    "General/DB/Row.md": (ROW1, "row", note(ROW1, "row", "Row")),
    "General/DB/_templates/T.md": (TPL1, "template", note(TPL1, "template", "T")),
    "x_to_notion/Root2.md": (P_ROOT2, "page", note(P_ROOT2, "page", "Root2")),
    ATT: (None, "attachment", PNG),
    "Notion_Obsidian/README.md": (None, "generated", "# README\n"),
    "Notion_Obsidian/sortspec.md": (None, "generated", "---\nsorting-spec: |\n  x\n---\n"),
    "Notion_Obsidian/运行记录/R0.md": (None, "runlog", "---\nnotion_kind: generated\n---\n\nrun\n"),
}
PARENTS = {P_HOME: None, P_CHILD: P_HOME, P_OTHER: P_HOME, P_SUB: P_HOME, P_LEAF: P_SUB, DB1: P_HOME, ROW1: DB1,
           TPL1: DB1, P_ROOT2: None}


def b(x):
    return x if isinstance(x, bytes) else x.encode("utf-8")


class ScanFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_scan_test_")
        self.vault = os.path.join(self.tmp, "vault")
        for rel, (_nid, _kind, data) in FILES.items():
            self.put(rel, data)
        self.put(".obsidian/plugins/custom-sort/data.json", "{}\n")
        self.export_db = os.path.join(self.tmp, "export.db")
        con = sqlite3.connect(self.export_db)
        con.executescript("""
        CREATE TABLE objects_map(notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, name TEXT, parent_id TEXT,
          teamspace TEXT, ord INT, title TEXT, rendered_sha256 TEXT, published_sha256 TEXT, published_path TEXT,
          published_run TEXT, status TEXT, first_run TEXT, last_run TEXT);
        CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT, run_id TEXT,
          status TEXT);
        """)
        for rel, (nid, kind, data) in FILES.items():
            con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                        (rel, hashlib.sha256(b(data)).hexdigest(), nid, kind, "R0", "active"))
            if nid and kind != "base":
                con.execute("INSERT INTO objects_map(notion_id, kind, rel_path, parent_id, title, status) "
                            "VALUES(?,?,?,?,?,?)", (nid, kind, rel, PARENTS.get(nid), os.path.basename(rel)[:-3], "active"))
        con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                    (".obsidian/plugins/custom-sort/data.json", "0" * 64, None, "obsidian-config", "R0", "active"))
        con.commit()
        con.close()
        self.wb_db = os.path.join(self.tmp, "state", "writeback.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def p(self, rel):
        return os.path.join(self.vault, *rel.split("/"))

    def put(self, rel, data):
        p = self.p(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b(data))

    def mv(self, a, c):
        os.makedirs(os.path.dirname(self.p(c)), exist_ok=True)
        os.rename(self.p(a), self.p(c))

    def scan(self, wb=None):
        return wb_scan.scan(self.vault, self.export_db, wb, {"mode": "off"})

    def wbdb(self):
        return wb_state.connect(self.wb_db)


class TestClean(ScanFixture):
    def test_clean_vault_has_no_findings(self):
        r = self.scan()
        for k in ("missing", "renamed_same_folder", "moved_other_folder", "unsupported_rename", "local_new",
                  "local_modified", "sync_conflict_copies", "duplicates", "generated_changed", "frontmatter_errors",
                  "path_id_mismatch", "unpublished_ids"):
            self.assertEqual(r[k], [], k)
        self.assertEqual(len(r["nid_index"]), 9)               # 9 notes (the .base shares the db id, not a note)
        self.assertEqual(wb_scan.anomaly_ids(r), {})
        self.assertEqual(r["counts"]["published_active"], len(FILES) + 1)
        json.dumps(r)  # JSON-serialisable

    def test_missing_export_db_and_wb_db_are_tolerated(self):
        r = wb_scan.scan(self.vault, os.path.join(self.tmp, "nope.db"), os.path.join(self.tmp, "nope_wb.db"), None)
        self.assertEqual(r["missing"], [])
        # notes with ids are never local_new (they are unpublished ids), tool files neither; exporter-named
        # attachments are not user files either; the unregistered .base sits next to its identified database note,
        # so it is that database's file (verify_vault: database_base_file), not a user file (W4 fix r1-n2o-01)
        self.assertEqual(r["local_new"], [])
        self.assertEqual(r["moved_database_base"], ["General/DB/DB.base"])
        self.assertEqual(len(r["unpublished_ids"]), 9)
        self.assertEqual(r["unregistered_exporter_files"], [ATT])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.db")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope_wb.db")))


class TestRenameMove(ScanFixture):
    def test_same_folder_rename_is_an_override_candidate(self):
        self.mv("General/Home/Child.md", "General/Home/Child renamed.md")
        r = self.scan()
        self.assertEqual(r["renamed_same_folder"], [{"notion_id": P_CHILD, "old_rel": "General/Home/Child.md",
                                                     "new_rel": "General/Home/Child renamed.md", "kind": "page",
                                                     "content_changed": False, "override": "new"}])
        self.assertEqual(r["missing"], [])         # the object is not missing: no restore, no duplicate ADD
        self.assertEqual(r["local_new"], [])
        self.assertEqual(wb_scan.anomaly_ids(r), {})

    def test_rename_with_edit_and_existing_override(self):
        self.mv("General/Home/Child.md", "General/Home/Kid.md")
        with open(self.p("General/Home/Kid.md"), "a", encoding="utf-8") as fh:
            fh.write("local line\n")
        db = self.wbdb()
        db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, since, status) VALUES(?,?,?,?,?)",
                   (P_CHILD, "General/Home/Child.md", "General/Home/Kid.md", "t", "open"))
        db.close()
        r = self.scan(self.wb_db)
        e = r["renamed_same_folder"][0]
        self.assertTrue(e["content_changed"])
        self.assertEqual(e["override"], "existing")
        self.assertEqual(r["overrides_open"][0]["local_rel"], "General/Home/Kid.md")

    def test_move_to_other_folder_is_unsupported_and_held(self):
        self.mv("General/Home/Other.md", "x_to_notion/Other.md")
        r = self.scan()
        self.assertEqual([m["notion_id"] for m in r["moved_other_folder"]], [P_OTHER])
        self.assertEqual(r["renamed_same_folder"], [])
        self.assertEqual(r["missing"], [])
        a = wb_scan.anomaly_ids(r)
        self.assertIn(P_OTHER, a)
        self.assertIn("unsupported_rename", a[P_OTHER]["reason"])
        # paths: the absent old name and the held note itself (both accepted by verify_vault)
        self.assertEqual(sorted(a[P_OTHER]["paths"]), ["General/Home/Other.md", "x_to_notion/Other.md"])

    def test_folder_note_rename_and_folder_rename(self):
        self.mv("General/Home/Sub/Sub.md", "General/Home/Sub/Sub2.md")
        r = self.scan()
        self.assertEqual([(u["notion_id"], u["reason"]) for u in r["unsupported_rename"]], [(P_SUB, "folder_note")])
        self.assertEqual(r["renamed_same_folder"], [])
        # renaming the folder moves the folder note and its child to another folder
        self.mv("General/Home/Sub/Sub2.md", "General/Home/Sub/Sub.md")
        os.rename(self.p("General/Home/Sub"), self.p("General/Home/Sub X"))
        r = self.scan()
        self.assertEqual(sorted(m["notion_id"] for m in r["moved_other_folder"]), sorted([P_SUB, P_LEAF]))
        self.assertEqual(r["local_new"], [])  # the renamed folder holds identified notes: not a user folder

    def test_template_and_database_renames_are_unsupported(self):
        self.mv("General/DB/_templates/T.md", "General/DB/_templates/T2.md")
        self.mv("General/DB/Row.md", "General/DB/Row 2.md")
        r = self.scan()
        self.assertEqual([u["notion_id"] for u in r["unsupported_rename"]], [TPL1])
        self.assertEqual([u["notion_id"] for u in r["renamed_same_folder"]], [ROW1])  # rows may be renamed


class TestNewModifiedMissing(ScanFixture):
    def test_tool_counts_have_paths_separate_from_missing_user_notes(self):
        run = "Notion_Obsidian/运行记录/R0.md"
        user = "General/Home/Child.md"
        extra = "Notion_Obsidian/运行记录/R1-未完成.md"
        os.unlink(self.p(run))
        os.unlink(self.p(user))
        self.put(extra, "run failed")
        r = self.scan()
        self.assertEqual(r.get("missing_tool_files"), [run])
        self.assertEqual(r.get("tool_files_unregistered_rels"), [extra])
        self.assertEqual({m["rel"] for m in r["missing"]}, {run, user})
        self.assertEqual(r["counts"]["tool_files_unregistered"], 1)

    def test_new_user_files_and_folders(self):
        self.put("General/Home/Idea.md", "# my idea\n")
        self.put("Inbox/sub/pic.png", b"\x89PNGuser")
        os.makedirs(self.p("Empty dir"))
        self.put("General/Home/Notes.txt", "x")
        self.put("Notion_Obsidian/conflicts/R1/General/Home/Child (Notion 版本 R1).md",
                 "---\nnotion_kind: generated\n---\n\nx\n")
        self.put("Notion_Obsidian/my own note.md", "user note in the tool folder\n")
        self.put("General/Home/gen copy.md", "---\nnotion_kind: generated\n---\n\nx\n")
        self.put("General/_attachments/stale__0123abcd.png", b"x")
        r = self.scan()
        self.assertEqual(r["local_new"], sorted(["General/Home/Idea.md", "Inbox/sub/pic.png", "General/Home/Notes.txt",
                                                 "Inbox", "Inbox/sub", "Empty dir"]))
        self.assertEqual(r["local_new_folders"], sorted(["Inbox", "Inbox/sub", "Empty dir"]))
        self.assertEqual(r["misplaced_generated"], ["General/Home/gen copy.md"])
        self.assertEqual(r["unregistered_exporter_files"], ["General/_attachments/stale__0123abcd.png"])
        self.assertEqual(r["tool_files_unregistered"], 2)   # everything under Notion_Obsidian/ is a tool file

    def test_modified_note_and_attachment(self):
        with open(self.p("General/Home/Child.md"), "a", encoding="utf-8") as fh:
            fh.write("edited locally\n")
        self.put(ATT, b"\x89PNGchanged")
        r = self.scan()
        lm = {m["rel"]: m for m in r["local_modified"]}
        self.assertEqual(set(lm), {"General/Home/Child.md", ATT})
        self.assertEqual(lm["General/Home/Child.md"]["notion_id"], P_CHILD)
        self.assertEqual(lm["General/Home/Child.md"]["published_sha"],
                         hashlib.sha256(b(FILES["General/Home/Child.md"][2])).hexdigest())
        self.assertIsNone(lm[ATT]["notion_id"])

    def test_missing_note_attachment_and_pending_flag(self):
        os.remove(self.p("General/Home/Sub/Leaf.md"))
        os.remove(self.p("General/DB/DB.base"))
        self.mv(ATT, "General/_attachments/my picture.png")
        db = self.wbdb()
        db.execute("INSERT INTO pending_deletes(notion_id, rel_path, status) VALUES(?,?,?)",
                   (P_LEAF, "General/Home/Sub/Leaf.md", "hold"))
        db.close()
        r = self.scan(self.wb_db)
        miss = {m["rel"]: m for m in r["missing"]}
        self.assertEqual(set(miss), {"General/Home/Sub/Leaf.md", "General/DB/DB.base", ATT})
        self.assertTrue(miss["General/Home/Sub/Leaf.md"]["pending"])
        self.assertEqual(miss["General/DB/DB.base"]["kind"], "base")
        self.assertEqual(r["counts"]["missing_notes_new"], 0)
        self.assertEqual(r["renamed_attachments"], [{"old_rel": ATT, "new_rel": "General/_attachments/my picture.png",
                                                     "kind": "attachment"}])
        self.assertIn("General/_attachments/my picture.png", r["local_new"])
        # the .obsidian config row is hidden: never missing
        self.assertNotIn(".obsidian/plugins/custom-sort/data.json", miss)


class TestAnomalies(ScanFixture):
    def test_duplicate_notion_id(self):
        shutil.copyfile(self.p("General/Home/Child.md"), self.p("General/Home/Child copy.md"))
        r = self.scan()
        self.assertEqual(r["duplicates"], [{"notion_id": P_CHILD, "rels": ["General/Home/Child copy.md",
                                                                           "General/Home/Child.md"],
                                            "published_rels": ["General/Home/Child.md"]}])
        self.assertEqual(r["local_new"], [])
        self.assertEqual(r["missing"], [])
        a = wb_scan.anomaly_ids(r)
        self.assertEqual(a[P_CHILD]["reason"], "mapping_duplicate")
        self.assertEqual(sorted(a[P_CHILD]["paths"]), ["General/Home/Child copy.md", "General/Home/Child.md"])

    def test_sync_conflicted_copy(self):
        name = "General/Home/Child (Conflicted copy 2026-11-17 101010).md"
        shutil.copyfile(self.p("General/Home/Child.md"), self.p(name))
        r = self.scan()
        self.assertEqual(r["sync_conflict_copies"], [{"rel": name, "notion_id": P_CHILD,
                                                      "original_rel": "General/Home/Child.md",
                                                      "original_notion_id": P_CHILD, "frontmatter_error": None}])
        self.assertEqual(r["duplicates"], [])        # a Sync copy is not a duplicate ...
        self.assertEqual(r["local_new"], [])         # ... and not a user file
        a = wb_scan.anomaly_ids(r)
        self.assertEqual(a[P_CHILD]["reason"], "sync_conflict_copy")  # the original is held this round (WB 4.0)
        self.assertEqual(a[P_CHILD]["paths"], ["General/Home/Child.md"])

    def test_generated_file_changed(self):
        self.put("Notion_Obsidian/sortspec.md", "---\nsorting-spec: |\n  edited\n---\n")
        self.put("Notion_Obsidian/运行记录/R0.md", "---\nnotion_kind: generated\n---\n\nedited\n")
        r = self.scan()
        self.assertEqual([g["rel"] for g in r["generated_changed"]],
                         ["Notion_Obsidian/sortspec.md", "Notion_Obsidian/运行记录/R0.md"])
        self.assertEqual(r["local_modified"], [])
        self.assertEqual(r["local_new"], [])

    def test_broken_frontmatter_is_held_never_local_new(self):
        self.put("General/Home/Child.md", f'---\nnotion_id: "{P_CHILD}"\nnotion_title: "Child\n')   # unterminated
        self.put("General/Home/Other.md", '---\nnotion_id: [unclosed\n---\n\nbody\n')                 # invalid YAML
        os.remove(self.p("General/Home/Sub/Leaf.md"))
        self.put("General/Home/Sub/broken user.md", "---\n: : :\n---\n")
        r = self.scan()
        fe = {e["rel"]: e for e in r["frontmatter_errors"]}
        self.assertEqual(set(fe), {"General/Home/Child.md", "General/Home/Other.md", "General/Home/Sub/broken user.md"})
        self.assertEqual(fe["General/Home/Child.md"]["published_notion_id"], P_CHILD)
        self.assertEqual(fe["General/Home/Sub/broken user.md"]["possible_ids"], [P_LEAF])
        self.assertEqual(r["local_new"], [])
        self.assertEqual(r["missing"][0]["notion_id"], P_LEAF)
        self.assertEqual(r["local_modified"], [])
        a = wb_scan.anomaly_ids(r)
        self.assertEqual(set(a), {P_CHILD, P_OTHER, P_LEAF})
        self.assertEqual(a[P_CHILD]["paths"], [])   # a broken file is never listed as a held note's path

    def test_notion_id_removed_or_replaced(self):
        self.put("General/Home/Child.md", '---\nnotion_title: "Child"\n---\n\nbody\n')
        self.put("General/Home/Other.md", note(P_ROOT2, "page", "Root2 moved here"))
        r = self.scan()
        pm = {m["rel"]: m for m in r["path_id_mismatch"]}
        self.assertEqual(pm["General/Home/Child.md"]["reason"], "notion_id_removed")
        self.assertEqual(pm["General/Home/Other.md"]["found_notion_id"], P_ROOT2)
        a = wb_scan.anomaly_ids(r)
        self.assertTrue({P_CHILD, P_OTHER, P_ROOT2} <= set(a))
        self.assertEqual(r["local_new"], [])

    def test_unknown_notion_id_is_reported_not_local_new(self):
        self.put("General/Home/Foreign.md", note(uid(777), "page", "Foreign"))
        r = self.scan()
        self.assertEqual(r["unpublished_ids"], [{"notion_id": uid(777), "rel": "General/Home/Foreign.md",
                                                 "known_object": False}])
        self.assertEqual(r["local_new"], [])


class TestFixRound(ScanFixture):
    """Adversarial-review fix round: expired / subtree pending deletes, NFC/NFD names, note-only holds."""

    def test_expired_and_subtree_pending_rows(self):
        os.remove(self.p("General/Home/Sub/Leaf.md"))
        os.remove(self.p("General/Home/Sub/Sub.md"))
        os.remove(self.p("General/Home/Other.md"))
        db = self.wbdb()
        db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, status) VALUES(?,?,?,?)",
                   (P_SUB, "General/Home/Sub/Sub.md", json.dumps({"ids": [{"notion_id": P_SUB}, {"notion_id": P_LEAF}],
                                                                   "high_risk": True}), "hold"))
        db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, status) VALUES(?,?,?,?)",
                   (P_OTHER, "General/Home/Other.md", json.dumps({"ids": [{"notion_id": P_OTHER}]}), "expired"))
        db.close()
        r = self.scan(self.wb_db)
        miss = {m["notion_id"]: m for m in r["missing"]}
        self.assertTrue(miss[P_SUB]["pending"] and miss[P_LEAF]["pending"])     # the subtree member is pending too
        self.assertEqual((miss[P_OTHER]["pending"], miss[P_OTHER]["expired"]), (False, True))
        self.assertEqual(r["counts"]["missing_notes_new"], 0)
        self.assertEqual([p["notion_id"] for p in r["pending_expired"]], [P_OTHER])
        self.assertEqual(r["pending_deletes"][0]["subtree_ids"], [P_LEAF])
        # the 3-tuple API is unchanged
        ovr, pend, pst = wb_scan.load_wb_state(self.wb_db)
        self.assertEqual([p["notion_id"] for p in pend], [P_SUB])

    def test_nfd_name_of_a_published_nfc_path_is_reported_and_held(self):
        import unicodedata
        nfc_rel = "General/Home/Café.md"
        nid = uid(42)
        data = note(nid, "page", "Café")
        con = sqlite3.connect(self.export_db)
        con.execute("INSERT INTO published_files VALUES(?,?,?,?,?,?)",
                    (nfc_rel, hashlib.sha256(b(data)).hexdigest(), nid, "page", "R0", "active"))
        con.commit()
        con.close()
        nfd_rel = unicodedata.normalize("NFD", nfc_rel)
        self.assertNotEqual(nfd_rel, nfc_rel)
        self.put(nfd_rel, data)
        r = self.scan()
        self.assertEqual(r["normalization_mismatch"], [{"rel": nfd_rel, "published_rel": nfc_rel, "notion_id": nid,
                                                        "kind": "page"}])
        self.assertEqual(r["missing"], [])
        a = wb_scan.anomaly_ids(r)
        self.assertEqual(a[nid]["reason"], "path_normalization_mismatch")
        self.assertFalse(a[nid]["subtree"])
        self.assertEqual(sorted(a[nid]["paths"]), sorted([nfd_rel, nfc_rel]))   # own note + absent published name
        # the exact name is clean
        os.remove(self.p(nfd_rel))
        self.put(nfc_rel, data)
        self.assertEqual(self.scan()["normalization_mismatch"], [])

    def test_note_only_anomalies_hold_no_subtree(self):
        shutil.copyfile(self.p("General/Home/Home.md"), self.p("General/Home/Home (Conflicted copy 2026-11-17 1).md"))
        shutil.copyfile(self.p("General/Home/Sub/Sub.md"), self.p("General/Home/Sub/Sub copy.md"))
        a = wb_scan.anomaly_ids(self.scan())
        self.assertFalse(a[P_HOME]["subtree"])          # Sync copy: only the note (WB 4.0)
        self.assertTrue(a[P_SUB]["subtree"])            # duplicate id: the subtree (WB 14.3)
        self.put("General/Home/Sub/Sub (Conflicted copy 2026-11-17 2).md", FILES["General/Home/Sub/Sub.md"][2])
        a = wb_scan.anomaly_ids(self.scan())
        self.assertTrue(a[P_SUB]["subtree"])            # any subtree reason wins



class TestW4Robustness(ScanFixture):
    """W4 fix round 1 (r1-data-safety-04 / -05, r1-n2o-completeness-01 scope): odd vault contents never crash or
    mis-classify the scan."""

    def test_undecodable_name_is_reported_and_kept_out_of_every_list(self):
        # a GBK name extracted from a zip: os.walk returns a surrogate-escaped str
        raw = os.fsencode(self.p("General/Home")) + b"/\xc4\xe3\xba\xc3.md"
        with open(raw, "wb") as fh:
            fh.write(b"user text\n")
        os.makedirs(os.fsencode(self.p("General")) + b"/\xb2\xe2")
        with open(os.fsencode(self.p("General")) + b"/\xb2\xe2/x.md", "wb") as fh:
            fh.write(b"x\n")
        r = self.scan()
        names = sorted(u["escaped"] for u in r["undecodable_names"])
        self.assertEqual(names, ["General/Home/\\xc4\\xe3\\xba\\xc3.md", "General/\\xb2\\xe2",
                                 "General/\\xb2\\xe2/x.md"])
        for k, v in r.items():
            if k != "undecodable_names" and isinstance(v, list):
                self.assertFalse(any(wb_scan.undecodable(json.dumps(x, ensure_ascii=False)) for x in v), k)
        self.assertEqual(r["local_new"], [])
        # the raw name is a usable path and the result encodes (refresh writes it with backslashreplace)
        self.assertTrue(any(os.path.exists(os.path.join(self.vault, u["rel"])) for u in r["undecodable_names"]))
        json.dumps(r)
        (json.dumps(r, ensure_ascii=False) + "").encode("utf-8", "backslashreplace")

    def test_file_vanishing_mid_scan_rescans_once_then_fails_closed(self):
        real = wb_scan.walk_vault
        gone = {"n": 0}

        def walk_then_delete(root):
            files, dirs = real(root)
            if gone["n"] == 0 and os.path.exists(self.p("General/Home/Other.md")):
                os.remove(self.p("General/Home/Other.md"))  # Sync deletes it between the listing and the read
            gone["n"] += 1
            return files, dirs
        with mock.patch.object(wb_scan, "walk_vault", walk_then_delete):
            r = self.scan()
        self.assertEqual(gone["n"], 2)
        self.assertEqual(len(r["rescanned_after_vanished"]), 1)
        self.assertEqual([m["rel"] for m in r["missing"]], ["General/Home/Other.md"])  # the settled view

        def walk_always_stale(root):
            files, dirs = real(root)
            return files + ["General/Home/ghost.md"], dirs
        with mock.patch.object(wb_scan, "walk_vault", walk_always_stale):
            with self.assertRaises(wb_scan.ScanUnsettled) as cm:
                self.scan()
        self.assertIsInstance(cm.exception, wb_scan.wb_common.WbError)
        self.assertIn("SYNC_SETTLING", str(cm.exception))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_file_is_reported_and_its_object_held_note_only(self):
        os.chmod(self.p("General/Home/Child.md"), 0)
        self.put("General/Home/locked user note.md", "mine\n")
        os.chmod(self.p("General/Home/locked user note.md"), 0)
        try:
            r = self.scan()
        finally:
            os.chmod(self.p("General/Home/Child.md"), 0o644)
            os.chmod(self.p("General/Home/locked user note.md"), 0o644)
        self.assertEqual([(u["rel"], u["published_notion_id"]) for u in r["unreadable"]],
                         [("General/Home/Child.md", P_CHILD), ("General/Home/locked user note.md", None)])
        self.assertNotIn("General/Home/locked user note.md", r["local_new"])
        self.assertEqual(r["missing"], [])
        a = wb_scan.anomaly_ids(r)
        self.assertEqual(a[P_CHILD]["reason"], "unreadable_file")
        self.assertFalse(a[P_CHILD]["subtree"])

    def test_base_file_of_a_locally_renamed_database_folder_is_not_local_new(self):
        os.rename(self.p("General/DB"), self.p("General/DB 本地"))
        r = self.scan()
        self.assertIn(DB1, [m["notion_id"] for m in r["moved_other_folder"]])
        self.assertEqual(r["moved_database_base"], ["General/DB 本地/DB.base"])
        self.assertEqual(r["local_new"], [])   # verify_vault would reject it as database_base_file (pause loop)
        self.assertIn("General/DB/DB.base", [m["rel"] for m in r["missing"]])


class TestReadOnly(ScanFixture):
    def tree(self, root):
        out = {}
        for dp, dns, fns in os.walk(root):
            for d in dns:
                out[os.path.relpath(os.path.join(dp, d), root)] = "dir"
            for f in fns:
                full = os.path.join(dp, f)
                st = os.lstat(full)
                with open(full, "rb") as fh:
                    out[os.path.relpath(full, root)] = (hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns)
        return out

    def test_scan_never_writes(self):
        self.mv("General/Home/Child.md", "General/Home/Kid.md")
        self.put("General/Home/Idea.md", "x\n")
        db = self.wbdb()
        db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, since, status) VALUES(?,?,?,?,?)",
                   (P_OTHER, "a", "b", "t", "open"))
        db.close()
        for sfx in ("-wal", "-shm"):
            if os.path.exists(self.wb_db + sfx):
                os.unlink(self.wb_db + sfx)
        before = self.tree(self.tmp)
        real_open, real_osopen = builtins.open, os.open

        def guarded_open(file, mode="r", *a, **kw):
            if any(c in mode for c in "wax+"):
                raise AssertionError(f"write open {file} {mode}")
            return real_open(file, mode, *a, **kw)

        def guarded_osopen(path, flags, *a, **kw):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError(f"write os.open {path}")
            return real_osopen(path, flags, *a, **kw)

        with mock.patch("builtins.open", guarded_open), mock.patch("os.open", guarded_osopen), \
                mock.patch("os.makedirs", side_effect=AssertionError("makedirs")), \
                mock.patch("os.replace", side_effect=AssertionError("replace")), \
                mock.patch("os.unlink", side_effect=AssertionError("unlink")):
            r = self.scan(self.wb_db)
        self.assertEqual(len(r["renamed_same_folder"]), 1)
        after = self.tree(self.tmp)
        # only SQLite's empty WAL companions of writeback.db may appear (wb_state.connect_readonly, A2 report)
        extra = set(after) - set(before)
        self.assertTrue(extra <= {os.path.join("state", "writeback.db-wal"), os.path.join("state", "writeback.db-shm")},
                        extra)
        for k, v in before.items():
            self.assertEqual(after.get(k), v, k)

    def test_module_never_loads_tokens_or_write_clients(self):
        with open(os.path.join(TOOLS, "wb_scan.py"), encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        for bad in ("writeback_client", "notion_tokens", "readonly_client", "requests", "urllib.request"):
            self.assertNotIn(bad, names)
        for bad in ("writeback.env", "load_writeback_token", "NOTION_WRITEBACK_TOKEN"):
            self.assertNotIn(bad, src)


@unittest.skipUnless(os.path.isdir(os.path.join(SANDBOX, "vault")) and
                     os.path.isfile(os.path.join(SANDBOX, "state", "export.db")), "sandbox vault copy not present")
class TestSandboxSmoke(unittest.TestCase):
    def test_sandbox_vault_copy_scans_clean(self):
        """Read-only scan of the sandbox vault copy against the sandbox export.db (opened immutable)."""
        r = wb_scan.scan(os.path.join(SANDBOX, "vault"), os.path.join(SANDBOX, "state", "export.db"), None, {})
        self.assertGreater(r["counts"]["notes_identified"], 5000)
        self.assertEqual(wb_scan.anomaly_ids(r), {})
        self.assertEqual(r["frontmatter_errors"], [])
        self.assertEqual(r["duplicates"], [])


if __name__ == "__main__":
    unittest.main()
