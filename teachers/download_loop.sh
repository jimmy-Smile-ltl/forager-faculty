#!/bin/bash
# 后台下载循环（2026-08-31）: 每轮 download_stage --all 增量补缺（名单更新自动纳入）。
# 2026-09-03 修改: 轮间休眠 600s→10s — 用户要求"保证每分钟都有 html 下载",
# 不等待; 已采系在轮内秒过(缺0写flag), 未采系才真下载, 连续轮转。
# 不依赖 LLM。用法:
#   nohup ./download_loop.sh >> logs/download_loop.log 2>&1 &
# 停止: kill $(pgrep -f download_loop.sh)（脚本本体, 否则600s复活造成双主循环）
# 2026-09-03: PROXY_MODE=off 全直连 — 快代理全站失效(517/curl56/000),
# 实测直连全 200。代理恢复需切回: 删除下行环境变量。
cd "$(dirname "$0")" || exit 1
echo "[$(date '+%F %T')] download_loop 启动 PID=$$ (PROXY_MODE=off 全直连)"
ROUND=0
while true; do
  ROUND=$((ROUND + 1))
  echo "[$(date '+%F %T')] ── 第 ${ROUND} 轮 --all --teacher-workers 5 --rate-limit 0.15 ──"
  PROXY_MODE=off uv run python download_stage.py --all --teacher-workers 5 --rank-order \
      --rate-limit 0.15
  echo "[$(date '+%F %T')] 第 ${ROUND} 轮结束, 休眠 10s"
  sleep 10
done
