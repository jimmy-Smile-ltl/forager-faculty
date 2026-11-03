#!/usr/bin/env python3
"""单院系下载子进程（upstream_refresh 隔离用）——读预过滤教师 JSON,
fetch-only 下载落盘。父进程按预算杀, 本进程的 10 分钟无进展看门狗
只杀自己, 不影响驱动。"""
import argparse
import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from detail_scraper import DetailScraper  # noqa: E402

_RAW = _BASE.parent / "tearchers raw data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--school", required=True)
    ap.add_argument("--dept", required=True)
    ap.add_argument("--teachers-json", required=True)
    ap.add_argument("--rate-limit", type=float, default=0.3)
    args = ap.parse_args()

    teachers = json.loads(Path(args.teachers_json).read_text(encoding="utf-8"))
    if not teachers:
        return
    scraper = DetailScraper(
        school_name=args.school, dept_name=args.dept,
        raw_data_root=str(_RAW), csv_output_root=str(_BASE),
        rate_limit=args.rate_limit, fetch_only=True)
    # 2026-10-25 用户指令加快并发: URL 级 5→8; 2026-10-26 再提 8→12
    # (用户指令抓紧下载, 代理可用窗口) — fetch-only 纯网络 IO,
    # 院系级 4×12=48 路, 机器内存无压力
    scraper.process_batch(teachers, workers=12)


if __name__ == "__main__":
    main()
