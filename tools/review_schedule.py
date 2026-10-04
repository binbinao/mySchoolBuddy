#!/usr/bin/env python3
"""review_schedule.py — 复测排期引擎（艾宾浩斯 R1-R6）

**为什么这个脚本存在**

`data/wrong/*.json` 的 `reviewRounds` 字段从建库起就是空的，而 `docs/方法论/错因分类与复习排期.md`
早已定义好 R1-R6 六个轮次。这意味着：**排期规则写在纸上，但从没有任何代码执行它。**
结果就是——2026-10-02 录入的两条错题，到今天 10-04 已经**静默错过 R1 与 R2**，
没有任何机制告诉任何人。

这是典型的「规范有、执行无」缺口：规范文档被 `check_data_readme` 守着，
但守的是「文档写没写」，不是「有没有人按它做」。

**本脚本做什么**

1. 读 `data/wrong/*.json`，按 `ts` 推算每条错题的 R1-R6 到期日
2. 输出**今日到期 / 已过期 / 未到期**三类，生成 `docs/实战表/复测看板.html`
3. 把「下一次该练什么」直接排出来，附每轮的通过标准（照抄方法论，不另立一套）

**不做什么（硬边界）**

- ❌ 不判定对错——「这次复测过没过」只能由人（或 AI 看她重做的过程）判定
- ❌ 不自动推进轮次——过没过要有人给结论，脚本只负责**提醒该测了**
- ❌ 不改 `data/wrong/*.json`——它是事实层，只读

这与项目既有纪律一致：脚本只做确定性填充，不做教学判断（`tools/README.md` 原则第一条）。

**用法**

```bash
tools/review_schedule.py                # 生成看板
tools/review_schedule.py --date 2026-10-06   # 假装今天是某天（看未来排期）
tools/review_schedule.py --dry-run     # 只打印，不写盘
tools/review_schedule.py --json        # 机器可读输出（供其他工具消费）
```

**R1-R6 定义（定义源 docs/方法论/错因分类与复习排期.md，不在此处另立一套）**

| 轮次 | 距首次 | 动作 | 通过标准 |
|---|---|---|---|
| R1 | 当天 | 遮住答案，独立重做一遍 | 能独立写出关键步骤 |
| R2 | 第 2 天 | 重做 + 口述思路 | 思路完整，无需提示 |
| R3 | 第 4 天 | 只做变式（换数字/换问法） | 变式也能独立完成 |
| R4 | 第 7 天 | 限时做（5 分钟内） | 限时内完成且正确 |
| R5 | 第 15 天 | 混入套卷，不单独练 | 不再触发提醒 |
| R6 | 第 30 天 | 完整章节复测 | 掌握，退出复测名单 |
"""

import argparse
import datetime
import glob
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRONG_GLOB = os.path.join(ROOT, "data", "wrong", "*.json")
OUT_PATH = os.path.join(ROOT, "docs", "实战表", "复测看板.html")

# 距首次的天数 —— 唯一真相在 docs/方法论/错因分类与复习排期.md
ROUNDS = [
    (1, 0, "遮住答案，独立重做一遍", "能独立写出关键步骤"),
    (2, 2, "重做 + 口述思路", "思路完整，无需提示"),
    (3, 4, "只做变式（换数字/换问法）", "变式也能独立完成"),
    (4, 7, "限时做（5 分钟内）", "限时内完成且正确"),
    (5, 15, "混入套卷，不单独练", "不再触发提醒"),
    (6, 30, "完整章节复测", "掌握，退出复测名单"),
]

PALETTE = {
    "overdue": ("#FCEBEB", "#F09595", "#501313", "#F7C1C1"),
    "today":   ("#FAEEDA", "#EF9F27", "#412402", "#FAC775"),
    "ahead":   ("#E6F1FB", "#85B7EB", "#042C53", "#B5D4F4"),
    "done":    ("#EAF3DE", "#97C459", "#173404", "#C0DD97"),
}


def esc(s):
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def parse_ts(item):
    """错题的「首次录入日」。ts 是毫秒时间戳。"""
    ts = item.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.datetime.fromtimestamp(ts / 1000).date()
    return None


