#!/usr/bin/env python3
"""Read-only capture of the Notion web sidebar (teamspace order + top-level page order).

Uses opencli's Chrome bridge in its OWN background window/session (default nmig_ro_sidebar), opens a page of
the Godcreate workspace and reads the sidebar DOM. The current Notion sidebar (IMPL_CONTRACT §7.11) shows the
sections 即将开始的活动 / 最近 / 团队协作区 / 私人; under 团队协作区 each teamspace is a `role=button` row with
`aria-expanded`, identified here by its sibling "添加到 <teamspace>" button (robust to icon-letter labels).
Live DOM (probed 2026-11-18): the teamspace header row itself is the toggle and contains the 打开 / 添加到 / settings
buttons; roots are read from the row's exclusive container, or, if the children render flat in the 团队协作区 section,
in document order between this row and the next one (never outside the section).
A collapsed teamspace renders no treeitems, so for each teamspace in the required order whose roots are not
rendered, this script expands that teamspace's toggle (the only click it ever makes), waits, reads the
treeitems inside the teamspace's own container (depth 0 = roots, in rendered order) and then clicks the toggle
again to restore the user's collapsed state. No other clicks, no typing, no edits.

The toggle is clicked only when it is a role=button with aria-expanded, without aria-haspopup, labelled with the
teamspace name or icon letter, and sits in the teamspace's own header row (the smallest ancestor that also holds
"添加到 <name>"; that row holds no page treeitem and no other teamspace's row). One plain click() only (no
synthetic event burst). After an expand click the script checks that the toggle now reports
aria-expanded="true" AND that root treeitems appeared in that teamspace's own container; otherwise it aborts:
it clicks nothing more (only the same, verified toggle again if it did flip to expanded, to restore the user's
state), skips every later teamspace and exits 2.

Writes <out-dir>/sidebar_<TS>.raw.json + sidebar_<TS>.json (format unchanged; extra keys only) and prints the
json path. Exit 2 when the bridge is unavailable, the observed teamspace order differs from the required order,
a teamspace yields no root or the capture aborted (the caller then keeps the previous evidence and reports it).

  python3 -I capture_sidebar.py [--out-dir DIR] [--session NAME] [--start-url URL]
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = ["General", "Quantitative Investing", "x_to_notion", "Godcreate Business", "Godcreate AI工作空间"]
SESSION = "nmig_ro_sidebar"
START_PAGE = "https://app.notion.com/p/04c0728e322e411f8fb0cf4ee5f9f5df"  # General / Team Home
ADD_PREFIXES = ["添加到 ", "Add a page to ", "Add page to ", "Add to "]
OUT_OF_SCOPE_SKIP = ("最近", "Recents", "收藏", "Favorites", "即将开始的活动", "Upcoming")
METHOD = ("opencli browser eval: read-only DOM read of the Notion web sidebar (role=treeitem), background window; "
          "collapsed teamspaces expanded via their own toggle and collapsed again")

# Shared JS: the "添加到 <name>" element -> the teamspace's expand toggle. The toggle is a role=button with
# aria-expanded whose label is the teamspace name or its icon letter (contract §7.11). Menu buttons
# (aria-haspopup, e.g. "团队协作区设置与成员...", which also carries aria-expanded), "打开" and the add button
# itself are never chosen; the search never climbs into a container that holds another teamspace's row.
PICK_JS = r"""const ADD = %ADD%;
const SKIP = ["团队协作区设置", "Teamspace settings", "打开", "Open", "更多", "More"];
const SECTION = ["即将开始的活动","最近","团队协作区","私人","收藏","共享","Upcoming","Recents","Teamspaces","Private","Favorites","Shared"];
function labelOf(b) { return ((b.getAttribute('aria-label') || (b.innerText || '').split('\n')[0] || '') + '').trim(); }
function addName(el) { const lab = el.getAttribute('aria-label') || ''; const pre = ADD.find(p => lab.startsWith(p)); return pre ? lab.slice(pre.length).trim() : null; }
function labelFits(l, name) {
  if (!l || SKIP.some(s => l.startsWith(s)) || ADD.some(p => l.startsWith(p)) || SECTION.includes(l)) return false;
  const a = l.toLowerCase(), n = name.toLowerCase();
  return a === n || n.startsWith(a) || a.startsWith(n);
}
function pickToggle(el, name) {
  // Live Notion (probed 2026-11-18 01:15): the teamspace header row ITSELF is the toggle (role=button,
  // aria-expanded, no aria-haspopup, first text line = name or icon letter) and contains 打开 / 添加到 <name> /
  // settings. Take that ancestor when it passes every guard; otherwise fall back to the sibling search.
  const anc = el.parentElement ? el.parentElement.closest('[role=button][aria-expanded]') : null;
  if (anc && !anc.hasAttribute('aria-haspopup') && !anc.closest('[role=treeitem]') && labelFits(labelOf(anc), name)
      && ![...anc.querySelectorAll('[aria-label]')].some(x => { const n = addName(x); return n && n !== name; }))
    return anc;
  let a = el.parentElement;
  for (let k = 0; a && a !== document.body && k < 8; k++, a = a.parentElement) {
    const foreign = [...a.querySelectorAll('[aria-label]')].some(x => { const n = addName(x); return n && n !== name; });
    if (foreign) return null;
    const c = [...a.querySelectorAll('[role=button][aria-expanded]')].filter(b =>
      !b.closest('[role=treeitem]') && !b.contains(el) && !el.contains(b) && !b.hasAttribute('aria-haspopup') && labelFits(labelOf(b), name));
    if (c.length) {
      // the header row: holds the toggle and "添加到 <name>" but no page item (else we climbed into the section)
      if (a.querySelector('[role=treeitem]')) return null;
      return c[0];
    }
  }
  return null;
}
function toggleOk(b) {
  return !!b && b.getAttribute('role') === 'button' && b.hasAttribute('aria-expanded') && !b.hasAttribute('aria-haspopup');
}"""

# Scan: every teamspace row (found through its "添加到 <name>" sibling) with the treeitems inside its own
# exclusive container (an ancestor that holds treeitems but no other teamspace row), plus every other treeitem
# with the nearest preceding section label (old heuristic) for the out-of-scope list.
SCAN_JS = r"""(() => {
%PICK%
const rows = [];
for (const el of document.querySelectorAll('[aria-label]')) {
  const name = addName(el);
  if (!name || rows.some(r => r.name === name)) continue;
  const btn = pickToggle(el, name);
  rows.push({name, btn, addEl: el});
}
const btns = rows.map(r => r.btn || r.addEl);
function container(btn) {
  if (!btn) return null;
  let a = btn.parentElement;
  while (a && a !== document.body) {
    if (btns.some(b => b !== btn && a.contains(b))) return null;
    if (a.querySelector('[role=treeitem]')) return a;
    a = a.parentElement;
  }
  return null;
}
function depthIn(x, stop) { let d = 0, p = x.parentElement; while (p && p !== stop) { if (p.getAttribute && p.getAttribute('role') === 'treeitem') d++; p = p.parentElement; } return d; }
function item(x, stop) { const a = x.querySelector('a[href]') || x.closest('a[href]'); return {depth: depthIn(x, stop), text: (x.innerText || '').split('\n')[0], href: a ? a.getAttribute('href') : null}; }
// The 团队协作区 section: the smallest common ancestor of every teamspace row.
let SEC = null;
{ const bs = btns.filter(Boolean);
  if (bs.length) { SEC = bs[0].parentElement; while (SEC && !bs.every(b => SEC.contains(b))) SEC = SEC.parentElement; } }
// Fallback when a teamspace has no exclusive container (children rendered flat in the section): the treeitems
// that follow this teamspace's row and precede the next row in document order, inside the section only, never
// inside any row (so the 私人 section and other teamspaces can never be read as this teamspace's roots).
function docOrder(r) {
  if (!SEC || !r.btn) return [];
  const out = []; let cur = null;
  for (const x of SEC.querySelectorAll('[role]')) {
    const bi = btns.indexOf(x);
    if (bi >= 0) { cur = rows[bi].name; continue; }
    if (cur !== r.name || x.getAttribute('role') !== 'treeitem') continue;
    if (btns.some(b => b && b.contains(x))) continue;
    out.push(x);
  }
  return out;
}
const owned = new Set();
const teamspaces = rows.map(r => {
  const c = r.btn ? container(r.btn) : null;
  const its = c ? [...c.querySelectorAll('[role=treeitem]')] : docOrder(r);
  its.forEach(x => owned.add(x));
  return {name: r.name, label: r.btn ? labelOf(r.btn) : null, toggle_found: !!r.btn, read: c ? 'container' : 'document_order',
          expanded: r.btn ? r.btn.getAttribute('aria-expanded') : null, items: its.map(x => item(x, c || SEC))};
});
const others = [];
for (const x of document.querySelectorAll('[role=treeitem]')) {
  if (owned.has(x)) continue;
  let sec = null, q = x;
  while (q && !sec) { let s = q.previousElementSibling; while (s && !sec) { const t = (s.innerText || '').trim(); if (t && s.querySelectorAll('[role=treeitem]').length === 0 && t.length < 30) sec = t; s = s.previousElementSibling; } q = q.parentElement; if (q && q.tagName === 'NAV') break; }
  const it = item(x, null); it.section = sec; others.push(it);
}
return JSON.stringify({url: location.href, captured: new Date().toISOString(), teamspaces, others});
})()"""

# Toggle one teamspace (by its "添加到 <name>" sibling) to the wanted aria-expanded state. The only click:
# one plain click() on a verified toggle (never a synthetic event burst, never another element).
TOGGLE_JS = r"""(() => {
const NAME = %NAME%, WANT = %WANT%;
%PICK%
let btn = null;
for (const el of document.querySelectorAll('[aria-label]')) {
  if (addName(el) !== NAME) continue;
  btn = pickToggle(el, NAME);
  break;
}
if (!toggleOk(btn)) return JSON.stringify({found: false});
const before = btn.getAttribute('aria-expanded');
if (before === WANT) return JSON.stringify({found: true, clicked: false, before});
btn.click();
return JSON.stringify({found: true, clicked: true, before, after: btn.getAttribute('aria-expanded')});
})()"""


def _js(template, **kw):
    s = template.replace("%PICK%", PICK_JS).replace("%ADD%", json.dumps(ADD_PREFIXES, ensure_ascii=False))
    for k, v in kw.items():
        s = s.replace(f"%{k}%", json.dumps(v, ensure_ascii=False))
    return s


def dashed(h):
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def id_from_href(href):
    """First 32-hex id in a sidebar href (a database link may carry a later ?v=<view id>)."""
    m = re.search(r"([0-9a-f]{32})", (href or "").split("?", 1)[0]) or re.search(r"([0-9a-f]{32})", href or "")
    return dashed(m.group(1)) if m else None


def parse_capture(scan, extra=None, order=ORDER):
    """Build the evidence dict from one SCAN_JS result (and per-teamspace re-scans after expanding).

    scan: {"url", "captured", "teamspaces": [{name, label, expanded, items: [{depth, text, href}]}], "others": [...]}
    extra: {teamspace name: items} read after expanding that teamspace (overrides the scan's items).
    Returns (evidence dict without capture metadata, problems [str])."""
    extra = extra or {}
    problems = []
    seen = [t["name"] for t in scan.get("teamspaces", []) if t.get("name") in order]
    out_ts = [t["name"] for t in scan.get("teamspaces", []) if t.get("name") not in order]
    by_name = {t["name"]: t for t in scan.get("teamspaces", [])}
    roots, counts = [], {}
    for name in order:
        t = by_name.get(name)
        items = extra.get(name, t.get("items", []) if t else [])
        n = 0
        for it in items:
            if it.get("depth") != 0:
                continue
            n += 1
            roots.append({"id": id_from_href(it.get("href")), "title": it.get("text"), "teamspace": name,
                          "teamspace_order": order.index(name) + 1, "root_order": n})
        counts[name] = n
        if t is None:
            problems.append(f"teamspace {name} not found")
        elif n == 0 and t.get("toggle_found") is False:
            problems.append(f"teamspace {name}: expand toggle not identified (nothing clicked), no root rendered")
        elif n == 0:
            problems.append(f"teamspace {name}: no root rendered")
    for r in roots:
        if not r["id"]:
            problems.append(f"root without id: {r['title']!r}")
    other = []
    for it in scan.get("others", []):
        sec = (it.get("section") or "").split("\n")[-1].strip()
        if it.get("depth") == 0 and sec not in OUT_OF_SCOPE_SKIP:
            other.append({"id": id_from_href(it.get("href")), "title": it.get("text"), "section": sec})
    ok = seen == list(order) and not problems
    ev = {"teamspace_order_observed": seen, "teamspace_order_required": list(order), "order_match": ok,
          "roots": roots, "out_of_scope_top_level": other, "roots_per_teamspace": counts,
          "teamspaces_out_of_scope": out_ts}
    return ev, problems


def compare_previous(ev, prev_path):
    """Roots of the previous good evidence that this capture no longer shows (informational)."""
    if not prev_path:
        return None
    try:
        with open(prev_path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        return None
    now = {r["id"] for r in ev["roots"]}
    return {"previous": os.path.basename(prev_path),
            "dropped": [{"id": r.get("id"), "title": r.get("title"), "teamspace": r.get("teamspace")}
                        for r in prev.get("roots", []) if r.get("id") not in now],
            "added": [r["id"] for r in ev["roots"] if r["id"] not in {x.get("id") for x in prev.get("roots", [])}]}


class Bridge:
    def __init__(self, session, timeout=60):
        self.session, self.timeout = session, timeout

    def __call__(self, *args, timeout=None):
        r = subprocess.run(["opencli", "browser", self.session, *args], capture_output=True, text=True,
                           timeout=timeout or self.timeout)
        return "\n".join(ln for ln in r.stdout.splitlines()
                         if "Update available" not in ln and "npm install" not in ln).strip()

    def eval_json(self, js):
        raw = self("eval", js)
        d = json.loads(raw)
        return (json.loads(d) if isinstance(d, str) else d), raw


def capture(bridge, start_url=START_PAGE, settle=6, expand_wait=3):
    """Run the capture through `bridge` -> (scan, extra, raw_log, actions). Restores collapsed teamspaces.

    An expand click must make the toggle report aria-expanded="true" and render root treeitems inside that
    teamspace's own container; otherwise the capture aborts (an `abort` action is recorded): no later teamspace
    is touched and the only further click is the restore click on the same toggle if it did flip to expanded."""
    actions, raws = [], []
    bridge("open", start_url, "--window", "background")
    bridge("wait", "time", str(settle))
    scan, raw = bridge.eval_json(_js(SCAN_JS))
    raws.append({"step": "scan", "raw": raw})
    extra, expanded = {}, []
    try:
        for name in ORDER:
            t = next((x for x in scan.get("teamspaces", []) if x.get("name") == name), None)
            if t is None or t.get("toggle_found") is False or any(i.get("depth") == 0 for i in t.get("items", [])):
                continue
            res, raw = bridge.eval_json(_js(TOGGLE_JS, NAME=name, WANT="true"))
            actions.append({"teamspace": name, "action": "expand", "result": res})
            if not res.get("found") or not res.get("clicked"):
                continue
            expanded.append(name)
            bridge("wait", "time", str(expand_wait))
            s2, raw2 = bridge.eval_json(_js(SCAN_JS))
            raws.append({"step": f"expanded:{name}", "raw": raw2})
            t2 = next((x for x in s2.get("teamspaces", []) if x.get("name") == name), None) or {}
            flipped = t2.get("expanded") == "true"
            roots = [i for i in t2.get("items", []) if i.get("depth") == 0]
            if not (flipped and roots):
                why = ("toggle did not report aria-expanded=true after the click" if not flipped else
                       "no root treeitem appeared in the teamspace's own container after expanding")
                actions.append({"teamspace": name, "action": "abort", "reason": why})
                if not flipped:
                    expanded.remove(name)  # nothing observable changed: no further click at all
                break
            extra[name] = t2.get("items", [])
            # collapse again right away (one teamspace expanded at a time keeps the list short)
            res, _ = bridge.eval_json(_js(TOGGLE_JS, NAME=name, WANT="false"))
            actions.append({"teamspace": name, "action": "collapse", "result": res})
            expanded.remove(name)
    finally:
        for name in list(expanded):  # abort / error path: restore the user's state (same verified toggle only)
            try:
                res, _ = bridge.eval_json(_js(TOGGLE_JS, NAME=name, WANT="false"))
                actions.append({"teamspace": name, "action": "collapse(cleanup)", "result": res})
            except Exception as exc:  # noqa: BLE001
                actions.append({"teamspace": name, "action": "collapse(cleanup)", "error": type(exc).__name__})
    return scan, extra, raws, actions


def latest_good(out_dir):
    for p in sorted(glob.glob(os.path.join(out_dir, "sidebar_*T*.json")), reverse=True):
        if p.endswith(".raw.json"):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                if json.load(fh).get("order_match"):
                    return p
        except (OSError, ValueError):
            continue
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="read-only Notion sidebar capture")
    ap.add_argument("--out-dir", default=os.path.join(WORK, "evidence"))
    ap.add_argument("--session", default=SESSION)
    ap.add_argument("--start-url", default=START_PAGE)
    ap.add_argument("--timeout", type=int, default=60, help="seconds per opencli call")
    a = ap.parse_args(argv)
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M")
    prev = latest_good(a.out_dir)
    try:
        scan, extra, raws, actions = capture(Bridge(a.session, a.timeout), a.start_url)
    except Exception as e:  # noqa: BLE001
        print(f"capture failed: {type(e).__name__}", file=sys.stderr)
        return 2
    ev, problems = parse_capture(scan, extra)
    aborted = [x for x in actions if x.get("action") == "abort"]
    for x in aborted:
        problems.append(f"capture aborted at teamspace {x['teamspace']}: {x['reason']} (nothing more clicked)")
    if aborted:
        ev["order_match"] = False
    raw_doc = json.dumps({"scan": scan, "expanded": extra, "steps": [r["step"] for r in raws], "actions": actions},
                         ensure_ascii=False)
    os.makedirs(a.out_dir, exist_ok=True)
    rawp = os.path.join(a.out_dir, f"sidebar_{ts}.raw.json")
    with open(rawp, "w", encoding="utf-8") as fh:
        fh.write(raw_doc)
    out = {"captured_at": scan.get("captured"), "capture_local": ts, "method": METHOD}
    out.update({k: ev[k] for k in ("teamspace_order_observed", "teamspace_order_required", "order_match", "roots",
                                   "out_of_scope_top_level")})
    out.update({"evidence_sha256": hashlib.sha256(raw_doc.encode()).hexdigest(),
                "roots_per_teamspace": ev["roots_per_teamspace"], "teamspaces_out_of_scope": ev["teamspaces_out_of_scope"],
                "expanded_by_capture": [x["teamspace"] for x in actions if x["action"] == "expand"
                                        and (x.get("result") or {}).get("clicked")],
                "restored_collapsed": all((x.get("result") or {}).get("found") for x in actions
                                          if x["action"].startswith("collapse")),
                "aborted": [{"teamspace": x["teamspace"], "reason": x["reason"]} for x in aborted],
                "problems": problems, "compared_with_previous": compare_previous(ev, prev)})
    p = os.path.join(a.out_dir, f"sidebar_{ts}.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(p)
    return 0 if out["order_match"] and out["roots"] else 2


if __name__ == "__main__":
    sys.exit(main())
