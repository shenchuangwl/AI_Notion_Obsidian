"""A Notion rename or move must keep references bound to the actual local object across conflict rounds."""
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_e2e_writeback as e2e
import test_export_block_lines as export_cases
import wb_gate



def page_edit_time(w, nid):
    return datetime.fromisoformat(w.fake.page(nid)["last_edited_time"].replace("Z", "+00:00"))


def rename_at(w, nid, title, when):
    props = {"properties": {"title": {"title": [{"text": {"content": title}}]}}}
    with w.fake._lock, mock.patch.object(w.fake, "now", return_value=when):
        w.fake._patch_page(nid, props, w.fake._tok("wb"))


def check_folder_recovery(case, w, nid, rel, original_name, remote_name, seed_time, personal):
    """Exercise both edit-time outcomes and restore a later conflict from its authenticated local base."""
    note = Path(w.path(rel))
    backup = Path(w.tmp) / "outside-vault" / note.name
    backup.parent.mkdir()
    backup.write_bytes(note.read_bytes())
    case.assertFalse(backup.is_relative_to(w.vault))
    case.assertEqual(e2e.wb_common.sha256_file(backup), w.sha(rel))

    rename_at(w, nid, original_name, seed_time)
    rc, st = w.refresh("same-minute-reverted")
    case.assertEqual((rc, st["result"]), (0, "published"))
    case.assertEqual(note.read_bytes(), backup.read_bytes())

    rename_at(w, nid, remote_name, seed_time + timedelta(minutes=1))
    rename_at(w, nid, original_name, seed_time + timedelta(minutes=2))
    run = "later-minute-reverted"
    rc, st = w.refresh(run)
    case.assertEqual((rc, st["result"]), (e2e.refresh.EXIT_PAUSED, "paused_for_review"))
    case.assertTrue(any("conflict_on_page_with_children" in reason and nid in reason
                        for reason in st["pause_reasons"]))
    case.assertFalse(Path(w.P.report(run, "publish_journal.jsonl")).exists())
    case.assertEqual(note.read_bytes(), backup.read_bytes())

    # Resolve the actual stale pause and retrieve the last published bytes, by object id, path and hash.
    with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"), \
            mock.patch.object(wb_gate, "confirm_yes_no", return_value=True):
        case.assertEqual(e2e.refresh.discard_run(w.P, run), 0)
    case.assertEqual(e2e.refresh.load_paused(w.P)["runs"][run]["status"], "discarded")
    [record] = w.q("SELECT sha256, rel_path FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1", (nid,))
    case.assertEqual(record["rel_path"], rel)
    base = e2e.wb_state.base_content(record["sha256"], base_dir=Path(w.P.state) / "base")
    case.assertIsNotNone(base)
    case.assertEqual(e2e.wb_common.parse_frontmatter(e2e.wb_common.split_note(base.decode())[0])["notion_id"], nid)
    case.assertEqual(backup.read_bytes(), base + personal)
    note.write_bytes(base)
    case.assertEqual(w.sha(rel), record["sha256"])

    rc, st = w.refresh("restore-synced-base")
    case.assertEqual((rc, st["result"]), (0, "published"))
    fresh = note.read_bytes()
    case.assertNotEqual(fresh, base)
    case.assertEqual(e2e.wb_common.split_note(fresh.decode())[1], e2e.wb_common.split_note(base.decode())[1])
    # Apply only the verified personal addition to the newly published note, keeping the fresh metadata.
    note.write_bytes(fresh + personal)
    rc, st = w.refresh("personal-text-restored")
    case.assertEqual((rc, st["result"]), (0, "published"))
    case.assertEqual(note.read_bytes(), fresh + personal)
    case.assertEqual(backup.read_bytes(), base + personal)
    case.assertEqual(w.writes(), [])


