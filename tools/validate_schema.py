#!/usr/bin/env python3
"""validate_schema.py — data/ 目录数据校验

为什么需要它：这个仓库的价值全在数据可信度上。三条纪律一旦破了，
后面所有的失分热区统计、错因归类、复习排期都会跟着错，而且**很难发现**——
因为错的数据看起来和对的完全一样。所以校验必须机器做，不能靠记性。

三条硬纪律：
  1. **分值纪律**：照片上没有分值标注就是 null，绝不估算。
     `full`/`lost` 为 null 时必须 `scorePending: true`，否则热区统计会把
     「没统计」误当成「没失分」。
  2. **溯源纪律**：每条错题/试卷必须有 `sourceRaw`，且文件真实存在于
     RAW/ 下并已登记在 raw-manifest.json。断链的记录无法核对，等于没有证据。
  3. **原件纪律**：manifest 里登记的原件文件必须真实存在、sha256 一致。
     对不上说明原件被动过——不可变事实层被破坏，必须查清。

用法：
  tools/validate_schema.py           # 全量校验，有问题 exit 1
  tools/validate_schema.py --quiet   # 只输出问题，不输出 OK 项
  tools/validate_schema.py --warn   # 有问题也 exit 0（只提示不阻断管道）
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAUSES = {"概念不清", "方法没想到", "计算失误", "审题错误", "表达不规范"}
# 错因枚举是「规范 + 执行器」两层结构，定义源在 docs/方法论/错因分类与复习排期.md。
# 下面的 check_cause_enum() 负责让执行器与规范源保持一致。
CAUSE_DOC = "docs/方法论/错因分类与复习排期.md"

# 试卷索引的信任度值域。历史上有两套刻度并存：主索引用这套，
# scan_zujuan_papers.py 曾写 "B"。两套混在一列里，页面统计条会把新条目算漏。
TRUST_LEVELS = {"high", "medium", "unverified", "suspect"}


class Report:
    def __init__(self, quiet=False):
        self.errors = []
        self.warns = []
        self.oks = 0
        self.quiet = quiet

    def err(self, where, msg, hint=""):
        self.errors.append((where, msg, hint))

    def warn(self, where, msg, hint=""):
        self.warns.append((where, msg, hint))

    def ok(self):
        self.oks += 1

    def print(self):
        for w, m, h in self.warns:
            print(f"  ⚠️  {w}\n      {m}")
            if h:
                print(f"      → {h}")
        for w, m, h in self.errors:
            print(f"  ❌ {w}\n      {m}")
            if h:
                print(f"      → {h}")
        if not self.quiet:
            if not self.errors and not self.warns:
                print(f"  ✓ 全部通过（{self.oks} 项检查）")
            else:
                print(f"  {self.oks} 项通过 · {len(self.warns)} 警告 · {len(self.errors)} 错误")


def load(path, rep, label):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        rep.err(label, f"JSON 解析失败：{e}", "检查是否手工编辑时破坏了格式")
        return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 1. RAW 原件层：文件存在 + 指纹一致 ──────────────────────────
def check_raw(rep, deep=True):
    mp = os.path.join(ROOT, "data", "raw-manifest.json")
    d = load(mp, rep, "data/raw-manifest.json")
    if not d:
        return {}
    known = {}
    for it in d.get("items", []):
        f = it.get("file", "")
        known[f] = it
        ap = os.path.join(ROOT, f)
        if not os.path.exists(ap):
            rep.err("raw-manifest", f"原件不存在：{f}", "RAW 层是不可变事实层，丢失后所有结论都无法复核")
            continue
        if deep and it.get("sha256"):
            actual = sha256_of(ap)
            if actual != it["sha256"]:
                # 区分「记录错了」与「文件被改了」——两者处理方式完全不同。
                # 判据：如果 Git 里存的那一版指纹等于当前文件，说明工作区文件没被动过，
                # 是 manifest 当时写错了（入库脚本算指纹的时机问题），补记即可。
                # 若 Git 里那一版也不等于当前文件，才是原件真被改了，必须查清。
                head_sha = _git_blob_sha256(ap)
                if head_sha == actual:
                    rep.err("raw-manifest",
                            f"指纹记录与文件不符（记录有误，文件本身未动）：{f}",
                            f"manifest 记 {it['sha256'][:12]}… 实际 {actual[:12]}…；"
                            f"已入库 {it.get('originalBytes') or '?'}→{it.get('bytes') or '?'}。"
                            f"跑 tools/fix-manifest-sha.sh 可批量订正")
                elif head_sha is None:
                    rep.err("raw-manifest",
                            f"指纹不一致，且无法比对 Git 历史（文件未入库过？）{f}",
                            f"记录 {it['sha256'][:12]}… 实际 {actual[:12]}…；原件不应修改，请查清是谁改的")
                else:
                    rep.err("raw-manifest",
                            f"指纹不一致（原件疑似被改动）：{f}",
                            f"记录 {it['sha256'][:12]}… 当前 {actual[:12]}… HEAD {head_sha[:12]}…；"
                            f"三个值互不相同，说明文件在入库后被改过。不可变事实层被破坏，必须查清。")
        rep.ok()
    return known


def _git_blob_sha256(abs_path):
    """取 HEAD 里该文件的 sha256；未入库或 git 不可用时返回 None。"""
    rel = os.path.relpath(abs_path, ROOT)
    try:
        import subprocess
        out = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                             capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return None
        return hashlib.sha256(out.stdout).hexdigest()
    except Exception:
        return None


# ── 2. 错题：schema + 分值纪律 + 溯源纪律 ───────────────────────
def check_wrong(rep, rawmap):
    wd = os.path.join(ROOT, "data", "wrong")
    if not os.path.isdir(wd):
        return
    for fn in sorted(os.listdir(wd)):
        if not fn.endswith(".json"):
            continue
        rel = f"data/wrong/{fn}"
        d = load(os.path.join(wd, fn), rep, rel)
        if not d:
            continue
        if d.get("meta", {}).get("schema") != "wrong.v1":
            rep.warn(rel, "meta.schema 不是 wrong.v1", "生成器靠它识别文件类型")
        seen = set()
        for it in d.get("items", []):
            iid = it.get("id", "?")
            where = f"{rel}#{iid}"
            if iid in seen:
                rep.err(where, "id 重复", "重复 id 会导致溯源与统计出错")
            seen.add(iid)

            # 分值纪律
            full, lost = it.get("full"), it.get("lost")
            pending = it.get("scorePending")
            if full is None or lost is None:
                if pending is not True:
                    rep.err(where, "分值缺失但 scorePending 不是 true",
                            "缺分值又不标记，统计会把它当成 0 失分——这是最危险的一类错")
                elif full is None and lost is not None:
                    rep.err(where, "有 lost 但 full 为 null", "失了却不知道满分，无法算得分率")
            else:
                if not pending:
                    rep.err(where, "分值齐全但 scorePending 仍为 true",
                            "该回填分值并去掉标记，否则永远不会进热区统计")
                if lost > full:
                    rep.err(where, f"失分 {lost} > 满分 {full}", "数值不合法")
                if lost < 0:
                    rep.err(where, f"失分为负：{lost}", "数值不合法")
                if pending is False and (full is None or lost is None):
                    rep.err(where, "scoreConfirmed=false 但分值仍为空", "两个标记语义冲突")

            # 错因
            cause = it.get("cause")
            if cause and cause not in CAUSES:
                rep.warn(where, f"错因不在五类之内：{cause}",
                         f"应为 {'/'.join(sorted(CAUSES))} 之一，否则统计归类会漏")
            if cause and not (it.get("myThought") or it.get("causeNote")):
                rep.warn(where, "已判错因但没有中间步骤判读",
                         "只看最终答案会误判——「方法没想到」和「概念不清」救法完全相反")

            # 溯源纪律
            # sourceRaw 允许是字符串（单张错题照片）或数组（跨页/多张原卷）。
            src = it.get("sourceRaw")
            srcs = [src] if isinstance(src, str) else (src or [])
            if not srcs:
                rep.warn(where, "缺 sourceRaw", "无法回溯到原卷，断链的记录等于没有证据")
            for s in srcs:
                if s in rawmap:
                    continue
                if os.path.exists(os.path.join(ROOT, s)):
                    rep.err(where, f"sourceRaw 未登记进 raw-manifest：{s}",
                            "跑 tools/ingest-raw.sh 补登记")
                else:
                    rep.err(where, f"sourceRaw 指向的文件不存在：{s}",
                            "原件丢失。检查 RAW/ 下是否被误删或误移")

            # 题号
            if not it.get("qno") and not any(
                    k in str(it.get("title", "")) for k in ("第", "q")):
                rep.warn(where, "既无 qno 字段，标题里也看不出题号",
                         "生成器会用条目序号当题号，可能与卷面题号不符")
            rep.ok()


# ── 3. 试卷：结构 + 合计一致性 ─────────────────────────────────
def check_exams(rep, rawmap):
    ed = os.path.join(ROOT, "data", "exams")
    if not os.path.isdir(ed):
        return
    for fn in sorted(os.listdir(ed)):
        if not fn.endswith(".json"):
            continue
        rel = f"data/exams/{fn}"
        d = load(os.path.join(ed, fn), rep, rel)
        if not d:
            continue
        if d.get("meta", {}).get("schema") != "exam.v1":
            rep.warn(rel, "meta.schema 不是 exam.v1", "生成器靠它识别文件类型")
        exams = d.get("exams") or d.get("items") or [d]
        if isinstance(exams, dict):
            exams = [exams]
        for ex in exams:
            if not isinstance(ex, dict):
                continue
            eid = ex.get("id", fn)
            where = f"{rel}#{eid}"
            s = ex.get("score", {}) or {}
            got, full = s.get("got"), s.get("full")
            confirmed = ex.get("scoreConfirmed")

            if (got is None or full is None) and confirmed is True:
                rep.err(where, "scoreConfirmed=true 但得分为空", "标了确认却没数据")
            if confirmed is not True:
                rep.warn(where, "分值未确认",
                         "未确认前不计入失分热区统计。请家长对照卷面确认后在 app/index.html 提交")

            # 模块合计应等于总分
            mods = ex.get("modules") or []
            mfull = [m.get("full") for m in mods if m.get("full") is not None]
            if mfull and full is not None and sum(mfull) != full:
                rep.err(where, f"各模块满分之和 {sum(mfull)} ≠ 试卷总分 {full}",
                        "至少有一个模块的满分记错了")
            mgot = [m.get("got") for m in mods if m.get("got") is not None]
            if mgot and got is not None and sum(mgot) != got:
                rep.err(where, f"各模块实得之和 {sum(mgot)} ≠ 试卷得分 {got}",
                        "至少有一个模块的实得记错了")

            # 2026-10-03 第五次核验新增：模块 got 不得与同一模块的失分题自相矛盾。
            # 背景：听力模块 got=25（满分）却在同一行的 evidence 里写着「11 仅错 1 空」，
            # 又在 wrongs 里有第 11 题。同一模块不能既满分又失分——这种矛盾肉眼极难发现，
            # 因为单看哪一处都「像对的」，而且当时 0 错误（说明旧校验器没覆盖这条路径）。
            for m in mods:
                mg, mf = m.get("got"), m.get("full")
                mname = m.get("name", "?")
                if mg is None or mf is None:
                    continue
                # 模块键取第一个词（"I 听力"→I，"V-D 简答"→V-D），
                # 必须整词相等：曾用 startswith 导致 "I" 命中 "III 完形填空"、
                # "II" 也命中 "III"，凭空报出两条不存在的错误。
                def mkey(s):
                    return str(s or "").strip().split(" ")[0]
                mk = mkey(mname)
                if not mk:
                    continue
                mws = [w for w in (ex.get("wrongs") or []) if mkey(w.get("module")) == mk]
                certain = [w for w in mws if w.get("uncertain") is not True]
                if not certain:
                    continue
                if mg >= mf:
                    rep.err(where,
                            f"模块「{mname}」got={mg} 已是满分（{mf}），"
                            f"但该模块有 {len(certain)} 道确定失分题（题号 "
                            f"{'、'.join(str(w.get('qno', '?')) for w in certain)}）",
                            "同一模块不能既满分又失分。要么 got 调低，"
                            "要么把该模块的失分题改标 uncertain/causePending。"

                            "若卷面无单题分值导致实得算不出，正确做法是 got 留 null，"
                            "而不是写满分——写满分会让失分热区统计整块漏掉这一模块")

            # 逐题失分之和应等于总失分
            ws = ex.get("wrongs") or []
            wlost = [w.get("lost") for w in ws if w.get("lost") is not None]
            if wlost and got is not None and full is not None:
                if sum(wlost) != full - got:
                    rep.err(where,
                            f"逐题失分之和 {sum(wlost)} ≠ 总失分 {full - got}",
                            "可能是漏记了某道失分题，或分值填错")

            for w in ws:
                # 字段名是 qno 不是 no——写错会让所有报错都显示「第?题」，
                # 等于把错误藏起来，比不校验更糟。
                wq = w.get("qno", "?")
                wf, wl = w.get("full"), w.get("lost")
                wpending = w.get("scorePending")
                # 分值纪律对逐题失分同样成立：卷面没标分值就是没有，不能估算。
                # （2026-10-03 修：此前只校验了错题 items，exam.wrongs 完全漏网，
                #   q36/q58/q61 分值为 null 却没标 scorePending，统计会当成 0 失分。）
                if wf is None or wl is None:
                    if wpending is not True:
                        rep.err(where, f"第{wq}题 分值缺失但 scorePending 不是 true",
                                "缺分值又不标记，失分热区会把它当成没失分——最危险的一类错")
                elif wpending is True:
                    rep.err(where, f"第{wq}题 分值齐全但 scorePending 仍为 true",
                            "该回填分值并去掉标记，否则永远进不了热区统计")
                if wf is not None and wl is not None and wl > wf:
                    rep.err(where, f"第{wq}题 失分 {wl} > 满分 {wf}", "数值不合法")
                c = w.get("cause")
                if c and c not in CAUSES:
                    rep.warn(where, f"第{wq}题 错因不在五类之内：{c}", "")

            if not ex.get("verdictHtml") and not ex.get("verdict"):
                rep.warn(where, "缺诊断结论", "「主要失分在哪、下一步先补什么」是报告的价值所在")

            # sourceRaw 可以是字符串（单页）或数组（多页试卷），
            # 两种都要校验——多页试卷是常态，不能只当字符串处理。
            src = ex.get("sourceRaw")
            srcs = [src] if isinstance(src, str) else (src or [])
            if not srcs:
                rep.warn(where, "缺 sourceRaw", "无法回溯到原卷，断链的记录等于没有证据")
            for s in srcs:
                if s in rawmap:
                    continue
                if os.path.exists(os.path.join(ROOT, s)):
                    rep.err(where, f"sourceRaw 未登记进 raw-manifest：{s}",
                            "跑 tools/ingest-raw.sh 补登记")
                else:
                    rep.err(where, f"sourceRaw 指向的文件不存在：{s}",
                            "原件丢失或被误移。检查 RAW/ 下是否还在")
            rep.ok()


# ── 4. 任务队列：待补条目是否长期未处理 ────────────────────────
def check_tasks(rep, rawmap):
    tp = os.path.join(ROOT, "data", "tasks", "pending.json")
    d = load(tp, rep, "data/tasks/pending.json")
    if not d:
        return
    items = d.get("items", [])
    pend = [i for i in items if i.get("status") != "done"]
    if pend:
        names = "、".join(f"{i.get('target')}" for i in pend[:3])
        rep.warn("data/tasks/pending.json",
                 f"{len(pend)} 条教学内容待 AI 补全",
                 f"最近：{names}{'…' if len(pend) > 3 else ''}。"
                 "打开对应 HTML 补 ⚠️ 待补 段，或让 AI 跑一次补全")

    # meta.pending 与实际条目对不上——队列自己会说谎，比空队列更危险。
    actual = len(pend)
    declared = d.get("meta", {}).get("pending")
    if declared is not None and declared != actual:
        rep.err("data/tasks/pending.json",
                f"meta.pending={declared} 与实际待办数 {actual} 不符",
                "队列状态与内容不一致时，任何基于 pending 的判断都不可信")

    # 标了 done 但声称要补的字段其实没落地——「done ≠ 做完了」的机器闸门。
    # （2026-10-03 修：q19 的 steps/variants/thinkQuestions 只存在于人工精讲页 HTML，
    #   从未回写 JSON，生成器与一切数据分析都读不到，任务却已标 done。）
    #
    # 2026-10-03 第六次核验修正：本闸门原先只查 data/wrong/，exam-* 任务因
    #   .get() 返回 None 被 continue 跳过 —— 等于半个闸门。回退法自证：
    #   把 exam 的 verdictHtml/wrongs 清空后校验仍 0 错误（只多一条警告）。
    #   任务 id 前缀即 kind：wrong- → data/wrong/，exam- → data/exams/。
    wrong_items = {}
    wd = os.path.join(ROOT, "data", "wrong")
    if os.path.isdir(wd):
        for fn in sorted(os.listdir(wd)):
            if not fn.endswith(".json"):
                continue
            wdd = load(os.path.join(wd, fn), rep, f"data/wrong/{fn}")
            for it in (wdd or {}).get("items", []):
                wrong_items[it.get("id")] = it

    exam_items = {}
    ed2 = os.path.join(ROOT, "data", "exams")
    if os.path.isdir(ed2):
        for fn in sorted(os.listdir(ed2)):
            if not fn.endswith(".json"):
                continue
            edd = load(os.path.join(ed2, fn), rep, f"data/exams/{fn}")
            exs = (edd or {}).get("exams") or (edd or {}).get("items") or []
            if isinstance(exs, dict):
                exs = [exs]
            for ex in exs:
                if isinstance(ex, dict) and ex.get("id"):
                    exam_items[ex["id"]] = ex

    for t in items:
        if t.get("status") != "done":
            continue
        want = t.get("fields") or []
        if not want:
            continue
        # 任务 id 形如 wrong-w-20261002-01（对应错题 id 去掉 wrong- 前缀）
        # 或 exam-e-20261002-01（对应试卷 id 去掉 exam- 前缀）。
        tid = str(t.get("id", ""))
        if tid.startswith("wrong-"):
            target = wrong_items.get(tid[len("wrong-"):])
        elif tid.startswith("exam-"):
            target = exam_items.get(tid[len("exam-"):])
        else:
            target = None
        if target is None:
            # 前缀认得、但 id 找不到对应数据项 —— 这是断链，不是「跳过」。
            rep.err("data/tasks/pending.json",
                    f"任务 {tid} 标为 done，但在 data/ 里找不到对应数据项",
                    "任务声明要补的字段无从校验。检查 id 前缀与实际数据项的 id 是否对得上")
            continue
        missing = [f for f in want if not target.get(f)]
        if missing:
            rep.err("data/tasks/pending.json",
                    f"任务 {t.get('id')} 标为 done，但 JSON 里字段仍为空：{'、'.join(missing)}",
                    f"内容可能只写进了 {t.get('target')} 的 HTML，数据层读不到。"
                    "按纪律要同时回写 data/ 里的 JSON，否则生成器与统计都拿不到")


# ── 5. 错因枚举跨层一致性（防分叉）─────────────────────────────
def check_cause_enum(rep):
    """五类错因在仓库里有 4 处定义，规范源是 docs/方法论/错因分类与复习排期.md。

    2026-10-03 修。此前实际存在两套互相矛盾的定义：
      · 规范源 + 生成器 + app 的两个 <select> → 含「表达不规范」
      · README 的 schema 示例 + app 的 ci 映射表 → 含「时间不够」，缺「表达不规范」
    前五轮只对齐了 select，漏了 ci 与 README。
    注意 ci 映射表不是死代码：它控制错题 chip 的着色（ci[w.cause]||0），
    缺一项就意味着该错因的标签退化成默认灰 —— 肉眼看着「有标签」，看不出是配色失效。

    靠人记四份清单必然再分叉，故做机器比对。
    """
    # 规范源：取方法论表格第一列的 **加粗** 类别名
    doc = os.path.join(ROOT, CAUSE_DOC)
    src = set()
    if os.path.exists(doc):
        with open(doc, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|", line.strip())
                if m and m.group(1) in CAUSES | {"时间不够"}:
                    src.add(m.group(1))
    if src and src != CAUSES:
        rep.err(CAUSE_DOC,
                f"规范源表格的类别是 {'/'.join(sorted(src))}，与 CAUSES "
                f"({'/'.join(sorted(CAUSES))}) 不一致",
                "改哪边都行，但必须两边一致。改 CAUSES 要同步改 app 的两个 select、"
                "ci 映射表与 README")
        return
    if not src:
        rep.warn(CAUSE_DOC, "读不出错因表格的类别行", "枚举闸门对本文件失效，确认表格结构没变")

    # app/index.html：两个 <select> + ci 映射表
    app = os.path.join(ROOT, "app", "index.html")
    if os.path.exists(app):
        with open(app, encoding="utf-8") as f:
            html = f.read()
        for sel in ("w-cause", "f-cause"):
            m = re.search(rf'id="{sel}"[^>]*>(.*?)</select>', html, re.S)
            if not m:
                rep.err("app/index.html", f"找不到 #{sel} 下拉框", "家长录错题/筛选都依赖它")
                continue
            # 「全部」是筛选器的空值哨兵（value=""），不是错因类别，必须先剔除，
            # 否则闸门会把「全部」当成枚举外的值误报——闸门自己出假阳性，
            # 等于把真错误淹掉（2026-10-03 首次跑就踩到）。
            raw_opts = re.findall(
                r"<option(\s[^>]*)?>([^<]*)</option>", m.group(1))
            opts = set()
            for attrs, label in raw_opts:
                label = label.strip()
                if not label:
                    continue
                # 显式 value="" 或纯文本「全部」都视为哨兵
                if 'value=""' in (attrs or "") or label in ("全部", "—", "-"):
                    continue
                opts.add(label)
            if sel == "f-cause" and not opts:
                continue
            if opts != CAUSES:
                missing = [c for c in sorted(CAUSES) if c not in opts]
                extra = [c for c in sorted(opts) if c not in CAUSES]
                parts = []
                if missing:
                    parts.append("缺 " + "、".join(missing) + "，按纪律补上")
                if extra:
                    parts.append("枚举外有 " + "、".join(extra) + "，删掉")
                rep.err("app/index.html",
                        f"#{sel} 的选项与五类枚举不一致"
                        + (f"（现有 {'、'.join(sorted(opts))}）" if opts else ""),
                        "家长从 UI 录的题会天然违反枚举，而校验器只查数据文件不查 UI 选项，"
                        "两边永远对不上。" + "；".join(parts))
            rep.ok()

        # ci 映射表：它控制 chip 着色，缺项会让错因标签退化成默认灰
        m = re.search(r"const ci\s*=\s*\{(.*?)\}", html, re.S)
        if not m:
            rep.err("app/index.html", "找不到错因配色映射表 const ci",
                    "着色靠它，缺失会让错因标签全部退化成默认灰色")
        else:
            keys = set(re.findall(r"'([^']+)'\s*:", m.group(1)))
            if keys != CAUSES:
                rep.err("app/index.html",
                        f"ci 配色映射表覆盖 {'/'.join(sorted(keys))}，与五类枚举不一致",
                        "缺项不会报错，只会让该错因的标签显示成默认灰——"
                        "属于『看起来正常、实际失效』的静默缺陷。缺 " +
                        "、".join(c for c in sorted(CAUSES) if c not in keys))
            rep.ok()

    # README 的 schema 示例
    rdm = os.path.join(ROOT, "README.md")
    if os.path.exists(rdm):
        with open(rdm, encoding="utf-8") as f:
            r = f.read()
        m = re.search(r'"cause":\s*"?([^"\n]*?)"?\s*,?\n', r)
        if m:
            listed = {x.strip() for x in m.group(1).split("|")}
            listed = {x for x in listed if x and not x.startswith("…")}
            if listed and listed != CAUSES:
                rep.err("README.md",
                        f"schema 示例的 cause 写了 {'/'.join(sorted(listed))}，与五类枚举不一致",
                        "README 是新人第一份参照，照着抄就会录错")
            rep.ok()


def check_resources(rep, rawmap):
    """data/resources/ 是第 4 个数据入口，此前完全无人校验。

    2026-10-03 第七次核验新增。这个入口装的是公共试卷资源索引
    （data/resources/shanghai-papers.json，21 套上海中考/一模卷索引），
    与 data/exams/ 不是一回事——**它不是孩子参加过的考试**，
    纪律上「不进 data/exams/」，但它同样是仓库的事实数据，同样会被引用。

    真实缺陷（与前六轮同型，都是「0 错误也抓不到」）：
      · meta.count = 21（累积套数），meta.stats 之和 = 17（某次运行的增量），
        且 stats.fetched = 0 —— 实际已抓取 11 份全文。
      · 根因：stats 在 fetch_shanghai_papers.py 里是「本次运行增量」，
        而 scan_zujuan_papers.py 重写 count 时没重算 stats，两者语义冲突。
      · 为什么没人发现：增量统计与累积总量并排放在同一处，单看任一处都像对的。

    本闸门把 stats 变成**可验证的派生量**：三项之和必须恒等于 count，
    且 fetched 必须等于实际有 rawFile 的条数、文件必须真实存在。
    """
    rd = os.path.join(ROOT, "data", "resources")
    if not os.path.isdir(rd):
        return
    for fn in sorted(os.listdir(rd)):
        if not fn.endswith(".json"):
            continue
        rel = f"data/resources/{fn}"
        d = load(os.path.join(rd, fn), rep, rel)
        if not d:
            continue
        papers = d.get("papers")
        if not isinstance(papers, list):
            continue

        # meta.count 与实际条数
        declared = d.get("meta", {}).get("count")
        if declared is not None and declared != len(papers):
            rep.err(rel, f"meta.count={declared} 与 papers 实际 {len(papers)} 条不符",
                    "count 是被文档和页面直接引用的数字，对不上会让所有引用都变成错的")

        # stats 三项之和必须恒等于 count（增量统计与累积总量混用时的典型症状）
        st = d.get("meta", {}).get("stats") or {}
        if st:
            parts = {k: st.get(k) for k in ("fetched", "metaOnly", "failed")
                     if isinstance(st.get(k), int)}
            if len(parts) == 3:
                s = sum(parts.values())
                if declared is not None and s != declared:
                    rep.err(rel,
                            f"meta.stats 三项之和 {s}（{'+'.join(str(v) for v in parts.values())}）"
                            f" ≠ meta.count {declared}",
                            "stats 必须是由 papers 现算的派生量，三项之和恒等于 count。"
                            "若 stats 记的是「本次运行增量」，它落盘即失去意义"
                            "（下次运行就变），且必然与累积总量对不上")
                # fetched 必须等于实际有 rawFile 的条数
                real_fetched = sum(1 for p in papers if isinstance(p, dict) and p.get("rawFile"))
                if st.get("fetched") != real_fetched:
                    rep.err(rel,
                            f"meta.stats.fetched={st.get('fetched')} 但实际有 rawFile 的是 {real_fetched} 条",
                            "「已抓取」是判断资源库真实库存的数字，"
                            "写错会让人以为资源没抓到而重复抓取")
            # unchanged 是运行时概念（本次没重新下载），落盘后无从考证，
            # 与 fetched 语义重叠，会让总数对不上。见到即提示清除。
            if "unchanged" in st:
                rep.err(rel, "meta.stats 含 unchanged 字段",
                        "unchanged 是单次运行概念，与 fetched 重叠且落盘即失真，"
                        "会让三项之和 ≠ count。已从两个采集脚本中移除")

        # 每条 rawFile 必须真实存在，且已登记 manifest（溯源纪律同样适用）
        for p in papers:
            if not isinstance(p, dict):
                continue
            rf = p.get("rawFile")
            if not rf:
                continue
            if rf not in rawmap and not os.path.exists(os.path.join(ROOT, rf)):
                rep.err(rel, f"{p.get('id', '?')} 的 rawFile 指向的文件不存在：{rf}",
                        "索引声称已归档但文件不在，断链的记录等于没有证据")
        rep.ok()


def check_embedded_snapshots(rep):
    """页面内嵌的数据副本必须与数据层逐字节相同（第八次核验新增）。

    2026-10-03 第八次核验发现的第八类缺陷，也是前七轮**全部漏网的方向**：
    前七轮查的全是「数据层内部是否自洽」「数据层与手写数字是否一致」，
    从未查过**页面里冻结的那份数据副本**。

    真实缺陷：docs/实战表/上海试卷地图.html 第 93 行 `const DATA = {...}`
    是 data/resources/shanghai-papers.json 的一份**手工拷贝**，
    拷贝之后数据层被第七轮修正（count 17→21、fetched 0→11、删除 unchanged），
    **页面一个字节都没跟着变**。后果：
      · 页面少渲染 4 张试卷卡片（2021中考数学 / 2022中考数学 / 2022真题-武 / 2026道法）
      · meta.count 与 meta.stats 是**死字段**，页面上没有任何一处显示它们
        ——统计数字错了反而没人看得见，真正吃亏的是那 4 张消失的卡片
      · 页面还带着已从数据层删除的 `unchanged: 10` 概念

    为什么七道闸门都抓不到：闸门全部作用于「磁盘上的 JSON 文件」，
    而漂移发生在「JSON 被拷进 HTML 之后」。按定义 grep JSON 查不到它；
    打开页面肉眼看，四张卡片少了几张也不会有人立刻察觉。

    处置原则不是「再手工同步一次」（那只是把漂移推迟到下次采集），
    而是**让副本可被机器验证**——漂移一旦产生就报错，采集脚本改动即刻可见。
    """
    pairs = [
        ("docs/实战表/上海试卷地图.html", "const DATA = ",
         "data/resources/shanghai-papers.json", "上海试卷地图页"),
    ]
    for rel_html, marker, rel_json, label in pairs:
        hp = os.path.join(ROOT, rel_html)
        if not os.path.exists(hp):
            continue
        src = open(hp, encoding="utf-8").read()
        i = src.find(marker)
        if i < 0:
            continue
        i += len(marker)
        # 平衡括号，取出完整对象字面量
        depth, j, instr, esc = 0, i, False, False
        while j < len(src):
            c = src[j]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
            else:
                if c == '"':
                    instr = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        else:
            rep.err(rel_html, f"{marker.strip()} 对象未闭合，无法校验内嵌副本",
                    "内嵌数据块结构损坏，页面可能整体报错")
            continue
        block = src[i:j + 1]

        jd = os.path.join(ROOT, rel_json)
        if not os.path.exists(jd):
            continue
        disk = load(jd, rep, rel_json)
        if not disk:
            continue
        expect = json.dumps(disk, ensure_ascii=False, indent=1)
        if block == expect:
            continue

        # 漂移已发生 —— 报出具体差异，而不是只说「不一致」
        try:
            pg = json.loads(block)
        except Exception as e:
            rep.err(rel_html, f"内嵌副本不是合法 JSON：{e}",
                    f"{label} 的 {marker.strip()} 块已损坏")
            continue
        ds, ps = disk.get("meta", {}), pg.get("meta", {})
        diffs = []
        dc, pc = ds.get("count"), ps.get("count")
        if dc != pc:
            diffs.append(f"count 数据层 {dc} / 页面 {pc}")
        dsta, psta = ds.get("stats") or {}, ps.get("stats") or {}
        for k in sorted(set(dsta) | set(psta)):
            if dsta.get(k) != psta.get(k):
                diffs.append(f"stats.{k} 数据层 {dsta.get(k)} / 页面 {psta.get(k)}")
        dnames = {p.get("id") for p in disk.get("papers", []) if isinstance(p, dict)}
        pnames = {p.get("id") for p in pg.get("papers", []) if isinstance(p, dict)}
        if dnames != pnames:
            only_disk = sorted(dnames - pnames)
            only_page = sorted(pnames - dnames)
            if only_disk:
                diffs.append(f"数据层有 {len(only_disk)} 条页面没有：{'、'.join(only_disk[:6])}")
            if only_page:
                diffs.append(f"页面有 {len(only_page)} 条数据层没有：{'、'.join(only_page[:6])}")
        rep.err(rel_html,
                f"{label}内嵌的数据副本与 {rel_json} 已漂移：" + "；".join(diffs),
                "页面把数据层拷了一份手工副本，两边从此各活各的。"
                "副本漂移不会让页面报错，只会让内容静默过期——"
                "少掉的卡片没人会发现。"
                "修复：用数据层重新生成该内嵌块"
                f"（json.dumps(d, ensure_ascii=False, indent=1)），"
                "不要手工改数字")
    rep.ok()


def check_no_fabricated_score(rep, rawmap):
    """页面不得把无卷面依据的折算值当作「实得分数」引用（第九次核验新增）。

    2026-10-03 第九次核验发现的第九类缺陷：
    第五轮已判定「modules[I 听力].got 必须留 null」（听力 C 只标 10 分共 5 空、
    无单空分值，24 与 25 都是 10÷5 的等分折算），同一页的提示条也写明
    「两页都不要再引用具体数字当实得」——**但同页另一处仍写着「听力大段正确（24/25）」**。

    为什么前八道闸门都抓不到：
      · 第八轮那道（check_embedded_snapshots）只比对**结构化内嵌副本**
        （试卷地图页的 const DATA），而这里是**手写进正文的散文数字**，
        既不在 JSON 里、也不在 const 块里，压根不在任何比对范围；
      · 前七轮查的是「数据层是否自洽」，这里的数据层本来就是对的（null），
        错的是页面没跟上——**单看数据层永远看不出页面在引用一个被否决的数字**；
      · 人工肉眼读页面，「24/25」出现在提示条附近，很容易以为已被处理。

    可复用的规律：**同一个事实散落在 N 处，改一处就漏 N−1 处。**
    提示条写清楚了不等于正文里没有残留。

    本检查的判定口径（**故意收窄，避免闸门自身出假阳性**）：
    只审「**数据层 got 为 null 的模块**」——这些模块是纪律明确判定过
    「无卷面依据、不得给实得」的。页面若在讲这个模块时写出 X/Y 形式的实得，即为违规。

    为什么不用「扫描全站所有 X/Y」：第九次核验首版就是这么写的，结果 11 条里
    **9 条是假阳性**——数学比例（AF:FC=2:3）、选择项（A. 3/2）、
    题号（第 35/36 题）、样板页的历史基线分（119/150）全被误报。
    闸门出假阳性等于把真错误淹掉，等于没闸门。
    「按定义找一个能自动判定的窄口径」比「覆盖广但要靠关键词打补丁」可靠。
    """
    # 数据层里 got 明确为 null 的模块 → 这些模块页面不得给出 X/Y 实得
    nullmod = {}   # 模块名 -> 该卷 full
    for rel_j in glob.glob(os.path.join(ROOT, "data", "exams", "*.json")):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for ex in d.get("exams", []):
            for m in ex.get("modules", []):
                if m.get("got", "missing") is None and m.get("gotNote"):
                    nullmod.setdefault(m.get("name", ""), m.get("full"))
    if not nullmod:
        rep.ok()
        return
    SCORE_PAIR = re.compile(r"(\d+)\s*/\s*(\d+)")
    pages = glob.glob(os.path.join(ROOT, "docs", "**", "*.html"), recursive=True)
    pages += [os.path.join(ROOT, "app", "index.html")]
    hits = 0
    for p in sorted(pages):
        rel = os.path.relpath(p, ROOT)
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue
        for m in SCORE_PAIR.finditer(src):
            g, b = m.group(1), m.group(2)
            if int(g) == int(b) or int(g) > int(b) or int(b) == 0:
                continue
            if str(b) not in {str(v) for v in nullmod.values() if v}:
                continue
            # 窗口取紧邻：解释句必须与该数字同处一句话，否则不算豁免。
            # 第九次核验踩过的坑：窗口开 300 字符时，
            # 上一条 <li> 里「不给 24/25 这类实得数字」的说明
            # 会替下一条 <li> 里的违规 24/25 豁免 ⇒ 闸门失灵却报 0 错误。
            # 边界对齐到句读：只认同句内的免责说明。
            lo = max(0, m.start() - 160)
            hi = min(len(src), m.end() + 120)
            seg = src[lo:hi]
            # 截到数字所在句子的边界（。！？；与换行）
            for ch in "。！？；\n":
                k = seg.rfind(ch, 0, m.start() - lo)
                if k >= 0:
                    lo2 = lo + k + 1
                    break
            else:
                lo2 = lo
            for ch in "。！？；\n":
                k = seg.find(ch, m.end() - lo)
                if k >= 0:
                    hi2 = lo + k
                    break
            else:
                hi2 = hi
            seg = src[lo2:hi2]
            text = re.sub(r"<[^>]+>", " ", seg).replace("\n", " ")
            # 提及该模块 = 上下文出现模块名或其简称
            mod_hit = None
            for name in nullmod:
                short = name.split()[-1] if " " in name else name
                if name in text or short in text:
                    mod_hit = name
                    break
            if not mod_hit:
                continue
            # 同句内解释了「为何不作实得」才是合法引用
            if re.search(r"(不(作|给|估算|折算)|无(单|卷面|依据)|未标注|"
                         r"只用于柱长|不得当|折算|存疑|待(家长|老师|确认)|留\s*null)", text):
                continue
            ln = src[:m.start()].count("\n") + 1
            rep.err(rel, f"第 {ln} 行在「{mod_hit}」上出现 {g}/{b} 实得分数，"
                         f"但该模块数据层 got 留 null（无卷面依据）",
                    "页面把纪律已否决的折算值当实得在用。"
                    "改成定性描述（如「15 题只错 1 空」）而不是 X/Y；"
                    "真要引用请在同处写明为何不折算。")
            hits += 1
    if hits:
        rep.ok()
        return
    rep.ok()


def check_papers_derived(rep):
    """试卷索引的派生字段与采集脚本的字段名必须自洽（第十次核验新增）。

    第十次核验抓到第十类缺陷：**同一个概念在仓库里有两套拼写**。
      · fetch_shanghai_papers.py 写`trusthLevel`（Level 前多一个 h，21 条全是它）
      · scan_zujuan_papers.py 写 `trustLevel`（正确拼写）
      · 页面六处读 `p.trusthLevel`
    两个采集脚本写的是**同一个JSON 文件**，所以跑一次 zujuan 采集，
    索引里就会同时出现两种键。后果全是静默的：
      · 页面统计条「标注存疑」恒为 0——`filter(p=>p.trusthLevel==="suspect")`
        对新条目取到 undefined，一条都不匹配
      · 存疑徽章与筛选器对新条目全部失效
      · 页面不报错、卡片照常渲染，肉眼完全看不出已经坏了
    值域也分叉：主文件用 high/medium/unverified/suspect，zujuan 脚本曾写 "B"。

    同一轮还发现 `subjects` 是**手工维护的派生列表**（第七类的同型复发）：
    它漏了「道德与法治」，而页面统计条正是读它 ⇒ 科目数少算 1。
    派生量原则：能被算出来的不要手写。
    """
    rel = "data/resources/shanghai-papers.json"
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return
    d = load(p, rep, rel)
    if not d:
        return
    papers = [x for x in d.get("papers", []) if isinstance(x, dict)]

    # 1) trustLevel 拼写必须唯一，且不得残留历史错拼
    typo = [x.get("id") for x in papers if "trusthLevel" in x]
    if typo:
        rep.err(rel, f"{len(typo)} 条仍用错拼 trusthLevel：{'、'.join(typo[:5])}",
                "这是历史错拼（Level 前多一个 h）。页面按 trustLevel 读，"
                "这些条目会静默失去存疑徽章/筛选/统计——"
                "页面不报错，只是内容悄悄失效。改数据层字段名，别改页面。")
    # 2) 每条都必须有 trustLevel，且取值在主值域内
    for x in papers:
        v = x.get("trustLevel")
        if v is None:
            rep.err(rel, f"{x.get('id', '?')} 没有 trustLevel",
                    "存疑徽章、筛选与「标注存疑」统计条都读它，缺了就当非存疑")
        elif v not in TRUST_LEVELS:
            rep.err(rel, f"{x.get('id', '?')} 的 trustLevel={v!r} 不在值域内",
                    f"值域是 {'/'.join(sorted(TRUST_LEVELS))}。"
                    "zujuan 采集脚本曾写 'B'，那是另一套刻度，会让统计条把新条目算漏")

    # 3) 派生列表必须与 papers 现算一致（不手写派生量）
    for key, pick in (("subjects", "subject"), ("regions", "region")):
        listed = d.get(key)
        if listed is None:
            continue
        actual = {x.get(pick) for x in papers if x.get(pick)}
        if set(listed) != actual:
            miss = sorted(actual - set(listed))
            extra = sorted(set(listed) - actual)
            parts = []
            if miss:
                parts.append("缺 " + "、".join(miss))
            if extra:
                parts.append("多 " + "、".join(extra))
            rep.err(rel, f"{key} 与 papers 现算不一致（{'；'.join(parts)}）",
                    f"{key} 是 papers 的派生量，必须现算。页面统计条与筛选器读它，"
                    "手写就会漏项——列表漏了，页面上那个维度直接少一块，"
                    "且不报错。改法：由papers 现算重写，不要手工补名字")
        rep.ok()
    rep.ok()


def check_doc_numbers(rep):
    """docs/ 下的手工统计数字必须与数据层一致（第十次核验新增）。

    前九轮修的都是「数据层 ↔ 页面内嵌副本」，`docs/*.md` 是**第五个**漂移面：
    它是纯文本，既不在 JSON 也不在 `const` 块里，任何按结构比对的闸门都覆盖不到。
    真实缺陷：上海试卷采集-现状与阻塞.md 的科目表合计 17，而 count=21
    （漏「全科」3 + 「跨学科」1）；同页正文另有 manifest 数漂移的历史前科。

    这里只对**能由数据层自动判定**的事实建闸门（合计恒等、计数比对），
    不做全文数字正则扫描——覆盖广但靠关键词打补丁的闸门，
    假阳性会把真错误淹掉（第九轮已踩过：11 条里9 条是假阳性）。
    """
    rel = "docs/试卷库建设/上海试卷采集-现状与阻塞.md"
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return
    txt = open(p, encoding="utf-8").read()

    res_p = os.path.join(ROOT, "data/resources/shanghai-papers.json")
    man_p = os.path.join(ROOT, "data/raw-manifest.json")
    if not (os.path.exists(res_p) and os.path.exists(man_p)):
        return
    res = load(res_p, rep, "data/resources/shanghai-papers.json")
    man = load(man_p, rep, "data/raw-manifest.json")
    if not res or not man:
        return
    papers = [x for x in res.get("papers", []) if isinstance(x, dict)]

    # 1) 文档正文声明的 manifest 条数必须与实际一致
    items = man.get("items", [])
    n_img = sum(1 for i in items if i.get("kind") == "image")
    n_txt = sum(1 for i in items if i.get("kind") == "text")
    m = re.search(r"manifest\s*(\d+)\s*条", txt)
    if m and int(m.group(1)) != len(items):
        rep.err(rel, f"文档写 manifest {m.group(1)} 条，实际 {len(items)} 条"
                    f"（image {n_img} + text {n_txt}）",
                "手工数字漂移过至少两次。以 data/raw-manifest.json 为准，"
                "改文档不要改数据")
    # 2) 文档里若列出科目表，各行套数之和必须等于 count
    m = re.search(r"九类合计\s*\*\*(\d+)\s*套\*\*", txt)
    if m and int(m.group(1)) != len(papers):
        rep.err(rel, f"文档写科目表合计 {m.group(1)} 套，索引实际 {len(papers)} 套",
                "科目表漏列的科目不会让页面报错，只会让读者按缺项做决策。"
                "以 data/resources/shanghai-papers.json 的 papers 现算为准")
    if not (m or re.search(r"manifest\s*\d+\s*条", txt)):
        rep.warn(rel, "读不出文档里的统计数字",
                 "本闸门对本文件失效，确认表格结构没变")
        return
    rep.ok()


def check_data_readme(rep):
    """data/README.md 是**写入方的 schema 规范**，它的口径错了会污染下轮数据。

    第十一次核验发现的最严重缺陷：前���轮的闸门全部在检查「已经写进 JSON 的数据对不对」，
    从没检查过**「指导人怎么写数据的文档对不对」**。

    `data/README.md` 当时写着：

        | `full` / `lost` | 是 | 满分 / 实失分，**缺了这两个字段就做不了失分点分析** |

    这与项目**分值纪律的核心原则直接相反**——纪律要求「照片上没有分值标注就是 null，
    绝不估算」，实际两条数学错题也都是 `null + scorePending`。按 README 写出来的数据，
    要么把 `full`/`lost` 当成必填数字（诱导估算），要么不写（无法区分「没统计」与「没失分」）。

    > 与前十类的本质差别：前十类是**数据错了**，这一类是**规范教人把纪律写反**。
    > 修数据只是改一个条目，改规范是改掉下一轮数据继续错下去的原因。

    这里同样只对**能自动判定的事实**建闸门（口径关键词、文件清单与磁盘一致），
    不做全文数字正则扫描（第九轮已证明那样假阳性 9/11）。
    """
    rel = "data/README.md"
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return
    txt = open(p, encoding="utf-8").read()

    # 1) 分值口径：必须同时出现「可为 null」与 scorePending，且不得写成无条件必填
    has_null_rule = ("可为 null" in txt) or ("允许是 `null`" in txt) or ("值为 `null`" in txt)
    has_sp = "scorePending" in txt
    # 必填列有两个写法：表头写「必填」时填 `| 是 |`，也可能直接写「必填」二字。
    # 只判「必填」二字会漏掉 `| 是 |` 这种——**闸门自身失效却报 0 错误**，第九轮同型坑。
    bad_line = None
    for line in txt.splitlines():
        if "`full` / `lost`" not in line and "`full`/`lost`" not in line:
            continue
        if "scorePending" in line or "可为 null" in line or "允许是" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # 字段说明表形如 | 字段 | 必填 | 说明 |
        req = cells[1] if len(cells) >= 3 else ""
        if req in ("是", "必填", "必填", "Y", "yes"):
            bad_line = line.strip()
            break
    if bad_line is not None:
        rep.err(rel, f"分值字段仍被写成无条件必填：{bad_line}",
                "与分值纪律相反。正确口径是「键必须存在、值可为 null，"
                "配 scorePending: true」。照原文写会把纪律写反")
    if not (has_null_rule and has_sp):
        rep.err(rel, "分值纪律（null + scorePending）在本文档里读不到",
                "README 是新人/AI 的第一份参照，缺了这条就会诱导估算。"
                "应写明：卷面无分值标注则留 null + scorePending，绝不估算")
        return
    # 章节本身必须还在，且必须是**标题行**。
    # 首版用 `"分值纪律" in txt` 全文匹配，被文档里两处「见下方分值纪律一节」的交叉引用顶住，
    # 改掉章节标题反而报 0 错误——**闸门自身的漏洞比没建闸门更危险**。
    has_section = any(
        ln.lstrip().lstrip("#").lstrip().startswith(("⚠️", "W", "w", "*"))
        and "分值纪律" in ln
        for ln in txt.splitlines()
        if ln.lstrip().startswith("#")
    )
    if not has_section:
        rep.err(rel, "「分值纪律」章节不见了",
                "只剩零散关键词、章节标题被改写，读者找不到这条纪律。"
                "章节名是给人看的锚点，不是装饰。"
                "注意：不能用全文关键词判定，文档里的交叉引用会造成假阴性")
        return

    # 3) 同一句话在根 README 里还有第二份副本（铁律五：同一事实散落 N 处）。
    # 只改 data/README.md 而不管根 README，等于把漂移推迟到下一个照根 README 干活的人。
    rel2 = "README.md"
    p2 = os.path.join(ROOT, rel2)
    if os.path.exists(p2):
        t2 = open(p2, encoding="utf-8").read()
        # 根 README 用散文句「必须记 full（满分）和 lost（实失）」
        anchor = "必须记 `full`"
        if anchor in t2:
            for i, ln in enumerate(t2.splitlines()):
                if anchor not in ln:
                    continue
                # 该行往下 3 行内必须出现 null / scorePending 的口径说明
                window = "\n".join(t2.splitlines()[i:i + 4])
                if "scorePending" not in window:
                    rep.err(rel2, "根 README 仍把 full/lost 写成无条件下必填",
                            "同一事实散落两处（根 README + data/README.md）。"
                            "只改一处 = 下一个人照另一处干活继续写反。"
                            "须补一句「值可为 null，无卷面分值标注时配 scorePending」")
                    break

    # 2) 文件清单表的状态列必须与磁盘实际一致（派生量不手写）
    st = {"exams/": "待建", "wrong/": "待建", "progress/": "待建"}
    rows = {}
    for line in txt.splitlines():
        for d in st:
            if line.startswith(f"| `{d}`") or line.startswith(f"| `{d}`".replace("/", "/ ")):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 3:
                    rows[d] = cells[2].strip("* ")
    if not rows:
        rep.warn(rel, "读不出文件清单表",
                 "本闸门对清单表失效，确认表格结构没变")
        return
    for d, claimed in rows.items():
        ap = os.path.join(ROOT, "data", d.rstrip("/"))
        real_files = []
        if os.path.isdir(ap):
            real_files = [f for f in os.listdir(ap) if not f.startswith(".")]
        nonempty = bool(real_files)
        truth = "已建" if nonempty else "待建"
        # 声称待建但目录有实际内容 / 声称已建但目录空 → 漂移
        if nonempty and claimed.startswith("待建"):
            rep.err(rel, f"`{d}` 标「{claimed}」，但目录里已有 {len(real_files)} 个文件",
                    "清单表是纯文本，前面九轮的闸门覆盖不到。"
                    "以磁盘实际为准改文档，不要改数据")
        elif not nonempty and not claimed.startswith("待建"):
            rep.err(rel, f"`{d}` 标「{claimed}」，但目录里没有任何实际文件",
                    "以磁盘实际为准改文档，不要凭空声称已建")
    rep.ok()


def check_module_got_grounded(rep, rawmap):
    """模块 got 必须有卷面依据：含存疑题、或自述为折算/估计时不得给具体值（第十二次核验新增）。

    2026-10-04 第十二次核验抓到的缺陷，是第五次核验的**同型复发**：
      · 第五轮判定 `modules[I 听力].got` 必须留 null——听力 C 只标「10 分」共 5 空、
        无单空分值，24 与 25 都是 10÷5 的等分折算；
      · 但同一份数据里 `III 完形填空 got=7`、`IV 句子完成 got=8` **照样写着具体数**。
        放大原卷确认：III 标题只标「（8 分）」共 8 空、IV 标题只标「（10 分）」共 5 空，
        **同样没有单题分值**——7 是「8 题对 7 题」的**题数**被当成了分数，
        8 是 10−2 的等分折算（其 gotNote 甚至自述「保守估计」）。

    为什么前十一道闸门一道都抓不到：
      · 第五轮那道只审「got 是否与本模块确定失分题矛盾」，III/IV 的 got 小于满分，
        不构成矛盾，闸门判定「合法」；
      · 第九轮那道只审**页面**是否引用 got 为 null 的模块当时的 X/Y——
        数据层自己就写着 7 和 8，nullmod 集合里根本没有 III/IV，闸门无从触发；
      · 人的注意力被「听力已经处理过了」锚定，**同一个错误换个模块就看不见**。

    可复用的规律：**修掉一个实例不等于修掉这一类。**
    纪律若只在具体模块上落实（"听力的 got 要留 null"），
    下一个录入者会把同一逻辑套到 III/IV/V-C 上再犯一次。
    所以这里按**可自动判定的口径**落成闸门，而不是靠人记住「还有哪几个模块要留空」。

    判定口径（两个信号，都不依赖具体模块名）：
      ① 该模块存在 `uncertain: true` 的失分题 → 得分天然算不出（红笔未明示即不推定为 0）；
      ② 该模块的 gotNote 自述含「折算 / 估计 / 等分」→ 说明这个值自己都承认不是实得。
    命中任一即报错。留 null + gotNote 写明理由是正确做法。
    """
    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "exams", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for ex in d.get("exams", []):
            where = f"{rel}#{ex.get('id', '?')}"
            ws = ex.get("wrongs") or []

            def mkey(s):
                return str(s or "").strip().split(" ")[0]

            for m in ex.get("modules", []):
                mg, mname = m.get("got"), m.get("name", "?")
                if mg is None:
                    continue          # 已留 null，合法
                mk = mkey(mname)
                unc = [w.get("qno") for w in ws
                       if mkey(w.get("module")) == mk and w.get("uncertain") is True]
                note = str(m.get("gotNote") or "")
                self_admit = re.search(r"(折算|估计|等分)", note)
                if unc:
                    rep.err(where,
                            f"模块「{mname}」got={mg}，但该模块有存疑失分题"
                            f"（题号 {'、'.join(str(x) for x in unc)}）",
                            "存疑题的红笔未明示扣分，**不能按「没红笔就算对」推定为 0 失分**，"
                            "因此该模块实得算不出。正确做法：got 留 null + gotNote 写明存疑题号。"
                            "（第五次核验已用同一理由把听力 got 改为 null，III/IV 应比照办理）")
                elif self_admit:
                    rep.err(where,
                            f"模块「{mname}」got={mg}，但其 gotNote 自述为"
                            f"「{self_admit.group(0)}」值",
                            "自己都写明是折算/估计，就不是实得分数。"
                            "正确做法：got 留 null，把折算依据写进 gotNote 供人参考，"
                            "不要让统计把它当成实际得分")
    rep.ok()


def check_page_matches_data(rep, rawmap):
    """页面写的 X/Y 必须与数据层一致，且必须能看见夹在标签里的数字（第十二次核验新增）。

    第十二次核验同时暴露了第九轮那道闸门的**两个盲区**：

    盲区一（看不见夹标签的写法）：`check_no_fabricated_score` 的正则是
        `(\\d+)\\s*/\\s*(\\d+)`，只能匹配纯文本的 `7/8`。
        而柱状图与表格里写的是 `<b>7</b> / 8`、`style="text-align:right">7<`——数字被
        `<b>`、`</b>` 隔开，**正则匹配不到，闸门静默放过**。
        人工读页面看到的是「7 / 8」，格式差异只有看源码才发现。

    盲区二（只审单向、不审打架）：那道闸门只管「null 模块被引用」，
        管不到**两个非 null 模块在两页上写同一个事实却数字不同**——
        本轮《整卷分析》写句子完成 9/10、《试卷拆解》与数据层都是 8，谁也不报警。

    本检查按「事实」而非按「文件」建闸门（第九轮铁律五）：
      · 扩展正则，允许数字之间夹 HTML 标签；
      · 从数据层取出每个模块的 (题号区间, 满分, got)，
        在页面里找同名模块的 X/Y，与数据层逐一比对，不一致即报错。
    满分是卷面直接标注的，**任何页面写错满分都是硬错误**；
    got 为 null 的模块若页面仍写 X/Y，则必须在同句说明为何不折算（沿用第九轮豁免口径）。
    """
    facts = []   # (rel, 模块名, 题号区间, 满分, got)
    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "exams", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for ex in d.get("exams", []):
            for m in ex.get("modules", []):
                facts.append((rel, m.get("name", ""), m.get("qRange", ""),
                              m.get("full"), m.get("got"), m.get("gotNote")))
    if not facts:
        rep.ok()
        return
    # 允许数字之间夹标签：<b>7</b> / 8、>7</td>…/10<
    PAIR = re.compile(r"(\d+)\s*(?:</?[a-zA-Z][^>]{0,40}>\s*)*/\s*(\d+)")
    # 模块表里「实得」常写成**裸数字**（<td>9</td>）而不是 9/10，
    # 只靠 PAIR 会完全看不见这类表——本轮注入「II 语法实得改 9」即漏网。
    # 口径收窄到**同一 <tr> 行**：先定位含模块名的表格行，再看该行里裸数字是否等于 got。
    ROW = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
    pages = glob.glob(os.path.join(ROOT, "docs", "**", "*.html"), recursive=True)
    hits = 0
    for p in sorted(pages):
        rel = os.path.relpath(p, ROOT)
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue
        # —— A. 表格行里的裸数字实得 ——
        # 口径必须**按列位置**判定，不能只看「这一行里有哪些数字」：
        # 早先版本把题号列「49-56」里的 49/56 也当成候选数字，导致
        # V-C 完形（数据层与页面都是 8，完全正确）被误报——闸门出假阳性等于把真错误淹掉。
        # 现在只取**题号列之后、最后一列之前**的数字（模块得分表的实得列就在这个位置）。
        for row in ROW.findall(src):
            plain = re.sub(r"<[^>]+>", " ", row)
            plain = re.sub(r"\s+", " ", plain)
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)]
            for _, mname, qrange, mfull, mgot, mnote in facts:
                if mfull is None:
                    continue
                short = mname.split()[-1] if " " in mname else mname
                # 模块名必须独占首列，避免「V-C 完形」被「阅读理解」等行误命中
                if not cells or not (cells[0].strip() == mname.strip()
                                     or cells[0].strip() == short.strip()):
                    continue
                # 题号列恒为第 2 列（含区间），末列是依据说明，均不参与
                mid = [c for c in cells[2:-1] if re.fullmatch(r"\d+", c)]
                if not mid:
                    continue
                vals = {int(c) for c in mid}
                if mgot is None:
                    # 数据层判无依据：表格里就不该出现裸的实得数字。
                    # ⚠️ 这条不能省：早先版本开头就 `if mgot is None: continue`，
                    # 结果「数据层已留 null、页面却把实得填回 7 和 8」这种状态完全查不出来——
                    # 而它恰恰是本轮修掉的那个缺陷的**页面侧残留**。
                    # 满分列本身是卷面直接标注的，必须豁免；其余裸数字即违规实得。
                    extra = vals - {int(mfull)}
                    if extra:
                        rep.err(rel, f"「{mname}」数据层 got 留 null（无卷面依据），"
                                     f"但表格行里仍有实得数字 {sorted(extra)}",
                                "模块表要和数据层一致：实得列写「待确认」、得分率写「—」，"
                                "不要把等分折算值填回去。")
                        hits += 1
                    continue
                # 实得列应等于 got；满分列应等于 full。
                # 只在「拿到了 got 但同列出现了既非满分也非 got 的第三个值」时报警。
                if int(mgot) not in vals:
                    continue
                extra = vals - {int(mgot), int(mfull)}
                if extra:
                    rep.err(rel, f"「{mname}」表格行出现 {sorted(extra)}，"
                                 f"与数据层（满分 {mfull}、实得 {mgot}）都不符",
                            "同一模块在页面上不能有三个数。核对是哪一列填错。")
                    hits += 1
        # —— B. X/Y 形式（含柱状图与正文）——
        for _, mname, qrange, mfull, mgot, mnote in facts:
            if not mname or not mfull:
                continue
            short = mname.split()[-1] if " " in mname else mname
            # 页面上用来指代该模块的写法：模块全名、简称、题号区间
            keys = {k for k in (mname, short, qrange) if k}
            if not any(k in src for k in keys):
                continue
            for mo in PAIR.finditer(src):
                g, b = mo.group(1), mo.group(2)
                if int(b) != int(mfull) or int(g) == int(b):
                    continue
                # 定位该数字所属的「块」：向上找最近的标签开边界（如 <div class="bar-row">），
                # 向下同理取到该块结束。
                # ⚠️ 不能只取「所在句子」：柱状图里数字独占一行
                #   `<div class="bar-val"><b>9</b> / 10</div>`，
                #   剥掉标签后整句只剩 "9 / 10"，**模块名在上一行的 bar-label 里**，
                #   按句读取窗口会漏掉它 ⇒ 闸门对柱状图完全失灵（首版注入违规报 0 错误）。
                blo = src.rfind("<div", 0, mo.start())
                bhi = src.find("</div>", mo.end())
                lo = blo if blo >= 0 else max(0, mo.start() - 300)
                hi = (bhi + 5) if bhi >= 0 else min(len(src), mo.end() + 300)
                # 块太小时向外扩一屏，保证能带上模块名与免责说明
                if hi - lo < 260:
                    lo = max(0, lo - 200)
                    hi = min(len(src), hi + 200)
                text = re.sub(r"<[^>]+>", " ", src[lo:hi])
                text = re.sub(r"\s+", " ", text)
                if not any(k in text for k in keys):
                    continue
                if mgot is not None and int(g) == int(mgot):
                    continue          # 与数据层一致
                if mgot is None:
                    # 数据层已判无依据：同句说明理由才算合法引用。
                    # ⚠️ 这里**只能**看页面同句的措辞，绝不能用数据层的 gotNote 当豁免依据——
                    #   首版写成 `... or mnote`，而 got=null 的模块 gotNote 必然非空，
                    #   等于给所有违规数字发免死金牌，注入违规后闸门报 0 错误。
                    #   （与第九轮「上一条 <li> 的免责说明替下一条豁免」是同一个形态：
                    #     豁免范围一旦越过句子边界，闸门就静默失效。）
                    if re.search(r"(不(作|给|估算|折算)|无(单|卷面|依据)|未标注|"
                                 r"只用于|不得当|折算|等分|存疑|待(家长|老师|确认)|"
                                 r"留\s*null|不给|无依据|非得分)", text):
                        continue
                    rep.err(rel, f"「{mname}」在页面上写成 {g}/{b}，"
                                 f"但数据层 got 留 null（无卷面依据）",
                            "改成定性描述（如「8 题对 7 题」）而不是 X/Y。")
                    hits += 1
                else:
                    rep.err(rel, f"「{mname}」在页面上写成 {g}/{b}，"
                                 f"与数据层 got={mgot} 不一致",
                            "同一份卷子的同一个模块在两页上不能有两个实得。"
                            "以数据层为准改页面，或先改数据层并写明依据。")
                    hits += 1
    if hits:
        rep.ok()
        return
    rep.ok()


def check_five_stage_paradigm(rep, rawmap):
    """错题精讲页必须满足「五段范式」，且 HTML 与 JSON 双侧一致（第十三次核验新增）。

    2026-10-04 第十三次核验抓到的缺陷，是前十二轮闸门**全都只审 exams 侧**的必然结果：

      · `check_page_matches_data()` 从 `data/exams/*.json` 取事实，
        **只校验试卷拆解页与整卷分析页**；
      · `data/wrong/*.json` 的 `steps` / `variants` / `thinkQuestions` /
        断点标记**只被检查了「非空」**（第四轮那道「done 但字段为空」），
        **内容对不对完全没有闸门**；
      · 而错题精讲页恰恰是**唯一**承载教学判断的产物——它带 `page` 字段却
        **从来没有被任何页面级闸门读过**。

    回退法自证（这才是发现它的原因）：删掉 q5 全部断点步骤 + 把「问答对调」
    变式换成普通变式，校验器报 **0 错误**；同样地只改 HTML 侧标题，也报 0 错误。
    ⇒ 数据层与页面层**双侧失守**，且两个方向都抓不到。

    判定口径全部按 MEMORY「五段范式」的硬性标准，且都选**可自动判定**的信号：
      ① 变式固定配比 = 简单变式(只改一个数值) + 同类型(换考点同手法)
         + **问答对调(已知与所求互换)**。第三条 MEMORY 写明「不可省」，
         因为「很多孩子是记住套路不是真懂，只有对调能测出来」——
         少一题，整段变式的存在意义就塌了。
      ② 想五题最后一题必须是「什么情况下这个方法会失效」，
         这是方法适用边界，也是防死记硬背的唯一一道题。
      ③ steps 至少一步带 `breakpoint`（断点是精讲页的核心教学结论，
         没有断点就退化成普通答案解析页）。
      ④ 每步三件套：动作/依据/检验，缺一即「有跳跃」。
      ⑤ HTML 侧标题与 JSON 侧 variants 逐条对应，且 HTML 不得残留
         未被 JSON 承认的变式标题（反之亦然）。
    """
    # 五段范式的类型标记：简写形式（页面标题里用「 · 」分隔）也要认
    KIND_PAT = re.compile(r"(简单变式|只改一个数值|原题变式|同类型|换个考点|换考点|问答对调|对调)")

    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for it in d.get("items", []):
            iid = it.get("id", "?")
            where = f"{rel}#{iid}"

            # —— ① 变式固定配比 ——
            vs = it.get("variants") or []
            if vs:
                kinds = [KIND_PAT.search(str(v.get("type", ""))) for v in vs]
                found = [k.group(1) if k else "" for k in kinds]
                blob = " ".join(found)
                has_easy = bool(re.search(r"(简单变式|只改一个数值|原题变式)", blob))
                has_same = bool(re.search(r"(同类型|换个考点|换考点)", blob))
                has_swap = bool(re.search(r"(问答对调|对调)", blob))
                missing = [n for n, ok in (("简单变式", has_easy),
                                           ("同类型", has_same),
                                           ("问答对调", has_swap)) if not ok]
                if missing:
                    rep.err(where,
                            f"三题变式配比不完整，缺：{'、'.join(missing)}",
                            "MEMORY 五段范式把配比定为硬性标准："
                            "简单变式(只改一个数值) + 同类型(换考点同手法) + "
                            "**问答对调(已知与所求互换)**。问答对调**不可省**——"
                            "很多孩子是「记住套路」不是「真懂」，只有对调能测出来。")
                # 简单变式必须真的「只改一个数值」：类型里写「只改一个数值」
                # 却没有任何数字变化，是自我声明与内容打架
                for v in vs:
                    t = str(v.get("type", ""))
                    if re.search(r"(只改一个数值)", t) and not re.search(r"\d", str(v.get("change", ""))):
                        rep.err(where,
                                f"变式标称「{t}」，但 change 里没有任何数值变化",
                                "「简单变式」的定义就是只改一个数值。名下无实会把这一题"
                                "降级成换个问法的同类型题，白占一个练习位。")

            # —— ② 想五题最后一题必须是「何时失效」 ——
            tq = it.get("thinkQuestions") or []
            if tq:
                last = tq[-1]
                qtext = " ".join(str(last.get(k, "")) for k in
                                 ("q", "question", "text", "title"))
                if not re.search(r"(失效|不成立|用不了|不适用|边界)", qtext):
                    rep.err(where,
                            "想五题最后一题不是「什么情况下这个方法会失效」",
                            "MEMORY 明定最后一题必须是方法适用边界题。"
                            "缺了它，学生会把方法当万灵公式，遇到变形就崩。")

            # —— ③ 断点 ——
            steps = it.get("steps") or []
            if steps and not any(s.get("breakpoint") for s in steps if isinstance(s, dict)):
                rep.err(where,
                        f"{len(steps)} 个 steps 里没有一个带 breakpoint",
                        "★ 断点是精讲页的核心教学结论——告诉孩子「你卡在哪一步」。"
                        "没有断点的页面退化成了普通答案解析页，"
                        "家长按 MEMORY「先建信心再定位断点」的用法也用不了。")

            # —— ④ 每步三件套：动作/依据/检验 ——
            for idx, s in enumerate(steps, 1):
                if not isinstance(s, dict):
                    continue
                lack = [k for k in ("action", "basis", "check") if not s.get(k)]
                if lack:
                    rep.err(where,
                            f"第 {idx} 步缺 {'/'.join(lack)}",
                            "MEMORY 要求每步三件套（动作/依据/检验）——"
                            "「依据」是为了让他能自己推下一步，「检验」是为了当场自查。"
                            "缺任何一件都会退化成有跳跃的讲解。")

            # —— ⑤ HTML 侧一致性 ——
            page = it.get("page")
            if not page:
                rep.warn(where, "缺 page 字段",
                         "没有页面路径，JSON 与精讲页无法互相追溯，"
                         "下面的页面级检查也覆盖不到这条")
                continue
            ap_ = os.path.join(ROOT, page)
            if not os.path.exists(ap_):
                rep.err(where, f"page 指向的页面不存在：{page}", "链接断裂")
                continue
            try:
                src = open(ap_, encoding="utf-8").read()
            except Exception:
                continue
            # 页面上的变式标题（<h3>第 N 题（…）</h3>）必须与 JSON 的 variants 一一对应
            heads = re.findall(r"<h3[^>]*>\s*第\s*\d+\s*题（([^）]*)）", src)
            if heads and len(heads) != len(vs):
                rep.err(where,
                        f"页面变式标题 {len(heads)} 个，数据层 variants {len(vs)} 条",
                        "同一份精讲页的两侧必须一致。第九轮铁律五："
                        "同一事实散落 N 处，改一处就会漏 N−1 处。")
            for h, v in zip(heads, vs):
                # 归一到三类再比，避免「简单变式」与「只改一个数值」被判成打架
                norm = lambda s: ("简单" if re.search(r"(简单变式|只改一个数值|原题变式)", s)
                                  else "同类型" if re.search(r"(同类型|换个考点|换考点)", s)
                                  else "对调" if re.search(r"(问答对调|对调)", s) else "")
                a, b = norm(h), norm(str(v.get("type", "")))
                if not a:
                    # ⚠️ 这个分支是回退法逼出来的：首版写成
                    #    `if norm(a) != norm(b) and a and b`，
                    #    于���页面上出现**四类之外的类型名**（如把「问答对调」改成
                    #    「普通变式 · 随手一题」）时 a 为空 → 条件短路 → **静默放过**。
                    #    这与第九轮「上一条免责说明替下一条豁免」同型：
                    #    闸门对「认不出来的东西」必须是**报错**而不是放过。
                    rep.err(where,
                            f"页面变式标题「{h}」不属于固定配比的三类",
                            "三题变式的类型只能是：简单变式(只改一个数值) / "
                            "同类型(换考点同手法) / 问答对调(已知与所求互换)。"
                            "写不出属于哪一类，说明这一题没按配比设计——"
                            "尤其不能拿它顶替问答对调那一题。")
                elif a != b:
                    rep.err(where,
                            f"页面第「{h}」题与数据层「{v.get('type')}」类型不一致",
                            "页面与数据层说的不是同一件事。以数据层为准，"
                            "或两边一起改——不要只改一处。")
    rep.ok()


def main():
    ap = argparse.ArgumentParser(description="校验 data/ 数据是否违反项目三条硬纪律")
    ap.add_argument("--quiet", action="store_true", help="只输出问题")
    ap.add_argument("--warn", action="store_true", help="有问题也 exit 0（不阻断管道）")
    ap.add_argument("--no-sha", action="store_true", help="跳过 sha256 全量重算（快）")
    args = ap.parse_args()

    rep = Report(quiet=args.quiet)
    print("校验 data/ …")
    rawmap = check_raw(rep, deep=not args.no_sha)
    check_wrong(rep, rawmap)
    check_exams(rep, rawmap)
    check_tasks(rep, rawmap)
    check_resources(rep, rawmap)
    check_cause_enum(rep)
    check_embedded_snapshots(rep)
    check_no_fabricated_score(rep, rawmap)
    check_module_got_grounded(rep, rawmap)
    check_page_matches_data(rep, rawmap)
    check_five_stage_paradigm(rep, rawmap)
    check_papers_derived(rep)
    check_doc_numbers(rep)
    check_data_readme(rep)
    rep.print()
    sys.exit(1 if rep.errors and not args.warn else 0)


if __name__ == "__main__":
    main()
