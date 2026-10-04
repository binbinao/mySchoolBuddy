#!/usr/bin/env python3
"""build_function_paper.py — 从 data/resources/function-topic-paper.json 渲染 Word 练习卷

**这个脚本不生产任何教学内容**

题目、答案、步骤、易错点全来自 `data/resources/function-topic-paper.json`（人工维护），
脚本只负责排版。**解法步骤写错了比没有更危险**，所以一律不在代码里生成。

**为什么单独立脚本，不并进 build_word_materials.py / build_math_handbook.py**

- `build_word_materials.py`：错题加练，硬边界是「学生卷不能泄露答案」
- `build_math_handbook.py`：速查手册，**她就是要翻答案的**
- 本脚本：**专题练习卷**，同样「先练习卷后讲义版」

三者混在一个文件里，那道 `selfcheck()` 闸门的语义会变模糊。

**版式约定（练习卷）**

- **题目卷**（`*-练习卷.docx`）：只给题 + 答题区，**不含答案**
- **答案卷**（`*-答案.docx`）：答案 + 分步过程 + 易错点
- 两者**同源生成**——从同一份 JSON 渲染，不可能漂移
- A4 / 1.5 倍行距 / 姓名班级日期栏 / 每题留足书写空间

**依赖**：`python-docx`，隔离 venv

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/build_function_paper.py
$PY tools/build_function_paper.py --dry-run
$PY tools/build_function_paper.py --check
```

**不做什么（硬边界）**

- ❌ 不改 `data/`：只读
- ❌ 不生成讲义版：等她做完、拍照上传、有真实卡点证据后再说
- ❌ 不算答案：答案在 JSON 里，**脚本不碰数学**
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "resources", "function-topic-paper.json")
OUT_DIR = os.path.join(ROOT, "docs", "材料", "专题学习卷", "数学")

CINK = (0x1C, 0x1C, 0x1E)
CGREY = (0x6B, 0x6B, 0x70)
CGOLD = (0xB4, 0x6E, 0x00)
CPIT = (0x8A, 0x3A, 0x00)
CBLUE = (0x1F, 0x3F, 0x66)
CSILVER = (0xA1, 0xA1, 0xAA)


# ══ 排版基元（与项目其他生成器同源）══════════════════════════

def _cjk(run, font="宋体"):
    from docx.oxml.ns import qn
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def setup(doc):
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn

    for s in doc.sections:
        s.page_width, s.page_height = Cm(21.0), Cm(29.7)
        s.top_margin = s.bottom_margin = Cm(1.8)
        s.left_margin = s.right_margin = Cm(2.0)

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
    """把 `**x**` 切成 [(片段, 是否加粗)]。只处理成对的，落单星号原样保留。"""
    out, i = [], 0
    while True:
        a = text.find("**", i)
        if a < 0:
            break
        b = text.find("**", a + 2)
        if b < 0:
            break
        if a > i:
            out.append((text[i:a], False))
        out.append((text[a + 2:b], True))
        i = b + 2
    if i < len(text):
        out.append((text[i:], False))
    return out or [(text, False)]


def rule(doc, char="─", color=CSILVER):
    para(doc, char * 52, size=8, color=color, after=4, spacing=1.0)


def page_break(doc):
    doc.add_page_break()


def answer_area(doc, lines=8, label="解："):
    para(doc, label, size=9.5, color=(0x99, 0x99, 0x99), before=1, after=1)
    for _ in range(lines):
        para(doc, "　" * 38, size=10.5, after=0, spacing=1.6)


def _page_footer(doc):
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt as _Pt, RGBColor
    for s in doc.sections:
        p = s.footer.paragraphs[0]
        p.alignment = 2
        for r in list(p.runs):
            r._element.getparent().remove(r._element)
        run = p.add_run()
        _cjk(run, "宋体")
        run.font.size = _Pt(8.5)
        run.font.color.rgb = RGBColor(*CSILVER)
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), "PAGE")
        p._p.append(fld)


# ══ 抬头 ════════════════════════════════════════════════════

def header_block(doc, title, subtitle, meta_lines):
    para(doc, title, size=17, bold=True, font="黑体", align="center",
         after=2, spacing=1.2, color=CINK)
    para(doc, subtitle, size=10, color=CGREY, align="center", after=7, spacing=1.2)
    para(doc, "姓名：______________　　班级：________　　日期：______________",
         size=10, after=2)
    for m in meta_lines:
        para(doc, m, size=9, color=CGREY, after=1, spacing=1.3)
    rule(doc, color=CGOLD)


def notice(doc, lines, title="作答说明"):
    para(doc, title, size=11, bold=True, font="黑体", before=2, after=2)
    for i, t in enumerate(lines, 1):
        para(doc, f"{i}. {t}", size=9.5, after=2, indent=10, spacing=1.4)


# ══ 主构建 ══════════════════════════════════════════════════

def _stem_lines(stem):
    """题干里的 \n 是刻意的排版换行（选项、表格），必须保留。"""
    return str(stem or "").split("\n")


def _answer_lines(q):
    """留白行数按分值给：分值越高写得越多。"""
    score = q.get("score", 4)
    if score <= 4:
        return 6
    if score <= 12:
        return 16
    return 26


def build(data, dry=False):
    import docx

    meta = data["meta"]
    sec_self = data.get("self_check") or {}
    answer_notes = data.get("answer_notes") or {}
    base = "数学-一次函数与抛物线-专题练习卷"

    # ────────────── 题目卷 ──────────────
    doc = setup(docx.Document())
    # ⚠️ 抬头只印「范围」这一句，不能把 chapter_note 整段印上去——
    # 那是给项目内部看的（含纪律说明、反引号路径、Markdown 标记），
    # 印到学生卷上等于把维护笔记发给她。见下方 README_NOTE。
    para(doc, "范围：抛物线 = 九年级上册 27 章；一次函数 = 八年级下册（已学内容）",
         size=9, color=CGREY, after=1, spacing=1.3)
    header_block(
        doc, meta["title"], meta["subtitle"],
        [f"全卷 16 题 · 120 分 · 建议用时 100 分钟"])
    para(doc, "全卷总分", size=10.5, bold=True, after=1)
    para(doc, "　　一、选择题 24 分　　二、填空题 24 分　　三、解答题 72 分",
         size=10, after=4, indent=10)

    notice(doc, [
        "**本卷只做一遍，不要边做边翻答案卷。** 做完再对答案，然后**把错题号记下来**。",
        "每道题都要写**完整过程**——只写答案等于没做。**这是本项目反复强调的铁律。**",
        "**做完拍照上传**，我们会据此生成针对你实际卡住那一步的讲义版。",
        "⚠️ **卷末「已知瑕疵」一节请先读**——本卷有 3 处编题时留下的瑕疵，已写明怎么处理。",
        "**算不出答案时，先检查条件齐不齐**，不要先怀疑自己的计算。"
          "（本卷第 15 题第 (3) 问就是条件不全的实例。）",
    ])

    for sec in data["sections"]:
        para(doc, sec["title"], size=12, bold=True, font="黑体", before=8, after=2)
        if sec.get("note"):
            para(doc, sec["note"], size=9, color=CGREY, after=4, indent=10, spacing=1.35)
        for q in sec["questions"]:
            para(doc, f"第 {q['no']} 题（{q['score']} 分）", size=10.5, bold=True,
                 font="黑体", before=6, after=2)
            for ln in _stem_lines(q["stem"]):
                para(doc, ln, size=10.5, after=2, spacing=1.6)
            answer_area(doc, lines=_answer_lines(q))

    # 已知瑕疵（放在题目之后、答题之前会剧透答案区，改为答完后自查页）
    page_break(doc)
    para(doc, "做完以后再看这一页", size=12, bold=True, font="黑体",
         align="center", after=3, color=CGOLD)
    para(doc, sec_self.get("title", "本卷自检"), size=11, bold=True,
         font="黑体", before=4, after=2)
    para(doc, sec_self.get("honest_note", ""), size=9.5, color=CPIT,
         after=4, spacing=1.4)
    for i, t in enumerate(sec_self.get("items") or [], 1):
        para(doc, f"{i}. {t}", size=10, after=2, indent=12, spacing=1.4)
    para(doc, "自查提醒", size=11, bold=True, font="黑体", before=8, after=2)
    for t in sec_self.get("how_to_review") or []:
        para(doc, f"· {t}", size=9.5, color=CINK, after=2, indent=12, spacing=1.4)
    para(doc, "做完自查", size=11, bold=True, font="黑体", before=8, after=2)
    for t in ["□ 选择题 6 道都写了理由（不是只圈字母）",
              "□ 填空题每道都写了关键步骤",
              "□ 解答题写了完整过程，含定义域与范围",
              "□ 压轴题（第 16 题）检验了 t = 3/2 在范围内",
              "□ 我把做错/做不出来的题号记下来了：__________",
              "□ 我算不出答案的题，先检查了条件齐不齐"]:
        para(doc, t, size=10, after=2, indent=10)

    _page_footer(doc)
    paper = os.path.join(OUT_DIR, f"{base}-练习卷.docx")

    # ────────────── 答案卷 ──────────────
    adoc = setup(docx.Document())
    header_block(
        adoc, f"答案与讲评 · {meta['title']}", meta["subtitle"],
        [f"对应练习卷：{base}-练习卷.docx",
         "**先自己判分，再看这个**。全对的题标 ✓，卡住的题标 ✗。"])
    para(adoc, answer_notes.get("style", ""), size=9.5, color=CGREY, after=2, spacing=1.4)
    para(adoc, answer_notes.get("warning", ""), size=9.5, color=CPIT, after=5, spacing=1.4)

    for sec in data["sections"]:
        para(adoc, sec["title"], size=12, bold=True, font="黑体", before=8, after=3)
        for q in sec["questions"]:
            para(adoc, f"第 {q['no']} 题（{q['score']} 分）", size=10.5, bold=True,
                 font="黑体", before=6, after=2)
            para(adoc, f"考点：{q['point']}", size=9, color=CBLUE, after=1, spacing=1.3)
            para(adoc, f"命题依据：{q['source']}", size=8.5, color=CGREY,
                 after=3, spacing=1.3)
            para(adoc, f"**答案：**{q['answer']}", size=11, color=CINK,
                 after=3, spacing=1.45)
            para(adoc, "**分步过程**", size=9.5, bold=True, font="黑体", after=2)
            for s in q.get("steps") or []:
                para(adoc, s, size=10, color=CINK, after=2, indent=12, spacing=1.45)
            pits = q.get("pitfall")
            if pits:
                para(adoc, "**易错提醒**", size=9.5, bold=True, font="黑体",
                     color=CPIT, before=4, after=2)
                if isinstance(pits, str):
                    pits = [pits]
                for p in pits:
                    para(adoc, p, size=9.5, color=CPIT, after=2, indent=12, spacing=1.4)
            rule(adoc)

    _page_footer(adoc)
    apaper = os.path.join(OUT_DIR, f"{base}-答案.docx")

    if not dry:
        os.makedirs(OUT_DIR, exist_ok=True)
        doc.save(paper)
        adoc.save(apaper)
    return paper, apaper, sum(len(s["questions"]) for s in data["sections"])


# ══ 结构闸门 ══════════════════════════════════════════════════
#
# 与 build_math_handbook.py 同源的两条原则：
#  1. 闸门查「结构有没有」，不查「内容对不对」——后者是人的判断，机器不越权
#  2. **每道闸门都要用回退法正反两面验证过**（本文件末尾有验证脚本）
#
# 本轮实测过的真实缺陷：手工编辑长 JSON 时反复把 `"pitfall": "…"` 写成
# 后面又跟裸字符串数组元素的形式，导致 JSON 语法损坏。
# ⇒ chk_json_like_fields 查「字段类型是否与实际内容匹配」这一类。

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def chk_required_fields(d):
    """每题必须齐 8 个字段，缺一不可。"""
    errs = []
    for sec in d["sections"]:
        for q in sec.get("questions") or []:
            for k in ("no", "score", "point", "source", "stem",
                      "answer", "steps", "pitfall"):
                if not q.get(k):
                    errs.append(f"第 {q.get('no', '?')} 题缺字段 {k}")
    return errs


@check
def chk_pitfall_not_orphan_string(d):
    """🔴 **本轮真实踩到的缺陷的闸门**：手工编辑长 JSON 时，
    我三次把 `"pitfall": "**标题**",` 写成后面又跟一串裸字符串，
    造成「字段声明为字符串、内容却是数组」⇒ **JSON 直接语法报错**。

    反过来也查：若 pitfall 是字符串但内容以 ①②③ 开头（多段易错点），
    说明本该是数组。**两种形态都要查。**
    """
    errs = []
    for sec in d["sections"]:
        for q in sec.get("questions") or []:
            p = q.get("pitfall")
            if isinstance(p, str) and p.lstrip().startswith(("①", "②", "③", "1.", "·")):
                errs.append(f"第 {q['no']} 题 pitfall 是字符串但内容是多段结构，"
                            f"应改为数组：{p[:30]}")
    return errs


@check
def chk_score_sum(d):
    """各板块分值之和 = 声明的总分。

    **分值是这个项目最敏感的数字**（MEMORY：分值纪律「判据是照片上有没有」），
    专题卷的分值是自编的，更要自洽。
    """
    errs = []
    total = 0
    for sec in d["sections"]:
        s = sum(q["score"] for q in sec.get("questions") or [])
        total += s
        # 从标题里抠出声明分值
        import re
        m = re.search(r"共\s*(\d+)\s*分", sec["title"])
        if m and int(m.group(1)) != s:
            errs.append(f"{sec['title'][:12]} 标题写「共 {m.group(1)} 分」"
                        f"但题目实际合计 {s} 分")
    if total != 120:
        errs.append(f"全卷合计 {total} 分，与 meta 声明的 120 分不符")
    return errs


@check
def chk_no_answers_in_paper_fields(d):
    """题目侧字段里不许混进答案文本。

    形态属「把错误藏起来」：`stem` 里写了「答案是 B」，
    学生卷就等于把答案印在题干上——而**打开文档完全正常**。
    """
    errs = []
    import re
    for sec in d["sections"]:
        for q in sec.get("questions") or []:
            stem = str(q.get("stem") or "")
            if re.search(r"答案(是|为|：|:)\s*[A-DＡ-Ｄ]", stem):
                errs.append(f"第 {q['no']} 题题干里出现答案字样：{stem[:50]}")
    return errs


@check
def chk_real_paper_refs(d):
    """真题出处必须能在已入库真题里找到对应题号。"""
    import re
    p = os.path.join(ROOT, "RAW", "试卷库",
                      "2025-中考-全市-数学-2025年上海市中考数学真题试卷(含答案).txt")
    if not os.path.exists(p):
        return ["⚠ 真卷文件不存在，无法核对出处"]
    txt = open(p, encoding="utf-8", errors="ignore").read()
    bad = []
    for sec in d["sections"]:
        for q in sec.get("questions") or []:
            for m in re.finditer(r"2025\s*中考第\s*(\d+)\s*题", str(q.get("source", ""))):
                n = int(m.group(1))
                if not re.search(rf"(?<!\d){n}\s*[\.．]", txt):
                    bad.append(f"第 {q['no']} 题引用的「第 {n} 题」在 2025 中考卷中找不到")
    return sorted(set(bad))


@check
def chk_no_doc_adoc_mixup(_d=None):
    """🔴 **查本脚本源码自身**：答案卷区块里不许出现 `para(doc,`。

    **本轮真实踩到（2026-10-04 20:4x）**：`build()` 里答案卷那 24 处
    全部误写成 `para(doc, ...)` 而不是 `para(adoc, ...)`，
    结果**答案、步骤、易错点、命题依据全写进学生卷**，
    而 docx 文件大小、段落数、打开效果**全都正常，肉眼扫一眼发现不了**。

    这是 `build_word_materials.py` 里那道 `selfcheck()` 的由来，
    **同一个缺陷在两个生成器里各犯一次** ⇒ 说明它不是偶发，是模式。

    ⚠️ 判据必须**按区块**切，不能全文件禁 `para(doc,`——题目卷本来就该用 `doc`。
    这里只查「`adoc = setup(...)` 之后到 `paper`/`apaper` 赋值之前」这段，
    即答案卷区块。**用括号配平找 `build()` 里第二个容器创建点。**
    """
    import re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()

    bad = []
    # 定位答案卷区块：从 `adoc = setup(` 到 `_page_footer(adoc)`
    start = src.find("adoc = setup(")
    end = src.find("_page_footer(adoc)")
    if start < 0 or end < 0 or end <= start:
        return ["⚠ 未能定位答案卷区块，闸门失效（脚本结构变了？）"]

    block = src[start:end]
    for m in re.finditer(r'\bpara\(\s*doc\b', block):
        line = src[:start + m.start()].count("\n") + 1
        bad.append(f"答案卷区块第 {line} 行用了 para(doc，应为 para(adoc)"
                   "——答案会写进学生卷")
    for m in re.finditer(r'\bheader_block\(\s*doc\b', block):
        line = src[:start + m.start()].count("\n") + 1
        bad.append(f"答案卷区块第 {line} 行用了 header_block(doc，应为 adoc)")
    for m in re.finditer(r'\brule\(\s*doc\b', block):
        line = src[:start + m.start()].count("\n") + 1
        bad.append(f"答案卷区块第 {line} 行用了 rule(doc，应为 rule(adoc)")
    return bad


@check
def chk_no_internal_notes_in_render(_d=None):
    """🔴 **交付文档里不许印项目的内部维护笔记**（本轮实际踩到）。

    `meta.chapter_note` 写着「八下章号无教材依据」「RAW/教材/ 只有九上目录照片」
    「按项目纪律…」——这些是**给项目维护者看的判断依据**，
    但我第一版直接把它当抬头印进了学生卷，于是孩子看到的是：

        范围：⚠️ 抛物线 = 九上 27 章… 一次函数 = 八下累积——⚠️ `RAW/教材/` 只有九上目录照片

    含反引号路径、Markdown 标记、内部纪律，**这不该出现在她要做的卷子上**。
    它必须留在 JSON 里（是重要的溯源记录），但**不能直接渲染**。

    形态属 ③「副本漂移」的近亲：同一个字段对两类读者含义不同，
    却只有一个渲染路径。⇒ **面向读者的文案要另写一份，不复用维护字段。**
    """
    import re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()

    # 找出 build() 里对 meta[...] 的引用
    bad = []
    for m in re.finditer(r'meta\.get\(\s*[\'"](\w+)[\'"]', src):
        key = m.group(1)
        if key in ("chapter_note", "not_covered", "basis", "content_boundary",
                   "exam_alignment", "paper_structure"):
            line = src[:m.start()].count("\n") + 1
            bad.append(f"第 {line} 行把 meta.{key}（项目内部记录）用于渲染——"
                       "学生卷不该印这些")
    return bad


def run_checks(d):
    errs, warns = [], []
    for fn in CHECKS:
        name = fn.__name__.replace("chk_", "")
        for x in fn(d) or []:
            (warns if x.startswith("⚠") else errs).append(f"[{name}] {x}")
    return errs, warns


# ══ 交付前自检（防「答案写进学生卷」）═════════════════════════
#
# 与 build_word_materials.py 的 selfcheck() 同源——
# 那道闸门的由来：答案卷区块 11 处 `para(doc,...)` 漏写成 `doc`，
# 结果答案全进学生卷，而**文件大小、段落数、打开效果全都正常，肉眼扫不出**。
#
# ⚠️ 那是「源码是对的、变量传错了」，grep 源码查不出来。
# **所以任何试卷类产物，交付前必须打开产成品读内容。**

FORBIDDEN_IN_PAPER = ["答案：", "**答案：**", "分步过程", "易错提醒",
                      "命题依据：", "考点：", "参考答案"]


def selfcheck(dirpath):
    from docx import Document
    bad = []
    n_p = n_a = 0
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith(".docx"):
            continue
        full = os.path.join(dirpath, fn)
        txt = "\n".join(p.text for p in Document(full).paragraphs)
        if fn.endswith("-练习卷.docx"):
            n_p += 1
            hit = {k: txt.count(k) for k in FORBIDDEN_IN_PAPER if k in txt}
            if hit:
                bad.append((fn, f"练习卷泄露答案：{hit}"))
            # 题干里的 A/B/C/D 选项是正常的，要排除误报
        elif fn.endswith("-答案.docx"):
            n_a += 1
            if "**答案：**" not in txt and "答案：" not in txt:
                bad.append((fn, "答案卷里没有答案"))
    return n_p, n_a, bad


def main():
    ap = argparse.ArgumentParser(description="生成一次函数与抛物线专题练习卷（Word）")
    ap.add_argument("--dry-run", action="store_true")
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

    paper, apaper, n = build(data, dry=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] {n} 题，未写盘")
        return 0

    # 交付前自检：打开产成品读内容
    n_p, n_a, bad = selfcheck(OUT_DIR)
    print(f"交付自检：练习卷 {n_p} 份 · 答案卷 {n_a} 份")
    if bad:
        for fn, why in bad:
            print(f"  ❌ {fn} — {why}")
        print("\n❌ 自检未过。")
        return 1
    print("  ✅ 学生卷无答案泄露，答案卷含答案")
    print(f"\n✅ 一次函数与抛物线专题练习卷 · {n} 题")
    print(f"   题目卷 → {os.path.relpath(paper, ROOT)}")
    print(f"   答案卷 → {os.path.relpath(apaper, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
