#!/usr/bin/env python3
"""
教师采集结果评估脚本 — 通用数据质量分析

功能:
  1. 数量统计: 各院系教师数、全校总数
  2. 缺失分析: 详情URL缺失率（>10% 不达标）、职称缺失率、姓名异常率
  3. 占比分析: 院系教师占比、职称分布、URL覆盖率分布
  4. 导航词检测: 误抓的导航词汇
  5. 输出报告: 控制台 + EVALUATION.json + EVALUATION.md

用法:
  uv run python evaluate.py --school 安徽大学
  uv run python evaluate.py --school 安徽大学 --fix
  uv run python evaluate.py --all
"""

import argparse, csv, json, re, sys, time
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # script/ 的上层 = department teachers 根
OUTPUT_DIR = BASE / "output"
LOGS_DIR = BASE / "logs"          # 顶层汇总文件(2026-08-28 从 output/ 迁出)
SKIP_SCHOOLS = {"中央美术学院","中央戏剧学院","中央音乐学院","中国美术学院","中国音乐学院","上海音乐学院","北京协和医学院","外交学院"}

# 常见导航词（被误抓为教师名的）
NAV_WORDS = {
    "首页","主页","学校","学院","概况","简介","领导","机构","设置",
    "师资","教师","导师","博士后","人才","本科生","研究生","招生","就业","新闻",
    "通知","公告","English","中文","管理","服务","下载","更多","详情","查看",
    "搜索","登录","注册","返回","上一页","下一页","尾页","跳转","科研","党建",
    "国际","交流","校友","建设","培养","教学","学术","课程","专业","图书馆",
    "联系","电话","邮箱","地址","邮编","部门","党委","行政","工会","团委",
    "合作","创新","实践","学科","实验室","中心","平台","基地","项目","基金",
    "出版","期刊","会议","文化","艺术","体育","新闻网","媒体","聚焦","要闻",
    "综合","时讯","前沿","视听","系所","国家级","省部级","地区","扫描","移动",
    "客户端","微信","微博","附件","表格","申请","备案","年度","报告","考核",
    "述职","测评","公示","经费","预算","决算","采购","招标","中标","合同",
    "审计","监督","督查","培训","进修","留学","交换","访问","讲座","论坛",
    "研讨会","沙龙","师德师风","导师风采","师资力量","师资队伍","师资介绍",
    "队伍建设","教师队伍","教师名录","团队概况","知名专家","人才引进",
    "科学研究","人才培养","招生就业","合作交流","学院概况","学院简介","领导简介",
    "机构设置","院长致辞","访问旧网址","联系我们","领导信箱","监督电话",
    "学院首页","学部概况","学部简介","现任领导",
    "职业发展",
    "学工动态",
    "成果奖励",
    "平台基地",
    "重点实验室",
    "招聘信息",
    "会议论坛",
    "学术讲座",
    "留学交换",
    "培训进修",
    "就业信息",
    "招生信息",
    "研究生教育",
    "本科教育",
    "服务动态",
    "团队建设",
    "服务平台",
    "服务社会",
    "学生交流",
    "教师交流",
    "国际交流",
    "制度建设",
    "纪律建设",
    "作风建设",
    "支部活动",
    "品牌支部",
    "两委委员",
    "组织建设",
    "思想建设",
    "政治建设",
    "党建思政",
    "纵向科研",
    "横向科研",
    "科研平台",
    "科研活动",
    "校友动态",
    "研的风采",
    "导的风采",
    "管的风采",
    "育的风采",
    "学的风采",
    "教的风采",
    "博士后培养",
    "研究生培养",
    "本科生培养",
    "基地平台",
    "学术报告",
    "科研成果",
    "科研动态",
    "校友之家",
    "制度流程",
    "党团学工",
    "学术交流",
    "学院总览","组织机构","学部动态",
    "通知公告","学科建设","学位点","重点学科","团队概况","教师风采","教学团队",
    "教研人员","在职教师","专任教师","人才队伍","教师信息","教授名录",
    "上一篇","下一篇","第一页","尾页","当前位置",
}

