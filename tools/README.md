# tools/ · 工具层

解析、统计与生成脚本。

## 已有脚本

| 脚本 | 用途 | 状态 |
|---|---|---|
| `ingest-raw.sh` | RAW 原件入库预处理：图片压缩（长边 2000px / JPEG q50）、原图备份至 `_originals/`、PDF 原样入库、更新 `data/raw-manifest.json`（含 sha256）。支持 `--dry-run` / `--force` / `--help`，幂等。 | ✅ **已实测通过**（4 条路径回归） |

### ingest-raw.sh 用法

```bash
tools/ingest-raw.sh --dry-run    # 预演，不写任何文件
tools/ingest-raw.sh              # 执行入库
tools/ingest-raw.sh --force      # 已入库文件也重新处理
```

依赖：macOS 自带 `sips`、`shasum`、`python3`。无第三方依赖。

实测数据：手机原图 3024×4032 / 10.5MB → 1500×2000 / 305KB（降幅 97%），文字清晰可读。

---

## 计划中的脚本

| 脚本 | 用途 | 状态 |
|---|---|---|
| `analyze-loss.js` | 统计失分点热区：按模块 / 知识点 / 病因三个维度**聚合分值**（不是题数） | 待建 |
| `migrate-localstorage.js` | 把 `app/index.html` 的 localStorage（`msb.v1`）转换为 `wrong.v1` 格式，提示补录缺失的分值字段 | 待建 |
| `validate-schema.js` | 校验 `data/` 下 JSON 是否符合 schema；`modules[].lost` 之和是否等于总分差；`sourceRaw` 是否指向 manifest 中存在的原件 | 待建 |

---

## 设计原则

- **不做 OCR 自动解析**。手写试卷的识别准确率不可靠，错了的分数比没有分数更危险。正确做法是 AI 读原件出题号清单，人填分值和错因。
- Node 脚本无外部依赖，或依赖记在 `package.json`。
- 每个脚本支持 `--help`。
- 只读脚本不修改文件；改数据的脚本必须先备份到 `data/_local/`。
- **脚本必须实测通过才算完成**。bash 在 macOS 3.2 下有一批坑，都已踩过并修复：
  - `mapfile` 不存在 → 改用 `while read` 兼容写法
  - `set -u` 下空数组展开报 `unbound variable` → 不用 `set -u`
  - `&& { }` 短路在 `set -u` 下误判退出 → 改正常 `if`
  - 含中文的路径当 shell 变量传给 `python -c` 会被截断 → 元数据走 TSV 文件传递
  - `awk '/pixel/'` 匹配两个字段导致维度错位 → 分别匹配 `pixelWidth` / `pixelHeight`
