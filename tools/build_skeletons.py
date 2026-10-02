#!/usr/bin/env python3
"""build_skeletons.py — 从结构化数据生成讲义骨架（单文件 HTML）

职责边界（重要）：
  脚本只做「确定性填充」——把 JSON 里已有的题干、答案、错因、分值，
  渲染成排版完整、交互可用的 HTML。**不做教学判断。**

  需要教学判断的部分（解法步骤怎么拆、断点怎么讲、变式题怎么设计），
  一律输出 `⚠️ 待补` 占位并登记到 data/tasks/pending.json，
  交给 AI 补全环节处理。理由：解法步骤写错了比没有更危险。

产出：
  data/wrong/ 中每条 status=pending 的错题 → docs/实战表/<学科>/q<题号>-<主题>-精讲.html
  data/exams/ 中每份试卷              → docs/实战表/<学科>/<日期>-<卷名>-试卷拆解.html

用法：
  tools/build_skeletons.py                # 生成/更新（有变化才写盘）
  tools/build_skeletons.py --dry-run      # 只列将要生成什么
  tools/build_skeletons.py --help
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(ROOT, "tools", "assets", "coach.css")
TASKS_PATH = os.path.join(ROOT, "data", "tasks", "pending.json")

# ── 视觉：沿用精讲页钛金灰 + 暖金，强调待补区块用琥珀色 ──────────────
TODO = 'border-left:3px solid #f59e0b;background:#fff8ec;color:#854f0b'


def esc(s):
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"  ⚠ JSON 解析失败 {os.path.relpath(path, ROOT)}: {e}", file=sys.stderr)
        return None


def rel(from_file, to_path):
    """相对路径，用于 HTML 里引用图片。"""
    return os.path.relpath(to_path, os.path.dirname(from_file))


# ══════════════════════════════════════════════════════════════════
#  待补任务登记
# ══════════════════════════════════════════════════════════════════

class TaskQueue:
    """收集需要 AI 补全的教学内容，写入 data/tasks/pending.json。

    已完成的条目由 AI 补全后在这里标记 done，脚本不会重复登记。
    """

    def __init__(self):
        self.items = []

    def add(self, tid, kind, target, subject, module, reason, fields):
        self.items.append({
            "id": tid,
            "kind": kind,
            "target": target,
            "subject": subject,
            "module": module,
            "reason": reason,
            "fields": fields,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    def write(self, dry_run):
        if dry_run:
            return
        os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
        # 保留历史 done 记录，避免 AI 补完后被抹掉
        old = load_json(TASKS_PATH) or {}
        done = {i["id"]: i for i in old.get("items", []) if i.get("status") == "done"}
        merged = []
        for it in self.items:
            if it["id"] in done:
                merged.append(done[it["id"]])
            else:
                merged.append(it)
        payload = {
            "meta": {
                "schema": "tasks.v1",
                "note": ("待 AI 补全的教学内容队列。脚本只搭骨架，教学判断留给 AI。\n"
                         "  AI 补全规则：填回对应 HTML 的 ⚠️ 待补 区块 + 更新 data/ 里的 JSON 字段，\n"
                         "  然后把本文件里对应条目 status 改为 done。\n"
                         "  禁止：编造分值、编造孩子作答、编造官方答案。"),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "pending": len([i for i in merged if i.get("status") != "done"]),
            },
            "items": merged,
        }
        with open(TASKS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")


# ══════════════════════════════════════════════════════════════════
#  共享页面骨架
# ══════════════════════════════════════════════════════════════════

def page(title, kicker, tags, body, js_key, js_body):
    css = ""
    if os.path.exists(CSS_PATH):
        with open(CSS_PATH, encoding="utf-8") as f:
            css = f.read()
    tags_html = "".join(
        f'<span class="tag{" hot" if t.get("hot") else ""}">{esc(t["text"])}</span>'
        for t in tags
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="{GENERATOR_MARK}">
<title>{esc(title)}</title>
<style>{css}</style>
</head>
<body>

<div class="prog">
  <div class="prog-in">
    <span id="ptxt">还没开始</span>
    <span class="bar"><i id="pbar"></i></span>
    <button class="reset" id="rst">重置</button>
  </div>
</div>

<div class="wrap">

<header>
  <div class="kicker">{esc(kicker)}</div>
  <h1>{esc(title)}</h1>
  <div class="meta">{tags_html}</div>
</header>

{body}

</div>

<script>
(function(){{
  var KEY='msb.{js_key}.progress.v1';
  var steps=Array.prototype.slice.call(document.querySelectorAll('.step'));
  var bar=document.getElementById('pbar');
  var ptxt=document.getElementById('ptxt');
  var opened={{}};

  try{{opened=JSON.parse(localStorage.getItem(KEY)||'{{}}')||{{}};}}catch(e){{opened={{}};}}

  function save(){{try{{localStorage.setItem(KEY,JSON.stringify(opened));}}catch(e){{}}}}

  function render(){{
    var n=0;
    steps.forEach(function(s,i){{
      var id=s.dataset.s, isOpen=!!opened[id];
      s.classList.toggle('open',isOpen);
      var prevOpen = i===0 ? true : !!opened[steps[i-1].dataset.s];
      s.classList.toggle('locked',!prevOpen);
      s.classList.toggle('done',isOpen);
      if(isOpen)n++;
    }});
    var pct=steps.length? Math.round(n/steps.length*100) : 0;
    bar.style.width=pct+'%';
    ptxt.textContent = n===0 ? '还没开始' : '{js_key} '+n+' / '+steps.length;
  }}

  steps.forEach(function(s,i){{
    s.querySelector('.step-h').addEventListener('click',function(){{
      var id=s.dataset.s;
      opened[id]=!opened[id];
      if(opened[id]){{for(var j=0;j<i;j++)opened[steps[j].dataset.s]=true;}}
      save();render();
    }});
  }});

  var rst=document.getElementById('rst');
  if(rst) rst.addEventListener('click',function(){{
    if(!confirm('清空这一页的阅读进度？答案和讲解不受影响。'))return;
    opened={{}};save();render();
  }});

  document.addEventListener('click',function(e){{
    var btn=e.target.closest('[data-chk]');
    if(!btn)return;
    var id=btn.dataset.chk, ans=(btn.dataset.a||'').trim();
    var inp=document.getElementById(id);
    if(!inp)return;
    var val=(inp.value||'').trim();
    var fb=document.getElementById('f'+id.slice(1));
    if(!fb)return;
    if(!val){{fb.className='fb show no';fb.textContent='先写点什么再对答案。';return;}}
    var keys=ans.split(/\\s*\\/\\s*/).filter(Boolean);
    var hit=keys.some(function(k){{return val.indexOf(k)>-1;}});
    if(hit){{fb.className='fb show ok';fb.textContent='✓ 对了。';}}
    else{{fb.className='fb show no';fb.textContent='再想想。参考答案：'+ans;}}
  }});

  document.addEventListener('keydown',function(e){{
    if(e.key!=='Enter')return;
    var t=e.target;
    if(t&&t.type==='text'){{
      var wrap=t.closest('.qa');
      if(wrap){{var b=wrap.querySelector('[data-chk]');if(b)b.click();}}
    }}
  }});

  render();
}})();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════
#  错题精讲页骨架
# ══════════════════════════════════════════════════════════════════

# 五类病因 → 讲解页措辞。分类口径与 docs/方法论/错因分类与复习排期.md 一致。
CAUSE_TONE = {
    "概念不清": "救法是回教材把定义重读一遍，不是刷题",
    "方法没想到": "救法是把题型归类，把该做的固定动作练成本能",
    "计算失误": "救法是限时练正确率，不是重学知识",
    "审题错误": "救法是建立答案回读动作，不是重做知识点",
    "表达不规范": "救法是照着评分标准逐项自查，不是多做难题",
}


def todo_block(what, why):
    """待补区块：明确标出缺什么、为什么不能由脚本代劳。"""
    return (f'<div class="note" style="{TODO}">'
            f'<b>⚠️ 待补 · {esc(what)}</b><br>'
            f'<span style="font-size:14px">{esc(why)}</span></div>')


def score_cell(v, pending_note=None):
    if v is None:
        return '<span style="color:var(--ink3)">待补</span>'
    return f"<b>{esc(v)}</b>"


GENERATOR_MARK = "generated-by: tools/build_skeletons.py"


def qno_of(item, idx_fallback="1"):
    """取题号。

    优先用显式 qno 字段；没有则从题干里抠「第19题」/「q19」/「(19)」；
    再没有才退回条目序号（并注明这是序号不是题号）。
    为什么要认真取：id 后缀 w-20261002-01 是「第几条记录」，
    不是「第几题」。直接拿它当题号会生成 q1-二次函数-精讲.html，
    而孩子手上的卷子写的是 q19 —— 对不上就没法用。
    """
    v = item.get("qno")
    if v not in (None, ""):
        return str(v).lstrip("0") or "1", False
    title = str(item.get("title", ""))
    for pat in (r"第\s*(\d+)\s*题", r"\bq\s*(\d+)\b", r"（(\d+)）", r"\((\d+)\)"):
        m = re.search(pat, title)
        if m:
            return m.group(1).lstrip("0") or "1", False
    return str(idx_fallback), True


def slug_for(qno, title, module):
    """生成文件名：q<题号>-<主题>-精讲.html

    主题优先用模块名（如 二次函数 / 相似三角形）——短、稳定、可读。
    没有模块时退回题干首句，但必须清洗标点，否则会生成
    「q2-在△ABC中，点E、D、F分别在边AB、BC、A-精讲.html」这种废名。
    """
    if module:
        topic = module
    else:
        topic = re.sub(r"[\s（(].*$", "", str(title or ""))  # 去掉括号补充
        topic = re.sub(r"[，。；：、？！,.;:?!\"'“”‘’…—－\-+*/=<>|\\\[\]{}#@$%^&~`]+", " ", topic)
        topic = "-".join(topic.split()[:6])
    topic = re.sub(r'[\\/:*?"<>|]+', "", topic).strip("- ") or "错题"
    return f"q{qno}-{topic}-精讲.html"


def norm(s):
    """归一化文本用于比对：去掉所有空白与常见等价符号。"""
    if not s:
        return ""
    s = str(s)
    for a, b in (("−", "-"), ("–", "-"), ("—", "-"), (" ", ""), ("　", ""),
                 ("\n", ""), ("\t", ""), ("×", "*"), ("÷", "/")):
        s = s.replace(a, b)
    return s.strip()


def find_manual_page(out_dir, item, qno):
    """学科目录下是否已有这道题的人工精讲页。

    两条判定依据（任一命中即认为是同一道题）：
      1. 文件名以 q<题号>- 开头 —— 题号对得上
      2. 页面正文里出现本条错题的正确答案或孩子原答案 —— 内容对得上

    带生成器标记的一律不算——那是骨架，可以重生成。
    人工成果优先级高于自动骨架，**绝不能被覆盖**：
    q19 那页是逐字对照手写过程改出来的，自动生成器只能让路。
    """
    if not os.path.isdir(out_dir):
        return None
    keys = [norm(item.get("answerKey")), norm(item.get("childAnswer")),
            norm(item.get("myAnswer"))]
    keys = [k for k in keys if len(k) >= 4]
    for fn in sorted(os.listdir(out_dir)):
        if not (fn.endswith("-精讲.html") or fn.endswith(".html")):
            continue
        p = os.path.join(out_dir, fn)
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if GENERATOR_MARK in text[:4000]:
            continue  # 骨架，可重生成
        if fn.startswith(f"q{qno}-"):
            return p
        body = norm(text)
        if keys and any(k in body for k in keys):
            return p
    return None


# 限定词高亮清单——这些是丢分重灾区，孩子最爱漏。
# 与 wrong-question-coaching 技能里的清单保持一致，并按实测补充。
#
# 补充说明：光标「互为/至少」这类词不够。像「AF:FC 的值是」这种
# 「求什么」的表述才是真正的陷阱——孩子算对了比值却把顺序写反，
# 而这类表述一个限定词都没有。所以必须加上问答焦点词。
KEY_WORDS = [
    # 逻辑/性质类
    "互为", "相反", "相似", "全等", "相切", "垂直", "平分",
    # 判断类
    "不正确的是", "不是", "不正确", "错误的是",
    # 程度类
    "至少", "至多", "恰好", "不超过", "不小于", "不高于", "不少于",
    "最大", "最小", "最长", "最短",
    # 范围类
    "全部", "任取", "任意",
    # 几何位置类
    "的中点", "外角", "内心", "切线", "延长线",
    # 问答焦点类：问的是什么，决定答案怎么写（顺序错就全错）
    "的值是", "是多少", "的值", "为多少",
]


def hl_keywords(text):
    """把限定词与问答焦点包进 <span class="hl">。

    实现要点：不能「转义 → 反复 replace」——前一次替换生成的
    <span class="hl"> 里含有「值」「的」这类字符片段，会被后一轮二次包裹，
    产出 <span class="hl">的<span class="hl">值</span>是</span> 这种烂标签。

    正确做法是**单趟扫描**：先用正则把所有命中词一次找出来，
    按位置切分成「普通文本 / 命中词」交替的序列，
    普通文本原样（已转义），命中词包一层 span。只走一遍，天然不会自我嵌套。
    """
    plain = esc(text)
    if not plain:
        return plain
    # 最长匹配优先：先试长的词，避免「的值」把「的值是」切碎
    pattern = "|".join(re.escape(w) for w in
                       sorted(KEY_WORDS, key=len, reverse=True))
    out = []
    pos = 0
    for m in re.finditer(pattern, plain):
        out.append(plain[pos:m.start()])
        out.append(f'<span class="hl">{m.group(0)}</span>')
        pos = m.end()
    out.append(plain[pos:])
    return "".join(out)


def build_stem_html(item, qno):
    """组装题干区。

    优先用 stemHtml（人工逐字转录版，最权威）；
    其次用 stem（纯文本，自动加限定词高亮）；
    再次退回 title（自动剥掉「第N题」前缀，避免和页头题号重复）；
    都没有才报待补。
    """
    if item.get("stemHtml"):
        stem = item["stemHtml"]
    else:
        raw = item.get("stem") or item.get("title") or ""
        if raw:
            # 剥掉「第5题」「q19.」这类前缀——页头已经写了题号，重复了看着乱
            raw = re.sub(r"^\s*(第\s*\d+\s*题|q\d+\s*[.、．]?)\s*", "", str(raw))
            stem = hl_keywords(raw)
        else:
            return ('<span style="color:var(--ink3)">'
                    '（题干待补：请从原卷照片逐字转录到 data/wrong JSON 的 <code>stem</code> 字段）'
                    '</span>')

    # 选择题自动列出选项——选项在错题里是独立字段，不拼进来题干就残缺
    opts = item.get("options")
    if isinstance(opts, dict) and opts:
        keys = sorted(opts.keys())
        cells = "　　".join(
            f"<b>{esc(k)}.</b> {esc(opts[k])}" for k in keys)
        stem = f"{stem}\n<div style=\"margin-top:12px;padding-top:12px;"
        stem += "border-top:1px dashed var(--line);font-size:15px\">"
        stem += f"{cells}</div>"
    return stem


def build_wrong_page(item, out_path, manifest, qno=None):
    """生成一道错题的精讲页骨架。out_path 必传——用于计算图片相对路径。"""
    wid = item.get("id", "")
    subject = item.get("subject", "数学")
    title_text = item.get("title", "（未命名错题）")
    module = item.get("module", "")
    cause = item.get("cause", "")
    # 题号：由调用方传入（或从 id/标题推断），保证与卷面一致
    qno = qno or qno_of(item)[0]
    short = (re.sub(r"^\s*(第\s*\d+\s*题|q\d+\s*[.、．]?)\s*", "", title_text)
             or "错题")[:22].strip(" ，,。.")

    # 原图引用：sourceRaw 指向 RAW/，从输出目录算相对路径
    src = item.get("sourceRaw")
    imgs = []
    if src:
        abs_src = os.path.join(ROOT, src)
        if os.path.exists(abs_src):
            imgs.append((rel(out_path, abs_src), "原卷照片（原件层，含批改与手写）"))
        else:
            imgs.append((rel(out_path, abs_src), "⚠️ 原件缺失：" + src))

    # 是否已有 AI 补全过的完整页面
    ref = item.get("refinedPage")
    if ref and os.path.exists(os.path.join(ROOT, ref)):
        q.skip = f"已有精讲页 {ref}，跳过骨架生成（补全版优先）"
        return None

    stem = build_stem_html(item, qno)

    cause_tone = CAUSE_TONE.get(cause, "")
    full = item.get("full")
    lost = item.get("lost")
    pending_score = item.get("scorePending", False) or (full is None or lost is None)

    # ── ① 原题 ──
    scan_html = ""
    if imgs:
        rows = "".join(
            f'<p class="cap">{esc(cap)}</p>\n      <img class="scan" src="{esc(p)}" alt="{esc(cap)}">'
            for p, cap in imgs
        )
        scan_html = f"""  <details>
    <summary>看原卷照片（原件层，含批改与手写）</summary>
    <div class="dbody">
      {rows}
      <p class="hint">原件在 <code>RAW/</code>，已入库 Git，是这份讲解的事实依据。</p>
    </div>
  </details>"""
    else:
        scan_html = todo_block(
            "原卷照片", "错题记录缺少 sourceRaw，或原件文件不存在。"
                        "补录时应把 photos/ 下的原图放进 RAW/错题照片/ 并跑 ingest-raw.sh")

    breakdown = item.get("breakdown")
    if breakdown:
        rows = "".join(
            f'<tr><td>{esc(b[0])}</td><td>{b[1]}</td><td>{esc(b.get("note", "") if isinstance(b, dict) else "")}</td></tr>'
            for b in breakdown)
        bd_html = f"""  <h3>题干拆成条件</h3>
  <table>
    <thead><tr><th style="width:32%">题干说的</th><th>翻译过来</th><th style="width:26%">备注</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>"""
    else:
        bd_html = todo_block(
            "题干拆解表", "拆解是逐句翻译题干的机械工作，但要判断「哪句是陷阱」需要看原卷，"
                        "由 AI 读照片产出。JSON 里补 breakdown 字段即可自动填入。")

    orig_html = f"""<div class="card orig">
  <h2><span class="n" style="background:var(--ink)">原</span>原题</h2>
  <p class="sub">先自己读一遍，再往下看。题干里的每一句话后面都有用。</p>

  <div class="stmt">
    {stem}
  </div>

