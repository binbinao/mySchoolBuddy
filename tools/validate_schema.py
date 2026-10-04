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

            # 错因（枚举、推理链、套话、存疑矛盾）全部交给 check_cause_reasoning()，
            # 那里是 err 级并覆盖 wrong 与 exams 两侧。第十七次核验把这里的
            # 两条 warn 降级删除：warn 不阻断管道，枚举外的值能一路提交进 Git。

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
                    # 枚举检查已由 check_cause_reasoning() 以 err 级统一处理，
                    # 此处不重复报告（否则同一问题会被计两条，闸门自身出假阳性）。
                    pass

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
        m = re.search(r'"cause":\s*"([^"\n]*)"', r)
        if not m:
            # 第十八轮修。原先写 `if m:`，正则失配时**静默放过**——
            # 把 cause 与 causeSecondary 合并成一行就会让整行不匹配，
            # 于是「0 错误」而 README 里的枚举清单已经不存在了。
            # 判据的前提不能是「违规内容本不具备的性质」：认不出来的东西要**报错**，
            # 不是放过（MEMORY 闸门铁律 6）。
            rep.warn("README.md",
                     "读不出 schema 示例里的 cause 枚举清单",
                     "枚举比对点对本文件失效。可能是 JSON 示例被重排、"
                     '"cause" 后面不再紧跟值，或示例被删——确认后改回单行独立写法')
        else:
            listed = {x.strip() for x in m.group(1).split("|")}
            listed = {x for x in listed if x and not x.startswith("…")}
            if listed != CAUSES:
                missing = [c for c in sorted(CAUSES) if c not in listed]
                extra = [c for c in sorted(listed) if c not in CAUSES]
                parts = []
                if missing:
                    parts.append("缺 " + "、".join(missing) + "，按纪律补上")
                if extra:
                    parts.append("枚举外有 " + "、".join(extra) + "，删掉")
                rep.err("README.md",
                        f"schema 示例的 cause 写了 {'/'.join(sorted(listed))}，"
                        f"与五类枚举不一致",
                        "README 是新人第一份参照，照着抄就会录错。" + "；".join(parts))
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


def check_answer_fields(rep, rawmap):
    """答案字段（事实源头）必须齐全、拼写唯一、不得凭空多出同义键（第 15 道闸门）。

    2026-10-04 第十四次核验抓到的缺陷，性质比前十三类都靠前：

      前十三轮十几道闸门查过——分值纪律、错因枚举、溯源、模块合计、
      跨页一致、五段范式、内嵌副本、派生量……**唯独没查过答案本身**。
      而答案恰恰是整个数据层的**事实源头**：
        · `childAnswer` 决定「已做对的部分」能写什么、
          「断点到底在哪」那个两栏对照框才有内容；
        · `answerKey` 决定断点判定成立与否。

      **回退法自证（这就是发现它的手段）**：
        把 q19 的 `correctAnswer` 改成 `y=-(x-9)²-99`（与官方答案完全不同），
        再把 `answerKey` 整个字段删掉 —— 校验器报 **98 项通过 · 0 错误**。
      删掉正确答案这件事，比分值估错严重得多：分值估错只是统计偏差，
      **答案错了整套教学判断全部反着走**，而孩子会照着错答案继续练。

    同时清掉了两处存量拼写分裂（第十轮铁律五：同一概念不能有两种拼写）：
      · `correctAnswer` —— 与 `answerKey` 同义，**全站零读取方**，
        写了没人看、改了没人发现，是拼写分裂的温床；
      · `myAnswer` —— 与 `childAnswer` 同义，生成器曾用
        `childAnswer or myAnswer` 兜底，两值可以长期并存且都不报错；
        改了带 h 的那个，`or` 右边会**静默生效**，孩子看到的「你写的」
        可能变成正确答案，断点框整个失去意义。

    判定口径（全部按「能自动判定」选，不做全文扫描）：
      ① 错因**已定论**（有 `cause` 且 `causePending` 不为 true）的题必须有
         `answerKey` 与 `childAnswer`；
      ② 出现 `correctAnswer` / `myAnswer` / `rightAnswer` 等同义别名即报错；
      ③ `answerKey` 与 `childAnswer` 内容完全相同 = 疑似把孩子的错答抄成了正确答案
         （判错因的前提就不成立），且此时不该有失分记录；
      ④ 有 `answerKey` 必须写 `answerKeySource` —— 本项目英语卷**没有官方答案卷**。

    ⚠️ **本闸门管不了什么（必须写明，否则会误以为已覆盖）**：
    「答案内容本身对不对」**无法自动判定**。回退法实测：把第 11 题的
    `answerKey` 从 `stay balanced` 改成 `give up smoking`，本闸门**不报错**——
    这是能力边界不是漏洞：英语/语文答案是自由文本，机器无从判断语义对错。
    本闸门只保证「答案**存在**、拼写唯一、有依据、与孩子作答**不是同一个**」。
    「答案**正确**」仍只能靠核卷（MEMORY 核卷纪律：判分前必须放大原卷）
    与第十四次人工复算——**机器能守住结构，守不住语义**。
    """
    # 同义别名黑名单：这些键一旦出现，就是「同一概念第二种拼写」
    ALIASES = ("correctAnswer", "myAnswer", "rightAnswer",
               "childanswer", "stdAnswer", "answer")

    def _scan(items, rel, tag):
        for it in items:
            if not isinstance(it, dict):
                continue
            where = f"{rel}#{it.get('id', it.get('qno', '?'))}"

            # ② 同义别名一律报错（认不出来的东西必须报错，不能放过）
            for bad in ALIASES:
                if bad in it:
                    rep.err(where,
                            f"出现同义别名键 `{bad}`",
                            "答案字段的规范拼写只有 `answerKey`（正确答案）"
                            "与 `childAnswer`（孩子原答案）两个，"
                            "不接受第二个拼写——第十轮铁律五：同一概念两种拼写时，"
                            "改了带 h 的那个会让读方**静默失效**，"
                            "页面不报错、卡片照常渲染，肉眼看不出已坏。"
                            f"`{bad}` 已被 `answerKey`/`childAnswer` 取代。")

            key = it.get("answerKey")
            child = it.get("childAnswer")
            # ① 必填：**已定论**的错因必须有 answerKey 与 childAnswer。
            #
            # ⚠️ 这里踩过一次闸门自身的坑（第九轮同型：误报 = 把真错误淹掉）：
            # 首版把 `causePending: true` 也算「已判错因」，于是 exams 侧
            # 36/58/61 三题全部误报。翻数据才看清——这三题恰恰是**错因尚未定论**的存疑题
            # （`uncertain: true`，红笔未明示批改，且**全卷没有官方答案卷**），
            # 正确答案客观上不可知。**要求它们填 answerKey 等于逼人编造官方答案**，
            # 直接违反「不编造官方答案」这条纪律。
            # ⇒ 判据是「错因是否已定论」：`causePending: true` 表示还没判，
            #   此时缺 answerKey 是**合法状态**，不该报错（但仍会在 ④ 里提醒写依据）。
            cause = it.get("cause")
            pending = it.get("causePending") is True
            settled = bool(cause) and not pending
            if settled and not (key and child):
                miss = [n for n, v in (("answerKey", key),
                                       ("childAnswer", child)) if not v]
                rep.err(where,
                        f"错因已定论为「{cause}」但缺答案字段：{'、'.join(miss)}",
                        "答案字段是事实源头：`childAnswer` 决定「已做对的部分」"
                        "能写什么，`answerKey` 决定「断点到底在哪」才成立。"
                        "MEMORY 错因纪律要求**回看孩子的中间步骤**才能判因——"
                        "没有孩子原答案，判读根本无从谈起。\n"
                        "若此题确实判不出来，用 `causePending: true` 标明存疑"
                        "（那样缺 answerKey 是合法的），不要硬凑一个答案。")

            # ③ 正确答案与孩子原答案完全相同 = 抄错
            if key and child and str(key).strip() == str(child).strip():
                rep.err(where,
                        "answerKey 与 childAnswer 内容完全相同",
                        "正确答案和孩子原答案一模一样，说明孩子的错答被抄成了正确答案。"
                        "判错因的前提（他确实错了）就不成立——"
                        "回原卷核一遍：是记错了孩子的作答，还是这题其实没失分。")

            # ④ 反推来的答案必须写明依据
            src = it.get("answerKeySource")
            if key and not src:
                rep.err(where, "有 answerKey 但没写 answerKeySource",
                        "答案是卷面印的，还是据红笔反推的？"
                        "本项目英语单元测**没有官方答案卷**，"
                        "反推的答案不写依据，将来没人敢信它——"
                        "「答案错了整套教学判断全部反着走」，这是本项目最贵的一类错。")

    wd = os.path.join(ROOT, "data", "wrong")
    if os.path.isdir(wd):
        for fn in sorted(os.listdir(wd)):
            if fn.endswith(".json"):
                rel = f"data/wrong/{fn}"
                d = load(os.path.join(wd, fn), rep, rel)
                if d:
                    _scan(d.get("items", []), rel, "wrong")

    ed = os.path.join(ROOT, "data", "exams")
    if os.path.isdir(ed):
        for fn in sorted(os.listdir(ed)):
            if not fn.endswith(".json"):
                continue
            rel = f"data/exams/{fn}"
            d = load(os.path.join(ed, fn), rep, rel)
            if not d:
                continue
            exams = d.get("exams") or d.get("items") or [d]
            if isinstance(exams, dict):
                exams = [exams]
            for ex in exams:
                if isinstance(ex, dict):
                    _scan(ex.get("wrongs") or [], rel, "exam")
    rep.ok()


