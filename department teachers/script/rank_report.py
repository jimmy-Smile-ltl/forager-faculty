#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排名模式 30 分钟表格汇报 (2026-08-28)。
读取 logs/_ranked_state.json + output/ 目录标记 + 失败队列, 输出:
  1) 批次状态行(第N轮sweep / 本批覆盖 / 已完成/总数)
  2) 当前批内前 15 校进度表(名次|大学|院系|完成|教师数|队列|待办)
  3) 全局合计
用法: uv run python script/rank_report.py
"""
import csv, io, json, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "output"
LOGS = BASE / "logs"
RANK_FILE = BASE / "script" / "school_rank.json"
STATE_FILE = LOGS / "_ranked_state.json"


def main():
    try:
        rank = json.load(open(RANK_FILE, encoding="utf-8"))
    except Exception:
        rank = []
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    try:
        q = json.load(open(LOGS / "_fix_loop_queue.json", encoding="utf-8"))
    except Exception:
        q = {}

    sweep = st.get("sweep", 1)
    attempted = st.get("attempted", {})  # {校: [院系名]}
    rank_idx = {s: i + 1 for i, s in enumerate(rank)}

    # ── 全局聚合 ──
    tot_dept = tot_done = tot_skip = tot_fail = tot_zombie = tot_teacher = tot_queue = tot_verified = 0
    school_stats = {}  # 校 -> dict
    for sdir in sorted(OUT.iterdir()):
        if not sdir.is_dir() or sdir.name.startswith((".", "_")):
            continue
        school = sdir.name
        d_n = d_done = d_skip = d_fail = d_zombie = d_verified = 0
        t_cnt = 0
        for d in sdir.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            d_n += 1
            # 标记互斥计数: fix_done > skip > zombie(774 个目录存在双标记, 避免重复计数)
            if (d / ".fix_done").exists():
                d_done += 1
            elif (d / ".skip_mark").exists():
                d_skip += 1
            elif (d / ".fail_mark").exists():
                d_fail += 1
            elif (d / ".zombie_mark").exists():
                d_zombie += 1
            try:
                if (d / "test_report.json").exists():
                    j = json.load(open(d / "test_report.json", encoding="utf-8"))
                    if j.get("status") in ("verified_small_center",) and j.get("passed"):
                        d_verified += 1
            except Exception:
                pass
            tc = d / "teachers.csv"
            if tc.exists():
                try:
                    with open(tc, "r", encoding="utf-8") as f:
                        lines = [ln for ln in f if ln.strip()]
                    t_cnt += max(0, len(lines) - 1)  # 去表头, 空文件不计
                except Exception:
                    pass
        if d_n:
            school_stats[school] = dict(dept=d_n, done=d_done, skip=d_skip, fail=d_fail,
                                        zombie=d_zombie, verified=d_verified, teacher=t_cnt)
            tot_dept += d_n
            tot_done += d_done
            tot_skip += d_skip
            tot_fail += d_fail
            tot_zombie += d_zombie
            tot_verified += d_verified
            tot_teacher += t_cnt

    tot_queue = sum(len(v) for v in q.values())
    # 只统计有真实目录的队列条目(无目录=幻影条目, 既不可执行也不占待办)
    def _has_dir(school, dept):
        return (OUT / school / dept).is_dir()
    queue_nonz = sum(1 for s, v in q.items() for n, it in v.items()
                     if not (isinstance(it, dict) and it.get("zombie")) and _has_dir(s, n))
    # 队列僵尸(跨批次 attempts 超限终态, 2026-08-31 MAX_QUEUE_ATTEMPTS):
    # 属"已定论"而非"待处理" — 计入僵尸, 不再重复显示为队列/待办
    queue_zombie_n = sum(1 for s, v in q.items() for n, it in v.items()
                         if isinstance(it, dict) and it.get("zombie") and _has_dir(s, n))
    tot_zombie += queue_zombie_n
    tot_queue = queue_nonz
    # 待采 = 院系 - (fix_done + skip + 僵尸) - 可执行队列(队列单独列, 不重复计)
    pending = tot_dept - tot_done - tot_skip - tot_fail - tot_zombie - queue_nonz

    # ── 批次状态行 ──
    print(f"🏆 排名全量采集 | 第{sweep}轮 sweep | 批次上限 800 | "
          f"院系 {tot_dept} | 完成 {tot_done} | 教师 {tot_teacher}")

    # ── 当前批内前 15 校(按排名) ──
    print("\n| 名次 | 大学 | 院系 | 完成 | 完成率 | 教师数 | 队列 | 待办 |")
    print("|----|------|-----|-----|-------|-------|-----|------|")
    shown = 0
    for s in rank:
        stt = school_stats.get(s)
        if not stt:
            continue
        if shown >= 15:
            break
        done = stt["done"]
        rate = f"{done / stt['dept'] * 100:.0f}%" if stt["dept"] else "-"
        qq = q.get(s, {})
        # 队列只显示可执行项(有目录); 队列僵尸计入僵尸口径(与全局一致)
        qn = sum(1 for n, it in qq.items()
                 if not (isinstance(it, dict) and it.get("zombie")) and (OUT / s / n).is_dir())
        zq = sum(1 for n, it in qq.items()
                 if isinstance(it, dict) and it.get("zombie") and (OUT / s / n).is_dir())
        pend = stt["dept"] - done - stt["skip"] - stt["fail"] - stt["zombie"] - zq - qn
        print(f"| {rank_idx[s]} | {s} | {stt['dept']} | {done} | {rate} | "
              f"{stt['teacher']} | {qn} | {pend} |")
        shown += 1
    print(f"| - | **合计** | **{tot_dept}** | **{tot_done}** | "
          f"**{tot_done / tot_dept * 100:.0f}%** | **{tot_teacher}** | "
          f"**{tot_queue}** | **{pending}** |")

    # ── 终态细分 ──
    print(f"\n终态: PASS(fix_done) {tot_done} | 小组织(verified) {tot_verified} | "
          f"SKIP {tot_skip} | FAIL终态 {tot_fail} | 僵尸 {tot_zombie} | "
          f"队列 {tot_queue}(非僵尸 {queue_nonz}) | 待采 {pending}")

    # ── 本批已提交/覆盖 (从状态 + full_run.log 的 📦 行) ──
    cov = {s: len(v) for s, v in attempted.items()}
    if cov:
        names = [s for s in rank if cov.get(s)]
        print(f"本批已提交: {sum(cov.values())} 院系 | 覆盖 {len(cov)} 校 | "
              f"{names[0]} → {names[-1]}")


if __name__ == "__main__":
    main()