{scan_html}

{bd_html}
</div>"""

    # ── 开场：已做对 / 断点 ──
    done_right = item.get("doneRight")
    if done_right:
        rows = "".join(
            f'<tr><td>{esc(s)}</td><td class="tick">✓</td><td>{esc(n)}</td></tr>'
            for s, n in done_right)
        right_html = f"""<div class="card">
  <h2><span class="n">1</span>你已经做对的部分</h2>
  <p class="sub">先把这部分看清楚——这不是安慰，是事实。</p>
  <table>
    <thead><tr><th style="width:46%">你写的</th><th style="width:10%">对不对</th><th>说明</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</div>"""
    else:
        right_html = f"""<div class="card">
  <h2><span class="n">1</span>你已经做对的部分</h2>
  {todo_block("已做对的部分逐项对照", "需要读他手写的中间步骤才能填，不能只看最终答案。"
                                      "JSON 里补 doneRight 字段：[步骤描述, 说明] 数组。")}
</div>"""

    correct = item.get("answerKey")
    child = item.get("childAnswer") or item.get("myAnswer")
    if correct and child:
        cmp_html = f"""<div class="card">
  <h2><span class="n">2</span>断点到底在哪</h2>
  <p class="sub">把两个答案摆一起看。</p>
  <div style="display:grid;grid-template-columns:1fr;gap:12px;margin:18px 0">
    <div class="box" style="background:var(--red-soft);border-color:var(--red-line)">
      <div style="font-size:12.5px;color:var(--red);font-weight:600;margin-bottom:7px">你写的</div>
      <div style="font:16px/1.6 ui-monospace,Menlo,monospace;color:var(--red);font-weight:600">{esc(child)}</div>
    </div>
    <div class="box box-ans">
      <div style="font-size:12.5px;color:var(--green);font-weight:600;margin-bottom:7px">正确的</div>
      <div style="font:16px/1.6 ui-monospace,Menlo,monospace;color:var(--green);font-weight:600">{esc(correct)}</div>
    </div>
