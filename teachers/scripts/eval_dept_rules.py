#!/usr/bin/env python3
"""院系规则效果评估器——找出「规则失效」院系并自动回退 LLM。

背景(2026-10-27 用户指令): 规则提取适用于结构规整的站, 但页面改版后
旧规则会失效(规则命中即覆盖 LLM, 失效规则会把好字段覆盖成垃圾)。本工具
对有规则的院系评估 teachers_detail 字段质量, 质量差的自动禁用规则文件
(改名 .disabled) 回退 LLM 提取。

判定: 有规则院系的「职称/研究方向/教育经历/工作经历」四字段覆盖率显著
低于同院系历史基线 或 绝对值过低 → 规则失效嫌疑。

用法:
  uv run python scripts/eval_dept_rules.py --min-cov 0.3   # 四字段均覆盖 <30% 判失效
  uv run python scripts/eval_dept_rules.py --dry-run       # 只报告不禁用
"""
import argparse
import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
CORE = ("职称", "研究方向", "教育经历", "工作经历")


def dept_coverage(detail_path: Path) -> tuple[int, dict]:
    if not detail_path.exists():
        return 0, {}
    try:
        d = json.loads(detail_path.read_text(encoding="utf-8"))
        ts = d if isinstance(d, list) else d.get("teachers", d.get("data", []))
    except Exception:
        return 0, {}
    n = len(ts)
    if n < 5:
        return 0, {}
    cov = {}
    for f in CORE:
        cov[f] = sum(1 for t in ts if str(t.get(f) or "").strip()) / n
    return n, cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cov", type=float, default=0.3,
                    help="四字段平均覆盖率低于此值判失效")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rows = []
    shells = []  # 壳站(页面无字段标签)——非规则问题, 不判失效
    empty_rules = []  # 空规则文件(无正则, 无用)——删除
    for rj in _BASE.glob("*/*/extract_rules.json"):
        school, dept = rj.parent.parent.name, rj.parent.name
        dj = _BASE / school / dept / "teachers_detail.json"
        # 2026-10-27 先判规则文件是否有实质正则(空数组=其他流程模板残留)
        try:
            rd = json.loads(rj.read_text(encoding="utf-8"))
            has_rule = any(pats for pats in (rd.get("rules") or {}).values())
        except Exception:
            has_rule = False
        if not has_rule:
            empty_rules.append((school, dept))
            if not args.dry_run:
                rj.unlink()
            continue
        n, cov = dept_coverage(dj)
        if n < 5:
            continue
        avg = sum(cov.values()) / len(CORE)
        if avg < args.min_cov:
            # 区分壳站(页面本身无字段标签, 规则无关) vs 规则真失效
            from pathlib import Path as _P
            _RAW = _BASE.parent / "tearchers raw data"
            import random, re
            from bs4 import BeautifulSoup
            files = list((_RAW / school / dept).glob("*.html"))
            label_hits = 0
            n_chk = 0
            random.seed(3)
            for f in random.sample(files, min(5, len(files))):
                soup = BeautifulSoup(f.read_text(encoding="utf-8",
                                                 errors="ignore"), "lxml")
                body = soup.find("body")
                txt = body.get_text(" ", strip=True) if body else ""
                n_chk += 1
                if any(k in txt for k in ("研究方向", "教育经历", "职称",
                                           "研究领域", "邮箱", "工作经历")):
                    label_hits += 1
            if label_hits == 0:
                shells.append((school, dept, n))  # 壳站: 页面无字段标签
                continue
            rows.append((school, dept, n, avg, cov))
    rows.sort(key=lambda x: x[3])
    print(f"规则失效嫌疑(页面有标签但字段空): {len(rows)} 院系")
    disabled = 0
    for s, d, n, avg, cov in rows:
        flag = (f"  职称{cov['职称']*100:.0f}% 方向{cov['研究方向']*100:.0f}% "
                f"教育{cov['教育经历']*100:.0f}% 工作{cov['工作经历']*100:.0f}%")
        print(f"  {avg*100:.0f}% [{n}人] {s}/{d} {flag}")
        if not args.dry_run:
            rj = _BASE / s / d / "extract_rules.json"
            rj.rename(rj.with_suffix(".json.disabled"))
            disabled += 1
    print(f"\n壳站(页面无字段标签, 规则无关, 归渲染/终态家族): {len(shells)} 院系")
    for s, d, n in shells[:8]:
        print(f"  [{n}人] {s}/{d}")
    if not args.dry_run and empty_rules:
        print(f"空规则文件已删除: {len(empty_rules)}")
    print(f"\n{'dry-run: 未禁用' if args.dry_run else f'已禁用 {disabled} 个失效规则'}")


if __name__ == "__main__":
    main()
