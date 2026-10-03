# data/ · 解析层

本目录存放从 `RAW/` 原件拆解出来的**结构化事实**。原件提供证据，这里提供可计算的形状。

## 为什么数据必须进 Git

代码会频繁改，但**"这个月比上个月多丢了哪些分"才是这个项目真正的价值**。
Git 天然擅长记录变化：谁在什么时候、哪个知识点、丢了多少分、后来补上没有——全部可 diff、可回溯。

如果数据只存在浏览器 localStorage，答案就没了：换个设备、清个缓存，全没了。

> **前提**：数据必须能溯源到 `RAW/` 里的原件，否则就是无根之木。
> 所以 `wrong.v1` / `exam.v1` 都带 `sourceRaw` 字段，指回具体的原件路径。

---

## 文件清单

| 文件/目录 | 存什么 | 状态 |
|---|---|---|
| `raw-manifest.json` | 原件索引：路径、sha256、尺寸、解析产物反向引用 | 首次入库时自动生成 |
| `exams/` | 试卷结构：卷面分值、模块划分、每模块得分 | 已建（2026-10-02 英语单元测） |
| `wrong/` | 错题（=失分点）条目 | 已建（wrong-2026-10.json，2 条） |
| `progress/` | 知识点掌握度快照、复测记录 | 待建（仅 .gitkeep） |

---

## 数据格式约定

- **纯 JSON**，UTF-8，2 空格缩进（diff 友好）
- 文件名带日期与批次：`2027-01-exam-math.json`、`wrong-2026-10.json`
- 每个文件顶层带 `meta` 字段（生成时间、来源、schema 版本）

---

## Schema

### exams/ — 试卷结构分析

> ⚠️ **本文档是示例结构，与磁盘上的实际数据有差异**——实际 `exams/*.json` 顶层是
> `{meta, exams:[...]}`，逐题失分清单在 `exams[0].wrongs`（不是 `modules[].questions`），
> 模块实得分用 `got`（不是 `score`）。**以实际文件为准，本节只说明字段含义。**
> 实际结构见 `data/exams/2026-10-02-单元测-英语.json`。

```json
{
  "meta": {
    "schema": "exam.v1",
    "created": "2026-10-05",
    "note": "初二期末数学卷"
  },
  "name": "初二期末数学",
  "date": "2026-06-30",
  "subject": "数学",
  "sourceRaw": ["RAW/试卷/20260630-期末-数学-p1.jpg", "RAW/试卷/20260630-期末-数学-p2.jpg"],
  "totalScore": 150,
  "scoreConfirmed": false,
  "score": { "got": null, "full": 150 },
  "modules": [
    {
      "name": "二次函数",
      "got": null,
      "gotNote": "卷面无单题分值标注，留 null 不做等分折算",
      "full": 20,
      "qRange": "18-22",
      "evidence": "逐大题的红笔批改情况"
    }
  ]
}
```

**分值纪律同样适用**（见下方「分值纪律」一节）：

| 字段 | 说明 |
|---|---|
| `totalScore` | 卷面总分。**卷面印了才填** |
| `scoreConfirmed` | 家长确认过单题分值与总分才置 `true`。**未确认一律 `false`**，此时不计入失分热区统计 |
| `score.got` | 全卷实得。**算不出就留 `null`**——分项里有一处得分不明时，不要给合计 |
| `modules[].got` | 该模块实得。**留 `null` 时必须在 `gotNote` 写明为何不折算** |

> **为什么 `got` 也要能留 null**：单空分值缺失时，「24」和「25」可能都是 `10÷5` 的等分折算，
> 两个都不能当实得。写任一个都会让热区分析站不住。写 `null` + `gotNote` 才是诚实的。

`sourceRaw` 指向 `raw-manifest.json` 里的原件路径，数组形式（多页卷子）。**必填**——没有它，这份分析就无法回溯核对。

### wrong/ — 错题（失分点）

```json
{
  "meta": {
    "schema": "wrong.v1",
    "created": "2026-10-05",
    "count": 12
  },
  "items": [
    {
      "id": "w-20261005-01",
      "sourceRaw": "RAW/试卷/20260630-期末-数学-p1.jpg",
      "examId": "2026-06-30-math",
      "title": "求抛物线在指定区间上的最值",
      "subject": "数学",
      "module": "二次函数",
      "point": "九下 26.2 二次函数应用",
      "full": 6,
      "lost": 4,
      "cause": "方法没想到",
      "myThought": "想用顶点公式直接算，忘了题目给的是区间不是全体实数",
      "note": "区间最值必须看端点，这是我的固定盲点",
      "status": "pending",
      "reviewRounds": [],
      "ts": 1759700000000
    },
    {
      "id": "w-20261005-02",
      "sourceRaw": "RAW/错题照片/20261005-数学-第23题.jpg",
      "examId": "2026-10-05-math",
      "title": "第23题 关联抛物线求解析式",
      "subject": "数学",
      "module": "二次函数",
      "point": "九下 26.2 二次函数应用",
      "full": null,
      "lost": null,
      "scorePending": true,
      "scoreNote": "照片中第23题未见分值标注，需对照卷面分值表回填后才能计入失分点热区统计。",
      "cause": "方法没想到",
      "status": "pending",
      "reviewRounds": [],
      "ts": 1759700000000
    }
  ]
}
```