</div>
</div>"""
    else:
        cmp_html = f"""<div class="card">
  <h2><span class="n">2</span>断点到底在哪</h2>
  {todo_block("断点对照", "需要把错误答案的值是怎么来的讲清楚——这是整页最不能由脚本代劳的部分。")}
</div>"""

    # ── ③ 零跳跃分步讲解（渐进解锁）──
    steps_data = item.get("steps")
    if steps_data:
        step_html = []
        for i, s in enumerate(steps_data):
            act = s.get("action", "")
            why = s.get("basis", "")
            chk = s.get("check", "")
            mark = ' <span class="tag hot">★ 断点</span>' if s.get("breakpoint") else ""
            step_html.append(f"""  <div class="step" data-s="{i + 1}">
    <div class="step-h"><span class="step-n">{i + 1}</span><span class="step-t">{esc(act)}</span></div>
    <div class="step-b">
      <div class="step-a"><b>动作</b> {esc(act)}</div>
      <div class="step-b"><b>依据</b> {esc(why)}</div>
      <div class="step-b"><b>检验</b> {esc(chk)}</div>{mark}
    </div>
  </div>""")
        steps_block = "\n".join(step_html)
    else:
        steps_block = todo_block(
            "零跳跃分步讲解", "这是整页的重心，必须一步一个动作、每步说清依据和检验，"
                            "并标出他的断点在哪一步。需要读原卷手写过程才能拆，脚本做不了。")

    explain_html = f"""<div class="card">
  <h2><span class="n">3</span>零跳跃分步讲解</h2>
  <p class="sub">一步一个动作，做完上一步再点下一步。不做完不许跳。</p>
{steps_block}
</div>"""

    # ── ④ 三题变式 ──
    variants = item.get("variants")
    if variants:
        vhtml = []
        for i, v in enumerate(variants):
            vtype = v.get("type", "变式")
            vhtml.append(f"""  <div class="card" style="margin-bottom:12px">
    <h3>{i + 1}. {esc(vtype)}</h3>
    <p class="sub">{esc(v.get("change", ""))}</p>
    <div class="stmt" style="margin-bottom:12px">{esc(v.get("stem", ""))}</p>
    <details>
      <summary>看答案与思路</summary>
      <div class="dbody">
        <div class="note blue"><b>思路</b> {esc(v.get("hint", ""))}</div>
        <div class="note green"><b>答案</b> {esc(v.get("answer", ""))}</div>
      </div>
    </details>
  </div>""")
        variants_block = f"""<div class="card orig">
  <h2><span class="n" style="background:var(--ink)">练</span>三题变式</h2>
  <p class="sub">练一题，看三题。答案都折起来了，做完再打开。</p>
{vhtml}
  </div>"""
    else:
        variants_block = f"""<div class="card orig">
  <h2><span class="n" style="background:var(--ink)">练</span>三题变式</h2>
  {todo_block("三道变式题", "固定配比：简单变式（改一个数值）+ 同类型（换考点同手法）+ "
                          "问答对调（已知与所求互换）。第三题不能省——很多孩子是「记住套路」不是「真懂」。")}
