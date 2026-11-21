#!/usr/bin/env python3
"""Isolated publish + idempotency + rollback rehearsal (never touches the formal vault).

1. copy the formal vault (read-only) to WORK/rehearsal/<run_id>/before and .../vault
2. copy the state DB; publish staging/<run_id> into the rehearsal vault (--allow-other-vault)
3. publish again: must be all UNCHANGED (idempotent publish)
4. rollback the first publish; compare the rehearsal vault with `before` (paths + sha256)
Writes WORK/reports/<run_id>/rollback_rehearsal.json.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(WORK, "tools")
FORMAL = "/data120/Obsidian/Godcreate"


def tree(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def run(args):
    r = subprocess.run([sys.executable, "-I", "-B"] + args, capture_output=True, text=True, cwd=T)  # §1.7: no __pycache__
    return r.returncode, (r.stdout + r.stderr)[-4000:]


def main(run_id, extra=None, state_src=None):
    base = os.path.join(WORK, "rehearsal", run_id)
    if os.path.exists(base):
        sys.exit(f"ABORT rehearsal dir exists: {base}")
    before, vault = os.path.join(base, "before"), os.path.join(base, "vault")
    formal_before = tree(FORMAL)
    shutil.copytree(FORMAL, before, symlinks=True)
    shutil.copytree(FORMAL, vault, symlinks=True)
    state = os.path.join(base, "export.db")
    shutil.copy2(state_src or os.path.join(WORK, "state", "export.db"), state)
    appr = os.path.join(WORK, "approvals", "rehearsal.approval.md")
    if not os.path.exists(appr):
        open(appr, "w", encoding="utf-8").write("# 演练授权\n\n仅用于隔离副本（WORK/rehearsal/），不触及正式库。\n")
    rid = run_id
    rep = os.path.join(base, "reports")
    os.makedirs(rep, exist_ok=True)
    common = ["publish_vault.py", "--run-id", rid, "--vault", vault, "--state", state, "--allow-other-vault", "--report-dir", rep]
    if extra:
        common += ["--extra", extra]
    res = {"run_id": run_id, "rehearsal_dir": base}
    rc1, o1 = run(common + ["--apply", "--approval", appr])
    res["publish_1"] = {"rc": rc1, "tail": o1[-1500:]}
    if rc1 != 0:
        sys.exit(f"ABORT publish_1 failed (rc={rc1}):\n{o1[-2000:]}")
    after1 = tree(vault)
    # second publish must not write anything (journal kept separately)
    os.replace(os.path.join(rep, "publish_journal.jsonl"), os.path.join(base, "publish_journal_1.jsonl"))
    rc2, o2 = run(common)
    plan2 = json.load(open(os.path.join(rep, "publish_plan.json"), encoding="utf-8"))
    res["publish_2_plan_counts"] = plan2["counts"]
    res["idempotent_publish"] = set(plan2["counts"]) <= {"UNCHANGED"}
    # rollback first publish
    rc3, o3 = run(["rollback.py", "--run-id", rid, "--vault", vault, "--allow-other-vault", "--apply",
                   "--journal", os.path.join(base, "publish_journal_1.jsonl"), "--quarantine", os.path.join(base, "quarantine"),
                   "--log-dir", rep])
    res["rollback"] = {"rc": rc3, "tail": o3[-800:]}
    after_rb = tree(vault)
    b = tree(before)
    res["files_before"] = len(b)
    res["files_after_publish"] = len(after1)
    res["files_after_rollback"] = len(after_rb)
    res["rollback_identical_to_before"] = after_rb == b
    res["diff_examples"] = sorted(set(after_rb) ^ set(b))[:10] + [k for k in b if k in after_rb and after_rb[k] != b[k]][:10]
    res["empty_dirs_left"] = sum(1 for d, ds, fs in os.walk(vault) if not ds and not fs and d != vault)
    # the formal vault is open in Obsidian (workspace.json etc. change on their own): compare content outside .obsidian/
    strip = lambda t: {k: v for k, v in t.items() if not k.startswith(".obsidian/")}
    res["formal_vault_unchanged_during_rehearsal(excluding .obsidian/)"] = strip(tree(FORMAL)) == strip(formal_before)
    os.makedirs(os.path.join(WORK, "reports", rid), exist_ok=True)
    out = os.path.join(WORK, "reports", rid, "rollback_rehearsal.json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k not in ("publish_1", "rollback")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--extra", default=None)
    ap.add_argument("--state", default=None, help="state DB to copy (default WORK/state/export.db)")
    a = ap.parse_args()
    main(a.run_id, a.extra, a.state)
