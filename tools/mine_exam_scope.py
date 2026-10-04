#!/usr/bin/env python3
"""mine_exam_scope.py — 从已入库真题反推教材考点权重

**为什么需要这个工具**

`RAW/教材/` 的课本目录照片只覆盖**四科九上**，九下没拍到。而 `RAW/试卷库/*.txt`
里有 2021-2026 五套上海数学中考真题、两套物理真题——**真题考了什么，就是考什么**。

这是项目既有纪律的延伸：「同一事实在数据层与所有页面必须一致，打架时回原件裁决」。
课本目录是「教什么」，真题是「考什么」，**后者对复习更有决策价值**，
而前者才是「按什么顺序学」。

**核心方法：关键词词频 + 上下文抽验**

⚠️ **词频会骗人，必须两步走**（这是本工具最重要的一条）：

1. 先用正则统计词频，得到「哪些考点出现过」
2. **再逐个抽验上下文**——关键词命中不等于真有这个考点

反面实例（2026-10-04 实际踩到）：搜「向量」命中闵行一模 5 次，看起来像高频考点，
抽验后发现第 3 题四个选项确实在讲向量——**但这份卷子与孩子的教材对不上**
（孩子 2024 新教材已删向量，而一模卷仍考）。**词频高 ≠ 对她适用**。
所以本工具的输出必须区分「考过」与「适用于当前教材」，后者需要人工裁决。

**另一个已知限制：爬虫剥符号**

`sips`/`21cnjy` 抓来的 txt 里，数学公式的字母/符号被剥掉了
（`y = ax² + bx + c` 会变成 ` = 2 + + `），所以**本工具只能做词频定位，
不能读题**。要读题必须回看 `RAW/试卷库/原卷扫描/*.png` 原件。

🔴 **最重要的一条限制（2026-10-04 逐题复读后补记，此前漏了八年）**

**词频只能确认已知考点，不能发现未知考点。**

实例：2025 中考第 22 题（10 分，「分割梯形 → 旋转 180° → 拼成等腰三角形」
的表述型设计题）**整题不在现有词表任何一条里**，而本工具从头到尾没报过它——
因为「旋转」「分割」「拼接」这些词在别的卷里也出现，权重低，
**根本进不了前列**。

⇒ **本工具的输出是「已知考点的权重」，不是「考点全集」。**
用它反推出的章号清单**必然漏题型类新考点**，尤其：
表述型/设计型/新定义型/综合实践型题目——这些题没有稳定的高频关键词。

**所以发现新考点只有一条路：逐题读卷面**（本工具做不到，必须人读）。
本工具的正当用途是**给已知的考点排权重**，仅此而已。

**用法**

```bash
tools/mine_exam_scope.py               # 看词频矩阵
tools/mine_exam_scope.py --subject 物理 --verify 向量,压强
tools/mine_exam_scope.py --json        # 机器可读
```
"""

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 考点词典：(正则, 归一名, 册次归属, 适用性备注)
# ⚠️ 「适用性」一栏是本工具最容易出错的地方，见文件头 docstring 的反面实例
PROBES = {
    "数学": [
        (r"⊙|外接圆|内切圆|圆心|半径|弦|弧|切线|圆周角|圆心角|正五边形|正多边形",
         "圆", "九下待定", "2025 中考第 6/18/23 题均涉圆；九上第 28 章只到相似三角形，圆必属九下"),
        (r"概率|随机|抽取|摸球|不重复|可能性",
         "概率", "九下待定", "2025 中考第 13 题为概率；旧教材归「随机事件的概率」"),
        (r"方差|标准差|中位数|众数|平均数|统计|样本|总体|分布|扇形图",
         "统计", "九下待定", "2025 中考第 4/15 题；旧教材归「抽样与数据分析」"),
        (r"反比例|二次函数|抛物线|顶点|解析式|平移",
         "函数", "九上 27 章", "✅ 课本照片已定，章号可靠"),
        (r"相似|位似|成比例|平行线分",
         "相似三角形", "九上 28 章", "✅ 课本照片已定，章号可靠"),
        (r"锐角|正弦|余弦|正切|解直角|仰角|俯角|坡度",
         "锐角三角比", "九上 29 章", "✅ 课本照片已定，章号可靠"),
        (r"三视图|投影|主视图|左视图|俯视图|立体模型",
         "投影与视图", "九上 30 章", "✅ 课本照片已定，章号可靠"),
        (r"向量|平面向量",
         "向量", "❌ 已确认不考", "2026-10-04 **孩子明确反馈不考** → 本条已裁决，"
         "不是待办。真题命中（2025 一模 3 处/中考 1 处）属往年口径，仅留痕不采信"),
        (r"方程|不等式|因式分解|分式|二次根|绝对值|整式",
         "数与代数", "六年级起累积", "跨章节，贯穿全卷"),
        (r"四边形|平行四边形|矩形|菱形|正方形|梯形|中位线|全等|旋转|平移",
         "图形与几何", "八下累积 + 九上 30 章", "压轴题主要战场"),
    ],
    "物理": [
        (r"压强|大气压|液体压强|浮力|漂浮|沉底|排开",
         "压强与浮力", "九下（待确认）", "2025/2026 中考均高频，中考物理大头"),
        (r"杠杆|支点|动力|阻力|力臂|滑轮|机械效率|做功",
         "杠杆与机械", "九下（待确认）", "中考必考，常与压强浮力组合"),
        (r"惯性|摩擦力|重力|弹力|受力分析|二力平衡",
         "力学基础", "八下累积", "2026 中考命中最多"),
        (r"欧姆|电阻|电压|电流|串联|并联|电功率|焦耳|电能表",
         "电学", "九上 10-13 章", "✅ 课本照片已定；**词频最高，九上必须吃透**"),
        (r"比热容|内能|热机|热量|熔点|沸点|比热",
         "热学", "九上 10 章", "✅ 课本照片已定"),
        (r"光的|折射|反射|凸透镜|焦距|成像|速度|光速",
         "光学", "八上累积", "高频但已学过"),
        (r"质量|密度|物质|天平|量筒|量杯",
         "质量与密度", "八上累积", "基础"),
    ],
}


