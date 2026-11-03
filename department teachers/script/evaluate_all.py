#!/usr/bin/env python3
"""
全量体检脚本 — 统一评估所有学校的教师采集质量

兼容 7/8/9 列三种 CSV 格式，产出:
  1. logs/HEALTH_REPORT.md     — 全校分级体检报告
  2. logs/HEALTH_REPORT.json   — 机器可读
  3. logs/FIX_QUEUE.json       — 修复队列(按优先级排序的坏院系清单)

问题分级:
  A级 严重:  详情URL缺失率 > 90%(旧版 batch_collect 抓不出URL的院系)
  B级 中等:  URL 60%~90% / 导航词混入 / 假详情URL / 重复名 > 30%
  C级 轻微:  URL 90%+ 但含少量导航词或假URL
  合法空:     医院/书院/继续教育学院(无独立教师名录, 符合预期)
  失败:       有CSV但0行 / 无CSV / test_report.json passed=false(真学院)

用法:
  uv run python evaluate_all.py
"""

import csv, json, re
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # script/ 的上层 = department teachers 根
OUTPUT_DIR = BASE / "output"      # 学校数据目录
LOGS_DIR = BASE / "logs"          # 顶层体检/队列文件(2026-08-28 从 output/ 迁出)

# 无需独立教师名录的组织类型(采集器有意标记为空)
NO_TEACHER_ORGS = {"书院", "医院", "继续教育"}

# 导航词大词典(合并 evaluate.py + fix_pku_batch.py 的集合)
NAV_WORDS = {
    "首页","主页","学校","学院","概况","简介","领导","机构","设置","师资","教师",
    "导师","博士后","人才","招生","就业","新闻","通知","公告","管理","服务","下载",
    "更多","详情","查看","搜索","登录","注册","返回","科研","党建","交流","校友",
    "培养","教学","学术","课程","专业","图书馆","联系","电话","邮箱","地址","邮编",
    "部门","党委","行政","工会","团委","合作","创新","实践","学科","实验室","中心",
    "平台","基地","项目","基金","出版","期刊","会议","文化","艺术","体育",
    "学院概况","学院简介","学院介绍","学院领导","院长寄语","院长致辞","组织机构","机构设置",
    "历史沿革","系图书馆","报考学生","系内通知","本系概况","历史人物","新闻动态",
    "在职教师","博士生导","硕士生导","离退休","永远怀念","研究中心","师资队伍",
    "科学研究","历任领导","院委员会","工学院办","本科教育","研究生教","硕士研究",
    "博士研究","博士后流","学生工作","招生信息","就业信息","国际交流","合作交流",
    "校友工作","党建工作","思政工作","教务管理","教学管理","科研管理","人事管理",
    "办事指南","服务指南","下载中心","资料下载","规章制度","政策法规","办事流程",
    "学院动态","系所动态","学术动态","科研动态","人才招聘","诚聘英才","访问学者",
    "客座教授","名誉教授","特聘教授","兼职教授","讲座教授","退休教师","荣休教师",
    "离退休教","离休干部","在校学生","毕业校友","研究生招生","本科生招生","博士生招生",
    "硕士生招生","国际学生","学生培养","人才培养","教学改革","课程建设","教材建设",
    "教学成果","科研项目","科研成果","科研获奖","学术论文","学术专著","发明专利",
    "重点实验室","工程中心","创新平台","实验平台","学院首页","系所中心","党政工团",
    "组织体系","委员会","百廿动态","新闻资讯","百廿回眸","百廿简史","发展历程",
    "专业设置","广聚英才","招聘信息","现况简介","组织架构","人才队伍","服务指南",
    "招聘事务","报到入职","学院历史","行政后勤","领导致辞","理事会","学术荣誉",
    "管理服务","教学单位","本系介绍","系主任致","行政办公","地图路线","了解更多",
    "科研概述","科研人员","联合实验","科研新闻","安全工作","教工之家","教育捐赠",
    "教育基地","院长致词","更多新闻","更多通知","留学生招","更多学院","更多重要",
    "中国道路","学生园地","办公服务","办公电话","业务办理","北大概况","教育教学",
    "校园生活","杰出人才","创新团队","职能部门","English","中文","当前位置",
    "第一页","下一页","尾页","跳转","上一页","末页","上一篇","下一篇",
    "两院院士","人才计划","导师介绍","个人主页","双聘教授","教学通知","教师发展",
    "继续教育","相关链接","合作需求","党团工会","党务工作","院内信息","政策园地",
    "下载专区","校内资源","专职教师","行政教辅","学科介绍","学科方向","学术成果",
    "校企合作","招贤纳士","青年论坛","学工队伍","学生风采","全职教师","行政管理",
    "加入我们","学术科研","学者观点","课题研究","学术刊物","研究机构","院内门户",
    "旧版网站","北大首页","北大邮箱","教授登录","招生就业","校友之家","学科建设",
    "平台基地","职业发展","媒体","视听","时讯","前沿","系所","媒体聚焦","新闻网",
}