</div>"""

    # ── ⑤ 迁移想五题 ──
    think_qs = item.get("thinkQuestions")
    if think_qs:
        qa_rows = ""
        for i, tq in enumerate(think_qs):
            fid = f"q{i + 1}"
            ans = tq.get("answer", "")
            qa_rows += f"""    <div class="qa">
      <div class="stmt" style="font-size:15px;padding:13px 15px;margin-bottom:9px">{i + 1}. {esc(tq.get("q", ""))}</div>
      <div class="inrow">
        <input id="{fid}" type="text" placeholder="用自己的话说，不要抄题目">
        <button class="btn" data-chk="{fid}" data-a="{esc(ans)}">检查</button>
      </div>
      <div class="fb" id="f{fid}"></div>
    </div>"""
        think_block = f"""<div class="card">
  <h2><span class="n">4</span>迁移想五题</h2>
  <p class="sub">从「做得出」到「想得出」。这一段练的是审题与建模，不是计算。</p>
  {qa_rows}
</div>"""
    else:
        think_block = f"""<div class="card">
  <h2><span class="n">4</span>迁移想五题</h2>
  {todo_block("3-5 个递进问题 + 即时判题", "最后一个问题必须是「什么情况下这个方法会失效」，"
          "练的是方法的适用边界。JSON 里补 thinkQuestions 字段。")}