def load_items():
    items = []
    for path in sorted(glob.glob(WRONG_GLOB)):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ⚠ 读不到 {os.path.relpath(path, ROOT)}：{e}", file=sys.stderr)
            continue
        for it in d.get("items", []):
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it["_file"] = os.path.relpath(path, ROOT)
            items.append(it)
    return items


def build_schedule(items, today):
    """算出每条错题每轮的状态。**不写回数据层**——判定权归人。"""
    rows = []
    for it in items:
        first = parse_ts(it)
        if first is None:
            rows.append({"item": it, "first": None, "rounds": [], "skip": "缺 ts，无法排期"})
            continue
        passed = set()
        for r in it.get("reviewRounds") or []:
            if isinstance(r, dict) and r.get("pass") is True:
                passed.add(r.get("round"))
        rounds = []
        for rid, offset, action, criteria in ROUNDS:
            due = first + datetime.timedelta(days=offset)
            if rid in passed:
                state = "done"
            elif due < today:
                state = "overdue"
            elif due == today:
                state = "today"
            else:
                state = "ahead"
            rounds.append({"round": rid, "offset": offset, "due": due,
                           "action": action, "criteria": criteria, "state": state})
        rows.append({"item": it, "first": first, "rounds": rounds, "skip": None})
    return rows


def counts(rows, state):
    return sum(1 for r in rows for x in r["rounds"] if x["state"] == state)


def next_round_for(row, today):
    """下一轮「该做但还没做」的轮次。已做的不算。"""
    for x in row["rounds"]:
        if x["state"] in ("overdue", "today"):
            return x
    for x in row["rounds"]:
        if x["state"] == "ahead":
            return x
    return None


# ── 看板渲染 ──────────────────────────────────────────────────

def render_html(rows, today, meta):
    def card(r):
        it = r["item"]
        title = it.get("title") or f"第 {it.get('qno', '?')} 题"
        sub = f"{esc(it.get('subject', '?'))} · {esc(it.get('module') or it.get('point') or '未标模块')}"
        cause = it.get("cause")
        cause_html = (
            f'<span class="cause">{esc(cause)}</span>' if cause
            else '<span class="cause pend">存疑 · 不计入统计</span>'
        )
        if r["skip"]:
            return (f'<article class="card"><h3>{esc(title)}</h3>'
                    f'<p class="sub">{sub}</p><p class="warn">{esc(r["skip"])}</p></article>')

        nxt = next_round_for(r, today)
        if nxt is None:
            nxt_html = '<p class="ok">六轮已全部通过 —— 已退出复测名单</p>'
        else:
            d = (nxt["due"] - today).days
            when = "今天" if d == 0 else (f"已过期 {d} 天" if d < 0 else f"{d} 天后")
            cls = "overdue" if d < 0 else ("today" if d == 0 else "ahead")
            nxt_html = (
                f'<div class="next {cls}"><span class="rnd">R{nxt["round"]}</span>'
                f'<div class="body"><p class="act">{esc(nxt["action"])}</p>'
                f'<p class="crit">通过标准：{esc(nxt["criteria"])}</p>'
                f'<p class="when">{when}（{nxt["due"]:%m-%d} 到期）</p></div></div>'
            )

        tl = "".join(
            f'<li class="{x["state"]}"><b>R{x["round"]}</b>'
            f'<span>{x["due"]:%m-%d}</span></li>' for x in r["rounds"]
        )
        page = it.get("page")
        page_html = (f'<a class="page" href="../实战表/'
                     f'{esc(os.path.relpath(page, "docs/实战表"))}">看精讲页 →</a>'
                     if page and os.path.exists(os.path.join(ROOT, page)) else "")
        return (f'<article class="card"><h3>{esc(title)}</h3>'
                f'<p class="sub">{sub} · {cause_html}</p>'
                f'{nxt_html}<ol class="tl">{tl}</ol>{page_html}</article>')

    c_od, c_td, c_ah, c_dn = counts(rows, "overdue"), counts(rows, "today"), counts(rows, "ahead"), counts(rows, "done")
    n_items = len(rows)
    gaps = sum(1 for r in rows if r["first"] and not (r["item"].get("reviewRounds") or []))

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>复测看板 · {today:%Y-%m-%d}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 20px 64px;background:#FAFAF8;color:#1C1C1E;
 font:16px/1.75 -apple-system,"PingFang SC",sans-serif}}
