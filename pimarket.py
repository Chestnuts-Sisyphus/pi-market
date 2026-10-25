#!/usr/bin/env python3
"""pimarket — PI (npm / pi.dev) packages <-> PI-Desktop plugin catalog adapter.

Design goals (long term)
-----------------------
1. One source of truth.  `lock.json` pins, for every packaged plugin: the npm
   package, its exact version, its license, the npm tarball integrity, and the
   sha256/size of the .piplug we produced.  Everything is reproducible from it.
2. Immutable artifacts.  Published packages live in GitHub release assets (a
   tag never changes); the catalog only points at them by absolute https URL, so
   the client's download allowlist is satisfied without any local server.
3. No client modification.  We generate a catalog v2 and .piplug files that the
   host already knows how to install; nothing in the app is patched.
4. Verifiable.  `verify` is a release gate: it re-hashes every artifact, checks
   the store-only zip shape and the manifest rules the host enforces.
5. Bounded and reversible.  Removing one directory (or reverting one commit)
   undoes everything.  The official channel is unaffected.

Commands
--------
  index      Fetch the pi.dev gallery into cache/pi-index.json
  build      Package one or more packages into .piplug
  build-top  Package by download rank (incremental, budget aware)
  catalog    Write catalog.json (relative URLs; --base-url for published URLs)
  publish    Write lock.json + a catalog whose URLs are release assets
  verify     Release gate over catalog.json + packages/
  serve      Optional loopback HTTP server (development fallback only)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
NPM_DIR = os.path.join(CACHE, "npm")
TGZ_DIR = os.path.join(CACHE, "tgz")
PLUGINS = os.path.join(BASE, "plugins")
PACKAGES = os.path.join(BASE, "packages")
INDEX = os.path.join(CACHE, "pi-index.json")
VERSIONS = os.path.join(CACHE, "npm-versions.json")
BUILT = os.path.join(CACHE, "built.json")
CATALOG = os.path.join(BASE, "catalog.json")
LOCK = os.path.join(BASE, "lock.json")
POLICY = os.path.join(BASE, "policy.json")

ID_PREFIX = "pi-pkg."
UA = "pimarket/2.0 (+local adapter)"
SKIP_DIRS = {".git", "node_modules", ".github", ".idea", ".vscode", "__pycache__"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")

# Resolved by the desktop sidecar's import aliases; a packaged plugin does not
# need to ship them.
HOST_PROVIDED = {
    "@earendil-works/pi-ai", "@earendil-works/pi-agent-core", "@earendil-works/pi-coding-agent",
    "@earendil-works/pi-tui", "@mariozechner/pi-ai", "@mariozechner/pi-agent-core",
    "@mariozechner/pi-coding-agent", "@mariozechner/pi-tui", "typebox", "@sinclair/typebox",
}

DEFAULT_POLICY = {
    # Redistribution policy: only licenses that allow republishing with notice.
    # A package without a declared license is never published.
    "licenses": ["MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0",
                 "Unlicense", "CC0-1.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
                 "GPL-3.0-only", "GPL-3.0-or-later", "LGPL-3.0-only", "MPL-2.0"],
    "max_files": 2000,
    "max_bytes": 52428800,
    "max_agent_extensions": 8,
    "bundle_dependencies": False,
    "max_package_mb": 8,
    "per_run_budget": 120,
}


def log(*a):
    print(*a, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def http(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ensure_dirs():
    for d in (CACHE, NPM_DIR, TGZ_DIR, PLUGINS, PACKAGES):
        os.makedirs(d, exist_ok=True)


def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def policy() -> dict:
    p = dict(DEFAULT_POLICY)
    p.update(load_json(POLICY, {}) or {})
    return p


# --------------------------------------------------------------------- index

def parse_gallery(html: str):
    out = []
    for m in re.finditer(r'<article([^>]*data-package-card="true"[^>]*)>(.*?)</article>', html, re.S):
        attrs, body = m.group(1), m.group(2)
        name = re.search(r'data-package-name="([^"]+)"', attrs)
        if not name:
            continue
        dl = re.search(r'data-package-downloads="([^"]+)"', attrs)
        types = re.search(r'data-package-types="([^"]*)"', attrs)
        date = re.search(r'data-package-date="([^"]+)"', attrs)
        desc = re.search(r'<p class="packages-desc">(.*?)</p>', body, re.S)
        meta = re.findall(r'<div class="packages-meta"><span>(.*?)</span>', body, re.S)
        out.append({
            "name": name.group(1),
            "types": (types.group(1) or "").split() if types else [],
            "downloads": int(dl.group(1)) if dl else 0,
            "date_ms": int(date.group(1)) if date else None,
            "description": re.sub(r"\s+", " ", desc.group(1)).strip() if desc else "",
            "author": re.sub(r"<[^>]+>", "", meta[0]).strip() if meta else "",
        })
    return out


def cmd_index(args):
    ensure_dirs()
    pkgs, page, total = {}, 1, None
    while True:
        try:
            html = http(f"https://pi.dev/packages?sort=downloads&page={page}").decode("utf-8", "ignore")
        except Exception as e:
            log(f"  page {page}: ERROR {e}")
            break
        m = re.search(r'packages-count">(\d+)-(\d+)\s*/\s*(\d+)<', html)
        if m:
            total = int(m.group(3))
        rows = parse_gallery(html)
        if not rows:
            break
        for r in rows:
            pkgs.setdefault(r["name"], r)
        if page == 1 or page % 20 == 0:
            log(f"  page {page}: unique {len(pkgs)}/{total or '?'}")
        if total and len(pkgs) >= total:
            break
        page += 1
        if page > 400:
            break
    data = {"fetched_at": now_iso(), "site_total": total, "count": len(pkgs),
            "packages": sorted(pkgs.values(), key=lambda r: -r["downloads"])}
    write_json(INDEX, data)
    log(f"index: {data['count']} packages (site {total}) -> {INDEX}")
    if args.fetch_versions:
        cmd_versions(args)


def load_index():
    idx = load_json(INDEX, None)
    if not idx:
        raise SystemExit("run `pimarket.py index` first")
    return idx


def cmd_versions(args):
    """Resolve latest versions for every indexed package via npm search paging."""
    ensure_dirs()
    names = {p["name"] for p in load_index()["packages"]}
    found = load_json(VERSIONS, {})
    for kw in ("keywords:pi-package", "keywords:pi-extension"):
        for page in range(0, 60):
            url = f"https://registry.npmjs.org/-/v1/search?text={urllib.parse.quote(kw)}&size=250&from={page*250}"
            try:
                d = json.loads(http(url).decode("utf-8", "ignore"))
            except Exception as e:
                log(f"  versions {kw} from={page*250}: {e}")
                break
            objs = d.get("objects") or []
            if not objs:
                break
            for o in objs:
                p = o["package"]
                if p["name"] in names and p["name"] not in found:
                    found[p["name"]] = p.get("version")
            total = d.get("total") or 0
            if (page + 1) * 250 >= total:
                break
            time.sleep(0.2)
        log(f"  {kw}: resolved {sum(1 for n in found if n in names)}/{len(names)}")
    missing = [n for n in names if n not in found]
    if args.fetch_versions:
        for n in missing[:args.fetch_versions]:
            try:
                meta = npm_meta(n)
                v, _ = npm_latest(meta)
                if v:
                    found[n] = v
            except Exception:
                pass
    write_json(VERSIONS, found)
    log(f"versions: {len(found)} resolved, {len(missing)} missing -> {VERSIONS}")


# ----------------------------------------------------------------------- npm

def npm_meta(name: str) -> dict:
    safe = urllib.parse.quote(name, safe="@")
    f = os.path.join(NPM_DIR, safe.replace("/", "_").replace("@", "at_") + ".json")
    cached = load_json(f, None)
    if cached:
        return cached
    data = json.loads(http(f"https://registry.npmjs.org/{safe}").decode("utf-8", "ignore"))
    write_json(f, data)
    return data


def npm_latest(meta: dict):
    lv = meta.get("dist-tags", {}).get("latest")
    return lv, meta.get("versions", {}).get(lv, {})


def fetch_tarball(name: str, version: str, url: str) -> str:
    safe = name.replace("/", "_").replace("@", "at_")
    fn = os.path.join(TGZ_DIR, f"{safe}-{version}.tgz")
    if os.path.exists(fn) and os.path.getsize(fn) > 0:
        return fn
    open(fn, "wb").write(http(url, timeout=180))
    return fn


def extract_tgz(tgz: str, dest: str) -> str:
    import tarfile
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(tgz) as tf:
        for m in tf.getmembers():
            if m.name.startswith("package/"):
                m.name = m.name[len("package/"):]
            if not m.name or m.name.startswith(("/", "..")) or ".." in m.name.split("/"):
                continue
            target = os.path.join(dest, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tf.extractfile(m) as fh, open(target, "wb") as out:
                    shutil.copyfileobj(fh, out)
    return dest


# --------------------------------------------------------------------- build

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower())
    return re.sub(r"-{2,}", "-", s).strip("-.") or "pkg"


def plugin_id(name: str) -> str:
    return ID_PREFIX + slug(name)


def norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def declared_entries(pkg_json: dict, root: str):
    pi = pkg_json.get("pi") or {}
    ext = [norm(p) for p in (pi.get("extensions") or []) if isinstance(p, str)]
    skills = [norm(p) for p in (pi.get("skills") or []) if isinstance(p, str)]
    if not ext and not skills:
        d = os.path.join(root, "extensions")
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                ext.append("extensions/" + fn)
        sd = os.path.join(root, "skills")
        if os.path.isdir(sd):
            for fn in sorted(os.listdir(sd)):
                skills.append("skills/" + fn)
    return ext, skills


def expand_extensions(ext, work, limit):
    out = []
    for p in ext:
        full = os.path.join(work, p)
        if os.path.isdir(full):
            for dp, dn, fn in os.walk(full):
                dn[:] = [x for x in dn if x not in SKIP_DIRS]
                for f in sorted(fn):
                    if f.endswith((".ts", ".js", ".mjs", ".cjs", ".tsx", ".jsx")) and not f.endswith(".d.ts"):
                        out.append(os.path.relpath(os.path.join(dp, f), work).replace("\\", "/"))
        else:
            out.append(p)
    return out[:limit]


def pack_piplug(plugin_dir: str, out_path: str) -> tuple[int, str]:
    entries = []
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            f = os.path.join(dirpath, fn)
            entries.append((os.path.relpath(f, plugin_dir).replace("\\", "/"), f))
    if os.path.exists(out_path):
        os.remove(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for rel, f in entries:
            z.write(f, rel)
    h = hashlib.sha256()
    with open(out_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return os.path.getsize(out_path), h.hexdigest()


WRAPPER = "// Generated by pimarket (PI -> PI-Desktop adapter).\nmodule.exports = {};\n"


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def copy_keep(work, dst_root, keep) -> tuple[int, int]:
    files = size = 0
    for rel in keep:
        src = os.path.join(work, rel)
        if os.path.isfile(src):
            _copy(src, os.path.join(dst_root, rel)); files += 1; size += os.path.getsize(src)
            continue
        for dp, dn, fn in os.walk(src):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    continue
                relp = os.path.relpath(p, work)
                _copy(p, os.path.join(dst_root, relp)); files += 1; size += os.path.getsize(p)
    return files, size


def bundle_deps(work: str, deps: dict) -> tuple[int, int]:
    """npm install --omit=dev into the package root, then copy node_modules."""
    if not deps:
        return 0, 0
    npm = shutil.which("npm")
    if not npm:
        return 0, 0
    pkg = {"name": "pi-market-bundle", "version": "1.0.0", "private": True, "dependencies": deps}
    tmp = tempfile.mkdtemp(prefix="pimarket-deps-")
    try:
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            json.dump(pkg, fh)
        subprocess.run([npm, "install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund",
                        "--legacy-peer-deps", "--loglevel=error"], cwd=tmp, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        nm = os.path.join(tmp, "node_modules")
        if not os.path.isdir(nm):
            return 0, 0
        files = size = 0
        for dp, dn, fn in os.walk(nm):
            for f in fn:
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    continue
                relp = os.path.relpath(p, nm)
                dst = os.path.join(work, "node_modules", relp)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(p, dst)
                files += 1; size += os.path.getsize(p)
        return files, size
    except Exception:
        return 0, 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_license(work: str) -> str:
    pj = load_json(os.path.join(work, "package.json"), {}) or {}
    lic = pj.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if not lic and isinstance(pj.get("licenses"), list) and pj["licenses"]:
        lic = pj["licenses"][0].get("type")
    return str(lic or "NONE")


def build_one(name: str, force: bool = False) -> dict:
    pol = policy()
    meta = npm_meta(name)
    ver, vmeta = npm_latest(meta)
    if not ver:
        return {"name": name, "ok": False, "error": "no latest version"}
    pid = plugin_id(name)
    out_dir = os.path.join(PLUGINS, pid)
    out_pkg = os.path.join(PACKAGES, f"{pid}-{ver}.piplug")
    rec = {"name": name, "id": pid, "version": ver}

    if os.path.exists(out_pkg) and not force:
        size, sha = pack_piplug(out_dir, out_pkg) if os.path.isdir(out_dir) else (os.path.getsize(out_pkg), None)
        man = load_json(os.path.join(out_dir, "manifest.json"), {}) or {}
        c = man.get("contributes") or {}
        rec.update(ok=True, cached=True, sizeBytes=size, shasum=sha,
                   permissions=man.get("permissions") or ["agent.extension"],
                   extensions=c.get("agentExtensions") or [], skills=c.get("skills") or [],
                   license=read_license(os.path.join(out_dir, "src")), path=out_pkg)
        return rec

    deps = {k: v for k, v in (vmeta.get("dependencies") or {}).items() if k not in HOST_PROVIDED}
    tgz = fetch_tarball(name, ver, vmeta["dist"]["tarball"])
    work = extract_tgz(tgz, os.path.join(CACHE, "work", slug(name)))
    ext, skills = declared_entries(vmeta, work)
    if not ext and not skills:
        rec.update(ok=False, error="no pi.extensions / pi.skills")
        return rec
    ext = expand_extensions(ext, work, pol["max_agent_extensions"])
    skills = [s for s in skills if os.path.exists(os.path.join(work, s))]

    if deps and pol.get("bundle_dependencies"):
        files, size = bundle_deps(work, deps)
        rec["bundledDeps"] = files
        if not files:
            rec.update(ok=False, error="dependency bundle failed")
            return rec
    elif deps:
        rec.update(ok=False, needs_import=True,
                   error="runtime dependencies (" + ", ".join(sorted(deps)[:4]) +
                         ") — use the in-app pi-extension import, or enable bundle_dependencies")
        return rec

    keep = sorted({os.path.dirname(p) or "." if os.path.isfile(os.path.join(work, p)) else p
                   for p in ext + skills})
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    files, size = copy_keep(work, os.path.join(out_dir, "src"), keep or ["."])
    if deps and pol.get("bundle_dependencies"):
        files += rec.get("bundledDeps") or 0
    for extra in ("package.json", "README.md", "LICENSE", "LICENSE.md", "LICENCE"):
        f = os.path.join(work, extra)
        if os.path.exists(f):
            _copy(f, os.path.join(out_dir, "src", extra)); files += 1
    if files > pol["max_files"] or size > pol["max_bytes"]:
        shutil.rmtree(out_dir, ignore_errors=True)
        rec.update(ok=False, error=f"too large for a .piplug ({files} files, {size} bytes)")
        return rec

    ext = [p for p in ext if os.path.exists(os.path.join(out_dir, "src", p))]
    skills = [p for p in skills if os.path.exists(os.path.join(out_dir, "src", p))]
    if not ext and not skills:
        rec.update(ok=False, error="declared contributions missing from the package")
        return rec

    perms, contributes = [], {}
    if ext:
        perms.append("agent.extension")
        contributes["agentExtensions"] = ["src/" + p for p in ext]
    if skills:
        perms.append("agent.prompt.inject")
        contributes["skills"] = ["src/" + p for p in skills]

    manifest = {
        "schemaVersion": 1,
        "id": pid,
        "name": (vmeta.get("displayName") or name.split("/")[-1])[:60],
        "version": ver,
        "description": (vmeta.get("description") or "")[:300],
        "main": "main.cjs",
        "author": vmeta.get("author") if isinstance(vmeta.get("author"), str)
        else (vmeta.get("author") or {}).get("name", ""),
        "permissions": perms,
        "contributes": contributes,
        "engines": {"piDesktop": ">=0.21.0"},
    }
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    with open(os.path.join(out_dir, "main.cjs"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(WRAPPER)

    size, sha = pack_piplug(out_dir, out_pkg)
    rec.update(ok=True, sizeBytes=size, shasum=sha, permissions=perms,
               extensions=contributes.get("agentExtensions", []), skills=contributes.get("skills", []),
               license=read_license(os.path.join(out_dir, "src")),
               npmIntegrity=(vmeta.get("dist") or {}).get("integrity"),
               path=out_pkg)
    return rec


def _record(results):
    built = load_json(BUILT, {})
    for r in results:
        if r.get("ok"):
            built[r["name"]] = r
        else:
            built.pop(r["name"], None) if r.get("needs_import") else None
    write_json(BUILT, built)
    return built


def cmd_build(args):
    ensure_dirs()
    results = []
    for n in args.names:
        try:
            r = build_one(n, force=args.force)
        except Exception as e:
            r = {"name": n, "ok": False, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        log(f"  {'OK  ' if r.get('ok') else 'SKIP'} {n:<40} "
            f"{json.dumps({k: v for k, v in r.items() if k in ('version','sizeBytes','license','error')}, ensure_ascii=False)}")
    _record(results)


def cmd_build_top(args):
    pol = policy()
    idx = load_index()
    built = load_json(BUILT, {})
    done = failed = 0
    limit = args.limit if args.limit is not None else pol["per_run_budget"]
    for p in idx["packages"]:
        if done >= limit:
            break
        name = p["name"]
        if args.skip_built and name in built:
            continue
        try:
            r = build_one(name)
        except Exception as e:
            failed += 1
            log(f"  FAIL {name}: {type(e).__name__}: {e}")
            continue
        if not r.get("ok"):
            failed += 1
            continue
        if r["sizeBytes"] > args.max_mb * 1024 * 1024:
            failed += 1
            continue
        built[name] = r
        write_json(BUILT, built)
        done += 1
        if done % 10 == 0:
            log(f"  built {done} (skipped {failed})")
    log(f"build-top: {done} built, {failed} skipped")


# ------------------------------------------------------------------- catalog

def _catalog_entries(base_url: str | None, only_installable: bool) -> tuple[list, dict]:
    idx = load_index()
    built = load_json(BUILT, {})
    versions = load_json(VERSIONS, {})
    pol = policy()
    allowed = set(pol["licenses"])
    plugins, stats = [], {"entries": 0, "installable": 0, "no_version": 0,
                          "license_blocked": 0, "needs_import": 0}
    for p in idx["packages"]:
        name = p["name"]
        b = built.get(name)
        ver = (b or {}).get("version") or versions.get(name)
        if not ver:
            stats["no_version"] += 1
            continue
        if b and b.get("license") and b["license"] not in allowed:
            stats["license_blocked"] += 1
            b = None
        if only_installable and not b:
            continue
        when = None
        if p.get("date_ms"):
            when = datetime.fromtimestamp(p["date_ms"] / 1000, tz=timezone.utc) \
                .isoformat(timespec="seconds").replace("+00:00", "Z")
        vobj = {"version": ver}
        if when:
            vobj["publishedAt"] = when
        if b:
            rel = f"packages/{b['id']}-{b['version']}.piplug"
            vobj.update({
                "url": (base_url.rstrip("/") + "/" + os.path.basename(rel)) if base_url else rel,
                "shasum": b["shasum"],
                "sizeBytes": b["sizeBytes"],
                "permissions": b.get("permissions") or ["agent.extension"],
                "minPiDesktop": ">=0.21.0",
                "yanked": False,
            })
            stats["installable"] += 1
        else:
            vobj.update({"permissions": [], "yanked": False})
        plugins.append({
            "id": plugin_id(name),
            "name": name,
            "description": (p.get("description") or "")[:280],
            "author": p.get("author") or "",
            "icon_url": None,
            "categories": sorted(set(p.get("types") or []) | {"pi-package"}),
            "trust": "community",
            "verified": False,
            "publisher_id": None,
            "downloads": p.get("downloads", 0),
            "homepage": f"https://pi.dev/packages/{name}",
            "repository": None,
            "readme_markdown": None,
            "safety_notes": None,
            "versions": [vobj],
        })
    stats["entries"] = len(plugins)
    return plugins, stats


def _write_catalog(plugins, stats, base_url):
    cat = {
        "schema_version": 2,
        "provider_id": "pi-adapter",
        "catalog_id": "pi-package-adapter",
        "name": "PI package adapter",
        "homepage": "https://pi.dev/packages",
        "generated_at": now_iso(),
        "updated_at": now_iso(),
        "policy_version": "local-adapter-2",
        "artifact_base_url": None,
        "plugins": plugins,
    }
    write_json(CATALOG, cat)
    log(f"catalog: {stats['entries']} entries, {stats['installable']} installable "
        f"(license-blocked {stats['license_blocked']}, no version {stats['no_version']}) -> {CATALOG} "
        f"({os.path.getsize(CATALOG)/1e6:.2f} MB)")
    return cat


def cmd_catalog(args):
    plugins, stats = _catalog_entries(args.base_url, False)
    _write_catalog(plugins, stats, args.base_url)


def cmd_publish(args):
    """Write lock.json + a catalog whose package URLs are release assets.

    Tags are immutable, so a package keeps the tag it was first published under;
    only packages that are new or whose bytes changed move to --release-tag.
    """
    pol = policy()
    allowed = set(pol["licenses"])
    built = load_json(BUILT, {})
    prev = (load_json(LOCK, {}) or {}).get("packages", {})
    releases_base = args.releases_base.rstrip("/")

    publishable = {n: r for n, r in built.items() if r.get("license") in allowed}
    blocked = {n: r.get("license") for n, r in built.items() if r.get("license") not in allowed}

    idx = load_index()
    versions = load_json(VERSIONS, {})
    plugins, stats = [], {"entries": 0, "installable": 0, "no_version": 0,
                          "license_blocked": len(blocked), "needs_import": 0}
    new_lock = {}
    moved = kept = 0
    for p in idx["packages"]:
        name = p["name"]
        b = publishable.get(name)
        ver = (b or {}).get("version") or versions.get(name)
        if not ver:
            stats["no_version"] += 1
            continue
        when = None
        if p.get("date_ms"):
            when = datetime.fromtimestamp(p["date_ms"] / 1000, tz=timezone.utc) \
                .isoformat(timespec="seconds").replace("+00:00", "Z")
        vobj = {"version": ver}
        if when:
            vobj["publishedAt"] = when
        if b:
            asset = f"{b['id']}-{b['version']}.piplug"
            old = prev.get(name)
            if old and old.get("sha256") == b["shasum"] and old.get("tag"):
                tag = old["tag"]; kept += 1
            else:
                tag = args.release_tag; moved += 1
            vobj.update({
                "url": f"{releases_base}/{tag}/{asset}",
                "shasum": b["shasum"],
                "sizeBytes": b["sizeBytes"],
                "permissions": b.get("permissions") or ["agent.extension"],
                "minPiDesktop": ">=0.21.0",
                "yanked": False,
            })
            stats["installable"] += 1
            new_lock[name] = {
                "npm": name, "version": b["version"], "license": b.get("license"),
                "npm_integrity": b.get("npmIntegrity"), "plugin_id": b["id"],
                "asset": asset, "tag": tag, "url": vobj["url"],
                "sha256": b["shasum"], "size_bytes": b["sizeBytes"],
                "permissions": b.get("permissions"), "extensions": b.get("extensions"),
                "skills": b.get("skills"),
            }
        else:
            vobj.update({"permissions": [], "yanked": False})
        plugins.append({
            "id": plugin_id(name), "name": name,
            "description": (p.get("description") or "")[:280],
            "author": p.get("author") or "", "icon_url": None,
            "categories": sorted(set(p.get("types") or []) | {"pi-package"}),
            "trust": "community", "verified": False, "publisher_id": None,
            "downloads": p.get("downloads", 0),
            "homepage": f"https://pi.dev/packages/{name}",
            "repository": None, "readme_markdown": None, "safety_notes": None,
            "versions": [vobj],
        })
    stats["entries"] = len(plugins)
    _write_catalog(plugins, stats, None)

    lock = {"schema": 1, "generated_at": now_iso(), "releases_base": releases_base,
            "release_tag": args.release_tag, "policy": {"licenses": sorted(allowed)},
            "packages": dict(sorted(new_lock.items())), "excluded": blocked}
    write_json(LOCK, lock)
    log(f"publish: {len(new_lock)} locked (tag {args.release_tag}: {moved} new/changed, "
        f"{kept} kept on earlier tags), {len(blocked)} excluded by license -> {LOCK}")
    for n, l in list(blocked.items())[:5]:
        log(f"  excluded {n} ({l})")


# -------------------------------------------------------------------- verify

def cmd_verify(args):
    cat = load_json(CATALOG, None)
    if not cat:
        raise SystemExit("no catalog.json")
    lock = (load_json(LOCK, {}) or {}).get("packages", {})
    only_tag = getattr(args, "only_tag", None)
    problems, installable = [], 0
    seen = set()
    in_base = BASE
    for p in cat.get("plugins") or []:
        pid = p.get("id", "")
        if pid in seen:
            problems.append(f"duplicate id {pid}")
        seen.add(pid)
        if not ID_RE.match(pid):
            problems.append(f"bad id {pid}")
        for v in p.get("versions") or []:
            if not SEMVER.match(v.get("version", "")):
                problems.append(f"{pid}: bad semver {v.get('version')}")
            url = v.get("url")
            if not url:
                continue
            lic = (lock.get(p["name"]) or {})
            if only_tag and lic.get("tag") != only_tag:
                continue
            installable += 1
            if not re.match(r"^[0-9a-f]{64}$", v.get("shasum") or ""):
                problems.append(f"{pid}: bad shasum")
                continue
            local = os.path.join(in_base, "packages", os.path.basename(url))
            if not os.path.exists(local):
                problems.append(f"{pid}: artifact missing {os.path.basename(url)}")
                continue
            h = hashlib.sha256(open(local, "rb").read()).hexdigest()
            if h != v["shasum"]:
                problems.append(f"{pid}: sha mismatch")
            if os.path.getsize(local) != v.get("sizeBytes"):
                problems.append(f"{pid}: size mismatch")
            if not isinstance(v.get("permissions"), list):
                problems.append(f"{pid}: permissions not a list")
            try:
                with zipfile.ZipFile(local) as z:
                    if any(i.compress_type != 0 for i in z.infolist()):
                        problems.append(f"{pid}: deflated entries")
                    if "manifest.json" not in z.namelist():
                        problems.append(f"{pid}: no root manifest.json")
                    if any(n.startswith("/") or ".." in n.split("/") for n in z.namelist()):
                        problems.append(f"{pid}: unsafe zip path")
            except Exception as e:
                problems.append(f"{pid}: zip unreadable ({e})")
    log(f"verify: {len(cat.get('plugins') or [])} entries, {installable} installable, {len(problems)} problems")
    for x in problems[:25]:
        log("  -", x)
    return problems


# --------------------------------------------------------------------- serve

def cmd_serve(args):
    import http.server
    import socketserver
    os.chdir(BASE)

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", args.port), H) as httpd:
        log(f"serving {BASE} at http://127.0.0.1:{args.port}/catalog.json (dev fallback only)")
        httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(prog="pimarket", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("index", help="fetch the pi.dev gallery")
    i.add_argument("--fetch-versions", nargs="?", type=int, const=0, default=None,
                   help="also resolve npm versions (optionally for N missing packages)")
    i.set_defaults(func=cmd_index)

    sub.add_parser("versions", help="resolve npm versions only").set_defaults(func=cmd_versions)

    b = sub.add_parser("build", help="package named packages")
    b.add_argument("names", nargs="+")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_build)

    bt = sub.add_parser("build-top", help="package by download rank")
    bt.add_argument("--limit", type=int, default=None)
    bt.add_argument("--max-mb", type=float, default=8.0)
    bt.add_argument("--skip-built", action="store_true")
    bt.set_defaults(func=cmd_build_top)

    c = sub.add_parser("catalog", help="write catalog.json")
    c.add_argument("--base-url", default=None, help="prefix for package URLs")
    c.set_defaults(func=cmd_catalog)

    p = sub.add_parser("publish", help="write lock.json + release-ready catalog")
    p.add_argument("--releases-base", required=True,
                   help="e.g. https://github.com/<owner>/<repo>/releases/download")
    p.add_argument("--release-tag", required=True, help="tag the new/changed assets go to")
    p.set_defaults(func=cmd_publish)

    v = sub.add_parser("verify", help="release gate")
    v.add_argument("--only-tag", default=None, help="gate only packages published under this tag")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("serve", help="loopback HTTP server (dev fallback)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    rc = args.func(args)
    if args.cmd == "verify" and rc:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
