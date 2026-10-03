# tools/ · 工具层

解析、校验、生成与同步脚本。**脚本不做教学判断**，只做确定性填充。

---

## 主管道

```
tools/pipeline.sh          # 一次跑完下面 4 步
```

| 步骤 | 脚本 | 做什么 |
|---|---|---|
| 1 | `ingest-raw.sh` | RAW 原件压缩 + 建索引（含 sha256） |
| 2 | `build_skeletons.py` | 从 JSON 生成讲义骨架 HTML |
| 3 | `validate_schema.py` | 校验三条硬纪律 |
| 4 | git | 提交 + 推送远端 |

```bash
tools/pipeline.sh                  # 正常跑
tools/pipeline.sh --dry-run        # 全程预演，不动 git
tools/pipeline.sh --force          # 跳过冷却窗口
tools/pipeline.sh --no-push        # 只提交不推送
tools/pipeline.sh --help
```

### 三条设计决定（都是踩坑换来的）

**1. 幂等** —— 没变化就完全不提交。定时任务会跑几百次，任何一次产生空提交都会让 git 历史失去可读性。

**2. 校验不阻断提交，只阻断推送** —— 数据有问题时**照样提交**（问题必须留下来让人看见），但拒绝推送，并在仓库里留下 `data/_local/VALIDATION-FAILED.md` 提醒。悄悄跳过才最危险。

**3. 锁里写 PID** —— 只写 `trap` 清理锁是不够的。实测：脚本用 `tee | tail` 时，tail 退出后本脚本收到 SIGPIPE，trap 不执行，锁目录残留，**管道从此再也不跑且无人知晓**。所以改用「mkdir 原子锁 + 记 PID + 检查进程是否存活」。同理把 `tee | tail` 换成先落盘再读文件。

---

## 脚本清单

| 脚本 | 用途 | 状态 |
|---|---|---|
| `pipeline.sh` | 主管道：入库 → 生成 → 校验 → 提交推送 | ✅ 已实测 |
| `ingest-raw.sh` | RAW 原件压缩（长边 2000px / JPEG q50）、备份原图、更新 manifest | ✅ 已实测 |
| `build_skeletons.py` | 生成精讲页 / 试卷拆解页骨架 | ✅ 已实测 |
| `validate_schema.py` | 校验分值/溯源/原件三条纪律 | ✅ 已实测（抓出真实数据错误） |
| `fix-manifest-sha.sh` | 订正 manifest 里记错的指纹 | ✅ 已实测 |
| `install-launchd.sh` | 部署/卸载定时任务 | ⚠️ 见下方说明 |

### build_skeletons.py

从 `data/wrong/*.json`（schema `wrong.v1`）和 `data/exams/*.json`（schema `exam.v1`）生成单文件 HTML：

```
data/wrong/w-xxx.json  →  docs/实战表/<学科>/q<题号>-<模块>-精讲.html
data/exams/e-xxx.json  →  docs/实战表/<学科>/<日期>-<卷名>-试卷拆解.html
```

**脚本能自动填的**（纯确定性）：题干全文 + 选项组装、限定词高亮、原卷照片引用、已做对项对照、断点答案对照、模块得分率与色阶、错因分布、家长区框架、分值纪律提示。

**脚本不填的**（标 `⚠️ 待补` 并登记到 `data/tasks/pending.json`）：零跳跃讲解步骤、断点分析、三题变式、想五题、逐题判分、诊断结论。

> 为什么不做 OCR 自动判分：手写试卷识别不可靠，**错的分数比没有分数更危险**。正确做法是 AI 读原件出题号清单 + 家长核对分值。

**关键安全阀**：页面头部有 `<meta name="generator" content="generated-by: tools/build_skeletons.py">`。带此标记的页面可被重新生成；**不带标记的（人工写就的）绝不会被覆盖**。自动骨架遇到同题号的人工页会跳过，并在任务队列里登记 `wrong-refine`，让 AI 把内容补进已有页而不是新开一页。

```bash
tools/build_skeletons.py --dry-run
```

### validate_schema.py

守三条硬纪律的机器闸门：

| 纪律 | 检查什么 |
|---|---|
| **分值** | `full`/`lost` 为 null 时必须 `scorePending: true`；反之亦然。缺分值又不标记 → 统计会把「没统计」误当「没失分」 |
| **溯源** | `sourceRaw`（字符串或数组）必须真实存在于 RAW/ 且已登记 manifest |
| **原件** | manifest 记录的 sha256 必须与文件一致。**不一致时会比对 Git HEAD 自动区分两种情况**：HEAD 一致 → 只是记录错了，可自动订正；HEAD 也不一致 → 原件真被改过，拒绝自动修复 |

额外检查：模块满分之和 = 试卷总分、逐题失分之和 = 总失分、错因在五类之内、已判错因但没附中间步骤判读。

覆盖的**数据入口**共 5 个（漏掉任何一个入口，该入口的数据就等于无约束）：

| 入口 | 查什么 |
|---|---|
| `data/raw-manifest.json` | sha256 与文件一致 |
| `data/wrong/` | 分值纪律、溯源 |
| `data/exams/` | 分值纪律、**模块 got 不得与本模块确定失分题矛盾** |
| `data/tasks/` | 标记 done 的任务，其 `fields` 在数据层不得为空；`meta.pending` 与实际待办数一致 |
| `data/resources/` | `count` 对条数、`stats` 三项之和恒等、`fetched` 对实际 rawFile 数、**`unchanged` 即报错** |

另有 4 项跨层闸门：

