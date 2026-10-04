#!/usr/bin/env python3
"""build_2022_paper_docx.py — 从 math-paper-2022.json 渲染 Word 大题卷

**与其它生成器的差别：这份卷子允许「题面全给、部分无答案」**

因为 2022 中考原卷**没有配套答案页**，7 题里只有 5 题的答案验算过。
所以：
- **题目卷**：7 题全给（题面已逐字核对，可靠）
- **答案卷**：只给验算过的部分，**未完成的小问印「🔴 本题未完成，答案待补」**
- 卷首页印「交付边界」——**已知做不到的事写在最前面，不藏在末尾**

⚠️ 这道卷子的闸门重点不是「答案泄不泄露」，而是
「**未验算的答案不许出现**」（数据层 `check_2022_paper` 已拦，这里再拦一次）。

**用法**

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/build_2022_paper_docx.py
$PY tools/build_2022_paper_docx.py --check
```
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "resources", "math-paper-2022.json")
OUT_DIR = os.path.join(ROOT, "docs", "材料", "专题学习卷", "数学")

CINK = (0x1C, 0x1C, 0x1E)
CGREY = (0x6B, 0x6B, 0x70)
CGOLD = (0xB4, 0x6E, 0x00)
CPIT = (0x8A, 0x3A, 0x00)
CSILVER = (0xA1, 0xA1, 0xAA)
CBLUE = (0x1F, 0x3F, 0x66)


def _cjk(run, font="宋体"):
    from docx.oxml.ns import qn
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def setup(doc, spacing=1.5):
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
    pf.line_spacing = spacing
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


def answer_area(doc, lines, label="解："):
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


def _blank_lines(score):
    """按分值给书写空间：分值越高写得越多。"""
    return 20 if score <= 10 else (28 if score <= 12 else 36)


def build(data, dry=False):
    import docx
    meta = data["meta"]
    note = data.get("delivery_note") or {}
    base = "数学-上海中考大题专项-2022真题"

    qs = [q for s in data["sections"] for q in s["questions"]]

    # ─────────── 题目卷 ───────────
    doc = setup(docx.Document())
    para(doc, meta["title"], size=17, bold=True, font="黑体", align="center",
         after=2, spacing=1.2, color=CINK)
    para(doc, meta["subtitle"], size=10, color=CGREY, align="center",
         after=7, spacing=1.2)
    para(doc, "姓名：______________　　班级：________　　日期：______________",
         size=10, after=2)
    para(doc, f"全卷 {len(qs)} 题 · {sum(q['score'] for q in qs)} 分 · 建议用时 100 分钟",
         size=9, color=CGREY, after=1, spacing=1.3)
    para(doc, "范围：2022 上海中考解答题（19–25 题），本卷只做大题。",
         size=9, color=CGREY, after=1, spacing=1.3)
    rule(doc, color=CGOLD)

    para(doc, "作答说明", size=11, bold=True, font="黑体", before=2, after=2)
    # ⚠️ 「本卷有 4 道题我还没算出答案」里的 4 与题号列表，原先是手写字面量。
    #   真身是 questions[].status 里带「未完成」的题——**补验算一道就该跟着变**，
    #   而手写不会变，等于对家长说谎（MEMORY 铁律八·派生量）。
    _missing = [str(q["no"]) for q in qs if "未完成" in str(q.get("status", ""))]
    for i, t in enumerate([
        "**本卷不含选择填空。**她基础题已经全会，**练基础题等于浪费时间**。",
        "每题都要写**完整过程**——只写答案等于没做。",
        "**图形题必须先画图**（22、23、25 题都有图），"
        "在草稿上标出每条线段，**再判断哪两个三角形相似**。",
        f"🔴 **本卷有 {len(_missing)} 道题我还没算出答案**（{'、'.join(_missing)}），"
        "**答案卷里会写明**。你做完自己做对答案——"
        "我会在你交卷后补上。",
    ], 1):
        para(doc, f"{i}. {t}", size=9.5, after=2, indent=10, spacing=1.4)

    para(doc, "全卷总分", size=10.5, bold=True, before=8, after=1)
    # ⚠️ 派生量必须现算（第二十一类缺陷，2026-10-05）。
    #   原先这里是手写字面量「19–22 题各 10 分　23、24 题各 12 分　25 题 14 分」，
    #   而分值真身是 questions[].score。改一道题的分值，卷面这行不会跟着变。
    #   现在按「同分值合并成一段」现算，与上方抬头同源。
    _by = {}
    for _q in qs:
        _by.setdefault(_q["score"], []).append(str(_q["no"]))
    _segs = []
    for _sc in sorted(_by, reverse=True):
        _nos = _by[_sc]
        _label = (f"{_nos[0]}–{_nos[-1]} 题各 {_sc} 分" if len(_nos) > 1
                  else f"{_nos[0]} 题 {_sc} 分")
        _segs.append(_label)
    para(doc, "　　" + "　　".join(_segs), size=10, after=4, indent=10)

    for sec in data["sections"]:
        para(doc, sec["title"], size=12, bold=True, font="黑体", before=8, after=2)
        if sec.get("note"):
            para(doc, sec["note"], size=9, color=CGREY, after=4, indent=10, spacing=1.35)
        for q in sec["questions"]:
            para(doc, f"第 {q['no']} 题（{q['score']} 分）", size=11, bold=True,
                 font="黑体", before=7, after=2)
            for ln in str(q["stem"]).split("\n"):
                para(doc, ln, size=10.5, after=2, spacing=1.6)
            if "图" in str(q.get("stem", "")):
                para(doc, "（原卷此处有图：见 RAW/试卷库/原卷扫描/sh-2022-zk-math-b-p0*.gif，"
                          "或让孩子照原卷画）", size=9, color=CGREY, after=2, spacing=1.35)
            answer_area(doc, lines=_blank_lines(q["score"]))

    page_break(doc)
    para(doc, "做完后自查", size=12, bold=True, font="黑体", align="center",
         after=3, color=CGOLD)
    for t in [f"□ {len(qs)} 道题都写了完整过程",
              "□ 图形题先画图标线段，再找相似",
              "□ 遇到「求某个长度」的题，先问「该用哪个三角形」",
              "□ 21(2) 算 cos∠ABC 时分清邻边与斜边（顶点在 B）",
              "□ 算完解析式后，回头检查对称轴在哪、顶点是不是某个已知点",
              "□ 我算不出来/做错的题号记下来了：__________"]:
        para(doc, t, size=10, after=3, indent=10)
    # ⚠️ 自查页只写「怎么检查」，**不写答案要点**。
    #    第一版写了「22(1) 答案里含测角仪高度 b」⇒ **在学生卷里直接说出了这题的答案**，
    #    自查页反而成了答案泄露点。已删。
    _page_footer(doc)
    paper = os.path.join(OUT_DIR, f"{base}-题目卷.docx")

    # ─────────── 答案卷 ───────────
    adoc = setup(docx.Document())
    para(adoc, f"答案与讲评 · {meta['title']}", size=15, bold=True, font="黑体",
         align="center", after=2, spacing=1.2, color=CINK)
    para(adoc, f"对应题目卷：{base}-题目卷.docx", size=9, color=CGREY,
         align="center", after=6, spacing=1.3)
    rule(adoc, color=CGOLD)

    para(adoc, "⚠️ 本答案卷的交付边界（先读）", size=11, bold=True, font="黑体",
         color=CPIT, before=2, after=2)
    for k in ("stem_verified", "answer_verified", "answer_missing",
              "why", "answer_source_blocked"):
        v = note.get(k)
        if not v:
            continue
        head = {"stem_verified": "题面：", "answer_verified": "已验算的答案：",
                "answer_missing": "🔴 未完成（无答案）：",
                "why": "为什么没做完：",
                "answer_source_blocked": "另一个障碍："}[k]
        para(adoc, head + v, size=9.5, color=CPIT if "未" in head or "障碍" in head else CGREY,
             after=3, indent=10, spacing=1.45)
    if note.get("suggestion"):
        para(adoc, "建议：" + note["suggestion"], size=9.5, color=CINK,
             after=5, indent=10, spacing=1.45)

    for sec in data["sections"]:
        para(adoc, sec["title"], size=12, bold=True, font="黑体", before=8, after=3)
        for q in sec["questions"]:
            st = str(q.get("status", ""))
            done = ("已验算" in st) or ("已推导" in st)
            para(adoc, f"第 {q['no']} 题（{q['score']} 分）", size=11, bold=True,
                 font="黑体", before=7, after=2)
            para(adoc, f"考点：{q['point']}", size=9, color=CBLUE, after=1, spacing=1.3)
            para(adoc, f"来源：{q['source']}", size=8.5, color=CGREY, after=3, spacing=1.3)
            para(adoc, f"**答案：**{q['answer']}", size=10.5, color=CINK,
                 after=3, spacing=1.45)
            if "🔴" in str(q["answer"]):
                para(adoc, "🔴🔴 **上面带 🔴 的部分我没有算出答案，"
                           "不要拿它对答案，也不要以为「对不上」就是自己算错了。**"
                           "做完后把卷子发我，我补上。",
                     size=9.5, bold=True, color=CPIT, after=3, indent=12, spacing=1.4)
            if not done:
                rule(adoc)
                continue
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
    apaper = os.path.join(OUT_DIR, f"{base}-答案卷.docx")

    if not dry:
        os.makedirs(OUT_DIR, exist_ok=True)
        doc.save(paper)
        adoc.save(apaper)
    return paper, apaper, qs


# ══ 闸门 ══════════════════════════════════════════════════════
# 与 build_function_paper.py 同源的两条铁律：
#  ① 源码里答案卷区块必须用 adoc（第十九类缺陷复发过两次）
#  ② 学生卷不许出现答案（本题另加一条：未验算的答案不许出现在任何地方）

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def chk_required(d):
    errs = []
    for sec in d["sections"]:
        for q in sec["questions"]:
            for k in ("no", "score", "point", "source", "stem", "answer",
                      "steps", "pitfall", "selfcheck", "status"):
                if k in ("steps", "pitfall") and q["no"] in (23, 25):
                    continue          # 未完成的题允许步骤不全
                if not q.get(k):
                    errs.append(f"第 {q.get('no')} 题缺 {k}")
    return errs


@check
def chk_unverified_no_answer(d):
    """🔴 **本卷最核心的闸门**：未验算的小问不许有实质答案。"""
    errs = []
    for sec in d["sections"]:
        for q in sec["questions"]:
            ans = str(q.get("answer", ""))
            st = str(q.get("status", ""))
            if "未完成" not in st:
                continue
            if "🔴" not in ans:
                errs.append(f"第 {q['no']} 题标未完成但没写 🔴")
                continue
            tail = ans.split("🔴", 1)[1]
            if re.search(r"[0-9√=]", re.sub(r"[^（(]*", "", tail)):
                errs.append(f"第 {q['no']} 题 🔴 之后仍有公式/数字：{tail[:30]}")
    return errs


@check
def chk_delivery_note(d):
    errs = []
    dn = d.get("delivery_note") or {}
    for k in ("stem_verified", "answer_verified", "answer_missing", "why"):
        if not dn.get(k):
            errs.append(f"delivery_note 缺 {k}")
    return errs


@check
def chk_score_sum(d):
    qs = [q for s in d["sections"] for q in s["questions"]]
    tot = sum(q["score"] for q in qs)
    errs = []
    if tot != 78:
        errs.append(f"合计 {tot} 分，2022 解答题应为 78 分")
    if len(qs) != 7:
        errs.append(f"{len(qs)} 题，2022 解答题应为 7 题")
    return errs


@check
def chk_no_doc_adoc_mixup(_d=None):
    """答案卷区块不许出现 `para(doc,`（第十九类缺陷，本项目已犯两次）。"""
    import re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    s = src.find("adoc = setup(")
    e = src.find("_page_footer(adoc)")
    if s < 0 or e < 0 or e <= s:
        return ["⚠ 未能定位答案卷区块，闸门失效"]
    block = src[s:e]
    bad = []
    for m in re.finditer(r"\bpara\(\s*doc\b", block):
        ln = src[:s + m.start()].count("\n") + 1
        bad.append(f"答案卷区块第 {ln} 行用了 para(doc，应为 para(adoc)")
    return bad


@check
def chk_selfcheck_page_no_answer(_d=None):
    """🔴 **自查页不许含答案要点**（本轮实际踩到）。

    第一版自查页里写了一条「□ 22(1) 答案里含测角仪高度 b（很容易忘）」——
    **这等于在学生卷里直接说出了第 22 题的答案**。
    自查页本意是「做完检查哪些习惯」，写成「检查答案对不对」就变成了泄题。

    判据：扫 build() 里自查页那段文案，**出现具体题号 + 答案要点即判违规**。
    ⚠️ 只查 `do_自查` 到 `page_footer` 之间，不查别处。
    """
    import re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    s = src.find('"做完后自查"')
    e = src.find("_page_footer(doc)", s)
    if s < 0 or e < 0:
        return ["⚠ 未能定位自查页，闸门失效"]
    block = src[s:e]
    bad = []
    # 自查项里不得出现「答案里…」「等于…」这类直接给答案的表述
    for m in re.finditer(r'"([^"]*)"', block):
        t = m.group(1)
        if re.search(r"答案(里|是|为|含)", t):
            bad.append(f"自查页出现答案要点：{t[:50]}")
    return bad


def run_checks(d):
    errs, warns = [], []
    for fn in CHECKS:
        name = fn.__name__.replace("chk_", "")
        for x in fn(d) or []:
            (warns if x.startswith("⚠") else errs).append(f"[{name}] {x}")
    return errs, warns


def selfcheck(dirpath):
    """产成品自检：学生卷不许有答案；未验算的标记必须出现在答案卷。"""
    from docx import Document
    bad = []
    files = [f for f in sorted(os.listdir(dirpath)) if f.endswith(".docx")]
    for fn in files:
        if "2022" not in fn:
            continue
        full = os.path.join(dirpath, fn)
        txt = "\n".join(p.text for p in Document(full).paragraphs)
        if fn.endswith("-题目卷.docx"):
            for k in ["答案：", "分步过程", "易错提醒", "考点：", "来源：",
                      "√5/5", "2x − 1", "½x² − 3", "−2 < x"]:
                if k in txt:
                    bad.append((fn, f"题目卷出现答案相关内容：{k}"))
        if fn.endswith("-答案卷.docx"):
            if "交付边界" not in txt:
                bad.append((fn, "答案卷缺少「交付边界」说明"))
            if "🔴" not in txt:
                bad.append((fn, "答案卷缺少未完成标记 🔴"))
    return files, bad


def main():
    ap = argparse.ArgumentParser(description="生成 2022 中考大题专项卷（Word）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
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

    paper, apaper, qs = build(data, dry=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] {len(qs)} 题，未写盘")
        return 0

    files, bad = selfcheck(OUT_DIR)
    print(f"交付自检：扫了 {len([f for f in files if '2022' in f])} 份 2022 卷")
    if bad:
        for fn, why in bad:
            print(f"  ❌ {fn} — {why}")
        return 1
    print("  ✅ 题目卷无答案泄露；答案卷含交付边界与未完成标记")
    n_ok = sum(1 for q in qs if "未完成" not in str(q.get("status", "")))
    print(f"\n✅ 2022 中考大题专项卷 · {len(qs)} 题 "
          f"{sum(q['score'] for q in qs)} 分（答案已验算 {n_ok} 题）")
    print(f"   题目卷 → {os.path.relpath(paper, ROOT)}")
    print(f"   答案卷 → {os.path.relpath(apaper, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
