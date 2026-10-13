#!/usr/bin/env bash
# 每日定时任务：补齐错过的交易日 -> 扫描 -> 静默更新表现跟踪。
#
# 重要：这个脚本本身不会"自己跑"。必须由 launchd / cron 在固定时间叫醒它。
# 如果到点时 Mac 是关机状态，launchd 和 cron 都不会补跑 —— 所以下面的
# `scan --catch-up` 会在下次运行时把缺的交易日按顺序补上。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PYTHON:-python3}"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$stamp] === 开始每日任务 ==="

# 想固定用某个参数方案，就在下面两处加上：  "$PYTHON" -m us_low_scan.cli --preset conservative scan --catch-up

# 1) 补齐错过的交易日（关机/睡眠/刚开机时第一次运行会在这里把缺的补上），再出最新一期
#    开机自启时网络可能还没就绪，失败就等 20 秒重试，最多 3 次
scan_status=1
for attempt in 1 2 3; do
  set +e
  "$PYTHON" -m us_low_scan.cli scan --catch-up
  scan_status=$?
  set -e
  if [ "$scan_status" -eq 0 ]; then
    break
  fi
  if [ "$attempt" -lt 3 ]; then
    echo "[$(date '+%F %T')] scan 失败（第 $attempt 次），20 秒后重试…"
    sleep 20
  fi
done
if [ "$scan_status" -ne 0 ]; then
  echo "[$(date '+%F %T')] scan 连续 3 次失败，退出码 $scan_status（详见上方错误信息）"
  exit "$scan_status"
fi

# 2) 静默记录"入选之后实际发生了什么"（用来验证规则，不是持仓管理）
#    想看的时候手动执行： python3 -m us_low_scan.cli outcomes
"$PYTHON" -m us_low_scan.cli outcomes >/dev/null 2>&1 || true

echo "[$(date '+%F %T')] === 完成 ==="

# 3) 通知：config.ini 里 [notify] enabled = true 时，上面的 scan 已经自动发送。
#    手动补发 / 调格式：  python3 -m us_low_scan.cli notify --dry-run
# 4) 网页仪表板：        python3 -m us_low_scan.cli serve --port 8765