- **`check_cause_enum()`** — 错因五类枚举在 7 处定义点（方法论表格 / `CAUSES` / 两个 `<select>` / `app` 的 `ci` 着色表 / README 示例 / 生成器 `CAUSE_TONE`）必须逐字一致。缺项会静默退化成灰色 chip，**属「看起来正常、实际失效」**。
- **`check_embedded_snapshots()`** — 页面内嵌的数据副本（如试卷地图页的 `const DATA`）必须与数据层**逐字节相同**。副本一旦漂移，页面不会报错，只会让内容静默过期。
- **`check_no_fabricated_score()`** — 页面不得对**数据层 `got` 为 null 的模块**给出 `X/Y` 实得分数（那些模块是纪律已判定「无卷面依据」的）。**只审这些模块**是有意收窄：扫全站 `X/Y` 会把数学比例 2:3、选择项 A. 3/2、题号 35/36 全误报。合法引用必须在**同一句**内写明为何不折算——窗口开太宽会让上一段的免责说明替下一段的违规数字豁免。
- **派生量恒等** — 能被算出来的数字不要手写。`meta.stats` 由 `papers` 现算，三项之和恒等于 `count`。

> ⚠️ **闸门出假阳性 = 把真错误淹掉。** 上线前先跑一次看它报什么：已踩过三次坑（`startswith` 匹配模块名让「I」误命中「III」凭空多报 2 条；把筛选器哨兵值 `value=""`／文本「全部」当枚举外值误报；扫全站 `X/Y` 误报 9 条数学/题号/基线分）。
> ⚠️ **回退法自证**：改完闸门要手动制造违规，确认它**立刻**抓得出；抓不出说明检查根本没生效。**同轮要验正反两面**——既验「违规能抓出」，也验「合法内容不误报」。

```bash
tools/validate_schema.py           # 有错误 exit 1
tools/validate_schema.py --warn    # 有问题也 exit 0
tools/validate_schema.py --no-sha  # 跳过 sha256 全量重算（快）
```

### fix-manifest-sha.sh

订正 manifest 里的错指纹。**带安全阀**：只在 Git HEAD 那版指纹 == 当前文件指纹时才订正（即文件没被动过，错的只是记录）。若 HEAD 也不一致则拒绝修复——一键「修好」会把证据被篡改的现场抹平。

```bash
tools/fix-manifest-sha.sh --dry-run
```

---

## 自动同步

### 方案 A：launchd 定时任务（推荐）

plist 模板在 `tools/launchd/com.myschoolbuddy.pipeline.plist.template`，用 `__REPO__` 占位绝对路径（换机器重跑脚本即可）。

两套触发器缺一不可：

- **WatchPaths** —— 目录变化时立刻跑
- **StartInterval 1800** —— 每 30 分钟兜底（合盖休眠时 launchd 不空转，唤醒后补跑）

只有 WatchPaths → 休眠期间丢的事件永远补不回来；只有定时 → 合盖时基本不跑，用户以为没生效。

```bash
tools/install-launchd.sh install      # 安装（自动替换 __REPO__）
tools/install-launchd.sh status       # 状态 + 最近日志 + 与远端差异
tools/install-launchd.sh kick         # 手动触发一次
tools/install-launchd.sh test-watch   # 实测 WatchPaths 是否真会触发
tools/install-launchd.sh uninstall    # 卸载
```

> ⚠️ **本 IDE/沙箱内的 shell 无法向 launchd 注册服务**（返回 `Bootstrap failed: 5`，且**不会真正生效**）。plist 已生成到 `~/Library/LaunchAgents/`，需在真正的「终端」App 里执行：
>
> ```bash
> launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myschoolbuddy.pipeline.plist
> launchctl print gui/$(id -u)/com.myschoolbuddy.pipeline | head -5
> ```
>
> 装完请用 `launchctl print` 确认，**不要只信 `launchctl load` 的返回值**——实测受限环境下它会返回成功但服务根本没注册。

### 方案 B：WorkBuddy 自动化（已配置）

即使 launchd 装不上，同步也不会停——WorkBuddy 自动化「mySchoolBuddy 教学内容补全」每 2 小时检查一次 `data/tasks/pending.json`，补全教学内容并跑 `pipeline.sh` 完成校验、提交、推送。

---

## 目录

```
tools/
├── pipeline.sh                    主管道
├── ingest-raw.sh                  原件入库
├── build_skeletons.py             骨架生成
├── validate_schema.py             数据校验
├── fix-manifest-sha.sh            指纹订正
├── install-launchd.sh             定时任务部署
├── assets/coach.css               讲义样式（从精讲页抽取，保证视觉一致）
└── launchd/*.plist.template       定时任务模板
```

## 原则

- **不做 OCR 自动解析**。手写试卷识别不可靠，错了的分数比没有分数更危险。
- 无第三方依赖，只用 macOS 自带 `sips` / `shasum` + python3 标准库。
- 每个脚本支持 `--help`。
- 只读脚本不改文件；改数据的脚本先备份到 `data/_local/`。
- **脚本必须实测通过才算完成**。bash 在 macOS 3.2 下的坑都已踩过：
  - `mapfile` 不存在 → 用 `while read`
  - `set -u` 下空数组展开报 `unbound variable`
  - 含中文的路径当 shell 变量传给 `python -c` 会被截断 → 元数据走文件传递
  - `awk '/pixel/'` 匹配两个字段导致维度错位 → 分别匹配 `pixelWidth` / `pixelHeight`
  - `tee | tail` 导致 SIGPIPE → 锁残留、管道静默失效（已改为先落盘再读）
