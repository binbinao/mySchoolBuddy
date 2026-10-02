#!/bin/bash
# install-launchd.sh — 部署 / 卸载内容管道的 launchd 定时任务
#
# 为什么用模板 + 部署脚本，而不是直接把 plist 提交到仓库：
# plist 里的路径是绝对的（/Users/<用户名>/...），不同机器不一样。
# 模板里用 __REPO__ 占位，部署时替换成本机实际路径。
# 这样仓库里能版本管理这份配置，换机器重跑一次脚本即可。
#
# 用法：
#   tools/install-launchd.sh install     # 安装并立即跑一次验证
#   tools/install-launchd.sh uninstall   # 卸载
#   tools/install-launchd.sh status      # 查看状态
#   tools/install-launchd.sh reinstall   # 先卸后装（改了配置后用）
#   tools/install-launchd.sh kick        # 手动触发一次（launchctl kickstart）
#   tools/install-launchd.sh test-watch  # 验证 WatchPaths 是否真的会触发

set -o pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.myschoolbuddy.pipeline"
TPL="$ROOT/tools/launchd/$LABEL.plist.template"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$ROOT/data/_local/logs"

ok()   { echo "✓ $*"; }
warn() { echo "⚠️  $*"; }
die()  { echo "✗ $*" >&2; exit 1; }

# 诊断当前能否操作 launchd 用户域。
#
# 实测踩过的坑：在 WorkBuddy/IDE 的受限 shell 里跑 launchctl bootstrap，
# 会返回 "Bootstrap failed: 5: Input/output error"，
# 但 **plist 本身完全正确**（plutil -lint 通过、路径也对）。
# 用最小 plist（只 echo）同样失败，证明不是配置问题，
# 而是当前进程无权向 launchd 用户域注册服务。
#
# 判据：`launchctl list` 返回空 = 当前上下文看不到任何用户服务，
# 说明处在受限环境。此时任何注册都会失败，必须让用户到真正的终端里执行。
diagnose_launchd() {
  local listed
  listed=$(launchctl list 2>/dev/null | grep -cv '^[[:space:]]*$')
  if [ "$listed" -eq 0 ]; then
    return 1
  fi
  return 0
}

do_install() {
  [ -f "$TPL" ] || die "找不到模板 $TPL"
  mkdir -p "$AGENTS" "$LOGDIR"

  # __REPO__ 里有 & 之类特殊字符的可能性不大，但路径含空格时 plist 仍能工作，
  # 用 sed 替换并转义 & 防止被当作替换串。
  ESC_ROOT=$(printf '%s' "$ROOT" | sed 's/[&|]/\\&/g')
  sed "s|__REPO__|$ESC_ROOT|g" "$TPL" > "$DST" || die "生成 plist 失败"

  # 校验生成的 plist 语法
  plutil -lint "$DST" >/dev/null 2>&1 || die "生成的 plist 语法有误：$DST"

  if ! diagnose_launchd; then
    echo "⚠️  当前 shell 无法操作 launchd 用户域。" >&2
    echo "" >&2
    echo "    plist 已生成且语法正确：$DST" >&2
    echo "" >&2
    echo "    但当前进程处在受限上下文（launchctl list 返回空），" >&2
    echo "    向 launchd 注册服务会返回 error 5 且**不会真正生效**。" >&2
    echo "" >&2
    echo "    请打开「终端」App，手动执行这一条：" >&2
    echo "" >&2
    echo "      launchctl bootstrap gui/\$(id -u) \"$DST\"" >&2
    echo "" >&2
    echo "    装好后验证：" >&2
    echo "      launchctl print gui/\$(id -u)/$LABEL | head -5" >&2
    echo "" >&2
    echo "    注意：不要相信本脚本之前打印的「✓ 已安装」——" >&2
    echo "    launchctl load 在受限环境下会返回成功但实际没注册，" >&2
    echo "    必须用 launchctl print 确认服务真的存在。" >&2
    return 1
  fi

  # 先卸载旧的再加载新的，避免 launchctl 报 "service already loaded"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
    launchctl unload "$DST" 2>/dev/null || true

  # macOS 13+ 用 bootstrap；老系统 fallback 到 load
  if launchctl bootstrap "gui/$(id -u)" "$DST" 2>/dev/null; then
    ok "已 bootstrap（macOS 13+ 方式）"
  elif launchctl load "$DST" 2>/dev/null; then
    ok "已 load（旧系统方式）"
  else
    die "launchctl 加载失败。手动排查：launchctl print gui/$(id -u)/$LABEL"
  fi

  launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

  # 关键：不 print 确认就不算装成功。
  # 受限环境里 bootstrap/load 可能"返回成功但没注册"，这是实测踩过的坑。
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    ok "已确认服务注册成功"
  else
    warn "命令返回成功，但 launchctl print 查不到该服务——实际未注册"
    return 1
  fi

  echo ""
  ok "已安装：$LABEL"
  echo "  plist：$DST"
  echo "  日志：$LOGDIR/pipeline.log"
  echo "  触发：目录变化时立刻 + 每 30 分钟兜底"
  echo ""
  echo "  验证： tools/install-launchd.sh status"
  echo "  手动跑： tools/pipeline.sh --force"
}

