#!/usr/bin/env python3
"""build_word_materials.py — 从 data/ 生成 Word 练习材料

**三类材料**（2026-10-04 用户定义）

| 类型 | 目录 | 原料 | 用途 |
|---|---|---|---|
| 错题总结与加练 | `docs/材料/错题加练/` | `data/wrong/*.json` 的 variants + thinkQuestions | 把错过的题重做一遍 |
| 专题学习卷 | `docs/材料/专题学习卷/` | 词表 + 卷库真题 | 按考点成卷 |
| 综合考察卷 | `docs/材料/综合卷/` | 旧卷打乱重组 + AI 新编 | 阶段性检测 |

**先出「练习卷版」，讲义版等孩子做完拍照上传后再生成**

这不是偷懒，是**流程顺序问题**：讲义版是「答案 + 讲解」，
应该写在她**已经暴露了卡点之后**——先看她的解答，才能知道该讲哪一步。
先出讲义版就是猜她哪里不会，猜错了白写。

**版式约定（练习卷版）**

- **题目卷**（`*-练习卷.docx`）：只给题 + 答题区，**不含答案**
- **答案卷**（`*-答案.docx`）：答案 + 关键步骤 + 变式设计意图
- 两者必须**同源生成**——从同一份 JSON 渲染，不可能漂移
- A4 / 1.5 倍行距 / 姓名班级学号栏 / 每题留足书写空间

**依赖**：`python-docx`，装在隔离 venv
`/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python`
项目不引入全局依赖，venv 与 `tools/README.md` 一起记。

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/build_word_materials.py                    # 全部
$PY tools/build_word_materials.py --type 错题加练      # 只出一类
$PY tools/build_word_materials.py --dry-run
$PY tools/build_word_materials.py --list             # 看会生成什么
```

**不做什么（硬边界，与其他生成器一致）**

- ❌ 不做教学判断：解法步骤、诊断结论、变式设计意图都从 JSON 读，不自己编
- ❌ 不改 `data/`：只读
- ❌ 不生成讲义版：等孩子做完、拍照上传、有真实卡点证据后再说
"""

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, "docs", "材料")

SUBJ_DIR = {"数学": "数学", "英语": "英语", "物理": "物理", "道法": "道德与法治"}


# ══ 排版基元 ══════════════════════════════════════════════════

def _cjk(run, font="宋体"):
    """中文必须单独设 rFonts，否则 Word 里回退到默认字体。"""
    from docx.oxml.ns import qn
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def setup(doc):
    """A4 + 中文字体 + 1.5 倍行距。返回可用样式名。"""
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn

    for s in doc.sections:
        s.page_width, s.page_height = Cm(21.0), Cm(29.7)
        s.top_margin = s.bottom_margin = Cm(2.0)
        s.left_margin = s.right_margin = Cm(2.2)

    n = doc.styles["Normal"]
    n.font.name = "Times New Roman"
    n.font.size = Pt(10.5)
    n.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    pf = n.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.5
    pf.space_after = Pt(3)
    return doc


def para(doc, text="", size=10.5, bold=False, font="宋体", align=None,
         before=0, after=3, indent=None, color=None, spacing=1.5):
    """写一个段落。`**粗体**` 会被解析成真加粗（不是字面星号）。"""
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    p = doc.add_paragraph()
    if align:
        p.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER,
                       "right": WD_ALIGN_PARAGRAPH.RIGHT,
                       "left": WD_ALIGN_PARAGRAPH.LEFT,
                       "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[align]
    pf = p.paragraph_format
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = spacing
    if indent:
        pf.left_indent = Pt(indent)
    for seg, strong in _split_bold(text):
        if not seg:
            continue
        r = p.add_run(seg)
        r.font.size = Pt(size)
        r.bold = bold or strong
        if color:
            r.font.color.rgb = RGBColor(*color)
        _cjk(r, font)
    return p


