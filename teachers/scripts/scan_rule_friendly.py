#!/usr/bin/env python3
"""全库扫描「规则友好」院系——储备 LLM 资源不可用时的规则兜底能力。

架构定位(2026-10-27 用户定调): **LLM 直接提取是首选且效果最好**; 规则提取
仅作 LLM 不可用时的备选。本扫描找出「规则能可靠兜底」的院系(核心字段标签
覆盖率达标), 供 gen_dept_rules.py 生成规则; 候选门槛按「备选保险」定位放宽,
最终规则质量由生成器的样本验证命中门控把关。

用法: uv run python scripts/scan_rule_friendly.py
输出: logs/_rule_friendly_depts.tsv (学校/院系/覆盖率/样本数/标签命中)
"""
import json
import random
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_RAW = _BASE.parent / "tearchers raw data"
_TCH = _BASE

LABELS = {
    "职称": ["职称", "职务", "头衔", "职称职称"],
    "教育经历": ["教育经历", "教育背景", "学习经历", "学历", "求学经历", "受教育经历"],
    "工作经历": ["工作经历", "工作履历", "任职经历", "职业经历", "从业经历"],
    "研究方向": ["研究方向", "研究领域", "科研方向", "研究兴趣", "主攻方向", "研究专长"],
    "学术成果": ["学术成果", "科研成果", "代表性成果", "发表论文", "论文", "专利", "著作", "论著", "学术论文"],
    "联系方式": ["联系方式", "邮箱", "电子邮箱", "E-mail", "Email", "电子邮件"],
    "荣誉": ["荣誉", "获奖", "奖励", "荣誉称号", "所获荣誉"],
    "讲授课程": ["讲授课程", "主讲课程", "承担课程", "开设课程", "课程名称"],
}


def scan(dj_path: str) -> tuple | None:
    dj = Path(dj_path)
    school, dept = dj.parent.parent.name, dj.parent.name
    files = list((_RAW / school / dept).glob("*.html"))
    if len(files) < 5:
        return None
    random.seed(abs(hash(dj_path)) % 100000)
    sample = random.sample(files, min(5, len(files)))
    hits = Counter()
    n_ok = 0
    for f in sample:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", txt)
            txt = re.sub(r"<[^>]+>", " ", txt)
        except Exception:
            continue
        if len(txt) < 300:
            continue
        n_ok += 1
        for field, kws in LABELS.items():
            if any(k in txt for k in kws):
                hits[field] += 1
    if n_ok < 3:
        return None
    core = ["职称", "教育经历", "研究方向", "联系方式"]
    cov = sum(1 for f in core if hits[f] >= max(1, int(n_ok * 0.6))) / len(core)
    # 2026-10-27 扩展: 阈值 0.75→0.5(规则是 LLM 备选保险, 候选放宽,
    # 最终质量由生成器命中门控把关)
    if cov >= 0.5:
        return (school, dept, cov, n_ok, dict(hits))
    return None


def main():
    djs = list(_TCH.glob("*/*/teachers_detail.json"))
    print(f"扫描 {len(djs)} 个院系...", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(scan, str(dj)): dj for dj in djs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                rows.append(r)
            if done % 1000 == 0:
                print(f"  {done}/{len(djs)}", flush=True)
    rows.sort(key=lambda x: -x[2])
    with open(_BASE / "logs" / "_rule_friendly_depts.tsv", "w",
              encoding="utf-8") as f:
        f.write("学校\t院系\t核心覆盖率\t样本数\t标签命中\n")
        for s, d, cov, n, hits in rows:
            f.write(f"{s}\t{d}\t{cov:.2f}\t{n}\t"
                    f"{json.dumps(hits, ensure_ascii=False)}\n")
    print(f"\n规则友好候选: {len(rows)} 院系 → logs/_rule_friendly_depts.tsv")
    for s, d, cov, n, hits in rows[:15]:
        print(f"  {cov*100:.0f}%  {s}/{d}")


if __name__ == "__main__":
    main()
