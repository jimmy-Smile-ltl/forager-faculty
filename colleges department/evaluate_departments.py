#!/usr/bin/env python3
"""
evaluate_departments.py — 单校 departments.csv/json 结果评估模块

对最终生成的 departments 结果做质量评估，检测三类问题:
  1. 数量异常 (综合性大学院系过少 → 疑似采漏)
  2. 内容噪声 (新闻/公告/导航/行政机构混入)
  3. 结构性异常 (同一 URL 重复、类型异常)

运行:
  uv run python "colleges department/evaluate_departments.py" --school "西南财经大学"
  uv run python "colleges department/evaluate_departments.py" --all

输出: 终端报告 + 每校 result_evaluation.json
"""

import argparse, csv, json, re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
INPUT_CSV = BASE_DIR / "985_211_双一流高校清单.csv"

# ── 常量 ────────────────────────────────────────
SPECIAL_TAGS = ["音乐", "戏剧", "美术", "体育", "语言", "警察", "公安", "民族", "外交"]

# 新闻/公告 URL 特征
NEWS_URL_PATTERNS = [
    r"/info/\d+/\d+",       # 典型 CMS 新闻: /info/1051/50662.htm
    r"/\d{4}/\d{4}/",       # /2024/0506/
    r"/\d{4}-\d{2}/\d+",    # /2024-05/123
    r"/content/\d+",
    r"/article/\d+",
    r"/detail/\d+",
]

# 新闻标题特征 (日期+长标题)
NEWS_TITLE_RES = [
    r"\d{4}[.\-/年]\d{1,2}[.\-/月]\d{1,2}",  # 2026.08.06 / 2026-08-06
    r"荣获|获奖|大赛|举办|举行|召开|调研|检查|成功发射|成立仪式",
]

# 行政/服务关键词 (从 30+ 学校实战积累)
ADMIN_KW = ["管理处", "办公室", "委员会", "党组", "纪委", "机关党委", "工会", "团委",
            "财务处", "审计处", "人事处", "保卫处", "后勤", "离退休", "老干部处",
            "学报编辑部", "校报编辑部",
            "本科生院", "研究生院", "发展规划处", "国际交流与合作处",
            "党委组织部", "党委宣传部", "党委统战部", "党委教师工作部", "党委学生工作部",
            "教师教学发展中心", "分析测试中心", "期刊中心", "就业指导", "学生工作部"]

NAV_KW = ["首页", "学校概况", "组织机构", "现任领导", "联系我们", "网站地图",
          "信息公开", "邮箱", "招聘", "捐赠", "媒体", "专题报道", "微信", "易班",
          "院系设置", "院系部门", "学部学院", "学院（系）", "教学机构", "科研机构",
          "标识系统", "IT服务", "文明校园", "学生工作网站", "干部培训", "就业创业",
          "科研信息网", "人事管理", "国际交流", "国内合作", "校友会", "教育基金会",
          "校园服务", "通勤车班次", "校历", "办公电话", "心理健康", "医疗服务",
          "运动与健康", "教师个人主页", "人才工作", "人事工作", "人才招聘",
          "大型设备仪器平台", "一流学科建设", "重点科技创新平台", "服务基础教育",
          "乡村振兴与对口支援", "成果转化", "合作办学", "专家学者走基层",
          "大学志愿者服务", "教育出版", "档案馆", "博物馆", "校史馆", "图书馆",
          "官方微信", "官方微博", "学习强国", "博士后流动站", "学报"]

