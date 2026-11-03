#!/usr/bin/env python3
"""规则自动生成器——为「规则友好」院系自动生成 extract_rules.json。

架构定位(2026-10-27 用户定调): **LLM 直接提取是首选且效果最好**; 院系级
规则只是 LLM 资源不可用(无凭证/网关故障/额度耗尽)时的**备选提取路径**——
规则生成的目标是「兜底可靠」, 不追求替代 LLM, 更不许覆盖 LLM 的好结果。

原理:
  1. 抽样 html → 纯文本 → 检测该院系实际使用的字段标签词
  2. 每字段生成锚点起止正则: {标签词}\s*([\s\S]{5,600}?)(?={全部其他标签词}|$)
     (前瞻集合含全部其他标签——不依赖标签顺序, 页面节序各异也命中)
  3. 样本验证命中率 ≥ MIN_HIT 才落盘——烂规则不落盘(提取侧 LLM 优先, 规则只补空)
  4. 已有 extract_rules.json 的院系跳过(改进工作流手写规则优先)

用法:
  uv run python scripts/gen_dept_rules.py --list logs/_rule_friendly_depts.tsv
  uv run python scripts/gen_dept_rules.py --list ... --min-hit 0.6
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_RAW = _BASE.parent / "tearchers raw data"

# 字段 → 候选标签词(按优先序, 检测时取院系实际出现的)
FIELD_LABELS = {
    "职称": ["职称", "职务", "头衔"],
    "教育经历": ["教育经历", "教育背景", "学习经历", "学历", "求学经历", "受教育经历"],
    "工作经历": ["工作经历", "工作履历", "任职经历", "职业经历", "从业经历"],
    "研究方向": ["研究方向", "研究领域", "科研方向", "研究兴趣", "主攻方向", "研究专长"],
    "学术成果": ["学术成果", "科研成果", "代表性成果", "发表论文", "论著", "学术论文"],
    "荣誉": ["荣誉奖项", "获奖", "奖励", "荣誉", "荣誉称号"],
    "讲授课程": ["讲授课程", "主讲课程", "承担课程", "开设课程"],
    "联系方式": ["邮箱", "电子邮箱", "E-mail", "Email", "联系方式", "电子邮件"],
    "通讯地址": ["通讯地址", "地址", "办公室"],
}


def _text(f: Path) -> str:
    """与提取侧 extract_text_from_html 同口径: BeautifulSoup 文本化
    (自动生成器 v1 用正则去标签, HTML 注释/JS 残留进值——'3 -->'、
    '情况 -->' 垃圾值实锤; 必须同口径)。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="ignore"),
                         "lxml")
    body = soup.find("body")
    return body.get_text("\n", strip=True) if body else ""


def _value_ok(val: str, chosen: dict) -> bool:
    """值质量校验(空节/标签前缀/HTML残留拦截——v1 教训):
    值不以任何标签词开头(空节), 不含 -->(注释残留), 有效内容 ≥5 汉字。"""
    val = val.strip()
    if any(val.startswith(v) for v in chosen.values()):
        return False
    if "-->" in val or "<?xml" in val:
        return False
    return len(re.findall(r"[一-鿿]", val)) >= 5


def gen_for_dept(school: str, dept: str, min_hit: float) -> tuple[bool, dict]:
    out_dir = _BASE / school / dept
    if (out_dir / "extract_rules.json").exists():
        return False, {}  # 已有规则, 跳过
    files = list((_RAW / school / dept).glob("*.html"))
    if len(files) < 5:
        return False, {}
    random.seed(abs(hash((school, dept))) % 100000)
    sample = random.sample(files, min(6, len(files)))
    texts = []
    for f in sample:
        t = _text(f)
        if len(t) >= 300:
            texts.append(t)
    if len(texts) < 3:
        return False, {}
    # 每字段选该院系实际出现的标签词(优先序第一个出现的, 且 ≥60% 页面出现)
    chosen = {}
    for field, kws in FIELD_LABELS.items():
        for kw in kws:
            n = sum(1 for t in texts if kw in t)
            if n >= max(1, int(len(texts) * 0.6)):
                chosen[field] = kw
                break
    if len(chosen) < 3:
        return False, {}
    # 前瞻集合 = 全部已选标签词(除自身)
    rules = {}
    for field, kw in chosen.items():
        others = sorted({v for k, v in chosen.items() if k != field})
        lookahead = "|".join(re.escape(o) for o in others)
        rules[field] = [
            rf"{re.escape(kw)}\s*([\s\S]{{5,600}}?)(?={lookahead}|$)"]
    # 样本验证: 各字段命中率(v2 严格值校验: _value_ok)
    hits = {f: 0 for f in rules}
    for t in texts:
        for field, pats in rules.items():
            m = re.search(pats[0], t)
            if m and _value_ok(m.group(1), chosen):
                hits[field] += 1
    ok_fields = [f for f, n in hits.items() if n >= max(1, int(len(texts) * min_hit))]
    if len(ok_fields) < 3:
        return False, {}
    final_rules = {f: rules[f] for f in ok_fields}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "extract_rules.json").write_text(
        json.dumps({
            "school": school, "dept": dept,
            "用途": "llm_fallback",  # LLM 资源不可用时的备选; 正常提取 LLM 直接提取优先
            "templates": f"自动生成(2026-10-27 规则友好扫描): 标签锚点起止正则, "
                         f"样本 {len(texts)} 页, 命中门控 {min_hit}",
            "rules": final_rules,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return True, final_rules


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default="logs/_rule_friendly_depts.tsv")
    ap.add_argument("--min-hit", type=float, default=0.6)
    args = ap.parse_args()
    import csv
    targets = []
    with open(_BASE / args.list, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            targets.append((r["学校"], r["院系"]))
    made = 0
    skipped_has = 0
    failed = 0
    for i, (s, d) in enumerate(targets):
        ok, rules = gen_for_dept(s, d, args.min_hit)
        if ok:
            made += 1
        elif (Path(_BASE) / s / d / "extract_rules.json").exists():
            skipped_has += 1
        else:
            failed += 1
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(targets)} 已生成 {made}", flush=True)
    print(f"\n生成 {made} 个规则文件 | 已有跳过 {skipped_has} | 未达标 {failed}")


if __name__ == "__main__":
    main()