def load(subject):
    return sorted(glob.glob(os.path.join(ROOT, "RAW", "试卷库", f"*{subject}*.txt")))


def read(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def verify(subject, paths, keyword, window=90):
    """抽验关键词上下文——词频会骗人，这一步不能省。"""
    out = []
    for p in paths:
        s = read(p)
        for m in re.finditer(re.escape(keyword), s):
            a, b = max(0, m.start() - window), min(len(s), m.end() + window)
            out.append({"file": os.path.basename(p), "offset": m.start(),
                        "context": s[a:b].replace("\n", " ⏎ ")})
    return out


def main():
    ap = argparse.ArgumentParser(description="从已入库真题反推教材考点权重")
    ap.add_argument("--subject", default="数学", choices=sorted(PROBES))
    ap.add_argument("--verify", help="逗号分隔的关键词，抽验上下文")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    paths = load(args.subject)

    if args.verify:
        for kw in [k.strip() for k in args.verify.split(",") if k.strip()]:
            hits = verify(args.subject, paths, kw)
            print(f"\n{'='*72}\n「{kw}」命中 {len(hits)} 处")
            for h in hits[:6]:
                print(f"  ▸ {h['file'][:44]} @{h['offset']}")
                print(f"    …{h['context']}…")
        return 0

    probes = PROBES[args.subject]
    matrix = []
    for pat, name, term, note in probes:
        counts = [len(re.findall(pat, read(p))) for p in paths]
        matrix.append({"name": name, "term": term, "note": note,
                       "counts": counts, "total": sum(counts)})

    if args.json:
        print(json.dumps({"subject": args.subject,
                          "files": [os.path.basename(p) for p in paths],
                          "matrix": matrix}, ensure_ascii=False, indent=2))
        return 0

    print(f"== {args.subject}真题考点反推 · {len(paths)} 套 ==\n")
    hdr = f"{'考点':<12}{'归属':<16}" + "".join(f"{os.path.basename(p)[6:20]:<16}" for p in paths)
    print(hdr)
    print("-" * len(hdr))
    for m in matrix:
        print(f"{m['name']:<12}{m['term']:<16}"
              + "".join(f"{c:<16}" for c in m["counts"]) + f"合计 {m['total']}")
    print("\n📌 已知限制：爬虫剥掉了数学公式符号（y=ax²+bx+c → = 2 + + ），")
    print("   所以本表只能做词频定位，**不能替代读题**。要读题回看 RAW/试卷库/原卷扫描/*.png。")
    print("\n📌 词频 ≠ 对她适用。判定顺序：课本目录照片（教什么）→ 考纲（考什么）→ 真题（往年考过什么）。")
    print("   三者不一致时以考纲为准，但考纲通常不公开——此时以课本为准，真题只作难度参考。")
    for m in matrix:
        if "⚠" in m["term"] or "待确认" in m["term"]:
            print(f"\n⚠️  {m['name']}（{m['term']}）：{m['note']}")
        elif "不考" in m["term"]:
            print(f"\n❌ {m['name']}（{m['term']}）：{m['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
