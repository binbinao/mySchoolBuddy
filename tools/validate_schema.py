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
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAUSES = {"概念不清", "方法没想到", "计算失误", "审题错误", "表达不规范"}


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
    wrong_items = {}
    wd = os.path.join(ROOT, "data", "wrong")
    if os.path.isdir(wd):
        for fn in sorted(os.listdir(wd)):
            if not fn.endswith(".json"):
                continue
            wdd = load(os.path.join(wd, fn), rep, f"data/wrong/{fn}")
            for it in (wdd or {}).get("items", []):
                wrong_items[it.get("id")] = it

    for t in items:
        if t.get("status") != "done":
            continue
        want = t.get("fields") or []
        if not want:
            continue
        # 任务 id 形如 wrong-w-20261002-01，对应错题 id 去掉前缀后的 w-20261002-01
        tid = str(t.get("id", ""))
        wid = tid.replace("wrong-", "", 1) if tid.startswith("wrong-") else tid
        it = wrong_items.get(wid)
        if it is None:
            continue
        missing = [f for f in want if not it.get(f)]
        if missing:
            rep.err("data/tasks/pending.json",
                    f"任务 {t.get('id')} 标为 done，但 JSON 里字段仍为空：{'、'.join(missing)}",
                    f"内容可能只写进了 {t.get('target')} 的 HTML，数据层读不到。"
                    "按纪律要同时回写 data/ 里的 JSON，否则生成器与统计都拿不到")


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
    rep.print()
    sys.exit(1 if rep.errors and not args.warn else 0)


if __name__ == "__main__":
    main()
