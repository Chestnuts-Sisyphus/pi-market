# -*- coding: utf-8 -*-
"""Build the screened-good subset (see screen.py) into .piplug artifacts.

Zero-dependency packages first (fast), then dependency packages (npm bundling).
Usage:
  python build_from_screen.py --workers 8 --limit 1500
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pimarket as pm

SCREEN = os.path.join(pm.BASE, "cache", "screen.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--time-budget", type=float, default=5400)
    ap.add_argument("--only", choices=["ok", "ok-deps", "both"], default="both")
    args = ap.parse_args()

    screen = json.load(open(SCREEN, encoding="utf-8"))
    built = pm.load_json(pm.BUILT, {}) or {}
    ok = [n for n, v in screen.items() if v.get("verdict") == "ok" and n not in built]
    deps = [n for n, v in screen.items() if v.get("verdict") == "ok-deps" and n not in built]
    idx = {p["name"]: p.get("downloads") or 0 for p in pm.load_index()["packages"]}
    deps = [n for n, v in screen.items() if v.get("verdict") == "ok-deps" and n not in built]
    heavy = ("sharp", "node-pty", "better-sqlite3", "@ast-grep/napi", "puppeteer", "playwright",
             "canvas", "sql.js", "onnxruntime-node", "@napi-rs/keyring", "koffi", "electron",
             "typescript", "tsx", "@swc/core", "esbuild", "re2", "bufferutil")
    meta = json.load(open(os.path.join(pm.BASE, "cache", "npm-latest.json"), encoding="utf-8"))
    def is_heavy(n):
        names = (meta.get(n) or {}).get("dep_names") or []
        return any(h in names for h in heavy) or len(names) > 3
    deps = [n for n in deps if not is_heavy(n)]
    deps.sort(key=lambda n: idx.get(n, 0), reverse=True)
    if args.only == "ok":
        todo = ok
    elif args.only == "ok-deps":
        todo = deps
    else:
        todo = ok + deps
    todo = todo[: args.limit]
    pm.log(f"screened ok={len(ok)} ok-deps={len(deps)} -> building {len(todo)}")

    t0 = time.time()
    done = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(pm.build_one, n, False, True): n for n in todo}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                r = {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
            if r.get("ok"):
                built[name] = r
                done += 1
            else:
                fail += 1
            if done % 25 == 0 and done:
                pm.write_json(pm.BUILT, built)
                pm.log(f"  built {done} (failed {fail}) {time.time() - t0:.0f}s")
            if done >= args.limit or time.time() - t0 > args.time_budget:
                break
    for f in futs:
        f.cancel()
    pm.write_json(pm.BUILT, built)
    pm.log(f"done: {done} built, {fail} failed, {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
