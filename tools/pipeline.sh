#!/bin/bash
# pipeline.sh — 内容管道主管道
#
# 一次跑完：原件入库 → 骨架生成 → 数据校验 → 提交 → 推送远端
#
# 设计上的三个要点（都是踩过坑换来的）：
#
#   1. **幂等**：没变化就什么都不做，不产生空提交、不触发推送。
#      定时任务会跑几百次，任何一次产生噪音都会让 git 历史失去可读性。
#
#   2. **自激保护**：本脚本会写 data/tasks/pending.json，而这个文件在
#      监听目录里。如果不加锁，定时器会被自己的输出唤醒，
#      再跑一次、再写一次……无限循环。LOCK 文件 + 冷却窗口双重防护。
#
#   3. **校验不阻断推送**：validate 报错误时仍然提交 RAW 和 JSON，
#      但拒绝推送。理由——数据有问题必须留下来让人看见，
#      悄悄跳过才最危险。同时用 MERGE 标记让人一眼知道有未解决的校验错误。
#
# 用法：
#   tools/pipeline.sh                  # 正常跑
#   tools/pipeline.sh --dry-run        # 全程预演，不 commit 不 push
#   tools/pipeline.sh --no-push        # 只提交不推送
#   tools/pipeline.sh --force          # 即使无变化也检查远端
#   tools/pipeline.sh --help

set -o pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

LOGDIR="$ROOT/data/_local/logs"
LOCKFILE="$ROOT/data/_local/pipeline.lock"
COOLDOWN=60   # 秒：距上次运行不足这个时间就直接退出

PY=""
for c in /Users/jiduobin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { echo "错误：找不到 python3" >&2; exit 1; }

DRY=0; NOPUSH=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1;;
    --no-push) NOPUSH=1;;
    --force)   FORCE=1;;
    --help|-h) sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0;;
    *) echo "未知参数：$a" >&2; exit 1;;
  esac
done

mkdir -p "$LOGDIR" "$ROOT/data/_local"
LOG="$LOGDIR/pipeline.log"

log()  { echo "$*" | tee -a "$LOG"; }
step() { log ""; log "── $* ──"; }

# ── 0. 并发锁 + 冷却窗口（防自激循环的核心）──────────────────
# macOS 自带 bash 3.2 没有 flock。用 mkdir 做原子锁（POSIX 保证 mkdir 原子性）。
#
# 实测踩过的坑：只写 trap 清理锁是不够的。管道里用了 `tee | tail`，
# tail 退出后本脚本会收到 SIGPIPE，trap 不保证执行，锁目录就残留了。
# 下一次运行看到锁在就跳过——管道从此再也不跑，而且没人知道为什么。
# 所以锁里必须写 PID：判断「锁是否有效」不能只看锁在不在，
# 要看锁里的进程还活着没有。死了就清掉重来。
LOCKDIR="$ROOT/data/_local/pipeline.lock.d"
LOCKSTALE=1800   # 秒：锁龄超过这个且进程不在，视为残留

lock_is_stale() {
  local age pid
  age=$(( $(date +%s) - $(stat -f%m "$LOCKDIR" 2>/dev/null || echo 0) ))
  pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    return 1   # 进程还活着 → 锁有效
  fi
  # 没有 PID 信息或进程已死：靠锁龄兜底，避免误抢刚建的锁
  [ "$age" -ge "$LOCKSTALE" ]
}

acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ > "$LOCKDIR/pid"
    trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT INT TERM HUP
    return 0
  fi
  if lock_is_stale; then
    log "🧹 清理残留锁（上次进程未正常退出）"
    rm -rf "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null || return 1
    echo $$ > "$LOCKDIR/pid"
    trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT INT TERM HUP
    return 0
  fi
  return 1
}

if ! acquire_lock; then
  lockpid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "?")
  lockage=$(( $(date +%s) - $(stat -f%m "$LOCKDIR" 2>/dev/null || echo 0) ))
  log "⏭  跳过：另一次管道正在运行（pid ${lockpid}，锁龄 ${lockage}s）"
  exit 0
fi

