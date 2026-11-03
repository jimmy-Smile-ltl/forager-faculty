#!/bin/bash
# 批次启动守卫(2026-10-10 用户指出调度缺陷后固化):
# 事件驱动架构下, 批次完成通知与 2h 兜底 cron 可能竞争触发续跑 —
# pgrep 单例守卫防双启动(互拉爆炸教训的批次版)。
# 用法: bash script/launch_next_batch.sh
cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." || exit 1
if pgrep -f "run_improve_batch.py" > /dev/null; then
  echo "批次已在运行, 跳过启动"
  exit 0
fi
# 批次序号: 用时间戳命名日志避免覆盖
TS=$(date +%m%d_%H%M)
# 2026-10-16 周末模式(用户下班指令「尽量使用资源」): 批次 workers 8 → 12。
# 历史边界: 12-16 均跑过安全(用户曾定12/16), 资源守卫(浏览器侧)独立
# 按负载自适应; LLM 侧 token 池 12 key 支撑 24+ 并发。负载 >25 时
# 资源守卫降级浏览器路径, 批次 CPU 链不受影响。
# 2026-10-24 用户指令「资源利用不充分,增加并发」: 12 → 16(负载实测 3,
# 瑞数户在破解链等待时 CPU 空转, 16 workers 提高吞吐)。
# 2026-10-24 晚 用户指令「36个并发」: 16 → 36。启动 2 分钟即内存风暴
# (load 48.9, swap 107% 超总量, 机器濒临卡死) — 启动风暴 36 户同时
# 抓页+LLM+probe 链把内存打爆, 立即杀批回落。36 不可行。
# 2026-10-24 23:48 回落 20(当年「夜间40并发卡死」事故后的安全上限,
# 且当前机器 swap 基线已被 9222 Chromium 堆到 79%+, 余量不足):
# 卡死强制重启的代价 >> 并发收益。
# 2026-10-25 用户指令「增加到24 + 完成一批后自行考虑增加并发」:
# 基准 24, 每批启动前按 swap/负载 自适应调档 —
#   swap>85% → 12 | swap>70% → 16 | load1>25 → 16
#   load1<4 且 swap<3000M → 28 (低负载+内存余量自动加档)
# 注: 真正约束是内存不是CPU — 36并发事故=启动风暴打爆swap(107%),
# 故加档必须 swap 余量充足; 9222 profile 已清(基线 -2.4G)。
# 2026-10-25 晚 用户指令「速度有点慢」: 基准 28, 加档上限提到 36 —
# 36 事故复盘: 当时基线 swap 79%+风暴1.4G=107%; 现在基线 46%, 同风暴
# 只到 ~73%, 36 已安全(每30min汇报盯 swap>85% 立即杀批回落12)。
export BATCH_WORKERS=28
# 2026-10-27 9333 构建级握手404(CloakBrowser Chromium 145 vs DrissionPage
# 4.1.1.4 专项待修): off 冻结浏览器依赖户, 纯CPU户正常跑; 修好后删此行为 on
export BROWSER_TIERS=off
_SWAP_MB=$(sysctl vm.swapusage 2>/dev/null | awk '{gsub(/M/,"",$7); print $7}')
# 2026-10-25 修复: macOS uptime 输出 "load averages: 4.64 4.72 4.52" 无逗号,
# 旧 cut -d, 取整串拼成 "4.644.724.52", 字符串比较 ">25" 对 4.x 开头的
# 负载误判降档 — 改取第一个空白分隔字段
_LOAD1=$(uptime | awk -F'load averages:' '{print $2}' | awk '{print $1}')
if [ -n "$_SWAP_MB" ] && [ -n "$_LOAD1" ]; then
  _SCALE=$(awk -v sw="$_SWAP_MB" -v ld="$_LOAD1" 'BEGIN{
    if (sw > 4400) print 12;
    else if (sw > 3600) print 16;
    else if (ld > 25) print 16;
    else if (ld < 6 && sw < 2400) print 36;
    else if (ld < 4 && sw < 3000) print 32;
    else print 28;}')
  if [ "$_SCALE" != "28" ]; then
    export BATCH_WORKERS=$_SCALE
    echo "[$(date '+%m-%d %H:%M:%S')] 自适应并发: swap=${_SWAP_MB}M load=${_LOAD1} → workers=${_SCALE}"
  fi
fi
# caffeinate -i: 批次运行期间防系统空闲休眠(用户放假无人值守需求)
nohup caffeinate -i /opt/homebrew/bin/uv run python script/run_improve_batch.py 1 "$TS" > "logs/_impb_${TS}_run.log" 2>&1 &
echo "批次已启动 (logs/_impb_${TS}_run.log, 报告 _impb${TS}_report.json)"
