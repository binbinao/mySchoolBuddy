#!/usr/bin/env python3
"""fetch_renrendoc_papers.py — 从人人文库拉取真题原卷扫描图

**为什么是这个站**（2026-10-04 实测得出）

前面试过 6 个渠道补 2021/2024 原卷，全部失败（见
`tools/probe_paper_sources.py` 的结论表）。**唯一成功的是人人文库**：

| 渠道 | 结果 |
|---|---|
| `preview.21cnjy.com` | 21cnjy 全面超时（限流 + 沙箱链路） |
| `mzujuan` / `zujuan` | 付费墙，题干 JS 注入，HTML 里没有 |
| `webshot.chujuan.cn` | 只有 780px 网页内嵌片段，不是整页 |
| `docin`（道客巴巴） | 预览只有 1244 字 |
| `51jiaoxi`（教习网） | 只渲染前 6 题解析 |
| **`m.renrendoc.com/paper/{id}.html`** | ✅ **页面内嵌 N 张整页 JPG，公式完整** |

**关键坑：Referer 必须是「具体页面 URL」，用站点首页就 403**

实测：Referer = `https://m.renrendoc.com/` → HTTP 403
     Referer = `https://m.renrendoc.com/paper/333654107.html` → 200，5/5 成功
这是本工具唯一的技术要点，注释里写清，别改。

**页面里怎么找图**

详情页的 `<img src="//file4.renrendoc.com/view12/.../xxx.jpg">` 就是**整页扫描图**。
一个试卷 = N 张连续页图（2024 中考 = 5 张 1239×1752）。

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/fetch_renrendoc_papers.py https://m.renrendoc.com/paper/333654107.html
$PY tools/fetch_renrendoc_papers.py --list          # 只打印图链不下
$PY tools/fetch_renrendoc_papers.py --out RAW/试卷库/原卷扫描 --id 2024-zk-math <url>
```

**硬边界（与其它采集脚本一致）**

- ❌ 不改 `data/`：只读，写入只落在 `RAW/试卷库/原卷扫描/`
- ❌ 不编造题目内容：抓不到就报错，不写任何推测出来的题面
- ⚠️ 抓回来的图**不保证是完整卷**（有的页面只放前 3 页），
  落盘后**必须逐字读一遍确认页数与完整性**——本工具只负责搬运，不负责判断。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "RAW", "试卷库", "原卷扫描")
MANIFEST = os.path.join(ROOT, "data", "raw-manifest.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ⚠️ 路径有两种，必须都收（实测）：
#   2024 卷 → //file4.renrendoc.com/view12/M06/32/12/xxx.jpg
#   2022 卷 → //file4.renrendoc.com/view/81f3…/81f3….gif     ← view 不是 view12，且是 gif
# 第一版只写 view12 + jpg，2022 那份直接报「页面结构可能变了」。
VIEW_IMG = re.compile(
    r'<img[^>]+src=["\']([^"\']*file\d+\.renrendoc\.com/(?:view|fileroot_temp3)[^"\']*?\.(?:jpg|gif|png|jpeg))["\']',
    re.I)


def get(url, referer, timeout=30, want_bytes=False):
    """⚠️ referer 必须是**具体页面 URL**。用站点首页必定 403——本工具的核心要点。"""
    h = {"User-Agent": UA,
         "Referer": referer,
         "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw if want_bytes else raw.decode("utf-8", "ignore")


def img_size(d):
    """不依赖 PIL：扫 SOF 标记读宽高。JPEG 与 GIF 都支持。"""
    if d[:6] in (b"GIF87a", b"GIF89a"):
        return (int.from_bytes(d[6:8], "little"),
                int.from_bytes(d[8:10], "little"))
    if d[:2] != b"\xff\xd8":
        return (0, 0)
    i = 2
    while i < len(d) - 9:
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(d[i + 5:i + 7], "big")
            w = int.from_bytes(d[i + 7:i + 9], "big")
            return (w, h)
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(d[i + 2:i + 4], "big")
        i += 2 + seg
    return (0, 0)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(
        description="从人人文库拉取真题原卷扫描图（公式完整）")
    ap.add_argument("url", nargs="?", help="人人文库试卷详情页 URL")
    ap.add_argument("--out", default=DEFAULT_OUT, help="落盘目录")
    ap.add_argument("--id", default="", help="卷标识（用于文件名，默认从 URL 推断）")
    ap.add_argument("--list", action="store_true", help="只打印图链，不下载")
    args = ap.parse_args()

    if not args.url:
        ap.error("需要试卷详情页 URL")
    page = args.url

    try:
        html = get(page, page, timeout=25)
    except Exception as e:
        print(f"❌ 页面抓取失败：{str(e)[:70]}")
        print("   （Referer 已设为页面自身；若仍失败，多半是站点改版或网络不通）")
        return 1

    imgs = []
    seen = set()
    for u in VIEW_IMG.findall(html):
        full = ("https:" + u) if u.startswith("//") else u
        if full not in seen:
            seen.add(full)
            imgs.append(full)

    if not imgs:
        print("❌ 页面里没有 view12 的整页图——**站点结构可能变了，先读页面再改**")
        print(f"   页面 {len(html)} 字符。请检查 VIEW_IMG 正则。")
        return 1

    pid = args.id or re.sub(r"\D+", "", page.rsplit("/", 1)[-1])
    print(f"找到 {len(imgs)} 张整页图  (paperId={pid})")

    if args.list:
        for u in imgs:
            print("  ", u)
        return 0

    os.makedirs(args.out, exist_ok=True)
    ok, records = 0, []
    for i, u in enumerate(imgs, 1):
        ext = ".gif" if u.lower().endswith(".gif") else (
            ".png" if u.lower().endswith(".png") else ".jpg")
        dst = os.path.join(args.out, f"sh-{pid}-p{i:02d}{ext}")
        if os.path.exists(dst):
            print(f"  [{i}/{len(imgs)}] 已存在，跳过")
            ok += 1
            continue
        try:
            d = get(u, page, timeout=30, want_bytes=True)   # ← page 作 Referer
            w, h = img_size(d)
            with open(dst, "wb") as f:
                f.write(d)
            print(f"  [{i}/{len(imgs)}] ✅ {len(d):>7} 字节  {w}×{h}  "
                  f"→ {os.path.basename(dst)}")
            records.append({
                "file": os.path.relpath(dst, ROOT),
                "bytes": len(d), "width": w, "height": h,
                "source": page, "sourceImg": u,
            })
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(imgs)}] ❌ {str(e)[:50]}")
        time.sleep(0.4)

    if records:
        append_manifest(records, pid)
        print(f"\n✅ {ok}/{len(imgs)} 张已落盘，manifest 已登记 {len(records)} 条")
    print("⚠️ **落盘不代表完整**：请逐页读一遍确认页数与题目齐全。")
    return 0 if ok else 1


def append_manifest(records, pid):
    """登记进 raw-manifest.json（指纹由 tools/validate_schema.py 校验）。"""
    data = {"meta": {}, "items": []}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    items = data.setdefault("items", [])
    for r in records:
        p = os.path.join(ROOT, r["file"])
        if not os.path.exists(p):
            continue
        items.append({
            "file": r.get("file") or r.get("path"),
            "kind": "image",
            "bytes": r["bytes"],
            "sha256": sha256(p),
            "width": r["width"], "height": r["height"],
            "source": r["source"],
            "fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "note": f"人人文库原卷扫描 {pid}",
        })
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
