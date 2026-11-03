#!/usr/bin/env python3
"""30分钟进展汇报 (2026-09-08 用户指令): 新增完成学院/新增老师 + 健康检查。

快照 logs/_progress_state.json: {fix_done, teachers, at} — 每次运行对比增量
并回写。健康检查(2026-09-03 阈值): 增量骤降/归零即使进程活着也要诊断
(30min <50 或 连降50% / 连2周期<20 / fail+skip+zombie 占比>30%)。

用法: uv run python script/progress_report.py
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

STATE = LOGS / "_progress_state.json"


def scan() -> dict:
    done = 0
    teachers = 0
    fail = skip = zomb = wait_b = blocked = 0
    for sdir in OUT.iterdir():
        if not sdir.is_dir():
            continue
        for d in sdir.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            if (d / ".fix_done").exists():
                done += 1
                cf = d / "teachers.csv"
                if cf.exists():
                    try:
                        with open(cf, encoding="utf-8", errors="ignore") as f:
                            n = sum(1 for _ in f) - 1
                        teachers += max(0, n)
                    except Exception:
                        pass
            else:
                fail += (d / ".fail_mark").exists()
                skip += (d / ".skip_mark").exists()
                zomb += (d / ".zombie_mark").exists()
                wait_b += (d / ".wait_browser").exists()
                blocked += (d / ".blocked_mark").exists()
    return {"done": done, "teachers": teachers, "fail": fail, "skip": skip,
            "zombie": zomb, "wait_browser": wait_b, "blocked": blocked}


def proc_alive(name: str) -> bool:
    try:
        out = os.popen(f"pgrep -f {name}").read().strip()
        return bool(out)
    except Exception:
        return True


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = scan()
    prev = {}
    if STATE.exists():
        try:
            prev = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    d_done = cur["done"] - prev.get("done", 0) if prev else 0
    d_teachers = cur["teachers"] - prev.get("teachers", 0) if prev else 0
    cur["at"] = now
    json.dump(cur, open(STATE, "w"), ensure_ascii=False, indent=1)

    lines = [f"📊 采集进展 {now}",
             f"  完成学院: {cur['done']} (+{d_done} 本周期)",
             f"  老师总数: {cur['teachers']} (+{d_teachers} 本周期)",
             f"  未完成: fail {cur['fail']} | skip {cur['skip']} | zombie {cur['zombie']}"
             f" | 浏览器挂起 {cur['wait_browser']} | blocked {cur['blocked']}"]
    # 健康检查
    health = []
    # 2026-09-14 模式感知: 全量采集(auto_resume)按用户指令关闭期间, 由定向重采
    # (run_targeted)承接; 阈值口径切换为定向模式, 避免每周期误报两则假警报。
    # fresh_then_full: fresh池优先→完成后自动转全量 的过渡循环。
    targeted = proc_alive("run_targeted.sh") or proc_alive("fresh_then_full.sh")
    if not proc_alive("auto_resume.sh"):
        if targeted:
            health.append("ℹ️ 定向重采模式(全量按用户指令关闭)")
        else:
            health.append("⚠️ auto_resume 进程不在!")
    if not proc_alive("multi_school_batch.py"):
        health.append("⚠️ 无运行中批次(可能批次间间隔)")
    if prev:
        p_done = prev.get("done", 0)
        if d_done < 0:
            health.append(f"⚠️ 完成数倒挂 {d_done}(检查数据清理)")
        if targeted:
            # 定向模式: 存量是硬质量门槛, 0-3/周期为常态; 专项期允许大增量
            if d_done < 0:
                pass
        elif d_done < 50:
            health.append(f"⚠️ 30min 增量 {d_done} < 50(阈值), 查波动/卡死")
        elif d_done < p_done * 0.5:
            health.append(f"⚠️ 增量连降>50%({p_done}→{d_done})")
    if health:
        lines.append("  健康: " + " | ".join(health))
    else:
        lines.append("  健康: ✅ 正常")
    # 2026-09-09 用户指令: 各步骤耗时分析 — 近 150 条 timing 的分段平均,
    # 便于识别瓶颈(prep/faculty页/spider/eval/llm 哪段烧时间一眼可见)
    try:
        tf = LOGS / "_dept_timing.log"
        if tf.exists():
            _tl = tf.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-150:]
            _segs = {}
            for ln in _tl:
                for m in re.finditer(r"(R\d\.)?([A-Za-z_一-鿿]+):([\d.]+)s", ln):
                    k = m.group(2).replace("llm_done", "llm").replace("wave_abort", "abort")
                    _segs.setdefault(k, []).append(float(m.group(3)))
            if _segs:
                _top = sorted(((k, sum(v) / len(v), len(v)) for k, v in _segs.items()),
                              key=lambda x: -x[1])[:6]
                lines.append("  步骤耗时均值(近%d院系): " % len(_tl) +
                             ", ".join(f"{k}={a:.0f}s" for k, a, _n in _top))
    except Exception:
        pass
    print("\n".join(lines))


if __name__ == "__main__":
    main()
