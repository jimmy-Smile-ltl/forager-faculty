#!/bin/bash
# 自愈流水线 v5 (2026-08-28 排名模式 — 用户指令: 按大学排名逐校处理):
# 1) 健康探针(LLM_READY) → 2) 排名全量采集(--ranked, 清华→北大→… 按
#    school_rank.json 排名逐校累加未完成院系, 每批 BATCH_CAP 上限) →
# 3) 批次内部断点续传, 退出后回探针循环; 排名采集全部完成(exit=2)则停止。
# 背景: rescan_verified 专项(13例)只是 SKIP 冰山一角(449 个标记已全清),
# 修复后的 fix_one(同组分类/AJAX/多入口重生)对旧 SKIP 大部分能采出,
# 独立专项不再必要 — 全部并入全量 fresh。
# 2026-09-02: 僵尸自动消化轮 — 正常批次跳过僵尸, 每轮排名批次结束后若有僵尸
# 且无专项在跑, 自动拉起小配额 --retry-zombies 专项(历史 bug 误伤僵尸如
# _KB_LOCK 事故 ~100 个不会自动重跑 — 教训)。
BASE="20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400cd "20 20 12 61 79 80 81 701 33 98 100 204 250 395 398 399 400dirname "-e")/.." && pwd)"
LOG="$BASE/logs/auto_resume.log"
PROBE="$BASE/logs/health_probe_run.log"

log() { echo "$(date +%H:%M:%S) $1" >> "$LOG"; }

# ── 2026-09-20 卡死事故根修#2: pidfile 单例 — 已有存活实例直接退出,
# 防双启动(互拉爆炸源头之一: 关闭复查不彻底 + 新旧实例并存) ──
_PIDF="/tmp/auto_resume.pid"
if [ -f "$_PIDF" ]; then
  _old=$(cat "$_PIDF" 2>/dev/null)
  if kill -0 "$_old" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$_PIDF"

# ── 浏览器守护 (2026-08-26): 9222 CDP 常驻浏览器 + 15819 快代理 + 孤儿清理。
# 缺它 → 池化失效 → 浏览器路径每次新建实例(历史上 421+ Chrome 进程拖垮内存)。
if ! pgrep -f "night_watchdog.sh" > /dev/null; then
  WATCHDOG_BROWSER_ONLY=1 nohup bash "$BASE/script/night_watchdog.sh" > /dev/null 2>&1 &
  log "已拉起浏览器守护(browser-only watchdog)"
fi

# ── token 探测守护 (2026-08-29): 每 240s 强制全量探测刷新 Redis ok TTL(300s),
# 保持 collect_teacher._pool_active_count 读到 13 个新鲜 ok → LLM 并发容量恒 120,
# 打破"容量掉→worker空转→少探测→新鲜ok更少→容量更低"恶性循环(72 worker 下
# 放大为 CPU 0% + spider 0)。no_quota/invalid 长 TTL 不会被误重探。
if ! pgrep -f "token_probe_loop.sh" > /dev/null; then
  nohup bash "$BASE/script/token_probe_loop.sh" > /dev/null 2>&1 &
  log "已拉起 token 探测守护"
fi
# ── 30分钟监控循环守护 (2026-09-12 用户指令「监控要正常运行」): 与 watchdog/
# token 同款自愈 — 只要 auto_resume 活着, progress_loop 必然活着(会话级 cron
# 会随 Claude 会话消亡, 这个 nohup 循环是独立监控的唯一可靠载体)。
if ! pgrep -f "progress_loop.sh" > /dev/null; then
  nohup bash "$BASE/script/progress_loop.sh" > /dev/null 2>&1 &
  log "已拉起 30 分钟监控循环守护"
fi

