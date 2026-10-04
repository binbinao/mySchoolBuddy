#!/usr/bin/env python3
"""用数据层重新生成页面内嵌的数据副本（修副本漂移，不手工改数字）。

**为什么要有这个脚本**：`docs/实战表/上海试卷地图.html` 里内嵌了一份
`data/resources/shanghai-papers.json` 的副本。两边从此各活各的——
采集新增 30 套后，数据层 51 套、页面还停在 21 套，
**页面不会报错，少掉的 30 张卡片没人会发现**。

修法铁律：**从数据层重新生成，不手工改数字**。
手工改这次，下次采集后又漂移。

用法：python tools/regen_paper_map_data.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "resources", "shanghai-papers.json")
PAGE = os.path.join(ROOT, "docs", "实战表", "上海试卷地图.html")


def main():
    if not os.path.exists(PAGE):
        print(f"❌ 页面不存在：{PAGE}")
        return 1

    with open(SRC, encoding="utf-8") as f:
        d = json.load(f)

    with open(PAGE, encoding="utf-8") as f:
        html = f.read()

    # 内嵌块是 <script> const DATA = { ... }; </script>
    # ⚠️ 结构必须先确认：第一次写成 application/json，第二次用非贪婪正则
    #    `\{.*?\}`——**两处都失败**：前者页面没这种块，后者遇到嵌套对象的
    #    第一个 } 就截断。⇒ 改用**括号配平**定位，这个方法对任意嵌套都成立。
    marker = "const DATA"
    mi = html.find(marker)
    if mi < 0:
        print(f"❌ 页面里没有 {marker} 块，页面结构可能变了——"
              "**不要手工插入，先读页面**")
        return 1

    # 从 marker 后的第一个 { 开始做括号配平（跳过字符串里的括号）
    i = html.find("{", mi)
    depth = 0
    in_str = False
    esc = False
    end = -1
    for k in range(i, len(html)):
        ch = html[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end < 0:
        print("❌ 括号配平失败，const DATA 块没有正常闭合")
        return 1

    payload = json.dumps(d, ensure_ascii=False, indent=1)
    out = html[:i] + payload + html[end:]
    replaced = 1

    if not replaced:
        print("❌ 找到 JSON 块但没有含 papers 的块，页面结构可能变了")
        return 1

    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"✅ 已用数据层重新生成 {replaced} 个内嵌块")
    print(f"   数据层 {len(d.get('papers', []))} 套 → {os.path.relpath(PAGE, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