</div>"""

    # ── 家长区 ──
    parent_html = f"""<div class="parent">
  <div class="kicker" style="color:#a1a1aa">写给家长</div>
  <h2 style="color:#fff">这一页怎么用</h2>
  <p style="font-size:14.5px;color:#d4d4d8;line-height:1.8">
    这一页写给<b>{esc(subject)}</b>，预计 <b>25-35 分钟</b>，不要一次做完。<br>
    错因判定为<b>「{esc(cause or '待补')}」</b>。{esc(cause_tone)}
  </p>
  <div style="display:grid;grid-template-columns:1fr;gap:9px;margin:16px 0">
    <div style="background:rgba(255,255,255,.07);border-radius:11px;padding:12px 14px">
      <div style="font-size:13.5px;color:#f59e0b;font-weight:600;margin-bottom:4px">先做（5 分钟）</div>
      <div style="font-size:14px;color:#d4d4d8">合上页面重做原题，盯第 ① 段拆解表，验证他能不能自己列出全部条件。</div>
    </div>
    <div style="background:rgba(255,255,255,.07);border-radius:11px;padding:12px 14px">
      <div style="font-size:13.5px;color:#f59e0b;font-weight:600;margin-bottom:4px">再做（15 分钟）</div>
      <div style="font-size:14px;color:#d4d4d8">按 ③ 段逐步展开，跟着他确认每一步的依据。他卡住就退回上一步，不要直接给答案。</div>
    </div>
    <div style="background:rgba(255,255,255,.07);border-radius:11px;padding:12px 14px">
      <div style="font-size:13.5px;color:#f59e0b;font-weight:600;margin-bottom:4px">最后（5 分钟）</div>
      <div style="font-size:14px;color:#d4d4d8">让他讲出第 ⑤ 段的第 1 题和最后一题。说不出来 = 只是「看懂」不是「会用」。</div>
    </div>
  </div>
  <p style="font-size:14px;color:#d4d4d8;line-height:1.8">
    <b style="color:#f59e0b">别做的事</b>：别让他抄完整解答。这一题的价值在思维转换，抄一遍等于没做。
    <br><b style="color:#f59e0b">关于诊断</b>：错因「{esc(cause or '待补')}」是回看中间步骤判出来的，不是只看答案。
  </p>
