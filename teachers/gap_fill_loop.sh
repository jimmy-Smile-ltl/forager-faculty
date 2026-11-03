#!/bin/bash
# 缺口补采循环（2026-10-27 用户指令「尽可能全部下载html, 之前遗漏的尽可能补上」）
# 17:16 重启修订: 校际交错会打散类别排序, 挂起系(瑞数/死站)长期占满 worker
# 拖垮吞吐(实测15min仅9文件) → 热清单(活页+瑞数 139 院系)先跑收敛,
# 之后全量 881 扫一次尾巴(死链/不可达由墙探针快速标死), 再热清单收尾。
# 停止:   kill $(pgrep -f gap_fill_loop.sh) 且 pkill -f "upstream_refresh.py --mode" 且 pkill -f "_fetch_dept.py"
# 环境:   .env 自动加载（PROXY_MODE=dps 转发器 15819 已在跑; RS_DISABLE=0）
#         浏览器(9222/9333)按需自拉(flock 锁纪律), 无需预启动
cd "$(dirname "$0")" || exit 1
LOG="logs/_gap_fill_run.log"
log() { echo "[$(date '+%F %T')] $1" >> "$LOG"; }

log "===== 缺口补采启动(热清单优先) PID=$$ ====="

run_pass() {
  local list="$1" pass="$2"
  log "── 第 ${pass} 遍 (${list}) ──"
  uv run python scripts/upstream_refresh.py --mode full \
      --list "logs/${list}.tsv" --dept-workers 8 >> "$LOG" 2>&1
  log "── 第 ${pass} 遍结束 ──"
}

hot_count() {
  local total=0 n=0
  while IFS=$'\t' read -r s d; do
    [ "$s" = "学校" ] && continue
    n=$(ls "../tearchers raw data/$s/$d" 2>/dev/null | wc -l)
    total=$((total + n))
  done < logs/_gap_fill_hot.tsv
  echo $total
}

# ── 阶段1: 热清单先行（活页 100 + 瑞数 27 + 跳转 12, 快速出成果）──
run_pass _gap_fill_hot 1
PREV=$(hot_count)
log "热清单第1遍后文件总数: $PREV"

# ── 阶段2: 热清单循环重试（瑞数罐重建/浏览器自拉后补收, 最多4遍）──
MAX_HOT=4
PASS=1
while [ $PASS -le $MAX_HOT ]; do
  sleep 300  # 5min 冷却: 瑞数罐重建/限速恢复
  run_pass _gap_fill_hot $((PASS + 1))
  NOW=$(hot_count)
  GAIN=$((NOW - PREV))
  log "热清单第 $((PASS + 1)) 遍后: $NOW (净增 $GAIN)"
  if [ "$GAIN" -le 0 ]; then
    log "🏁 热清单零进展, 收敛"
    break
  fi
  PREV=$NOW
  PASS=$((PASS + 1))
done

# ── 阶段3: 全量 881 扫尾一遍（死链/不可达探针标死快, 捡漏活页/其他）──
run_pass _gap_fill_targets 1

# ── 阶段4: 热清单再收一轮 ──
sleep 300
run_pass _gap_fill_hot 9

log "===== 缺口补采收官 ====="
