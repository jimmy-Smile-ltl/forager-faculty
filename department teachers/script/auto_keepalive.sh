#!/bin/bash
# 批次保活器（2026-10-10 用户放假需求）: 检查批跑器是否存活, 空闲则自动
# 选批+启动。2026-10-10 批次10死锁实锤升级: 进程活着但0%CPU日志静默
# 28分钟(无心跳守护的批跑器死锁) — 加日志停滞自愈: 最新批次日志mtime
# 超30分钟未推进→杀批重启(假期无人值守场景的关键保障)。
cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." || exit 1
# 2026-10-14 用户指令「关闭后台测试任务, 专注解决6院系改进工作流」:
# 暂停标记存在时不续跑批次(专注改进期), 删除 logs/_PAUSE_TEST 即恢复
if [ -f logs/_PAUSE_TEST ]; then
  exit 0
fi
LOG="logs/_auto_loop.log"
ts() { date "+%m-%d %H:%M:%S"; }

# 2026-10-15 token_probe_loop 保活(DeepSeek崩溃事故补全): 电脑重启后常驻
# 探测进程死了没人拉 → Redis 里 net_fail 陈旧状态躺死 → 批跑器误判"token
# 全灭"纯模板空转一上午。探测循环每240s刷新token状态, keepalive 每15min
# 检查一次兜底拉起(重启自愈闭环)。
if ! pgrep -f "token_probe_loop.sh" > /dev/null; then
  echo "[$(ts)] token_probe_loop 死亡 → 拉起" >> "$LOG"
  nohup bash script/token_probe_loop.sh >> logs/token_probe_loop.log 2>&1 &
fi

if pgrep -f "run_improve_batch.py" > /dev/null; then
  # 停滞检测: 最新批次运行日志 mtime
  LATEST=$(ls -t logs/_impb_*_run.log 2>/dev/null | head -1)
  if [ -n "$LATEST" ]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$LATEST" 2>/dev/null || echo $(date +%s)) ))
    if [ "$AGE" -gt 1800 ]; then
      echo "[$(ts)] 批次停滞 ${AGE}s(>30min) → 杀批重启" >> "$LOG"
      pkill -9 -f "run_improve_batch.py"
      sleep 3
    else
      exit 0   # 批次在跑且日志新鲜, 什么都不做
    fi
  fi
fi
echo "[$(ts)] 批跑器空闲 → 自动续跑" >> "$LOG"
# 2026-10-13 反思(用户: RETRY很多压根没PASS): 波动期批次纯空转烧池 —
# 启动前探测, 波动期暂停(exit 0), 健康期才选批启动
if /opt/homebrew/bin/uv run python script/check_wave.py >> "$LOG" 2>&1; then
  echo "[$(ts)] 波动期暂停新批, 池子留健康期测" >> "$LOG"
  exit 0
fi
# 每次续跑都重新选批(select_batch 自带状态过滤: 已PASS/已测的自动跳过)
/opt/homebrew/bin/uv run python script/select_batch.py 150 >> "$LOG" 2>&1
# 2026-10-20 空池防空转: 池枯竭时(0户可选, 如 ip_blocked 全部冷却中)
# 跳过启动 — 否则每5分钟一次空批启动/退出循环(12:40-12:55 实锤4连空转)
_N_SEL=$(/opt/homebrew/bin/uv run python -c "import json;print(len(json.load(open('logs/_improve_batches.json'))[0]))" 2>/dev/null || echo 0)
if [ "$_N_SEL" = "0" ]; then
  echo "[$(ts)] 池空(0户可选), 跳过启动等冷却户过期" >> "$LOG"
  exit 0
fi
/opt/homebrew/bin/uv run python script/backfill_pass_status.py >> "$LOG" 2>&1
# 2026-10-26 名单不缩减自愈: 重跑可能把恢复种子缩水覆盖(上外新闻36→5实锤),
# 批间隙(无批次运行)合并旧管线名单缺失姓名 — 幂等只增不删, 不与批次写盘竞争
if ! pgrep -f "run_improve_batch.py" > /dev/null; then
  /opt/homebrew/bin/uv run python script/restore_roster.py >> "$LOG" 2>&1
  # 2026-10-29 RAW 孤儿回填自愈: 写盘路径覆盖丢失的孤儿恢复名自动补回
  /opt/homebrew/bin/uv run python script/backfill_raw_orphans.py >> "$LOG" 2>&1
fi
# 2026-10-13 harness自我进化: 批次完成后自动改进循环(规则匹配→应用→账本)
/opt/homebrew/bin/uv run python script/improve_cycle.py --apply >> "$LOG" 2>&1
bash script/launch_next_batch.sh >> "$LOG" 2>&1