> 示例第二条演示的是**分值缺失时该怎么写**：`full`/`lost` 为 `null` + `scorePending: true`。
> 真实数据里的两条数学错题目前都是这个状态（照片里确实没有分值标注）。

**字段说明：**

| 字段 | 必填 | 说明 |
|---|---|---|
| `sourceRaw` | 是 | 指向 RAW/ 原件路径（字符串或数组）。**没有它，这条错题就是无根之木** |
| `examId` | 是 | 来源试卷 ID，关联 `exams/` |
| `module` | 是 | 模块名，用于热区图分组 |
| `full` / `lost` | **键必须存在，值可为 null** | 满分 / 实失分。**键在、值 null**，配 `scorePending: true`（详见下方「分值纪律」） |
| `cause` | 是 | 五类错因之一，见 `docs/方法论/错因分类与复习排期.md` |
| `myThought` | 建议 | **我当时是怎么想的**（不是正确思路），诊断价值最高 |
| `status` | 是 | `pending` / `reviewing` / `mastered` |
| `reviewRounds` | 否 | 复测轮次记录 `[{round:1,date,pass:true}]` |

---

## ⚠️ 分值纪律（`full` / `lost` 的双态规范）

> **`full` / `lost` 这两个键必须存在，但它们的值允许是 `null`。**
> **卷面照片上没有分值标注，就留 `null` 并配 `scorePending: true`，绝不估算。**

这不是可选风格，是项目三条硬纪律之一，`tools/validate_schema.py` 有机器闸门强制。

| 情况 | `full` / `lost` | 附加字段 |
|---|---|---|
| 卷面**明确标了**分值（如「每题 2 分」） | **必须回填实际数字** | 无 |
| 卷面**无**分值标注 | `null` | `scorePending: true` + `scoreNote` 写明为何留空 |
| 有满分、无扣分明细 | 填 `full`，`lost` 留 `null` | `scorePending: true`（红笔未明示扣空数，不替老师判卷） |

**两个方向都会失职**：

- ❌ 卷面印着分值却留 `null` —— **失职**。判据是「照片上有没有」，不是「推算得出来吗」。
- ❌ 卷面没印却填个看起来合理的数 —— **更糟**。编造的分值会被当成事实引用，污染失分热区统计，且极难发现。

**为什么 `null` 比错值好**：填了 `null` + `scorePending` 的题，统计时会明确排除在热区之外；
填了错值的题，会混进热区、误导复习优先级，而且没有任何标记能把它挑出来。

```json
{
  "id": "w-20261002-01",
  "full": null,
  "lost": null,
  "scorePending": true,
  "scoreNote": "照片中第19题未见分值标注，需对照卷面分值表回填后才能计入失分点热区统计。"
}
```

**算不出合计就不给合计。** 分项里有一处得分不明时，别写「已确认实得 X 分」——
无依据的合计数字比没有更糟，它会被当成结论引用。

### progress/ — 掌握度快照

```json
{
  "meta": { "schema": "progress.v1", "created": "2026-10-05" },
  "points": [
    { "point": "九下 26.2 二次函数应用", "mastery": 0.4, "lost": 18, "count": 7 }
  ]
}
```

`mastery` 取值 0~1，按复测通过轮次加权计算。

---

## 迁移说明

当前 `app/index.html` 使用 localStorage（key `msb.v1`），数据结构为：
`{plan:[], wrong:[], settings:{exam}}`

现有 wrong 对象仅 6 字段（`title/sub/point/cause/note/ts`），**缺 `full`/`lost`/`module`/`examId`/`sourceRaw`**。

下轮升级时需要写一个 `tools/migrate-localstorage.js`，把 localStorage 数据转换成 `wrong.v1` 格式。

> ⚠️ **迁移时最容易犯的错**：把 `full`/`lost` 当成必填数字，缺失就**替家长估一个**。
> 正确做法是——转换脚本对这些题写 `null` + `scorePending: true`，
> **提示语应是「这条卷面上没有分值，需要家长对照原卷补录或确认为留空」**，
> 而不是「请填写满分与失分」。前者是补证据，后者是逼人编数字。

---

## 拆解工作流（人来做，工具不猜）

AI 不做 OCR 自动解析——手写试卷的识别准确率不可靠，错了的分数比没有分数更危险。
正确做法是：**AI 读原件 + 出题号清单，人填分值和错因**。

1. 拍卷子 → 存入 `RAW/试卷/` → 跑 `tools/ingest-raw.sh`
2. 对照压缩后的原件，逐题核对：题号 / 模块 / 知识点 / 满分 / 实失 / 错因
   —— **满分实失只填卷面上看得见的**；看不见就留 `null` + `scorePending`，不做等分折算
3. 写入 `data/wrong/wrong-YYYYMMDD-科目.json`
4. 汇总模块得分率写入 `data/exams/YYYY-MM-DD-科目.json`
5. 回填 `raw-manifest.json` 的 `status` 与 `parsed` 字段
6. 提交（建议 raw 和 data 分两条 commit）

> 第 5 步的 `parsed` 是**双向可溯**的关键：`{"wrongIds": [...], "modules": [...]}`，
> 记下这份原件解析出了什么。缺了它就只剩单向引用，没法从结论走回证据。
