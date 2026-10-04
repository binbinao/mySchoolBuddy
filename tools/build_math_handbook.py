#!/usr/bin/env python3
"""build_math_handbook.py — 从 data/resources/math-keypoints.json 渲染 Word 速查手册

**这个脚本不生产任何教学内容**

要点内容全部来自 `data/resources/math-keypoints.json`（人工维护），
脚本只负责排版。**任何解法步骤、诊断结论、易错提醒都不许写进这个文件**——
理由与其他生成器一致：解法写错了比没有更危险。

**为什么单独一个脚本，不并进 build_word_materials.py**
后者是「练习卷/答案卷」，硬边界是**学生卷不能泄露答案**；
本脚本产出的是「速查手册」——她就是要翻着看答案的，两者风险方向相反。
混在一个文件里，那道 `selfcheck()` 闸门的语义会变模糊。

**版式约定**

- A4 / 1.35 倍行距（手册比练习卷密，单倍行距会太挤）
- 四个 tier 用**色块标题**区分，颜色从钛金灰暖金体系取
- 「核心结论」/「易错提醒」/「自检问」三类内容**样式必须视觉可辨**——
  手册的价值在于「翻到易错提醒那一栏」，三栏长得一样就等于没有易错提醒
- 每条知识点末尾给一条 `selfcheck`，是她自己判断过没过关的标准

**依赖**：`python-docx`，隔离 venv
`/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python`

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/build_math_handbook.py
$PY tools/build_math_handbook.py --dry-run
$PY tools/build_math_handbook.py --check     # 只跑结构闸门，不写盘
```

**不做什么（硬边界）**

- ❌ 不改 `data/`：只读
- ❌ 不生成练习题：本册是「查」的材料，题在错题加练与专题学习卷里
- ❌ 不补内容：JSON 里标了 `null` 的（如九下章号）就印成「待定」，
  **绝不因为「看起来应该是第几章」而填一个猜的值**
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "resources", "math-keypoints.json")
OUT = os.path.join(ROOT, "docs", "材料", "学习要点", "数学-学习要点速查手册.docx")

# ══ 视觉体系（钛金灰 + 暖金，与项目既有 HTML 一致）═════════════════

CINK = (0x1C, 0x1C, 0x1E)        # 钛黑 · 正文标题
CSILVER = (0xA1, 0xA1, 0xAA)     # 银灰 · 次要信息
CGOLD = (0xB4, 0x6E, 0x00)       # 暖金加深 · tier1（打印比 #F59E0B 更清楚）
CCORE = (0x1A, 0x1A, 0x1C)       # 结论正文近黑
CPIT = (0x8A, 0x3A, 0x00)        # 砖红 · 易错提醒（不用正红，打印不刺眼）
CQ = (0x2B, 0x4A, 0x6B)         # 深蓝 · 自检问
CGREY = (0x6B, 0x6B, 0x70)       # 灰 · 出处与元信息

TIER_COLOR = {
    "tier1": CGOLD,
    "tier2": (0x2B, 0x4A, 0x6B),
    "tier3": (0x2E, 0x6B, 0x4A),
    "tier4": CGREY,
}


# ══ 排版基元 ══════════════════════════════════════════════════

def _cjk(run, font="宋体"):
    """中文必须单独设 rFonts，否则 Word 回退到默认字体。"""
    from docx.oxml.ns import qn
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def setup(doc):
    """A4 + 中文字体 + 1.35 倍行距。手册比练习卷密，单倍行距会挤。"""
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn

    for s in doc.sections:
        s.page_width, s.page_height = Cm(21.0), Cm(29.7)
        s.top_margin = s.bottom_margin = Cm(1.8)
        s.left_margin = s.right_margin = Cm(2.0)

    n = doc.styles["Normal"]
    n.font.name = "Times New Roman"
    n.font.size = Pt(10)
    n.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    pf = n.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.35
    pf.space_after = Pt(2)
    return doc


def para(doc, text="", size=10, bold=False, font="宋体", align=None,
         before=0, after=2, indent=None, color=None, spacing=1.35):
    """写一个段落。`**粗体**` 解析成真加粗（不是字面星号）。"""
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
    """把 `**x**` 切成 [(片段, 是否加粗)]。**只处理成对的**，落单星号原样保留。"""
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


def rule(doc, color=CSILVER, char="─"):
    para(doc, char * 52, size=8, color=color, after=4, spacing=1.0)


def page_break(doc):
    doc.add_page_break()


def _page_footer(doc):
    """页脚页码：手册会翻很多遍，找得到第几页是刚需。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    for s in doc.sections:
        p = s.footer.paragraphs[0]
        p.alignment = 2          # right
        for r in list(p.runs):
            r._element.getparent().remove(r._element)
        run = p.add_run()
        _cjk(run, "宋体")
        run.font.size = __import__("docx").shared.Pt(8.5)
        run.font.color.rgb = __import__("docx").shared.RGBColor(*CSILVER)
        # PAGE 域
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), "PAGE")
        p._p.append(fld)