class KeptObjectLinks(unittest.TestCase):
    def test_rename_keeps_identity_even_when_personal_file_occupies_target(self):
        w = e2e.World(self, "kept-id")
        title = "子页面Notion改名"
        canonical = f"General/写回测试页/{title}.md"
        kept = e2e.CHILD
        local = w.read(kept) + "\n本地追加的一行\n"
        w.write(kept, local)
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": title}}]}}},
                               w.fake._tok("wb"))
        for run in ("R2", "R3", "R4"):
            if run == "R4":
                w.write(canonical, "")
            rc, st = w.refresh(run)
            self.assertEqual(rc, 0, st)
            self.assertIn(st.get("result"), ("published", "published_with_holds"))
            self.assertEqual(w.read(kept), local)
            if run == "R4":
                self.assertEqual(w.read(canonical), "")
            else:
                self.assertFalse(Path(w.path(canonical)).exists())
                self.assertEqual(st["conflicts"][0]["dedup"], run == "R3")
            for rel, count in ((e2e.MAIN, 2), ("Notion_Obsidian/Godcreate 目录索引.md", 1)):
                with self.subTest(run=run, links_in=rel):
                    paths = re.findall(r"\[\[([^|\]]+)\|" + re.escape(title) + r"\]\]", w.read(rel))
                    self.assertEqual(len(paths), count)
                    for path in paths:
                        self.assertEqual(path + ".md", kept)
                        self.assertEqual(e2e.wb_common.parse_frontmatter(
                            e2e.wb_common.split_note(w.read(path + ".md"))[0])["notion_id"], w.child)
            with self.subTest(run=run, mapping=w.child):
                rows = [line for line in w.read("Notion_Obsidian/映射清单.md").splitlines()
                        if f"`{w.child}`" in line]
                self.assertEqual(len(rows), 1)
                self.assertIn(f"`{kept}`", rows[0])
            with self.subTest(run=run, sort="only the retained note, in sidebar order"):
                sort = w.read("Notion_Obsidian/sortspec.md").split("target-folder: General/写回测试页\n", 1)[1]
                sort = sort.split("\n\n", 1)[0]
                entries = [line.strip() for line in sort.splitlines() if line.strip().startswith("/:files.")]
                self.assertEqual(entries, ["/:files. 写回测试页.md", "/:files. 子页面.md", "/:files. 待删页面.md"])
            with self.subTest(run=run, post_verify=True):
                for name in ("link_targets_verify", "post_publish_verify"):
                    self.assertTrue(all(st.get(name, {}).get(f"C{i:02}") == "PASS" for i in range(1, 11)))
        # A second copy with the same id cannot identify a unique target for a blocked remote rename.
        w.write("General/写回测试页/重复身份.md", local)
        rc, st = w.refresh("ambiguous")
        self.assertEqual((rc, st["result"]), (0, "published_with_holds"))
        self.assertNotIn(w.child, st.get("link_targets", {}))
        self.assertEqual(w.read(canonical), "")
        self.assertEqual(w.read(kept), local)
        self.assertEqual(w.read("General/写回测试页/重复身份.md"), local)
        # No note carrying this ID remains, although both filenames are ordinary files.
        w.write("General/写回测试页/重复身份.md", "普通笔记\n")
        w.write(kept, "")
        rc, st = w.refresh("missing-id")
        # An overwritten tracked note still exists but no longer identifies itself. The normal
        # post-publish integrity checks flag that note even though redirection no longer aborts the round.
        self.assertEqual((rc, st["result"]), (e2e.refresh.EXIT_FAIL, "published_post_verify_failed"))
        self.assertEqual(set(st["post_verify_failed"]), {"C02", "C08"})
        self.assertEqual(st["post_verify_holds"], [w.child])
        self.assertFalse(st["post_verify_unlocatable"])
        self.assertFalse(Path(w.P.pause_next).exists())
        self.assertNotIn(w.child, st.get("link_targets", {}))
        self.assertEqual(w.read(canonical), "")
        self.assertEqual(w.read(kept), "")

    def test_target_loses_identity_between_scan_and_publish(self):
        w = e2e.World(self, "kept-changed-after-scan")
        w.write(e2e.CHILD, w.read(e2e.CHILD) + "\n本地修改\n")
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": "远端改名"}}]}}},
                               w.fake._tok("wb"))
        original = e2e.refresh.Round.apply
        before = w.read(e2e.MAIN)

        def replace_after_plan(rnd, *args, **kwargs):
            w.write(e2e.CHILD, "个人笔记\n")
            return original(rnd, *args, **kwargs)

        with mock.patch.object(e2e.refresh.Round, "apply", replace_after_plan):
            rc, st = w.refresh("identity-race")
        self.assertNotEqual(rc, 0)
        self.assertIn(w.child, st.get("error", ""))
        self.assertEqual(w.read(e2e.MAIN), before)
        self.assertEqual(w.read(e2e.CHILD), "个人笔记\n")
        self.assertFalse(Path(w.P.report("identity-race", "publish_journal.jsonl")).exists())

    def test_mixed_folder_layout_refuses_before_moving_the_child(self):
        w = e2e.World(self, "kept-folder")
        grand = w.fake.add_page("孙页面", parent=w.child, children=[e2e.blk("paragraph", e2e.rt("孙"))])
        rc, st = w.refresh("folder-seed")
        self.assertEqual(rc, 0, st)
        old_dir = "General/写回测试页/子页面"
        old_note, old_child = f"{old_dir}/子页面.md", f"{old_dir}/孙页面.md"
        personal = "\n个人修改\n"
        local = w.read(old_note) + personal
        seed_time = page_edit_time(w, w.child)
        before = w.read(e2e.MAIN)
        w.write(old_note, local)
        rename_at(w, w.child, "远端目录页", seed_time)
        rc, st = w.refresh("folder-rename")
        self.assertNotEqual(rc, 0)
        report = e2e.refresh.read_json(w.P.report("folder-rename", "verify_link_targets", "verify.json")) or {}
        problems = report.get("checks", {}).get("C03", {}).get("details", {}).get("issues", [])
        self.assertTrue(any(i.get("issue") == "wrong_location" and i.get("id") == grand for i in problems),
                        st.get("error"))
        self.assertEqual(w.read(e2e.MAIN), before)
        self.assertEqual(w.read(old_note), local)
        self.assertTrue(Path(w.path(old_child)).is_file())
        self.assertFalse(Path(w.P.report("folder-rename", "publish_journal.jsonl")).exists())
        self.assertIn("先备份本地修改", st.get("error", ""))
        check_folder_recovery(self, w, w.child, old_note, "子页面", "远端目录页", seed_time, personal.encode())
        self.assertTrue(Path(w.path(old_child)).is_file())

    def test_invalid_second_export_stops_before_publication(self):
        w = e2e.World(self, "kept-bad-export")
        w.write(e2e.CHILD, w.read(e2e.CHILD) + "\n本地修改\n")
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": "远端改名"}}]}}},
                               w.fake._tok("wb"))
        before = w.read(e2e.MAIN)
        original = e2e.refresh.Runner.__call__

        def corrupt_export(runner, script, args, **kwargs):
            rc = original(runner, script, args, **kwargs)
            if script == "export_obsidian.py" and "--link-targets" in args:
                out = Path(args[args.index("--out") + 1])
                index = out / "vault/Notion_Obsidian/Godcreate 目录索引.md"
                with index.open("a", encoding="utf-8") as fh:
                    fh.write("\n[[General/没有这篇笔记]]\n")
            return rc

        with mock.patch.object(e2e.refresh.Runner, "__call__", corrupt_export):
            rc, st = w.refresh("bad")
        self.assertNotEqual(rc, 0)
        report = e2e.refresh.read_json(w.P.report("bad", "verify_link_targets", "verify.json")) or {}
        self.assertEqual(report.get("status_by_check", {}).get("C06"), "FAIL", st.get("error"))
        self.assertEqual(w.read(e2e.MAIN), before)
        self.assertFalse(Path(w.P.report("bad", "publish_journal.jsonl")).exists())


