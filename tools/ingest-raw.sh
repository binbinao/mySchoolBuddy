#!/bin/bash
# ingest-raw.sh — RAW 原件入库预处理
#
# 职责：把新放入 RAW/ 的照片/文档压缩成适合入库的形态，并更新 data/raw-manifest.json 索引。
# 原则：RAW/ 内的文件是「不可变事实层」——只做无损语义的压缩，不裁剪、不修改内容。
#
# 用法：
#   tools/ingest-raw.sh              # 处理全部未入库文件
#   tools/ingest-raw.sh --dry-run    # 只看会做什么，不写任何文件
#   tools/ingest-raw.sh --force      # 已入库文件也重新处理
#   tools/ingest-raw.sh --help

set -o pipefail
# 刻意不使用 set -u / set -e：macOS bash 3.2 下空数组展开会触发 unbound variable，
# 单个文件处理失败也不应中断整批入库。逐项判断错误更符合「批量入库」语义。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW="$ROOT/RAW"
MANIFEST="$ROOT/data/raw-manifest.json"
MAX_EDGE=2000
JPEG_Q=50
PDF_WARN_MB=10

DRY=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1;;
    --force)   FORCE=1;;
    --help|-h) sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0;;
    *) echo "未知参数：$a（用 --help 查看用法）" >&2; exit 1;;
  esac
done

command -v sips >/dev/null || { echo "错误：需要 macOS 自带的 sips" >&2; exit 1; }
[ -d "$RAW" ] || { echo "错误：找不到 RAW/ 目录" >&2; exit 1; }

PY=""
for c in /Users/jiduobin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { echo "错误：找不到 python3" >&2; exit 1; }

is_img() { case "$1" in *.jpg|*.jpeg|*.JPG|*.JPEG|*.png|*.PNG|*.heic|*.HEIC|*.heif|*.HEIF|*.webp|*.WEBP) return 0;; *) return 1;; esac; }
is_pdf() { case "$1" in *.pdf|*.PDF) return 0;; *) return 1;; esac; }
bytes() { stat -f%z "$1" 2>/dev/null || echo 0; }

# 已入库判定：以 manifest 已登记的文件为准。
# 不用 git ls-files —— 刚处理完的文件还没 commit，git 查不到会导致重复入库。
in_manifest() {
  [ -f "$MANIFEST" ] || return 1
  "$PY" -c "import json,sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    sys.exit(0 if any(it.get('file')==sys.argv[2] for it in d.get('items',[])) else 1)
except Exception:
    sys.exit(1)" "$MANIFEST" "$1" >/dev/null 2>&1
}

# ── 1. 扫描 RAW/ 下的待处理文件 ──────────────────────────────
# 注意：macOS 自带 bash 3.2 无 mapfile，改用 while read 兼容写法
FILES=()
while IFS= read -r line; do
  [ -n "$line" ] && FILES+=("$line")
done < <(find "$RAW" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
  -o -iname '*.heic' -o -iname '*.heif' -o -iname '*.webp' -o -iname '*.pdf' \) \
  ! -name '.*' ! -path '*/_*' | sort)