def _md_section(txt, keyword):
    """从 markdown 里取出标题含 keyword 的**那一节**（到下一个同级或更高级标题为止）。

    ⚠️ 2026-10-04 第十八次核验踩坑：首版写 `txt[m.start():]`，
    于是「答案字段纪律」这一节的切片一路取到**文末**（9602/13559 字符），
    后面 4 个章节里的「能力边界」字样替它豁免了边界检查
    —— 与第九轮「上一条免责说明替下一条豁免」**完全同型**，
    回退法注入后删光本章边界词仍报 0 错误。
    ⇒ 章节切片**必须按标题层级截断**，不能取到文末。
    """
    m = re.search(r"^#{2,6}\s*(.+?)\s*$", txt, flags=re.M)
    if not m:
        return ""
    for mm in re.finditer(r"^(#{2,6})\s*(.+?)\s*$", txt, flags=re.M):
        if keyword in mm.group(2):
            level = len(mm.group(1))
            nxt = len(txt)
            for m3 in re.finditer(r"^(#{1,6})\s*", txt[mm.end():], flags=re.M):
                if len(m3.group(1)) <= level:
                    nxt = mm.end() + m3.start()
                    break
            return txt[mm.start():nxt]
    return ""


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

    # 2026-10-04 第十四次核验：答案字段的语义边界必须写在规范里。
    # 只写「有闸门」而不写「闸门管不到答案对错」，下一个人会误以为
    # 答案正确性已被机器守住 —— 而实测第 11 题改成完全不同的答案也不报错。
    #
    # ⚠️⚠️ 第十九次核验：**本段判据是恒真豁免，等于没有闸门**（第十八类形态复发）。
    #   首版（第十八轮已改过一次）：`if "check_answer_fields" in txt:` —— 条件恒假（死代码）。
    #   第十八轮把它改成「按章节标题定位」，但**留下了 `if True:` 外壳**，
    #   里面又写 `has_limit = (not _asec) or (...)`：
    #     · `if True:` = 把「无条件检查」的意图写成了字面量，掩盖了真实结构；
    #     · `(not _asec)` = **章节不存在时反而豁免**。
    #   ⇒ 两者相乘的效果是：**整节删掉「答案字段纪律」，校验器报 0 错误。**
    #   实测（回退法）：README 13559→12240 字符，126 项通过 · 1 警告 · **0 错误**。
    #
    #   **判据的前提不能是「正确内容本不具备的性质」**（铁律六第 4 条形态），
    #   也不能是「章节在就查、不在就算了」——**章节不在才是必须报错的形态**。
    _asec = _md_section(txt, "答案字段纪律")
    if not _asec:
        rep.err(rel, "README 缺少「答案字段纪律」章节",
                "第十四次核验发现：README 完全没有答案字段定义，"
                "而答案是数据层的事实源头（`childAnswer` 决定「已做对」写什么、"
                "`answerKey` 决定断点判定成立与否）。教人写数据的文档缺字段定义"
                "＝持续产出没人认的键（第十一类：规范教人把纪律写反）。")
    elif not (("管不住" in _asec) or ("判不了" in _asec) or ("能力边界" in _asec)):
        rep.err(rel, "写了答案字段闸门，却没写明它管不住「答案对错」",
                "自由文本答案（英语/语文）机器判不了语义。"
                "实测把第 11 题 answerKey 从 `stay balanced` 改成 "
                "`give up smoking`，校验器不报错——这是能力边界。"
                "规范里必须写明：**机器守结构，语义靠核卷**，"
                "否则下一个人会误以为答案正确性已被守住。")

    # 答案字段的规范名必须写进文档，且必须列出禁止的同义别名
    for need in ("answerKey", "childAnswer"):
        if need not in txt:
            rep.err(rel, f"答案字段规范未定义 `{need}`",
                    "第十四次核验发现：README 完全没有答案字段定义，"
                    "而答案是数据层的事实源头。教人写数据的文档缺字段定义"
                    "＝持续产出没人认的键（第十一类：规范教人把纪律写反）。")

    # 2026-10-04 第十八次核验：变式与想五题的规范必须写进文档，且必须写明能力边界。
    # 为什么这道闸门是必需的（第十一类同型）：`variants` / `thinkQuestions`
    # 在此之前**只被检查过「非空」**，README 里也**完全没有它们的字段定义**——
    # 于是没人知道 `answer` 是「判题关键词集合」还是「标准答案」，
    # 也不知道它能不能写「略。」。规范缺定义 = 纪律无从遵守。
    #
    # ⚠️ 判据踩坑（回退法逼出）：首版写成
    #   `if "check_variants_faithful" in txt:` 才检查，
    #   而 README 里**从来没有出现过这个函数名** ⇒ 条件恒假
    #   ⇒ 删掉整节也不报错（**给自己发免死金牌**，铁律六第 4 条形态）。
    # 改成按**章节标题**判定，标题才是规范里真实存在的锚点。
    # ⇒ 第十九次核验进一步把它并入下面的**规范章节表**，不再单独写一段。

    # ══════════════════════════════════════════════════════════════════
    # 第十九次核验新增：**规范章节表**——把「每节必须存在 + 必须写明能力边界」
    # 收敛成一张表逐条机器判定。
    #
    # 为什么必须收敛成表（第十八类形态·第十九次执行）：
    #   修之前这段代码里有**两段同型逻辑**——
    #     · 变式章节：`if not _vsec: rep.err(...)`  ← 有守卫
    #     · 答案章节：`has_limit = (not _asec) or (...)` ← **恒真豁免，删掉整节不报错**
    #   同一函数、相邻几行、同一件事，一种写法有效另一种无效。
    #   靠「记得给每一节都写守卫」必然漏——**回退法实测 7 节里 5 节无守卫**：
    #     答案字段纪律 / 题干拆解字段纪律 / 已做对的部分字段纪律 /
    #     判错因的推理链纪律 / 拆解工作流   → 整节删除后 126 项通过 · 0 错误。
    #
    #   **修掉一个实例不等于修掉这一类**（第十二轮铁律）：
    #   把「哪些节、每节要什么」写成表，表里没有的节**默认不检查也不拦**，
    #   新增节必须往表里加一行——漏加会在下次核验被同一张表发现。
    #
    # 表的每一行 = (章节标题锚点, 必写的字段/口径关键词, 是否要求写明能力边界)
    # ⚠️ 判据只用**能自动判定**的事实：章节标题（真实锚点）、反引号字段名（规范里就有）。
    #   不做散文语义扫描——第九轮已证明那样假阳性 9/11（铁律三、铁律六）。
    _README_SECTIONS = [
        # (标题锚点,        必写字段,                              要求能力边界)
        ("分值纪律", ["full", "lost", "scorePending", "scoreNote"], True),
        ("答案字段纪律", ["answerKey", "childAnswer", "answerKeySource"], True),
        ("题干拆解字段纪律", ["stem", "breakdown", "breakdownKey", "page"], True),
        ("已做对的部分字段纪律",
         ["doneRight", "doneRightNote", "doneWrong", "doneWrongNote"], True),
        ("判错因的推理链纪律",
         ["cause", "causeSecondary", "causeNote", "myThought", "causePending"], True),
        ("变式与想五题字段纪律",
         ["variants", "thinkQuestions", "type", "change", "hint", "answer"], True),
        ("拆解工作流", ["sourceRaw", "scorePending"], False),
    ]
    # 「能力边界」的等价写法。**收窄到宁可漏不可淹**：
    # 只认这几个真实出现过的说法，不做同义扩展（扩展＝假阳性来源）。
    _LIMIT_WORDS = ("能力边界", "管不住", "管不到", "机器管不到",
                    "判不了", "判不到", "机器守结构")

    for _anchor, _need_fields, _need_limit in _README_SECTIONS:
        _sec = _md_section(txt, _anchor)
        if not _sec:
            rep.err(rel, f"README 缺少「{_anchor}」章节",
                    "章节是给读者找纪律的**锚点**，不是装饰。"
                    "第十一次核验：前若干轮的闸门全在查「已经写进 JSON 的数据对不对」，"
                    "从没查过「指导人怎么写数据的文档对不对」——"
                    "教人把纪律写反的文档，比一条写错的数据更持久。"
                    "（本节校验由第十九次核验的规范章节表统一承担，"
                    "新增纪律章节请同时往该表里加一行）")
            continue
        missing = [f for f in _need_fields if f"`{f}`" not in _sec]
        if missing:
            rep.err(rel, f"「{_anchor}」章节未定义字段：{'、'.join(missing)}",
                    "章节在，但字段本身没写——**等于没写**。"
                    "缺失的字段会以各种别名/自由文本的形式被写进数据，"
                    "而没有任何闸门认得它（铁律五：同一概念两种拼写 ⇒ 静默失效）。")
        if _need_limit and not any(w in _sec for w in _LIMIT_WORDS):
            rep.err(rel, f"「{_anchor}」章节没写明机器闸门的能力边界",
                    "只写「有闸门」而不写「闸门管不到什么」，"
                    "下一个人会误以为这一类内容已被机器守住。"
                    "本项目已实测多次：把自由文本的答案改成完全不同的内容，"
                    "校验器不报错——**机器守结构，语义靠核卷**。"
                    "这条边界必须写在**它自己那一节**里，"
                    "写别处会被后面章节的同款字样替它豁免（第九轮同型）。")

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
                kinds = [KIND_PAT.search(str(v.get("type", "")) if isinstance(v, dict) else "")
                         for v in vs]
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
                    t = str(v.get("type", "")) if isinstance(v, dict) else ""
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
                a, b = norm(h), norm(str(v.get("type", "")) if isinstance(v, dict) else "")
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
                            f"页面第「{h}」题与数据层「{b}」类型不一致",
                            "页面与数据层说的不是同一件事。以数据层为准，"
                            "或两边一起改——不要只改一处。")
    rep.ok()