# 全量并发: 用户 2026-08-27 指令"并发继续小幅上升(耗资源的是浏览器, LLM/requests 便宜)" → 25→30;
# 2026-08-28 用户指令"并发扩大10" → 30→40 (浏览器槽位: 窗口池 3×6=18 + CDP new_page 12 = 30,
# 超槽 worker 排队等浏览器, LLM 层不受钳制 — 内存/槽位允许时取上限值)
# 2026-08-29 用户指令"并发太小, 充分利用资源加速": 机器 8核/8GB, 采集 CPU 0%、内存余量 45%,
# LLM cap 120(18 token × 15) 远高于 worker 数 → 瓶颈在 worker 数 → 40→72(IO bound, 8核×9)。
full_workers() {
  # 2026-08-31 用户指令"5个并发开启全量采集": 院系级 worker 72→5
  # 2026-09-02 用户指令"增加并发, 同时压减僵尸": 5→12(历史恢复参数 12-16 区间,
  # 与 LLM_SEM=24/浏览器30槽/8核机器匹配; 另有僵尸专项 6 并发并行, 合计 ~18)
  # 2026-09-02 用户指令"恢复吧, 15个并发": 12→15(僵尸轮 3 并行, 合计 18 ≤ LLM_SEM 24)
  # 2026-09-02 用户指令"恢复全面采集, 30个并发"(浏览器空闲回收省内存):
  # 15→30 — LLM 层仍受 LLM_SEM=24 钳制, 浏览器按需启动+180s 空闲回收后
  # 槽位压力大降; 用户授权: 运行中若卡顿可自行下调并发。
  # 2026-09-02 23:45 下调 30→20: LLM 网关持续降级 1.5h+(生成截断/注入重试,
  # 3 轮 done +3/+6/+0), 按用户授权下调至 20 减网关压力(仍受 LLM_SEM=24 钳制,
  # 主要降 fetch/浏览器并发); 网关恢复后如需可再上调。
  # 2026-09-03 11:15 用户指令下调 20→5: 白天网关仍间歇波动(波间小抖), 用户明确
  # 要求 5 并发小步低频策略; 需重启 auto_resume 生效(bash 不重读脚本)。
  # 2026-09-03 17:20 用户指令"并发调整到 15"(西交封锁搁置后要看到产出):
  # 5→15 — 历史验证 12-16 为稳定区间(LLM_SEM=24/浏览器30槽/8核机器),
  # 配合 blocked 搁置+1800s 单院系超时+15min 无进展保护。需重启 auto_resume。
  # 2026-09-04 10:0x 用户指令"重启本阶段全量采集, 并发数为5": 15→5 —
  # 与 teachers 项目 download_loop(教师详情 10 workers)并行共享机器资源,
  # 且 09:45 机器刚重启过(重启前负载 14-29), 降并发减资源争抢。需重启 auto_resume。
  # 2026-09-04 14:1x 用户指令"并发调整为10, 全面重启全量采集": 5→10 —
  # 提速四项修复+LLM_TIMEOUT 420→120 落地后, 网关间歇挂起问题已缓解,
  # 10 并发仍在历史稳定区间(12-16)内、与 LLM_SEM=24/浏览器30槽匹配。需重启 auto_resume。
  # 2026-09-04 19:0x 用户指令"20个workers, 但浏览器要少启动太占内存,
  # 及时关闭浏览器": 10→20 — 浏览器层配套收紧(DRISSION_EXTRA_BROWSERS 12→3,
  # BROWSER_IDLE_REAP 180→120s), LLM 层仍受 LLM_SEM=24 钳制, 主要提
  # fetch/提取并发。需重启 auto_resume(bash 不重读脚本)。
  # 2026-09-07 用户指令重启全量采集"15个并发": 20→15 — 重采清单937院系
  # (URL缺失>30% + <20人)与未采集任务按校排名优先, 15 在历史稳定区间
  # (12-16)内、与 LLM_SEM=24/浏览器30槽匹配。需重启 auto_resume。
  # 2026-09-08 用户指令"worker改为30, 晚上deepseek半价; 明天早上10点改成
  # 10个并发省钱": 改时间自调整 — 白天 10:00-19:00 全价期 10 并发省钱;
  # 其余时段(夜间半价期) 30 并发冲量。bash 函数每次循环调用时重新求值,
  # 无需重启即可在次日 10:00 自动切 10。注意: 时窗如需调整改此处区间。
  # 2026-09-09 用户指令"白天15, 夜间40": 10/30 → 15/40 — 白天提一档兼顾
  # 产出, 夜间半价期加大冲量; 时窗 10:00-19:00 不变。
  # 2026-09-12 用户指令"白天并发40, 周末deepseek半价": 周末(周六/周日)
  # 全天半价 → 白天也 40; 工作日白天全价 15 不变, 夜间恒 40。
  # 2026-09-14 用户指令「20并发, 控制浏览器打开数量」: 固定 20 全时段 —
  # 浏览器零开启(BROWSER_TIERS=off 纯CPU链), 20 并发与 8核机器/LLM_SEM 匹配。
  # 2026-09-16 用户指令「重采1124低质量院系, 15个并发」: 固定 15 全时段 —
  # A(fail136)+B(学院级漏采670)+C(URL缺失318) 全量重采期。
  # 2026-09-17 用户指令「按 deepseek 分时定价自动切并发」: 官方定价=空闲时段
  # 半价, 高峰时段=北京时间周一至周五 9:00-12:00、14:00-18:00。高峰 15 省钱,
  # 空闲(夜间/午休/周末) 40 冲量。函数每次循环调用重算 → 批间自动切换无需
  # 重启(运行中的批不中断, 下批生效); CPU 支撑前提=浏览器零开启
  # (BROWSER_TIERS=off 纯CPU链, 2026-09-11 机器卡死教训即浏览器时代)。
  # 2026-09-20 用户指令「并发修改为25」: 全时段统一 25(不分峰谷,
  # 覆盖此前周末15/高峰15/空闲40的分时逻辑)。
  # 2026-10-08 用户指令「太慢了/并发」提速: 25→30 — 历史用户授权 30 档
  # (2026-09-02), 配合 4h 重试闸(batch 不再每轮重烧硬院系)内存可控;
  # 8GB 机器警戒线 40(2026-09-11 卡死教训), 30 是安全上限。
  echo 30
}

