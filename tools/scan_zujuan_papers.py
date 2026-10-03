#!/usr/bin/env python3
"""scan_zujuan_papers.py — 从 zujuan.com 上海卷库批量发现试卷并入库

发现（2026-10-02）：**www.zujuan.com 才是规模来源**，21cnjy 单点只能捞到零散几十份。

    https://www.zujuan.com/paper/paper-exam-list?xd=2&chid={科目}&page={页}&province_id=9
    province_id=9 = 上海，每页 10 条，全科合计 94 套/科
    详情页 https://www.zujuan.com/paper/view-{paperId}.shtml

列表项 HTML 结构（2026-10-02 实测）：
    <a href='/paper/view-6688079.shtml' target='_blank' title='上海市2026年中考数学真题'>
年份、类型（中考真卷/中考模拟）在同一 <ul><li> 块内，可区分真卷与一模二模。

全文正文在详情页是 JS 渲染的，抓不到；但
    https://mzujuan.21cnjy.com/paper/preview/{paperId}
有纯文本题干且**带【知识点】标签**——错因归因的原料，比裸题干值钱。

产出：
    data/resources/shanghai-papers.json   试卷索引（含 paperId / zujuanUrl / 知识点正文状态）
    RAW/试卷库/全文/{id}.txt               zujuan 公开预览全文（含知识点标签）
    data/_local/zujuan-scan.json           分页游标，中断续扫
"""

import argparse
import gzip
import hashlib
import html as htmllib
import json
import os
import random
import re
import time
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "data", "resources", "shanghai-papers.json")
MANIFEST = os.path.join(ROOT, "data", "raw-manifest.json")
CACHE = os.path.join(ROOT, "data", "_local", "zujuan-scan.json")
RAW_DIR = os.path.join(ROOT, "RAW", "试卷库", "全文")

PROVINCE_ID = 9  # 上海
LIST = "https://www.zujuan.com/paper/paper-exam-list?xd=2&chid={chid}&page={page}&province_id={pid}"
VIEW = "https://www.zujuan.com/paper/view-{pid}.shtml"
LIST_REF = "https://www.zujuan.com/paper/paper-exam-list?xd=2&chid={chid}&page=1&province_id={prov}"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 中考 9 科（小学 chid=1 不入库：项目定位是初中）
SUBJECTS = {2: "语文", 3: "数学", 4: "英语", 6: "物理", 7: "化学",
            8: "历史", 9: "道德与法治", 10: "地理", 11: "生物学"}

DISTRICTS = ["黄浦", "徐汇", "长宁", "静安", "普陀", "虹口", "杨浦",
             "闵行", "宝山", "嘉定", "浦东新", "金山", "松江", "青浦",
             "奉贤", "崇明", "闸北"]