</div>"""

    # ── 标签页 ──
    tags = [
        {"text": subject},
        {"text": module or "模块待补"},
        {"text": f"错因：{cause}" if cause else "错因待补", "hot": True},
    ]
    if full is not None:
        tags.append({"text": f"失分 {lost} 分"})
    else:
        tags.append({"text": "分值待补", "hot": True})

    body = (orig_html + open_note(cause) + right_html + cmp_html
            + explain_html + variants_block + think_block + parent_html)

    return page(
        title=f"第{qno}题 · {module or short} · 精讲",
        kicker=f"{subject} · {module or '模块待补'} · 错题精讲",
        tags=tags,
        body=body,
        js_key=f"q{qno}",
        js_body="",
    )


def open_note(cause):
    if cause == "方法没想到":
        return """<div class="card">
  <div class="note gold">
    <b>先说结论：你不是不会，是没想到要这么做。</b><br>
    这一页要解决的不是「学会这道题」，而是「下次碰到这类题，脑子会自动启动同一个动作」。
  </div>
</div>
"""
    if cause == "审题错误":
        return """<div class="card">
  <div class="note gold">
    <b>先说结论：知识你会，错在读题。</b><br>
    这一页的重点不是重学知识点，而是把「答案回读」这个动作练成本能。
  </div>
</div>
"""
    if cause == "计算失误":
        return """<div class="card">
  <div class="note gold">
    <b>先说结论：方法你都会，错在手上。</b><br>
    这一页的重点不是重学知识，而是限时训练正确率，把正确率变成习惯。
  </div>
</div>
"""
    return """<div class="card">
  <div class="note gold">
    <b>先说结论：先看断点在哪，再决定怎么练。</b><br>
    这一页要解决的是断点本身，而不是刷同类题。
  </div>
</div>
"""


# ══════════════════════════════════════════════════════════════════
#  试卷拆解报告
# ══════════════════════════════════════════════════════════════════

def build_exam_page(exam, out_path, q):
    eid = exam.get("id", "")
    subject = exam.get("subject", "数学")
    name = exam.get("name", "试卷")
    date = exam.get("date", "")
    score = exam.get("score", {}) or {}

    got = score.get("got")
    full = score.get("full")
    # 分值纪律：没确认就是 null，绝不估算
    unconfirmed = exam.get("scoreConfirmed") is not True
    if unconfirmed:
        score_line = "⚠️ 分值待家长确认（未确认前不计入失分热区统计）"
        score_tag = {"text": "分值未确认", "hot": True}
    else:
        score_line = f"得分 {got} / {full}"
        score_tag = {"text": f"{got}/{full} 分"}

    # ── 总览卡：得分结构 ──
    modules = exam.get("modules", []) or []
    if modules:
        rows = []
        for m in modules:
            mf = m.get("full")
            mg = m.get("got")
            if m.get("scoreConfirmed") is False or mf in (None, 0) or mg is None:
                rate = '<span style="color:var(--ink3)">待确认</span>'
                row_cls = ""
            else:
                pct = round(mg / mf * 100)
                rate = f"<b>{pct}%</b>（{mg}/{mf}）"
                if pct < 60:
                    row_cls = ' style="background:#fdf0ee"'
            rows.append(
                f'<tr{row_cls}><td><b>{esc(m.get("name", ""))}</b></td>'
                f'<td>{score_cell(mf)}</td><td>{score_cell(mg)}</td><td>{rate}</td></tr>')
        mod_table = f"""  <table>
    <thead><tr><th>模块</th><th style="width:14%">满分</th><th style="width:14%">实得</th><th style="width:26%">得分率</th></tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>"""
    else:
        mod_table = todo_block("模块得分结构", "逐模块的满分与实得必须来自卷面，不能估算。"
                                 "JSON 里补 modules 数组。")

    # ── 失分热区 ──
    wrongs = exam.get("wrongs", []) or []
    if wrongs:
        wrows = []
        for w in wrongs:
            wf = w.get("full")
            wl = w.get("lost")
            wcause = w.get("cause", "")
            cause_chip = f'<span class="tag">{esc(wcause)}</span>' if wcause else '<span style="color:var(--ink3)">待判</span>'
            lost_disp = f"<b>{esc(wl)}</b>" if wl is not None else '<span style="color:var(--ink3)">待补</span>'
            wrows.append(
                f'<tr><td>第{esc(w.get("no", ""))}题</td>'
                f'<td>{esc(w.get("module", ""))}</td>'
                f'<td>{score_cell(wf)}</td>'
                f'<td>{lost_disp}</td>'
                f'<td>{cause_chip}</td></tr>')
        wrong_table = f"""  <table>
    <thead><tr><th style="width:12%">题号</th><th>模块</th><th style="width:12%">满分</th>
    <th style="width:12%">实失</th><th style="width:22%">错因</th></tr></thead>
    <tbody>
      {''.join(wrows)}
    </tbody>
  </table>"""
        # 病因聚合：按病因数错题数量分布（分值未确认时只能数题数）
        cause_count = {}
        for w in wrongs:
            c = w.get("cause", "") or "未判定"
            cause_count[c] = cause_count.get(c, 0) + 1
        cause_rows = "".join(
            f'<tr><td>{esc(c)}</td><td>{n} 题</td></tr>'
            for c, n in sorted(cause_count.items(), key=lambda x: -x[1]))
        cause_table = f"""  <h3>错因分布</h3>
  <table>
    <thead><tr><th>错因</th><th style="width:22%">数量</th></tr></thead>
    <tbody>
      {cause_rows}
    </tbody>
  </table>"""
    else:
        wrong_table = todo_block("逐题失分清单", "逐题判分是卷面分析的核心，必须逐题看卷。"
                                "JSON 里补 wrongs 数组，每条记题号、模块、full、lost、cause。")
        cause_table = ""

    verdict_html = exam.get("verdictHtml")
    if verdict_html:
        verdict = f"""  <div class="note blue">
{verdict_html}
  </div>"""
    else:
        verdict = todo_block("诊断结论", "「主要失分在哪、为什么、下一步先补什么」——"
                                     "这是全篇最有价值的判断，只能由 AI 或老师做，脚本做不了。")

    body = f"""<div class="card orig">
  <h2><span class="n" style="background:var(--ink)">卷</span>本次考试</h2>
  <p class="sub">{esc(date)} · {esc(name)}</p>
  <div class="stmt" style="font-size:14.5px">{esc(score_line)}</div>
