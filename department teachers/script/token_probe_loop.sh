#!/bin/bash
# 常驻 token 探测循环 (2026-08-29):
#   每 240s 强制全量探测一次, 刷新 Redis 中 status=ok 的 TTL(300s), 保持
#   SimpleLLM 实时池"新鲜 ok"≈13 → collect_teacher._pool_active_count 稳定
#   → LLM 并发容量恒 120(13×15, cap) → 打破"容量掉→worker空转→少探测→
#   新鲜ok更少→容量更低"的恶性循环 (72 worker 下放大为 CPU 0% + spider 0)。
#   no_quota(月初TTL)/invalid(86400s TTL) 不会被误重探, 只刷新 ok。
BASE="20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." && pwd)"
TM="20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400cd "/.." && pwd)/token manager"
LOG="$BASE/logs/token_probe_loop.log"
while true; do
  # 2026-10-24 修复: nohup 启动的 shell PATH 无 /opt/homebrew/bin, uv 找不到
  # (rc=127 每轮失败, 探测循环坏死但进程存活, keepalive 的 pgrep 检不出) —
  # 必须用 uv 全路径
  cd "$TM" && /opt/homebrew/bin/uv run --project "$BASE" python token_pool.py check >> "$LOG" 2>&1
  rc=$?
  echo "[$(date +%H:%M:%S)] probe round done (rc=$rc)" >> "$LOG"
  sleep 240
done