def fetch(url, timeout=25, retries=2, referer=None):
    """GET 并按 Content-Encoding 解压。

    踩过的坑（2026-10-02，浪费三轮）：**不声明 Accept-Encoding 时服务端仍会返回 gzip，
    urllib 不会自动解压**，抓到的全是二进制乱码。最初误判成「反爬占位页」，
    实际上 gzip 解压后就是完整原卷题干。必须显式声明 + 手动 decompress。
    """
    last = None
    for i in range(retries):
        try:
            h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                 "Accept-Encoding": "gzip, deflate",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Referer": referer or "https://www.zujuan.com/"}
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                data = gzip.decompress(data)
            elif "deflate" in enc:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            return data
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def decode(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def parse_list(page_html):
    """从列表页提取试卷条目。返回 [{paperId, title, year, kind, updated}]。"""
    out = []
    # 每个 <li> 是一套卷的完整信息块
    for block in re.findall(r"<li>(.*?)</li>", page_html, re.S):
        m = re.search(r"view-(\d+)\.shtml'[^>]*title='([^']*)'", block)
        if not m:
            continue
        pid, title = m.group(1), htmllib.unescape(m.group(2))
        year = re.search(r"年份：\s*(\d{4})", block)
        upd = re.search(r"更新：\s*([\d-]+)", block)
        kind = "中考模拟" if "模拟" in block else "中考真卷"
        out.append({"paperId": m.group(1), "title": title.strip(),
                    "year": int(year.group(1)) if year else None,
                    "kind": kind,
                    "updated": upd.group(1) if upd else None})
    return out


def classify(title):
    """从标题判定场次与区域。真题多为裸年份，模拟卷带区县。"""
    session = "中考"
    if "一模" in title:
        session = "一模"
    elif "二模" in title:
        session = "二模"
    elif "三模" in title or "三次" in title:
        session = "三模"
    region = "上海市"
    for d in DISTRICTS:
        if d in title:
            region = f"上海市{d}区"
            break
    return session, region


def extract_body(page_html):
    """从试卷详情页提取题干正文。

    坑（2026-10-02，浪费三轮）：直接把整页 strip 成纯文本，导航菜单
    （"二一教育/网站首页/帮助中心"）会混进正文，还让长度阈值误判通过，
    生成一堆标题对、正文空的假 RAW。现在三步净化：剥 head → 剥 script/style
    → 只留成句的行并去重。
    """
    b = re.sub(r"<head.*?</head>", " ", page_html, flags=re.S | re.I)
    b = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", b, flags=re.S | re.I)
    txt = htmllib.unescape(re.sub(r"<[^>]+>", "\n", b))
    lines, seen = [], set()
    for ln in txt.split("\n"):
        ln = ln.strip()
        if len(ln) < 12:# 太短的多是导航碎片
            continue
        if ln in seen:                # 导航在页首页尾各出现一次，去重
            continue
        if re.match(r"^[\s{};.#*\-_=|]+$", ln):
            continue
        seen.add(ln)
        lines.append(ln)
    return "\n".join(lines)


def looks_like_exam(body):
    """校验是否真是题干。返回 (是否通过, 原因)。"""
    # 反爬挑战页：网络环境检测中，并发过高/频率过高就返回这个，必须显式拦截
    for wall in ("正在检测当前网络环境", "访问安全", "请耐心等待",
                 "你已经进入异次元", "验证码", "滑动验证"):
        if wall in body:
            return False, "反爬挑战页"
    if len(body) < 400:
        return False, f"正文过短 {len(body)}"
    nav = sum(body.count(k) for k in ("网站首页", "帮助中心", "视频帮助",
                                      "VIP服务", "旗下产品", "二一教育"))
    if nav > 8:
        return False, f"疑似导航页（导航特征 {nav} 处）"
    sig = sum(body.count(k) for k in ("下列", "如图", "已知", "（　　）", "解答",
                                      "选择题", "填空", "解答题", "试题"))
    if sig < 3:
        return False, f"缺题干特征（信号 {sig} 处）"
    return True, ""


def fetch_text(paper_id, title, subject, chid):
    """抓试卷详情页全文（真题干，公式以文本/上下标呈现）。"""
    url = VIEW.format(pid=paper_id)
    try:
        raw = fetch(url, timeout=25, referer=LIST_REF.format(chid=chid, prov=PROVINCE_ID))
    except Exception as e:
        return None, f"详情页失败 {str(e)[:60]}"
    body = extract_body(decode(raw))
    ok, why = looks_like_exam(body)
    if not ok:
        return None, why
    return {"title": title, "subject": subject, "sourceUrl": url,
            "fetchedAt": datetime.now().strftime("%Y-%m-%d"),
            "questionSignals": sum(body.count(k) for k in ("下列", "如图", "已知")),
            "body": body}, None


def scan_list(chid, subject, pages, workers=5, cache={}):
    """并发拉取某科的全部分页。"""
    found = {}

    def one(pg):
        u = LIST.format(chid=chid, page=pg, pid=PROVINCE_ID)
        try:
            return pg, parse_list(decode(fetch(u, timeout=25)))
        except Exception as e:
            return pg, f"ERR {str(e)[:50]}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, pg): pg for pg in range(1, pages + 1)}
        for fu in as_completed(futs):
            pg = futs[fu]
            r = fu.result()
            if isinstance(r[1], str):
                print(f"    ✗ {subject} p{pg} {r[1]}", flush=True)
                continue
            for it in r[1]:
                found.setdefault(it["paperId"], it)
            print(f"    · {subject} p{pg} {len(r[1])} 条", flush=True)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=10, help="每科翻多少页（每页10条）")
    ap.add_argument("--subjects", default="", help="逗号分隔科目名，留空=全部9科")
    ap.add_argument("--list-only", action="store_true", help="只建索引，不抓全文")
    ap.add_argument("--text-workers", type=int, default=2,
                    help="详情页并发。默认2：zujuan 有反爬，并发>3 就返回"
                         "『正在检测当前网络环境』挑战页")
    a = ap.parse_args()

    subs = ([s.strip() for s in a.subjects.split(",") if s.strip()]
            if a.subjects else list(SUBJECTS))
    cid_of = {v: k for k, v in SUBJECTS.items()}

    os.makedirs(RAW_DIR, exist_ok=True)
    with open(INDEX, encoding="utf-8") as f:
        idx = json.load(f)
    papers = idx["papers"]
    by_id = {p["id"]: p for p in papers}

    # 阶段1：全科全分页发现
    print("阶段1 扫描试卷目录", flush=True)
    for s in subs:
        cid = cid_of[s]
        found = scan_list(cid, s, a.pages)
        new = 0
        for pid, it in found.items():
            if pid in by_id:
                continue
            session, region = classify(it["title"])
            _id = f"sh-{it['year'] or 0}-{session}-{pid}"
            by_id[_id] = {
                "id": _id,
                "year": it["year"],
                "session": session,
                "region": region,
                "subject": s,
                "title": it["title"],
                "paperKind": it["kind"],
                "sourceSite": "zujuan.com（组卷网）",
                "sourceUrl": VIEW.format(pid=pid),
                "paperId": pid,
                "access": "preview",
                "trust": "medium",
                "trustLevel": "B",
                "trustNote": "组卷网公开详情页题干全文；公式以下标/纯文本呈现，"
                             "几何图形与作图缺失，扫描版原卷需另找。",
                "rawFile": None,
                "chars": 0,
                "questionSignals": 0,
                "chid": cid,
            }
            new += 1
        print(f"  {s}: 累计发现 {len(found)} 套，新增 {new}", flush=True)

    papers[:] = sorted(by_id.values(), key=lambda p: p["id"])
    print(f"\n索引合计 {len(papers)} 套", flush=True)
    if a.list_only:
        idx["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
        with open(INDEX, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        print("已写索引（list-only）")
        return

    # 阶段2：抓公开全文入库 RAW
    todo = [p for p in papers
            if not p.get("rawFile") and p.get("paperId")
            and p.get("sourceSite", "").startswith("zujuan")]
    print(f"\n阶段2 抓全文 {len(todo)} 份（并发 {a.text_workers}）", flush=True)

    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    known = {i["file"] for i in man["items"]}
    new_items = []

    def grab(p):
        rec, err = fetch_text(p["paperId"], p["title"], p["subject"], p.get("chid", 3))
        if err:
            return p, None, err
        rel = f"RAW/试卷库/全文/{p['id']}.txt"
        path = os.path.join(ROOT, rel)
        # RAW 不可变：内容指纹不变就复用旧文件，避免 sha 漂移
        body = rec["body"]
        header = (f"# {rec['title']}\n# 科目：{rec['subject']}｜来源：{rec['sourceUrl']}\n"
                  f"# 抓取：{rec['fetchedAt']}｜题干信号 {rec['questionSignals']} 处\n\n")
        sha = hashlib.sha256((header + body).encode("utf-8")).hexdigest()
        if not (os.path.exists(path) and p.get("bodySha") == sha):
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + body)
        with open(path, "rb") as f:
            data = f.read()
        time.sleep(random.uniform(1.2, 2.8))   # 抖动：反爬按固定间隔判定
        return p, (rel, len(body), hashlib.sha256(data).hexdigest(),
                   rec["questionSignals"]), None

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=a.text_workers) as ex:
        futs = [ex.submit(grab, p) for p in todo]
        for n, fu in enumerate(as_completed(futs), 1):
            p, r, err = fu.result()
            if not r:
                fail += 1
                p["fetchError"] = err
                if n <= 10 or n % 20 == 0:
                    print(f"  [{n}/{len(futs)}] ⚠️  {p['id']} {err}", flush=True)
                continue
            rel, chars, fsha, qs = r
            p["rawFile"] = rel
            p["chars"] = chars
            p["questionSignals"] = qs
            p["bodySha"] = fsha
            p.pop("fetchError", None)
            if rel not in known:
                new_items.append({"file": rel, "kind": "text",
                                  "bytes": os.path.getsize(os.path.join(ROOT, rel)),
                                  "sha256": fsha, "status": "公开详情页全文",
                                  "parsed": {"wrongIds": [], "modules": []}})
                known.add(rel)
            ok += 1
            if n <= 10 or n % 25 == 0:
                print(f"  [{n}/{len(futs)}] ✅ {p['id']} {chars}字 "
                      f"{qs}处题干信号", flush=True)

    idx["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
    idx["meta"]["count"] = len(papers)
    # 2026-10-03 第七次核验修：meta.stats 原本只在 fetch_shanghai_papers.py 的
    # 单次运行里累加（fetched/metaOnly/failed/unchanged 都是「本次增量」），
    # 本脚本重写 count 却没重算 stats，导致两者语义冲突：
    # count=21（累积套数）而 stats 之和=17（某次运行的增量），fetched=0 但实际已抓 11 份。
    # 增量统计与累积总量并排放在同一处，读者必然误读，且没有任何机器校验。
    # 改为：stats 全部从 papers 数组现算，且三项之和恒等于 count。
    # 删掉 unchanged —— 它是运行时概念（「本次没重新下载」），落盘后无从考证，
    # 且与 fetched 语义重叠，留着只会让总数对不上。
    recomputed = {"fetched": 0, "metaOnly": 0, "failed": 0}
    for p in papers:
        if p.get("rawFile"):
            recomputed["fetched"] += 1
        elif p.get("fetchError"):
            recomputed["failed"] += 1
        else:
            recomputed["metaOnly"] += 1
    recomputed["note"] = ("由 papers 数组现算，非单次运行增量，三项之和恒等于 count："
                          "fetched=有 rawFile 的条数，failed=有 fetchError 的条数，"
                          "metaOnly=其余（尚未抓取全文）")
    idx["meta"]["stats"] = recomputed
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    man["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)

    print(f"\n完成：成功 {ok} · 失败 {fail} · "
          f"manifest 新增 {len(new_items)} · 索引 {len(papers)} 套", flush=True)


if __name__ == "__main__":
    main()
