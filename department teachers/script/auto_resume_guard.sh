#!/bin/bash
# auto_resume 守护 (2026-09-18 用户周末离岗, 授权自主恢复):
# 每60s检查 auto_resume.sh 存活, 死了拉起 — 采集管道最后一道保险。
#
# 2026-09-20 卡死事故根修: 原 pgrep 模式锚定 "bash script/auto_resume\.sh$"
# 只匹配相对路径形态, 而 auto_resume.sh 用 $BASE 绝对路径拉起本守护 —
# 双方互相看不见(相对 vs 绝对) → 互拉 fork 爆炸(20秒68进程, CPU 100%,
# 用户被迫重启设备)。修复:
# 1) pgrep 模式只锚定文件名 "auto_resume\.sh$"(任意路径前缀均匹配)
# 2) 本守护自身 pidfile 单例(防重复拉起)
# 3) 多实例只记日志不动手(杀主循环会孤儿化批次进程)
BASE="20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." && pwd)"
LOG="$BASE/logs/_autoresume_guard.log"
PIDF="/tmp/autoresume_guard.pid"

# ── 单例: pidfile 存活即退出(启动瞬时竞争由 flock 兜底) ──
if [ -f "$PIDF" ]; then
  _old=$(cat "$PIDF" 2>/dev/null)
  if kill -0 "$_old" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$PIDF"

while true; do
  # 只锚定文件名: 相对(bash script/auto_resume.sh)与绝对路径形态均可见
  _n=$(pgrep -f "auto_resume\.sh$" | grep -v "^$$\$" | wc -l | tr -d ' ')
  if [ "$_n" = "0" ]; then
    echo "$(date '+%F %T') auto_resume 缺失, 拉起" >> "$LOG"
    cd "$BASE" && nohup bash script/auto_resume.sh > /dev/null 2>&1 &
  elif [ "$_n" -gt 1 ]; then
    # 多实例只记日志不动手(杀主循环会孤儿化批次进程, 双批次风险) —
    # 留给 Claude 会话 cron 或人工处置
    echo "$(date '+%F %T') 警告: auto_resume 多实例($_n 个)" >> "$LOG"
  fi
  sleep 60
done