do_uninstall() {
  if launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then
    ok "已停止运行"
  elif launchctl unload "$DST" 2>/dev/null; then
    ok "已停止运行（旧系统）"
  else
    warn "未发现运行中的任务（可能已卸载）"
  fi
  if [ -f "$DST" ]; then rm -f "$DST"; ok "已删除 $DST"; fi
  # 不删日志——日志是排查问题的唯一线索，留着
}

do_status() {
  echo "── 运行状态 ──"
  # 必须用 print 而不是 list 判断：list 在受限环境下返回空但不代表没装，
  # print 直接问 launchd 这个服务是否存在，才是可信判据。
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    ok "服务已注册"
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|program|pid|last exit code" | head -6
    echo ""
    if [ -f "$DST" ]; then
      echo "  plist：$DST"
    else
      warn "服务在跑但 plist 不见了（可能已被手动删除）"
    fi
  else
    warn "服务未注册。若要启用，在真正的「终端」里执行："
    echo "    launchctl bootstrap gui/\$(id -u) \"$DST\""
    echo ""
    echo "  （本 IDE/沙箱内的 shell 无法向 launchd 注册服务，会报 error 5）"
  fi
  echo ""
  echo "── 最近日志 ──"
  if [ -f "$LOGDIR/pipeline.log" ]; then
    tail -15 "$LOGDIR/pipeline.log"
  else
    warn "还没有日志，说明从未运行过"
  fi
  echo ""
  echo "── 最近提交 ──"
  git -C "$ROOT" log --oneline -5 2>/dev/null
  echo ""
  echo "── 与远端差异 ──"
  git -C "$ROOT" status -sb 2>/dev/null | head -3
}

do_kick() {
  ok "手动触发…"
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null && ok "已触发" || \
    warn "kickstart 失败，直接跑 tools/pipeline.sh --force"
  sleep 2
  tail -8 "$LOGDIR/pipeline.log" 2>/dev/null
}

# 验证 WatchPaths 真的能触发：往监听目录里写一个临时文件，
# 观察 launchd 是否在合理时间内启动了 pipeline。
# 这个测试很有必要——WatchPaths 在某些 macOS 版本 + 某些卷（外置盘、网络盘）
# 上会静默失效，只能实测发现。
do_test_watch() {
  echo "── WatchPaths 触发测试 ──"
  local before after mark
  before=$(wc -l < "$LOGDIR/pipeline.log" 2>/dev/null || echo 0)
  mark="$ROOT/RAW/其他材料/.watchtest-$$"
  echo "写入触发文件：$mark"
  touch "$mark"
  echo "等待 launchd 反应（最多 25 秒）…"
  for i in $(seq 1 25); do
    sleep 1
    after=$(wc -l < "$LOGDIR/pipeline.log" 2>/dev/null || echo 0)
    if [ "$after" -gt "$before" ]; then
      ok "已触发（第 ${i} 秒），WatchPaths 生效"
      rm -f "$mark"
      tail -6 "$LOGDIR/pipeline.log"
      return 0
    fi
  done
  rm -f "$mark"
  warn "25 秒内没有触发，WatchPaths 可能未生效"
  echo "  排查：launchctl print gui/$(id -u)/$LABEL | grep -A5 WatchPaths"
  echo "  兜底：StartInterval 仍在生效，最迟 30 分钟内会跑一次"
  return 1
}

CMD="${1:-status}"
case "$CMD" in
  install)   do_install;;
  uninstall) do_uninstall;;
  reinstall) do_uninstall; sleep 1; do_install;;
  status)    do_status;;
  kick)      do_kick;;
  test-watch) do_test_watch;;
  *) sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 1;;
esac
