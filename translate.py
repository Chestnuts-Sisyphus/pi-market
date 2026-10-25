#!/usr/bin/env python3
"""translate — offline-free Chinese copy for the pi-market catalog.

Reads provider keys from D:/AI/KEY/FREE-FLEET.txt (free tiers only; the key value
never leaves memory and is never printed). Translates each package description to
Simplified Chinese, caches per package in cache/translations.json so the run is
resumable, and leaves identifiers (package names) untranslated on purpose.

Backends are tried in order: gemini -> zhipu -> groq -> openrouter.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache")
CACHE = os.path.join(CACHE_DIR, "translations.json")
INDEX = os.path.join(CACHE_DIR, "pi-index.json")
KEY_FILE = os.environ.get("PI_MARKET_KEY_FILE", r"D:\AI\KEY\FREE-FLEET.txt")

SYSTEM = (
    "你是软件目录的本地化译者。把用户给出的英文插件描述逐条译成简体中文。"
    "规则：保留产品名、包名、协议名与技术缩写原样（pi、MCP、LSP、Git、npm、API、CLI、JSON、SDK、OAuth 等）；"
    "不要解释、不要加序号以外的内容、不要翻译代号；每条控制在一句话内。"
    "只输出一个 JSON 数组，元素是与输入等长的字符串。"
    "硬性要求：除非原文本身已经是中文，否则输出必须全部是简体中文，禁止整句保留英文原文；"
    "原文是图片/HTML/链接之类的非文案时，输出空字符串。"
)


def log(*a):
    print(*a, flush=True)


def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0)
        fh.write("\n")
    os.replace(tmp, p)


# ------------------------------------------------------------------ keys

def read_keys():
    """Return [{label, key, provider}] parsed from the key file. Values stay in memory."""
    if not os.path.exists(KEY_FILE):
        return []
    out, label = [], ""
    for line in open(KEY_FILE, encoding="utf-8", errors="replace"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            label = re.sub(r"^#\s*【?([^】]*)】?.*$", r"\1", s).strip()
            continue
        if len(s) <= 14 and re.search(r"[\u4e00-\u9fffA-Za-z]", s):
            label = s
            continue
        prov = None
        if s.startswith("hf_"):
            prov = "hf"
        elif s.startswith("sk-or-v1-"):
            prov = "openrouter"
        elif s.startswith("gsk_"):
            prov = "groq"
        elif s.startswith("AQ."):
            prov = "gemini"
        elif s.startswith("csk-"):
            prov = "cerebras"
        elif s.startswith("ms-"):
            prov = "modelscope"
        elif s.startswith("sk-"):
            prov = "siliconflow"
        elif re.match(r"^[0-9a-f]{16,}\.[A-Za-z0-9_\-]{8,}$", s):
            prov = "zhipu"
        if prov:
            out.append({"label": label, "key": s, "provider": prov})
    return out


# --------------------------------------------------------------- backends

def _post(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _batch_prompt(items):
    lines = [f"{i+1}. {t}" for i, t in enumerate(items)]
    return "把下面 {} 条描述逐条译成简体中文，返回 JSON 数组。\n\n{}".format(len(items), "\n".join(lines))


def _parse_array(text, n):
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    return [str(x).strip() for x in arr]


def call_gemini(key, items):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.0-flash:generateContent?key=" + key)
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": _batch_prompt(items)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    d = _post(url, payload, {"Content-Type": "application/json"})
    text = d["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_array(text, len(items))


def _openai_style(url, key, model, items, extra=None):
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _batch_prompt(items)}],
    }
    if extra:
        payload.update(extra)
    d = _post(url, payload, {"Content-Type": "application/json",
                             "Authorization": "Bearer " + key})
    text = d["choices"][0]["message"]["content"]
    return _parse_array(text, len(items))


def call_zhipu(key, items):
    return _openai_style("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                         key, "glm-4-flash", items)


def call_groq(key, items):
    return _openai_style("https://api.groq.com/openai/v1/chat/completions",
                         key, "llama-3.1-8b-instant", items)


def call_openrouter(key, items):
    return _openai_style("https://openrouter.ai/api/v1/chat/completions", key,
                         "meta-llama/llama-3.1-8b-instruct:free", items,
                         {"response_format": {"type": "json_object"}})


BACKENDS = {"gemini": call_gemini, "zhipu": call_zhipu, "groq": call_groq,
            "openrouter": call_openrouter}


# ------------------------------------------------------------------- main

def translate_all(limit=None, batch=20, workers=6):
    import threading
    from queue import Queue

    idx = load_json(INDEX, None)
    if not idx:
        raise SystemExit("run `pimarket.py index` first")
    cache = load_json(CACHE, {"generated_at": None, "entries": {}})
    entries = cache["entries"]

    todo = [(p["name"], p["description"].strip())
            for p in idx["packages"]
            if p.get("description") and len(p["description"].strip()) > 3
            and p["name"] not in entries]
    log(f"packages: {len(idx['packages'])}, translated: {len(entries)}, todo: {len(todo)}")
    if limit:
        todo = todo[:limit]
    if not todo:
        return

    keys = [k for k in read_keys() if k["provider"] in BACKENDS]
    log("endpoints: " + ", ".join(f"{k['provider']}" for k in keys))
    if not keys:
        raise SystemExit("no usable free-tier key found in " + KEY_FILE)

    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    q = Queue()
    for i, b in enumerate(batches):
        q.put((i, b))
    lock = threading.Lock()
    stats = {"ok": 0, "items": 0, "failed": 0}
    slot = {"n": 0}

    def worker():
        while True:
            try:
                i, chunk = q.get_nowait()
            except Exception:
                return
            names = [c[0] for c in chunk]
            texts = [c[1] for c in chunk]
            out = None
            for attempt in range(len(keys)):
                with lock:
                    k = keys[(slot["n"] + attempt) % len(keys)]
                try:
                    out = BACKENDS[k["provider"]](k["key"], texts)
                    if out is None:
                        raise ValueError("unparsable batch")
                    break
                except Exception as e:
                    code = getattr(e, "code", None)
                    msg = f"HTTP {code}" if code else type(e).__name__
                    if code in (429, 503):
                        time.sleep(5)
                    out = None
            with lock:
                slot["n"] += 1
                if out is None:
                    stats["failed"] += len(chunk)
                else:
                    for n, z in zip(names, out):
                        entries[n] = {"zh": z}
                    stats["ok"] += 1
                    stats["items"] += len(chunk)
                    write_json(CACHE, {
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "entries": entries})
                    if stats["ok"] % 5 == 0:
                        log(f"  {stats['items']}/{len(todo)} items in {stats['ok']} batches "
                            f"(failed {stats['failed']})")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log(f"done: {stats['items']} new items, {stats['failed']} failed, "
        f"cache {len(entries)}, {time.time()-t0:.0f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--probe", action="store_true", help="translate 3 samples and print them")
    a = ap.parse_args()
    if a.probe:
        keys = [k for k in read_keys() if k["provider"] in BACKENDS]
        for k in keys:
            try:
                out = BACKENDS[k["provider"]](k["key"], [
                    "MCP (Model Context Protocol) adapter extension for the Pi coding agent.",
                    "Token-frugal long-term memory for pi: hybrid FTS + static embeddings.",
                    "Real-time code feedback for pi - LSP, linters, formatters.",
                ])
                log(f"{k['provider']} ({k['label']}): OK -> {out}")
                return
            except Exception as e:
                log(f"{k['provider']} ({k['label']}): {type(e).__name__}: {str(e)[:120]}")
    else:
        translate_all(limit=a.limit, batch=a.batch, workers=a.workers)


if __name__ == "__main__":
    main()
