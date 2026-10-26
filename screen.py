# -*- coding: utf-8 -*-
"""Pre-screen every PI package for "could this become a good PI-Desktop plugin?".

Cheap pass: one tarball download per package, in memory, no npm, no packing.
Result is cached in cache/screen.json so the expensive build pass only touches
packages that can actually pass the market gates.

Usage:
  python screen.py --meta <npm-latest.json> --workers 12
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import tarfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
SCREEN = os.path.join(BASE, "cache", "screen.json")
POLICY = json.load(open(os.path.join(BASE, "policy.json"), encoding="utf-8"))
UA = {"User-Agent": "pi-market-screen/1.0"}
TUI_RE = re.compile(
    r"\.(setWidget|setFooter|setHeader|setTitle|custom|overlay|onTerminalInput|"
    r"setWorkingVisible|setWorkingIndicator|setHiddenThinkingLabel|pasteToEditor|"
    r"editor|registerMessageRenderer|registerEntryRenderer|navigateTree|shutdown)\s*\(")
HOST_PROVIDED = ("@earendil-works/", "@mariozechner/")


def http(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def resolve(names, entry):
    e = str(entry).replace("\\", "/").lstrip("./").rstrip("/")
    for c in (e, e + ".ts", e + ".js", e + ".mjs", e + ".cjs",
              e + "/index.ts", e + "/index.js", e + "/index.mjs", e + "/index.cjs"):
        if c in names:
            return c
    return None


def judge(item):
    name, meta = item
    tb = meta.get("tarball")
    if not tb:
        return name, {"verdict": "no-tarball"}
    try:
        raw = http(tb)
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except Exception as e:  # noqa: BLE001
        return name, {"verdict": "download-failed", "reason": str(e)[:80]}
    members = [m for m in tf.getmembers() if m.isfile()]
    names = [m.name.split("/", 1)[1] if m.name.startswith("package/") else m.name for m in members]
    total = sum(m.size for m in members)
    if len(members) > POLICY["max_files"]:
        return name, {"verdict": "too-many-files", "files": len(members)}
    if total > POLICY["max_bytes"]:
        return name, {"verdict": "too-large", "bytes": total}
    pkgjson = None
    for m in members:
        nm = m.name.split("/", 1)[1] if m.name.startswith("package/") else m.name
        if nm == "package.json":
            try:
                pkgjson = json.loads(tf.extractfile(m).read().decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                pkgjson = None
            break
    if not pkgjson:
        return name, {"verdict": "no-package-json"}
    lic = pkgjson.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    lic = str(lic or "NONE")
    if lic not in POLICY["licenses"]:
        return name, {"verdict": "license", "license": lic}
    pi = pkgjson.get("pi") or {}
    ext = pi.get("extensions") or []
    skills = pi.get("skills") or []
    if not ext and not skills:
        return name, {"verdict": "no-contributions"}
    if ext:
        resolved = [resolve(names, e) for e in ext]
        if not all(resolved):
            return name, {"verdict": "entry-missing", "ext": ext[:3]}
    reasons = set()
    for m in members:
        nm = m.name.split("/", 1)[1] if m.name.startswith("package/") else m.name
        if not nm.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")) or m.size > 1_200_000:
            continue
        try:
            src = tf.extractfile(m).read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        if "pi-tui" in src:
            reasons.add("pi-tui")
        for hit in TUI_RE.findall(src):
            reasons.add("ui." + hit)
    if reasons:
        return name, {"verdict": "tui", "reason": ",".join(sorted(reasons)[:5])}
    deps = [k for k in (pkgjson.get("dependencies") or {}) if not k.startswith(HOST_PROVIDED)]
    if deps and not ext:
        return name, {"verdict": "ok", "license": lic, "deps": 0, "files": len(members),
                      "bytes": total, "skills": len(skills), "version": meta.get("version")}
    return name, {"verdict": "ok-deps" if deps else "ok", "license": lic, "deps": len(deps),
                  "files": len(members), "bytes": total, "ext": len(ext), "skills": len(skills),
                  "version": meta.get("version")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True, help="npm-latest.json produced by the coverage pass")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--recheck", action="store_true")
    args = ap.parse_args()
    meta = json.load(open(args.meta, encoding="utf-8"))
    done = {}
    if os.path.exists(SCREEN) and not args.recheck:
        done = json.load(open(SCREEN, encoding="utf-8"))
    todo = [(n, m) for n, m in meta.items() if n not in done and m.get("version")]
    print(f"meta={len(meta)} screened={len(done)} todo={len(todo)}", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(judge, it) for it in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                n, r = fut.result()
            except Exception as e:  # noqa: BLE001
                continue
            done[n] = r
            if i % 250 == 0:
                json.dump(done, open(SCREEN, "w", encoding="utf-8"), ensure_ascii=False)
                print(f"  {i}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    for f in futs:
        f.cancel()
    json.dump(done, open(SCREEN, "w", encoding="utf-8"), ensure_ascii=False)
    from collections import Counter
    c = Counter(v["verdict"] for v in done.values())
    print("verdicts:", json.dumps(c, ensure_ascii=False))
    print("buildable:", c.get("ok", 0) + c.get("ok-deps", 0), f"in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
