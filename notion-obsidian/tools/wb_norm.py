"""Canonical JSON normalisation shared by the exporter (snapshot JSON) and write-back (live JSON).

Decision D-10 (speccheck.md §3, adopted by IMPL_CONTRACT §2):

* ``json_norm(obj)`` drops, at any depth,
    - keys starting with ``_`` (snapshot injections: ``_completed_via_property_item``, ``_row_ids``, ``_views`` ...),
    - ``request_id``,
    - ``created_*`` and ``last_edited_*`` keys,
  and additionally
    - for ``file`` objects (any dict with ``"type": "file"``): ``file.url`` and ``file.expiry_time`` (signed, rotating),
    - for mention segments (``"type": "mention"``): ``plain_text`` and ``href``,
    - ``has_more`` anywhere inside page/data-source ``properties`` (properties are completed before hashing,
      see ``nmig.rest_inventory._complete_properties``).
  Clarification (not a change of the rule): the *keys of a ``properties`` mapping are property names*, i.e. user
  data, not API fields.  A property named e.g. ``created_by_me`` or ``_draft`` is therefore kept (its value is
  normalised with the rules above); dropping it would hide real remote edits from the three-way comparison.
* canonical dump: ``json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))``.
* ``block_json_sha(block)`` = sha256(canonical(json_norm(block))).
* ``page_json_sha(page_obj, blocks_dfs)`` = sha256 of canonical
  ``{"page": {properties, icon, cover, is_locked, in_trash}, "blocks": [json_norm(b) for b in blocks_dfs]}``
  where ``blocks_dfs`` is the page's block tree in DFS order by (parent, ord) and **children of any
  synced_block are excluded** (snapshot references have no child rows; see D-10).  ``page_json_sha`` removes
  synced_block descendants itself, so callers may pass either the filtered or the unfiltered DFS list.

Use the SAME functions on snapshot JSON (exporter) and on live API reads (planner / apply precheck).
Pure functions, no I/O, no third-party imports.
"""
import hashlib
import json

__all__ = ["json_norm", "canon_json", "canon_block", "block_json_sha", "page_json_sha", "page_json_sha_canon",
           "page_blocks_dfs", "PAGE_KEYS"]

PAGE_KEYS = ("properties", "icon", "cover", "is_locked", "in_trash")
_FILE_VOLATILE = ("url", "expiry_time")
_DROP_PREFIXES = ("_", "created_", "last_edited_")
# blocks whose children are NOT part of the page's own block tree
_NO_DESCEND = ("child_page", "child_database", "synced_block")


def _norm(obj, in_props):
    t = type(obj)
    if t is list:
        return [_norm(x, in_props) for x in obj]
    if t is not dict:
        if isinstance(obj, (list, tuple)):
            return [_norm(x, in_props) for x in obj]
        if isinstance(obj, dict):
            obj = dict(obj)
        else:
            return obj
    typ = obj.get("type")
    mention = typ == "mention"
    filey = typ == "file"
    out = {}
    for k, v in obj.items():
        if type(k) is str:
            if k.startswith(_DROP_PREFIXES) or k == "request_id":
                continue
            if in_props and k == "has_more":
                continue
            if mention and (k == "plain_text" or k == "href"):
                continue
            if k == "properties" and type(v) is dict:
                # keys are property NAMES (data): keep them all; normalise the values
                out[k] = {name: _norm(pv, True) for name, pv in v.items()}
                continue
            if filey and k == "file" and type(v) is dict:
                v = {fk: fv for fk, fv in v.items() if fk not in _FILE_VOLATILE}
        out[k] = _norm(v, in_props)
    return out


def json_norm(obj):
    """Return a normalised deep copy of a Notion API object (block, page, rich text, ...)."""
    return _norm(obj, False)


def canon_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canon_block(block):
    """canonical(json_norm(block)): the string both block_json_sha and page_json_sha hash."""
    return canon_json(json_norm(block))


def block_json_sha(block):
    """sha256 of the canonical normalised block JSON (block_lines.json_sha)."""
    return _sha(canon_block(block))


def _without_synced_children(blocks_dfs):
    out = []
    synced, skipped = set(), set()
    for b in blocks_dfs or []:
        bid = b.get("id")
        par = b.get("parent") or {}
        pid = par.get("block_id") if par.get("type") == "block_id" else None
        if pid is not None and (pid in synced or pid in skipped):
            if bid is not None:
                skipped.add(bid)
            continue
        out.append(b)
        if b.get("type") == "synced_block" and bid is not None:
            synced.add(bid)
    return out


def page_json_sha(page_obj, blocks_dfs):
    """sha256 of the canonical page document (D-10).  ``page_obj`` is the page (or database) API object.
    Equal to sha256(canon_json({"page": json_norm({k: page_obj.get(k) for k in PAGE_KEYS}),
                                "blocks": [json_norm(b) for b in blocks_dfs without synced children]}))."""
    return page_json_sha_canon(page_obj, [canon_block(b) for b in _without_synced_children(blocks_dfs)])


def page_json_sha_canon(page_obj, canon_blocks):
    """page_json_sha from already canonicalised blocks (``canon_block`` strings, synced children already
    excluded).  Composes the canonical dump directly: sorted keys put "blocks" before "page"."""
    page_obj = page_obj or {}
    page = canon_json(json_norm({k: page_obj.get(k) for k in PAGE_KEYS}))
    return _sha('{"blocks":[' + ",".join(canon_blocks) + '],"page":' + page + "}")


def page_blocks_dfs(children_of, page_id):
    """DFS (pre-order, children in API/`ord` order) of a page's own blocks.

    ``children_of(parent_id) -> list[block_json] | None``.  Descends into every block with ``has_children``
    except child_page / child_database (separate notes) and synced_block (excluded by D-10)."""
    out = []
    stack = [iter(children_of(page_id) or [])]
    while stack:
        b = next(stack[-1], None)
        if b is None:
            stack.pop()
            continue
        out.append(b)
        if b.get("has_children") and b.get("type") not in _NO_DESCEND:
            stack.append(iter(children_of(b["id"]) or []))
    return out
