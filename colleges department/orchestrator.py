#!/usr/bin/env python3
"""
orchestrator.py — 批量编排

三阶段流水线:
  1. 检查每校有无 spider_config.json → 无则调 auto_analysis
  2. 检查每校有无 spider.py → 无则调 spider_generator
  3. 运行 spider.py → 产出 departments.json/csv + 更新全局大表

运行:
  uv run python "colleges department/orchestrator.py" --all        # 全量
  uv run python "colleges department/orchestrator.py" --limit 5    # 限5所
  uv run python "colleges department/orchestrator.py" --name "安徽大学"  # 单校
"""

import argparse, csv, json, os, subprocess, sys, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHOOLS_DIR = BASE_DIR / "output"
INPUT_CSV = BASE_DIR / "985_211_双一流高校清单.csv"
ANALYSIS_SCRIPT = BASE_DIR / "auto_analysis.py"
GENERATOR_SCRIPT = BASE_DIR / "spider_generator.py"


def load_schools() -> list[dict]:
    with open(INPUT_CSV, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("学校网址", "").strip()]


def run_phase(name: str, script: Path, args: list, timeout: int = 120) -> bool:
    """运行一个子脚本, 返回是否成功"""
    cmd = [sys.executable, str(script)] + args
    print(f"    → {script.name} {' '.join(args)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                cwd=str(BASE_DIR))
        # 只打印最后几行
        lines = result.stdout.strip().split("\n")
        for line in lines[-5:]:
            if line.strip():
                print(f"      {line.strip()[:100]}")
        if result.returncode != 0:
            print(f"      ⚠️ exit={result.returncode}")
            stderr = result.stderr.strip()
            if stderr:
                print(f"      {stderr[:200]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"      ⏰ 超时")
        return False
    except Exception as e:
        print(f"      ❌ {e}")
        return False


def process_school(name: str, url: str, skip_analysis: bool = False,
                   skip_generation: bool = False, skip_spider: bool = False) -> dict:
    """处理一所学校的三阶段"""
    folder = SCHOOLS_DIR / name
    config_file = folder / "spider_config.json"
    spider_file = folder / "spider.py"
    dept_json = folder / "departments.json"

    print(f"\n{'─'*50}")
    print(f"🏫 {name}")

    # Phase 1: auto_analysis
    if not skip_analysis and not config_file.exists():
        print(f"  📋 Phase 1: 分析页面结构")
        ok = run_phase(name, ANALYSIS_SCRIPT, ["--name", name, "--url", url])
        if not ok:
            return {"school": name, "status": "analysis_failed", "phase": 1}
    elif config_file.exists():
        print(f"  ✅ spider_config.json 已存在，跳过分析")

    # Phase 2: spider_generator
    if not skip_generation:
        if not config_file.exists():
            return {"school": name, "status": "no_config", "phase": 2}
        print(f"  📋 Phase 2: 生成 spider.py")
        ok = run_phase(name, GENERATOR_SCRIPT, ["--name", name])
        if not ok:
            return {"school": name, "status": "generation_failed", "phase": 2}

    # Phase 3: 运行 spider.py
    if not skip_spider and spider_file.exists():
        print(f"  📋 Phase 3: 运行采集")
        ok = run_phase(name, spider_file, [], timeout=300)
        if not ok:
            return {"school": name, "status": "spider_failed", "phase": 3}

        # 读结果
        if dept_json.exists():
            with open(dept_json, encoding="utf-8") as f:
                r = json.load(f)
            n = len(r.get("departments", []))
            v = r.get("validation", {})
            dept_types = set(d.get("type","?") for d in r.get("departments", []))
            print(f"  🎉 {n} 院系, {v.get('score','?')}分, ok={v.get('ok',False)}, types={sorted(dept_types)}")
            return {"school": name, "status": "ok", "depts": n,
                    "score": v.get("score", 0), "ok": v.get("ok", False),
                    "types": sorted(dept_types)}
        else:
            return {"school": name, "status": "no_output", "phase": 3}

    return {"school": name, "status": "skipped", "phase": 3}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="全量处理")
    parser.add_argument("--name", type=str, help="单校名称")
    parser.add_argument("--url", type=str, help="单校网址")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--skip-analysis", action="store_true")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--skip-spider", action="store_true")
    parser.add_argument("--force", action="store_true", help="强制重跑（忽略已有产出）")
    args = parser.parse_args()

    # 确定目标
    if args.name:
        targets = [{"学校名称": args.name, "学校网址": args.url or ""}]
    elif args.all:
        targets = load_schools()
        if args.limit > 0:
            targets = targets[args.start:args.start + args.limit]
    else:
        targets = load_schools()
        if args.limit > 0:
            targets = targets[args.start:args.start + args.limit]
        else:
            print("请指定 --name / --all / --limit")
            return

    print("=" * 60)
    print(f"🎓 orchestrator — 目标: {len(targets)} 所学校")
    print(f"   Phase 1: auto_analysis.py (分析页面)")
    print(f"   Phase 2: spider_generator.py (生成爬虫)")
    print(f"   Phase 3: spider.py (运行采集)")
    if args.force:
        print(f"   ⚠️ 强制模式: 忽略已有产出")
    print("=" * 60)

    results = []
    for i, s in enumerate(targets):
        name, url = s["学校名称"], s.get("学校网址", "").strip()
        if not url:
            continue

        if args.force:
            folder = SCHOOLS_DIR / name
            fns = [folder / "departments.json", folder / "departments.csv",
                   folder / "spider.py", folder / "spider_config.json"]
            for f in fns:
                f.unlink(missing_ok=True)

        result = process_school(name, url,
                                skip_analysis=args.skip_analysis,
                                skip_generation=args.skip_generation,
                                skip_spider=args.skip_spider)
        results.append(result)

        if result["status"] == "ok":
            print(f"  ✅ {name}: {result.get('depts',0)} 院系, {result.get('score',0)}分")
        else:
            print(f"  ❌ {name}: {result['status']} (phase {result.get('phase','?')})")

        if i < len(targets) - 1:
            time.sleep(1)

    # ── 汇总 ──
    ok_count = sum(1 for r in results if r["status"] == "ok")
    fail_count = len(results) - ok_count

    # 全局统计
    jsonl = BASE_DIR / "depart_985211.jsonl"
    if jsonl.exists():
        all_recs = []
        for line in jsonl.read_text("utf-8").strip().split("\n"):
            if line.strip():
                try:
                    all_recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        total_depts = sum(len(r.get("departments", [])) for r in all_recs)
        ok_depts = sum(1 for r in all_recs if r.get("departments") and r.get("validation", {}).get("ok", True))
        print(f"\n{'='*60}")
        print(f"📊 汇总: {len(all_recs)} 所学校, {total_depts} 院系, {ok_depts} 所验证通过")
        print(f"   本轮: {ok_count} OK, {fail_count} 失败")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print(f"📊 本轮: {ok_count} OK, {fail_count} 失败")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
