#!/usr/bin/env python3
"""规则落盘后的提取跟进（2026-08-31）——对已有 dept_rules JSON 的院系跑
第二步提取（--local-only 零网络, LLM+启发式+学院规则全链）, 随后评估。

与 gen_rules_batch 解耦: 规则批量写盘 → 本脚本提取 → evaluate 产出覆盖率
与污染桶对比 → 人工确认（用户口径: 确认提取结果正确 = 通过）。

用法:
  uv run python extract_after_rules.py --schools 清华大学 --workers 3
  uv run python extract_after_rules.py --schools 清华大学 --dept 会计系
  uv run python extract_after_rules.py --evaluate-only   # 只评估不提取
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

from extract_all_details import list_schools, list_depts, load_teachers  # noqa: E402
from detail_scraper import _load_dept_rules_json, _DEPT_EXTRACT_RULES  # noqa: E402


def rule_ready_depts(schools: list[str]) -> list[tuple[str, str, int]]:
    """有规则 JSON 或硬编码规则且有下载标志、且尚未按当前规则提取过的院系。

    已提取判定: evaluation_report.md 比规则 JSON 新 = 规则已应用并评估过。
    返回 (school, dept, 教师数)——教师数用于预算伸缩。
    """
    out: list[tuple[str, str, int]] = []
    for school in schools:
        for d in list_depts(school):
            dept = d["dept"]
            has_rule = ((school, dept) in _DEPT_EXTRACT_RULES
                        or _load_dept_rules_json(school, dept))
            if not has_rule:
                continue
            dept_dir = _BASE / school / dept
            if not (dept_dir / "download_done.flag").exists():
                continue
            rule_p = _BASE / school / dept / "extract_rules.json"
            if rule_p.exists():
                rule_mtime = rule_p.stat().st_mtime
            else:
                # 硬编码规则（北大历史/哲学等无 JSON）: 以代码 mtime 为版本
                rule_mtime = (_BASE / "detail_scraper.py").stat().st_mtime
            eval_p = dept_dir / "evaluation_report.md"
            if (eval_p.exists()
                    and eval_p.stat().st_mtime >= rule_mtime):
                continue  # 该版本规则已应用并评估
            # 预算超时冷却: 网关慢速期百人系跑不完, 2h 内不再重试
            defer_p = dept_dir / ".extract_defer"
            if (defer_p.exists()
                    and time.time() - defer_p.stat().st_mtime < 7200):
                continue
            try:
                n_teachers = len(load_teachers(d))
            except Exception:
                n_teachers = 0
            out.append((school, dept, n_teachers))
    return out


def main():
    ap = argparse.ArgumentParser(description="规则落盘后的第二步提取+评估")
    ap.add_argument("--schools", nargs="+", default=["清华大学", "北京大学"])
    ap.add_argument("--dept", help="只处理这一个院系")
    ap.add_argument("--skip-depts", nargs="*", default=[],
                    help="跳过的院系（如刚手动重提取过的）")
    ap.add_argument("--workers", type=int, default=6,
                    help="并行院系数（提取 LLM 由 LLM_SEM 闸门控制）")
    ap.add_argument("--teacher-workers", type=int, default=16,
                    help="每系教师级并发")
    ap.add_argument("--dept-budget-min", type=int, default=25,
                    help="每系提取子进程预算分钟（超时杀掉防僵尸, "
                         "2026-08-31 百人系 50min 无落盘教训）")
    ap.add_argument("--evaluate-only", action="store_true")
    args = ap.parse_args()

    targets = rule_ready_depts(args.schools)
    if args.dept:
        targets = [(s, d, n) for s, d, n in targets if d == args.dept]
    if args.skip_depts:
        targets = [(s, d, n) for s, d, n in targets if d not in args.skip_depts]
    print(f"🎯 有规则的院系 {len(targets)} 个")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = {"ok": 0, "fail": 0}
    t0 = time.time()

    def work(sd):
        school, dept, n_teachers = sd
        if not args.evaluate_only:
            cmd = [sys.executable, str(_BASE / "extract_all_details.py"),
                   "--school", school, "--dept", dept,
                   # --reapply-rules: 禁用 resume 跳过——存量旧提取行也重跑,
                   # 否则新规则不会应用到已有数据（清华 100 系全有旧基线）
                   "--reapply-rules", "--local-only",
                   "--teacher-workers", str(args.teacher_workers)]
            # 预算随人数伸缩（2026-08-31: 数学系 77 人固定 25min 两连超,
            # 波动期 LLM 人均延迟高, 大院系按每 10 人 +1 分钟）
            budget_min = args.dept_budget_min + max(0, n_teachers - 50) // 10
            try:
                r = subprocess.run(cmd, check=False,
                                   timeout=budget_min * 60)
            except subprocess.TimeoutExpired:
                # 写冷却标记: 2h 内 smart-skip 不再重试（等网关恢复）
                (_BASE / school / dept / ".extract_defer").touch()
                return (school, dept, "fail",
                        f"预算超时被杀（{budget_min}min, 冷却 2h 重试）")
            if r.returncode != 0:
                return (school, dept, "fail", "提取非零退出")
        r = subprocess.run([sys.executable, str(_BASE / "evaluate_teachers.py"),
                            "--school", school, "--dept", dept], check=False)
        return (school, dept, "ok" if r.returncode == 0 else "fail", "")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, t): t for t in targets}
        for i, f in enumerate(as_completed(futs)):
            school, dept, status, detail = f.result()
            done[status] = done.get(status, 0) + 1
            print(f"[{i + 1}/{len(targets)}] {school}/{dept} → {status} {detail}"
                  f"（ok {done['ok']} / fail {done['fail']}, "
                  f"用时 {time.time() - t0:.0f}s）", flush=True)
    print(f"\n✅ 提取跟进完成: {done}（总用时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
