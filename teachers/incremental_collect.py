#!/usr/bin/env python3
"""增量采集器（2026-08-31）——上游名单更新后只处理有新工作的院系。

逐院系差量判断: 名单中既非 ok 也非 no_url 的教师（新增 + failed +
pending_pdf + unknown）才需要处理; 无新工作的院系直接跳过。
每院系一个 extract_all_details 子进程（8 分钟墙钟预算, 超时杀掉进
下一系; 子进程内 10 分钟无进展看门狗兜底挂起）。

用法:
  uv run python incremental_collect.py --all                        # 全校增量
  uv run python incremental_collect.py --all --fetch-only           # 只下载 HTML/PDF
  uv run python incremental_collect.py --school 北京大学 --dept 光华管理学院
  uv run python incremental_collect.py --all --dept-budget-min 8

两阶段跑法: 先 --fetch-only 下载落盘, 后加 --reextract-llm 复用
本地文件提取（零抓取补 LLM 质量）。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from extract_all_details import list_schools, list_depts, load_teachers  # noqa: E402
from detail_scraper import classify_record  # noqa: E402

_TEACHERS = _THIS_DIR


def dept_work_items(school: str, dept: str) -> tuple[int, list[str]]:
    """返回 (待处理数, 分类明细)。名单与已落盘详情做差量。"""
    detail_path = _TEACHERS / school / dept / "teachers_detail.json"
    done: dict[tuple[str, str], str] = {}
    if detail_path.exists():
        try:
            data = json.loads(detail_path.read_text(encoding="utf-8"))
            ts = data.get("teachers", []) if isinstance(data, dict) else data
            for t in ts:
                done[(str(t.get("姓名") or ""), str(t.get("详情页URL") or ""))] = \
                    classify_record(t)
        except Exception:
            pass

    dept_info = {"dept": dept}
    try:
        # list_depts 返回 [{dept, json_path}], load_teachers 需要该结构
        infos = list_depts(school)
        dept_info = next((d for d in infos if d["dept"] == dept), {"dept": dept})
        teachers = load_teachers(dept_info)
    except Exception:
        teachers = []

    work: list[str] = []
    for t in teachers:
        key = (str(t.get("name") or ""), str(t.get("detail_url") or ""))
        st = done.get(key)
        if st is None:
            work.append("新增")
        elif st in ("failed", "pending_pdf", "unknown"):
            work.append(st)
    return len(work), work


def main():
    ap = argparse.ArgumentParser(description="增量采集（名单差量驱动）")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--school")
    ap.add_argument("--dept")
    ap.add_argument("--fetch-only", action="store_true", help="只下载 HTML/PDF 落盘")
    ap.add_argument("--reextract-llm", action="store_true", help="降级行复用本地文件重跑 LLM")
    ap.add_argument("--dept-budget-min", type=int, default=8,
                    help="每院系墙钟预算（分钟）, 超时杀掉进下一系")
    ap.add_argument("--teacher-workers", type=int, default=6)
    ap.add_argument("--rate-limit", type=float, default=0.3)
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []
    if args.school and args.dept:
        targets = [(args.school, args.dept)]
    elif args.school:
        targets = [(args.school, d["dept"]) for d in list_depts(args.school)]
    elif args.all:
        for school in list_schools():
            targets.extend((school, d["dept"]) for d in list_depts(school))
    else:
        ap.print_help()
        sys.exit(1)

    log = _THIS_DIR / "logs" / f"incremental_{int(time.time())}.log"
    done_cnt = skipped_cnt = 0
    for i, (school, dept) in enumerate(targets):
        n_work, kinds = dept_work_items(school, dept)
        if n_work == 0:
            skipped_cnt += 1
            continue
        from collections import Counter
        kc = Counter(kinds)
        msg = (f"[{i + 1}/{len(targets)}] {school}/{dept}: 待处理 {n_work} "
               f"({dict(kc)})")
        print(msg)
        with open(log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        cmd = [sys.executable, str(_THIS_DIR / "extract_all_details.py"),
               "--school", school, "--dept", dept, "--resume",
               "--teacher-workers", str(args.teacher_workers),
               "--rate-limit", str(args.rate_limit)]
        if args.fetch_only:
            cmd.append("--fetch-only")
        if args.reextract_llm:
            cmd.append("--reextract-llm")
        try:
            subprocess.run(cmd, timeout=args.dept_budget_min * 60,
                           check=False)
        except subprocess.TimeoutExpired:
            print(f"  ⏰ 墙钟预算超时, 跳过 {school}/{dept}")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"  ⏰ 超时跳过 {school}/{dept}\n")
        done_cnt += 1

    print(f"\n✅ 增量扫描完成: 处理 {done_cnt} 系, 跳过 {skipped_cnt} 系 "
          f"（无新工作）\n日志: {log}")


if __name__ == "__main__":
    main()
