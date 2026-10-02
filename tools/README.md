# tools/

解析、统计与生成脚本。

## 计划中的脚本

| 脚本 | 用途 | 状态 |
|---|---|---|
| `migrate-localstorage.js` | 把 app/index.html 的 localStorage 数据（`msb.v1`）转换为 `data/wrong/wrong.v1` 格式，补录缺失的分值字段 | 待建 |
| `analyze-loss.js` | 统计失分点热区：按模块 / 知识点 / 病因三个维度聚合分值 | 待建 |
| `validate-schema.js` | 校验 data/ 下 JSON 是否符合 schema，`modules[].lost` 之和是否等于总分差 | 待建 |

## 约定

- Node.js 无外部依赖，或依赖记在 `package.json`
- 每个脚本支持 `--help`
- 只读脚本不修改文件；改数据的脚本必须先备份到 `data/_local/`