# ── 16. 题干拆解表：页数声明 + 逐条对应 + breakdownKey 引用完整性 ──
def _strip_tags(s):
    """去掉 HTML 标签与实体，只留可读文本（用于跨格式比对）。"""
    import html as _html
    s = re.sub(r"<[^>]+>", "", str(s))
    return _html.unescape(s)


def _norm_text(s):
    """归一化题干原句：中文标点/空白/引号差异不算内容差异。

    只归一化**排版噪声**，不做模糊匹配——一旦开了模糊，
    「两条其实不一样」就会被当成「差不多」，闸门自动失效（铁律六）。
    """
    s = _strip_tags(s)
    s = s.replace("　", "")
    s = re.sub(r"\s+", "", s)
    for a, b in (("，", ","), ("。", "."), ("：", ":"), ("；", ";"),
                 ("（", "("), ("）", ")"), ("“", '"'), ("”", '"'),
                 ("‘", "'"), ("’", "'"), ("＝", "="), ("－", "-"),
                 ("−", "-"), ("√", ""), ("△", "")):
        s = s.replace(a, b)
    return s


def _token_set(s):
    """把一句话切成可比较的语义片段（按标点与连接词切）。"""
    t = _norm_text(s)
    parts = re.split(r"[,.!:;()\"']|并且|同时|而且", t)
    return [p for p in parts if len(p) >= 2]


# 事实标记：这一行里**机器判得了**的硬事实（数学式、点坐标、选项字母…）。
# 「已做对」表与 doneRight 常有近义改写（「展开成」vs「展开为」），
# 整句字面比对必然假阳性（铁律三）；但**这些标记必须一致**——
# 「y₁ 展开成 x²−4x+7」与「y₁ 展开成 x²−5x+7」是同义句却不同事实。
_CJK_PUNCT = re.compile(r"[、。：；，？！]")

_FACT_MARKS = re.compile(
    r"[A-Za-z][A-Za-z0-9]*\s*=\s*[^,，;；]+"                    # y=...  a₂=...
    r"|[A-Za-z][0-9]?\s*[（(]\s*[^)）]*\s*[)）]"                # M(2, 3) / (6, −3)
    r"|[−\-+]?\d+(?:\.\d+)?\s*/\s*\d+"                          # 3/4
    r"|\d+\s*:\s*\d+"                                            # AE:EB
    r"|[A-E]\s*[.、）)]"                                          # 选项 A.
    # 裸多项式/代数式（无等号）：x²−4x+7、4x²−13x+10。
    # 必须含**加减号**才认——否则「D、E、F」「M、N」这类纯罗列也会被当标记，
    # 抽出噪声后子集判据会失灵（闸门自身假阳性/漏网）。
    r"|(?<![A-Za-z0-9])[0-9a-zA-Z²³²]*(?:\s*[−+]\s*[0-9a-zA-Z²³²]+)+"
    # 裸坐标对 (6, −3)：无字母前缀时前面常是中文「顶点写成 」，故不要求首字符为数字
    r"|\([−\-+]?\d+(?:\.\d+)?\s*,\s*[−\-+]?\d+(?:\.\d+)?\)"
)


def _fact_marks(s):
    """抽出事实标记集合（归一化后）。空集 = 该行不含可判定的硬事实。"""
    t = _norm_text(s)
    out = set()
    for m in _FACT_MARKS.finditer(t):
        g = m.group(0).strip()
        if not g or _CJK_PUNCT.search(g):
            continue  # 「D、E、F」这类纯罗列不是硬事实，别当标记
        if len(_norm_text(g)) >= 2:
            out.add(g)
    return out


def _rows_correspond(page_cell, data_cell):
    """页面的「他写了什么」与数据层对应条目是否指同一件事。

    **为什么不用整句字面包含**：实测页面写「主动重画 △ABC，把 D、E、F 标在对应边上」、
    数据层写「主动重画△ABC，把D、E、F标在对应边」——近义改写让字面包含必然失败，
    首版闸门 5 条里 4 条是假阳性。**假阳性会把真错误淹掉**（铁律三）。

    **为什么不能开模糊匹配**：一旦开了模糊，「两条其实不一样」会被当成「差不多」，
    闸门自动失效（铁律六）。所以只比**事实标记**——数学式、点坐标、选项字母，
    这些机器判得了、且一旦不同就是教学事故。

    三级判据，**前两级一旦判否就到此为止**（不做兜底）：
      ① 归一化后字面包含                       —— 最强，直接判真
      ② 事实标记：页面的每个标记都在数据层出现   —— 数学事故防线（x²−4x+7 ≠ x²−5x+7）
      ③ 实词覆盖：**仅当两侧都抽不出任何标记**时启用 —— 纯文字行的兜底

    为什么 ② 不许兜底：首版在 ② 判否后接了 ③，于是把数据层的
    「顶点写成 (6, 3)」也算成页面的「顶点写成 (6, −3)」——
    两字窗里 (6 这个片段共有，坐标符号一错反而蒙混过关（回退法自证抓出）。
    **放宽必须严格限定在「无标记可判」的场景，否则防线自己就漏了。**
    """
    p, dd = _norm_text(page_cell), _norm_text(data_cell)
    if p and p in dd:
        return True
    pm, dm = _fact_marks(page_cell), _fact_marks(data_cell)
    if pm and dm:
        return pm <= dm          # 判否即否：坐标/系数不符就是不符
    if pm or dm:
        # 只有一侧有标记 ⇒ 页面把硬事实写漏了，或数据层漏记，同样判否
        return False
    return _word_cover(page_cell, data_cell)   # 两侧皆无标记，才走词元兜底


def _word_cover(page_cell, data_cell):
    """两侧都无硬事实时的兜底：实词**双向**覆盖，取高者。

    为什么双向：数据层允许比页面简略——页面写「标出 DE∥AC、DF∥AB 的平行关系」、
    数据层写「标出两条平行线关系」，只按页面方向算覆盖率必然偏低而误报。

    为什么只看长度 ≥2 的片段：单字（的/了/把）会出现在任何句子里，
    共有的「标、出、平、行」就能把两条不相干的句子判成对应。
    """
    p, d = _norm_text(page_cell), _norm_text(data_cell)

    def words(s):
        w = set(x for x in re.findall(r"[A-Za-z]+\d*|\d+(?:\.\d+)?", s) if len(x) >= 2)
        for i in range(len(s) - 1):
            seg = s[i:i + 2]
            if re.fullmatch(r"[一-鿿]{2}", seg):
                w.add(seg)
        return w

    pw, dw = words(p), words(d)
    if not pw or not dw:
        return False
    fwd = sum(1 for w in pw if w in d) / len(pw)      # 页面实词被数据层覆盖
    bwd = sum(1 for w in dw if w in p) / len(dw)      # 数据层实词被页面覆盖
    return max(fwd, bwd) >= 0.6


