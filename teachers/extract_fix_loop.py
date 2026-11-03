#!/usr/bin/env python3
"""评估驱动规则改进闭环（发现58, 2026-09-07）。

输入: 评估不通过院系清单（职称<70% 或漏提桶非空, 如 logs/_true_leak_depts.tsv
      的 学校<TAB>院系 行, 或 --school/--dept 单系）。

每院系流程（rounds 循环）:
  1) 重提取: extract_all_details.py --resume --reapply-rules --reextract-llm
     --local-only（复用本地 HTML, A1-A4 通用启发式修复随重提取生效）
  2) 复评: evaluate_teachers.analyze_dept（职称/研究方向覆盖率 + 疑似漏提桶）
  3) 达标（职称≥70% 且 方向≥50% 且 漏提率≤30%）→ 通过
  4) 不达标 → dept_rule_generator.iterative_refine（失败字段带原文窗口
     反馈重设计）→ write_dept_rules 写 <校>/<院>/extract_rules.json →
     下一轮回到 1), 至 --max-rounds（默认 2）

并发: --dept-workers（默认 8, 复用 extract_stage 的 ThreadPoolExecutor
      模式）; 提取子进程内的教师级 LLM 并发受 LLM_SEM 全局闸门约束。

用法:
  uv run python extract_fix_loop.py --list logs/_true_leak_depts.tsv --dept-workers 8
  uv run python extract_fix_loop.py --school 华中师范大学 --dept 公共管理学院
"""
import argparse
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

from extract_all_details import list_schools, list_depts  # noqa: E402
from dept_rule_generator import (iterative_refine, sample_texts,  # noqa: E402
                                 write_dept_rules)

# 达标阈值（发现58 闭环判定）
PASS_ZC = 70.0    # 职称覆盖率 %
PASS_FX = 50.0    # 研究方向覆盖率 %
PASS_LEAK = 0.30  # 疑似漏提率上限（漏提数/检查文件数）


def evaluate_dept(school: str, dept: str) -> dict | None:
    """复用评估侧 analyze_dept（含发现58 疑似漏提桶）。"""
    from evaluate_teachers import analyze_dept
    jp = _BASE / school / dept / "teachers_detail.json"
    if not jp.exists():
        return None
    return analyze_dept(jp, _BASE / school / dept / "skipped_urls.json")


def dept_passed(r: dict) -> tuple[bool, str]:
    """达标判定 → (通过?, 原因)。

    职称按「可判页提取率」判定（发现58: 简历/简介型页面不写职称,
    全系覆盖率被它们拉低是口径失真——湘潭文新 30 人空职称实测页面
    0 职称词, 70% 线对其不可达, 规则白烧 LLM）。研究方向同理
    （东北师大历史 44 人空方向实测页面 0 方向词）。无线索页时对应
    检查豁免。疑似漏提率仍按全系口径。"""
    cov = r.get("coverage") or {}
    tr = r.get("title_rate") or {}
    # 可判页样本下限: <5 个线索页时豁免（北大历史 0/1 单页噪声会
    # 误判 0%, 为 1 页生成规则不值得）
    if tr.get("clue", 0) >= 5:
        zc = tr["filled"] / tr["clue"] * 100
    else:
        zc = 100.0
    dr = r.get("dir_rate") or {}
    if dr.get("clue", 0) >= 5:
        fx = dr["filled"] / dr["clue"] * 100
    else:
        fx = 100.0
    checked = (r.get("junk_files") or {}).get("checked", 0) or 1
    leak_rate = r.get("leak", 0) / checked
    if zc < PASS_ZC:
        return False, (f"职称可判页提取率{zc:.0f}%<{PASS_ZC:.0f}%"
                       f"（{tr.get('filled', 0)}/{tr.get('clue', 0)}）")
    if fx < PASS_FX:
        return False, (f"方向可判页提取率{fx:.0f}%<{PASS_FX:.0f}%"
                       f"（{dr.get('filled', 0)}/{dr.get('clue', 0)}）")
    if leak_rate > PASS_LEAK and r.get("leak", 0) > 5:
        return False, f"疑似漏提 {r['leak']} 人（{leak_rate * 100:.0f}%）"
    return True, ""


def run_extract(school: str, dept: str, teacher_workers: int,
                rate_limit: float) -> None:
    """重提取（本地模式, 规则+LLM 全重跑）。日志落 fix_loop 目录。"""
    logdir = _BASE / "logs" / "fix_loop"
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{school}__{dept}.log"
    with open(logf, "a", encoding="utf-8") as f:
        subprocess.run(
            [sys.executable, str(_BASE / "extract_all_details.py"),
             "--school", school, "--dept", dept,
             "--resume", "--reextract-llm", "--reapply-rules",
             "--local-only",
             "--teacher-workers", str(teacher_workers),
             "--rate-limit", str(rate_limit)],
            check=False, stdout=f, stderr=subprocess.STDOUT)