# 硬排除模式 (与 spider 模板 HARD_DROP 一致 — 导航/分类标题/行政中心/医院内部页)
HARD_DROP_SUBSTR = [
    "院系设置", "院系导航", "院系部门", "学院部门", "院系机构", "院系中心", "学部学院", "学院（系）",
    "学院学部", "教学机构", "科研机构", "教学科研机构", "机构设置", "组织机构",
    "机构导航", "机构部门", "校内科研基地",
    "信息与网络中心", "网络信息中心", "网络与信息中心", "信息化办公室", "信息化建设", "信息化管理",
    "信息化教学中心", "教学运行", "考务", "智慧校园", "服务中心", "心理健康教育与咨询", "智算", "超算",
    "现代教育技术中心", "电化教育", "融媒体中心",
    "医院概况", "医院特色", "医院介绍", "医院文化", "就医指南", "预约挂号",
    "专家介绍", "名医传承", "品牌护理", "院内制剂", "科室介绍", "专科介绍",
    "医疗团队", "护理团队", "重点学科详情", "特色诊疗详情", "专家介绍详情",
    "名医传承工作室详情", "品牌护理详情", "院内制剂详情",
    "校医院", "门诊部", "医务室", "卫生所", "医疗服务中心", "党委", "监委", "党工委",
    "社保",
    "管理系统",
    "国家自然科学奖",
]
HARD_DROP_EXACT = {
    "院系设置", "院系导航", "院系部门", "院系机构", "学部学院", "学院（系）", "学院学部",
    "教学机构", "科研机构", "教学科研机构", "机构设置", "组织机构", "机构导航", "机构部门",
    "校内科研基地", "教育部野外科学观测研究站", "国家野外科学观测研究站", "野外科学观测研究站",
    "医院", "校医院", "门诊部", "数据中心", "信息中心", "计算中心", "心理咨询中心",
    "更多", "查看更多", "更多详情",
    "自然科学", "人文科学", "自然科学科研基地", "自然科学研究机构",
    "自然科学成果", "自然科学项目", "自然科学类", "自然科学科研信息网",
    "研究所（中心）", "重点实验室",
    "人文社会科学科研基地", "国家工程技术研究中心", "前沿科学中心", "教育部工程研究中心",
    "农业农村部重点实验室", "教育部重点实验室", "山东省重点实验室", "山东省技术创新中心", "山东省工程研究中心",
    "山东省工程技术研究中心", "科技部国际科技合作基地", "教育部国际合作联合实验室",
    "山东省国际科技合作示范基地", "高等学校学科创新引智基地", "青岛市重点实验室",
    "青岛市技术创新中心", "青岛市工程研究中心", "青岛工程技术研究中心",
    "校地共建科研基地", "校际共建科研基地", "校企共建科研基地",
    "基地", "生态环境部重点实验室", "国家矿山安全监察局重点实验室", "国家（地方联合）工程实验室",
}
CLINICAL_KW = ["内科", "外科", "妇产", "妇儿", "儿科", "妇科", "产科", "骨科", "眼科",
    "耳鼻喉", "口腔科", "麻醉", "病理", "检验", "放射", "影像", "药剂", "药学",
    "护理", "急诊", "重症", "急重症", "五官", "医技", "肿瘤", "神经", "心血管",
    "呼吸", "消化", "泌尿", "内分泌", "血液", "风湿免疫", "中医科", "针灸",
    "推拿", "康复", "皮肤", "整形", "烧伤", "急救", "预防保健", "体检", "营养", "感染"]

def _hard_noise(name: str) -> bool:
    if any(k in name for k in HARD_DROP_SUBSTR):
        return True
    if name in HARD_DROP_EXACT:
        return True
    if re.fullmatch(r'[一-鿿]{2,5}(省|市|自治区)(国际联合实验室|工程实验室|高校重点实验室|重点实验室|工程研究中心|技术创新中心|工程技术研究中心|国际科技合作示范基地)', name):
        return True
    if name.endswith(("详情", "概况", "特色")):
        return True
    if name.endswith(("部", "科")) and any(k in name for k in CLINICAL_KW):
        return True
    return False

def _first_label(url: str) -> str:
    return urlparse(url).netloc.split(".")[0] if url and urlparse(url).netloc else ""

