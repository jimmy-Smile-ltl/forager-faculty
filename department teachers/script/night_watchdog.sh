#!/bin/bash
# 夜间 watchdog: 独立于 Claude 会话, 保证全量批次持续运行到 fresh 清空
# 用法: nohup bash script/night_watchdog.sh > /dev/null 2>&1 &
# 逻辑: 每 5 分钟检查批次进程; 不存在 → fresh 清空则停, 否则重启(幂等);
#       连续 3 次快速失败(<5min 内退出, 网关异常) → 停止等待人工。
cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." || exit 1
LOG="logs/_watchdog.log"
BATCH_LOG="logs/_full_batch.log"
# 2026-09-10 浏览器操作审计: 与 util/browser_ops.py 同一日志文件同格式,
# 便于排查「创建/销毁是否太频繁」(用户指令)
BROWSER_OPS_LOG="logs/browser_ops.log"
browser_op() {  # $1=action $2=detail
  printf '%s [   shell] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$BROWSER_OPS_LOG" 2>/dev/null || true
}
# 2026-08-26: BROWSER_ONLY=1 → 只守护浏览器/代理/孤儿清理, 不守护批次
# (批次由 auto_resume.sh 双轨流水线管理, 避免两个"主人"抢批次)
BROWSER_ONLY="${WATCHDOG_BROWSER_ONLY:-0}"
# 2026-08-25 恢复: 加 --skip-queue — 队列难点院系(每校队列≥40 时 fresh 一个不选,
# 夜间 39023 批次 3h 零产出的根因)留给专项小批次, 全量批次先扫 977 fresh
ARGS="--all --per-school 40 --workers 20 --skip-queue"
fast_fails=0
last_start=0
echo "$(date '+%F %T') watchdog 启动 (pid $$)" >> "$LOG"
while true; do
  # 2026-08-24 孤儿 Chrome 清理: 批次 kill/重启后 DrissionPage 池浏览器
  # 失去父进程成孤儿 (ppid=1), 每实例 9-11 进程 × 数十实例 → 数百进程
  # 拖垮内存 (实测累计 421 个)。仅清 DrissionPage 池实例, 保留 9222(CDP
  # 常驻) 与 9333(瑞数常驻)。
  # 2026-08-27: 9222 也排除 — 实测 auto-launch(ChromiumPage 连接失败自动
  # 拉起 userData/9222 非 headless)被此清理 kill 造成"每 5 分钟 9222 死"
  # 循环(18:40 定案); 该实例虽非 watchdog 所启, 但已在服务批次, 清理它
  # 只会让 9222 空窗, 排除后由 smart_fetch 探测升级(/json/version)从根上
  # 不再触发 auto-launch。
  orphans=""
  for pid in $(ps -eo pid,ppid,args | awk '/Google Chrome/ && /DrissionPage\/userData/ && $2==1 {print $1}'); do
    if ! ps -o args= -p "$pid" 2>/dev/null | grep -qE "userData/(9333|9222)"; then
      orphans="$orphans $pid"
    fi
  done
  if [ -n "$orphans" ]; then
    echo "$(date '+%F %T') 清理孤儿 Chrome $(echo $orphans | wc -w | tr -d ' ') 个" >> "$LOG"
    kill -9 $orphans 2>/dev/null
    browser_op "kill" "orphan chrome x$(echo $orphans | wc -w | tr -d ' ')"
  fi
  # 2026-09-04 孤儿 playwright driver 清理: spider 子进程 180s 超时被杀后,
  # 其 connect_over_cdp 的 driver(node) 进程成孤儿(ppid=1), 每个占着到
  # 9222 的一个 ESTABLISHED ws 连接永不释放 → 连接饱和 → 新 connect 全超时
  # → 又产生新孤儿(恶性循环, 一晚三次, 连累健康系误判死系)。只清 ppid=1
  # 的真孤儿, 有活父进程(在用)的 driver 绝不碰。
  odrv=""
  for pid in $(ps -eo pid,ppid,args | awk '/playwright.*driver/ && $2==1 {print $1}'); do
    odrv="$odrv $pid"
  done
  if [ -n "$odrv" ]; then
    echo "$(date '+%F %T') 清理孤儿 playwright driver $(echo $odrv | wc -w | tr -d ' ') 个" >> "$LOG"
    kill -9 $odrv 2>/dev/null
    browser_op "kill" "orphan playwright driver x$(echo $odrv | wc -w | tr -d ' ')"
  fi
  # 2026-08-24 池化: 9222 CDP 常驻浏览器守护 — playwright/cdp 路径全依赖它,
  # 无监听则重启 (网络断/内存压力会杀 Chrome)。
  # 2026-08-27: --remote-allow-origins=* — Chrome 137+ 默认拒绝 CDP ws 的
  # 跨源 Origin (403), Chrome 自动更新到 151 后 9222 不带此参数导致
  # playwright/DrissionPage 全挂 (实测 403 Forbidden)。
  # 2026-08-27: --remote-debugging-address=127.0.0.1 — macOS Chrome 重启后可能
  # 只绑 IPv6 [::1]:9222, 而所有连接方(playwright connect_over_cdp / smart_fetch /
  # socket 探测)都走 127.0.0.1 → ECONNREFUSED, CDP 池静默失效(实测 18:23)。
  # 2026-09-02 浏览器按需启动+空闲回收(用户指令): 9222/9333 不再常驻保活 —
  # 空闲 BROWSER_IDLE_REAP(默认 1800s, 2026-09-10 由 180s 调长 — 渲染需求
  # 断续时 180s 造成「拉起→空闲被杀→再拉起」冷启动循环, 一天 7~20 轮)自动
  # 回收; 租用方(cdp_util.cdp_page / handleRuishu)连接失败会自启(参数与此前
  # 一致), 冷启动首请求慢 2-5s。
  # 活动时间戳: 各浏览器路径每次使用时 touch BROWSER_ACTIVITY_FILE。
  REAP="${BROWSER_IDLE_REAP:-1800}"
  ACTIVITY="${BROWSER_ACTIVITY_FILE:-/tmp/browser_activity.ts}"
  IDLE=99999
  if [ -f "$ACTIVITY" ]; then
    IDLE=$(( $(date +%s) - $(stat -f %m "$ACTIVITY") ))
  fi
  # 2026-09-15 资源守卫(用户指令: 结合CPU动态管理防卡死): red(内存压力>70%
  # 或 CPU>90%) 时无视空闲阈值立即回收全部项目浏览器 — 高负载时浏览器是
  # 最大内存/CPU 重量, 先释放再等恢复(需求来时租用方自启, 但 red 期间
  # cdp_util/drission_pool 已被守卫拒绝自启)
  RG_STATE="green"
  if [ "$RESOURCE_GUARD" != "0" ]; then
    RG_STATE=$(uv run python -c "
import sys; sys.path.insert(0, '..')
try:
    from util.resource_guard import state
    print(state(force=True))
except Exception:
    print('green')" 2>/dev/null | tail -1)
  fi
  # 9222: 空闲超阈值 → 回收; 活着但非 IPv4 绑定(2026-08-27 历史陷阱:
  # Chrome 重启后只绑 [::1] 致 CDP 池静默失效) → 视为坏实例回收, 需求来时
  # cdp_util 会带 --remote-debugging-address=127.0.0.1 重新拉起。
  if lsof -nP -iTCP:9222 -sTCP:LISTEN 2>/dev/null | grep -q "9222"; then
    if [ "$RG_STATE" = "red" ]; then
      echo "$(date '+%F %T') 资源守卫red 立即回收 9222 (内存压力高, 先释放后等恢复)" >> "$LOG"
      pkill -f "user-data-dir=/tmp/chrome_cdp_9222" 2>/dev/null
      browser_op "kill" "9222 资源守卫red立即回收"
    elif ! lsof -nP -iTCP:9222 -sTCP:LISTEN 2>/dev/null | grep -q "127.0.0.1"; then
      echo "$(date '+%F %T') 回收 9222 (非 IPv4 绑定, 坏实例, 需求来时自启)" >> "$LOG"
      pkill -f "user-data-dir=/tmp/chrome_cdp_9222" 2>/dev/null
      browser_op "kill" "9222 非IPv4绑定坏实例回收"
    elif [ "$REAP" != "0" ] && [ "$IDLE" -gt "$REAP" ]; then
      echo "$(date '+%F %T') 空闲回收 9222 CDP Chrome (空闲 ${IDLE}s > ${REAP}s)" >> "$LOG"
      pkill -f "user-data-dir=/tmp/chrome_cdp_9222" 2>/dev/null
      browser_op "kill" "9222 空闲回收 idle=${IDLE}s>${REAP}s"
    fi
  fi
  # 9333 瑞数浏览器: 空闲回收(handleRuishu 侧同有惰性回收, 互为兜底)
  if { [ "$RG_STATE" = "red" ] || { [ "$REAP" != "0" ] && [ "$IDLE" -gt "$REAP" ]; }; } && \
     lsof -nP -iTCP:9333 -sTCP:LISTEN > /dev/null 2>&1; then
    echo "$(date '+%F %T') 回收 9333 瑞数浏览器 (${RG_STATE}red 或 空闲 ${IDLE}s > ${REAP}s)" >> "$LOG"
    pkill -f "remote-debugging-port=9333" 2>/dev/null
    pkill -f "user-data-dir=$PWD/util/.chrome_profile" 2>/dev/null
    browser_op "kill" "9333 空闲回收 idle=${IDLE}s>${REAP}s"
  fi
  # 2026-09-08 跨进程窗口上限兜底(用户浏览器异常观察): _new_window 的全局
  # 计数在并发建窗时有竞态(6 spider 同时检查同建, 实测 6>4 窗口) — watchdog
  # 每 60s 周期兜底: 唯一 user-data-dir 数超上限即回收多余窗口(按目录杀,
  # 先出现的目录先杀; 活跃窗口被收后调用方按预算降级/重建, 总量恒有界)。
  GMAX="${DRISSION_GLOBAL_MAX_WINDOWS:-4}"
  WIN_DIRS=$(ps aux | grep -oE "user-data-dir=/[^ ]*DrissionPage/userData/[0-9]+" | sed 's/^user-data-dir=//' | sort -u)
  WIN_N=$(echo "$WIN_DIRS" | grep -c "DrissionPage" || true)
  if [ "$WIN_N" -gt "$GMAX" ]; then
    EXCESS=$((WIN_N - GMAX))
    KILLED=""
    for DIR in $WIN_DIRS; do
      if [ "$EXCESS" -le 0 ]; then break; fi
      pkill -9 -f "user-data-dir=$DIR" 2>/dev/null
      KILLED="$KILLED $(basename "$DIR")"
      browser_op "kill" "drission window $(basename "$DIR") over-cap"
      EXCESS=$((EXCESS - 1))
    done
    echo "$(date '+%F %T') DrissionPage 窗口 $WIN_N > 上限 $GMAX, 回收:$KILLED" >> "$LOG"
  fi
  # 2026-08-24 快代理转发器守护: DrissionPage 浏览器代理出口 (电脑重启后
  # 该进程不自动恢复 → 浏览器回退直连, 瑞数站单IP高频被风控)
  # 2026-09-03: PROXY_MODE=off(快代理失效期全直连) → 不重启转发器;
  # local_proxy.py 自身也有同款守卫(启动即退), 双保险防 517 反复出现。
  # 2026-09-03 修复: cut -d'#' -f1 剥行尾注释 — 此前 "off  # ..." 经 tr 拼成
  # "off2026-09-03..." ≠ off, 守卫失效, 每 60s 空重启转发器一次(启动即退循环)
  ENV_PROXY_MODE=$(grep -E "^PROXY_MODE=" "$(dirname "$0")/../../.env" 2>/dev/null | head -1 | cut -d= -f2 | cut -d'#' -f1 | tr -d ' ' | head -c 20)
  if [ "$ENV_PROXY_MODE" != "off" ] && ! lsof -nP -iTCP:15819 -sTCP:LISTEN > /dev/null 2>&1; then
    echo "$(date '+%F %T') 重启 local_proxy 转发器 (15819)" >> "$LOG"
    cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/../.." || exit 1
    nohup uv run python util/local_proxy.py > /tmp/local_proxy.log 2>&1 &
    cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." || exit 1
  fi
  # 2026-09-17 卡死守护(无条件执行, 四次同款实锤: 北交 bjtu spider 死循环
  # 持 GIL 饿死主线程心跳 → 内置无进展保护失效, 主进程 100% CPU 空转;
  # 且 BROWSER_ONLY=1 时批次守护段被跳过, 守护必须放在门之前):
  # 按 full_run.log mtime 判死 — >20 分钟无写入且批次活着 → 杀批自愈
  if pgrep -f "multi_school_batch.py" > /dev/null; then
    _now=$(date +%s)
    # 2026-09-17 误杀修正: 批次日志有两个 — 主批次 full_run.log / 僵尸轮
    # zombie_auto.log。守护须看"最近更新的那个"(max mtime), 只盯 full_run
    # 会把正常跑着的僵尸轮误判卡死(09:47/10:11 两次误杀实锤)
    _last_mtime=0
    for _lf in "logs/full_run.log" "logs/zombie_auto.log"; do
      if [ -f "$_lf" ]; then
        _mt=$(stat -f %m "$_lf" 2>/dev/null || echo 0)
        [ "$_mt" -gt "$_last_mtime" ] && _last_mtime=$_mt
      fi
    done
    if [ $_last_mtime -gt 0 ] && [ $((_now - _last_mtime)) -gt 1200 ]; then
      echo "$(date '+%F %T') 💀 批次卡死($((_now-_last_mtime))s 无写入), 杀批自愈" >> "$LOG"
      pkill -9 -f "multi_school_batch.py"; pkill -9 -f "spider.py"; pkill -9 -f "probe_site.py"
    fi
  fi
  # ── 批次守护段 (仅 BROWSER_ONLY=0; auto_resume 流水线时用 =1) ──
  if [ "$BROWSER_ONLY" = "1" ]; then
    # 2026-09-10 架构升级: 原「个人 Chrome 未运行 → 反清理项目浏览器」块
    # 整套删除 — 项目浏览器已改用独立 Chromium(不同 bundle ID, 不共享
    # macOS 单例), 与个人 Chrome 互不干扰; 反向清理曾造成 609 次/天的
    # 建窗↔杀窗互搏(09-09 事故日实测), 现已无存在意义。
    # 2026-09-03: 9222 陈旧 tab 回收 — spider 子进程超时被杀致 playwright
    # page 永不 close, tab 泄漏推高内存(实测 36→44 个/10min, swap 81%);
    # 只关同 URL 10min 不变/blank 2min 的 tab, 活跃渲染不受影响。
    REAP_OUT=$(uv run python script/reap_cdp_tabs.py 2>/dev/null | tail -1)
    if [ -n "$REAP_OUT" ]; then
      echo "$(date '+%F %T') $REAP_OUT" >> "$LOG"
    fi
    sleep 60  # 2026-09-02: 300→60 — 空闲回收粒度更贴近 3 分钟阈值
    continue
  fi
  if pgrep -f "multi_school_batch.py" > /dev/null; then
    fast_fails=0
  else
    # 2026-09-04 双主循环防护: auto_resume 流水线在岗时批次归它管, watchdog
    # 即使非 BROWSER_ONLY 启动也绝不拉批 — 02:58 实锤(杀批#18后2min空窗,
    # watchdog 按 ARGS 拉起 --all 批, 与 03:00 auto_resume 拉起的 --ranked
    # 批并存抢 worker/LLM/浏览器, 35 并发超授权)。auto_resume 死后本守护仍兜底。
    if pgrep -f "auto_resume.sh" > /dev/null; then
      sleep 300
      continue
    fi
    # 全量完成检测: fresh(未尝试且不在队列) == 0 → 停止
    fresh=$(python3 -c "
import json, pathlib
q = json.load(open('logs/_fix_loop_queue.json', encoding='utf-8'))
base = pathlib.Path('output'); f = 0
for s in base.iterdir():
    if not s.is_dir() or s.name.startswith(('.','_')): continue
    for d in s.iterdir():
        if not d.is_dir() or d.name.startswith(('.','_')): continue
        if (d/'.fix_done').exists() or (d/'.skip_mark').exists(): continue
        if d.name not in q.get(s.name, {}): f += 1
print(f)" 2>/dev/null)
    if [ "$fresh" = "0" ]; then
      echo "$(date '+%F %T') ✅ fresh=0 全量扫描完成, watchdog 退出" >> "$LOG"
      exit 0
    fi
    # 连续快速失败保护(LLM 健康检查不过等)
    now=$(date +%s)
    if [ $((now - last_start)) -lt 300 ]; then fast_fails=$((fast_fails+1)); else fast_fails=0; fi
    if [ "$fast_fails" -ge 3 ]; then
      echo "$(date '+%F %T') ⛔ 连续3次快速失败(网关异常?), 停止重启等待人工" >> "$LOG"
      exit 1
    fi
    echo "$(date '+%F %T') 重启批次 (fast_fails=$fast_fails, fresh=$fresh)" >> "$LOG"
    nohup uv run python script/multi_school_batch.py $ARGS >> "$BATCH_LOG" 2>&1 &
    last_start=$(date +%s)
  fi
  sleep 300
done
