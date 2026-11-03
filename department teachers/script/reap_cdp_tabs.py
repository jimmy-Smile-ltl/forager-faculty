#!/usr/bin/env python3
"""9222 CDP 陈旧 tab 回收器 (2026-09-03)。

背景: spider 子进程 180s 超时被杀 → playwright page 永不 close → 9222 tab 泄漏
(实测 36 个 page target, 每个 renderer 100-400MB, 是 8GB 机内存爬升/swap 告急的
主因)。idle-reap 只管"整个浏览器空闲", 采集连续运行时浏览器恒热, 管不到 tab 泄漏。

策略(安全第一, 只关可证伪的陈旧 tab):
- 每轮快照 /json, 按 tab id 记录 "URL 首次不变时间";
- 同一 URL 持续不变 > 600s (10min) → 关闭 — 活跃渲染 URL 数秒~数分钟内必变
  (spider 预算 180s, 僵尸预算 900s 内最后一次渲染也早于 10min 前结束);
- about:blank 持续 > 120s → 关闭 — 租用后从未导航 = 孤儿租用;
- 新 tab id 首次出现只登记不关闭 (新鲜期保护);
- 9222 不在 / 无响应 → 退出 0 (idle-reap 已回收浏览器, 无需动作)。

用法: python3 script/reap_cdp_tabs.py [--dry-run]
状态文件: /tmp/cdp_tab_state.json (id → {url, since_ts})
由 night_watchdog.sh 每轮调用 (BROWSER_ONLY=1 每 60s / 批守护模式每 300s)。
"""
import json, os, sys, time, urllib.request
from pathlib import Path

# util/ 入 sys.path — browser_ops 审计日志 (2026-09-10)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "util"))

STATE = Path("/tmp/cdp_tab_state.json")
SAME_URL_TTL = 600   # 同 URL 不变 10min → 关
BLANK_TTL = 120      # about:blank 2min → 关
DRY = "--dry-run" in sys.argv


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    try:
        targets = _get("http://127.0.0.1:9222/json")
    except Exception:
        return  # 9222 无响应 (已回收或启动中): 无动作
    pages = {t["id"]: t.get("url", "") for t in targets if t.get("type") == "page"}
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    now = time.time()
    to_close = []
    for tid, url in pages.items():
        prev = state.get(tid)
        if prev and prev.get("url") == url:
            since = prev.get("since", now)
        else:
            since = now  # 新 tab 或 URL 变化: 重置计时
        blank = url in ("about:blank", "")
        if (blank and now - since > BLANK_TTL) or (now - since > SAME_URL_TTL):
            to_close.append((tid, url, now - since))
        else:
            state[tid] = {"url": url, "since": since}
    # 清理已消失 tab 的状态
    for tid in list(state):
        if tid not in pages:
            del state[tid]
    closed = 0
    for tid, url, age in to_close:
        if not DRY:
            try:
                _get(f"http://127.0.0.1:9222/json/close/{tid}", timeout=3)
                closed += 1
                from browser_ops import browser_op
                browser_op("reap", f"9222 stale tab age={age:.0f}s {url[:50]}")
            except Exception:
                pass  # 已被并发关闭/浏览器重启: 幂等忽略
        state.pop(tid, None)
        print(f"  {'[dry-run] ' if DRY else ''}关闭陈旧 tab {tid[:8]} "
              f"age={age:.0f}s {url[:60]}", flush=True)
    if not DRY:
        try:
            STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    print(f"[reap-cdp-tabs] pages={len(pages)} closed={closed} "
          f"tracked={len(state)}", flush=True)


if __name__ == "__main__":
    main()
