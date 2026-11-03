#!/usr/bin/env python3
"""导出未完成院系清单（重采准备清单生成器, 2026-10-08 阶段关闭时创建）。

扫描 output/<学校>/<院系>/test_report.json 的状态字段, 将「未完成」状态
(reopened / fail_extraction / fail_ip_blocked / pending_recollec /
 no_faculty_page / skip_candidate / no_url / pending_human / spa /
 budget_timeout / retry) 的院系导出为 TSV, 附教师数与院系URL(来自各校
departments.csv), 供重新采集时直接读取建批。

用法: uv run python script/export_unfinished.py
产物: logs/_unfinished_depts.tsv (全量未完成) + 终端打印状态分布
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

# 未完成状态池（终态 done/deterministic_done/collected/verified_small_center/
# verified_pure_text/no_teacher_org/skip_no_teacher_org/legal_empty 不在此列）
# 2026-10-12: skip_candidate=已归档专项候选(终态, 不再重复测) — 移出未完成,
# 另设候选计数单独统计(归档台账口径修复)
UNFINISHED = {
    "reopened", "fail_extraction", "fail_ip_blocked", "pending_recollect",
    "no_faculty_page", "no_url", "pending_human_analysis",
    "spa_platform_special", "partial_roster", "budget_timeout", "retry",
    "fail", "fail_network_unreachable", "deterministic_failed",
    "url_mismatch_special", "unreachable", "no_researcher_page",
    "unreachable_or_no_faculty",
}


def _urls_by_school() -> dict:
    """各校 departments.csv: (学校,院系) -> URL。
    2026-10-20 修复: 同名多行后者空 URL 覆盖前者(中财「法学院」vs「法学院、
    纪检监察研究院」) + 目录名与名单名带后缀差异(上体「马克思主义学院／
    SchoolofMarxismStudies」) → ①空 URL 不覆盖已有非空; ②精确失败后前缀
    匹配(院系名与名单名互为前缀)。"""
    m = {}
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        csvf = s / "departments.csv"
        if not csvf.exists():
            continue
        try:
            with open(csvf, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = (s.name, row.get("院系名称", ""))
                    u = (row.get("院系URL") or "").strip()
                    if u and not m.get(key):  # 空 URL 不覆盖非空
                        m[key] = u
        except Exception:
            pass
    return m


def _find_url(urls, school, dept):
    """精确 → 归一化前缀 → 中文主名匹配。2026-10-20 修复: 目录名与名单名
    存在全角/半角分隔符差异(上体「体育教育学院／School…」全角斜杠 vs 名单
    半角斜杠) — 归一化后按「/」截断中文主名精确匹配。"""
    u = urls.get((school, dept), "")
    if u:
        return u
    dept_n = dept.replace("／", "/").replace("　", " ").strip()
    dept_main = dept_n.split("/")[0].strip()
    if not dept_main:
        return ""
    for (s2, name), u2 in urls.items():
        if s2 != school or not u2:
            continue
        name_n = name.replace("／", "/").replace("　", " ").strip()
        name_main = name_n.split("/")[0].strip()
        if name_main and dept_main == name_main:
            return u2
        if dept_n.startswith(name_n) or name_n.startswith(dept_n):
            return u2
    return ""


def main():
    urls = _urls_by_school()
    dist = Counter()
    rows = []
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        for d in s.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            tr = d / "test_report.json"
            status = ""
            reason = ""
            if tr.exists():
                try:
                    j = json.load(open(tr, encoding="utf-8"))
                    status = j.get("status", "")
                    reason = (j.get("reason") or "")[:80]
                except Exception:
                    status = "bad_json"
            dist[status or "(无标记)"] += 1
            if status in UNFINISHED:
                n = 0
                t = d / "teachers.csv"
                if t.exists():
                    try:
                        with open(t, encoding="utf-8") as f:
                            n = max(0, sum(1 for _ in f) - 1)
                    except Exception:
                        pass
                rows.append([s.name, d.name, status, reason, n,
                             _find_url(urls, s.name, d.name)])

    out = LOGS / "_unfinished_depts.tsv"
    rows.sort(key=lambda r: (r[2], -r[4], r[0], r[1]))
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["学校", "院系", "状态", "原因", "教师数", "院系URL"])
        w.writerows(rows)

    print("=== test_report.json 状态分布 ===")
    for k, v in dist.most_common():
        tag = " ←未完成" if k in UNFINISHED else ""
        print(f"{v:>6}  {k}{tag}")
    print(f"\n未完成合计: {sum(v for k, v in dist.items() if k in UNFINISHED)} "
          f"→ {out}")


if __name__ == "__main__":
    main()