def check_breakdown_faithful(rep, rawmap):
    """题干拆解表（② 段）：页数声明、逐条对应、breakdownKey 引用完整性。

    2026-10-04 第十五次核验抓到的缺陷，是**第十四类（源头无闸门）家族的第二个成员**：

    第十四轮补上了 `answerKey`/`childAnswer` 的结构闸门，但**题干拆解表**
    （`breakdown` + `breakdownKey`）同样是事实源头级的教学事实——

      · `breakdown` 的每一行左边必须是**题干里的原句**。左边一旦被改写或替换，
        整张表就不再是「把这道题翻译成条件」，变成自说自话，
        而孩子正是照着这张表建立题感。
      · `breakdownKey`（收尾点明「最容易漏的是第 N 条」）是 MEMORY 五段范式
        对②段的**硬性标准**。指向不存在的条号 ⇒ 收尾结论失效。
      · 页面标题「题干拆成 N 个已知」的 N 与数据层 `len(breakdown)` 必须一致。
        两边各自自洽时单看任一处都「像对的」——第九轮铁律五同型。

    回退法自证（发现手段）：把 q19 的 breakdown 第 1 条原句替换成与题目无关的内容、
    删掉一条（6→5）、把 breakdownKey 改成「第 9 条」，
    校验器报 **99 项通过 · 0 错误**。三处全是教学事故级：
    拆解表内容失控 + 页面与数据层条数打架 + 收尾结论指向虚空。

    为什么不能只查「非空」：`breakdown` 有一条和六条同样「非空」，
    页面和数据层也能各自自洽——**非空检查与条数一致性检查通过，
    恰恰是这类缺陷最容易存活的环境**。

    覆盖范围：**仅 `data/wrong/*.json` 的 items（错题精讲页）**。
    试卷侧 `verdictHtml` 是自由散文，不适用逐条比对，按设计不管。
    """
    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for it in d.get("items", []):
            iid = it.get("id", "?")
            where = f"{rel}#{iid}"
            bd = it.get("breakdown") or []
            if not bd:
                rep.warn(where, "缺 breakdown（题干拆解表）",
                         "五段范式 ② 段要求把题干逐句翻译成数学条件。"
                         "缺了它，孩子只能自己读题干，而读不懂正是这类题的根因")
                continue

            # —— ① 结构：每条必须是 [题干原句, 翻译] 二元组 ——
            for idx, row in enumerate(bd, 1):
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    rep.err(where,
                            f"breakdown 第 {idx} 条不是 [题干原句, 翻译] 二元组",
                            "拆解表的形状必须固定，否则表格渲染与自动比对都会失准。"
                            f"实际类型：{type(row).__name__}")
                    continue
                if not str(row[0]).strip() or not str(row[1]).strip():
                    rep.err(where, f"breakdown 第 {idx} 条有空的单元格",
                            "左边是题干原句，右边是翻译成数学条件的写法，"
                            "两者都必须有内容")

            # —— ② breakdownKey 必须指向真实存在的条号 ——
            key = it.get("breakdownKey")
            if key:
                # 只在**「第 N 条」这个语法位置**上取条号，
                # 否则说明文里出现的其它数字（d=6、b₂=5/2…）会被误当条号。
                refs = re.findall(r"第\s*(\d+)\s*条", str(key))
                if not refs:
                    rep.err(where,
                            "breakdownKey 没有点明「最容易漏的是第几条」",
                            "MEMORY 五段范式要求 ② 段收尾必须点明"
                            "「最容易漏的是第几条」——这是整张表唯一被强调的那一条，"
                            "写不出条号等于没收尾")
                else:
                    for r in refs:
                        n = int(r)
                        if not (1 <= n <= len(bd)):
                            rep.err(where,
                                    f"breakdownKey 指向「第 {n} 条」，但拆解表只有 {len(bd)} 条",
                                    "收尾结论指向了一条不存在的条目，等于整段失效。"
                                    f"改回 1–{len(bd)} 之间的实际条号，或先补齐拆解表")

            # —— ③ 页数声明与逐条对应（HTML 侧）——
            page = it.get("page")
            if not page:
                continue
            ap_ = os.path.join(ROOT, page)
            if not os.path.exists(ap_):
                continue  # 文件缺失已由 check_five_stage_paradigm 报错
            try:
                src = open(ap_, encoding="utf-8").read()
            except Exception:
                continue

            heads = re.findall(r"题干拆(?:成|解成)\s*(\d+)\s*个", src)
            if heads and int(heads[0]) != len(bd):
                rep.err(where,
                        f"页面写「题干拆成 {heads[0]} 个已知」，数据层 breakdown 有 {len(bd)} 条",
                        "同一张拆解表的事实散落在两处，改一处就会漏另一处。"
                        "要改就两边一起改；若页面把两条并成一行展示，"
                        "请把数据层拆细到与页面一致，别让页数声明说谎")

            # 逐条：页面表格第一列的每一行都必须能在数据层找到对应原句。
            # 用**分片包含**而非全等——页面可能把 HTML 实体与子标签折行，
            # 但每一行的可读文本必须是某条题干原句的连续片段（不得改词序/增词）。
            tbl = re.search(r"<h3[^>]*>[^<]*题干拆(?:成|解成)[^<]*</h3>\s*<table.*?</table>",
                            src, re.S)
            if not tbl:
                continue
            col1 = []
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(0), re.S):
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                if tds:
                    col1.append(tds[0])
            if not col1:
                continue
            have = [_norm_text(r[0]) for r in bd if isinstance(r, (list, tuple)) and r]
            for ci, cell in enumerate(col1, 1):
                # 页面把两条并进同一格展示时用 <br> 分行。必须**先拆再比**——
                # 首版只按 "\n" 拆，而 <br> 不是换行符，两行被粘成
                # 「直线 MN 与 x 轴正半轴交于 Dtan∠MDO」这种现实中不存在的串，
                # 于是每一条都匹配不上 → 闸门自身失效且报的是假阳性。
                for piece in re.split(r"<br\s*/?>", cell, flags=re.I):
                    piece = _strip_tags(piece).strip()
                    if len(_norm_text(piece)) < 4:
                        continue
                    if not any(_norm_text(piece) in h for h in have):
                        rep.err(where,
                                f"页面拆解表第 {ci} 行的「{piece[:24]}」在数据层 breakdown 里找不到",
                                "拆解表左边必须是**题干原句**。这一行被改写过或替换了，"
                                "整张表就不再是「把题干翻译成条件」，而是自说自话——"
                                "孩子正是照着这张表建立题感的")
    rep.ok()