# 假详情URL特征
LIST_URL_PAT = re.compile(r'(list|index|default|channel|menu)\.(htm|html|shtml)', re.I)
NEWS_PATH_PAT = re.compile(r'/(xwgg|tzgg|tpxw|kyjz|xyxw|xwzx|xydt|news|tz|xygk|zzjg|about)/', re.I)

# 中文名(允许外国名字母形式)
NAME_OK_PAT = re.compile(r'^[一-鿿·⺀-鿿]{2,4}$|^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ\'\.\-\s]{2,40}$')


def is_detail_url(url):
    """判断URL是否像教师详情页(排除法)。
    个人目录型详情(ysllx/ly/index.htm, zzjs/ll/index.htm)合法"""
    if not url or not url.startswith("http"):
        return False
    if url.endswith("#") or re.search(r'#$', url):
        return False
    # 个人目录型: /slug/index.htm (slug为拼音缩写, 非频道名) → 合法详情
    if re.search(r"/index\.(htm|html|shtml)$", url):
        parts = url.rstrip("/").split("/")
        slug = parts[-2] if len(parts) >= 2 else ""
        if slug and not re.match(
                r"^(szdw|jsdw|sdw|kytd|xstd|xwzx|xygk|zzjg|rcpy|xsgz|djgz|znxy|bgfw|xxzq|"
                r"bnzf|bndt|index|page|qzjs|zzjs|kzjs|bsh|grtx|ysfc|txjs|xzjs|zsjs|jzjs|"
                r"zyysj|rxjs|qxjy|rtjs|jsmd|lyys)$", slug, re.I):
            return True
        return False
    if LIST_URL_PAT.search(url):
        return False
    if NEWS_PATH_PAT.search(url):
        return False
    return True


def read_csv(csv_path):
    """读取 teachers.csv, 自动识别列位置"""
    if not csv_path.exists() or csv_path.stat().st_size <= 50:
        return None
    try:
        rows = list(csv.reader(open(csv_path, encoding="utf-8")))
    except Exception:
        return None
    if len(rows) < 2:
        return rows, {}  # 有CSV但无数据行, idx置空
    hdr = rows[0]
    idx = {"name": None, "title": None, "dept": None, "detail": None, "fac": None, "cat": None}
    for i, h in enumerate(hdr):
        if idx["name"] is None and ("姓名" in h or h == "name"):
            idx["name"] = i
        if "职称" in h or "职务" in h:
            if idx["title"] is None or "职务职称" in h:
                idx["title"] = i
        if "所属" in h:
            idx["dept"] = i
        if "详情" in h and "URL" in h:
            idx["detail"] = i
        if "师资页" in h:
            idx["fac"] = i
        if "类别" in h:
            idx["cat"] = i
    if idx["name"] is None:
        return None
    return rows, idx


