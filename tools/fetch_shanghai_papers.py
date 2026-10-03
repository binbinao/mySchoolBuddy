#!/usr/bin/env python3
"""fetch_shanghai_papers.py — 上海中考试卷资源索引构建 + 可见全文归档

为什么分两层：
  试卷全文大多在付费平台（学科网/21cnjy下载页/网盘），脚本无法直下。
  但 zy.21cnjy.com 与 renrendoc.com 的**预览页包含完整题目文本**，
  可以真实抓取。因此本脚本只做两件确定的事，不做任何教学判断：

  1) 抓取公开可见的试卷全文 → RAW/试卷库/（算 sha256，登记 manifest）
  2) 生成结构化索引 → data/resources/shanghai-papers.json

绝不编造：抓不到的只记元数据 + 标记 access，不写任何题目内容或分值。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "RAW", "试卷库")
INDEX = os.path.join(ROOT, "data", "resources", "shanghai-papers.json")
MANIFEST = os.path.join(ROOT, "data", "raw-manifest.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:      # 站点限流会随机超时，退避重试
            last = e
            time.sleep(2 + i * 3)
    raise last


def strip_html(html):
    """抽取预览页正文。

    这些站点的正文在 `assets-intro` / `entry-content` 容器里，其余部分是
    导航、相关推荐、页脚。全页 strip 会把噪音一起算进来，导致字数统计失真
    ——反过来，若正文本身较短（区域卷），仅按总字数判断会误杀。
    所以先定位容器，定位不到再退回全页。
    """
    m = re.search(r'(?is)<div[^>]*class="[^"]*assets-intro[^"]*"[^>]*>(.*?)'
                  r'(?=<div[^>]*class="[^"]*(?:fixed-detail|side|recommend|related))', html)
    if not m:
        m = re.search(r'(?is)<div[^>]*class="[^"]*entry-content[^"]*"[^>]*>(.*?)$', html)
    if m:
        html = m.group(1)
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h\d)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", "", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&ensp;", " "), ("&emsp;", " ")):
        html = html.replace(a, b)
    html = re.sub(r"[ \t\xa0]+", " ", html)
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html)
    return html.strip()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(s, limit=60):
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", s).strip("-")
    return s[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只抓这些 sourceId，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fetch", action="store_true", help="只更新索引，不抓全文")
    a = ap.parse_args()

    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "shanghai_papers_source.json")
    with open(src_path, encoding="utf-8") as f:
        src = json.load(f)

    only = set(a.only.split(",")) if a.only else None
    all_src = [s for s in src["papers"] if not s.get("discard")]
    items = [s for s in all_src if not only or s["id"] in only]

    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)

    # 增量合并：--only 只处理部分条目时，不能把索引里其他条目冲掉。
    # 2026-10-02 实测踩过这个坑：单条重跑导致索引从 16 条塌成 1 条。
    prev = {}
    if os.path.exists(INDEX):
        try:
            with open(INDEX, encoding="utf-8") as f:
                for r in json.load(f).get("papers", []):
                    prev[r["id"]] = r
        except (json.JSONDecodeError, KeyError):
            pass

    records = []
    # 2026-10-03 第七次核验修：原 stats 是「本次运行增量」（fetched/metaOnly/failed
    # 累加，另有 unchanged 复用旧文件），落盘后与 meta.count（累积套数）语义冲突：
    # 出现 count=21 而 stats 之和=17、fetched=0 但实际已抓 11 份的情况。
    # 增量数落盘即失去意义（下次运行就变了），且与总量并排必被误读。
    # 改为：stats 一律由 records 现算，三项之和恒等于 count。
    # 增量的用途（看本次抓了多少）改由本函数返回值/日志承担，不再落盘。
    stats = {"fetched": 0, "metaOnly": 0, "failed": 0, "unchanged": 0}  # 本次运行增量，仅供打印

    for s in items:
        rec = {
            "id": s["id"],
            "year": s["year"],
            "session": s["session"],       # 中考 / 一模 / 二模 / 期末
            "region": s["region"],         # 全市 / 区名
            "subject": s["subject"],
            "title": s["title"],
            "sourceSite": s["site"],
            "sourceUrl": s["url"],
            "access": s.get("access", "unknown"),
            "accessNote": s.get("accessNote", ""),
            "curriculumNote": s.get("curriculumNote", ""),
            # 字段名是 trustLevel（Level 前只有一个 t）。历史写成 trusthLevel，
            # 而 scan_zujuan_papers.py 用的是正确拼写，两边写同一个 JSON →
            # 采集一次就让索引里同时出现两种键，页面读 trusthLevel 时新条目静默失效。
            "trustLevel": s.get("trust", "unverified"),
            "trustNote": s.get("trustNote", ""),
            "hasAnswer": s.get("hasAnswer", "unknown"),
            "rawFile": None,
            "chars": 0,
            "fetchedAt": None,
        }
        # 已归档过的条目复用上次的 rawFile，避免索引退化成空壳
        if s["id"] in prev and prev[s["id"]].get("rawFile"):
            old = prev[s["id"]]
            if os.path.exists(os.path.join(ROOT, old["rawFile"])):
                rec["rawFile"] = old["rawFile"]
                rec["chars"] = old.get("chars", 0)
                rec["fetchedAt"] = old.get("fetchedAt")

        if not a.no_fetch and s.get("fetchable"):
            try:
                html = fetch(s["url"])
                text = strip_html(html)
                if len(text) < s.get("minChars", 1500):
                    raise ValueError(f"正文过短（{len(text)} 字符），疑似未渲染")
                fn = f"{s['year']}-{s['session']}-{s['region']}-{s['subject']}-{slugify(s['title'])}.txt"
                path = os.path.join(RAW_DIR, fn)

                # ⚠️ 原件不可变：文件头带抓取时间，每次重写都会改变 sha256，
                # 触发 validate_schema 的「原件被改动」红线（2026-10-02 实测踩过）。
                # 做法：内容指纹（不含时间戳）一致就跳过写入，只补登记。
                body = text
                cached = prev.get(s["id"], {}).get("bodySha")
                if cached and cached == hashlib.sha256(body.encode()).hexdigest() \
                        and os.path.exists(path):
                    rec["rawFile"] = f"RAW/试卷库/{fn}"
                    rec["chars"] = len(body)
                    rec["fetchedAt"] = prev[s["id"]].get("fetchedAt")
                    stats["unchanged"] += 1
                    print(f"  ＝ {s['id']} 内容未变，复用已归档 {len(body)} 字")
                else:
                    if not a.dry_run:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(f"# {s['title']}\n")
                            f.write(f"# 来源：{s['url']}（{s['site']}）\n")
                            f.write(f"# 抓取时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                            f.write(f"# 说明：{s.get('accessNote','')}\n")
                            f.write("=" * 60 + "\n\n")
                            f.write(text)
                    rec["rawFile"] = f"RAW/试卷库/{fn}"
                    rec["chars"] = len(body)
                    rec["fetchedAt"] = datetime.now().strftime("%Y-%m-%d")
                    rec["bodySha"] = hashlib.sha256(body.encode()).hexdigest()
                    stats["fetched"] += 1
                    print(f"  ✅ {s['id']} {len(body)} 字  {s['title'][:40]}")
            except Exception as e:
                rec["fetchError"] = str(e)[:120]
                stats["failed"] += 1
                print(f"  ⚠️  {s['id']} 抓取失败：{str(e)[:70]}")
            time.sleep(1.2)
        else:
            stats["metaOnly"] += 1

        records.append(rec)

    if a.dry_run:
        print(f"\n[dry-run] fetched={stats['fetched']} metaOnly={stats['metaOnly']} failed={stats['failed']}")
        return

    # 合并：本次处理的条目覆盖旧记录，未处理的原样保留
    if only:
        merged = {r["id"]: r for r in records}
        for pid, old in prev.items():
            merged.setdefault(pid, old)
        order = {s["id"]: i for i, s in enumerate(all_src)}
        records = sorted(merged.values(), key=lambda r: order.get(r["id"], 9999))
    records = [r for r in records if not r.get("discard")]

    # 落盘的 stats 一律现算（2026-10-03 第七次核验），增量 stats 只用于上面的打印。
    # merged 模式下 records 含本次未处理的旧记录，只有现算才能得到真实的全库库存。
    stats_onfile = {"fetched": 0, "metaOnly": 0, "failed": 0}
    for r in records:
        if r.get("rawFile"):
            stats_onfile["fetched"] += 1
        elif r.get("fetchError"):
            stats_onfile["failed"] += 1
        else:
            stats_onfile["metaOnly"] += 1
    stats_onfile["note"] = ("由 papers 数组现算，非单次运行增量，三项之和恒等于 count："
                            "fetched=有 rawFile 的条数，failed=有 fetchError 的条数，"
                            "metaOnly=其余（尚未抓取全文）")

    # 写索引
    doc = {
        "meta": {
            "schema": "resource-index.v1",
            "note": ("上海市中考/一模/二模试卷资源索引。**这不是孩子参加过的考试记录**——"
                     "公共教辅资源，不进 data/exams/。rawFile 指向已归档的公开可见全文；"
                     "access=paywall/login 表示需人工到平台下载后放 RAW/试卷库/。"),
            "created": datetime.now().strftime("%Y-%m-%d"),
            "count": len(records),
            "stats": stats_onfile,
            "discipline": "索引只记可核实元数据；题目内容一律来自实际抓取，不编造。",
        },
        "shanghaiPolicy": {
            "总分": 750,
            "科目分值": {
                "语文": "150（闭卷100分钟）",
                "数学": "150（闭卷100分钟）",
                "外语": "150（笔试140含听力25 + 听说测试10）",
                "道德与法治": "60（统一考试30开卷40分钟 + 日常考核30）",
                "历史": "60（统一考试30开卷40分钟 + 日常考核30）",
                "综合测试": "150（物理70 + 化学50 + 跨学科案例分析15 + 实验操作15）",
                "体育与健身": "30（日常考核15 + 统一测试15）",
            },
            "录取批次": ["自主招生录取", "名额分配综合评价录取（到区/到校）", "统一招生录取（1至15志愿）"],
            "名额分配同分排序": ["综合素质评价", "语数外三科合计", "数学", "语文", "综合测试"],
            "来源": "上海市教育考试院 沪教考院中招〔2026〕3号 + 沪教委基〔2026〕2号",
            "note": "2026 年政策与 2025 年一致。分数线/政策须以市教委与市教育考试院最新文件为准。",
        },
        "regions": sorted({r["region"] for r in records}),
        "subjects": sorted({r["subject"] for r in records}),
        "papers": records,
    }
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 索引写入 {INDEX}")
    print(f"   新增归档 {stats['fetched']} · 复用 {stats['unchanged']} · "
          f"仅元数据 {stats['metaOnly']} · 失败 {stats['failed']}")

    # 登记 manifest
    if stats["fetched"]:
        with open(MANIFEST, encoding="utf-8") as f:
            man = json.load(f)
        known = {i["file"] for i in man["items"]}
        added = 0
        for r in records:
            if not r["rawFile"] or r["rawFile"] in known:
                continue
            ap_ = os.path.join(ROOT, r["rawFile"])
            if not os.path.exists(ap_):
                continue
            man["items"].append({
                "file": r["rawFile"],
                "kind": "text",
                "bytes": os.path.getsize(ap_),
                "sha256": sha256_of(ap_),
                "status": "已入库·公开教辅",
                "parsed": {"wrongIds": [], "modules": []},
            })
            added += 1
        man["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
        print(f"   manifest 新增 {added} 条")


if __name__ == "__main__":
    main()