# ══ 知识点一节 ═══════════════════════════════════════════════

def point_block(doc, pt, tier):
    """一条知识点 = 三块视觉可辨的内容 + 一条自检问。

    **三块必须长得不一样**（核心结论 / 易错提醒 / 自检问）——
    手册的使用方式是「翻到易错提醒那一栏扫一眼」。
    三栏同款排版 = 没有易错提醒栏，这是版式事故不是审美问题。
    """
    acc = TIER_COLOR.get(tier, CGREY)

    # 标题行：code + 名称 + 章 + 真题出处
    para(doc, f"{pt['code']}　{pt['name']}", size=11.5, bold=True,
         font="黑体", color=CINK, before=8, after=1, spacing=1.2)
    meta = [f"章：{pt.get('chapter') or '—'}"]
    if pt.get("page"):
        meta.append(f"页：p{pt['page']}")
    else:
        meta.append("页：章号待定（真题反推只能得出考什么，推不出编在第几章）")
    para(doc, "　│　".join(meta), size=8.5, color=CGREY, after=1, spacing=1.2)
    para(doc, f"真题出处：{pt.get('exam_evidence') or '—'}",
         size=8.5, color=CGREY, after=3, spacing=1.25)

    # ① 核心结论
    para(doc, "核心结论 · 要能默写", size=9.5, bold=True, font="黑体",
         color=acc, before=2, after=2)
    for c in pt.get("core") or []:
        para(doc, f"· {c}", size=10, color=CCORE, after=2, indent=12, spacing=1.4)

    # ② 易错提醒（砖红 + 前缀标记，与结论栏视觉区分）
    pits = pt.get("pitfalls") or []
    if pits:
        para(doc, "⚠ 易错提醒 · 这一栏是本手册最该看的部分", size=9.5, bold=True,
             font="黑体", color=CPIT, before=5, after=2)
        for p_ in pits:
            para(doc, f"▸ {p_}", size=9.5, color=CPIT, after=2, indent=12, spacing=1.35)

    # ③ 自检问（深蓝，问句形式）
    sc = pt.get("selfcheck")
    if sc:
        para(doc, f"自检问：{sc}", size=9.5, color=CQ, after=2, indent=12,
             spacing=1.4, before=3)

    rule(doc)


# ══ 主构建 ══════════════════════════════════════════════════