def check_done_right_faithful(rep, rawmap):
    """已做对的部分（① 段）：doneRight / doneWrong 结构 + 精讲页逐行对应。

    2026-10-04 第十六次核验抓到的缺陷，是**第十四类（源头无闸门）家族的第三个成员**：

    第十四轮补了 `answerKey`/`childAnswer`，第十五轮补了 `stem`/`breakdown`，
    但**「已做对的部分」这一族**（`doneRight`/`doneWrong`）同样是事实源头——
    MEMORY 五段范式的 ① 段全靠它，而「断点到底在哪」全靠 `doneWrong`。

    回退法自证（发现手段）：把 q19 的 doneRight 第 1 条引述改写成与题目无关的内容、
    把第 2 条退化成字符串、把第 3 条写成三元组、整段清空 q5 的 doneRight——

      · 整段清空 → **被抓出**（第四轮那道「done 但字段为空」的旧闸门）
      · 另外三种内容级违规 → 校验器报 **100 项通过 · 0 错误**

    只查「非空」是这个家族最容易存活的环境：一行和五行同样「非空」，
    引述被改写、退化成字符串、写成三元组都不会触发任何现有检查。

    **本轮更严重的一层：数据层根本没有承载 ✗ 行的字段。**
    两页精讲页的「已做对」表里都有一行 ✗（q19「顶点写成 (6,−3) 拼凑」、
    q5「括号里填 A 即 3/2」）——那是**断点的直接证据**，
    而 doneRight 只记 ✓ 行 ⇒ 证据只活在 HTML 里，
    生成器（build_skeletons.py 只渲染 doneRight）重新生成时会**静默丢掉它**。
    故本闸门按「事实」而非按「文件」建：**页面表格的每一行（✓ 与 ✗ 都算）
    都必须在数据层有对应条目。**

    覆盖范围：**仅 `data/wrong/*.json` 的 items（错题精讲页）**。

    回退法自证 12 例（正向 6 全部抓出 + 反向 6 全部判定正确）：
      正向：展开式改错（x²−4x+7→x²−5x+7，定位到具体行）· doneWrong 清空 ·
            退化成字符串 · 三元组 · 整个字段缺失 · 坐标改错（(6,−3)→(6,3)）
      反向：干净基准 0 误报 · 数据层引述更详细 · 页面用近义改写 ·
            数据层多出一条 · 页面文字比数据层更长 · **数据层丢了具体内容
            （把「DE∥AC、DF∥AB」压缩成「两条平行线」）——这一条本轮
            确实报错，且**应当报错**：页面有的事实数据层没有，正是本闸门
            要抓的形态（重新生成会静默丢失）**。

    本轮自身踩了三次坑，全部靠回退法暴露：
      ① 首版按整句字面包含比对 ⇒ 5 条里 4 条假阳性（页面「展开成」vs
         数据层「展开为」）。**假阳性会把真错误淹掉**（铁律三）。
         改法：比**事实标记**（数学式/坐标/选项字母），不做模糊匹配——
         一开模糊，「两条其实不一样」会被当成「差不多」，闸门自动失效（铁律六）。
      ② 事实标记的正则首版漏了**裸多项式**（无等号的 x²−4x+7），
         抽不出任何标记就退化成字符集重合，「把展开式改错」蒙混过关。
      ③ 标记判否后又接了词元兜底 ⇒ 把「顶点写成 (6,3)」也算成页面的
         「顶点写成 (6,−3)」（两字窗里 (6 共有）。**放宽必须严格限定在
         「两侧都抽不出标记」的场景**，否则防线自己就漏了。

    **能力边界（勿误以为已被守住）**：页面与数据层**同时**改错同一处事实时，
    两侧一致 ⇒ 本闸门通过。这与第 15 轮 `check_breakdown_faithful` 同源：
    **两侧一致 ≠ 两侧都对**。要抓这类必须核卷（`sips` 裁切放大逐字读）。
    """
    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for it in d.get("items", []):
            iid = it.get("id", "?")
            where = f"{rel}#{iid}"

            # —— ① 两个字段各自的非空与结构 ——
            for fld, human in (("doneRight", "已做对的部分"),
                               ("doneWrong", "做错的那一步（断点直接证据）")):
                rows = it.get(fld)
                if not rows:
                    rep.err(where, f"缺 {fld}（{human}）",
                            "① 段要求逐项对照原卷手写过程，而不是从最终答案倒推。"
                            "缺了它，「断点在哪」只能靠推断——而推断会把"
                            "「方法没想到」误判成「概念不清」，"
                            "两者的救法完全相反（见 MEMORY 错因判定纪律）")
                    continue
                if not isinstance(rows, list):
                    rep.err(where, f"{fld} 不是数组",
                            f"实际类型：{type(rows).__name__}")
                    continue
                for idx, row in enumerate(rows, 1):
                    if not isinstance(row, (list, tuple)) or len(row) != 2:
                        rep.err(where,
                                f"{fld} 第 {idx} 条不是 [他写下的, 说明] 二元组",
                                "形状必须固定，否则表格渲染与自动比对都会失准。"
                                f"实际：{type(row).__name__}，"
                                f"长度 {len(row) if isinstance(row, (list, tuple)) else '—'}")
                        continue
                    if not str(row[0]).strip() or not str(row[1]).strip():
                        rep.err(where, f"{fld} 第 {idx} 条有空的单元格",
                                "左边是他写在卷面上的东西，右边是判断依据，两者都要有内容")

            # —— ② 精讲页「已做对」表的每一行都要在数据层找得到 ——
            # 按「事实」建闸门：✓ 行查 doneRight、✗ 行查 doneWrong，
            # 只查 ✓ 会让断点证据永远查不出来（这正是本轮发现缺陷的方式）。
            page = it.get("page")
            if not page:
                continue
            ap_ = os.path.join(ROOT, page)
            if not os.path.exists(ap_):
                continue  # 文件缺失已由 check_five_stage_paradigm 报错
            try:
                src = open(ap_, encoding="utf-8").read()
            except Exception:
                continue

            h2 = src.find("你已经做对的部分")
            if h2 < 0:
                continue
            tbl = re.search(r"<table.*?</table>", src[h2:], re.S)
            if not tbl:
                continue

            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(0), re.S):
                # 判定格必须连**属性**一起抓：`tds[1]` 只有符号文本（'✓'/'✗'），
                # class="cross" 落在 <td> 的属性里 —— 只按符号判更稳（页面改配色不改符号）。
                tds = re.findall(r"<td([^>]*)>(.*?)</td>", tr, re.S)
                if len(tds) < 3:
                    continue  # 表头 / 单列行
                attrs, (said, mark, _note) = tds[0][0], [c[1] for c in tds[:3]]
                # ✗ = 判定符号是叉。**只看第 2 格**：页面里 <span class="cross">
                # 常用于行内强调（q5 拆解表就有一处），不是判定标记。
                is_cross = "✗" in mark or "cross" in attrs
                pool = [r[0] for r in (it.get("doneWrong") if is_cross
                                       else it.get("doneRight") or [])
                        if isinstance(r, (list, tuple)) and r]
                fld = "doneWrong" if is_cross else "doneRight"
                # 页面首格可能带 <code>/<b>/换行；按 <br> 拆后逐片比对。
                # （铁律三第五类假阳性：<br> 不是换行符。）
                for piece in re.split(r"<br\s*/?>", said, flags=re.I):
                    piece = _strip_tags(piece).strip()
                    if len(_norm_text(piece)) < 4:
                        continue
                    if not any(_rows_correspond(piece, c) for c in pool):
                        rep.err(where,
                                f"页面「已做对」表的{'✗' if is_cross else '✓'}行"
                                f"「{piece[:24]}」在数据层 {fld} 里找不到",
                                "这一列是给孩子看的**事实认定**。页面列出来了、数据层没有，"
                                "意味着重新生成页面时它会被静默丢掉"
                                "（生成器只渲染 doneRight）。"
                                + ("✗ 行是**断点的直接证据**，"
                                   "只记 ✓ 不记 ✗，「断点在哪」就退化成推断。"
                                   if is_cross else
                                   "✓ 行必须逐项对照原卷手写过程，不是从最终答案倒推的"))
    rep.ok()


def _cause_is_settled(rec):
    """错因是否**已定论**。

    判据沿用 check_answer_fields 的口径（第十四轮踩过一次坑）：
    `causePending: true` 表示这道题的病因**尚未判出来**。
    存疑题即使填了 `cause`（那只是一个暂记的猜测），也不能进统计。
    """
    return rec.get("cause") and rec.get("causePending") is not True


def check_cause_reasoning(rep, rawmap):
    """判错因的推理链：cause / causeNote / myThought / causeSecondary / causePending。

    2026-10-04 第十七次核验抓到的缺陷，是**第十四类（源头无闸门）家族的第四个成员**：

      第十四轮 `answerKey`/`childAnswer` · 第十五轮 `stem`/`breakdown` ·
      第十六轮 `doneRight`/`doneWrong` · **本轮 判错因的推理链**。

    为什么它比前三轮任何一个都严重：`cause` 决定**救法**。
    MEMORY 写着「方法没想到」与「概念不清」**救法完全相反**，混淆即无效刷题；
    而在第十四轮之前，**没有任何一道闸门读过 cause 的依据**——
    校验器对 `cause` 只做了一件事：查它在不在五类之内（还是 warn，不是 err），
    另外一条 warn 查「有没有 myThought/causeNote」，**但只看存不存在、不看写得对不对**。

    回退法自证（发现手段）——**九例注入，八例全漏网**：

      正向（应抓出，实际全部 0 错误）：
        A 把 q19 的 myThought 整段改成与题目无关的胡话
        B 把 q5 的中间步骤判读压缩成「粗心。」两个字
        E 把 `causeSecondary` 写成枚举外的「时间不够」
        F 把 `causeSecondary` 写成自由文本「手感不好」
        G 把 `causeSecondary` 写成数组（类型漂移）
        H 存疑题（causePending=true、causeNote 明说「不下定论」）
          却把 `cause` 改成定论性的「计算失误」
        D 存疑题 `cause=概念不清`，`causeNote` 却写「概念完全清楚」——自相矛盾
      唯一被抓出的：
        I 删掉 `causePending`/`uncertain` 标记——被**第十四轮那道闸门**
          间接抓到（causePending 缺失 ⇒ answerKey 变必填）。
          **这正说明两道闸门不能互相替代**：第十四轮那道抓的是「答案」，
          本轮要抓的是「推理链」，即使被连带命中也不能算覆盖。

    本轮发现的**第四层，比前三轮更深**：不是「字段错了没人查」，
    而是**下游统计直接把存疑题算进了错因分布**。
    `build_skeletons.py` 的错因分布表遍历全部 `wrongs`，
    只判 `w.get("cause")` 是否为空，**完全不看 `causePending`**——
    7 道失分题全部计入，而实际只有 4 道错因已定论（36/58/61 存疑）。
    ⇒ 报告上的「错因分布」有 3/7 是猜的。**本闸门同时修生成器根因。**

    覆盖范围：**`data/wrong/*.json` 的 items 与 `data/exams/*.json` 的 wrongs 两侧**。

    能力边界（勿误以为已被守住）：
      · **机器查不出「推理是否成立」**。`cause=概念不清` + `causeNote` 写
        「读的时候理解错了」在字面上完全通顺，本闸门通过——但它其实该是
        「审题错误」。**推理链的语义只能靠核卷**（看孩子的中间步骤）。
        本闸门能抓的是**结构性违规**：枚举外值、类型漂移、自相矛盾、
        套话敷衍、存疑却定论。**语义靠人，结构靠机器。**
      · 「套话」判据是**可自动判定的窄口径**（见下方 `_is_boilerplate`），
        不是「读起来像不像敷衍」——第九轮已证明全文关键词扫描 11 条里 9 条假阳性。
    """
    # 套话判定：**只认「极短 + 无任何具体步骤痕迹」这一种形态**。
    #
    # ⚠️ 本闸门自身踩过两次坑（铁律六第 4 条形态：豁免条件恒真）：
    #   首版加了「必须出现主因名字才判套话」这个前提，
    #   而套话恰恰**不含**类目名（「粗心」里没有「方法没想到」）
    #   ⇒ 该条件对全部真实套话恒为假，等于给它们发免死金牌。
    #   **判据的前提不能是「套话本来就不具备的性质」**——那是在给自己开免死金牌。
    #   二版补了「敷衍词表」，仍漏「与题目无关的占位句」这一形态：
    #   它既不短到 4 字、也不含任何已知敷衍词，却同样零信息量。
    # 三版去掉词表依赖：**判据收敛为「够短 + 不含任何具体步骤痕迹」**——
    # 无信息量的判读在这两个条件下必然被抓住，且不必穷举敷衍词。
    #
    # 为什么阈值定在 24 字而不是更严：第九轮已证明全文关键词扫描假阳性 9/11。
    # 第 35 题的合法 causeNote「句子结构意识够，只是落笔时漏了」只有 22 字，
    # 若把阈值压到 20 以下就会误报真实数据。**宁可漏，不可淹。**
    # （「句型/宾语/主语/标点」这几个词是刻意留的具体痕迹词：
    #   它们让短句能通过，正是第 35 题这类合法短判读不被误报的关键。）

    def _is_boilerplate(text):
        """是否属于「无信息量的套话/占位句」。只在**能自动判定**时返回 True。"""
        t = _norm_text(text)
        # 有实质长度的一律不判：判据只能是窄口径（第九轮教训）
        if len(t) >= 24:
            return False
        # 出现了具体步骤的痕迹（公式/语法点/题型词）⇒ 在讲具体步骤，不算套话
        if re.search(r"[=＝≈≠≤≥]|\d|函数|方程|顶点|根|抛物线|比例|相似|全等|"
                     r"平行|选项|问句|疑问词|连读|时态|单词|句型|宾语|主语|"
                     r"标点|公式|定理|定义|条件|读音|拼写|草图|红笔|原卷|审题|回读",
                     str(text)):
            return False
        # 够短、且不含任何具体痕迹 ⇒ 判为无信息量。
        # 短到 4 字（无论内容）必判；更长一些的也判——因为它既然不含任何
        # 具体步骤痕迹，短到这个程度就只能是套话或占位句。
        return True

    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for it in d.get("items", []):
            where = f"{rel}#{it.get('id', '?')}"
            _check_one_cause(rep, where, it, _is_boilerplate)

    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "exams", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for ex in d.get("exams", []) or []:
            eid = ex.get("id", "?")
            for w in ex.get("wrongs", []) or []:
                _check_one_cause(rep, f"{rel}#{eid}", w, _is_boilerplate,
                                 prefix=f"第{w.get('qno', '?')}题 ")
    rep.ok()


