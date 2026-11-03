#!/usr/bin/env python3
"""采集进度统计 + 30 分钟汇报 checkpoint (2026-09-02 用户指令: 每 30 分钟
以表格汇报进展 — 完成进度 / 总任务 / 本轮 30 分钟完成多少)。

扫描 output/<学校>/<院系>/ 目录的终态标记:
  .fix_done=PASS/LEGAL_EMPTY  .fail_mark=FAIL  .skip_mark=SKIP  .zombie_mark=僵尸
教师数 = 各院系 teachers.csv 行数之和(减表头)。
checkpoint 存 logs/_report_checkpoint.json, 每次运行输出与上轮的 delta。

用法: uv run python script/collection_stats.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

CKPT = LOGS / "_report_checkpoint.json"
_TERMINALS = (".fix_done", ".fail_mark", ".skip_mark", ".zombie_mark")


def scan() -> dict:
    total = done = fail = skip = zombie = teachers = 0
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        for d in s.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            total += 1
            if (d / ".fix_done").exists():
                done += 1
            elif (d / ".fail_mark").exists():
                fail += 1
            elif (d / ".skip_mark").exists():
                skip += 1
            elif (d / ".zombie_mark").exists():
                zombie += 1
            t = d / "teachers.csv"
            if t.exists():
                try:
                    with open(t, encoding="utf-8") as f:
                        teachers += max(0, sum(1 for _ in f) - 1)
                except Exception:
                    pass
    return {"total": total, "done": done, "fail": fail,
            "skip": skip, "zombie": zombie, "teachers": teachers}


def main():
    now = scan()
    prev = {}
    try:
        prev = json.load(open(CKPT, encoding="utf-8"))
    except Exception:
        pass
    now["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CKPT.write_text(json.dumps(now, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    settled = now["done"] + now["fail"] + now["skip"]
    fresh = now["total"] - settled - now["zombie"]
    pct = 100.0 * now["done"] / now["total"] if now["total"] else 0.0
    spct = 100.0 * settled / now["total"] if now["total"] else 0.0
    prev_at = prev.get("at", "首轮")
    print(f"at={now['at']}  (上轮 checkpoint: {prev_at})")
    print(f"total={now['total']} done={now['done']}({pct:.1f}%) settled={settled}"
          f"({spct:.1f}%) fail={now['fail']} skip={now['skip']} "
          f"zombie={now['zombie']} fresh={fresh} teachers={now['teachers']}")
    if prev:
        dd = {k: now[k] - int(prev.get(k, 0))
              for k in ("total", "done", "fail", "skip", "zombie", "teachers")}
        print(f"delta: done+{dd['done']} fail+{dd['fail']} skip+{dd['skip']} "
              f"zombie+{dd['zombie']} teachers+{dd['teachers']}")
    else:
        print("delta: 首轮, 无对照")


if __name__ == "__main__":
    main()