.wrap{{max-width:780px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:#71717A;font-size:14px;margin:0 0 24px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:28px}}
.stat{{border-radius:12px;padding:14px 16px}}
.stat b{{display:block;font-size:28px;font-weight:500;line-height:1.2}}
.stat span{{font-size:12px}}
.s-overdue{{background:{PALETTE['overdue'][0]};border:1px solid {PALETTE['overdue'][1]}}}
.s-overdue b,.s-overdue span{{color:{PALETTE['overdue'][2]}}}
.s-today{{background:{PALETTE['today'][0]};border:1px solid {PALETTE['today'][1]}}}
.s-today b,.s-today span{{color:{PALETTE['today'][2]}}}
.s-ahead{{background:{PALETTE['ahead'][0]};border:1px solid {PALETTE['ahead'][1]}}}
.s-ahead b,.s-ahead span{{color:{PALETTE['ahead'][2]}}}
.s-done{{background:{PALETTE['done'][0]};border:1px solid {PALETTE['done'][1]}}}
.s-done b,.s-done span{{color:{PALETTE['done'][2]}}}
h2{{font-size:17px;margin:32px 0 12px}}
.card{{background:#fff;border:1px solid #E4E4E0;border-radius:12px;padding:18px 20px;margin-bottom:14px}}
.card h3{{font-size:17px;margin:0 0 4px;font-weight:500}}
.card .sub{{font-size:13px;margin:0 0 12px}}
.cause{{display:inline-block;background:#F4F4F1;border-radius:4px;padding:1px 7px;font-size:12px}}
.cause.pend{{background:#FAEEDA;color:#854F0B}}
.next{{display:flex;gap:14px;align-items:flex-start;border-radius:10px;padding:12px 14px;margin-bottom:12px}}
.next.overdue{{background:{PALETTE['overdue'][0]};border-left:3px solid {PALETTE['overdue'][1]}}}
.next.today{{background:{PALETTE['today'][0]};border-left:3px solid {PALETTE['today'][1]}}}
.next.ahead{{background:{PALETTE['ahead'][0]};border-left:3px solid {PALETTE['ahead'][1]}}}
.rnd{{font-size:15px;font-weight:500;flex:0 0 auto}}
.next .body p{{margin:0}}
.act{{font-size:14px}}
.crit{{font-size:12px;opacity:.75}}
.when{{font-size:12px;opacity:.75;margin-top:2px!important}}
.next.overdue .rnd,.next.overdue .act{{color:{PALETTE['overdue'][2]}}}
.next.today .rnd,.next.today .act{{color:{PALETTE['today'][2]}}}
.next.ahead .rnd,.next.ahead .act{{color:{PALETTE['ahead'][2]}}}
.tl{{list-style:none;display:flex;gap:4px;padding:0;margin:0}}
.tl li{{flex:1;text-align:center;font-size:11px;padding:5px 0;border-radius:5px;background:#F4F4F1;color:#8A8A85}}
.tl li b{{display:block;font-size:12px;font-weight:500}}
.tl li.overdue{{background:{PALETTE['overdue'][0]};color:{PALETTE['overdue'][2]}}}
.tl li.today{{background:{PALETTE['today'][0]};color:{PALETTE['today'][2]}}}
.tl li.done{{background:{PALETTE['done'][0]};color:{PALETTE['done'][2]}}}
.page{{display:inline-block;margin-top:12px;font-size:13px;color:#185FA5;text-decoration:none}}
.ok{{font-size:14px;color:{PALETTE['done'][2]};margin:0}}
.warn{{font-size:13px;color:{PALETTE['overdue'][2]};margin:0}}
.gap{{background:#1C1C1E;color:#E4E4E0;border-radius:12px;padding:16px 18px;margin-top:8px}}
.gap h3{{color:#fff;font-size:15px;margin:0 0 8px}}
.gap p{{margin:0 0 6px;font-size:13px}}
.gap b{{color:#F59E0B}}
footer{{margin-top:40px;padding-top:16px;border-top:1px solid #E4E4E0;color:#8A8A85;font-size:12px}}
</style></head><body><div class="wrap">
<h1>复测看板</h1>
<p class="sub">{today:%Y 年 %m 月 %d 日}　·　艾宾浩斯 R1-R6　·　距中考 {meta['days_left']} 天</p>

<div class="stats">
<div class="stat s-overdue"><b>{c_od}</b><span>已过期</span></div>
<div class="stat s-today"><b>{c_td}</b><span>今天到期</span></div>
<div class="stat s-ahead"><b>{c_ah}</b><span>未到期</span></div>
<div class="stat s-done"><b>{c_dn}</b><span>已通过</span></div>
</div>

{'' if gaps == 0 else f'''<div class="gap"><h3>⚠ {gaps} 条错题的复测记录是空的</h3>
<p>排期规则写在方法论里，但 <b>reviewRounds 从没被写过</b>——所以没有任何机制告诉过我们哪一轮到期了。</p>
<p>请先做一次「基线复测」：按 R1 的标准（遮住答案独立重做一遍），过就把 R1 记上，不��就跳到当前该做的那一轮。</p></div>'''}

<h2>今天该做什么（{n_items} 条错题）</h2>
{''.join(card(r) for r in rows) if rows else '<p class="sub">data/wrong/ 还没有错题记录。</p>'}

<footer>
R1-R6 的动作与通过标准来自 <code>docs/方法论/错因分类与复习排期.md</code>，本看板不另立一套。<br>
本页由 <code>tools/review_schedule.py</code> 生成 —— 它只算「该测了」，不判「过了没」，
也不自动推进轮次：<b>过没过要有人给结论。</b><br>
数据源 {esc(' · '.join(sorted({r['item']['_file'] for r in rows if 'item' in r}))) or '—'}
</footer>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="艾宾浩斯 R1-R6 复测排期引擎")
    ap.add_argument("--date", help="基准日 YYYY-MM-DD，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    today = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    items = load_items()
    rows = build_schedule(items, today)
    exam = datetime.date(2027, 6, 20)
    meta = {"today": today.isoformat(), "items": len(rows),
            "days_left": (exam - today).days}

    if args.json:
        out = {"meta": meta, "counts": {
            "overdue": counts(rows, "overdue"), "today": counts(rows, "today"),
            "ahead": counts(rows, "ahead"), "done": counts(rows, "done")},
            "items": [{"id": r["item"].get("id"), "qno": r["item"].get("qno"),
                       "title": r["item"].get("title"), "subject": r["item"].get("subject"),
                       "cause": r["item"].get("cause"),
                       "first": r["first"].isoformat() if r["first"] else None,
                       "rounds": [{"round": x["round"], "due": x["due"].isoformat(),
                                   "state": x["state"], "action": x["action"],
                                   "criteria": x["criteria"]} for x in r["rounds"]]}
                      for r in rows]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"复测排期 · {today} · 距中考 {meta['days_left']} 天")
    print(f"  错题 {len(rows)} 条 · 已过期 {counts(rows,'overdue')} · 今天 {counts(rows,'today')}"
          f" · 未到期 {counts(rows,'ahead')} · 已通过 {counts(rows,'done')}")
    for r in rows:
        nxt = next_round_for(r, today)
        head = f"  q{r['item'].get('qno','?')} {r['item'].get('title','')}"
        if nxt is None:
            print(head + " → 六轮已通过，退出名单")
        else:
            d = (nxt["due"] - today).days
            tag = f"已过期 {-d} 天" if d < 0 else ("今天到期" if d == 0 else f"{d} 天后")
            print(head + f" → R{nxt['round']} {nxt['action']}（{tag}）")

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return 0
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(rows, today, meta))
    print(f"\n已生成 {os.path.relpath(OUT_PATH, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