# 职称关键词
TITLE_KEYWORDS = [
    "教授","副教授","讲师","助教","研究员","副研究员","院士","博导","硕导",
    "工程师","高级工程师","主任","副主任","院长","副院长","系主任","书记",
    "博士后","博士","硕士","实验师","助理研究员","高级实验师","讲师团",
]


def read_teachers_csv(csv_path: Path) -> dict:
    """读取 teachers.csv, 返回统计结果"""
    result = {
        "exists": False,
        "total": 0,
        "url_ok": 0,
        "url_missing": 0,
        "title_ok": 0,
        "valid_names": 0,
        "nav_words": [],
        "duplicate_names": 0,
        "title_dist": Counter(),
        "category_dist": Counter(),
        "names": [],
    }

    if not csv_path.exists() or csv_path.stat().st_size < 50:
        return result

    result["exists"] = True
    rows = list(csv.reader(open(csv_path, encoding="utf-8")))
    if len(rows) < 2:
        return result

    header = rows[0]
    name_idx = url_idx = title_idx = 2
    cat_idx = None
    for i, h in enumerate(header):
        if '姓名' in h or 'name' in h.lower(): name_idx = i
        # 精确匹配"个人详情URL", 不是"师资页URL"(后者含URL但非详情)
        if h in ('个人详情URL', '详情URL') or ('详情' in h and 'URL' in h): url_idx = i
        if '职称' in h or '职务' in h or 'title' in h.lower(): title_idx = i
        if '类别' in h or 'category' in h.lower(): cat_idx = i

    seen_names = set()
    for r in rows[1:]:
        if len(r) <= max(name_idx, url_idx): continue
        name = r[name_idx].strip() if name_idx < len(r) else ""
        url = r[url_idx].strip() if url_idx < len(r) else ""
        title = r[title_idx].strip() if title_idx < len(r) else ""

        if not name: continue
        result["total"] += 1
        result["names"].append(name)

        # URL统计: 有URL计入url_ok, 无URL计入no_url_teachers(合法,如纯文本页)
        if url.startswith("http"):
            result["url_ok"] += 1
        else:
            result["no_url_teachers"] = result.get("no_url_teachers", 0) + 1
        if title: result["title_ok"] += 1
        if re.match(r'^[一-鿿]{2,4}$', name): result["valid_names"] += 1
        if name in NAV_WORDS: result["nav_words"].append(name)
        if name in seen_names: result["duplicate_names"] += 1
        seen_names.add(name)

        if title:
            for kw in TITLE_KEYWORDS:
                if kw in title:
                    result["title_dist"][kw] += 1
                    break

        # 教师类别
        if cat_idx is not None and cat_idx < len(r):
            cat = r[cat_idx].strip()
            if cat:
                result["category_dist"][cat] += 1

    return result