def analyze_dept(school, dept_dir):
    """分析单个院系, 返回统计 dict"""
    result = {
        "school": school.name, "dept": dept_dir.name,
        "has_csv": False, "total": 0, "url_ok": 0, "title_ok": 0,
        "nav": [], "fake_urls": 0, "anchor_urls": 0, "dup": 0,
        "no_teacher_org": False, "reasons": [], "pure_text_status": None,
    }
    is_no_org = any(kw in dept_dir.name for kw in NO_TEACHER_ORGS)
    result["no_teacher_org"] = is_no_org
    # test_report.json 里 status=no_teacher_org 的也视为合法空组织(如行政学部)
    try:
        tr = json.load(open(dept_dir / "test_report.json", encoding="utf-8"))
        if tr.get("status") == "no_teacher_org":
            result["no_teacher_org"] = True
        # 校园网限制/官网不可达/无师资板块 → 合法空(已人工分析留档)
        if tr.get("status") in ("unreachable_or_no_faculty", "no_faculty_page"):
            result["no_teacher_org"] = True
        # 无官网URL的系/实验室级单位 → 挂靠学院, 不采集
        if tr.get("status") in ("no_url", "pending_recollect"):
            result["no_teacher_org"] = True
        # 纯文本验证状态(verify_pure_text.py 写入)
        if tr.get("status") in ("verified_pure_text", "fake_pure_text"):
            result["pure_text_status"] = tr.get("status")
    except Exception:
        pass

    read = read_csv(dept_dir / "teachers.csv")
    if read is None:
        # 无CSV: 若是合法空组织, 不算失败
        return result
    rows, idx = read
    result["has_csv"] = True
    if not rows or not idx:
        return result

    seen = set()
    is_old_format = len(rows[0]) == 7  # 旧batch_collect产物(学校,院系,姓名,职称,详情URL,师资页URL,时间)
    for r in rows:
        ni = idx["name"]
        if len(r) <= ni:
            continue
        name = r[ni].strip()
        if not name:
            continue
        # 姓名净化: 去掉(兼)等后缀与职称词
        raw = name
        name_clean = re.sub(r'[（(].*?[）)]', "", name).strip()
        name_clean = re.sub(r'(长聘副教授|助理教授|副教授|教授|讲师|研究员|副研究员|助理研究员|博士生导师|硕士生导师|博士后|博士|硕士|院士)$', "", name_clean)
        name_clean = name_clean.strip()
        result["total"] += 1

        if raw in NAV_WORDS or name_clean in NAV_WORDS:
            result["nav"].append(raw)
        if name_clean in seen:
            result["dup"] += 1
        seen.add(name_clean)

        url = r[idx["detail"]].strip() if idx["detail"] is not None and idx["detail"] < len(r) else ""
        if url.startswith("http"):
            if is_detail_url(url):
                result["url_ok"] += 1
            else:
                result["fake_urls"] += 1
        if "#" in url:
            result["anchor_urls"] += 1

        ti = idx["title"]
        if ti is not None and ti < len(r) and r[ti].strip():
            result["title_ok"] += 1

    # 判定等级
    t = result["total"]
    if t == 0:
        return result
    url_rate = result["url_ok"] / t
    fake_rate = result["fake_urls"] / t
    # 纯文本师资页: URL率<10%且无假URL无导航
    # 新规则: 不直接豁免, 而是标 V(待验证) — 由 verify_pure_text.py 甄别真假
    # 已通过验证的(verified_pure_text)才豁免为OK; fake_pure_text 打回
    if t >= 3 and url_rate < 0.1:
        pts = result.get("pure_text_status")
        if pts == "verified_pure_text":
            result["grade"] = "OK"
            result["reasons"].append("纯文本师资页(已验证, 无详情链接)")
        elif pts == "fake_pure_text":
            result["grade"] = "A"
            result["reasons"].append("假纯文本: 页面有教师详情链接但未提取, 需确定性重采")
        elif result["fake_urls"] == 0 and not result["nav"] and not is_old_format:
            result["grade"] = "V"
            result["reasons"].append("纯文本页待验证: 需运行 verify_pure_text.py 甄别")
        else:
            result["grade"] = "A"
            result["reasons"].append(f"详情URL缺失 {t - result['url_ok']}/{t} ({url_rate:.0%})")
    # 假URL占比>20% → 严重问题
    elif fake_rate > 0.2:
        result["grade"] = "B"
        result["reasons"].append(f"假详情URL {result['fake_urls']}/{t}")
    # URL率 10%~60%: 混合页面(部分纯文本无详情, 合法) vs 漏抓
    elif url_rate < 0.6:
        if result["fake_urls"] == 0:
            result["grade"] = "C"
            result["reasons"].append(f"混合页面: 部分教师无详情页(纯文本/表格), 合法")
        else:
            result["grade"] = "B"
            result["reasons"].append(f"URL覆盖率低 {url_rate:.0%}")
    else:
        result["grade"] = "C" if (result["nav"] or result["fake_urls"]) else "OK"
    if result["nav"]:
        result["reasons"].append(f"导航词混入 {len(set(result['nav']))} 个")
    if result["dup"] / t > 0.3:
        result["reasons"].append(f"重复名 {result['dup']}/{t}")
    if result["nav"]:
        result["nav"] = sorted(set(result["nav"]))[:8]
    return result