def _split_bold(text):
    """把 `**x**` 切成 [(片段, 是否加粗)]。**只处理成对的**，落单的星号原样保留。"""
    out, i = [], 0
    while True:
        a = text.find("**", i)
        if a < 0:
            break
        b = text.find("**", a + 2)
        if b < 0:                      # 落单 ⇒ 当普通字符
            break
        if a > i:
            out.append((text[i:a], False))
        out.append((text[a + 2:b], True))
        i = b + 2
    if i < len(text):
        out.append((text[i:], False))
    return out or [(text, False)]


def answer_area(doc, lines=6, label="答："):
    """留出真实书写空间——练习卷没有答题区等于没法做。"""
    para(doc, label, size=9.5, color=(0x99, 0x99, 0x99), before=1, after=1)
    for _ in range(lines):
        para(doc, "　" * 38, size=10.5, after=0, spacing=1.6)


def page_break(doc):
    doc.add_page_break()


def hr(doc, char="─"):
    para(doc, char * 46, size=9, color=(0xCC, 0xCC, 0xCC), after=4, spacing=1.0)


# ══ 抬头 ══════════════════════════════════════════════════════

def header_block(doc, title, subtitle, meta_lines):
    para(doc, title, size=16, bold=True, align="center", after=2, spacing=1.2)
    para(doc, subtitle, size=10, color=(0x66, 0x66, 0x66), align="center", after=8, spacing=1.2)
    para(doc, "姓名：______________　　班级：________　　日期：______________",
         size=10, after=2)
    for m in meta_lines:
        para(doc, m, size=9.5, color=(0x88, 0x88, 0x88), after=1, spacing=1.3)
    hr(doc)


def notice(doc, lines):
    """开卷提示。练习卷必须说清「不许带什么」和「不会怎么办」。"""
    para(doc, "作答说明", size=11, bold=True, font="黑体", before=2, after=2)
    for i, t in enumerate(lines, 1):
        para(doc, f"{i}. {t}", size=9.5, after=1, indent=10, spacing=1.3)


# ══ ① 错题加练 ═══════════════════════════════════════════════

