# -*- coding: utf-8 -*-
"""Produce a slim catalog.json: only the fields the market renders and installs with.

The catalog is fetched over a throttled link, so every byte costs refresh latency.
Cards need: name, Chinese description, version, artifact URL, sha256, size.
Dropped: English description, author, categories, homepage, repository, downloads,
publisher/trust flags, readme, icon.

Usage: python slim.py [--zh-limit 80] [--src catalog.json] [--out catalog.json]
"""
from __future__ import annotations

import argparse
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(BASE, "catalog.json"))
    ap.add_argument("--out", default=os.path.join(BASE, "catalog.json"))
    ap.add_argument("--zh-limit", type=int, default=80)
    args = ap.parse_args()

    cat = json.load(open(args.src, encoding="utf-8"))
    slim = []
    for p in cat["plugins"]:
        v = p["versions"][0]
        zh = ((p.get("i18n") or {}).get("zh-CN") or {}).get("description") or ""
        if len(zh) > args.zh_limit:
            zh = zh[: args.zh_limit].rstrip("，,。.、；; ") + "…"
        entry = {"id": p["id"], "name": p["name"]}
        if zh:
            entry["i18n"] = {"zh-CN": {"description": zh}}
        vv = {"version": v["version"]}
        for k in ("url", "shasum", "sizeBytes", "permissions"):
            if v.get(k):
                vv[k] = v[k]
        vv["yanked"] = False
        entry["versions"] = [vv]
        slim.append(entry)

    out = {
        "schema_version": cat.get("schema_version", 2),
        "provider_id": cat.get("provider_id", "pi-adapter"),
        "catalog_id": cat.get("catalog_id"),
        "name": cat.get("name"),
        "homepage": cat.get("homepage"),
        "generated_at": cat.get("generated_at"),
        "updated_at": cat.get("updated_at"),
        "policy_version": cat.get("policy_version"),
        "plugins": slim,
    }
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    print(f"entries={len(slim)} bytes={len(body.encode('utf-8'))} zh_limit={args.zh_limit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
