#!/bin/sh
# 把进程外看门狗装成 launchd 定时任务（每分钟一次）。
#
# 用法：
#   sh scripts/install-watchdog.sh            # 装 + 立刻起
#   sh scripts/install-watchdog.sh uninstall  # 卸
#   sh scripts/install-watchdog.sh log        # 看最近的记录
#
# ⚠️ **这是改你机器上的东西**（往 ~/Library/LaunchAgents 写一个 plist 并加载它），
# 所以脚本不会自己跑 —— 要装得你敲这一行。卸掉就是 uninstall 那行。
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
LABEL="com.qidian.watchdog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PY" ]; then
  echo "找不到 $PY —— 先在这个仓库里建 venv（python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'）" >&2
  exit 1
fi

case "${1:-install}" in
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已卸载（plist 也删了）。"
    exit 0
    ;;
  log)
    tail -n 30 "$REPO/.qidian/watchdog.log"
    exit 0
    ;;
esac

mkdir -p "$REPO/.qidian"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$REPO/scripts/watchdog.py</string>
    <string>--once</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$REPO/.qidian/watchdog.launchd.log</string>
  <key>StandardErrorPath</key><string>$REPO/.qidian/watchdog.launchd.log</string>
  <!-- 看门狗自己挂死时要能退出来，别留个僵尸占着 label -->
  <key>ExitTimeOut</key><integer>30</integer>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

echo "已装：$PLIST（每 60 秒一次）"
echo "  看记录：sh scripts/install-watchdog.sh log"
echo "  卸掉：  sh scripts/install-watchdog.sh uninstall"
echo
echo "先手工确认它判得对再装（下面这条应该立刻返回）："
echo "  $PY $REPO/scripts/watchdog.py --once"