def build_wrong_drills(items, dry=False):
    """一条错题 = 一节：3 道变式 + 5 道想题。

    变式固定配比（MEMORY 五段范式硬性要求）：简单变式 + 同类型 + **问答对调**。
    第三题是「已知与所求互换」，很多孩子是记住套路不是真懂，只有对调能测出来。
    """
    for it in items:
        subject = SUBJ_DIR.get(it.get("subject"), it.get("subject") or "综合")
        qno = it.get("qno")
        outdir = os.path.join(OUT_ROOT, "错题加练", subject)
        base = f"{subject}-错题加练-q{qno}-{it.get('module') or ''}".replace(" ", "")

        vars_ = it.get("variants") or []
        tq = it.get("thinkQuestions") or []
        # 断点只在**学生卷**给「卡在哪一步」，不给完整诊断——
        # `breakpoint` 是 200 字左右的完整分析（含隐藏坑），
        # 放学生卷既是剧透又冗长，留在答案卷里讲。
        bp_step = ""
        for s_ in it.get("steps") or []:
            if s_.get("breakpoint"):
                bp_step = re.sub(r"^★\s*", "", str(s_.get("action") or ""))
                break

        # ── 题目卷 ──
        doc = setup(__import__("docx").Document())
        header_block(
            doc,
            f"错题重做与加练 · 第 {qno} 题",
            f"{it.get('module') or ''}　{it.get('point') or ''}",
            [f"原题错因：{it.get('cause') or '未定论'}"
             + (f"（副因：{it['causeSecondary']}）" if it.get("causeSecondary") else ""),
             (f"上次就卡在这里：{bp_step}" if bp_step else "上次就卡在这一题"),
             f"本卷共 {len(vars_)} 道加练 + {len(tq)} 道想题，做完对照答案卷"],
        )
        notice(doc, [
            "**先遮住答案独立做**。做不出来就空着，不要翻过程。",
            "每道题都要写**完整过程**——只写答案等于没做，这是本项目反复强调的铁律。",
            "想五题要**写出理由**，不只写结论。",
            "做完后拍照上传，我们会据此生成讲义版（针对你实际卡住的那一步）。",
        ])

        para(doc, f"一、加练题（{len(vars_)} 道，含一道问答对调）", size=12, bold=True,
             font="黑体", before=6, after=3)
        for i, v in enumerate(vars_, 1):
            para(doc, f"第 {i} 题　【{v.get('type') or ''}】", size=10, bold=True, before=4, after=2)
            para(doc, str(v.get("stem") or ""), size=10.5, after=2, spacing=1.6)
            answer_area(doc, lines=9 if len(str(v.get("stem") or "")) < 120 else 14)

        tq = it.get("thinkQuestions") or []
        para(doc, "二、想五题（写下理由，不只写结论）", size=12, bold=True,
             font="黑体", before=8, after=3)
        for i, t in enumerate(tq, 1):
            para(doc, f"{i}. {t.get('q')}", size=10.5, after=1, before=2, spacing=1.5)
            answer_area(doc, lines=3 if "★" not in str(t.get("q")) else 5)

        page_break(doc)
        para(doc, "做完后自查", size=11, bold=True, font="黑体", after=2)
        for t in ["□ 三道加练题都写出了完整过程（不是只写答案）",
                  "□ 能说清「为什么第一步这样想」",
                  "□ 想五题第 5 题（方法失效的边界）我想过",
                  "□ 我把做错/做不出来的题号记下来了：__________"]:
            para(doc, t, size=10, after=2, indent=10)

        paper = os.path.join(outdir, f"{base}-练习卷.docx")

        # ── 答案卷 ──
        adoc = setup(__import__("docx").Document())
        header_block(adoc, f"答案与讲评 · 第 {qno} 题",
                     f"{it.get('module') or ''}　{it.get('point') or ''}",
                     [f"对应练习卷：{base}-练习卷.docx",
                      "**先自己判分，再看这个**。全对的题标 ✓，卡住的题标 ✗。"])
        if it.get("breakpoint"):
            para(adoc, "原题断点（完整分析）", size=11, bold=True, font="黑体",
                 before=4, after=2)
            para(adoc, str(it["breakpoint"]), size=9.5, color=(0x55, 0x55, 0x55),
                 after=3, spacing=1.5)
        para(adoc, "一、加练题答案", size=12, bold=True, font="黑体", before=6, after=3)
        for i, v in enumerate(vars_, 1):
            para(adoc, f"第 {i} 题【{v.get('type') or ''}】", size=10, bold=True, before=4, after=2)
            para(adoc, f"答案：{v.get('answer') or '—'}", size=10.5, after=2, spacing=1.5)
            if v.get("change"):
                para(adoc, f"改了什么：{v['change']}", size=9.5, after=1, indent=10,
                     color=(0x66, 0x66, 0x66), spacing=1.4)
            if v.get("hint"):
                para(adoc, f"提示：{v['hint']}", size=9.5, after=2, indent=10,
                     color=(0x33, 0x66, 0x99), spacing=1.4)
            if "对调" in str(v.get("type")):
                para(adoc, "⚠ 这道是「问答对调」，答案是「条件不足」——"
                          "如果她算出了具体数值，说明套的是原题流程而不是真懂。",
                     size=9.5, after=2, indent=10, color=(0xB0, 0x50, 0x00), spacing=1.4)
            hr(adoc)

        para(adoc, "二、想五题参考答案", size=12, bold=True, font="黑体", before=8, after=3)
        for i, t in enumerate(tq, 1):
            para(adoc, f"{i}. {t.get('q')}", size=10, after=1, before=2, spacing=1.4)
            para(adoc, f"　要点：{t.get('answer') or '—'}", size=9.5, after=2, indent=12,
                 color=(0x33, 0x66, 0x99), spacing=1.4)

        apaper = os.path.join(outdir, f"{base}-答案.docx")

        if not dry:
            os.makedirs(outdir, exist_ok=True)
            doc.save(paper)
            adoc.save(apaper)
        yield {"type": "错题加练", "subject": subject,
               "paper": os.path.relpath(paper, ROOT), "answer": os.path.relpath(apaper, ROOT),
               "n_var": len(vars_), "n_tq": len(tq)}