def _check_one_cause(rep, where, rec, is_boilerplate, prefix=""):
    """单条错因记录的推理链检查。wrong 侧与 exams 侧共用。"""
    cause = rec.get("cause")
    sec = rec.get("causeSecondary")
    note = rec.get("causeNote")
    thought = rec.get("myThought")
    pending = rec.get("causePending") is True

    # —— ① 副因必须是五类之一（只选一个，不接受数组/自由文本）——
    if sec not in (None, ""):
        if not isinstance(sec, str):
            rep.err(where, f"{prefix}causeSecondary 不是字符串",
                    "副因**只能选一个**（MEMORY：只选一个主因，副因同理）。"
                    f"实际类型：{type(sec).__name__}。"
                    "写成数组会让「主因唯一」这条纪律在数据层失效")
        elif sec not in CAUSES:
            rep.err(where, f"{prefix}副因不在五类之内：{sec}",
                    f"副因必须与主因同口径，应为 {'/'.join(sorted(CAUSES))} 之一。"
                    "自由文本副因无法统计，会在错因分布里凭空多出一类")
        elif cause and sec == cause:
            rep.err(where, f"{prefix}副因与主因相同：{sec}",
                    "副因必须是一个**不同的**病因，否则归类没有信息量")

    # —— ② 主因不在五类之内：这是 **err 不是 warn** ——
    # 第十四轮把它设成 warn，理由当时写的是「统计归类会漏」。
    # 但 warn 不会阻断管道 ⇒ 枚举外的错因能一路提交进 Git。
    if cause and cause not in CAUSES:
        rep.err(where, f"{prefix}错因不在五类之内：{cause}",
                f"应为 {'/'.join(sorted(CAUSES))} 之一（定义源 {CAUSE_DOC}）。"
                "**这是 err 不是 warn**——枚举外的值会让错因分布凭空多一类，"
                "而「方法没想到」与「概念不清」落进不同类目，救法正好相反。"
                "另：若此题确实判不出来，用 `causePending: true` 标明存疑，"
                "不要硬凑一个类目")

    # —— ③ 已定论却没写推理链 / 写的是套话 ——
    if _cause_is_settled(rec):
        if not note and not thought:
            rep.err(where, f"{prefix}错因已定论但没有推理链",
                    "只看最终答案会误判——「方法没想到」和「概念不清」救法完全相反。"
                    "必须写下**回看中间步骤**得到的判读（`causeNote` 或 `myThought`）")
        else:
            # 套话检测只在「极短 + 无具体步骤痕迹」时报，判据窄口径宁漏勿淹
            for fld, val in (("causeNote", note), ("myThought", thought)):
                if val and is_boilerplate(val):
                    rep.err(where,
                            f"{prefix}{fld} 是无信息量的套话",
                            f"内容仅「{str(val)[:20]}」，没有指向任何具体步骤。"
                            "推理链必须写明**回看了孩子哪一步中间过程**才得出这个错因——"
                            "这正是「方法没想到」与「概念不清」能分开的唯一依据")

    # —— ④ 存疑题却把错因写成定论（自相矛盾）——
    if pending and cause:
        rep.err(where, f"{prefix}causePending=true 但仍填了定论错因",
                "`causePending: true` 的含义是**病因尚未判出来**。"
                f"当前填的是「{cause}」——若它只是猜测，应清空 `cause` "
                "只保留 `causeNote` 写明为什么定不了；"
                "若确已定论，就去掉 `causePending`。"
                "两者并存会让统计把猜测当成结论（生成器按 `cause` 计数，"
                "存疑题会混进错因分布）")
    rep.ok()


def check_math_keypoints(rep):
    """data/resources/math-keypoints.json 是第 8 个数据入口，2026-10-04 新增。

    **为什么它不能被 check_resources 覆盖**：那个闸门只认
    `data.get("papers")` 是 list 的结构，碰到本文件（顶层是 `sections`）
    会 `continue` 跳过——**新增入口落进别人的 if 里，是最隐蔽的漏网**。
    形态属 MEMORY 记的「② 检查本身抓不到」：闸门在跑、报 0 错误、
    实际上这个文件一次都没被看过。

    **本闸门查的是「这份数据会被印进给孩子看的 Word」**，所以风险点有三条：
      1. `meta.count` 与实际条数不符 ⇒ 封面写「共 N 条」而正文不是 N 条
      2. `code` 重复 ⇒ 两个知识点同名，复习时定位不到
      3. `exam_evidence` 里引用的真题文件不存在 ⇒ 出处指向空气
    内容对不对是人的判断（教学判断不进机器闸门），本闸门不越权。
    """
    p = os.path.join(ROOT, "data", "resources", "math-keypoints.json")
    if not os.path.exists(p):
        return
    rel = "data/resources/math-keypoints.json"
    d = load(p, rep, rel)
    if not d:
        return

    meta = d.get("meta") or {}
    sections = d.get("sections") or []
    if not sections:
        rep.err(rel, "顶层 sections 为空或缺失",
                "本文件是「数学学习要点速查手册」的唯一内容源，"
                "sections 空了手册就是一本空壳，而生成器不会报错")
        rep.ok()
        return

    codes, n = [], 0
    for si, s in enumerate(sections):
        tier = s.get("tier")
        if tier not in ("tier1", "tier2", "tier3", "tier4"):
            rep.err(rel, f"sections[{si}].tier={tier!r} 不在 tier1..tier4 内",
                    "梯队 id 是生成器 `TIER_COLOR` 的索引，"
                    "拼错会让该梯队没有配色（标题退化成黑色，视觉分层失效）")
        for pt in s.get("points") or []:
            n += 1
            code = pt.get("code") or "?"
            codes.append(code)
            # page 为 null 是合法状态（九下章号待定），
            # 但 chapter 必须同步说明「待定」——否则页面会印出「章：九下 第 ? 章」
            pg, ch = pt.get("page"), pt.get("chapter") or ""
            if pg is None and "待定" not in ch and "综合与实践" not in ch:
                rep.err(rel, f"{code} page 为 null 但 chapter 未标「章号待定」：{ch}",
                        "空值必须显式说明「为什么空」，不能静默留白——"
                        "静默留白会让读者以为是漏印，而真相是「真题反推不出章号」")
            if pg is not None and "待定" in ch:
                rep.err(rel, f"{code} chapter 写「待定」却填了页码 p{pg}",
                        "章号还没确定却有页码，两边自相矛盾——"
                        "页码比章号更容易被当成事实引用")

    declared = meta.get("count")
    if declared is not None and declared != n:
        rep.err(rel, f"meta.count={declared} 与实际知识点 {n} 条不符",
                "封面直接印这个数字，对不上就是「封面说 29 条、翻完只有 28 条」")

    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        rep.err(rel, f"code 重复：{dup}",
                "code 是复习时的定位标识（练习入口、真题对照表都引它），"
                "重复会导致两处引用指向不同内容")

    # 引用的真题文件必须真实存在
    # ⚠ 判据踩坑（2026-10-04 当场踩到）：evidence_sources 里写的是
    #   「RAW/试卷库/（5 套上海数学真题：2023 中考 / …）」——
    #   **带括号描述**，直接 os.path.exists 整串必然 False，
    #   于是每次跑都报「出处指向空气」，而路径其实是对的。
    # ⇒ 取第一个括号/书名号前的路径前缀再判存在性。
    for s in meta.get("evidence_sources") or []:
        m = re.match(r"\s*((?:RAW|data|docs|tools|app)/[^\s（(【\[]+)", s)
        if not m:
            continue
        path = m.group(1).rstrip("：:")
        if not os.path.exists(os.path.join(ROOT, path)):
            rep.err(rel, f"meta.evidence_sources 引用的路径不存在：{path}",
                    "出处指向不存在的路径 = 出处指向空气")
    rep.ok()


