#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
depart_985211.jsonl 采集结果评估分析 v3

v3 新增:
  - 质量评分 (0-100): 子域多样性 / 名称质量 / URL模式 / 数量vs学校类型
  - 低分学校排行 + 修复建议
  - 质量分分布直方图
  - 验证覆盖统计

保留 v2 全部章节: 成功/失败、规模、类型分布、≤5明细、失败详情、采集方式、数据质量诊断

运行:
  cd "<repo-root>" && uv run python "colleges department/evaluate_results.py"
"""

import json, re, sys, csv
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

DATA_FILE = Path(__file__).resolve().parent / "depart_985211.jsonl"
FIXES_FILE = Path(__file__).resolve().parent / "URL_FIXES.json"
SCHOOLS_FILE = Path(__file__).resolve().parent / "985_211_双一流高校清单.csv"

LOW_THRESHOLD = 5
QUALITY_THRESHOLD = 60  # 低于此分标记

# ── 质量验证常量（与 scrape_departments.py v4 一致）─
NAV_PATTERNS = ["概况", "简介", "设置", "列表", "首页", "历史", "沿革", "机构"]
SPECIAL_SCHOOL_TAGS = ["音乐", "戏剧", "美术", "体育", "语言", "警察", "公安", "民族", "外交"]
NUMERIC_PATH_RE = re.compile(r"/\d+/list\.htm")


def load_records(path):
    if not path.exists():
        sys.exit(f"[错误] {path} 不存在")
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def load_fixes(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_schools(path):
    schools = {}
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                schools[r["学校名称"]] = r
    return schools


def classify_method(method: str) -> str:
    m = method or ""
    if m.startswith("CloakBrowser"):
        return "CloakBrowser"
    if m.startswith("CDP bridge"):
        return "CDP bridge"
    if m.startswith("skip:"):
        return "skip/手动"
    if m.startswith("LLM classify") or m.startswith("manual_url → LLM"):
        return "curl_cffi + LLM"
    if m.startswith("PW:"):
        return "Playwright"
    if any(k in m for k in ["dept page not found", "dept page fetch failed",
                              "homepage fetch failed", "no candidates",
                              "manual_review_needed"]):
        return "失败/需人工"
    if "LLM" in m:
        return "curl_cffi + LLM"
    return "其他"


def method_brief(method: str) -> str:
    m = method or ""
    if m.startswith("LLM classify"):
        return "LLM classify"
    if m.startswith("manual_url → LLM"):
        return "manual_url → LLM"
    if m.startswith("url_fixes → LLM"):
        return "url_fixes → LLM"
    if m.startswith("fallback path → LLM"):
        return "fallback → LLM"
    if m.startswith("CloakBrowser headless → LLM"):
        return "CloakBrowser → LLM"
    if m.startswith("CDP bridge → LLM"):
        return "CDP → LLM"
    if m.startswith("CDP → LLM"):
        return "CDP → LLM"
    if m.startswith("skip:"):
        return "skip"
    if m.startswith("PW:"):
        return "Playwright 失败"
    if "manual_review_needed" in m:
        return "需人工"
    return m.split("(")[0].strip()[:28]


def section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)


# ── 质量评分（独立实现，不依赖 scrape_departments.py）─
def _first_label(url: str) -> str:
    host = urlparse(url).netloc
    return host.split(".")[0] if host else ""


def quality_score(school_name: str, school_info: dict,
                  departments: list[dict], dept_page_url: str = "") -> tuple:
    """
    返回 (score: int, ok: bool, issues: list[str])
    """
    n = len(departments)
    if n == 0:
        return 0, False, ["empty"]

    issues = []
    fatal = False
    deductions = 0

    is_special = any(tag in school_name for tag in SPECIAL_SCHOOL_TAGS)
    is_985 = str(school_info.get("is_985", "")).strip() == "1"

    # A: 子域多样性 + dept_page_url 子域检查
    labels = [_first_label(d.get("url", "")) for d in departments]
    unique_labels = set(labels)
    lc = Counter(labels)
    dl = _first_label(dept_page_url) if dept_page_url else ""

    if dl and dl != "www" and n >= 3 and not is_special:
        ratio = lc.get(dl, 0) / n
        if ratio >= 0.4 and n < 10:
            deductions += 25
            issues.append(f"dept_page_is_subdomain:{dl}")
            if ratio >= 0.6 and n <= 8:
                fatal = True
                issues.append("FATAL_SUB_SITE")

    if n >= 5 and len(unique_labels) <= 2:
        if len(unique_labels) == 1 and "www" in unique_labels:
            deductions += 10
        else:
            deductions += 20
            issues.append("low_label_diversity")

    # B: 名称质量
    nav_count = sum(1 for d in departments if any(p in d.get("name", "") for p in NAV_PATTERNS))
    if nav_count >= max(2, 0.25 * n):
        deductions += min(25, int(25 * nav_count / n))
        issues.append(f"nav:{nav_count}")

    # C: URL 模式
    num_paths = sum(1 for d in departments
                    if NUMERIC_PATH_RE.search(d.get("url", ""))
                    and _first_label(d.get("url", "")) == "www")
    if not is_special and num_paths >= max(2, 0.5 * n):
        deductions += min(25, int(25 * num_paths / n))
        issues.append(f"num:{num_paths}")
        if num_paths >= 0.5 * n and len(unique_labels) <= 2:
            fatal = True
            issues.append("FATAL")

    # D: 数量 vs 学校类型
    types = Counter(d.get("type", "") for d in departments)
    dom = types.most_common(1)[0] if types else ("", 0)
    if not is_special:
        if is_985 and n <= 10 and dom[1] / n >= 0.7:
            deductions += 20
            issues.append(f"anomaly:{n}")
        elif n <= 7 and len(unique_labels) <= 2:
            deductions += 15
            issues.append("low_count")

    score = max(0, 100 - deductions)
    ok = score >= 60 and not fatal
    return score, ok, issues


def gen_fix_suggestion(name: str, method: str, issues: list[str],
                       school_info: dict, fixes: dict) -> str:
    """为低分学校生成修复建议"""
    parts = []

    if "manual_review_needed" in method:
        parts.append("人工: URL_FIXES.json 指定院系列表页 URL 后重跑")
    if name in fixes:
        f = fixes[name]
        if f.get("suggestion"):
            parts.append(f"已有修复: {f['suggestion'][:50]}")
        if f.get("note"):
            parts.append(f.get("note", "")[:60])
    if any("subdomain" in i or "FATAL_SUB_SITE" in i for i in issues):
        parts.append("dept_page指向子站；检查首页'学院设置'入口")
    if any("num:" in i or "FATAL" in i for i in issues):
        parts.append("列表页疑似文章/历史页；参考同模式修正路径(重庆大学jgsz/jxkyjg.htm)")
    if any("nav:" in i for i in issues):
        parts.append("院系名称含导航词(概况/简介)；可能是导航页非列表页")
    if any("anomaly" in i for i in issues):
        parts.append("985综合性大学院系异常偏少；可能只采集到部分类型")

    is_special = any(tag in name for tag in SPECIAL_SCHOOL_TAGS)
    if is_special and len(issues) == 1 and "low_label_diversity" in issues[0]:
        parts.append("特殊院校(音乐/美术/体育)，可能正常但需确认")

    return " | ".join(parts) if parts else "—"


def main():
    records = load_records(DATA_FILE)
    fixes = load_fixes(FIXES_FILE)
    all_schools = load_schools(SCHOOLS_FILE)

    print(f"数据文件: {DATA_FILE}")
    print(f"记录总数: {len(records)}")

    successes = [r for r in records if r.get("departments")]
    failures = [r for r in records if not r.get("departments")]
    all_depts = [d for r in successes for d in r["departments"]]

    # ──── 一、成功/失败 ────
    section("一、成功/失败统计")
    print(f"  成功: {len(successes)} / {len(records)}  ({len(successes)/len(records)*100:.1f}%)")
    print(f"  失败: {len(failures)} / {len(records)}  ({len(failures)/len(records)*100:.1f}%)")

    skipped = [r for r in failures if "skip" in r.get("method", "")]
    real_fail = [r for r in failures if r not in skipped]
    manual_review = [r for r in records if "manual_review_needed" in r.get("method", "")]
    if skipped:
        print(f"  刻意跳过: {len(skipped)} 所")
    if manual_review:
        print(f"  需人工核查: {len(manual_review)} 所")
    if real_fail:
        print(f"  真正失败: {len(real_fail)} 所")

    # ──── 二、规模统计 ────
    section("二、院系规模统计")
    total = len(all_depts)
    dept_counts = [len(r["departments"]) for r in successes]
    avg_all = total / len(records)
    avg_ok = total / len(successes) if successes else 0
    print(f"  院系总数:       {total}")
    print(f"  平均每校:       {avg_all:.1f} (含失败) / {avg_ok:.1f} (仅成功)")
    print(f"  范围:           {min(dept_counts)} ~ {max(dept_counts)}")
    print(f"  中位数:         {sorted(dept_counts)[len(dept_counts)//2]}")

    # ──── 三、类型分布 ────
    section("三、院系类型分布")
    raw_types = Counter(d.get("type", "") for d in all_depts)
    for t, c in raw_types.most_common():
        bar = "█" * max(1, c * 40 // total)
        print(f"  {t or '(空)':<14} {c:>5} ({c/total*100:5.1f}%) {bar}")

    groups = {
        "学院": ["学院"], "研究院": ["研究院"], "研究所": ["研究所"],
        "中心": ["中心", "研究中心", "教学中心", "实验教学中心"],
        "实验室": ["实验室", "重点实验室"],
        "系/教学部": ["系", "教学部", "部", "学系", "教学科研", "教学科研部"],
        "书院": ["书院"], "学部": ["学部"], "基地": ["基地", "研究基地"],
        "医院": ["医院"],
    }
    grouped = Counter()
    for t, c in raw_types.items():
        found = False
        for g, members in groups.items():
            if t in members:
                grouped[g] += c
                found = True
                break
        if not found and t:
            grouped[f"其他({t})"] += c
        elif not t:
            grouped["无类型"] += c

    print(f"\n  -- 归一化 --")
    for g, c in grouped.most_common():
        print(f"  {g:<14} {c:>5} ({c/total*100:5.1f}%)")

    # ──── 四、≤5 院校 ────
    section(f"四、院系数 ≤ {LOW_THRESHOLD} 的学校（疑似 URL 匹配错误）")
    low = [(r["school_name"], len(r["departments"]), r.get("dept_page_url", ""),
            r.get("method", ""), r["departments"])
           for r in successes if len(r["departments"]) <= LOW_THRESHOLD]
    low.sort(key=lambda x: x[1])

    if low:
        print(f"  共 {len(low)} 所\n")
        for name, n, url, method, depts in low:
            dept_types = Counter(d.get("type", "?") for d in depts)
            dt_str = ",".join(f"{t}×{c}" for t, c in dept_types.most_common(3))
            url_short = (url or "(无)")[:60]
            fix = fixes.get(name, {})
            suggestion = fix.get("suggestion", "")[:50] if fix else ""
            print(f"  {name:<14} {n:>4} {dt_str:<8} {url_short:<60} {suggestion}")
    else:
        print("  (无)")

    # ──── 五、失败详情 ────
    if failures or manual_review:
        section("五、失败/跳过/需人工学校")
        for r in failures:
            method = r.get("method", "")
            dept_url = r.get("dept_page_url", "")
            attempts = r.get("attempts", [])
            print(f"  {r['school_name']:<14} → {method[:80]}")
            if dept_url:
                print(f"    dept_page_url: {dept_url}")
            if attempts:
                for a in attempts[:3]:
                    print(f"    try: {a.get('label','')} → {a.get('reason','')[:60]}")
        for r in manual_review:
            if r in failures:
                continue
            attempts = r.get("attempts", [])
            print(f"  {r['school_name']:<14} → {r.get('method','')[:80]}")

    # ──── 六、采集方式统计 ────
    section("六、采集方式统计")
    fams = Counter(classify_method(r.get("method", "")) for r in records)
    for fam, c in fams.most_common():
        ok = sum(1 for r in records if classify_method(r.get("method", "")) == fam and r.get("departments"))
        pct = ok / c * 100 if c else 0
        print(f"  {fam:<22} {c:>4} 校 (成功 {ok}/{c} = {pct:.0f}%)")

    print(f"\n  -- 方法明细 --")
    briefs = Counter(method_brief(r.get("method", "")) for r in records)
    for b, c in briefs.most_common():
        ok = sum(1 for r in records if method_brief(r.get("method", "")) == b and r.get("departments"))
        print(f"  {b:<28} {c:>4} 校 (成功 {ok}/{c})")

    # ──── 七、数据质量诊断 ────
    section("七、数据质量诊断")
    no_name = sum(1 for d in all_depts if not d.get("name"))
    no_url = sum(1 for d in all_depts if not d.get("url"))
    no_type = sum(1 for d in all_depts if not d.get("type"))
    print(f"  缺少 name: {no_name}  缺少 url: {no_url}  缺少 type: {no_type}")

    bad = [(r["school_name"], d["name"], d.get("type", ""), d.get("url", ""))
           for r in successes for d in r["departments"]
           if d.get("type") in ("", "导航", "导航页", "导航页面", "党团", "行政") or not d.get("type")]
    if bad:
        print(f"\n  类型异常的院系 ({len(bad)} 条):")
        for school, dname, t, url in bad[:15]:
            print(f"    {school}: [{t or '(空)'}] {dname[:35]}")

    xi_schools = [(r["school_name"],
                   len([d for d in r["departments"] if d.get("type") in ("系", "教学科研")]),
                   len(r["departments"]))
                  for r in successes
                  if sum(1 for d in r["departments"]
                         if d.get("type") in ("系", "教学科研")) / max(len(r["departments"]), 1) > 0.5
                  and len(r["departments"]) >= 3]
    if xi_schools:
        print(f"\n  以'系'为主的学校 ({len(xi_schools)} 所):")
        for name, xi_n, total_n in xi_schools:
            print(f"    {name}: {xi_n} 系 / {total_n} 总")

    # ──── 八、质量评分 (v3 新增) ────
    section("八、质量评分 (0-100)")
    scored = []
    for r in records:
        name = r["school_name"]
        depts = r.get("departments", [])
        dpurl = r.get("dept_page_url", "")
        info = all_schools.get(name, {})
        if "validation" in r and "score" in r.get("validation", {}):
            # 使用 scraped 记录中的已有验证
            v = r["validation"]
            score = v.get("score", 0)
            ok = v.get("ok", False)
            issues = v.get("issues", [])
        else:
            # v3 旧记录没有 validation → 重新计算
            score, ok, issues = quality_score(name, info, depts, dpurl)
        scored.append((score, ok, name, len(depts), issues, r.get("method", "")))

    scored.sort()

    # 分布直方图
    buckets = [("0-19", 0), ("20-39", 0), ("40-59", 0), ("60-79", 0), ("80-100", 0)]
    for s, _, _, _, _, _ in scored:
        if s < 20:
            buckets[0] = (buckets[0][0], buckets[0][1] + 1)
        elif s < 40:
            buckets[1] = (buckets[1][0], buckets[1][1] + 1)
        elif s < 60:
            buckets[2] = (buckets[2][0], buckets[2][1] + 1)
        elif s < 80:
            buckets[3] = (buckets[3][0], buckets[3][1] + 1)
        else:
            buckets[4] = (buckets[4][0], buckets[4][1] + 1)

    avg_score = sum(s for s, _, _, _, _, _ in scored) / len(scored)
    flagged = [s for s in scored if s[0] < QUALITY_THRESHOLD]

    print(f"  总校数: {len(scored)} | 平均分: {avg_score:.1f} | 阈值: {QUALITY_THRESHOLD}分")
    for label, cnt in buckets:
        bar = "█" * max(1, cnt * 40 // len(scored))
        flag_mark = " ⚠️" if label in ("0-19", "20-39", "40-59") else ""
        print(f"  {label:<8} {cnt:>3} {bar}{flag_mark}")

    if flagged:
        print(f"\n  📋 低分学校 ({len(flagged)}所 得分<{QUALITY_THRESHOLD}):")
        print(f"  {'得分':>4} {'学校':<16} {'院系':>4} {'通过':>4} {'验证来源':<12} 问题 → 修复建议")
        print(f"  {'─'*4} {'─'*16} {'─'*4} {'─'*4} {'─'*12} {'─'*60}")
        for score, ok, name, n, issues, method in flagged:
            info = all_schools.get(name, {})
            rec = next((r for r in records if r["school_name"] == name), {})
            ver_src = "采集自检" if rec.get("validation") else "评估补算"
            ok_str = "✅" if ok else "❌"
            fix = gen_fix_suggestion(name, method, issues, info, fixes)
            print(f"  {score:>4} {name:<16} {n:>4} {ok_str:<4} {ver_src:<12} {fix[:60]}")
    else:
        print(f"\n  全部通过！无得分<{QUALITY_THRESHOLD}的学校")

    # ❗ 展示得分≥60但验证不通过（有FATAL标志）的学校
    fatal_but_passing = [(s, o, n, d, i, m) for s, o, n, d, i, m in scored
                         if s >= QUALITY_THRESHOLD and not o]
    if fatal_but_passing:
        print(f"\n  📋 得分≥{QUALITY_THRESHOLD}但不通过 ({len(fatal_but_passing)}所，有致命标志):")
        print(f"  {'得分':>4} {'学校':<16} {'院系':>4} {'通过':>4} 问题 → 修复建议")
        print(f"  {'─'*4} {'─'*16} {'─'*4} {'─'*4} {'─'*60}")
        for score, ok, name, n, issues, method in fatal_but_passing:
            info = all_schools.get(name, {})
            fix = gen_fix_suggestion(name, method, issues, info, fixes)
            print(f"  {score:>4} {name:<16} {n:>4} ❌    {fix[:60]}")

    # ──── 九、验证覆盖统计 (v3 新增) ────
    section("九、验证覆盖统计")
    has_validation = [r for r in records if "validation" in r]
    no_validation = [r for r in records if "validation" not in r]
    print(f"  含验证数据: {len(has_validation)} 所 (来自 v4 采集自检)")
    print(f"  无验证数据: {len(no_validation)} 所 (v3 旧记录或手工补录)")

    if no_validation:
        methods = Counter(method_brief(r.get("method", "")) for r in no_validation)
        print(f"  无验证记录的采集方式:")
        for m, c in methods.most_common():
            print(f"    {m}: {c} 所")

    # ──── 十、改进建议 ────
    section("十、改进建议")
    issues_list = []

    if flagged:
        issues_list.append(f"{len(flagged)} 所学校质量分 < {QUALITY_THRESHOLD}，需确认结果或修复 URL")
    if no_validation:
        issues_list.append(f"{len(no_validation)} 所无自验证记录，建议用 v4 重跑")
    if manual_review:
        issues_list.append(f"{len(manual_review)} 所标记 manual_review_needed，需人工指定 URL")
    if real_fail:
        issues_list.append(f"{len(real_fail)} 所真正失败需要重新采集")
    if no_type > 0:
        issues_list.append(f"{no_type} 条院系缺少 type 字段")

    for i, iss in enumerate(issues_list, 1):
        print(f"  {i}. {iss}")

    print(f"\n{'=' * 70}")
    print(f"  评估完成 — 数据文件: {DATA_FILE}")
    print(f"{'=' * 70}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