# ══ 主流程 ════════════════════════════════════════════════════

def load_wrong():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        for it in d.get("items", []):
            if isinstance(it, dict) and (it.get("variants") or it.get("thinkQuestions")):
                out.append(it)
    return out


def main():
    ap = argparse.ArgumentParser(description="生成 Word 练习材料")
    ap.add_argument("--type", default="全部", help="错题加练 / 专题学习卷 / 综合卷 / 全部")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    made = []
    if args.type in ("全部", "错题加练"):
        made += list(build_wrong_drills(load_wrong(), args.dry_run))

    if args.type in ("全部", "专题学习卷", "综合卷"):
        print(f"⏸  {args.type} 需要知识点词表通过审核后才有内容可出"
              "（词表在 docs/教材版本/知识点词表-待审.md，仍是待审状态）",
              file=sys.stderr)

    if args.list or args.dry_run:
        for m in made:
            print(f"[{'dry-run' if args.dry_run else 'list'}] {m['type']} · {m['subject']}　"
                  f"加练 {m['n_var']} 道 + 想题 {m['n_tq']} 道")
            print(f"      题目卷 → {m['paper']}")
            print(f"      答案卷 → {m['answer']}")
        if args.dry_run:
            print("\n[dry-run] 未写盘")
        return 0

    if not made:
        print("没有可生成的内容。")
        return 0
    for m in made:
        print(f"✅ {m['type']} · {m['subject']}")
        print(f"   题目卷 → {m['paper']}")
        print(f"   答案卷 → {m['answer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ══ 交付前自检（防「答案写进学生卷」）════════════════════════
#
# **为什么这道闸门必须有**（2026-10-04 实际踩到）：
# 答案卷区块里的 11 处 `para(doc, ...)` 全部漏写成 `doc` 而不是 `adoc`，
# 结果**答案、提示、要点、断点全写进了学生卷**——练习卷失去全部意义，
# 而 docx 文件大小、段落数、打开效果全都正常，**肉眼扫一眼发现不了**。
#
# 形态上属于闸门纪律的 ①「把错误藏起来」+ ②「检查本身抓不到」：
# 靠 `grep '答案：'` 在源码里查不到（源码是对的，变量传错了），
# 只能**产出后打开文件读内容**才看得见。
#
# 所以：**任何「练习卷/试卷」类产物，交付前必须跑这个检查。**

FORBIDDEN_IN_PAPER = ["答案：", "要点：", "改了什么", "提示：",
                      "原题断点（完整分析）", "⚠ 这道是"]


def selfcheck(papers_dir=None):
    """检查所有 *-练习卷.docx 不含答案、且 *-答案.docx 确实含答案。"""
    from docx import Document
    papers_dir = papers_dir or os.path.join(OUT_ROOT, "错题加练")
    bad = []
    n_p = n_a = 0
    for dirpath, _, files in os.walk(papers_dir):
        for fn in files:
            if not fn.endswith(".docx"):
                continue
            full = os.path.join(dirpath, fn)
            txt = "\n".join(p.text for p in Document(full).paragraphs)
            is_paper = fn.endswith("-练习卷.docx")
            if is_paper:
                n_p += 1
                hit = {k: txt.count(k) for k in FORBIDDEN_IN_PAPER if k in txt}
                if hit:
                    bad.append((full, f"练习卷泄露答案：{hit}"))
            elif fn.endswith("-答案.docx"):
                n_a += 1
                if "答案：" not in txt:
                    bad.append((full, "答案卷里没有答案"))
    return n_p, n_a, bad