def build(data, dry=False):
    import docx

    meta = data["meta"]
    doc = setup(docx.Document())

    # ── 封面 ──
    para(doc, meta["title"], size=22, bold=True, font="黑体",
         align="center", after=3, spacing=1.15, color=CINK)
    para(doc, meta["grade"], size=11, align="center",
         color=CGREY, after=1, spacing=1.2)
    para(doc, meta["textbook"], size=10, align="center",
         color=CGREY, after=8, spacing=1.2)
    para(doc, f"{meta['audience']}　·　共 {meta['count']} 条",
         size=10.5, bold=True, align="center", color=CGOLD, after=10, spacing=1.2)
    rule(doc, color=CGOLD)

    para(doc, "怎么用这本手册", size=11, bold=True, font="黑体", before=2, after=2)
    for i, t in enumerate([
        "**只查不背**：第一梯队的「核心结论」要能默写，第二梯队扫一眼，"
        "第三梯队周末看，第四梯队中考前扫。",
        "**「⚠ 易错提醒」是重点**：每一条都是从真题的失分形态倒推出来的，"
        "**考前只看这一栏也来得及**。",
        "**「自检问」是过关标准**：答不上来就是这条没过，不要用「看着眼熟」骗自己。",
        "**每条后面的真题出处就是练习入口**：做完那道题再回来对核心结论，不要先背后做。",
        "**做题必须写完整过程**——只写答案等于没做。",
    ], 1):
        para(doc, f"{i}. {t}", size=9.5, after=2, indent=10, spacing=1.4)

    para(doc, "本册的边界（先看这段，再看内容）", size=11, bold=True, font="黑体",
         before=8, after=2)
    for t in [f"· {x}" for x in meta.get("known_limits", [])]:
        para(doc, t, size=9, color=CPIT, after=2, indent=10, spacing=1.35)
    para(doc, "· " + meta.get("content_boundary", ""), size=9,
         color=CGREY, after=4, indent=10, spacing=1.35)

    # ── 目录（四梯队一览）──
    para(doc, "梯队一览", size=11, bold=True, font="黑体", before=8, after=2)
    for sec in data["sections"]:
        acc = TIER_COLOR.get(sec["tier"], CGREY)
        para(doc, sec["label"], size=10, bold=True, font="黑体",
             color=acc, before=5, after=1, spacing=1.2)
        para(doc, sec["why"], size=9, color=CGREY, after=2, indent=10, spacing=1.35)
        names = "　·　".join(f"{p['code']} {p['name']}" for p in sec["points"])
        para(doc, names, size=9, color=CGREY, after=3, indent=10, spacing=1.4)

    # ── 逐条 ──
    for sec in data["sections"]:
        page_break(doc)
        acc = TIER_COLOR.get(sec["tier"], CGREY)
        para(doc, sec["label"], size=15, bold=True, font="黑体",
             color=acc, after=2, spacing=1.2)
        para(doc, sec["why"], size=9.5, color=CGREY, after=5, spacing=1.4)
        rule(doc, color=acc)
        for pt in sec["points"]:
            point_block(doc, pt, sec["tier"])

    # ── 附录一 · 真题逐题对照 ──
    ev = data.get("exam_paper_evidence") or {}
    if ev:
        page_break(doc)
        para(doc, ev["title"], size=15, bold=True, font="黑体",
             color=CINK, after=2, spacing=1.2)
        para(doc, ev["why"], size=9.5, color=CGREY, after=3, spacing=1.4)
        para(doc, f"卷源：{ev.get('paper')}", size=8.5, color=CGREY, after=2, spacing=1.3)
        para(doc, ev.get("caveat", ""), size=9, color=CPIT, after=5, spacing=1.35)

        for row in ev.get("rows", []):
            para(doc, f"■ {row['no']}　{row['type']}", size=11, bold=True,
                 font="黑体", color=CGOLD, before=6, after=2, spacing=1.25)
            for t in row["points"]:
                para(doc, f"· {t}", size=9.5, color=CCORE, after=2,
                     indent=12, spacing=1.4)

        para(doc, "这份卷面告诉我们的复习顺序", size=11, bold=True,
             font="黑体", color=CINK, before=8, after=2)
        for t in ev.get("reading_guide", []):
            para(doc, f"· {t}", size=9.5, color=CCORE, after=2, indent=12, spacing=1.4)

    # ── 附录二 · 怎么用 ──
    st = data.get("study_strategy") or {}
    if st:
        page_break(doc)
        para(doc, st["title"], size=15, bold=True, font="黑体",
             color=CINK, after=2, spacing=1.2)
        para(doc, st.get("strategy_note", ""), size=8.5, color=CGREY, after=5,
             spacing=1.3)

        para(doc, "为什么要先攻第一梯队", size=11, bold=True, font="黑体", after=2)
        para(doc, f"· **定位**：{st['position']}", size=9.5, color=CCORE,
             after=2, indent=12, spacing=1.4)
        para(doc, f"· **现状**：{st['current']}", size=9.5, color=CCORE,
             after=2, indent=12, spacing=1.4)

        para(doc, "六周排法", size=11, bold=True, font="黑体", before=6, after=2)
        for i, t in enumerate(st.get("plan", []), 1):
            para(doc, f"{i}. {t}", size=9.5, color=CCORE, after=2,
                 indent=12, spacing=1.4)

        wl = st.get("watch_list") or []
        if wl:
            para(doc, "本轮真卷暴露出的 6 个失分点（按优先级）", size=11, bold=True,
                 font="黑体", before=6, after=2)
            for t in wl:
                para(doc, f"· {t}", size=9.5, color=CCORE, after=2,
                     indent=12, spacing=1.4)

        dn = st.get("do_not_learn") or []
        if dn:
            para(doc, "不学 / 暂不学（省下的时间比多学的更重要）", size=11,
                 bold=True, font="黑体", color=CPIT, before=6, after=2)
            for t in dn:
                para(doc, t, size=9.5, color=CPIT, after=2, indent=12, spacing=1.4)

    # ── 免责 ──
    rule(doc)
    para(doc, "免责与依据", size=10, bold=True, font="黑体", before=2, after=2)
    para(doc, f"· 生成日期：{meta.get('created')}　·　来源：{'; '.join(meta.get('evidence_sources', []))}",
         size=8.5, color=CGREY, after=2, indent=10, spacing=1.35)
    para(doc, "· 教材版本、章节顺序、考点分布以**孩子手上那本课本**为准，版本会修订。"
              "九下章号待补拍目录照片后补齐（不影响按考点复习）。",
         size=8.5, color=CGREY, after=2, indent=10, spacing=1.35)
    para(doc, "· 中考政策以当年上海市教委官方文件为准，2027 届细则尚未全部发布。",
         size=8.5, color=CGREY, after=2, indent=10, spacing=1.35)

    _page_footer(doc)

    if not dry:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        doc.save(OUT)
    return doc