</div>

<div class="card">
  <h2><span class="n">1</span>各模块得分情况</h2>
  <p class="sub">先看结构，再看细节。哪一块塌得最狠，先补哪一块。</p>
{mod_table}
</div>

<div class="card">
  <h2><span class="n">2</span>失分在哪</h2>
  <p class="sub">逐题判分，把「考了多少分」变成「不会什么」。</p>
{wrong_table}
{cause_table}
</div>

<div class="card">
  <h2><span class="n">3</span>诊断结论</h2>
{verdict}
</div>

{parent_note(exam)}

</div>"""

    # 标签
    tags = [{"text": subject}, {"text": name or "试卷"}]
    if date:
        tags.append({"text": date})
    tags.append(score_tag)

    return page(
        title=f"{date} {name} · 试卷拆解",
        kicker=f"{subject} · 试卷拆解 · 失分结构",
        tags=tags,
        body=body,
        js_key=f"exam-{eid or date}",
        js_body="",
    )


def parent_note(exam):
    s = exam.get("score", {}) or {}
    got, full = s.get("got"), s.get("full")
    if got is None or full is None:
        return ""
    rate = round(got / full * 100)
    nwrong = len(exam.get("wrongs", []) or [])
    return f"""<div class="parent">
  <div class="kicker" style="color:#a1a1aa">写给家长</div>
  <h2 style="color:#fff">接下来怎么做</h2>
  <p style="font-size:14.5px;color:#d4d4d8;line-height:1.8">
    本次 {got}/{full} 分，得分率 {rate}%。下面第 2 段列出的 {nwrong} 道失分题，
    就是接下来两周唯一要攻克的内容——不要横向加新题，先把这些吃透。
  </p>
  <div style="display:grid;grid-template-columns:1fr;gap:9px;margin:16px 0">
    <div style="background:rgba(255,255,255,.07);border-radius:11px;padding:12px 14px">
      <div style="font-size:13.5px;color:#f59e0b;font-weight:600;margin-bottom:4px">每天 10 分钟</div>
      <div style="font-size:14px;color:#d4d4d8">关掉本页，把今天该做的 1-2 道失分题重做一遍。卡住就回来对照第 2 段，不要直接抄答案。</div>
    </div>
    <div style="background:rgba(255,255,255,.07);border-radius:11px;padding:12px 14px">
      <div style="font-size:13.5px;color:#f59e0b;font-weight:600;margin-bottom:4px">周末 30 分钟</div>
      <div style="font-size:14px;color:#d4d4d8">做一次混合限时练习，按模块出题不按题型出题。目的是模拟考试的取舍，不是刷题量。</div>
    </div>
  </div>
  <p style="font-size:14px;color:#d4d4d8;line-height:1.8">
    <b style="color:#f59e0b">别做的事</b>：别看到某块低分就立刻买新资料。失分结构不清楚之前，加题只会把同样的洞挖得更深。
  </p>
