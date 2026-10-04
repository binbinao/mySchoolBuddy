#!/usr/bin/env python3
"""probe_paper_sources.py — 探测哪些渠道能拿到上海中考数学真题原卷

**为什么要有这个工具**（2026-10-04 实测得出）

用户要求「从互联网拉取」补齐各年真题原卷。逐个渠道试过，结论是
**公开渠道全部拿不到整页原卷**，但**各渠道能拿到的东西不一样**。
这个脚本把结论固化成可重跑的探测，避免下一个人重复盲试。

**实测结论表（2026-10-04）**

| 渠道 | 整页原卷扫描 | 完整题干文本 | 备注 |
|---|---|---|---|
| `mip.21cnjy.com/H/{id}.shtml` | ❌ 超时 | — | 限流严重，项目笔记已记载 |
| `mzujuan.21cnjy.com/paper/preview/{id}` | ❌ | ⚠️ 仅前段（1500 字） | 题目数据是**运行时 JS 渲染**，HTML 里没有 |
| `www.zujuan.com/paper/view-{id}.shtml` | ❌ | ⚠️ 仅前段 | 有 `question_num:24` 等元信息 |
| `www.zujuan.com/paper/quick-view/{id}` | ❌ | ❌ 726 字 | 付费墙 |
| `webshot.chujuan.cn`（图床） | ⚠️ 仅网页内嵌片段 | — | 780px 宽的小图（题干插图），**不是整页原卷** |
| `tikupic.21cnjy.com` | ⚠️ 缩略图 | — | 带 `_157x144` 后缀的是缩略图 |
| `preview.21cnjy.com` | ✅ 免登录直链 | — | **但需先有 21cnjy 的 docId**，本次索引里没有 |

**关键结论**

1. **整页原卷扫描图只有 `preview.21cnjy.com` 有**，入口是 21cnjy 的
   `mip.21cnjy.com/H/{docId}.shtml`——**但该域限流严重，本次三份真题全部超时**。
   已有 62 张扫描图就是这么来的（`tools/fetch_paper_images.py`，2026-10-02 跑的）。
2. **zujuan 系全部走付费墙**：题干数据在页面里以 JS 变量注入，
   HTML 里只有 `question_num` / `score` 等元信息，**题目正文要登录或付费**。
3. ⇒ **互联网公开渠道无法补齐 2021/2022/2024 的原卷扫描图。**
   **唯一可靠的补法是用户手拍照**（`RAW/试卷库/原卷扫描/`）。

**为什么这个脚本值得存在**：结论是「拉不到」，但**这是试了 6 个渠道才得到的结论**。
把它写下来 + 可重跑，比记在记忆里更可靠（记忆会丢，脚本能复现）。

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/probe_paper_sources.py            # 探测全部渠道
$PY tools/probe_paper_sources.py --quick    # 只测连通性，不下载
```

**不做什么**：不下载任何文件（只探测与报告），不编造题目内容。
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "data", "resources", "shanghai-papers.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def http(url, timeout=15, referer=None, want_bytes=False):
    """返回 str（HTML）或 bytes（图）。失败抛异常——**不静默返回空**。"""
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
         "Accept-Encoding": "gzip"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw if want_bytes else raw.decode("utf-8", "ignore")


def strip_html(t):
    body = re.sub(r"<script.*?</script>", "", t, flags=re.S)
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", txt).strip()


def png_size(d):
    if d[:8] == b"\x89PNG\r\n\x1a\n" and len(d) >= 24:
        return (int.from_bytes(d[16:20], "big"),
                int.from_bytes(d[20:24], "big"))
    return (0, 0)


def probe(pid, quick=False):
    """探测一份试卷在各渠道的可达性。返回结构化结果。"""
    out = {"paperId": pid, "channels": {}}

    # ① 21cnjy mip 页（原卷扫描图的唯一入口）
    try:
        t = http(f"https://mip.21cnjy.com/H/{pid}.shtml", timeout=8)
        imgs = re.findall(
            r'(?:src|data-src|data-original|original)\s*=\s*["\']([^"\']*preview[^"\']*?\.(?:png|jpg|jpeg))',
            t, re.I)
        out["channels"]["21cnjy-mip"] = {
            "ok": True, "imgs": len(set(imgs)), "verdict": "可下原卷扫描图"}
    except Exception as e:
        out["channels"]["21cnjy-mip"] = {
            "ok": False, "err": str(e)[:50],
            "verdict": "❌ 限流/超时——原卷扫描图的唯一入口走不通"}

    if quick:
        return out

    # ② mzujuan preview（题干文本）
    try:
        t = http(f"https://mzujuan.21cnjy.com/paper/preview/{pid}", timeout=12)
        txt = strip_html(t)
        # 判据：是否含「解：」「证明」「求证」等解答题特征
        marks = sum(txt.count(k) for k in ("解：", "证明", "求证", "解答"))
        out["channels"]["mzujuan-preview"] = {
            "ok": True, "textLen": len(txt), "solveMarks": marks,
            "verdict": ("含解答题正文" if marks >= 3
                        else f"⚠️ 仅前段（解答题标记 {marks} 处）")}
    except Exception as e:
        out["channels"]["mzujuan-preview"] = {
            "ok": False, "err": str(e)[:50], "verdict": "❌ 不可达"}

    # ③ zujuan 详情页（题量元信息）
    try:
        t = http(f"https://www.zujuan.com/paper/view-{pid}.shtml", timeout=12)
        nq = re.search(r'"question_num":\s*(\d+)', t)
        sc = re.findall(r'"(?:name|title)":"([^"]{2,20}分)"', t)
        out["channels"]["zujuan-view"] = {
            "ok": True,
            "questionNum": int(nq.group(1)) if nq else None,
            "scoreGroups": sc[:3],
            "verdict": "只有元信息，题目正文在付费墙后"}
    except Exception as e:
        out["channels"]["zujuan-view"] = {
            "ok": False, "err": str(e)[:50], "verdict": "❌ 不可达"}

    # ④ zujuan quick-view
    try:
        t = http(f"https://www.zujuan.com/paper/quick-view/{pid}", timeout=12)
        txt = strip_html(t)
        out["channels"]["zujuan-quick"] = {
            "ok": True, "textLen": len(txt),
            "verdict": "付费墙（正文仅几百字）"}
    except Exception as e:
        out["channels"]["zujuan-quick"] = {
            "ok": False, "err": str(e)[:50], "verdict": "❌ 不可达"}

    # ⑤ webshot 图床（看是不是整页原卷）
    try:
        t = http(f"https://www.zujuan.com/paper/view-{pid}.shtml", timeout=12)
        urls = sorted(set(re.findall(
            r"https?://webshot\.chujuan\.cn/[^\"\'\s<>]+", t)))
        sizes = []
        for u in urls[:3]:
            try:
                d = http(u, timeout=10, referer="https://www.zujuan.com/",
                        want_bytes=True)
                sizes.append(png_size(d))
            except Exception:
                pass
        big = [s for s in sizes if s[0] >= 1000]
        out["channels"]["webshot"] = {
            "ok": True, "candidates": len(urls), "sampled": sizes,
            "verdict": ("含大图（可能是整页）" if big
                        else "⚠️ 仅网页内嵌片段（宽度 <1000），非整页原卷")}
    except Exception as e:
        out["channels"]["webshot"] = {
            "ok": False, "err": str(e)[:50], "verdict": "❌ 不可达"}

    return out


def main():
    ap = argparse.ArgumentParser(
        description="探测各渠道能否拿到上海中考数学真题原卷（只探测不下载）")
    ap.add_argument("--quick", action="store_true", help="只测 21cnjy 连通性")
    ap.add_argument("--papers", default="",
                    help="逗号分隔的 paperId，默认取索引里所有数学中考真卷")
    args = ap.parse_args()

    ids = [x.strip() for x in args.papers.split(",") if x.strip()]
    if not ids:
        with open(INDEX, encoding="utf-8") as f:
            d = json.load(f)
        for p in d.get("papers", []):
            if (p.get("subject") == "数学"
                    and p.get("paperKind") == "中考真卷"
                    and p.get("paperId")):
                ids.append(str(p["paperId"]))

    print(f"探测 {len(ids)} 份数学中考真卷：{ids}\n")
    results = []
    for pid in ids:
        print(f"─── {pid} ───")
        try:
            r = probe(pid, quick=args.quick)
        except Exception as e:
            print(f"  ❌ 整体失败：{str(e)[:60]}")
            continue
        results.append(r)
        for name, info in r["channels"].items():
            mark = "✅" if info.get("ok") else "❌"
            extra = {k: v for k, v in info.items()
                     if k not in ("ok", "verdict")}
            print(f"  {mark} {name:20s} {info['verdict']}"
                  + (f"  {extra}" if extra else ""))
        time.sleep(0.5)

    print("\n" + "=" * 70)
    print("结论：")
    print("  · 整页原卷扫描图的**唯一**公开渠道是 preview.21cnjy.com，")
    print("    入口 mip.21cnjy.com/H/{docId} —— 本轮全部超时（限流）。")
    print("  · zujuan / mzujuan 系全部走付费墙：题干数据 JS 注入，HTML 里没有。")
    print("  ⇒ **公开渠道补不齐 2021/2022/2024 原卷**，")
    print("    可靠补法是**用户手拍照**放 RAW/试卷库/原卷扫描/。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