class CrossFolderSortspec(export_cases._Base):
    def test_database_folder_note_and_row_use_only_their_retained_file_entries(self):
        db = "General/旧库/旧库.md"
        row = "General/原记录.md"
        out, _state, ex, _rep, _man = export_cases.export(
            self.fx, self.tmp, "kept-folders", link_targets={export_cases.DB: db, export_cases.ROW: row})
        vault = Path(out) / "vault"
        spec = (vault / "Notion_Obsidian/sortspec.md").read_text(encoding="utf-8")
        sections = [section.splitlines() for section in spec.split("  target-folder: ")[1:]]
        with self.subTest(target="one section for each actual folder"):
            self.assertEqual(len(sections), len({section[0] for section in sections}))
        by_folder = {section[0]: [line.strip() for line in section[1:]] for section in sections}
        source_folder = str(Path(ex.nodes[export_cases.DB]["note"]).parent)
        row_name = Path(ex.nodes[export_cases.ROW]["note"]).name
        with self.subTest(target="old database folder note listed once"):
            self.assertEqual(by_folder["General/旧库"].count("/:files. 旧库.md"), 1)
            self.assertNotIn("/:files. Tasks.md", by_folder[source_folder])
            self.assertIn("/:files. Tasks.base", by_folder[source_folder])
        with self.subTest(target="row in its own folder, no fictitious entry in db"):
            self.assertNotIn("/:files. " + row_name, by_folder[source_folder])
            self.assertEqual(by_folder["General"].count("/:files. 原记录.md"), 1)
        with self.subTest(target="links to database and row follow identity"):
            root = (vault / ex.nodes[export_cases.ROOT]["note"]).read_text(encoding="utf-8")
            database = (vault / ex.nodes[export_cases.DB]["note"]).read_text(encoding="utf-8")
            self.assertIn("[[" + db[:-3] + "|", root)
            self.assertIn("[[" + row[:-3] + "|", database)
            self.assertEqual(ex.link_index()[export_cases.ROW]["note"], row)

    def test_retained_note_at_vault_root_has_one_concrete_sort_entry(self):
        out, _, ex, _, _ = export_cases.export(
            self.fx, self.tmp, "root-file", link_targets={export_cases.ROW: "本地记录.md"})
        spec = (Path(out) / "vault/Notion_Obsidian/sortspec.md").read_text(encoding="utf-8")
        sections = [s.splitlines() for s in spec.split("  target-folder: ")[1:]]
        root = [s for s in sections if s[0] == "/"]
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0].count("  /:files. 本地记录.md"), 1)
        before = Path(ex.nodes[export_cases.ROW]["note"])
        folder = next(s for s in sections if s[0] == str(before.parent))
        self.assertNotIn("  /:files. " + before.name, folder)