# ══ 结构闸门 ══════════════════════════════════════════════════
#
# **为什么要有**：本册的全部价值在「内容」，而内容在 JSON 里。
# 闸门查的不是内容对不对（那是人判断的），而是
# **「该有的结构有没有、有没有把空值渲染成看起来像有值的东西」**。
#
# 形态属闸门纪律 ⑤「源头无闸门」——
# 本文件是 JSON 派生产物，闸门围着 JSON 建，但 JSON 里的 `page: null`
# 如果被渲染成 "p0" 或 "第 ? 章"，就是**页面比数据层多出信息**，
# 而这正是 MEMORY 里写明的告警信号。
#
# **所以必查项之一：`page` 为 null 时必须印成「章号待定」而不是猜一个值。**

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def chk_structure(d):
    """四梯队齐备、tier 命名合法、每条必备三块内容。"""
    errs = []
    tiers = [s["tier"] for s in d["sections"]]
    if len(set(tiers)) != 4:
        errs.append(f"梯队数不对：{tiers}")
    n = 0
    for s in d["sections"]:
        if not s.get("label") or not s.get("why"):
            errs.append(f"{s['tier']} 缺 label 或 why")
        for p in s["points"]:
            n += 1
            for k in ("code", "name", "chapter", "exam_evidence",
                      "core", "pitfalls", "selfcheck"):
                if not p.get(k):
                    errs.append(f"{p.get('code')} 缺字段 {k}")
    if d["meta"].get("count") != n:
        errs.append(f"meta.count={d['meta'].get('count')} 与实际条数 {n} 不符")
    return errs


@check
def chk_tier_ordering(d):
    """tier1 必须真的比 tier4 更该先学——用真题出处密度做代理指标。

    这是**弱校验**，只能抓明显倒挂（如 tier1 里有条目一条真题都没提）。

    ⚠ **判据踩过的坑（2026-10-04 本轮当场踩到）**：
    第一版判据写成 `"真题" not in ev`，结果 6 条 tier1 全部误伤——
    因为出处里写的是「**2025 中考**第 11 题」，根本没有「真题」二字。
    **判据的字面锚点必须选一个真实存在的、不依赖我措辞的东西。**
    现在改用两条硬标记：`第 N 题`（具体题号）或 `命中`（跨 5 套的合计命中），
    两者都是引用真卷必然出现的形式。

    真正的权重判断是人的判断，闸门不越权。
    """
    import re
    warns = []
    for s in d["sections"]:
        if s["tier"] == "tier1":
            for p in s["points"]:
                ev = p.get("exam_evidence") or ""
                has_ref = re.search(r"第\s*\d+\s*题", ev)
                has_count = "命中" in ev
                if not (has_ref or has_count):
                    warns.append(
                        f"tier1 的 {p['code']} 真题出处可疑：既无题号也无命中统计 → {ev[:40]}")
    return warns


