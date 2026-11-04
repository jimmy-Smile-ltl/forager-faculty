#!/usr/bin/env python3
"""
高校二级院系采集 — LLM 增强版 v4

核心流程:
  1. 从学校首页找到"院系设置/学部院系/教学机构"入口链接（返回 top-5 候选）
  2. 对每个候选：抓取 → 提取 → LLM 分类 → 质量验证
  3. 验证不通过自动回退下一个候选 / 常见路径模式兜底
  4. 全部失败则标记 manual_review_needed
  5. 输出 JSONL + CSV

v4 新增:
  ─ 质量自验证: 子域多样性 / 名称质量 / URL模式 / 数量vs学校类型
  ─ 多候选回退: top-5 → 常见路径 → manual_review
  ─ 断点续跑修复: 验证通过才标记完成

v3 经验教训 (来自 144 所学校实战):
  ─ 院系入口关键词必须足够宽
  ─ 链接容易指向错误页面: 管理机构/党群部门/孔子学院/新闻页
  ─ 瑞数 WAF 分两类: 应用层(CloakBrowser可绕过) vs 网络层(CDP桥接本地Chrome)
  ─ 特殊院校(音乐/戏剧/协和)不能一概而论

运行:
  cd "<repo-root>"
  uv run python "colleges department/scrape_departments.py" [--limit N] [--start N]
"""

import csv, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from collections import Counter

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # college teacher/ 根 (统一 util 层)
from util.llm import get_llm

# ── 路径配置 ────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
INPUT_CSV   = BASE_DIR / "985_211_双一流高校清单.csv"
JSONL_FILE  = BASE_DIR / "depart_985211.jsonl"
CSV_FILE    = BASE_DIR / "depart_985211.csv"
CHECKPOINT  = BASE_DIR / "checkpoint_depart.json"
URL_FIXES_FILE = BASE_DIR / "URL_FIXES.json"

FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

DEPT_KW = ["学院", "系", "研究院", "研究所", "中心", "学部", "书院", "实验室", "基地"]

# ── 院系导航入口关键词 (按优先级排列) ──────────────
ENTRY_KEYWORDS = {
    # 精确匹配 — 最可靠
    "学院设置": 105,  # v4 新增: 最高优先级
    "院系设置": 100, "院系介绍": 95, "学部院系": 95, "学部与院系": 95,
    "教学院系": 90, "院系部门": 85, "院部设置": 85,
    "二级学院": 85, "院系专业": 80, "学院部门": 80, "院系机构": 80,
    "学部学院": 80, "学院（系）": 80, "学院部所": 80, "院所设置": 80,
    # 模糊匹配 — 次选
    "教学机构": 50, "教学单位": 50, "教学科研机构": 50, "教学科研单位": 50,
    "教学科研": 50, "学院导航": 50, "院系中心": 45,
    "机构设置": 30, "组织机构": 25,
}

# ── 质量验证 ───────────────────────────────────
NAV_PATTERNS = ["概况", "简介", "设置", "列表", "首页", "历史", "沿革", "机构"]
NUMERIC_PATH_RE = re.compile(r"/\d+/list\.htm")

# 常见兜底路径模式
FALLBACK_PATH_PATTERNS = [
    "/xysz/index.html", "/xysz.htm",
    "/jgsz/jxkyjg.htm", "/jgsz/jxjg.htm",
    "/xxgk/xysz.htm", "/xyxk1/ejxy.htm",
    "/jgsz/yxsz/index.htm", "/yxsz/list.htm",
]

# 特殊院校（以系/小规模为主，不做数量异常扣分）
SPECIAL_SCHOOL_TAGS = ["音乐", "戏剧", "美术", "体育", "语言", "警察", "公安", "民族", "外交"]