if [ ${#FILES[@]} -eq 0 ]; then
  echo "RAW/ 下没有待处理的图片或 PDF。"; exit 0
fi

# ── 2. 逐个处理 ─────────────────────────────────────────────
CHANGED=0; SKIPPED=0
TSV_NEW="$(mktemp -t rawnew)"
: > "$TSV_NEW"

for f in "${FILES[@]}"; do
  rel="${f#$ROOT/}"; base="$(basename "$f")"; size=$(bytes "$f")
  w=""; h=""; newsize="$size"

  if [ "$FORCE" = "0" ] && in_manifest "$rel"; then
    SKIPPED=$((SKIPPED+1)); echo "  跳过（已入库）：$rel"; continue
  fi

  echo "处理：$rel  ($(( size / 1024 )) KB)"

  if is_img "$f"; then
    tmp="$f.ingest.jpg"
    # -Z 限制长边；formatOptions 控制 JPEG 质量。
    # 试卷是白底黑字的高对比内容，q=50 即可清晰可读且体积最小。
    # 实测 3024x4032 手机原图 10.5MB → 约 305KB（降幅 97%），文字完全可读。
    if ! sips -s format jpeg -s formatOptions "$JPEG_Q" -Z "$MAX_EDGE" "$f" --out "$tmp" >/dev/null 2>&1; then
      echo "  ✗ 压缩失败，跳过：$rel"; rm -f "$tmp"; continue
    fi
    dim="$(sips -g pixelWidth -g pixelHeight "$tmp" 2>/dev/null | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w" "h}')"
    w="${dim%% *}"; h="${dim##* }"
    newsize=$(bytes "$tmp")
    sha="$(shasum -a 256 "$tmp" | awk '{print $1}')"
    origsize="$size"
    if [ "$DRY" = "0" ]; then
      # 原图（HEIC/PNG 等）移入 _originals/（已被 .gitignore 排除），本地保留可回溯性
      case "$base" in
        *.jpg|*.jpeg|*.JPG|*.JPEG) rm -f "$f";;
        *) mkdir -p "$RAW/_originals"; mv "$f" "$RAW/_originals/$base";;
      esac
      mv "$tmp" "$f"
    else
      rm -f "$tmp"
    fi
    ratio="$(awk -v n="$newsize" -v o="$origsize" 'BEGIN{ if(o>0) printf "%.0f", (1 - n/o)*100; else print 0 }')"
    echo "  ✓ 压缩完成 → ${w}x${h}  $(( newsize / 1024 )) KB（原 $(( origsize / 1024 )) KB，降幅 ${ratio}%）"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rel" "image" "$newsize" "$sha" "$w" "$h" "$origsize" >> "$TSV_NEW"
    CHANGED=$((CHANGED+1))

  elif is_pdf "$f"; then
    sha="$(shasum -a 256 "$f" | awk '{print $1}')"
    warn=""
    if [ "$size" -gt $(( PDF_WARN_MB * 1048576 )) ]; then
      warn="  ⚠ 超过 ${PDF_WARN_MB}MB，建议先拆分或转图后入库"
    fi
    echo "  ✓ PDF 原样入库（不做有损压缩）  $(( size / 1024 )) KB${warn}"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rel" "pdf" "$size" "$sha" "" "" "" >> "$TSV_NEW"
    CHANGED=$((CHANGED+1))
  fi
done

# ── 3. 汇总 ─────────────────────────────────────────────────
echo ""
echo "──────────────────────────────"
echo "新增/更新 $CHANGED 个 · 跳过 $SKIPPED 个"
if [ "$DRY" = "1" ]; then
  rm -f "$TSV_NEW"; echo "（dry-run：未写入任何文件）"; exit 0
fi
if [ "$CHANGED" = "0" ]; then
  rm -f "$TSV_NEW"; echo "manifest 无需更新。"; exit 0
fi

CREATED="$(date +%Y-%m-%d)"

# ── 4. 合并进 manifest ───────────────────────────────────────
# 元数据先落到 TSV 文件再交给 Python 读。
# 不把含中文/特殊字符的文件名当 shell 变量传给 python -c 的参数 ——
# 实测中文路径会被截断，导致 JSON 解析失败。文件传递最稳妥。
"$PY" - "$MANIFEST" "$TSV_NEW" "$CREATED" <<'PY'
import json, os, sys

manifest, tsv, created = sys.argv[1], sys.argv[2], sys.argv[3]
NOTE = ("RAW 原件索引。每条记录指向 RAW/ 下一个已压缩入库的原件；"
        "解析产物（错题、卷面结构分析）通过 sourceRaw 字段反向溯源到这里。详见 RAW/README.md")

if not os.path.exists(manifest):
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    d = {"meta": {"schema": "raw.v1", "note": NOTE, "created": created}, "items": []}
else:
    with open(manifest, encoding="utf-8") as f:
        d = json.load(f)
    d["meta"]["note"] = NOTE

d["meta"]["updated"] = created

known = {it.get("file") for it in d["items"]}
added = 0
with open(tsv, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rel, kind, nbytes, sha = parts[0], parts[1], parts[2], parts[3]
        w = parts[4] if len(parts) > 4 else ""
        h = parts[5] if len(parts) > 5 else ""
        obytes = parts[6] if len(parts) > 6 else ""
        if rel in known:
            continue
        item = {"file": rel, "kind": kind, "bytes": int(nbytes or 0), "sha256": sha}
        if w:
            item["width"] = int(w)
        if h:
            item["height"] = int(h)
        if kind == "image" and obytes:
            item["originalBytes"] = int(obytes or 0)
        item["status"] = "已入库·待解析"
        item["parsed"] = {"wrongIds": [], "modules": []}
        d["items"].append(item)
        known.add(rel)
        added += 1

with open(manifest, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(f"  manifest 新增 {added} 条，共 {len(d['items'])} 条")
PY
rm -f "$TSV_NEW"

echo "manifest 已更新：$MANIFEST"
echo "下一步：提交 RAW/ 改动，然后对照原件做解析（错题 → data/wrong/，卷面结构 → data/exams/）"