def load_schools() -> dict:
    schools = {}
    with open(INPUT_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            schools[r["学校名称"]] = r
    return schools

def evaluate_school(name: str) -> dict:
    """评估一所学校的 departments 结果，返回 {score, ok, issues, stats}"""
    dept_file = OUTPUT_DIR / name / "departments.json"
    if not dept_file.exists():
        return {"school": name, "score": 0, "ok": False,
                "issues": ["no departments.json"], "stats": {}}

    import json as _json
    data = _json.loads(dept_file.read_text())
    depts = data.get("departments", [])

    schools = load_schools()
    info = schools.get(name, {})
    is_985 = str(info.get("is_985", "")).strip() == "1"
    is_special = any(t in name for t in SPECIAL_TAGS)

    n = len(depts)
    issues = []
    deductions = 0
    stats = {}

    # ── 提取精度提示 (不扣分): 读 _filter_log.json 的过滤率 — >40% 说明提取过宽, 过滤在兜底 ──
    fl_file = OUTPUT_DIR / name / "_filter_log.json"
    if fl_file.exists():
        try:
            fl = _json.loads(fl_file.read_text())
            st = fl.get("stats", {})
            fr = st.get("filter_rate", 0)
            if fr > 0.4:
                issues.append(f"提取过宽: {st.get('extracted', '?')}→{st.get('llm_kept', '?')} (过滤率{fr:.0%}, 建议回环节1收窄 css_selectors)")
            elif fr > 0.15:
                issues.append(f"提取偏宽: 过滤率{fr:.0%}, 可考虑收窄选择器")
        except Exception:
            pass

    # ── 1. 数量检查 (40分) ──
    if n == 0:
        return {"school": name, "score": 0, "ok": False,
                "issues": ["院系为空"], "stats": {"n": 0}}
    # 行业特色院校(海洋/农业/矿业/地质/林业等)院系天然偏少, 阈值放宽
    industry_tags = ["海洋", "农业", "矿业", "地质", "林业", "石油", "电力", "化工", "中医药", "医科"]
    is_industry = any(t in name for t in industry_tags)
    if not is_special:
        if is_industry:
            # 行业院校: ≥10 正常, <8 才怀疑遗漏
            if n < 8:
                deductions += 30
                issues.append(f"院系过少: {n}个 (行业院校应≥10，疑似采漏)")
        elif n < 10:
            deductions += 40
            issues.append(f"院系过少: {n}个 (综合类应≥15，疑似采漏)")
        elif n < 15:
            deductions += 25
            issues.append(f"院系偏少: {n}个 (疑似部分遗漏)")
        elif n < 20:
            deductions += 10
            issues.append(f"院系略少: {n}个")
    elif n < 5:
        deductions += 30
        issues.append(f"特殊院校院系过少: {n}个")
    stats["n"] = n

    # ── 2. 噪声检查 (40分) ──
    noise_count = 0
    noise_samples = []
    for d in depts:
        name_ = d.get("name", "")
        url_ = d.get("url", "")
        type_ = d.get("type", "")
        is_noise = False
        reason = ""

        # 新闻 URL — 但 /info/NNNN/NNNN.htm 若名称是具体学院名(公安大学11学院用介绍页)不算新闻
        if any(re.search(p, url_) for p in NEWS_URL_PATTERNS):
            # /info/1151/6148.htm + 名称含学院/系 → 学院介绍页, 不是新闻
            # (收紧: 名称≤40字、不含"我校"、非日期前缀 — 防"我校XX学院团队…"新闻标题误放行)
            if (re.search(r'/info/\d+/\d+', url_) and len(name_) <= 40
                    and "我校" not in name_ and not re.match(r'^\d{1,2}\d{4}-\d{2}', name_)
                    and any(k in name_ for k in ["学院", "系", "学部", "研究院", "研究所", "中心", "实验室", "书院"])):
                pass  # 学院介绍页, 保留
            # /2021/0506/c11436a151192/page.htm + 名称含机构关键词 → 科研机构介绍页, 不是新闻
            elif re.search(r'/\d{4}/\d{4}/c\d+a\d+/page', url_) and any(k in name_ for k in ["实验室", "研究中心", "研究所", "研究院", "工程中心", "基地"]):
                pass  # 科研机构介绍页(北化大模式), 保留
            else:
                is_noise, reason = True, f"新闻URL: {url_[:50]}"
        # 新闻标题 — 日期前缀 "282026-05我校中药学院团队…" (2026-05-28 新闻)
        elif re.match(r'^\d{1,2}\d{4}-\d{2}', name_) or ("我校" in name_ and len(name_) > 20):
            is_noise, reason = True, f"新闻标题: {name_[:40]}"
        # 分类导航标题: URL 指向分类页且名称是精确的泛化分类词
        elif (re.search(r'/list\.(?:htm|psp|jsp|shtml)', url_) or 'sys_.htm' in url_ or 'xlss.htm' in url_
              or 'sjjxjd' in url_ or 'stkyjg' in url_ or 'fstkyjg' in url_ or 'kyjg1' in url_ or 'jxtj1' in url_):
            # 精确泛化分类词 — 只有这些算分类标题, 具体实验室名(有子域/具体名)不算
            generic_exact = {"国家级重点实验室", "省、部级重点实验室", "国家级工程技术研究中心",
                             "省、部级工程技术研究中心（所）", "国家工程实验室",
                             "独立类实体科研机构", "挂靠类实体科研机构", "学校自建非实体科研机构",
                             "与企业合建非实体科研机构", "实践教学基地", "训练设施", "警务装备应用培训"}
            if name_ in generic_exact:
                is_noise, reason = True, f"分类导航标题: {name_[:40]}"
            elif name_ == "实验室" or name_ == "中心" or name_ == "研究院":
                is_noise, reason = True, f"分类导航标题: {name_[:40]}"
        # 硬排除项 (导航/分类标题/行政中心/医院内部页 — 与 spider HARD_DROP 一致)
        elif _hard_noise(name_):
            is_noise, reason = True, f"硬排除项: {name_[:40]}"
        # 行政机构 (排除有独立子域的教学科研机构)
        elif any(k in name_ for k in ADMIN_KW) and type_ not in ("医院", "实验室", "基地", "研究院", "研究所"):
            is_noise, reason = True, f"行政机构: {name_[:40]}"
        # 导航项 (名称完全等于导航词, 或含导航词且类型为教学类)
        elif (name_ in NAV_KW or any(k in name_ for k in NAV_KW)) and type_ in ("学院", "部", "教学部", "学部", "系"):
            is_noise, reason = True, f"导航项: {name_[:30]}"
        # 异常类型
        elif type_ not in ("学院", "系", "研究院", "研究所", "学部", "书院", "中心",
                           "实验室", "基地", "医院", "教学部", "部", "研究中心",
                           "继续教育学院", "国际教育学院", "研究机构", "研究基地", "研究平台",
                           "孔子学院"):
            is_noise, reason = True, f"异常类型[{type_}]: {name_[:30]}"

        if is_noise:
            noise_count += 1
            if len(noise_samples) < 5:
                noise_samples.append(reason)

    noise_ratio = noise_count / n if n else 0
    if noise_ratio > 0.3:
        deductions += 40
        issues.append(f"噪声过多: {noise_count}/{n} ({noise_ratio:.0%}) 疑似采集了新闻/行政/导航")
    elif noise_ratio > 0.15:
        deductions += 25
        issues.append(f"噪声较多: {noise_count}/{n} ({noise_ratio:.0%})")
    elif noise_ratio > 0.05:
        deductions += 10
        issues.append(f"少量噪声: {noise_count}/{n}")
    stats["noise"] = noise_count
    stats["noise_samples"] = noise_samples

    # ── 3. 结构检查 (20分) ──
    # URL 重复 (空 URL 不算重复 — 无官网的院系是合法的)
    # 同 URL 不同名 = 一子站挂多机构的真并列(中科大math站4系/北科大ai站10中心/协和pumch.cn 7医院),
    # 是合法并列不算重复; 仅同 URL 且同名 才判真重复(同一机构重复抓取)
    urls = [_norm_url(d.get("url", "")) for d in depts if d.get("url", "").strip()]
    dup = [u for u, c in Counter(urls).items() if c > 1]
    if dup:
        real_dup = {}
        for d_ in depts:
            u_ = _norm_url(d_.get("url", ""))
            if u_ in dup:
                real_dup.setdefault(u_, []).append(d_.get("name", ""))
        real_dup = {u: nm for u, nm in real_dup.items() if len(set(nm)) < len(nm)}
        if real_dup:
            deductions += 10
            issues.append(f"URL重复: {len(real_dup)}个 ({list(real_dup)[0][:50]})")
    stats["dup_urls"] = len(dup)

    # 名称重复
    names = [d.get("name", "") for d in depts]
    dup_names = [nm for nm, c in Counter(names).items() if c > 1 and nm]
    if dup_names:
        deductions += 5
        issues.append(f"名称重复: {len(dup_names)}个")
    stats["dup_names"] = len(dup_names)

    # 子域多样性（非特殊院校检查）
    if not is_special and n >= 10:
        labels = set(_first_label(d.get("url", "")) for d in depts)
        if len(labels) <= 2:
            deductions += 5
            issues.append(f"子域多样性低: {sorted(labels)}")
        stats["labels"] = sorted(labels)

    # ── 4. URL 覆盖检查 (参考标准: 每校至少有 20 个有 URL 的二级组织) ──
    # 院系总数 ≥20 的学校要求有 URL ≥20; 院系总数 <20 的特殊/行业校(外交/音乐/体育等
    # 本身机构就少)放宽为 有URL ≥ 70%×n 且至少 10 个 — 避免按绝对数误杀小规模院校
    has_url = sum(1 for d in depts if d.get("url", "").strip())
    stats["has_url"] = has_url
    if n >= 20:
        if has_url < 20:
            deductions += 10
            issues.append(f"URL覆盖不足: 有URL {has_url}/{n} (参考标准≥20, 建议补全官网链接)")
    elif n >= 10:
        need = max(10, int(n * 0.7))
        if has_url < need:
            deductions += 5
            issues.append(f"URL覆盖不足: 有URL {has_url}/{n} (特殊/行业校参考标准≥{need})")

    score = max(0, 100 - deductions)
    ok = score >= 60
    return {"school": name, "score": score, "ok": ok,
            "issues": issues, "stats": stats,
            "n": n, "is_985": is_985, "is_special": is_special}

def _norm_url(url: str) -> str:
    # 保留 query string (央美等站点用 ?j=101 区分院系)
    u = url.rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", type=str, help="单校评估")
    parser.add_argument("--all", action="store_true", help="全部评估")
    args = parser.parse_args()

    if args.school:
        r = evaluate_school(args.school)
        _print_report([r])
        # 写评估文件
        out = OUTPUT_DIR / args.school / "result_evaluation.json"
        import json as _json
        out.write_text(_json.dumps(r, ensure_ascii=False, indent=2), "utf-8")
        print(f"\n评估文件: {out}")
    elif args.all:
        results = []
        for d in sorted(OUTPUT_DIR.iterdir()):
            if d.is_dir() and (d / "departments.json").exists():
                results.append(evaluate_school(d.name))
        _print_report(results)
        import json as _json
        summary = {
            "total": len(results),
            "ok": sum(1 for r in results if r["ok"]),
            "avg_score": sum(r["score"] for r in results) / len(results) if results else 0,
            "results": results,
        }
        (BASE_DIR / "evaluation_summary.json").write_text(
            _json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(f"\n汇总文件: {BASE_DIR / 'evaluation_summary.json'}")
    else:
        parser.print_help()

def _print_report(results: list):
    print("=" * 70)
    print("  院系结果评估")
    print("=" * 70)
    results.sort(key=lambda x: x["score"])
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        print(f"\n{mark} {r['school']}: {r['score']}分, {r.get('n', 0)}院系")
        for iss in r["issues"]:
            print(f"    - {iss}")
        if r["stats"].get("noise_samples"):
            for s in r["stats"]["noise_samples"][:3]:
                print(f"    噪声示例: {s}")

    # ── 修复建议 (重试机制指引) ──
    flagged = [r for r in results if not r["ok"]]
    if flagged:
        print("\n" + "=" * 70)
        print("  修复建议 (按顺序尝试)")
        print("=" * 70)
        print("""  1. 补充 filter_list drop 清单:
       python -c "import json; fl=json.load(open('output/{学校}/filter_list.json')); fl['drop']+=['噪声名']; ..."
  2. 重新生成 spider 并重跑:
       uv run python "colleges department/spider_generator.py" --name "{学校}"
       uv run python "colleges department/output/{学校}/spider.py"
  3. 若仍不合格, 检查 CSS 选择器是否过宽(全站 a[href] 必换):
       编辑 output/{学校}/spider_config.json 的 css_selectors
  4. 若 fetch 失败(blocked), 需要 CloakBrowser/CDP:
       编辑 spider_config.json 的 fetch_method 为 cloakbrowser 或 cdp
  5. 全量重评估:
       uv run python "colleges department/evaluate_departments.py" --all""")

if __name__ == "__main__":
    main()
