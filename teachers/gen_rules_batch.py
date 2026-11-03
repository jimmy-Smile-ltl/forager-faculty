#!/usr/bin/env python3
"""批量院系规则生成（2026-08-31）——清华/北大优先, 逐系走
dept_rule_generator 闭环: 随机抽本地 HTML/PDF 样本 → LLM 生成规则
→ 静态校验 → 回放验证 → 通过即写 <校>/<系>/extract_rules.json
（采集侧 _load_dept_rules_json 懒加载即生效, 零采集侧侵入）。

引导期语义: 院系还没有 teachers_detail.json 提取基线时, 回放对命中
字段标记「未验证」而非假命中——结构校验通过即可落盘, 最终正确性由
后续提取 + 人工确认（用户口径: 确认提取结果正确 = 通过）。

用法:
  uv run python gen_rules_batch.py --schools 清华大学 --workers 2
  uv run python gen_rules_batch.py --schools 清华大学 北京大学 --workers 2 --write
  uv run python gen_rules_batch.py --ready-only --limit 10 --dry-run  # 只列目标
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

from extract_all_details import list_schools, list_depts  # noqa: E402
from download_stage import dept_download_state  # noqa: E402
from dept_rule_generator import (sample_texts, iterative_refine,  # noqa: E402
                                 write_dept_rules, _DEPT_EXTRACT_RULES,
                                 _load_dept_rules_json)

# 上游名单污染: 非院系栏目名（其"教师"实为栏目页链接, 不生成规则, 待上游修复）
_SKIP_DEPTS = frozenset({"奖助体系"})

_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        print(msg, flush=True)


def dept_targets(schools: list[str], ready_only: bool,
                 skip_existing: bool, limit: int) -> list[tuple[str, str]]:
    """按排名顺序（传入顺序即优先级）列出待生成院系。"""
    targets: list[tuple[str, str]] = []
    for school in schools:
        for d in list_depts(school):
            dept = d["dept"]
            if dept in _SKIP_DEPTS:
                continue
            if (school, dept) in _DEPT_EXTRACT_RULES:
                continue  # 硬编码人工规则保护
            if skip_existing and _load_dept_rules_json(school, dept):
                continue
            if ready_only:
                st = dept_download_state(school, dept)
                if st["missing"] != 0 or st["have"] == 0:
                    continue
            targets.append((school, dept))
            if limit and len(targets) >= limit:
                return targets
    return targets


def process_one(school: str, dept: str, args) -> dict:
    """单系闭环: 抽样本 → 生成 → 校验 → 回放 → 通过写盘。"""
    out = {"school": school, "dept": dept, "status": "error", "detail": ""}
    try:
        samples = sample_texts(school, dept, args.sample, args.seed)
        if len(samples) < 3:
            # 样本太少 = 本地 raw 不足或大部分是导航页（自强书院仅"一人一策"
            # 政策页）——规则证据不足, 跳过待下载补齐
            out.update(status="skip", detail=f"样本不足({len(samples)})")
            return out
        log(f"  [{school}/{dept}] 样本 {len(samples)}: "
            + ", ".join(f"{s['name']}({s['kind']})" for s in samples))
        final = iterative_refine(school, dept, samples, args.max_iters)
        # 零命中且页面无此信息的字段（absent）已被清空, 剔除空规则再统计
        final["rules"] = {f: p for f, p in final.get("rules", {}).items() if p}
        v = final.get("validation", {})
        # 假命中/不一致不阻塞（参考值是旧提取而非真值, 规则可能在补旧漏）
        ok = bool(v.get("ok"))
        n_fields = len(final.get("rules", {}))
        if ok and n_fields:
            if args.write:
                write_dept_rules(school, dept, final)
                out.update(status="written", detail=f"{n_fields} 字段")
            else:
                out.update(status="passed", detail=f"{n_fields} 字段（未 --write）")
        elif ok:
            out.update(status="skip", detail="空规则集")
        else:
            errs = "; ".join(v.get("errors", [])[:3])
            out.update(status="failed",
                       detail=errs[:160] or "无有效规则草案(LLM连续校验失败)")
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {e}"[:160]
        # 完整 traceback 落盘便于事后诊断（单系异常不拖批量, 下轮自动重试）
        try:
            import traceback
            tb = _BASE / "logs" / f"gen_error_{school}_{dept}.tb"
            tb.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description="批量院系提取规则生成（清华/北大优先）")
    ap.add_argument("--schools", nargs="+",
                    default=["清华大学", "北京大学"],
                    help="按优先级排列的学校列表")
    ap.add_argument("--workers", type=int, default=2, help="并行院系数（网关波动期保守）")
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--ready-only", action="store_true",
                    help="只跑本地已下载齐（missing==0）的院系")
    ap.add_argument("--skip-existing", action="store_true",
                    help="跳过已有 dept_rules JSON 的院系")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个院系")
    ap.add_argument("--write", action="store_true", help="通过回放即写盘 dept_rules/")
    ap.add_argument("--dry-run", action="store_true", help="只列目标院系不生成")
    args = ap.parse_args()

    schools = args.schools
    for s in schools:
        if s not in list_schools():
            print(f"⚠️ 学校不在名单: {s}")
    targets = dept_targets(schools, args.ready_only, args.skip_existing, args.limit)
    print(f"🎯 待生成院系 {len(targets)} 个（schools={schools} "
          f"ready_only={args.ready_only} write={args.write}）")
    if args.dry_run:
        for s, d in targets:
            print(f"  {s}/{d}")
        sys.exit(0)

    t0 = time.time()
    done = {"written": 0, "passed": 0, "failed": 0, "skip": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, s, d, args): (s, d) for s, d in targets}
        for i, f in enumerate(as_completed(futs)):
            s, d = futs[f]
            r = f.result()
            done[r["status"]] = done.get(r["status"], 0) + 1
            log(f"[{i + 1}/{len(targets)}] {s}/{d} → {r['status']} {r['detail']}"
                f"（累计: 写盘 {done['written']} / 失败 {done['failed']}"
                f" / 跳过 {done['skip']} / 异常 {done['error']}, "
                f"用时 {time.time() - t0:.0f}s）")

    print(f"\n✅ 批量完成: {done}（总用时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