@check
def chk_null_not_guessed(d):
    """`page: null` 只能出现在章号待定的条目上，且 chapter 必须写明「待定」。

    反向也查：chapter 说「待定」却填了 page ⇒ 前后矛盾。
    """
    errs = []
    for s in d["sections"]:
        for p in s["points"]:
            ch, pg = p.get("chapter") or "", p.get("page")
            if pg is None and "待定" not in ch and "综合与实践" not in ch:
                errs.append(f"{p['code']} page 为 null 但 chapter 没标待定：{ch}")
            if pg is not None and "待定" in ch:
                errs.append(f"{p['code']} chapter 标待定却有页码 {pg}")
    return errs


@check
def chk_no_ghost_content(d):
    """每个附录小节都要有内容——空附录等于占版面。

    形态属 ①「把错误藏起来」：附录标题渲染出来了，内容是空的，
    打开文档一眼看过去「结构完整」，实际什么也没有。
    """
    errs = []
    ev = d.get("exam_paper_evidence") or {}
    if ev:
        if not ev.get("rows"):
            errs.append("真题对照附录有标题无 rows")
        if not ev.get("reading_guide"):
            errs.append("真题对照附录缺 reading_guide")
    st = d.get("study_strategy") or {}
    if st:
        for k in ("plan", "watch_list", "do_not_learn"):
            if not st.get(k):
                errs.append(f"学习策略缺 {k}")
    return errs