# ══════════════════════════════════════════════════════════════════════
# 第 19 道闸门 · 第十八类缺陷：五段范式第 ④⑤ 段（variants / thinkQuestions）
#                       的**内容正确性**无闸门
# ══════════════════════════════════════════════════════════════════════

# 页面与数据层做**不等式数值断言**比对时用的形态。
# ⚠️ 只认 `≥N` / `≤N` 这两种**闭式**形态，且必须紧跟「数字」——
#    `>1` / `<2` 一律不认。这是本闸门**首跑时 7 条假阳性里的一处**：
#    页面 HTML 里的 `</b>`、`<sup>2</sup>`、SVG 的 `y="466"` 全被
#    「`>` 后面跟个数字」匹配上，把标签当成数学断言。
#    第九轮已证明「覆盖广」的判据假阳性会淹掉真错误，这里收窄到宁可漏不可淹。
_REL_CLAIM = re.compile(r"([≥≤])\s*(\d+(?:\.\d+)?)")


def _strip_html(text):
    """剥掉 HTML 标签后再做数值断言比对。

    ⚠️ 这不是洁癖：页面上的数学断言都在正文里，标签（`</b>`、`<sup>`、
    `<text y="466">`）不是断言。不剥掉就是把标签当数学结论。
    """
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", str(text),
               flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return s


def _rel_claims(text):
    """抽出文本里全部不等式数值断言，归一化成 {(符号, 数值)} 集合。"""
    return {(op, _norm_num(val)) for op, val in _REL_CLAIM.findall(_strip_html(text))}


def _norm_num(val):
    v = str(val).strip()
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return v


def check_variants_faithful(rep, rawmap):
    """三题变式与想五题：答案必须有**可判定的内部自洽**，且页面的数值断言不得多于数据层。

    2026-10-04 第十八次核验抓到的缺陷，是**第十四类（源头无闸门）家族的第五个成员**：
      第十四轮 answerKey/childAnswer · 第十五轮 stem/breakdown ·
      第十六轮 doneRight/doneWrong · 第十七轮 判错因的推理链 · **本轮 变式与想五题**。

    上一轮留的候选方向就是它。回退法自证（发现手段）——**八例注入，八例全漏网**
    （校验器报 0 错误）：

      正向（应抓出，实际全部漏网）：
        A 变式答案改成与正确结论矛盾的值
        B 变式答案压成「略。」
        C 变式 hint 清空
        D 简单变式的 stem 抄成**另一道题**的（简单变式却与本题完全无关）
        E 想五题答案写成「不知道」
        F 想五题答案字段类型漂移成数组
        G 变式答案退化成直接抄 answerKey
        H 问答对调变式的 change 改成「把问的方向反过来」——与第 1 题同一手法
      原因很直接：`check_five_stage_paradigm()` 查的是**配比与结构**，
      `check_tasks()` 查的是**字段非空**，**没有任何一道闸门读过答案本身**。
      而变式答案错了 = 孩子照着错答案练，与第 14 轮「答案错了整套教学判断全部反着走」同源。

    判定口径（全部按**可自动判定**的窄口径，不做语义推断）：
      ① 变式四字段 stem/change/hint/answer 缺一即报错（结构）。
      ② 变式答案不得为空话：套话判定复用第 17 轮那套
         「够短 + 无任何具体步骤痕迹」的窄口径，不另立词表。
      ③ ~~变式答案须在 hint 推导链里~~ **首跑 3 条假阳性后已删除**，
         理由见代码注释：**判据的前提不能是「正确内容本不具备的性质」**。
      ④ **变式答案不得与原题 answerKey 的数值集合完全相同**——三题变式的意义
         就在于换一个数据再走一遍，答案与原题一字不差说明没换。
      ⑤ **同题内变式答案两两不得完全相同**——变式 1 和变式 3 答案相同，
         等于两个练习位只覆盖一种能力（第十三轮已在 q5 上踩过这个形态）。
      ⑥ 问答对调变式的**所求量必须真的变了**（与原题所求不同）。
      ⑦ 想五题答案必须能拆出**可判题段**（≥2 字的判据至少一个），
         否则孩子答什么都判对/判错，即时判题形同虚设。
      ⑧ **页面的不等式数值断言不得多于数据层**（跨层一致，见铁律五）。

    ⚠️ 能力边界（勿误以为已被守住）：
      · **机器判不出变式答案对不对**，只能判「答案有没有自己的推导依据」。
        变式 hint 与答案若**一起改错**，⑧ 之外的口径全部放行——
        这与第 15 轮「两侧一致 ≠ 两侧都对」同源，语义只能靠核卷。
      · 口径③要求答案的数值能在 hint 里找到，故**纯文字答案的学科（语文/英语）
        天然不适用**——那里答案不是数值。本闸门对这类记录自动跳过 ③④，
        只保留结构类检查①②⑤⑥⑦，避免闸门自己变成噪音源。
    """
    def _is_empty_answer(t):
        """变式/想五题的答案是不是**真·占位符**。

        ⚠️ 本函数踩过两次坑，两次都是**回退法逼出来的**：

        坑 1（首跑误报真实数据）：首版直接复用第 17 轮那套「够短 + 无具体痕迹」
        的套话判据，把 q5 想五题 1 的答案「BD 换 DC / AF 换 FC / 同一条边换段」
        判成零信息量——**这正是即时判题要用的判据串**。
        ⇒ 判据串与套话在字面上无法区分（都是「够短的中文」），
        区别只在**有没有具体名词**。

        坑 2（回退法抓到漏网）：二版改成「必须含数字或字母」才算有内容，
        而「略。」「不知道」全是**中文** ⇒ 反而被判成「有内容」放过去了。
        ⇒ 中文本身就是内容信号，不能用它当判据。

        现行口径：**白名单式占位符表**（整串匹配）+ **非字符串直接判违规**。
        之所以敢用词表：判据是 `fullmatch` 整串相等，不是「含某个词」，
        不存在「正文里恰好出现『不知道』三个字」这类假阳性。
        """
        # 类型漂移：数组/数字/对象都不是答案
        if t is not None and not isinstance(t, str):
            return True
        s = _norm_text(t)
        if not s:
            return True
        # 整串相等的占位符（不是"含"，所以不会误伤正文）
        # ⚠️ 词表要同时含 `_norm_text` 归一后的形态：它会把中文句号转成 `.`，
        #    所以「略。」归一后是「略.」。漏了这一点「略。」就会漏网
        #    （回退法 C2 实测抓到的）。
        if s in {"略", "略.", "无", "不", "未知", "没", "不知道", "不确定",
                 "待补", "待补齐", "待补全", "待定", "待填写", "同上",
                 "见上", "见下", "答案", "答", "?", "？", "答不上来",
                 "不会", "没思路", "空白"}:
            return True
        return False

    def _wanted(text):
        """取所求量：最后一个「那么」之后，否则最后一个「求」之后。"""
        s = _norm_text(text)
        i = s.rfind("那么")
        if i >= 0:
            return s[i + 2:]
        i = s.rfind("求")
        if i >= 0:
            return s[i + 1:]
        return s

    for rel_j in sorted(glob.glob(os.path.join(ROOT, "data", "wrong", "*.json"))):
        rel = os.path.relpath(rel_j, ROOT)
        d = load(rel_j, rep, rel)
        if not d:
            continue
        for it in d.get("items", []):
            where = f"{rel}#{it.get('id', '?')}"
            vs = it.get("variants") or []
            tq = it.get("thinkQuestions") or []

            # —— ①② 四字段结构 + 答案不得空话 ——
            for i, v in enumerate(vs, 1):
                if not isinstance(v, dict):
                    rep.err(where, f"第 {i} 个变式不是对象",
                            f"实际类型：{type(v).__name__}。"
                            "类型漂移会让下面的字段检查全部静默失效")
                    continue
                lack = [k for k in ("stem", "change", "hint", "answer")
                        if not str(v.get(k, "")).strip()]
                if lack:
                    rep.err(where, f"第 {i} 个变式缺 {'/'.join(lack)}",
                            "变式四件套（题干/改了什么/思路/答案）缺任何一件，"
                            "孩子就只剩一道没头绪的题——练不成方法。")
                if _is_empty_answer(v.get("answer")):
                    rep.err(where, f"第 {i} 个变式的答案是空话",
                            f"实际：{str(v.get('answer'))[:20]!r}。"
                            "答案压成「略。」「不知道」这类零信息量内容，"
                            "等于这一题什么都没练。")
            for j, t in enumerate(tq, 1):
                if not isinstance(t, dict):
                    rep.err(where, f"第 {j} 个想五题不是对象",
                            f"实际类型：{type(t).__name__}。类型漂移会让判题关键词检查失效")
                    continue
                if not str(t.get("q", "")).strip():
                    rep.err(where, f"第 {j} 个想五题没有题面",
                            "想五题的「问」不能为空——没有问就没有递进")
                if _is_empty_answer(t.get("answer")):
                    rep.err(where, f"第 {j} 个想五题的答案是空话",
                            f"实际：{str(t.get('answer'))[:20]!r}。"
                            "即时判题靠这些关键词判定孩子答对没答对，空话判不了。")

            # —— ③⑤⑥ 需要数值型答案，纯文字学科（语文/英语）不适用 ——
            ans_marks = {}
            for i, v in enumerate(vs, 1):
                if not isinstance(v, dict):
                    continue
                am = _num_marks(v.get("answer"))
                if not am:
                    continue
                ans_marks[i] = am
                # ⚠️ 这里**曾经**有第三条口径「答案的数值必须在 hint 推导链里出现」，
                #   首跑即 3 条假阳性，**已删除**——它的前提是错的：
                #     · q19 变式 3 的答案是**负向答案**（「b 无法唯一确定」），
                #       其中的 3/4 只是引述原题条件，不是本题的推导结论；
                #     · q5 变式 1 的答案是选项 B（4/3），而 hint 的推导链
                #       止于 AF/AC=4/7、FC=3/7，**不必重写最终比值**——
                #       答案本来就是「把推导链的结果写成一句话」。
                #   ⇒ 「答案 ⊆ 推导链」把**合法的精简**判成了抄错。
                #   **判据的前提不能是「正确内容本不具备的性质」**（铁律六第 4 条形态）。
                #   若 hint 与答案一起改错，这类检查同样会放行，收益本就有限。
                # ⑤ 同题内答案两两不得完全相同
                for j2, am2 in ans_marks.items():
                    if j2 < i and am2 == am:
                        rep.err(where,
                                f"第 {i} 个与第 {j2} 个变式的答案完全相同：{sorted(am)[:4]}",
                                "两个练习位给同一个答案，等于只练了一种能力。"
                                "第十三轮在 q5 上已踩过这个形态（变式 1 与变式 3 答案都是 A）。")
            # ④ 变式答案不得与原题答案一字不差
            akm = _num_marks(it.get("answerKey"))
            for i, am in ans_marks.items():
                if akm and am == akm:
                    rep.err(where,
                            f"第 {i} 个变式的答案与原题答案完全相同：{sorted(am)[:4]}",
                            "三题变式的全部意义就是「换一个数据再走一遍同一手法」。"
                            "答案与原题一字不差，说明这个变式其实没换数据，"
                            "只是在同一个答案上多花了孩子五分钟。")
            # ⑥ 问答对调的所求量必须真的变了
            w0 = _wanted(it.get("stem", ""))
            for i, v in enumerate(vs, 1):
                if not isinstance(v, dict):
                    continue
                if not re.search(r"(问答对调|对调)", str(v.get("type", ""))):
                    continue
                wv = _wanted(v.get("stem", ""))
                if w0 and wv == w0:
                    rep.err(where,
                            f"第 {i} 个问答对调变式，所求量与原题完全相同",
                            f"两边都是：{w0[:30]!r}。"
                            "问答对调的硬性标准是**已知与所求互换**——"
                            "所求没换，就不是对调，只是把原题重抄了一遍。")

            # —— ⑦ 想五题必须能拆出可判题段 ——
            for j, t in enumerate(tq, 1):
                if not isinstance(t, dict):
                    continue
                segs = [s for s in re.split(r"[|/、,，]", str(t.get("answer", "")))
                        if len(_norm_text(s)) >= 2]
                if not segs:
                    rep.err(where,
                            f"第 {j} 个想五题拆不出任何可判题段",
                            f"实际：{str(t.get('answer'))[:24]!r}。"
                            "即时判题靠「孩子输入里出现了哪个判据」来给反馈，"
                            "判据为空 = 答什么都算对，这一题对练习没有任何约束力。")

            # —— ⑧ 页面不等式断言不得多于数据层（跨层一致，铁律五）——
            page = it.get("page")
            if not page:
                continue
            ap_ = os.path.join(ROOT, page)
            if not os.path.exists(ap_):
                continue
            try:
                src = open(ap_, encoding="utf-8").read()
            except Exception:
                continue
            data_claims = set()
            for t in tq:
                if isinstance(t, dict):
                    data_claims |= _rel_claims(t.get("q", ""))
                    data_claims |= _rel_claims(t.get("answer", ""))
            data_claims |= _rel_claims(it.get("methodBoundary", ""))
            for s in it.get("steps") or []:
                if isinstance(s, dict):
                    data_claims |= _rel_claims(json.dumps(s, ensure_ascii=False))
            page_claims = _rel_claims(src)
            extra = sorted(page_claims - data_claims)
            if extra:
                rep.err(where,
                        f"页面出现数据层无据的不等式断言：{'、'.join(a + b for a, b in extra)}",
                        "同一事实散落两处时，以数据层为准（铁律五）。"
                        "本闸门只审**可自动判定**的数值断言（≥N/≤N 这类闭式形态），"
                        "不做全文数字扫描——第九轮已证明那样假阳性 9/11。"
                        "实际修法：把数值改对，或把这个结论补进数据层。")
    rep.ok()