def evaluate_school(school_name: str, fix: bool = False) -> dict | None:
    """评估单个学校"""
    school_dir = OUTPUT_DIR / school_name
    if not school_dir.exists():
        print(f"❌ 学校不存在: {school_name}")
        return None

    depts_csv = school_dir / "departments.csv"
    total_depts = 0
    if depts_csv.exists():
        total_depts = sum(1 for _ in csv.DictReader(open(depts_csv)))

    dept_results = []
    grand_total = 0
    grand_url_ok = 0
    grand_title_ok = 0
    grand_valid = 0
    grand_title_dist = Counter()
    nav_word_depts = []
    url_issue_depts = []
    empty_depts = []

    for d in sorted(school_dir.iterdir()):
        if not d.is_dir(): continue
        stat = read_teachers_csv(d / "teachers.csv")
        dept_results.append({"dept": d.name, **stat})

        grand_total += stat["total"]
        grand_url_ok += stat["url_ok"]
        grand_title_ok += stat["title_ok"]
        grand_valid += stat["valid_names"]
        grand_title_dist.update(stat["title_dist"])

        if stat["nav_words"]:
            nav_word_depts.append((d.name, stat["nav_words"]))
        # URL缺失判定: URL缺失率>30%的院系标记(允许少量无URL教师,如纯文本页)
        # 阈值: 有URL教师数 < 总人数*70% → 打回重新采集
        if stat["total"] >= 3 and stat["url_ok"] < stat["total"] * 0.7:
            url_issue_depts.append((d.name, stat["url_ok"], stat["total"]))
        if stat["total"] == 0:
            empty_depts.append(d.name)

    # ══ 控制台报告 ══
    print(f"\n{'='*80}")
    print(f"  {school_name} — 教师采集数据质量评估")
    print(f"{'='*80}")
    print(f"  院系数: {total_depts} (有数据 {sum(1 for r in dept_results if r['total']>0)})")
    print(f"  教师总数: {grand_total}")
    if grand_total:
        print(f"  详情URL覆盖率: {grand_url_ok}/{grand_total} ({grand_url_ok/grand_total*100:.1f}%) {'✅' if grand_url_ok/grand_total >= 0.9 else '🔴 不达标!'}")
        print(f"  职称覆盖率: {grand_title_ok}/{grand_total} ({grand_title_ok/grand_total*100:.1f}%)")
        print(f"  有效姓名率: {grand_valid}/{grand_total} ({grand_valid/grand_total*100:.1f}%)")

    # 职称分布
    if grand_title_dist:
        print(f"\n  📊 职称分布:")
        for title, count in grand_title_dist.most_common(10):
            bar = '█' * int(count / grand_total * 50)
            print(f"    {title:10s} {count:4d} ({count/grand_total*100:4.1f}%) {bar}")

    # 教师类别分布
    grand_cat = Counter()
    for r in dept_results:
        grand_cat.update(r.get("category_dist", {}))
    if grand_cat:
        print(f"\n  🏷️ 教师类别分布:")
        for cat, count in grand_cat.most_common():
            print(f"    {cat:8s} {count:4d} ({count/grand_total*100:4.1f}%)")

    # 院系教师占比
    if grand_total:
        print(f"\n  📊 院系教师占比 (Top 10):")
        sorted_depts = sorted([r for r in dept_results if r['total'] > 0], key=lambda x: -x['total'])
        for r in sorted_depts[:10]:
            bar = '█' * int(r['total'] / grand_total * 50)
            print(f"    {r['dept'][:30]:30s} {r['total']:4d} ({r['total']/grand_total*100:4.1f}%) {bar}")

    # URL 缺失
    if url_issue_depts:
        print(f"\n  🔴 URL缺失院系 ({len(url_issue_depts)}) — 缺失率>10%:")
        for name, ok, total in url_issue_depts:
            print(f"    {name:35s} {ok}/{total} ({ok/total*100:.0f}%)")
    else:
        print(f"\n  ✅ 无 URL 缺失院系")

    # 导航词
    if nav_word_depts:
        print(f"\n  ⚠️ 导航词混入 ({len(nav_word_depts)} 院系):")
        for name, words in nav_word_depts[:5]:
            unique = sorted(set(words))
            print(f"    {name[:35]:35s} {', '.join(unique[:6])}")

    # 空院系
    if empty_depts:
        print(f"\n  ❌ 无数据院系 ({len(empty_depts)}):")
        for n in empty_depts: print(f"    - {n}")

    # 明细表
    print(f"\n  {'院系':38s} {'人数':>4s} {'URL':>6s} {'职称':>5s} {'有效名':>5s} {'重复':>4s}")
    print(f"  {'-'*68}")
    for r in dept_results:
        if not r["exists"]:
            print(f"  ❌ {r['dept'][:36]:36s} {'NO_CSV':>10s}")
        elif r["total"] == 0:
            print(f"  ❌ {r['dept'][:36]:36s} {'空':>4s}")
        else:
            url_pct = r['url_ok']/r['total']*100
            icon = '✅' if url_pct >= 90 else '🔴'
            print(f"  {icon} {r['dept'][:36]:36s} {r['total']:4d} {url_pct:5.0f}% {r['title_ok']:4d} {r['valid_names']:4d} {r['duplicate_names']:3d}")

    # ══ 写报告 ══
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = {
        "school": school_name,
        "evaluated_at": timestamp,
        "total_depts": total_depts,
        "total_teachers": grand_total,
        "url_coverage": grand_url_ok / grand_total if grand_total else 0,
        "title_coverage": grand_title_ok / grand_total if grand_total else 0,
        "valid_name_rate": grand_valid / grand_total if grand_total else 0,
        "title_distribution": dict(grand_title_dist.most_common()),
        "url_issue_depts": [{"dept": n, "url_ok": o, "total": t} for n, o, t in url_issue_depts],
        "nav_word_depts": [{"dept": n, "words": sorted(set(w))} for n, w in nav_word_depts],
        "empty_depts": empty_depts,
        "dept_details": dept_results,
    }
    (school_dir / "EVALUATION.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # EVALUATION.md
    md_lines = [
        f"# {school_name} — 教师数据质量评估报告",
        f"",
        f"- **评估时间**: {timestamp}",
        f"- **院系数**: {total_depts}",
        f"- **教师总数**: {grand_total}",
    ]
    if grand_total:
        md_lines += [
            f"- **详情URL覆盖率**: {grand_url_ok/grand_total*100:.1f}% {'✅ 达标' if grand_url_ok/grand_total >= 0.9 else '🔴 不达标'}",
            f"- **职称覆盖率**: {grand_title_ok/grand_total*100:.1f}%",
            f"- **有效姓名率**: {grand_valid/grand_total*100:.1f}%",
        ]
    if grand_title_dist:
        md_lines += [f"", f"## 职称分布", f"", f"| 职称 | 人数 | 占比 |", f"|------|------|------|"]
        for title, count in grand_title_dist.most_common(10):
            md_lines.append(f"| {title} | {count} | {count/grand_total*100:.1f}% |")
    if url_issue_depts:
        md_lines += [f"", f"## 🔴 URL缺失院系", f"", f"| 院系 | URL覆盖率 |", f"|------|-----------|"]
        for n, o, t in url_issue_depts:
            md_lines.append(f"| {n} | {o}/{t} ({o/t*100:.0f}%) |")
    if empty_depts:
        md_lines += [f"", f"## ❌ 无数据院系", f""] + [f"- {n}" for n in empty_depts]
    md_lines += [f"", f"---", f"*由 evaluate.py 自动生成*"]
    (school_dir / "EVALUATION.md").write_text("\n".join(md_lines), encoding="utf-8")

    print(f"\n  📄 报告: {school_dir/'EVALUATION.json'}")
    print(f"  📄 报告: {school_dir/'EVALUATION.md'}")
    return report


def main():
    p = argparse.ArgumentParser(description="教师采集结果评估脚本")
    p.add_argument("--school", help="评估指定学校")
    p.add_argument("--all", action="store_true", help="评估全部学校")
    p.add_argument("--fix", action="store_true", help="标记URL缺失院系(供后续修复)")
    args = p.parse_args()

    if args.school:
        evaluate_school(args.school, fix=args.fix)
    elif args.all:
        schools = sorted(d.name for d in OUTPUT_DIR.iterdir()
                         if d.is_dir() and (d/"departments.csv").exists()
                         and d.name not in SKIP_SCHOOLS)
        summary = {"schools": [], "total_teachers": 0, "url_issue_count": 0, "empty_count": 0}
        for s in schools:
            r = evaluate_school(s, fix=args.fix)
            if r:
                summary["schools"].append({
                    "name": s,
                    "teachers": r["total_teachers"],
                    "url_coverage": r["url_coverage"],
                })
                summary["total_teachers"] += r["total_teachers"]
                summary["url_issue_count"] += len(r["url_issue_depts"])
                summary["empty_count"] += len(r["empty_depts"])
        print(f"\n{'='*80}")
        print(f"  全局汇总: {len(schools)}所学校, {summary['total_teachers']}名教师")
        print(f"  URL缺失院系: {summary['url_issue_count']}, 无数据院系: {summary['empty_count']}")
        json.dump(summary, open(LOGS_DIR/"EVALUATION_SUMMARY.json", "w"),
                  ensure_ascii=False, indent=2)
        print(f"  汇总报告: {LOGS_DIR/'EVALUATION_SUMMARY.json'}")
    else:
        p.print_help()
        print(f"\n示例:")
        print(f"  uv run python evaluate.py --school 安徽大学")
        print(f"  uv run python evaluate.py --all")


if __name__ == "__main__":
    main()