# ── 数据加载 ─────────────────────────────────────
def load_schools() -> list[dict]:
    schools = []
    with open(INPUT_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("学校网址", "").strip():
                schools.append(r)
    return schools


def load_url_fixes() -> dict:
    if URL_FIXES_FILE.exists():
        with open(URL_FIXES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── HTTP 请求 ────────────────────────────────────
def fetch_html(url: str, timeout: int = 15, retries: int = 2) -> str:
    for _ in range(retries):
        try:
            resp = cffi_requests.get(
                url, headers=FAKE_HEADERS, impersonate="chrome131", timeout=timeout)
            resp.encoding = "utf-8"
            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
        except Exception:
            time.sleep(2)
    return ""


# ── URL 工具 ─────────────────────────────────────
def _first_label(url: str) -> str:
    """'linxue.njfu.edu.cn/x/y' -> 'linxue'; 'www.ecust.edu.cn' -> 'www'"""
    host = urlparse(url).netloc
    return host.split(".")[0] if host else ""


def _norm_url(url: str) -> str:
    return url.split("?")[0].rstrip("/")


# ── Step 1: 首页 → 找院系入口 (v4: 返回 top-5) ──
def find_dept_page(html: str, base_url: str, top_n: int = 5) -> list[dict]:
    """
    经验: 启发式 >80% 覆盖率, 新闻和系统链接需要严格过滤
    v4: 返回 top-N 候选而非单选
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        if len(text) < 2 or len(text) > 30:
            continue

        score = 0
        reason = ""
        for phrase, s in ENTRY_KEYWORDS.items():
            if text == phrase:
                score, reason = s, phrase
                break
        if score == 0:
            for phrase in ["院系", "学部", "机构", "组织", "部门", "导航"]:
                if phrase in text and len(text) < 12:
                    score, reason = (60 if phrase in ("院系","学部") else 20), f"contains:{phrase}"
                    break

        if score > 0:
            # 去噪: 新闻/系统入口
            noise = ["新闻", "通知", "公告", "图书馆", "邮箱", "OA", "招标", "召开",
                     "举办", "会议", "讲座", "论坛", "报告"]
            if any(w in text for w in noise):
                continue
            if any(p in href.lower() for p in ["/news","/xw","/info/","/article/",
                                                 "/content/","/detail","/szupload/","/jdyw","/zhxw"]):
                continue
            if len(text) > 25:
                continue

            full = urljoin(base_url, href)
            candidates.append({"url": full, "text": text, "score": score, "reason": reason})

    candidates.sort(key=lambda x: -x["score"])

    # 去重
    seen, unique = set(), []
    for c in candidates:
        k = _norm_url(c["url"])
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique[:top_n]


# ── Step 2: 院系列表页 → 提取候选链接 ──────────────
def extract_candidate_links(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates, seen = [], set()

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or len(text) < 3 or len(text) > 80:
            continue
        if not re.search(r'[一-鿿]', text):
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        if re.search(r'\d{4}[年./-]\d{1,2}[月./-]', text):
            continue
        if not any(kw in text for kw in DEPT_KW):
            continue

        full = urljoin(base_url, href)
        key = _norm_url(full)
        if key in seen:
            continue
        seen.add(key)

        parent = a.parent
        while parent and parent.name not in ("li", "td", "div", "p", "tr"):
            parent = parent.parent
        parent_text = parent.get_text(strip=True)[:100] if parent else ""

        candidates.append({
            "name": text, "url": full,
            "parent_text": parent_text,
            "in_list": bool(a.find_parent(["ul", "ol"])),
            "in_table": bool(a.find_parent("table")),
            "in_nav": bool(a.find_parent("nav")),
        })
    return candidates


# ── Step 3: LLM 批量分类 ──────────────────────────
def _resolve_hospital_urls(departments: list[dict], school_website: str) -> list[dict]:
    """2026-10-22 医院URL错位根修(用户人工实锤: 北大一院采到新闻页/广医一院采到
    研究生院页/重大肿瘤采到新闻页/成中医内江采到新闻页): 学校官网的「附属医院」
    导航链 href 指向校内附属医院列表/介绍页, 而非医院自身官网域名。LLM 分类后对
    type=医院 条目做二次解析: 抓取其 href 页面, 找外部域名链接(医院官网)替换 URL。
    找不到外部域名时保留原 URL(校内附属页至少能打开)。"""
    if not departments:
        return departments
    school_host = urlparse(school_website).netloc.replace("www.", "") if school_website else ""
    for d in departments:
        if (d.get("type") or "") != "医院" or not d.get("url"):
            continue
        u = d["url"]
        host = urlparse(u).netloc.replace("www.", "")
        # 医院官网若已是独立域名(与学校主域不同) → 已是官网, 跳过
        if school_host and host and host != school_host and not host.endswith("." + school_host.split(".")[-2] + ".edu.cn"):
            continue
        # 校内附属页/新闻页 → 抓页面找医院官网链接
        try:
            html = fetch_html(u, timeout=10)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            best = None
            for a in soup.find_all("a", href=True):
                txt = a.get_text(strip=True)
                href = a["href"]
                if not txt or href.startswith(("#", "javascript")):
                    continue
                full = urljoin(u, href)
                h2 = urlparse(full).netloc.replace("www.", "")
                if not h2 or h2 == host:
                    continue
                # 只接受医院官网特征: 非学校域 + 非通用垃圾域 + 锚文本含医院/官网/首页
                if any(k in h2 for k in (".edu.cn", ".gov.cn")):
                    if not any(k in txt for k in ("官网", "网站", "首页", "医院", "门户")):
                        continue
                if any(j in h2 for j in ("baike", "weibo", "qq", "gov", "sina", "163")):
                    continue
                if any(k in txt for k in ("官网", "官方网站", "网站首页", "首页")):
                    best = full
                    break
                if "医院" in txt and best is None:
                    best = full
            if best:
                print(f"    🏥 医院URL二次解析: {d['name']} {u[:50]} → {best[:60]}")
                d["url"] = best
        except Exception:
            continue
    return departments


def _expand_hospital_list(html: str, base_url: str) -> list[dict]:
    """2026-10-22 医院清单遗漏根修(用户人工实锤: 广医15家缺妇女儿童中心/成中医
    18家缺公共卫生中心): 学校官网首页/院系页通常只有「附属医院」单条导航链,
    完整医院清单在二级列表页(如 jgsz/fsyy.htm)。发现「附属医院/医疗机构」导航链
    时抓取列表页, 提取全部医院链接补充进候选。"""
    soup = BeautifulSoup(html, "html.parser")
    extra = []
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True)
        href = a["href"]
        if not txt or not re.search(r"附属医院|医疗机构|临床医学院|直属医院", txt):
            continue
        if href.startswith(("#", "javascript")):
            continue
        full = urljoin(base_url, href)
        if any(k in full.lower() for k in ("/news", "/info/", "/article")):
            continue
        try:
            sub = fetch_html(full, timeout=10)
            if not sub:
                continue
            for a2 in BeautifulSoup(sub, "html.parser").find_all("a", href=True):
                t2 = a2.get_text(strip=True)
                h2 = a2["href"]
                if not t2 or not re.search(r"医院|医疗|卫生", t2):
                    continue
                if h2.startswith(("#", "javascript")):
                    continue
                f2 = urljoin(full, h2)
                if f2 not in [e["url"] for e in extra]:
                    extra.append({"name": t2, "url": f2})
            print(f"    🏥 医院列表页展开: {txt[:20]} → {len(extra)} 个医院链接")
        except Exception:
            continue
    return extra


LLM_PROMPT = """你是高校院系数据分析师。以下是 [{school_name}] 网站上标签含"学院/系/所/中心/实验室/学部/书院/基地"的链接。

请判断每个链接是否属于**教学科研组织**。

**保留 (教学科研):**
- 学院、系、教研室、教学部
- 研究院、研究所、研究中心 (从事学术研究)
- 学部、书院
- 国家级/省部级/教育部重点实验室、工程实验室、工程研究中心
- 实验教学中心、实训中心、实践基地
- 附属医院、教学医院 (协和/上海中医药等)
- 孔子学院 (中外文化交流教学机构)
- 继续教育学院 (非学历教育培训机构)
- 国际教育学院、国际学院

**排除 (非教学):**
- 行政管理: 人事处/财务处/学生处/教务处/后勤/保卫/招生/就业/审计/资产/国资
- 党政机构: 组织部/宣传部/统战部/纪委/机关党委/党校/工会/团委
- 服务窗口: 联系我们/学院地址/学院电话/一卡通/邮箱/图书馆/校医院/出版社
- 系统入口: OA/教务/财务/VPN/邮件/网络认证/校园卡
- 技术管理: 技术转移/实验室管理/实验室风险/设备处/采购/招标
- 新闻/通知 (含日期/动词"召开""举办""举行")

**链接列表 (JSON):**
{links_json}

**返回纯 JSON 数组:**
[{{"name": "原名称", "url": "原URL", "type": "学院"}}, ...]
type 只取: 学院 | 系 | 研究院 | 研究所 | 学部 | 书院 | 中心 | 实验室 | 基地 | 医院

只输出 JSON 数组, 不要 markdown, 不要解释。"""


def llm_classify(school_name: str, candidates: list[dict]) -> list[dict]:
    client = get_llm()
    if not client or not client.available:
        return candidates

    all_results = []
    for i in range(0, len(candidates), 30):
        batch = candidates[i:i + 30]
        links_json = json.dumps(
            [{"name": c["name"], "url": c["url"], "ctx": c["parent_text"][:60]}
             for c in batch],
            ensure_ascii=False)

        prompt = LLM_PROMPT.format(school_name=school_name, links_json=links_json)
        try:
            text = client.ask("", prompt, temperature=0.0)
            match = re.search(r'\[[\s\S]*\]', text)
            if match:
                all_results.extend(json.loads(match.group()))
        except Exception as e:
            print(f"    ⚠️ LLM error: {e}")

    seen = set()
    unique = []
    for d in all_results:
        k = _norm_url(d.get("url", ""))
        if k not in seen:
            seen.add(k)
            unique.append(d)
    return unique


# ── v4 新增: 质量验证器 ──────────────────────────
def validate_result(school: dict, departments: list[dict],
                    dept_page_url: str = "") -> dict:
    """
    四维度评分 (0-100), 返回 {ok, score, issues, checks}
    ok = score >= 60 AND no fatal flags
    """
    if not departments:
        return {"ok": False, "score": 0, "issues": ["empty"], "checks": {}}

    n = len(departments)
    issues = []
    fatal = False
    deductions = 0

    # 学校特征
    name = school.get("学校名称", "")
    feature = school.get("院校特色", "") or school.get("特色", "") or ""
    is_special = any(tag in name or tag in feature for tag in SPECIAL_SCHOOL_TAGS)
    is_985 = str(school.get("is_985", "")).strip() == "1"

    # ── A: 子域多样性 (30分) ──
    labels = [_first_label(d.get("url", "")) for d in departments]
    unique_labels = set(labels)
    label_counts = Counter(labels)

    # ❗ 检查 dept_page_url 是否是非www子域（如 linxue.njfu.edu.cn）
    # 只有当该子域的院系URL占比≥40%，且院系数<10时才标记
    # （院系数≥10说明可能是手工多源拼凑，子域来源多样但dept_page_url恰好是某个）
    dept_label = _first_label(dept_page_url) if dept_page_url else ""
    if dept_label and dept_label != "www" and n >= 3 and not is_special:
        subdomain_ratio = label_counts.get(dept_label, 0) / n
        if subdomain_ratio >= 0.4 and n < 10:
            deductions += 25
            issues.append(f"dept_page_is_subdomain: {dept_label} ({label_counts[dept_label]}/{n} depts)")
            # 如果该子域占绝对主导(≥60%)且院系数≤8 → 致命
            if subdomain_ratio >= 0.6 and n <= 8:
                fatal = True
                issues.append("FATAL: dept page is a single-college sub-site")

    if n >= 5 and len(unique_labels) <= 2:
        # 纯 www 单标签不致命
        if len(unique_labels) == 1 and "www" in unique_labels:
            deductions += 10
        else:
            deductions += 20
            issues.append(f"subdomain_diversity: only {sorted(unique_labels)} (n={n})")

    # ── B: 名称质量 (25分) ──
    # ❗ 阈值从 max(3, 0.3*n) 降到 max(2, 0.25*n)：南京林业大学 nav=2/6=33%
    nav_count = sum(1 for d in departments if any(p in d.get("name", "") for p in NAV_PATTERNS))
    if nav_count >= max(2, 0.25 * n):
        deductions += min(25, int(25 * nav_count / n))
        issues.append(f"nav_names: {nav_count}/{n} look like navigation links")

    # ── C: URL 模式健康 (25分) ──
    # ❗ 阈值从 0.6 降到 0.5：华东理工大学 4/8=0.5 应该触发
    # ❗ 特殊院校（音乐/美术等）豁免，它们常用CMS统一URL模式
    num_paths = sum(1 for d in departments
                    if NUMERIC_PATH_RE.search(d.get("url", ""))
                    and _first_label(d.get("url", "")) == "www")
    if not is_special and num_paths >= max(2, 0.5 * n):
        deductions += min(25, int(25 * num_paths / n))
        issues.append(f"numeric_paths: {num_paths}/{n} are /数字/list.htm on main site")
        # 致命组合: 数字路径主导 + 低多样性
        if num_paths >= 0.5 * n and len(unique_labels) <= 2:
            fatal = True
            issues.append("FATAL: numeric list page with single domain")

    # ── D: 数量 vs 学校类型 (20分) ──
    types = Counter(d.get("type", "") for d in departments)
    dominant_type = types.most_common(1)[0] if types else ("", 0)

    if not is_special:
        if is_985 and n <= 10 and dominant_type[1] / n >= 0.7:
            deductions += 20
            issues.append(f"comprehensive_anomaly: 985 school, only {n} depts, {dominant_type[0]} dominant")
        elif n <= 7 and len(unique_labels) <= 2:
            deductions += 15
            issues.append(f"low_count_single_domain: {n} depts, {len(unique_labels)} labels")

    score = max(0, 100 - deductions)
    ok = score >= 60 and not fatal

    return {
        "ok": ok,
        "score": score,
        "issues": issues,
        "checks": {
            "n": n, "labels": sorted(unique_labels),
            "nav_count": nav_count, "num_paths": num_paths,
            "types": dict(types.most_common(5)),
            "is_special": is_special, "is_985": is_985,
        },
    }


# ── v4 新增: 单候选链路 ──────────────────────────
def _run_candidate(school: dict, name: str, url: str, label: str) -> dict:
    """
    对单个候选 URL: 抓取 → 提取 → LLM分类 → 验证
    返回 {"url": str, "ok": bool, "score": int, "issues": list,
          "result": dict|None, "candidate_count": int}
    """
    html = fetch_html(url)
    if not html:
        return {"url": url, "ok": False, "score": 0,
                "issues": ["fetch failed"], "result": None, "candidate_count": 0}

    candidates = extract_candidate_links(html, url)
    if not candidates:
        return {"url": url, "ok": False, "score": 0,
                "issues": ["no candidates"], "result": None, "candidate_count": 0}

    # 2026-10-22 医院清单展开: 附属医院列表页在二级页时补充进候选(广医/成中医遗漏实锤)
    _extra = _expand_hospital_list(html, url)
    if _extra:
        seen_keys = {_norm_url(c["url"]) for c in candidates}
        for e in _extra:
            if _norm_url(e["url"]) not in seen_keys:
                candidates.append(e)

    depts = llm_classify(name, candidates) if len(candidates) > 3 else candidates
    # 2026-10-22 医院URL二次解析: 校内附属页 → 医院官网域名(北大一院/广医一院错位实锤)
    depts = _resolve_hospital_urls(depts, school.get("学校网址", ""))
    validation = validate_result(school, depts, url)

    partial = {
        "school_name": name,
        "school_url": school.get("学校网址", "").strip(),
        "dept_page_url": url,
        "departments": depts,
        "method": f"LLM classify ({len(depts)}/{len(candidates)} kept)",
        "validation": validation,
    }
    return {
        "url": url, "ok": validation["ok"], "score": validation["score"],
        "issues": validation["issues"], "result": partial,
        "candidate_count": len(candidates),
    }


def _make_manual_review(name: str, website: str, attempts: list, reason: str) -> dict:
    return {
        "school_name": name, "school_url": website,
        "dept_page_url": "",
        "method": f"manual_review_needed ({reason})",
        "departments": [],
        "attempts": attempts,
    }


# ── Step 4: 单所学校处理 (v4 重写) ─────────────────
def process_school(school: dict, url_fixes: dict) -> dict:
    llm = get_llm()
    if llm:
        llm.begin_task()  # 任务边界: 每个学校开始前重新均衡分配 token (校内连续调用固定同一 token)
    name = school["学校名称"]
    website = school.get("学校网址", "").strip()
    if not website.startswith("http"):
        website = "https://" + website

    attempts = []
    visited_urls = set()

    # ── 0. URL_FIXES.json 覆盖 ──
    if name in url_fixes:
        fixed_url = url_fixes[name].get("suggestion", "")
        if fixed_url:
            print(f"    🔧 URL_FIXES → {fixed_url}")
            cand = _run_candidate(school, name, fixed_url, "url_fixes")
            visited_urls.add(_norm_url(fixed_url))
            if cand["ok"]:
                r = cand["result"]
                r["method"] = f"url_fixes → LLM ({len(r['departments'])}/{cand['candidate_count']} kept)"
                r["validation"]["score"] = cand["score"]
                return r
            attempts.append({"url": fixed_url, "label": "url_fixes",
                             "reason": f"❌ {cand['score']}分: {'; '.join(cand['issues'])}"})

    # ── 1. 首页 → top-5 候选 ──
    html = fetch_html(website)
    if not html:
        return _make_manual_review(name, website, attempts, "homepage fetch failed")

    candidates = find_dept_page(html, website, top_n=5)
    if candidates:
        print(f"    🔍 首页候选: {len(candidates)} 个")
    for c in candidates:
        if _norm_url(c["url"]) in visited_urls:
            continue
        visited_urls.add(_norm_url(c["url"]))
        print(f"      → [{c['score']}] {c['text']} ({c['url'][:70]})")
        cand = _run_candidate(school, name, c["url"], c["text"])
        attempts.append({
            "url": c["url"], "label": c["text"],
            "reason": f"{'✅' if cand['ok'] else '❌'} {cand['score']}分: {'; '.join(cand['issues'])}" if cand['issues'] else f"✅ {cand['score']}分",
        })
        if cand["ok"]:
            r = cand["result"]
            r["method"] = f"LLM classify ({len(r['departments'])}/{cand['candidate_count']} kept)"
            r["validation"]["score"] = cand["score"]
            return r

    if not candidates:
        attempts.append({"url": website, "label": "homepage",
                         "reason": "dept page not found (no candidates)"})

    # ── 2. 常见路径模式兜底 ──
    base = website.rstrip("/")
    for pat in FALLBACK_PATH_PATTERNS:
        u = f"{base}{pat}"
        if _norm_url(u) in visited_urls:
            continue
        visited_urls.add(_norm_url(u))
        print(f"    🔄 fallback: {pat}")
        cand = _run_candidate(school, name, u, pat)
        attempts.append({
            "url": u, "label": f"fallback:{pat}",
            "reason": f"{'✅' if cand['ok'] else '❌'} {cand['score']}分: {'; '.join(cand['issues'])}" if cand['issues'] else f"✅ {cand['score']}分",
        })
        if cand["ok"]:
            r = cand["result"]
            r["method"] = f"fallback path → LLM ({len(r['departments'])}/{cand['candidate_count']} kept)"
            r["validation"]["score"] = cand["score"]
            return r

    # ── 3. 全部失败 ──
    return _make_manual_review(name, website, attempts, "all paths exhausted")


# ── JSONL 读写 ────────────────────────────────────
def load_existing_jsonl() -> dict:
    """返回 {school_name: record}"""
    records = {}
    if JSONL_FILE.exists():
        with open(JSONL_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                        records[r["school_name"]] = r
                    except json.JSONDecodeError:
                        pass
    return records


def write_jsonl(records: dict):
    """按 school_name 排序列化写入"""
    with open(JSONL_FILE, "w", encoding="utf-8") as f:
        for name in sorted(records.keys()):
            f.write(json.dumps(records[name], ensure_ascii=False) + "\n")


# ── 主入口 ────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="即使已标记完成也重新采集")
    args = parser.parse_args()

    llm = get_llm()
    if not llm or not llm.available:
        print("❌ LLM 不可用 (检查 college teacher/.env 的 LLM_API_KEY / LLM_BASE_URL)")
        return

    schools = load_schools()
    if args.limit > 0:
        schools = schools[args.start:args.start + args.limit]

    url_fixes = load_url_fixes()

    print("=" * 60)
    print("🎓 985/211 高校院系采集 v4 (质量自验证)")
    print(f"   目标: {len(schools)} 所 | 模型: Claude Haiku 4.5")
    print(f"   URL_FIXES: {len(url_fixes)} 条 | 验证阈值: 60分")
    print("=" * 60)

    # 加载已有记录
    all_records = load_existing_jsonl()

    # 加载断点
    completed = set()
    if CHECKPOINT.exists():
        completed = set(json.loads(CHECKPOINT.read_text()).get("completed", []))

    for i, school in enumerate(schools):
        name = school["学校名称"]
        if not args.force and name in completed:
            continue

        # v4: 也检查已有记录是否已验证通过
        if not args.force and name in all_records:
            existing = all_records[name]
            if existing.get("departments") and existing.get("validation", {}).get("ok", True):
                # v3 旧记录无 validation 字段 → 视为可能未验证，重跑
                if "validation" in existing and existing["validation"].get("ok"):
                    completed.add(name)
                    continue
                elif "validation" not in existing:
                    print(f"\n  [{i+1}/{len(schools)}] {name} (v3旧记录，重验证)")
                else:
                    continue
            else:
                # 已验证不通过或空结果 → 重跑
                print(f"\n  [{i+1}/{len(schools)}] {name} (验证未通过，重跑)")
        else:
            print(f"\n{'─' * 50}\n  [{i+1}/{len(schools)}] {name}")

        try:
            r = process_school(school, url_fixes)
            all_records[name] = r

            # 验证通过才标记完成
            validation = r.get("validation", {})
            if r.get("departments") and validation.get("ok", True):
                if "validation" not in r:
                    # 无validation字段的旧记录: 跑一次验证
                    v = validate_result(school, r["departments"], r.get("dept_page_url", ""))
                    r["validation"] = v
                    if v["ok"]:
                        completed.add(name)
                else:
                    completed.add(name)
                print(f"    ✅ 完成 ({len(r['departments'])} 院系, {validation.get('score','?')}分)")
            else:
                issues = validation.get("issues", [])
                method = r.get("method", "")
                print(f"    ⚠️ 未通过: {'; '.join(issues[:3])} | method={method}")
                # 不要添加到 completed

            # 每处理完一所就保存（防止中断丢失）
            write_jsonl(all_records)
            CHECKPOINT.write_text(json.dumps(
                {"completed": sorted(completed)}, ensure_ascii=False))

        except Exception as e:
            print(f"    ❌ {e}")

        time.sleep(0.3)

    # 重新加载以确保一致
    all_records = load_existing_jsonl()

    # ── 生成 CSV ──
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["学校名称", "学校网址", "院系名称", "院系URL", "院系类型",
                     "采集方式", "验证分数"])
        for r in all_records.values():
            m = r.get("method", "")
            vs = str(r.get("validation", {}).get("score", ""))
            if r.get("departments"):
                for d in r["departments"]:
                    w.writerow([r["school_name"], r["school_url"], d["name"],
                                d["url"], d.get("type", ""), m, vs])
            else:
                w.writerow([r["school_name"], r["school_url"], "", "", "", m, vs])

    # ── 统计 ──
    success = sum(1 for r in all_records.values() if r.get("departments"))
    total = sum(len(r.get("departments", [])) for r in all_records.values())
    validated_ok = sum(1 for r in all_records.values()
                       if r.get("validation", {}).get("ok"))
    manual_review = sum(1 for r in all_records.values()
                        if "manual_review_needed" in r.get("method", ""))
    print(f"\n{'=' * 60}")
    print(f"📊 完成: {success}/{len(all_records)} 所, {total} 院系")
    print(f"   验证通过: {validated_ok} | 需人工处理: {manual_review}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
