#!/usr/bin/env python3
"""batch_fetch_renrendoc.py — 批量从人人文库拉取试卷原卷

**为什么要有这个工具**

题库没建起来是项目根因（用户 2026-10-04 判断）。单份拉取
（`fetch_renrendoc_papers.py`）效率太低：每份都要人工搜一次、
人工敲一次 URL、跑一次命令。**建题库要的是「一次几十份」**。

**人人文库是当前唯一可用的原卷渠道**（`probe_paper_sources.py` 实测结论）：
页面内嵌 N 张整页 JPG/GIF，公式完整，**Referer 必须是具体页面 URL**。

**paperId 从哪来**

⚠️ **人人文库没有可用的站内搜索接口**（`/search?q=` 返回 404），
所以 paperId 只能从**外部搜索引擎**拿到——这一点必须写在工具里，
否则下一个人会以为它能自己发现试卷（它不能）。

本工具的职责是**批量搬运已知的 paperId 清单**，不是发现。
发现靠 `data/resources/renrendoc-papers.json`（人工/搜索维护的清单）。

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/batch_fetch_renrendoc.py                 # 拉清单里全部
$PY tools/batch_fetch_renrendoc.py --year 2018      # 只拉某年
$PY tools/batch_fetch_renrendoc.py --list           # 只看清单
$PY tools/batch_fetch_renrendoc.py --workers 4      # 并发（默认 3，勿调高）
```

⚠️ **并发别调高**：人人文库会 403 / SSL 中断。默认 3 是试出来的稳定值。
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(ROOT, "data", "resources", "renrendoc-papers.json")
IMG_DIR = os.path.join(ROOT, "RAW", "试卷库", "原卷扫描")
MANIFEST = os.path.join(ROOT, "data", "raw-manifest.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

VIEW_IMG = re.compile(
    r'<img[^>]+src=["\']([^"\']*file\d+\.renrendoc\.com/(?:view|fileroot_temp3)[^"\']*?\.(?:jpg|gif|png|jpeg))["\']',
    re.I)


def get(url, referer, timeout=35, want_bytes=False, tries=4):
    """⚠️ referer 必须是**具体页面 URL**——用站点首页必定 403。
    这个是整个采集链的技术要点，注释别删。"""
    last = None
    for i in range(tries):
        try:
            h = {"User-Agent": UA, "Referer": referer,
                 "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                 "Accept-Language": "zh-CN,zh;q=0.9", "Connection": "close"}
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw if want_bytes else raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


def img_size(d):
    if d[:6] in (b"GIF87a", b"GIF89a"):
        return (int.from_bytes(d[6:8], "little"),
                int.from_bytes(d[8:10], "little"))
    if d[:2] == b"\xff\xd8":
        i = 2
        while i < len(d) - 9:
            if d[i] != 0xFF:
                i += 1
                continue
            m = d[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return (int.from_bytes(d[i + 7:i + 9], "big"),
                        int.from_bytes(d[i + 5:i + 7], "big"))
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    return (0, 0)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


def fetch_one(entry):
    """拉一份试卷。返回 (entry_id, 状态, 落盘数, 说明, 新下载记录)。"""
    pid = str(entry["paperId"])
    page = entry.get("url") or f"https://www.renrendoc.com/paper/{pid}.html"
    tag = entry["id"]
    try:
        html = get(page, page, timeout=30)
    except Exception as e:
        return (tag, "页面失败", 0, str(e)[:50], [])

    imgs, seen = [], set()
    for u in VIEW_IMG.findall(html):
        full = ("https:" + u) if u.startswith("//") else u
        if full not in seen:
            seen.add(full)
            imgs.append(full)
    if not imgs:
        return (tag, "无图链", 0,
                f"页面 {len(html)} 字符但没找到 renrendoc 图链", [])

    os.makedirs(IMG_DIR, exist_ok=True)
    n_ok, recs = 0, []
    for i, u in enumerate(imgs, 1):
        ext = ".gif" if u.lower().endswith(".gif") else (
            ".png" if u.lower().endswith(".png") else ".jpg")
        dst = os.path.join(IMG_DIR, f"{tag}-p{i:02d}{ext}")
        if os.path.exists(dst):
            n_ok += 1
            continue
        try:
            d = get(u, page, timeout=40, want_bytes=True)
            w, h = img_size(d)
            with open(dst, "wb") as f:
                f.write(d)
            recs.append({"file": os.path.relpath(dst, ROOT), "bytes": len(d),
                         "width": w, "height": h, "source": page})
            n_ok += 1
        except Exception:
            pass
    return (tag, "ok", n_ok, f"共 {len(imgs)} 张", recs)


def append_manifest(records, tag):
    data = {"meta": {}, "items": []}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    items = data.setdefault("items", [])
    # ⚠️ 标准键是 `file`（ingest-raw.sh / check_raw / check_manifest_key_consistency 都只认它）。
    # 下面两处曾误写 `path`：`it.get("path")` 让 have 恒为 {None} ⇒ 去重完全失效；
    # `r["path"]` 而 rec 只有 `file` 键 ⇒ 每次登记必抛 KeyError。
    # 症状是脚本跑完最后一步才炸，而图片早已落盘 ⇒ 极易被「手工补登记」掩盖过去。
    have = {it.get("file") for it in items}
    for r in records:
        p = os.path.join(ROOT, r["file"])
        if r["file"] in have or not os.path.exists(p):
            continue
        items.append({
            "file": r["file"], "kind": "image", "bytes": r["bytes"],
            "sha256": sha256(p), "width": r["width"], "height": r["height"],
            "source": r["source"],
            "fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "note": f"人人文库批量采集 {tag}",
        })
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description="批量从人人文库拉试卷原卷")
    ap.add_argument("--year", default="", help="只拉某年，如 2018")
    ap.add_argument("--subject", default="数学", help="科目，默认数学")
    ap.add_argument("--list", action="store_true", help="只打印清单")
    ap.add_argument("--workers", type=int, default=3,
                    help="并发数，默认 3（⚠️ 调高会 403）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(LIST, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("papers", [])
    if args.year:
        entries = [e for e in entries if str(e.get("year", "")).startswith(args.year)]
    if args.subject:
        entries = [e for e in entries if e.get("subject") == args.subject]

    print(f"清单里 {len(entries)} 份（{args.subject} {args.year or '全部年份'}）")
    if args.list:
        for e in entries:
            have = sum(1 for f in os.listdir(IMG_DIR)
                       if f.startswith(e["id"])) if os.path.isdir(IMG_DIR) else 0
            print(f"  {e['id']:28s} {e.get('year')}  已有 {have} 张")
        return 0
    if not entries:
        print("（清单为空）")
        return 0
    if args.dry_run:
        return 0

    results, all_recs = [], []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, e): e for e in entries}
        for n, fut in enumerate(cf.as_completed(futs), 1):
            e = futs[fut]
            try:
                tag, st, cnt, msg, recs = fut.result()
            except Exception as ex2:
                tag, st, cnt, msg, recs = e["id"], "异常", 0, str(ex2)[:50], []
            all_recs.extend(recs)
            mark = "✅" if st == "ok" else "❌"
            print(f"  [{n}/{len(entries)}] {mark} {tag:28s} {cnt} 张  {msg}",
                  flush=True)
            results.append((tag, st, cnt))

    if all_recs:
        append_manifest(all_recs, "batch")
    n_ok = sum(1 for _, s, _ in results if s == "ok")
    n_img = sum(c for _, _, c in results)
    print(f"\n完成：{n_ok}/{len(entries)} 份成功，共 {n_img} 张图"
          f"（新下载 {len(all_recs)} 张，已登记 manifest）")
    print("⚠️ **落盘不代表完整**：每份都要逐页读一遍确认题号齐全。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