def _num_marks(text):
    """抽出文本里的数值标记（分数优先，整数次之），用于跨字段比对。"""
    t = str(text or "")
    marks = {x.replace(" ", "") for x in
             re.findall(r"[−\-]?\d+\s*/\s*\d+", t)}
    marks |= {x.replace(" ", "") for x in
              re.findall(r"(?<![/\d])[−\-]?\d+(?:\.\d+)?(?!\s*/)", t)}
    return marks


def check_function_paper(rep):
    """data/resources/function-topic-paper.json 是第 9 个数据入口，2026-10-04 新增。

    **为什么单独立 check**：与 math-keypoints 同理——
    `check_resources` 只认顶层 `papers` 是 list 的结构，
    本文件顶层是 `sections` ⇒ 会被 continue 跳过（第二十类形态复发）。

    **专题卷的特殊风险**：它是**练习卷**，同一份数据要渲染出
    「学生卷（无答案）」与「答案卷（有答案）」两份文件。
    风险点是**答案泄露进学生卷**——本轮实际踩到（第十九类形态复发）：
    `build_function_paper.py` 里答案卷区块 24 处 `para(doc,...)` 全误写成 `doc`
    而非 `adoc`，结果答案、步骤、易错点全进学生卷，
    **而 docx 大小/段落数/打开效果全都正常，肉眼扫不出来**。

    本闸门查数据层的自洽性（分值、字段、来源）；
    「答案有没有漏进学生卷」由 `build_function_paper.py` 的
    `selfcheck()` 查产成品——**两个闸门各管一段，不互相替代。**
    """
    p = os.path.join(ROOT, "data", "resources", "function-topic-paper.json")
    if not os.path.exists(p):
        return
    rel = "data/resources/function-topic-paper.json"
    d = load(p, rep, rel)
    if not d:
        return

    sections = d.get("sections") or []
    if not sections:
        rep.err(rel, "顶层 sections 为空或缺失",
                "专题卷的全部内容都在 sections 里，空了生成器会产出一份空白卷，"
                "而 docx 打开完全正常")
        rep.ok()
        return

    nos = []
    total = 0
    for sec in sections:
        for q in sec.get("questions") or []:
            nos.append(q.get("no"))
            total += q.get("score") or 0
            for k in ("no", "score", "point", "source", "stem", "answer", "steps"):
                if not q.get(k):
                    rep.err(rel, f"第 {q.get('no', '?')} 题缺字段 {k}",
                            "练习卷缺字段 ⇒ 学生卷与答案卷都会少一整段，"
                            "而文件能正常打开")

    dup = sorted({n for n in nos if n in (None,) or nos.count(n) > 1})
    if dup:
        rep.err(rel, f"题号重复或缺失：{dup}",
                "题号是学生与家长对照的唯一标识，重复会让「第 24 题」指到两道题")

    # 分值合计：分值是这个项目最敏感的数字（MEMORY 分值纪律）
    if total != 120:
        rep.err(rel, f"全卷合计 {total} 分，与 meta 声明的 120 分不符",
                "分值必须是派生量（现算），手写必然漂移")

    # 试卷类产物不许把答案写进题干
    import re as _re
    for sec in sections:
        for q in sec.get("questions") or []:
            if _re.search(r"答案(是|为|：|:)\s*[A-D]", str(q.get("stem") or "")):
                rep.err(rel, f"第 {q['no']} 题题干里出现答案字样：{str(q['stem'])[:40]}",
                        "题干印答案 ⇒ 学生卷等于把答案抄在题面上，"
                        "而打开文档完全正常")

    # 自检节必须存在：编题出错时要向使用者交代，不能默默发一份有错的卷
    if not d.get("self_check", {}).get("items"):
        rep.err(rel, "缺 self_check.items",
                "本卷已声明有 3 处编题瑕疵（见 JSON）。"
                "**交付物必须交代已知瑕疵**——发一份有错的练习卷而不说，"
                "比卷子有错本身更糟")
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
    check_breakdown_faithful(rep, rawmap)
    check_done_right_faithful(rep, rawmap)
    check_cause_reasoning(rep, rawmap)
    check_variants_faithful(rep, rawmap)
    check_answer_fields(rep, rawmap)
    check_papers_derived(rep)
    check_doc_numbers(rep)
    check_data_readme(rep)
    check_math_keypoints(rep)
    check_function_paper(rep)
    rep.print()
    sys.exit(1 if rep.errors and not args.warn else 0)


if __name__ == "__main__":
    main()
