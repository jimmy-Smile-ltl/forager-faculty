#!/usr/bin/env python3
"""规则可用性测试器——每个 extract_rules.json 实测样本命中, 标记可用性。

背景(2026-10-27 用户要求): 规则不能随便乱写——每个规则文件都要在
实际样本上测试命中率, 不可用的标记/禁用, 防止「纸面规则」蒙混。

测试: 抽样 3 html(同提取侧口径 BeautifulSoup 文本化) → 逐字段跑正则
→ 值过 _value_ok 校验 → 统计命中。字段命中率 ≥60% 记可用, 30-60% 记弱,
<30% 记不可用。不可用的规则文件改后缀 .disabled(回退 LLM 备选=无规则)。

用法:
  uv run python scripts/test_dept_rules.py --dry-run   # 只报告
  uv run python scripts/test_dept_rules.py             # 不可用的禁用
输出: logs/_rule_test_report.tsv
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

_BASE = Path(__file__).resolve().parent.parent
_RAW = _BASE.parent / "tearchers raw data"


def _text(f: Path) -> str:
    soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="ignore"),
                         "lxml")
    body = soup.find("body")
    return body.get_text("\n", strip=True) if body else ""


def _value_ok(val: str, chosen_words: set) -> bool:
    val = val.strip()
    if not val:
        return False
    if any(val.startswith(w) for w in chosen_words):
        return False
    if "-->" in val or "<?xml" in val:
        return False
    return len(re.findall(r"[一-鿿A-Za-z0-9@.\-]", val)) >= 5


def test_one(rj: Path) -> dict:
    school, dept = rj.parent.parent.name, rj.parent.name
    try:
        d = json.loads(rj.read_text(encoding="utf-8"))
        rules = d.get("rules") or {}
    except Exception:
        return {"status": "json错误"}
    files = list((_RAW / school / dept).glob("*.html"))
    if len(files) < 3:
        return {"status": "样本不足"}
    random.seed(abs(hash((school, dept))) % 100000)
    sample = random.sample(files, min(3, len(files)))
    texts = [t for t in (_text(f) for f in sample) if len(t) >= 300]
    if len(texts) < 2:
        return {"status": "无有效样本(壳站)"}
    all_words = set()
    for pats in rules.values():
        for pat in pats:
            if isinstance(pat, str):
                all_words.update(re.findall(r"[一-鿿]{2,8}", pat))
    field_hits = {}
    for field, pats in rules.items():
        n = 0
        for t in texts:
            for pat in pats:
                try:
                    if isinstance(pat, str):
                        m = re.search(pat, t)
                        if m and m.lastindex and _value_ok(m.group(1), all_words):
                            n += 1
                            break
                    elif isinstance(pat, dict) and pat.get("css"):
                        # CSS 规则: 用文件实测(html_path)
                        pass
                except Exception:
                    continue
        field_hits[field] = n
    n_fields = len(rules)
    if n_fields == 0:
        return {"status": "空规则"}
    usable = sum(1 for f, n in field_hits.items() if n >= 2)  # 3样本≥2命中
    ratio = usable / n_fields
    if ratio >= 0.6:
        status = "可用"
    elif ratio >= 0.3:
        status = "弱"
    else:
        status = "不可用"
    return {"status": status, "fields": f"{usable}/{n_fields}",
            "detail": {f: n for f, n in field_hits.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rjs = list(_BASE.glob("*/*/extract_rules.json"))
    report = []
    stat = {"可用": 0, "弱": 0, "不可用": 0, "样本不足": 0,
            "无有效样本(壳站)": 0, "json错误": 0, "空规则": 0}
    disabled = 0
    for rj in rjs:
        r = test_one(rj)
        stat[r["status"]] = stat.get(r["status"], 0) + 1
        school, dept = rj.parent.parent.name, rj.parent.name
        report.append((school, dept, r["status"], r.get("fields", "")))
        if r["status"] == "不可用" and not args.dry_run:
            rj.rename(rj.with_suffix(".json.disabled"))
            disabled += 1
    report.sort(key=lambda x: (x[2], x[0], x[1]))
    with open(_BASE / "logs" / "_rule_test_report.tsv", "w",
              encoding="utf-8") as f:
        f.write("学校\t院系\t状态\t字段命中\n")
        for s, d, st, fl in report:
            f.write(f"{s}\t{d}\t{st}\t{fl}\n")
    print(f"测试 {len(rjs)} 个规则文件:")
    for k in ("可用", "弱", "不可用", "样本不足", "无有效样本(壳站)",
              "json错误", "空规则"):
        if stat.get(k):
            print(f"  {k}: {stat[k]}")
    if not args.dry_run:
        print(f"已禁用不可用规则: {disabled}")
    else:
        print("(dry-run: 未禁用)")
    print("报告 → logs/_rule_test_report.tsv")


if __name__ == "__main__":
    main()
