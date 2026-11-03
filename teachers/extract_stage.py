#!/usr/bin/env python3
"""第二步：提取阶段（2026-08-31, 依赖 LLM, 与下载解耦）。

只看下载标志: download_done.flag 存在才提取（未下载完的院系跳过）。
流程: 检查标志 → 复用本地 HTML/PDF 提取（学院规则 + LLM, 零抓取）
→ evaluate 质量评估 → 输出确认报告（提取结果正确性由人确认）。

用法:
  uv run python extract_stage.py --school 北京大学                  # 单校（只提取已下载完的院系）
  uv run python extract_stage.py --school 北京大学 --dept 考古文博学院 --force
  uv run python extract_stage.py --all --teacher-workers 8
  uv run python extract_stage.py --school 北京大学 --evaluate-only  # 只评估不提取
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

from extract_all_details import list_schools, list_depts  # noqa: E402

FLAG_NAME = "download_done.flag"
_RAW_ROOT = _BASE.parent / "tearchers raw data"
# 2026-09-07 本地样本门控: LLM 分析需抽样 5 个 HTML/PDF——本地文件
# 少于该量级（no_url 型院系如北大计算机学院 133 行全 no_url, raw 目录
# 0 文件）提取必然零产出, 直接跳过不烧 LLM。环境变量可调。
_MIN_SAMPLES = int(os.environ.get("EXTRACT_MIN_SAMPLES", "5"))


def flag_ready(school: str, dept: str) -> bool:
    return (_BASE / school / dept / FLAG_NAME).exists()


def local_sample_count(school: str, dept: str) -> int:
    """院系本地 raw 目录的 HTML+PDF 文件数（提取的实际输入量）。"""
    raw = _RAW_ROOT / school / dept
    if not raw.is_dir():
        return 0
    return sum(1 for f in raw.iterdir()
               if f.suffix.lower() in (".html", ".pdf"))


def main():
    ap = argparse.ArgumentParser(description="第二步: 标志门控提取+评估")
    ap.add_argument("--school")
    ap.add_argument("--dept")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="无视下载标志强制提取")
    ap.add_argument("--evaluate-only", action="store_true",
                    help="只跑评估, 不提取")
    ap.add_argument("--teacher-workers", type=int, default=16,
                    help="教师级 LLM 并发（第二步完全本地, 无网络限制, "
                         "由 LLM_SEM 全局闸门控制实际并发）")
    ap.add_argument("--dept-workers", type=int, default=1,
                    help="院系级并发（2026-09-07 用户指令: 8 并发处理存量; "
                         "院系×教师并发之积受 LLM_SEM 闸门约束）")
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

    # 2026-09-07 院系级并发: worker 内做门控+提取+评估, 主线程汇总计数。
    # 并发安全: 各院系目录/文件互不重叠; subprocess 各自独立。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _print_lock = __import__("threading").Lock()

    def _process_one(i: int, school: str, dept: str) -> str:
        """单院系处理: 返回 kind = extracted/evaluated_only/done/low_samples/skipped。"""
        if not args.force and not flag_ready(school, dept):
            return "skipped"
        if not args.force and os.path.exists(_BASE / school / dept / "evaluation_report.md"):
            return "done"
        if local_sample_count(school, dept) < _MIN_SAMPLES:
            return "low_samples"
        with _print_lock:
            print(f"[{i + 1}/{len(targets)}] {school}/{dept}", flush=True)
        if not args.evaluate_only:
            cmd = [sys.executable, str(_BASE / "extract_all_details.py"),
                   "--school", school, "--dept", dept,
                   "--resume", "--reextract-llm",
                   "--local-only",
                   "--teacher-workers", str(args.teacher_workers),
                   "--rate-limit", str(args.rate_limit)]
            subprocess.run(cmd, check=False)
        subprocess.run([sys.executable, str(_BASE / "evaluate_teachers.py"),
                        "--school", school, "--dept", dept], check=False)
        return "evaluated_only" if args.evaluate_only else "extracted"

    counts: dict[str, int] = {}
    total = len(targets)
    if args.dept_workers <= 1:
        for i, (school, dept) in enumerate(targets):
            kind = _process_one(i, school, dept)
            counts[kind] = counts.get(kind, 0) + 1
    else:
        with ThreadPoolExecutor(max_workers=args.dept_workers) as ex:
            futs = {ex.submit(_process_one, i, s, d): (s, d)
                    for i, (s, d) in enumerate(targets)}
            for fut in as_completed(futs):
                counts[fut.result()] = counts.get(fut.result(), 0) + 1
    extracted = counts.get("extracted", 0)
    evaluated = extracted + counts.get("evaluated_only", 0)
    skipped = counts.get("skipped", 0)
    done = counts.get("done", 0)
    low_samples = counts.get("low_samples", 0)

    print(f"\n✅ 提取阶段完成: 提取 {extracted} 系, 评估 {evaluated} 系, "
          f"跳过 {skipped} 系（下载未完成, 无标志）, "
          f"跳过 {done} 系（已提取过, --force 重提）, "
          f"跳过 {low_samples} 系（本地样本 < {_MIN_SAMPLES}, 无提取意义）\n"
          f"并发: 院系 {args.dept_workers} × 教师 {args.teacher_workers}")
    if skipped:
        print("提示: 被跳过的院系先跑 download_stage.py 下载, 完成后自动有标志")
    if low_samples:
        print("提示: 本地样本不足的院系待上游修复 URL + 重新下载后, 自动恢复提取")


if __name__ == "__main__":
    main()
