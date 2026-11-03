#!/usr/bin/env python3
"""
批量重采修复脚本 — 驱动 FIX_QUEUE.json 中标记的坏院系重新采集

读取 logs/FIX_QUEUE.json (由 evaluate_all.py 生成), 对每个坏院系:
  1. 从 output/<school>/departments.csv 找到院系URL与类型
  2. 删除旧 teachers.csv (强制重采, 覆盖旧错误数据)
  3. 调用 collect_teacher.collect_dept() 走新流水线 (分析→生成spider→运行→LLM评估→CSV净化→质量门禁)

支持断点续传: logs/fix_progress.json 记录已处理院系。
用法:
  uv run python fix_pipeline.py                # 全量
  uv run python fix_pipeline.py --limit 20     # 只修前20个
  uv run python fix_pipeline.py --school 北京大学  # 只修某校
  uv run python fix_pipeline.py --dry-run      # 只列出计划不执行
"""

import argparse, csv, json, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # script/ 的上层 = department teachers 根
OUTPUT_DIR = BASE / "output"      # 学校数据目录
LOGS_DIR = BASE / "logs"          # 顶层队列/进度文件(2026-08-28 从 output/ 迁出)
QUEUE = LOGS_DIR / "FIX_QUEUE.json"
PROGRESS = LOGS_DIR / "fix_progress.json"

sys.path.insert(0, str(BASE))
from collect_teacher import collect_dept  # noqa: E402


def load_queue():
    if not QUEUE.exists():
        print("❌ FIX_QUEUE.json 不存在, 先运行 evaluate_all.py")
        return []
    return json.load(open(QUEUE, encoding="utf-8"))


def load_progress():
    if PROGRESS.exists():
        return set(json.load(open(PROGRESS, encoding="utf-8")))
    return set()


def save_progress(done):
    PROGRESS.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=2), encoding="utf-8")


def get_dept_info(school, dept):
    """从 departments.csv 找院系URL和类型"""
    p = OUTPUT_DIR / school / "departments.csv"
    if not p.exists():
        return None, "学院"
    try:
        for r in csv.DictReader(open(p, encoding="utf-8")):
            name = r.get("院系名称", "").strip()
            if name == dept or name.replace("/", "／").replace(":", "：") == dept:
                return r.get("院系URL", "").strip(), r.get("院系类型", "学院").strip()
    except Exception:
        pass
    return None, "学院"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只修前N个")
    ap.add_argument("--school", help="只修指定学校")
    ap.add_argument("--dry-run", action="store_true", help="只列出计划")
    ap.add_argument("--sanitize-only", action="store_true",
                    help="只对C级院系做CSV净化(不重新采集)")
    args = ap.parse_args()

    if args.sanitize_only:
        sanitize_only(school=args.school, dry_run=args.dry_run)
        return

    queue = load_queue()
    done = load_progress()
    if args.school:
        queue = [q for q in queue if q["school"] == args.school]
    if args.limit > 0:
        queue = queue[: args.limit]

    print(f"修复队列: {len(queue)} 个院系 (已完成 {len(done)} 个)\n")

    ok = fail = skip = 0
    for i, q in enumerate(queue):
        school, dept = q["school"], q["dept"]
        key = f"{school}/{dept}"
        if key in done:
            skip += 1
            continue

        url, dtype = get_dept_info(school, dept)
        if args.dry_run:
            print(f"[{i+1}/{len(queue)}] 📋 {key} [{q['grade']}] url={'✅' if url else '❌'}")
            continue
        if not url:
            print(f"[{i+1}/{len(queue)}] ⚠️ {key} 无URL, 跳过")
            fail += 1
            done.add(key)
            continue

        # 删除旧数据强制重采
        out = OUTPUT_DIR / school / dept.replace("/", "／").replace(":", "：")
        for stale in ("teachers.csv", "teachers.json", "test_report.json", "analysis.md"):
            (out / stale).unlink(missing_ok=True)

        print(f"\n[{i+1}/{len(queue)}] 🔧 {key} [{q['grade']}] 重采中...")
        try:
            result = collect_dept(school, dept, url, dtype)
            if result.get("status") == "done" and result.get("passed"):
                ok += 1
                done.add(key)
            else:
                fail += 1
                done.add(key)  # 避免死循环, 记录为已尝试
        except Exception as e:
            print(f"  💥 异常: {str(e)[:80]}")
            fail += 1
            done.add(key)

        save_progress(done)
        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"完成: ✅ {ok} | ❌ {fail} | ⏭️ 跳过 {skip} | 队列剩余 {len(queue)-ok-fail-skip}")
    print(f"进度文件: {PROGRESS}")


if __name__ == "__main__":
    main()


# ── 快速净化批处理: 对C级院系直接跑 sanitize_teachers_csv ──
def sanitize_only(school=None, dry_run=False):
    """对 C 级(有URL但混入少量导航词/假URL)的院系, 直接净化CSV, 不重新采集"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ct", str(BASE / "collect_teacher.py"))
    ct = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ct)

    hr = json.load(open(LOGS_DIR / "HEALTH_REPORT.json", encoding="utf-8"))
    targets = []
    for s, sd in hr["school_details"].items():
        if school and s != school:
            continue
        if sd.get("C", 0) == 0:
            continue
        for d in sorted((OUTPUT_DIR / s).iterdir()):
            if not d.is_dir():
                continue
            r = ct.sanitize_teachers_csv(d, s, d.name)
            if r[0] > 0:
                targets.append((s, d.name, r))
    print(f"\n净化完成: {len(targets)} 个院系")
    for s, d, r in targets[:20]:
        print(f"  {s[:12]:12s} {d[:22]:22s} 保留{r[0]} 剔导航{r[2]} 剔假URL{r[3]}")
