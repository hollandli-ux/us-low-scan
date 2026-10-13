#!/usr/bin/env bash
# 在 macOS 上注册每日定时任务（launchd 能正确处理时区与夏令时）。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.uslowscan.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${PYTHON:-$(command -v python3)}"
# 建议时间：上海/新加坡时间 05:30（美东收盘后约 45 分钟，夏令时约 05:30、冬令时约 06:30）
HOUR="${HOUR:-5}"
MINUTE="${MINUTE:-30}"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/tools/run_daily.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHON</key>
        <string>$PYTHON</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
        <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    </array>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

echo "已写入 $PLIST"
echo
echo "注意：launchd 只负责【到点叫醒这个脚本】。"
echo "  · 开机 / 登录后 -> 会立刻先跑一次（把落下的交易日补齐）"
echo "  · Mac 只是睡眠   -> 唤醒后 launchd 会把这次任务补跑掉"
echo "  · Mac 完全关机   -> 不会补跑；下次开机后手动运行或等下一次定时触发时，"
echo "                       scan --catch-up 会自动把缺的交易日补齐"
echo
# macOS 的隐私保护（TCC）不允许 launchd 启动的后台任务读取 ~/Documents、
# ~/Desktop、~/Downloads —— 项目放在这些目录里，定时任务会直接报
# "Operation not permitted"。这里提前拦一下，别等用户第二天发现没更新。
case "$PROJECT_DIR" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
    echo
    echo "⚠️  注意：项目在 macOS 受保护目录（Documents / Desktop / Downloads）里。"
    echo "    launchd 启动的后台任务**没有权限**读取这些目录，任务会失败并报："
    echo "        bash: $PROJECT_DIR/tools/run_daily.sh: Operation not permitted"
    echo
    echo "    解决办法（推荐）：把项目复制到不受保护的位置再装定时任务，例如"
    echo "        cp -R \"$PROJECT_DIR\" \"$HOME/us-low-scan\" && cd \"$HOME/us-low-scan\" && ./tools/install_launchd.sh"
    echo
    ;;
esac

echo "加载：  launchctl load -w \"$PLIST\""
echo "卸载：  launchctl unload -w \"$PLIST\""
echo "手动跑一次： launchctl start $LABEL"
