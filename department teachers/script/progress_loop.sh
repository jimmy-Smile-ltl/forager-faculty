#!/bin/bash
# 30分钟进度记录循环 (2026-09-11 用户指令「任务监控」): 独立于 Claude 会话,
# 每 30 分钟跑一次 progress_report.py 追加写历史文件 — 会话结束/重启后仍可查。
# 注意: 机器重启后本循环需重新拉起(与 run_targeted.sh 同规则)。
cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." || exit 1
LOG="logs/_progress_history.log"
echo "$(date '+%F %T') 监控循环启动" >> "$LOG"
while true; do
  echo "===== $(date '+%F %T') =====" >> "$LOG"
  uv run --project "$(dirname "$0")/../.." python script/progress_report.py >> "$LOG" 2>&1
  sleep 1800
done
