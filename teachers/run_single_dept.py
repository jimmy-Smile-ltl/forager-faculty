#!/usr/bin/env python3
"""单院系完整管道入口（复用 dept_agent.run_dept，可跳过师资发现以省网络）。

用法:
  uv run python run_single_dept.py --school 安徽大学 --dept 材料科学与工程学部 [--sample 5] [--with-discovery]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dept_agent import run_dept  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="教师详情页采集 Agent（单院系）")
    parser.add_argument("--school", required=True)
    parser.add_argument("--dept", required=True)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--with-discovery", action="store_true",
                        help="运行师资分类发现（需要访问院系列表页；教师清单已完整时跳过省网络）")
    args = parser.parse_args()

    result = run_dept(args.school, args.dept, sample=args.sample,
                      max_rounds=args.max_rounds,
                      skip_discovery=not args.with_discovery)
    if result.get("passed"):
        sys.exit(0)
    sys.exit(2)


if __name__ == "__main__":
    main()
