# data/ · 数据层

本目录是整个项目的**核心资产**。所有失分结构的变化都在这里被记录。

## 为什么数据必须进 Git

代码会频繁改，但**"这个月比上个月多丢了哪些分"才是这个项目真正的价值**。
Git 天然擅长记录变化：谁在什么时候、哪个知识点、丢了多少分、后来补上没有——全部可 diff、可回溯、可追责。

如果数据只存在浏览器 localStorage，答案就没了：换个设备、清个缓存，全没了。

---

## 子目录说明

| 目录 | 存什么 | 状态 |
|---|---|---|
| `exams/` | 试卷结构：卷面分值、模块划分、每模块得分 | 待建 |
| `wrong/` | 错题（=失分点）条目 | 待建 |
| `progress/` | 知识点掌握度快照、复测记录 | 待建 |

---

## 数据格式约定

- **纯 JSON**，UTF-8，2 空格缩进（diff 友好）
- 文件名带日期与批次：`2027-01-exam-math.json`、`wrong-2026-10.json`
- 每个文件顶层带 `meta` 字段（生成时间、来源、schema 版本）

---

## Schema

### exams/ — 试卷

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
  "totalScore": 150,
  "scored": 119,
  "modules": [
    {
      "name": "二次函数",
      "score": 12,
      "full": 20,
      "questions": [
        {
          "id": "q18",
          "point": "九下 26.2 二次函数应用",
          "full": 6,
          "lost": 4,
          "cause": "方法没想到"
        }
      ]
    }
  ]
}
```

**`modules[].lost` 之和应等于 `totalScore - scored`**，导入时做校验。

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
    }
  ]
}
```

**字段说明：**

| 字段 | 必填 | 说明 |
|---|---|---|
| `examId` | 是 | 来源试卷 ID，关联 `exams/` |
| `module` | 是 | 模块名，用于热区图分组 |
| `full` / `lost` | 是 | 满分 / 实失分，**缺了这两个字段就做不了失分点分析** |
| `cause` | 是 | 五类错因之一，见 `docs/方法论/错因分类与复习排期.md` |
| `myThought` | 建议 | **我当时是怎么想的**（不是正确思路），诊断价值最高 |
| `status` | 是 | `pending` / `reviewing` / `mastered` |
| `reviewRounds` | 否 | 复测轮次记录 `[{round:1,date,pass:true}]` |

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

现有 wrong 对象仅 6 字段（`title/sub/point/cause/note/ts`），**缺 `full`/`lost`/`module`/`examId`**。
下轮升级时需要写一个 `tools/migrate-localstorage.js`，把 localStorage 数据转换成 `wrong.v1` 格式，
并对缺失的分值字段做人工补录提示。