def main():
    schools = sorted(s for s in OUTPUT_DIR.iterdir()
                     if s.is_dir() and (s / "departments.csv").exists())
    school_results = {}
    fix_queue = []
    verify_queue = []
    summary = Counter()
    grade_depts = Counter()

    for school in schools:
        dept_list = []
        n_depts = sum(1 for _ in csv.DictReader(open(school / "departments.csv")))
        for d in sorted(school.iterdir()):
            if not d.is_dir():
                continue
            r = analyze_dept(school, d)
            dept_list.append(r)

            # 分级归类
            if r["total"] == 0:
                if r["no_teacher_org"]:
                    summary["legal_empty"] += 1
                elif not r["has_csv"]:
                    summary["no_csv"] += 1
                else:
                    summary["failed_empty"] += 1
                    grade_depts["失败"] += 1
                    summary["need_fix"] += 1
                    fix_queue.append({
                        "school": school.name, "dept": d.name,
                        "grade": "失败", "total": 0,
                        "url_ok": 0, "reasons": ["有CSV但0行, 真学院采集失败, 需重采"],
                    })
            else:
                grade_depts[r["grade"]] += 1
                summary["total_teachers"] += r["total"]
                summary["total_url_ok"] += r["url_ok"]
                if r["grade"] in ("A", "B"):
                    summary["need_fix"] += 1
                    fix_queue.append({
                        "school": school.name, "dept": d.name,
                        "grade": r["grade"], "total": r["total"],
                        "url_ok": r["url_ok"], "reasons": r["reasons"],
                    })
                if r["grade"] == "V":
                    # 纯文本待验证 → 进验证队列(不重采, 先甄别)
                    summary["need_verify"] = summary.get("need_verify", 0) + 1
                    verify_queue.append({
                        "school": school.name, "dept": d.name,
                        "total": r["total"], "reasons": r["reasons"],
                    })

        school_results[school.name] = {
            "total_depts": n_depts,
            "with_data": sum(1 for r in dept_list if r["total"] > 0),
            "teachers": sum(r["total"] for r in dept_list),
            "url_ok": sum(r["url_ok"] for r in dept_list),
            "A": sum(1 for r in dept_list if r.get("grade") == "A"),
            "B": sum(1 for r in dept_list if r.get("grade") == "B"),
            "C": sum(1 for r in dept_list if r.get("grade") == "C"),
            "failed_empty": sum(1 for r in dept_list if r["total"] == 0 and r["has_csv"] and not r["no_teacher_org"]),
            "no_csv": sum(1 for r in dept_list if r["total"] == 0 and not r["has_csv"]),
        }

    # 修复队列按等级+行数排序: 失败/A级优先, 行数多的先修
    order = {"失败": 0, "A": 0, "B": 1}
    fix_queue.sort(key=lambda x: (order.get(x["grade"], 2), -x["total"]))

    # ── 输出 JSON ──
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = {
        "checked_at": ts,
        "schools": len(schools),
        "total_teachers": summary["total_teachers"],
        "url_coverage": summary["total_url_ok"] / summary["total_teachers"] if summary["total_teachers"] else 0,
        "grade_dist": dict(grade_depts),
        "school_details": school_results,
        "fix_queue": fix_queue,
        "verify_queue": verify_queue,
    }
    (LOGS_DIR / "HEALTH_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 输出 FIX_QUEUE.json ──
    (LOGS_DIR / "FIX_QUEUE.json").write_text(
        json.dumps(fix_queue, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 输出 VERIFY_QUEUE.json (纯文本待验证) ──
    (LOGS_DIR / "VERIFY_QUEUE.json").write_text(
        json.dumps(verify_queue, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 输出 MD ──
    lines = [
        "# 全国高校教师采集 — 全量体检报告",
        "",
        f"- **体检时间**: {ts}",
        f"- **学校数**: {len(schools)}",
        f"- **教师总行数**: {summary['total_teachers']:,}",
        f"- **详情URL覆盖率**: {summary['total_url_ok']/summary['total_teachers']*100:.1f}%" if summary["total_teachers"] else "- **教师总行数**: 0",
        f"- **需修复院系**: {summary['need_fix']} (A级 {grade_depts.get('A',0)} + B级 {grade_depts.get('B',0)} + 失败空 {grade_depts.get('失败',0)})",
        f"- **纯文本待验证**: {summary.get('need_verify', 0)} 个院系 (运行 verify_pure_text.py 甄别)",
        "",
        "## 问题分级",
        "",
        "| 等级 | 说明 | 数量 |",
        "|------|------|------|",
        f"| 🔴 A | 详情URL缺失率>90% (旧版batch_collect抓不出URL) | {grade_depts.get('A',0)} |",
        f"| 🟠 B | URL覆盖率低/假URL>20%/导航词 | {grade_depts.get('B',0)} |",
        f"| 🟡 C | 有少量导航词或假URL混入 | {grade_depts.get('C',0)} |",
        f"| ✅ OK | 健康 | {grade_depts.get('OK',0)} |",
        f"| ⬛ 失败 | 有CSV但0行(真学院采集失败) | {grade_depts.get('失败',0)} |",
        "",
        "## 学校汇总",
        "",
        "| 学校 | 院系 | 有数据 | 教师行 | URL率 | A级 | B级 | 失败 | 无CSV |",
        "|------|-----:|-------:|-------:|------:|----:|----:|-----:|------:|",
    ]
    for s, r in sorted(school_results.items(), key=lambda x: -x[1]["A"]):
        url_rate = r["url_ok"] / r["teachers"] * 100 if r["teachers"] else 0
        lines.append(
            f"| {s} | {r['total_depts']} | {r['with_data']} | {r['teachers']:,} | {url_rate:.0f}% | "
            f"{r['A']} | {r['B']} | {r['failed_empty']} | {r['no_csv']} |")

    lines += [
        "",
        f"## 修复队列 Top 60 (共 {len(fix_queue)})",
        "",
        "| # | 学校 | 院系 | 等级 | 行数 | URL | 原因 |",
        "|---|------|------|------|-----:|----:|------|",
    ]
    for i, q in enumerate(fix_queue[:60]):
        lines.append(
            f"| {i+1} | {q['school']} | {q['dept'][:24]} | {q['grade']} | {q['total']} | "
            f"{q['url_ok']}/{q['total']} | {'; '.join(q['reasons'])[:50]} |")
    (LOGS_DIR / "HEALTH_REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    # ── 控制台摘要 ──
    print(f"体检完成: {len(schools)} 校, {summary['total_teachers']:,} 行教师数据")
    print(f"URL覆盖率: {summary['total_url_ok']/summary['total_teachers']*100:.1f}%")
    print(f"等级分布: {dict(grade_depts)}")
    print(f"需修复: {summary['need_fix']} 个院系 (A={grade_depts.get('A',0)}, B={grade_depts.get('B',0)}, 失败空={grade_depts.get('失败',0)})")
    print(f"纯文本待验证: {summary.get('need_verify', 0)} 个 (V级)")
    print(f"合法空组织: {summary['legal_empty']}, 失败空院系: {summary['failed_empty']}, 无CSV: {summary['no_csv']}")
    print(f"\n📄 logs/HEALTH_REPORT.md")
    print(f"📄 logs/HEALTH_REPORT.json")
    print(f"📄 logs/FIX_QUEUE.json")
    print(f"📄 logs/VERIFY_QUEUE.json")


if __name__ == "__main__":
    main()
