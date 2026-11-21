#!/usr/bin/env python3
"""Write the whitelisted .obsidian files for custom-sort into <out>/obsidian_extra.json for publish_vault.py --extra."""
import hashlib
import json
import os
import sys

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VER = "3.2.0"
EXPECT = {"main.js": "089315129555cde9e9085063b6a8553b4179e3a42a5a8dd1aa79cf79fb147b01",
          "manifest.json": "efeefc37bbe132ff03c2093f1b05db6817b9fb4599b36241a9f1f33c0338ebdb"}


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


VAULT = "/data120/Obsidian/Godcreate"


def main(out):
    src = os.path.join(WORK, "plugins", "custom-sort", VER)
    gen = os.path.join(WORK, "plugins", "custom-sort", "generated")
    os.makedirs(gen, exist_ok=True)
    extra = {}
    for f, h in EXPECT.items():
        p = os.path.join(src, f)
        if sha(p) != h:
            sys.exit(f"ABORT plugin file hash mismatch: {f}")
        extra[f".obsidian/plugins/custom-sort/{f}"] = {"src": p, "sha256": h, "kind": "obsidian-config"}
    # recommended settings verified in docs/custom-sort.md §6 (suspended=false => applies on start)
    data = {"additionalSortspecFile": "", "indexNoteNameForFolderNotes": "", "suspended": False,
            "statusBarEntryEnabled": True, "notificationsEnabled": True, "mobileNotificationsEnabled": False,
            "customSortContextSubmenu": True, "automaticBookmarksIntegration": False, "bookmarksContextMenus": False,
            "bookmarksGroupToConsumeAsOrderingReference": "sortspec", "delayForInitialApplication": 5000}
    dp = os.path.join(gen, "data.json")
    open(dp, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
    cp = os.path.join(gen, "community-plugins.json")
    open(cp, "w", encoding="utf-8").write(json.dumps(["custom-sort"], indent=2) + "\n")
    extra[".obsidian/plugins/custom-sort/data.json"] = {"src": dp, "sha256": sha(dp), "kind": "obsidian-config"}
    extra[".obsidian/community-plugins.json"] = {"src": cp, "sha256": sha(cp), "kind": "obsidian-config"}
    # files the user already installed via the community store are never rewritten
    # (store copies append "/* nosourcemap */" to main.js; community-plugins.json is user-owned)
    skipped = {}
    for rel in list(extra):
        dst = os.path.join(VAULT, rel)
        if os.path.exists(dst):
            skipped[rel] = {"exists": True, "sha256": sha(dst)}
            del extra[rel]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(extra, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(skipped, open(out.replace(".json", ".skipped.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"extra": list(extra), "skipped_existing": list(skipped)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
