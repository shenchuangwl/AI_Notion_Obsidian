#!/usr/bin/env python3
"""Second-pass asset download for a finished snapshot (read-only Notion API).

1. Notion-hosted files referenced by embed / bookmark / link_preview blocks (signed S3 URLs
   that the crawler does not download) -> files row  "<block_id>:embed".
2. Retries file rows whose status != ok (signed URL expired, network error): the owning
   block / page is re-read to obtain a fresh URL, then downloaded.

Bytes go to WORK/blobs/<sha256> (tmp + fsync + os.replace).  Only GET requests.
Usage: python3 -I assets_fetch.py --db <snapshot.db> [--rate 2.7]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid

NMIG = "/data120/Obsidian/notion-migration"
sys.path.insert(0, NMIG)
from nmig.client import NotionAPIError  # noqa: E402
from nmig.config import load_config  # noqa: E402
import requests  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tools/ (python3 -I drops the script dir)
from rate_gate import RateGate  # noqa: E402
from readonly_client import make_read_client  # noqa: E402

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATE_GATE_STATE = os.path.join(WORK, "state", "rate_gate.json")
RATE_GATE_TOTAL_RPS = 2.5  # WRITEBACK_SPEC §15 rate_limit_total_rps, shared by every Notion API process
BLOBS = os.path.join(WORK, "blobs")
HOSTED = re.compile(r"^https://(prod-files-secure\.s3\.[a-z0-9-]+\.amazonaws\.com|s3\.[a-z0-9-]+\.amazonaws\.com/secure\.notion-static\.com|file\.notion\.so)/")


def download(http, url):
    tmp = os.path.join(BLOBS, "tmp", uuid.uuid4().hex)
    h, size = hashlib.sha256(), 0
    try:
        with http.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    h.update(chunk)
                    size += len(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        d = h.hexdigest()
        dst = os.path.join(BLOBS, d)
        if os.path.exists(dst):
            os.unlink(tmp)
        else:
            os.replace(tmp, dst)
        return d, size, ctype
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fresh_url(client, key):
    owner, field = key.split(":", 1)
    if field in ("cover", "icon") or field.startswith("prop:"):
        p = client.retrieve_page(owner)
        if field in ("cover", "icon"):
            return ((p.get(field) or {}).get("file") or {}).get("url")
        _, name, idx = field.split(":", 2)
        f = ((p.get("properties") or {}).get(name) or {}).get("files", [])[int(idx)]
        return (f.get("file") or {}).get("url")
    b = client.retrieve_block(owner)
    t = b.get("type")
    v = b.get(t) or {}
    if field == "embed":
        return v.get("url")
    return (v.get("file") or {}).get("url")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--rate", type=float, default=2.7,
                    help="per-process cap in requests/s; the shared rate gate (state/rate_gate.json) "
                         "limits all processes together to 2.5 requests/s")
    a = ap.parse_args()
    cfg = load_config()
    client = make_read_client(token=cfg["target_token"], version=cfg["version"], label="godcreate-ro", rate=a.rate,
                              gate=RateGate(RATE_GATE_STATE, total_rps=RATE_GATE_TOTAL_RPS))
    http = requests.Session()
    db = sqlite3.connect(a.db)
    todo = []
    for bid, typ, data in db.execute("SELECT id,type,data FROM blocks WHERE type IN ('embed','bookmark','link_preview')"):
        url = (json.loads(data).get(typ) or {}).get("url") or ""
        if HOSTED.match(url):
            key = f"{bid}:embed"
            row = db.execute("SELECT status FROM files WHERE key=?", (key,)).fetchone()
            if not row or row[0] != "ok":
                todo.append((key, bid, "block", "embed", url.split("?")[0].rsplit("/", 1)[-1]))
    for key, owner, okind, field, name in db.execute("SELECT key,owner_id,owner_kind,field,name FROM files WHERE status != 'ok'").fetchall():
        todo.append((key, owner, okind, field, name))
    stats = {"todo": len(todo), "ok": 0, "error": 0}
    for key, owner, okind, field, name in todo:
        try:
            url = fresh_url(client, key)
            if not url:
                raise RuntimeError("no url on re-read")
            d, size, ctype = download(http, url)
            db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, owner, okind, field, name, size, d, "ok", ctype[:100] or None))
            stats["ok"] += 1
        except (NotionAPIError, Exception) as e:  # noqa: BLE001
            db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, owner, okind, field, name, None, None, "error", f"{type(e).__name__}: {e}"[:300]))
            stats["error"] += 1
        db.commit()
    stats["requests"] = client.stats
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
