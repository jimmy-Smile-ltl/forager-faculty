#!/usr/bin/env python
"""增量提取驱动（2026-09-02 用户指令）: 对「本地 HTML/PDF 文件数 > 已提取 ok 行数」
的盖章院系, 并发跑 extract_all_details --resume --reextract-llm --local-only,
只补新增行（存量 ok 行零重复 LLM）, 随后 evaluate。

与 extract_after_rules 错开: 本工具目标集合 = 有缺口的院系（无论有无规则）。
用法: uv run python extract_gap.py [--workers 6] [--teacher-workers 16] [--limit N]
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BASE = Path(__file__).parent
sys.path.insert(0, str(_BASE))
from extract_all_details import list_schools, list_depts  # noqa: E402

RAW_ROOT = _BASE.parent / "tearchers raw data"
_OK_SRC = ("html", "pdf", "html_shell_pdf", "html_fallback")


def gap_depts() -> list[tuple[str, str, int, int]]:
    """返回 (school, dept, 缺口行数, 文件数) 清单。"""
    out: list[tuple[str, str, int, int]] = []
    for school in list_schools():
        try:
            depts = list_depts(school)
        except Exception:
            continue
        for d in depts:
            dept = d["dept"]
            dept_dir = _BASE / school / dept
            if not (dept_dir / "download_done.flag").exists():
                continue
            raw_root = RAW_ROOT / school / dept
            n_files = 0
            if raw_root.is_dir():
                n_files = len(list(raw_root.glob("*.html"))) + len(list(raw_root.glob("*.pdf")))
            detail_p = dept_dir / "teachers_detail.json"
            ok_rows = 0
            if detail_p.exists():
                try:
                    dta = json.loads(detail_p.read_text(encoding="utf-8"))
                    ok_rows = sum(
                        1 for r in dta.get("teachers", [])
                        if (r.get("来源类型") or "").strip() in _OK_SRC)
                except Exception:
                    ok_rows = 0
            gap = n_files - ok_rows
            if gap > 0:
                out.append((school, dept, gap, n_files))
    out.sort(key=lambda x: -x[2])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--teacher-workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 系(0=全部)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = gap_depts()
    print(f"[{time.strftime('%F %T')}] 缺口院系 {len(targets)} 个, "
          f"合计缺口 {sum(t[2] for t in targets)} 行", flush=True)
    if args.limit:
        targets = targets[:args.limit]
    if args.dry_run:
        for s, d, g, f in targets[:20]:
            print(f"  {s}/{d}: 缺口 {g} (文件 {f})", flush=True)
        return

    done = {"ok": 0, "fail": 0}
    t0 = time.time()

    def work(t):
        school, dept, gap, n_files = t
        cmd = [sys.executable, str(_BASE / "extract_all_details.py"),
               "--school", school, "--dept", dept,
               "--resume", "--reextract-llm", "--local-only",
               "--teacher-workers", str(args.teacher_workers),
               "--rate-limit", "0.3"]
        budget_min = 25 + max(0, n_files - 50) // 10
        try:
            r = subprocess.run(cmd, check=False, timeout=budget_min * 60)
        except subprocess.TimeoutExpired:
            return school, dept, "fail", f"预算超时({budget_min}min)"
        if r.returncode != 0:
            return school, dept, "fail", "提取非零退出"
        r2 = subprocess.run([sys.executable, str(_BASE / "evaluate_teachers.py"),
                             "--school", school, "--dept", dept], check=False)
        return school, dept, "ok" if r2.returncode == 0 else "fail", ""

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, t): t for t in targets}
        for i, f in enumerate(as_completed(futs)):
            school, dept, status, detail = f.result()
            done[status] = done.get(status, 0) + 1
            print(f"[{i + 1}/{len(targets)}] {school}/{dept} → {status} {detail}"
                  f"（ok {done['ok']} / fail {done['fail']}, "
                  f"用时 {time.time() - t0:.0f}s）", flush=True)
    print(f"\n✅ 增量提取完成: {done}（总用时 {time.time() - t0:.0f}s）",
          flush=True)


if __name__ == "__main__":
    main()