# 2026-08-29 用户指令"尽可能把资源用起来": 浏览器 CDP tab 12→18(+6 槽≈900MB,
# 8GB 内存余 ~1.2GB 安全), 与兜底窗口池 12 合计 30 浏览器槽; LLM_SEM 24→36 在 .env。
# 2026-08-29 二次提速: 内存核算 swap 已用 3GB/4GB(74%) → tab 不再扩;
# workers 72→128 翻倍 fetch 并发(IO 等待线程内存小), LLM 36/浏览器 30 槽不变。
export DRISSION_CDP_MAX_TABS=18

while true; do
  # 2026-09-18 周末离岗互监督: 守护循环死了也要拉起(守护侧每60s互查本循环;
  # 本检查每批次循环一次 — 两者其一存活即互相恢复)
  # 2026-09-20 卡死事故根修: 原模式锚定 "bash script/" 前缀只匹配相对形态,
  # 而本脚本用 $BASE 绝对路径拉起守护 → 每轮都看不见守护 → 每轮重复拉起
  # (20秒互拉fork爆炸68进程, CPU 100%, 用户被迫重启设备)。改为只锚定文件名,
  # 相对/绝对路径形态均可见。
  if ! pgrep -f "auto_resume_guard\.sh$" > /dev/null; then
    nohup bash "$BASE/script/auto_resume_guard.sh" > /dev/null 2>&1 &
    log "已拉起 auto_resume 守护"
  fi
  # ── 全量采集: 探针 LLM_READY 门控(网关截杀波保护) → 跑全量 ──
  log "准备全量采集(workers=$(full_workers)), 先探针确认服务端"
  : > "$PROBE"
  uv run --project "$BASE" python "$BASE/script/llm_health_probe.py" 2>&1 | tee -a "$PROBE" | grep -E --line-buffered "READY|BAD |EXC|LLM_READY" >> "$LOG"
  if ! grep -q "LLM_READY" "$PROBE"; then
    # 2026-09-04 波期转下载(用户指令): 批内 R1 下载不依赖 LLM, 网关挂时照样
    # 产出 — 旧「未READY空等」门控让批退出后的整个波期无批可跑(12:44 实锤:
    # 批 exit=0 后探针失败 → 空转等波过)。照常启动, 批内波期守卫接管
    # (下载落盘 + 评估推迟), 波过自动补评估。
    log "探针未READY, 仍启动批(波期转下载模式, 批内守卫接管)"
  else
    log "LLM_READY → 启动排名全量采集(workers=$(full_workers), 按大学排名逐校累加, 每批 ${BATCH_CAP:-800})"
  fi
  cd "$BASE" || exit 1
  uv run python script/multi_school_batch.py --ranked --cap "${BATCH_CAP:-800}" --workers "$(full_workers)" >> logs/full_run.log 2>&1
  rc=$?
  log "排名全量采集退出(exit=$rc), 30s后检查是否续跑"
  if [ "$rc" = "2" ]; then
    # 2026-09-03 用户指令: 僵尸最后消化——fresh/队列未清完前不再并行拉僵尸轮
    # (原逻辑每轮批次后并行拉起, 抢 worker/LLM 配额拖慢 fresh);
    # 现在只在排名全量采集全部完成(exit=2)后做一次性收尾消化。
    # 2026-09-04 19:1x 周末稳定运行修正: 原逻辑僵尸轮结束后 break 停循环 —
    # 17:23 与 19:07 两次实测排名模式 exit=2(队列全僵尸化)后自愈循环停止,
    # 采集静默死亡(用户周末必踩)。改: 僵尸轮结束后不再 break, 继续循环
    # (排名空跑 exit=2 → 僵尸消化轮 → 30s → 复查 IP → 再循环), 僵尸轮是
    # 周末唯一的工作来源, 必须常转; 僵尸轮并发 3→6 提吞吐(浏览器 EXTRA=3
    # 钳制浏览器路径, LLM 层 LLM_SEM=24 不受影响)。
    log "🏁 排名全量采集完成(无未完成院系且队列已清) → 僵尸消化轮(循环持续)"
    # 2026-09-09 用户指令"夜间40并发": 僵尸轮是剩余存量(121僵尸+68瑞数)的
    # 唯一工作源, 6 并发让它一轮跑 2-3 小时占住循环 — 夜间提到 15 加速消化
    # (白天 10 时档由时间窗函数全量 worker 控制, 僵尸轮固定 15 不随动)。
    # 2026-09-17 分时定价: 僵尸轮与排名批同随 full_workers 分时切换
    # (现在队列全部僵尸化, 僵尸轮是唯一工作源, 半价期 40 才有意义)。
    DRISSION_CDP_MAX_TABS=18 uv run --project "$BASE" python \
      script/multi_school_batch.py --retry-zombies --all \
      --per-school 5 --workers "$(full_workers)" >> logs/zombie_auto.log 2>&1
    log "僵尸消化轮结束, 继续自愈循环(周末稳定运行)"
  fi
  # ── IP 封禁错峰复查 (2026-09-02 缺点#4 改进): fail_ip_blocked 院系冷却期
  # (默认 6h) 后自动复活重采, 南师大类封禁解封即自愈; 账本上限 3 次防乱轰。
  # 纯本地标记操作, 无网络无 worker, 批次间静默执行。
  log "IP 封禁错峰复查(冷却 ${IP_BLOCK_COOLDOWN:-6h}/上限 ${IP_BLOCK_MAX_ATTEMPTS:-3} 次)"
  uv run --project "$BASE" python "$BASE/script/retry_ip_blocked.py" --apply \
    >> "$BASE/logs/ip_block_retry.log" 2>&1
  # (2026-09-10 架构升级: 浏览器守卫窗口复查已删 — 独立 Chromium 下
  # 无「等个人 Chrome 窗口」挂起机制, retry_browser_wait 整套退役)
  sleep 30
done
