"""custom-sort 3.2.0 sorting-spec generator (syntax verified in ../docs/custom-sort.md, 2026-11-17).

Rules used: spec lives in a note named `sortspec.md` (frontmatter key `sorting-spec`, literal block);
`target-folder: <path from vault root>` ('/' = root); folders listed as `/folders <name>`, files as
`/:files. <name>.<ext>` (match with extension, so a folder note and its folder never collide);
`%` = position of unlisted items; `_attachments` after `%` so it sorts last.  Names are
case-sensitive and not normalised, so the exporter writes NFC names; '...' and leading '%' are
removed from names by sanitize().
"""

TASK_DIR = "Notion_Obsidian"
# WRITEBACK_SPEC 5.1a / R-122: sections for the tool folder and its generated-record folders, newest first.
# `> a-z` = folder-level order-desc alphabetical (custom-sort 3.2.0 main.js: '>' is order-desc, 'a-z' desc = reverse
# alphabetical; text search only), so run ids (yyyymmddThhmm-…) and plan ids list the latest on top. These change
# sortspec.md bytes: the exporter passes task_sections=True only past the byte-frozen renderer 1.2.1
# (export_obsidian.renderer_bytes_frozen). verify_vault C05 accepts them (test_generated_files_and_new_sections_pass).
TASK_SECTIONS = (
    (f"target-folder: {TASK_DIR}", "/:files. README.md", "/:files. Godcreate 目录索引.md", "/:files. 映射清单.md",
     "/:files. sortspec.md", "/folders 运行记录", "/folders 写回预览", "/folders conflicts", "%"),
    (f"target-folder: {TASK_DIR}/运行记录", "> a-z", "%"),
    (f"target-folder: {TASK_DIR}/写回预览", "> a-z", "%"),
)


def build_sortspec(ex, task_sections=False):
    sections = []
    tpl = getattr(ex, "tpl_folder_owner", {})
    # Match the concrete .md entries for redirected objects. Same-folder renames keep the Notion slot;
    # cross-folder moves list the retained file only in the folder where it actually lives.
    redirected = {ex.nodes[oid]["note"]: rel for oid, rel in getattr(ex, "link_targets", {}).items()
                  if oid in ex.nodes and ex.nodes[oid]["note"] != rel}
    incoming = {}
    for note, target in redirected.items():
        parent, _, name = target.rpartition("/")
        if parent != note.rpartition("/")[0]:
            incoming.setdefault(parent, []).append(name)
    object_folders = (set(ex.folder_owner) | set(tpl) | set(incoming)) - set(ex.ts_roots) - {""}
    folders = ["", *ex.ts_roots, *sorted(object_folders, key=lambda f: (f.count("/"), f))]
    for folder in folders:
        lines = [f"target-folder: {folder or '/'}"]
        listed_files = set()
        for name, is_folder, ext in ex.folder_children(folder):
            if is_folder:
                lines.append(f"/folders {name}")
                continue
            filename = f"{name}.{ext or 'md'}"
            target = redirected.get(f"{folder}/{filename}" if folder else filename)
            if target:
                parent, _, filename = target.rpartition("/")
                if parent != folder:
                    continue
            if filename not in listed_files:
                listed_files.add(filename)
                lines.append(f"/:files. {filename}")
        for name in sorted(set(incoming.get(folder, ())) - listed_files):
            lines.append(f"/:files. {name}")
        lines.append("%")
        if folder in ex.ts_roots:
            lines.append("/folders _attachments")
        owner = ex.folder_owner.get(folder)
        if owner and ex.nodes[owner].get("templates"):
            lines.append("/folders _templates")
        sections.append(lines)
    if task_sections:
        sections += [list(sec) for sec in TASK_SECTIONS]
    out = ["---", "notion_kind: generated", "generated_by: export_obsidian.py", "sorting-spec: |",
           "  // generated from Notion order (sidebar evidence + block/query order); do not edit by hand"]
    for i, sec in enumerate(sections):
        if i:
            out.append("")
        out += ["  " + ln for ln in sec]
    out += ["---", "", "按 Notion 顺序排列文件列表的 custom-sort 配置（插件 3.2.0）。由导出器生成，请勿手改。", ""]
    return "\n".join(out)