@check
def chk_no_internal_codename(d):
    """交付给孩子看的正文里不许出现内部代号 tier1/tier2/…

    **为什么这道闸门必须有**：本手册的内部键名是 `tier1`…`tier4`
    （脚本的 `TIER_COLOR` 与闸门都按它索引），但**读者看到的是
    「第一梯队·必考」**。第一版正文里两套命名混着出现 17 处
    （如「tier1 的 7 条覆盖了…」），而**页面上完全正常**——
    封面写「第一梯队」，正文写 tier1，属于 MEMORY 记的
    ③「副本漂移」：同一事实两套拼写，各自自洽，渲染照常。

    ⚠ 注意判据的边界：`sections[].tier` **本身就是键名，不能查**。
    只能查「面向读者的文案字段」：label / why / core / pitfalls /
    selfcheck / exam_evidence / reading_guide / plan / watch_list 等。
    """
    import re

    READER_FIELDS = ("label", "why", "core", "pitfalls", "selfcheck",
                     "exam_evidence", "caveat", "reading_guide", "plan",
                     "watch_list", "do_not_learn", "position", "current",
                     "strategy_note", "title", "known_limits",
                     "content_boundary")

    errs = []

    def walk(node, path):
        if isinstance(node, str):
            if re.search(r"\btier\s*\d", node):
                errs.append(f"{path} 含内部代号：{node[:50]}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                if k in ("tier",):          # 键名本身跳过
                    continue
                walk(v, f"{path}.{k}")

    for k, v in d.items():
        if k == "sections":
            for si, s in enumerate(v):
                for f in READER_FIELDS:
                    if f in s:
                        walk(s[f], f"sections[{si}].{f}")
                for pi, p in enumerate(s.get("points", [])):
                    for f in READER_FIELDS:
                        if f in p:
                            walk(p[f], f"sections[{si}].points[{pi}].{f}")
        elif k in ("exam_paper_evidence", "study_strategy"):
            walk(v, k)
        elif k == "meta":
            for f in READER_FIELDS:
                if f in v:
                    walk(v[f], f"meta.{f}")
    return errs


@check
def chk_real_exam_refs(d):
    """真题出处里的题号必须能在真卷文本里找到对应位置。

    **这是唯一一道查「引用是否真实」的闸门。**
    MEMORY 记着「答案错了整套教学判断全部反着走」，
    本册每条都挂着真题出处——出处编错，等于把错误定位引到错的题上。
    """
    import re
    path = os.path.join(ROOT, "RAW", "试卷库",
                        "2025-中考-全市-数学-2025年上海市中考数学真题试卷(含答案).txt")
    if not os.path.exists(path):
        return ["⚠ 真卷文件不存在，无法核对出处"]
    txt = open(path, encoding="utf-8", errors="ignore").read()
    # 抽出所有「第 N 题」引用，核对卷面里确实有第 N 题
    bad = []
    blob = json.dumps(d, ensure_ascii=False)
    for m in re.finditer(r"第\s*(\d+)\s*题", blob):
        n = int(m.group(1))
        if not re.search(rf"(?<!\d){n}\s*[\.．]", txt):
            bad.append(f"引用「第 {n} 题」在 2025 中考卷面中找不到")
    return sorted(set(bad))


def run_checks(d):
    errs, warns = [], []
    for fn in CHECKS:
        out = fn(d)
        name = fn.__name__.replace("chk_", "")
        for x in out or []:
            (errs if not x.startswith("⚠") else warns).append(f"[{name}] {x}")
    return errs, warns


@check
def chk_script_copy(_d=None):
    """**闸门必须覆盖脚本自己的文案**——这是一个真实漏过的缺口。

    `chk_no_internal_codename` 只查 JSON，查不到脚本里硬编码的字符串。
    实际踩到：脚本文案里的「只查不背：tier1 的核心结论…」和
    「为什么要先攻 tier1」两处带着内部代号，**JSON 全绿、docx 照样漂**。
    形态属 MEMORY 铁律 2「闸门必须覆盖它声称覆盖的全部入口」——
    声称覆盖本手册的命名一致性，实际只覆盖了一半。

    做法：扫本文件里**会被渲染出去的中文字符串字面量**，
         排除「闸门函数体」与注释/docstring。

    ⚠ **判据三次收紧才对，两次都是被自己的回退法打回来的**：
    - v1 扫「全部中文字符串」⇒ 误伤 `chk_tier_ordering` 的**报错文案**
      「tier1 的 {p['code']} 真题出处可疑」——那是给开发看的，**不会被渲染**。
    - v2 只扫 `para(...)` 实参 ⇒ **回退法自证直接失败**：
      「只查不背：tier1 的…」这段在 `for ... in enumerate([...])` 的列表里，
      不在 `para()` 实参内，**注入违规后闸门仍报 0 错误**。
      这就是 MEMORY 铁律 6「只有回退法能暴露」——判据收紧到窄口径时，
      必须用回退法确认它还抓得住。
    - v3 ⇒ 按**函数体**切分：排除 `def chk_*` 整个函数（那里只有报错文案），
      其余全部中文字面量都算「可能渲染」。

    **教训同 MEMORY 铁律 3：判据的前提不能是「违规内容本不具备的性质」。**
    「中文 = 面向读者」这个前提不成立，报错文案也是中文。
    """
    import re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()

    # 屏蔽闸门函数体：那里的中文只出现在报错文案里，不会被渲染
    masked = re.sub(r"def chk_\w+\(.*?(?=\n(?:@check|def |# ══|CHECKS))",
                    lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    # 屏蔽 docstring（三引号）与行注释
    masked = re.sub(r'""".*?"""', "", masked, flags=re.S)
    masked = re.sub(r"#[^\n]*", "", masked)

    bad = []
    for m in re.finditer(r'"([^"\\]*[\u4e00-\u9fff][^"\\]*)"', masked):
        s = m.group(1)
        if re.search(r"\btier\s*\d", s, re.I):
            line = masked[:m.start()].count("\n") + 1
            bad.append(f"渲染文案第 {line} 行含内部代号：{s[:60]}")
    return bad


def main():
    ap = argparse.ArgumentParser(description="生成数学学习要点速查手册（Word）")
    ap.add_argument("--dry-run", action="store_true", help="只跑闸门与渲染，不写盘")
    ap.add_argument("--check", action="store_true", help="只跑结构闸门")
    args = ap.parse_args()

    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)

    errs, warns = run_checks(data)
    print(f"结构闸门：{len(CHECKS)} 项 · {len(errs)} 错误 · {len(warns)} 警告")
    for w in warns:
        print(f"  ⚠ {w}")
    for e in errs:
        print(f"  ❌ {e}")
    if errs:
        print("\n❌ 闸门未过，不写盘。")
        return 1
    if args.check:
        return 0

    build(data, dry=args.dry_run)
    n = sum(len(s["points"]) for s in data["sections"])
    if args.dry_run:
        print(f"[dry-run] {n} 条知识点，未写盘")
    else:
        print(f"✅ 数学学习要点速查手册 · {n} 条知识点")
        print(f"   → {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())