</div>
"""


# ══════════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════════

def iter_json(folder, schema):
    d = os.path.join(ROOT, "data", folder)
    if not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        data = load_json(os.path.join(d, fn))
        if not data:
            continue
        if data.get("meta", {}).get("schema") != schema:
            continue
        yield os.path.join(d, fn), data


def write_if_changed(path, content, dry_run, q):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == content:
                return False
    if dry_run:
        q.changed.append(os.path.relpath(path, ROOT))
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return True


def main():
    ap = argparse.ArgumentParser(
        description="从 JSON 生成讲义骨架（单文件 HTML）",
        epilog="脚本只搭骨架，教学判断留 ⚠️ 待补 并登记到 data/tasks/pending.json。",
    )
    ap.add_argument("--dry-run", action="store_true", help="只列将要生成什么，不写盘")
    args = ap.parse_args()

    q = type("Q", (), {"skip": None, "changed": []})()
    tq = TaskQueue()
    css_ok = os.path.exists(CSS_PATH)
    if not css_ok:
        print(f"⚠️ 找不到样式资产 {CSS_PATH}，生成的页面会没有样式", file=sys.stderr)

    made = unchanged = skipped = 0

    # ── 错题精讲页 ──
    for path, data in iter_json("wrong", "wrong.v1"):
        for idx, item in enumerate(data.get("items", []), start=1):
            if item.get("status") in ("done", "mastered"):
                skipped += 1
                print(f"  · 跳过（已掌握）：{item.get('id')}")
                continue

            subject = item.get("subject", "数学")
            wid = item.get("id", "x")
            qno, guessed = qno_of(item, str(idx))
            title = item.get("title", "")
            module = item.get("module", "")
            out_dir = os.path.join(ROOT, "docs", "实战表", subject)
            fn = slug_for(qno, title, module)

            # 保护：已有这道题的人工成果页则跳过，绝不覆盖
            manual = find_manual_page(out_dir, item, qno)
            if manual and os.path.basename(manual) != fn:
                skipped += 1
                print(f"  · 跳过（已有人工精讲页）：{os.path.relpath(manual, ROOT)}")
                # 反向登记：让 AI 知道要把内容补进已有页，而不是新开一页
                tq.add(f"wrong-{wid}", "wrong-refine", os.path.relpath(manual, ROOT),
                       subject, module,
                       "教学判断：把该错题的讲解补进已有人工精讲页（保留人工内容，只补空缺段）",
                       ["steps", "variants", "thinkQuestions"])
                continue

            out_path = os.path.join(out_dir, fn)
            html_out = build_wrong_page(item, out_path, None, qno)
            if html_out is None:
                skipped += 1
                print(f"  · 跳过（已补全）：{wid}")
                continue

            changed = write_if_changed(out_path, html_out, args.dry_run, q)
            tq.add(f"wrong-{wid}", "wrong", os.path.relpath(out_path, ROOT), subject,
                   module,
                   "教学判断：零跳跃讲解步骤、断点分析、三题变式、想五题",
                   ["stem", "breakdown", "doneRight", "steps", "variants", "thinkQuestions"])
            rel_out = os.path.relpath(out_path, ROOT)
            flag = "（题号取自条目序号，非卷面题号，建议补 qno 字段）" if guessed else ""
            if changed:
                print(f"  ✓ {rel_out}{flag}")
                made += 1
            else:
                print(f"  · 无变化 {rel_out}{flag}")
                unchanged += 1

    # ── 试卷拆解报告 ──
    for path, data in iter_json("exams", "exam.v1"):
        exams = data.get("exams") or data.get("items") or [data]
        if isinstance(exams, dict):
            exams = [exams]
        for ex in exams:
            if not isinstance(ex, dict):
                continue
            if ex.get("status") in ("done", "mastered"):
                skipped += 1
                print(f"  · 跳过（已归档）：{ex.get('id')}")
                continue
            subject = ex.get("subject", "数学")
            date = ex.get("date", "")
            name = ex.get("name", "试卷")
            slug = re.sub(r'[\\/:*?"<>|\s]+', "-", name).strip("-")
            out_path = os.path.join(ROOT, "docs", "实战表", subject,
                                    f"{date}-{slug}-试卷拆解.html")
            html_out = build_exam_page(ex, out_path, q)
            changed = write_if_changed(out_path, html_out, args.dry_run, q)
            tq.add(f"exam-{ex.get('id', date)}", "exam", os.path.relpath(out_path, ROOT),
                   subject, "试卷",
                   "教学判断：逐题判分、模块得分率、失分热区诊断结论",
                   ["modules", "wrongs", "verdictHtml", "score"])
            rel_out = os.path.relpath(out_path, ROOT)
            if changed:
                print(f"  ✓ {rel_out}")
                made += 1
            else:
                print(f"  · 无变化 {rel_out}")
                unchanged += 1

    tq.write(args.dry_run)

    print("")
    print("─" * 30)
    if args.dry_run:
        print(f"dry-run：将写盘 {len(q.changed)} 个文件，本命令未写入任何文件")
        for c in q.changed:
            print(f"    {c}")
    else:
        print(f"生成/更新 {made} 个 · 无变化 {unchanged} 个 · 跳过 {skipped} 个")
        print(f"待补任务队列：{os.path.relpath(TASKS_PATH, ROOT)}")


if __name__ == "__main__":
    main()