# 冷却窗口：定时器与 WatchPaths 可能同时触发
STAMP="$ROOT/data/_local/last-run"
if [ "$FORCE" = "0" ] && [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  now=$(date +%s)
  if [ $(( now - last )) -lt "$COOLDOWN" ]; then
    log "⏭  跳过：距上次运行仅 $(( now - last ))s（冷却 ${COOLDOWN}s），用 --force 可强制执行"
    exit 0
  fi
fi
date +%s > "$STAMP"

log "══════════════════════════════════════════════════════════"
log "mySchoolBuddy 管道 · $(date '+%Y-%m-%d %H:%M:%S')${DRY:+ [dry-run]}"

cd "$ROOT" || exit 1

# 子脚本输出：全量写日志，屏幕上只显示末尾若干行。
# 不用 `tee | tail`——那样 tail 先退出会给本脚本发 SIGPIPE，
# trap 不保证执行，锁目录残留，管道下次直接跳过自己。改成先落盘再 tail 文件。
run_step() {
  local n="$1"; shift
  local tmp="$LOGDIR/step.$$.tmp"
  "$@" > "$tmp" 2>&1
  cat "$tmp" >> "$LOG"
  tail -n "$n" "$tmp"
  rm -f "$tmp"
}

# ── 1. RAW 原件入库（压缩 + 建索引）─────────────────────────
step "1/4 RAW 原件入库"
if [ "$DRY" = "1" ]; then
  run_step 20 ./tools/ingest-raw.sh --dry-run
else
  run_step 30 ./tools/ingest-raw.sh
fi

# ── 2. 生成讲义骨架 ────────────────────────────────────────
step "2/4 生成讲义骨架"
if [ "$DRY" = "1" ]; then
  run_step 20 "$PY" tools/build_skeletons.py --dry-run
else
  run_step 30 "$PY" tools/build_skeletons.py
fi

# ── 3. 数据校验 ────────────────────────────────────────────
step "3/4 数据校验（分值/溯源/原件三条纪律）"
VALID_RC=0
VLOG="$LOGDIR/validate.$$.tmp"
"$PY" tools/validate_schema.py --quiet > "$VLOG" 2>&1 || VALID_RC=1
cat "$VLOG" >> "$LOG"
cat "$VLOG"
rm -f "$VLOG"

# ── 4. 提交与推送 ──────────────────────────────────────────
step "4/4 提交与推送"

# 待补任务数量（用于提交信息与提醒）
PENDING=0
if [ -f "$ROOT/data/tasks/pending.json" ]; then
  PENDING=$("$PY" -c "
import json
d=json.load(open('$ROOT/data/tasks/pending.json',encoding='utf-8'))
print(len([i for i in d.get('items',[]) if i.get('status')!='done']))
" 2>/dev/null || echo 0)
fi

# 只提交受管目录，避免把临时文件带进去
git add -A RAW data docs tools app README.md 2>/dev/null

if git diff --cached --quiet; then
  log "无变化，跳过提交"
  CHANGED=0
else
  CHANGED=$(git diff --cached --name-only | wc -l | tr -d ' ')
  # 统计本次新增了几份原件、几份错题
  NEWRAW=$(git diff --cached --name-only --diff-filter=A -- RAW | wc -l | tr -d ' ')
  NEWW=$(git diff --cached --name-only --diff-filter=AM -- data/wrong | wc -l | tr -d ' ')
  NEWE=$(git diff --cached --name-only --diff-filter=AM -- data/exams | wc -l | tr -d ' ')
  NEWDOC=$(git diff --cached --name-only --diff-filter=AM -- docs | wc -l | tr -d ' ')

  MSG="内容更新："
  PARTS=""
  [ "$NEWRAW" -gt 0 ] && PARTS="${PARTS}原件 +${NEWRAW}，"
  [ "$NEWW" -gt 0 ]   && PARTS="${PARTS}错题 ${NEWW} 条，"
  [ "$NEWE" -gt 0 ]   && PARTS="${PARTS}试卷 ${NEWE} 份，"
  [ "$NEWDOC" -gt 0 ] && PARTS="${PARTS}讲义 ${NEWDOC} 份"
  # 工具/脚本改动单独说明，否则只能写「派生数据与工具改动」这种含糊的话，
  # 三个月后回头看 git 历史根本想不起来当时改了什么。
  NEWTOOL=$(git diff --cached --name-only --diff-filter=AM -- tools | wc -l | tr -d ' ')
  [ "$NEWTOOL" -gt 0 ] && PARTS="${PARTS}工具 ${NEWTOOL} 个"
  [ -z "$PARTS" ] && PARTS="派生数据与工具改动"
  MSG="${MSG}${PARTS}"

  if [ "$VALID_RC" != "0" ]; then
    MSG="${MSG}
校验有错误：已提交但**未推送**，请先跑 tools/validate_schema.py 排查"
    # 留一个醒目的标记文件在仓库里，避免有人在不知情的情况下手动 push 掉
    printf '校验未通过：%s\n请运行 tools/validate_schema.py 查看详情。\n' "$(date '+%Y-%m-%d %H:%M')" \
      > "$ROOT/data/_local/VALIDATION-FAILED.md"
    git add -f "$ROOT/data/_local/VALIDATION-FAILED.md" 2>/dev/null
    DO_PUSH=0
  else
    rm -f "$ROOT/data/_local/VALIDATION-FAILED.md"
    DO_PUSH=1
  fi

  if [ "$PENDING" -gt 0 ]; then
    MSG="${MSG}
教学内容待 AI 补全 ${PENDING} 项（见 data/tasks/pending.json）"
  fi

  if [ "$DRY" = "1" ]; then
    log "dry-run：将提交 ${CHANGED} 个文件"
    git diff --cached --stat | tail -15
  else
    git commit -q -m "$MSG" 2>&1 | tee -a "$LOG"
    log "已提交：$MSG"
  fi
fi

# ── 5. 推送 ────────────────────────────────────────────────
if [ "$DRY" = "1" ]; then
  log "dry-run：跳过推送"
elif [ "$NOPUSH" = "1" ]; then
  log "--no-push：跳过推送"
elif [ "${DO_PUSH:-0}" = "0" ] && [ "$VALID_RC" != "0" ]; then
  log "⛔ 校验未通过，暂不推送（本地提交已保留）"
else
  # 先同步远端，避免非快进冲突
  if git fetch -q origin 2>>"$LOG"; then
    BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
    if [ "$BEHIND" -gt 0 ]; then
      log "远端有 ${BEHIND} 个新提交，先 rebase 再推"
      if git rebase origin/main >>"$LOG" 2>&1; then
        log "rebase 成功"
      else
        log "⚠️  rebase 冲突，已中止。手动处理：git rebase --abort && git status"
        exit 2
      fi
    fi
  fi

  if git rev-list --count origin/main..HEAD 2>/dev/null | grep -q '^0$'; then
    log "✓ 已是最新，无需推送"
  else
    if git push -q origin main 2>>"$LOG"; then
      log "✓ 已推送到 origin/main"
    else
      log "✗ 推送失败，详见 $LOG"
      exit 3
    fi
  fi
fi

log ""
log "完成 · $(date '+%Y-%m-%d %H:%M:%S')"
