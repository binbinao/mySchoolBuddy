#!/usr/bin/env python3
"""fetch_paper_images.py — 批量抓取试卷原卷扫描图（两阶段）

发现（2026-10-02）：21cnjy 的试卷原卷扫描图托管在 preview.21cnjy.com，
**免登录直链可下**（约 860×1216 PNG）。这是真正的「原卷」——化学式下标、
分数线、几何图形全部保留；网页预览文本转纯文本后会丢这些。

入口：https://mip.21cnjy.com/H/{docId}.shtml → HTML 内含 preview 图链。

为什么要两阶段（第一版踩的坑）：
  串行单线程时 mip 页限流严重，每份试卷要等 30s+ 重试，9 份跑了 4 分钟
  还没产出第一张图。拆成两阶段后：
    阶段1 解析图链 → 缓存到 data/_local/paper-img-index.json（带重试）
    阶段2 从 preview 直链下图 → 并发多线程（preview 域限流宽松得多）
  阶段1 结果逐份落盘，中断可续，重跑不再重复请求被限流的 mip 页。
"""

import argparse
import hashlib
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "RAW", "试卷库", "原卷扫描")
CACHE = os.path.join(ROOT, "data", "_local", "paper-img-index.json")
MANIFEST = os.path.join(ROOT, "data", "raw-manifest.json")
INDEX = os.path.join(ROOT, "data", "resources", "shanghai-papers.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url, timeout=25, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://mip.21cnjy.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


def decode(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def find_preview_imgs(html):
    pat = (r'(?:src|data-src|data-original|original)\s*=\s*'
           r'["\']([^"\']*preview[^"\']*?\.(?:png|jpg|jpeg))')
    out, seen = [], set()
    for m in re.findall(pat, html, re.I):
        u = m if m.startswith("http") else "https://mip.21cnjy.com" + m
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def png_size(p):
    with open(p, "rb") as f:
        d = f.read(26)
    if len(d) >= 24 and d[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
    return None, None


def phase1_resolve(targets, refresh=False):
    """解析每份试卷的原卷图链，逐份缓存到磁盘。"""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    cache = {}
    if os.path.exists(CACHE) and not refresh:
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f).get("items", {})

    todo = [p for p in targets
            if refresh or not cache.get(p["id"], {}).get("imgs")]
    print(f"阶段1 解析图链：待解析 {len(todo)} / 共 {len(targets)} 份", flush=True)

    for n, p in enumerate(todo, 1):
        mip = p.get("mipUrl") or f"https://mip.21cnjy.com/H/{p['id'].rsplit('-', 1)[-1]}.shtml"
        try:
            imgs = find_preview_imgs(decode(fetch(mip, timeout=12, retries=2)))
            cache[p["id"]] = {"imgs": imgs, "mipUrl": mip,
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            print(f"  [{n}/{len(todo)}] {p['id']:28s} {len(imgs)} 张", flush=True)
        except Exception as e:
            cache[p["id"]] = {"imgs": [], "mipUrl": mip, "error": str(e)[:80],
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            print(f"  [{n}/{len(todo)}] {p['id']:28s} 失败 {str(e)[:40]}", flush=True)
        with open(CACHE, "w", encoding="utf-8") as f:      # 逐份落盘，中断可续
            json.dump({"meta": {"updated": datetime.now().strftime("%Y-%m-%d %H:%M")},
                       "items": cache}, f, ensure_ascii=False, indent=1)
        time.sleep(0.6)

    total = sum(len(v.get("imgs", [])) for v in cache.values())
    print(f"阶段1 完成：{len(cache)} 份 / {total} 张图链", flush=True)
    return cache


def scan_dir():
    """扫描已落盘的原卷图 → 登记记录。用于补登记与对账。

    踩过的坑：main() 只登记「本次新下载」的图。62 张早已落盘后重跑，
    全部被 exists() 跳过 → manifest 一张都没写，且每次重跑都要重下。
    现在改为以磁盘为准反查登记，幂等。
    """
    out = []
    if not os.path.isdir(IMG_DIR):
        return out
    for fn in sorted(os.listdir(IMG_DIR)):
        if not fn.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        path = os.path.join(IMG_DIR, fn)
        with open(path, "rb") as f:
            data = f.read()
        w, h = png_size(path)
        out.append({"file": f"RAW/试卷库/原卷扫描/{fn}", "kind": "image",
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "width": w, "height": h,
                    "status": "原卷扫描·公开预览",
                    "parsed": {"wrongIds": [], "modules": []}})
    return out


def phase2_download(cache, workers=6, dry=False):
    """从 preview 直链并行下图。"""
    os.makedirs(IMG_DIR, exist_ok=True)
    jobs = []
    for pid, v in cache.items():
        for i, url in enumerate(v.get("imgs", []), 1):
            fn = f"{pid}-p{i:02d}.png"
            path = os.path.join(IMG_DIR, fn)
            if os.path.exists(path) and os.path.getsize(path) > 2000:
                continue
            jobs.append((pid, i, url, path, fn))
    print(f"\n阶段2 下载：{len(jobs)} 张待下（并发 {workers}）", flush=True)
    if not jobs:
        print("  全部已存在", flush=True)
        return []

    got, fail = [], []

    def one(j):
        pid, i, url, path, fn = j
        try:
            data = fetch(url, timeout=25, retries=3)
            if not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
                raise ValueError("非图片响应")
            if not dry:
                with open(path, "wb") as f:
                    f.write(data)
                w, h = png_size(path)
            else:
                w = h = 0
            return (fn, len(data), w, h, f"RAW/试卷库/原卷扫描/{fn}",
                    hashlib.sha256(data).hexdigest(), None)
        except Exception as e:
            return (fn, 0, 0, 0, None, None, str(e)[:60])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for n, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            if r[4]:
                got.append(r)
                print(f"  [{n}/{len(jobs)}] ✅ {r[0]}  {r[1]//1024}KB", flush=True)
            else:
                fail.append(r)
                print(f"  [{n}/{len(jobs)}] ⚠️  {r[0]} {r[6]}", flush=True)

    print(f"\n阶段2 完成：成功 {len(got)} · 失败 {len(fail)}", flush=True)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="强制重新解析图链")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scan-only", action="store_true",
                    help="只扫盘补登记 manifest，不联网")
    a = ap.parse_args()

    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    known = {i["file"] for i in man["items"]}

    if a.scan_only:
        recs = [r for r in scan_dir() if r["file"] not in known]
        man["items"].extend(recs)
        man["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
        print(f"补登记 {len(recs)} 张（manifest 共 {len(man['items'])} 条）")
        return

    with open(INDEX, encoding="utf-8") as f:
        idx = json.load(f)
    targets = [p for p in idx["papers"] if p.get("docId")]

    cache = phase1_resolve(targets, refresh=a.refresh)
    got = phase2_download(cache, workers=a.workers, dry=a.dry_run)
    if a.dry_run:
        return

    # 以磁盘为准登记：本轮新下的 + 之前落盘但漏登的（幂等）
    before = len(known)
    recs = [r for r in scan_dir() if r["file"] not in known]
    man["items"].extend(recs)
    man["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print(f"manifest 新增 {len(recs)} 张（本轮下载 {len(got)} 张，"
          f"登记 {before} → {len(man['items'])}）", flush=True)


if __name__ == "__main__":
    main()
