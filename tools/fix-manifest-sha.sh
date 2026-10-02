#!/bin/bash
# fix-manifest-sha.sh — 订正 raw-manifest.json 里记错的指纹
#
# 为什么需要它：ingest-raw.sh 在压缩后算 sha256，但曾出现记录值与
# 实际文件对不上的情况（bytes 也差值）。若不订正，validate_schema.py
# 会一直报错，久而久之人就开始忽略校验输出——那才是真正的危险。
#
# 安全阀（重要）：只在「Git 里存的那一版指纹 == 当前文件指纹」时才订正。
# 这意味着文件本身没被动过，错的只是记录。
# 若 Git 里那一版也不同 → 说明原件真被改过，此时**拒绝自动订正**，
# 必须人工查清。否则一键「修复」就把证据被篡改的现场抹平了。

set -o pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PY=""
for c in /Users/jiduobin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { echo "错误：找不到 python3" >&2; exit 1; }

DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1;;
    --help|-h) sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0;;
    *) echo "未知参数：$a" >&2; exit 1;;
  esac
done

if [ "$DRY" = "1" ]; then echo "dry-run：只报告，不写盘"; echo ""; fi

"$PY" - "$DRY" <<'PY'
import hashlib, json, os, subprocess, sys

ROOT = os.getcwd()
dry = sys.argv[1] == "1"
MP = os.path.join(ROOT, "data", "raw-manifest.json")

def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()

def sha_head(rel):
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return hashlib.sha256(r.stdout).hexdigest()

with open(MP, encoding="utf-8") as f:
    d = json.load(f)

fixed, refused = [], []
for it in d.get("items", []):
    rel, rec = it.get("file", ""), it.get("sha256")
    if not rel or not rec:
        continue
    ap = os.path.join(ROOT, rel)
    if not os.path.exists(ap):
        refused.append((rel, "文件不存在"))
        continue
    actual = sha_file(ap)
    if actual == rec:
        continue
    head = sha_head(rel)
    if head == actual:
        fixed.append((rel, rec, actual, it.get("bytes")))
        it["sha256"] = actual
        it["bytes"] = os.path.getsize(ap)
    else:
        refused.append((rel, f"HEAD={str(head)[:12]}… 当前={actual[:12]}… 记录={rec[:12]}…"))

for rel, old, new, _ in fixed:
    print(f"  ✓ 订正 {rel}\n      {old[:16]}… → {new[:16]}…")
for rel, why in refused:
    print(f"  ✗ 拒绝自动订正 {rel}：{why}\n      文件可能真被改动过，需人工核对，不要盲目覆盖记录")

if fixed and not dry:
    with open(MP, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\n已订正 {len(fixed)} 条")
elif fixed:
    print(f"\ndry-run：将订正 {len(fixed)} 条，未写盘")
else:
    print("\n无需订正")
sys.exit(1 if refused else 0)
PY