def fix_one(school: str, dept: str, args) -> dict:
    """单院系闭环: 先复评（已达标直接跳过, 省 LLM）→ 重提取 →
    复评 → 不达标生成规则 → 循环。"""
    # 上游名单缺失（extract CLI 按 list_depts 校验, 无名单→"未找到院系"
    # 秒退, 重提取永远失败）——西交法学院/北大外院实测。跳过省 LLM,
    # 待上游补名单后重扫。
    if dept not in {d["dept"] for d in list_depts(school)}:
        with _print_lock:
            print(f"  [{school}/{dept}] 上游无该院系名单, 跳过", flush=True)
        return {"school": school, "dept": dept, "status": "no-roster",
                "rounds": 0, "why": "上游名单缺失（list_depts 无此院系）"}
    r0 = evaluate_dept(school, dept)
    if r0 is not None:
        ok0, _ = dept_passed(r0)
        if ok0:
            with _print_lock:
                print(f"  [{school}/{dept}] 已达标, 跳过", flush=True)
            return {"school": school, "dept": dept,
                    "status": "passed-initial", "rounds": 0, "why": ""}
        # 垃圾文件（JS 壳, 含名率<20%）: 重提取/规则都救不了, 直接跳过
        # 省 LLM——湖南大学壳家族 159 文件含名 0 实测（待渲染/上游修复）
        jf = r0.get("junk_files") or {}
        if jf.get("checked", 0) >= 5 and (jf.get("rate") or 100) < 20:
            with _print_lock:
                print(f"  [{school}/{dept}] 垃圾文件（含名率 "
                      f"{jf.get('rate')}%）, 跳过", flush=True)
            return {"school": school, "dept": dept,
                    "status": "junk-files", "rounds": 0,
                    "why": f"含名率 {jf.get('rate')}%<20%"}
    for round_i in range(1, args.max_rounds + 1):
        with _print_lock:
            print(f"  [{school}/{dept}] 第 {round_i}/{args.max_rounds} 轮 重提取…",
                  flush=True)
        run_extract(school, dept, args.teacher_workers, args.rate_limit)
        r = evaluate_dept(school, dept)
        if r is None:
            return {"school": school, "dept": dept, "status": "no-data",
                    "why": "无 teachers_detail.json"}
        ok, why = dept_passed(r)
        with _print_lock:
            tr = r.get("title_rate") or {}
            dr = r.get("dir_rate") or {}
            print(f"  [{school}/{dept}] 复评: 职称可判页 {tr.get('filled')}/"
                  f"{tr.get('clue')} ({tr.get('rate')}%) 方向可判页 "
                  f"{dr.get('filled')}/{dr.get('clue')} ({dr.get('rate')}%) "
                  f"漏提{r['leak']} "
                  f"→ {'✅ 通过' if ok else '❌ ' + why}", flush=True)
        if ok:
            return {"school": school, "dept": dept, "status": "passed",
                    "rounds": round_i, "why": ""}
        if round_i == args.max_rounds:
            return {"school": school, "dept": dept, "status": "not-passed",
                    "rounds": round_i, "why": why}
        # 不达标 → LLM 生成/改进规则（样本=本地 raw 文本, 失败字段带
        # 原文窗口反馈重设计, 详见 dept_rule_generator.iterative_refine）
        samples = sample_texts(school, dept, n=5)
        if not samples:
            return {"school": school, "dept": dept, "status": "no-samples",
                    "rounds": round_i, "why": f"本地无样本可生成规则（{why}）"}
        with _print_lock:
            print(f"  [{school}/{dept}] LLM 生成规则…", flush=True)
        try:
            payload = iterative_refine(school, dept, samples, max_iters=3)
        except Exception as e:
            return {"school": school, "dept": dept, "status": "llm-failed",
                    "rounds": round_i, "why": f"{type(e).__name__} {str(e)[:80]}"}
        if not payload.get("rules"):
            return {"school": school, "dept": dept, "status": "rules-empty",
                    "rounds": round_i, "why": f"LLM 空规则（{why}）"}
        p = write_dept_rules(school, dept, payload)
        with _print_lock:
            print(f"  [{school}/{dept}] ✍️ 规则已写 {p}", flush=True)
    return {"school": school, "dept": dept, "status": "unknown",
            "rounds": 0, "why": ""}


_print_lock = threading.Lock()


def main():
    ap = argparse.ArgumentParser(description="评估驱动规则改进闭环（发现58）")
    ap.add_argument("--list", default=None,
                    help="不达标院系 TSV（学校<TAB>院系 列, 含表头）")
    ap.add_argument("--school", default=None)
    ap.add_argument("--dept", default=None)
    ap.add_argument("--dept-workers", type=int, default=8)
    ap.add_argument("--teacher-workers", type=int, default=8,
                    help="提取子进程教师级并发（受 LLM_SEM 全局闸门约束）")
    ap.add_argument("--max-rounds", type=int, default=2)
    ap.add_argument("--rate-limit", type=float, default=0.3)
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []
    if args.school and args.dept:
        targets = [(args.school, args.dept)]
    elif args.list:
        import csv
        with open(args.list, encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                s, d = (row.get("学校") or "").strip(), (row.get("院系") or "").strip()
                if s and d:
                    targets.append((s, d))
    else:
        ap.print_help()
        sys.exit(1)

    total = len(targets)
    print(f"🎯 闭环开始: {total} 院系, 并发 {args.dept_workers}, "
          f"最多 {args.max_rounds} 轮/系", flush=True)
    counts: dict[str, int] = {}
    if args.dept_workers <= 1:
        for s, d in targets:
            res = fix_one(s, d, args)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
    else:
        with ThreadPoolExecutor(max_workers=args.dept_workers) as ex:
            futs = [ex.submit(fix_one, s, d, args) for s, d in targets]
            for fut in as_completed(futs):
                res = fut.result()
                counts[res["status"]] = counts.get(res["status"], 0) + 1
    print(f"\n✅ 闭环完成: {total} 院系 | " + " | ".join(
        f"{k} {v}" for k, v in sorted(counts.items())), flush=True)


if __name__ == "__main__":
    main()
