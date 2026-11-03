#!/usr/bin/env python3
"""
auto_analysis.py — LLM辅助学校网页结构自动分析

输入: 985_211_双一流高校清单.csv 中的一行（学校名称 + 学校网址）
输出: {学校名}/analysis_report.md + {学校名}/spider_config.json

核心流程:
  1. smart_fetch(首页) → 检测反爬类型
  2. find_dept_page(top_n=10) → 找所有院系入口
  3. LLM 首页分析 → 区分教学/科研/医院等入口，识别多页面
  4. 逐页 fetch + DOM 分析 → 推导 CSS 选择器
  5. LLM 生成过滤规则
  6. 输出 spider_config.json + analysis_report.md

运行:
  uv run python "colleges department/auto_analysis.py" --name "安徽大学" --url "https://www.ahu.edu.cn/"
  uv run python "colleges department/auto_analysis.py" --limit 3
"""

import argparse, csv, json, re, sys, time, traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # college teacher/ 根 (统一 util 层)
from util.llm import get_llm

# ── 路径 ────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
SCHOOLS_DIR = BASE_DIR / "output"
INPUT_CSV = BASE_DIR / "985_211_双一流高校清单.csv"
UTIL_DIR = BASE_DIR.parent / "util"

# ── 常量 ────────────────────────────────────────
ENTRY_KEYWORDS = {
    "学院设置": 105, "院系设置": 100, "教学单位": 95, "科研单位": 95,
    "学院部所": 95, "学校单位": 92, "学院学部": 95, "学院（研究院）": 95,
    "学部院系": 95, "学部与院系": 95, "院系介绍": 95,
    "教学院系": 90, "院系部门": 85, "院部设置": 85, "二级学院": 85,
    "学院（部）": 90, "学院(部)": 90, "院系设置": 100,
    "院系专业": 80, "学院部门": 80, "院系机构": 80,
    "学部学院": 80, "学院（系）": 80, "院所设置": 80,
    "教学机构": 50, "教学科研": 50, "学院导航": 50, "机构设置": 30, "组织机构": 25,
    "科学研究": 90, "科研机构": 90, "研究机构": 85, "科研平台": 85,
    "附属医院": 85, "直属医院": 85, "临床医学院": 85, "独立学院": 85,
    "直属所院": 85, "院所中心": 85,
}

DEPT_KW = ["学院", "系", "研究院", "研究所", "中心", "学部", "书院", "实验室", "基地", "医院"]

# 新闻/动态类噪声词 (find_dept_entries / discover_sub_pages 共用)
noise_hint = ["新闻", "通知", "公告", "图书馆", "邮箱", "OA", "招标", "召开",
              "举办", "会议", "讲座", "论坛", "报告"]

# ── 网络请求 (延迟导入，兼容独立运行) ────────────
_smart_fetch = None

def smart_fetch(url: str, timeout: int = 15):
    global _smart_fetch
    if _smart_fetch is None:
        sys.path.insert(0, str(UTIL_DIR))
        from smart_fetch import smart_fetch as sf
        _smart_fetch = sf
    return _smart_fetch(url, timeout=timeout)


def _first_label(url: str) -> str:
    host = urlparse(url).netloc
    return host.split(".")[0] if host else ""


def _norm_url(url: str) -> str:
    """URL 归一化: 去 query/尾斜杠, 协议统一 https (药科大 zzjg 页 http/https 链接混合,
    expand_source_pages 曾把同页当两页抓取)"""
    u = url.split("?")[0].rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    return u


# ── Step 1: 首页找院系入口 ────────────────────────
def find_dept_entries(html: str, base_url: str, top_n: int = 15) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if len(text) < 2 or len(text) > 40:
            continue

        score = 0
        reason = ""
        for phrase, s in ENTRY_KEYWORDS.items():
            if phrase in text:
                if score < s:
                    score, reason = s, phrase
        if score == 0:
            continue

        # 入口短语命中但链接无效（#/javascript → JS 下拉菜单，如厦大"学部院系"）
        # 收集父容器(li/dropdown)内子菜单的有效链接作为子入口
        if not href or href.startswith("#") or href.startswith("javascript"):
            parent = a.find_parent("li") or a.parent
            sub_links = []
            for sib in parent.find_all("a", href=True) if parent else []:
                shref = sib["href"]
                stext = sib.get_text(strip=True)
                if shref.startswith("#") or shref.startswith("javascript") or not shref:
                    continue
                if not any(kw in stext for kw in DEPT_KW):
                    continue
                if len(stext) < 4 or len(stext) > 40:
                    continue
                if any(w in stext for w in noise_hint):
                    continue
                sfull = urljoin(base_url, shref)
                _bh = urlparse(base_url).netloc
                _nh = urlparse(sfull).netloc
                if _nh != _bh and not _nh.endswith("." + _bh) and not _bh.endswith("." + _nh):
                    continue
                sub_links.append((sfull, stext))
            if sub_links:
                for sfull, stext in sub_links:
                    candidates.append({"url": sfull, "text": stext,
                                       "score": max(score - 5, 60),
                                       "reason": f"sub_of:{reason}"})
            continue

        noise = noise_hint
        if any(w in text for w in noise):
            continue
        if any(p in href.lower() for p in ["/news", "/xw", "/info/", "/article/",
                                             "/content/", "/detail", "/szupload/"]):
            continue

        full = urljoin(base_url, href)
        # 外站链接排除 (mp.weixin.qq.com 等 — 入口页必须是本校站点; 同站/子域互认)
        _bh = urlparse(base_url).netloc
        _nh = urlparse(full).netloc
        if _nh != _bh and not _nh.endswith("." + _bh) and not _bh.endswith("." + _nh):
            continue
        candidates.append({"url": full, "text": text, "score": score, "reason": reason})

    candidates.sort(key=lambda x: -x["score"])
    seen, unique = set(), []
    for c in candidates:
        k = _norm_url(c["url"])
        if k not in seen:
            seen.add(k)
            unique.append(c)

    # 入口短语候选 <3 但首页直接列出 ≥5 个学院链接 → 首页就是院系列表页
    # (南京林业大学模式: unique 为空; 中国药科大学模式: unique 有"科研单位"
    #  但首页另有 13 个学院链接未被任何入口短语命中 — 两者都补入首页)
    if len(unique) < 3:
        direct_names = []
        _nav = ("简介", "概况", "新闻", "通知", "公告", "招聘", "校友", "招生",
                "就业", "党建", "师资", "学生", "教学", "科研", "服务", "管理")
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]
            if len(text) < 4 or len(text) > 40:
                continue
            if not any(kw in text for kw in DEPT_KW):
                continue
            if any(w in text for w in _nav):  # "学院简介/学院新闻"不算直接学院链接
                continue
            if href.startswith("#") or href.startswith("javascript") or not href:
                continue
            direct_names.append(text)
        if len(direct_names) >= 5:
            print(f"   🏠 首页直接列出 {len(direct_names)} 个学院，将首页作为院系列表页")
            unique.append({"url": base_url, "text": "🏠 首页(直接列表)",
                           "score": 200, "reason": "homepage_is_dept_list"})

    return unique[:top_n]


# ── Step 2: DOM 分析 → CSS 选择器推导 ────────────
def analyze_page_dom(html: str, base_url: str) -> dict:
    """返回页面 DOM 分析结果"""
    soup = BeautifulSoup(html, "html.parser")

    # 机构链接判定: 含院系关键词 且 以机构词结尾 且 长度3-16 且 非新闻路径
    # (防新闻标题"XX学院举办活动"污染密度 — 天医大 #wp_news_w55 新闻容器误判案例)
    _INST_END = ("学院", "书院", "系", "学部", "研究院", "研究所", "实验室",
                 "教学部", "教研部", "中心", "基地", "医院", "所", "部")
    _NEWS_PATH = ("/news/", "/xw/", "/article/", "/content/", "/info/", "/tzgg/",
                  "/notice/", "/announcement/")

    def _is_inst_text(t):
        t = t.strip()
        return 3 <= len(t) <= 16 and t.endswith(_INST_END)

    def _is_inst_link(a):
        t = a.get_text(strip=True)
        if not _is_inst_text(t):
            return False
        href = a.get("href", "")
        return not any(np in href for np in _NEWS_PATH)

    # 找到所有含院系关键词的链接
    dept_links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if len(text) < 3 or len(text) > 80:
            continue
        if not any(kw in text for kw in DEPT_KW):
            continue
        href = a["href"]
        if href.startswith("#") or href.startswith("javascript"):
            continue
        dept_links.append(a)

    if not dept_links:
        return {"link_count": 0, "total_links": len(soup.find_all("a", href=True)),
                "selectors": [], "container_class": ""}

    # ── 选择器选取 v2: 容器密度评分 (机构链接密度量化 — 提取严格化) ──
    # 收集候选容器: 院系链接的直接父 + 各级带 class/id 祖先 (li→ul.list→div→#app)
    # 新闻特征容器 (id/class 含 news/xw/article 等) 跳过 — 新闻标题含学院词会污染密度
    _NEWS_HINT = ("news", "xw", "article", "notice", "tzgg", "announce")

    def _news_hint(text):
        low = text.lower()
        return any(h in low for h in _NEWS_HINT)

    container_sels = set()
    for a in dept_links[:80]:
        p = a.parent
        level = 0
        while p and p.name not in ("body", "html", "[document]") and level < 6:
            cls = p.get("class") or []
            pid = p.get("id", "")
            if p.name in ("li", "ul", "ol", "div", "table", "dl", "section"):
                if pid:
                    if not _news_hint(pid):
                        container_sels.add(f"#{pid} a")
                elif cls:
                    cname = ".".join(cls[:2])
                    if not _news_hint(cname):
                        container_sels.add(f"{p.name}.{cname} a")
            if level == 0:  # 直接父形态: div.departbox > a / li > a
                if pid:
                    if not _news_hint(pid):
                        container_sels.add(f"#{pid} > a")
                elif cls:
                    cname = ".".join(cls[:2])
                    if not _news_hint(cname):
                        container_sels.add(f"{p.name}.{cname} > a")
                else:
                    container_sels.add(f"{p.name} > a")
            p = p.parent
            level += 1

    # 密度评分: 容器内机构链接占比 (机构链接 = 以机构词结尾的短名称, 非新闻路径)
    scored = []
    for sel in container_sels:
        els = soup.select(sel)
        total = len(els)
        if total < 3:
            continue
        dept_count = sum(1 for a in els if _is_inst_link(a))
        scored.append((sel, dept_count, total, dept_count / total))

    def _tier(density):
        if density >= 0.9:
            return 1      # 超高密度: 纯机构容器 (优先)
        if density >= 0.7:
            return 2      # 高密度
        if density >= 0.4:
            return 3      # 中密度 (附容器内噪声样本供 LLM 参考)
        return 9          # 低密度: 不采用

    selectors = []
    for sel, dept_count, total, density in sorted(scored, key=lambda x: (_tier(x[3]), -x[1])):
        tier = _tier(density)
        if tier == 9:
            continue
        if density >= 0.7 and dept_count < 5:
            continue      # 高密度但覆盖太少 (防过窄漏采)
        item = {"selector": sel, "confidence": int(density * 100),
                "density": round(density, 2),
                "sample": [a.get_text(strip=True)[:30] for a in soup.select(sel)[:5]]}
        if tier == 3:     # 中密度: 记录容器内噪声名, LLM 判定时参考
            item["noise_sample"] = [a.get_text(strip=True)[:30] for a in soup.select(sel)
                                    if not any(kw in a.get_text(strip=True) for kw in DEPT_KW)][:5]
        selectors.append(item)
        if len(selectors) >= 3:
            break

    # 回落: 密度评分无果 (SPA/卡片布局等) → 通用列表选择器 (保留原逻辑)
    if not selectors:
        for generic in ["ul li a", ".list li a", ".news_list li a", ".list_content a",
                        "table a", ".wp_list a", ".article_list a"]:
            hits = soup.select(generic)
            if len(hits) >= 3:
                match_count = sum(1 for a in hits if _is_inst_link(a))
                if match_count >= 3:
                    selectors.append({"selector": generic, "confidence": min(90, 100 * match_count / len(hits)),
                                      "sample": [a.get_text(strip=True)[:30] for a in hits[:5]]})
            if len(selectors) >= 3:
                break

    best_container = max(scored, key=lambda x: _tier(x[3]))[0].split(" > ")[0] if scored else ""
    selectors.sort(key=lambda x: -x["confidence"])
    return {
        "link_count": len(dept_links),
        "total_links": len(soup.find_all("a", href=True)),
        "selectors": selectors[:3],
        "container_class": best_container,
        "sample_names": [a.get_text(strip=True)[:40] for a in dept_links[:20]],
    }


# 展开子页的导航/管理排除词 (西北大学 zzjg → yx 场景: "院系"要收, "学校简介/研究生院"不收)
SUB_NAV_EXC = ["简介", "概况", "首页", "学校", "招聘", "校友", "基金", "图书馆",
               "新闻", "通知", "公告", "学报", "校历", "地图", "邮箱", "章程",
               "标识", "校徽", "院徽", "党建", "党委", "团委", "工会", "党政",
               "机关", "后勤", "财务", "招生", "就业", "联系", "关于", "管理",
               "服务", "研究生", "教师", "学生", "办学", "文化", "国际", "继续教育"]
# 栏目型子页的修饰词 (实体学院名"药学院"不含 → 不收, 防展开 30 个学院官网)
SUB_MOD_WORDS = ("设置", "机构", "单位", "院系", "教学", "科研", "附属", "研究", "部门", "学系")
SUB_PURE_ORG = {"学院", "医院", "研究所", "研究院", "实验室", "中心", "系", "学部", "书院", "基地"}


def discover_sub_pages(html: str, base_url: str, max_subs: int = 40) -> list[dict]:
    """在当前页面中发现子列表页(hub 页面)。

    借鉴 department teachers probe_site.py enumerate_links 的子菜单展开思路:
    - 短文本(≤6字)链接, 文本是"栏目型"组织词即收为子页候选
      (纯组织词"学院/医院/研究所" = 协和 xy/yy/yjs 模式;
       含修饰词"教学/科研/附属/院系/设置" = 西北大学"院系"→yx.htm 模式)
    - 实体学院名("药学院""外国语学院")不收 — 它们是院系列表本身, 展开会造成
      source_page 膨胀(30 个学院官网), 且学院官网"学院简介"链接会污染提取
    - 2~max_subs 个子页才认为是 hub(>40 说明是列表页本身, 不递归, 防膨胀)
    """
    soup = BeautifulSoup(html, "html.parser")
    sub_candidates = []

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or not href:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        if len(text) < 2 or len(text) > 8:
            continue
        # 导航/管理词排除 ("学校简介"→简介, "研究生院"→研究生, "后勤处"→后勤)
        if any(w in text for w in SUB_NAV_EXC):
            continue
        if any(w in text for w in noise_hint):  # 新闻/通知/公告
            continue
        # 栏目型判定: 纯组织词 (协和 xy/yy/yjs) 或 含修饰词的组合词 (院系/教学单位)
        if text not in SUB_PURE_ORG and not any(m in text for m in SUB_MOD_WORDS):
            continue
        full = urljoin(base_url, href)
        _bh = urlparse(base_url).netloc
        _nh = urlparse(full).netloc
        if _nh != _bh and not _nh.endswith("." + _bh) and not _bh.endswith("." + _nh):
            continue
        sub_candidates.append({"text": text, "url": full})

    if not sub_candidates:
        return []

    # 去重
    seen = set()
    unique_subs = []
    for s in sub_candidates:
        k = _norm_url(s["url"])
        if k not in seen:
            seen.add(k)
            unique_subs.append(s)

    # 2~max_subs 个子页才认为是 hub (借鉴 probe_site 容器 ≤40 防膨胀)
    if 2 <= len(unique_subs) <= max_subs:
        return unique_subs

    return []


# 机构名链接判定: 3-16字、含组织词、以机构词结尾 (用于区分"列表页"与"导航hub")
_INST_END2 = ("学院", "书院", "系", "学部", "研究院", "研究所", "实验室", "中心", "基地", "医院", "教学部", "教研部")


def _count_inst_links(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    n = 0
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        if 3 <= len(t) <= 16 and any(kw in t for kw in DEPT_KW) \
                and t.endswith(_INST_END2):
            n += 1
    return n


# ── Step 2b: 扩展页面列表（BFS 发现子页）─────
def expand_source_pages(entry_pages: list[dict], entries: list[dict],
                        max_depth: int = 1) -> list[dict]:
    """BFS 展开: 对组织/机构类入口页做一层子页扫描。

    借鉴 department teachers probe_site.py enumerate_links 的 BFS 展开思路:
    - 展开候选 = LLM 选中的入口 ∪ 首页候选中的组织类入口(不限分数 —
      西北大学"组织机构" zzjg.htm 仅 25 分, 但它指向"院系"→ yx.htm 院系列表)
    - 子页文本是栏目型组织词(院系/教学单位/…), 2-40 个才展开
    - 原页若直接含 ≥5 个机构链接 → 它是院系列表页本身(厦大学部页 32 学院),
      保留原页, 子页只作补充; 否则(协和 zssy 导航 hub)子页替换原页
    - LLM 入口 discover 无果时保底保留; 低分 bfs 候选无果不硬塞
    """
    # 收集展开候选 (URL 去重)
    cand_map: dict[str, dict] = {}
    for ep in entry_pages:
        cand_map[_norm_url(ep["url"])] = {"url": ep["url"], "label": ep["label"],
                                          "src": "llm"}
    for e in entries:
        if any(k in e["text"] for k in ("机构", "组织", "单位", "设置", "院系")):
            k = _norm_url(e["url"])
            if k not in cand_map:
                cand_map[k] = {"url": e["url"], "label": e["text"], "src": "bfs"}

    all_pages = []
    for cand in cand_map.values():
        url = cand["url"]
        label = cand["label"]
        html, _ = smart_fetch(url)
        if not html:
            if cand["src"] == "llm":
                all_pages.append({"url": url, "label": label, "reason": "llm"})
            continue

        subs = discover_sub_pages(html, url) if max_depth > 0 else []
        if subs:
            direct_count = _count_inst_links(html)
            if direct_count >= 5:
                # 列表页本身 (厦大学部页: 页内直接列 32 学院) — 保留 + 子页补充
                print(f"      🔀 二级发现({label}): {len(subs)} 个子页, 原页含 "
                      f"{direct_count} 个机构链接 → 保留原页+追加")
                all_pages.append({"url": url, "label": label, "reason": "llm"})
            else:
                print(f"      🔀 二级发现({label}): {len(subs)} 个子页 → {url[:60]}")
            for s in subs:
                all_pages.append({
                    "url": s["url"],
                    "label": s["text"],
                    "reason": f"二级发现: 从 {url[:40]}",
                })
        elif cand["src"] == "llm":
            all_pages.append({"url": url, "label": label, "reason": "llm"})

    # 去重: 按 URL 归一化
    seen = set()
    unique_pages = []
    for p in all_pages:
        k = _norm_url(p["url"])
        if k not in seen:
            seen.add(k)
            unique_pages.append(p)
        else:
            # 保留更短的 label
            for up in unique_pages:
                if _norm_url(up["url"]) == k:
                    if len(p["label"]) < len(up["label"]):
                        up["label"] = p["label"]
                    break

    return unique_pages


# ── Step 3: LLM 首页分析 → 多页面识别 ────────────
ANALYSIS_PROMPT = """你是高校网站结构分析师。分析 [{school_name}] 首页的院系相关入口链接。

**上下文:**
学校名称: {school_name}
学校网址: {school_url}

**首页候选入口链接 (按启发式评分排列):**
{candidates_text}

**任务:**
1. 判断哪些链接指向"二级院系列表页"（教学单位、科研单位、附属医院等组织结构页）
2. 如果一个页面包含了所有院系,不需要多个。但如果教学单位和科研单位在不同页面,**必须都保留**（如安徽大学: 24279教学 + 24281科研）
3. 排除: 新闻页、管理机构(党政/后勤/财务)、单个学院/研究所的介绍页
4. 对每个有效入口,归类标签: 教学单位 | 科研单位 | 附属医院 | 综合列表

**⚠️ 硬规则 (必须遵守):**
- 首页如果有多个并列的机构入口(如"学院学部"/"教学单位"/"科研单位"/"附属医院"/"研究机构"等),**必须全部纳入 entry_pages**,不能只选部分!遗漏任何一个都会导致院系采集不完整。
- 名称含"学院"或"学部"的入口(如"学院学部"、"学院设置")是**最高优先级**,绝对不能遗漏。
- 如果你不确定某个入口是否有效,倾向于**保留**而不是排除。

**返回纯 JSON:**
{{
  "entry_pages": [
    {{"url": "完整URL", "label": "教学单位", "reason": "首页'教学单位'链接指向的列表页"}}
  ],
  "multi_page_note": "如只有一个综合列表填'单页',如分多页说明原因",
  "special_notes": "任何特殊结构说明(分学部/分校区/JS渲染等)"
}}

只输出 JSON, 不要 markdown, 不要解释。"""


def llm_analyze_homepage(name: str, url: str, entries: list[dict]) -> dict:
    client = get_llm()
    if not client or not client.available:
        # 无 LLM 时用启发式: 保留前 3 个高分候选
        return {
            "entry_pages": [{"url": e["url"], "label": e["text"], "reason": f"score={e['score']}"}
                           for e in entries[:3]],
            "multi_page_note": "无LLM，启发式取前3",
            "special_notes": "",
        }

    candidates_text = "\n".join(
        f"  [{e['score']}] {e['text']} → {e['url']} (reason: {e['reason']})"
        for e in entries[:15]
    )

    prompt = ANALYSIS_PROMPT.format(
        school_name=name, school_url=url, candidates_text=candidates_text)

    try:
        text = client.ask("", prompt, temperature=0.0)
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"    ⚠️ LLM首页分析失败: {e}")

    return {"entry_pages": [{"url": e["url"], "label": e["text"], "reason": f"fallback score={e['score']}"}
                           for e in entries[:3]], "multi_page_note": "LLM失败，启发式兜底", "special_notes": ""}


# ── Step 4: LLM 过滤规则生成 ─────────────────────
FILTER_PROMPT = """你是高校数据采集规则专家。分析 [{school_name}] 的院系列表页内容,生成精确的过滤规则。

**学校信息:**
- 名称: {school_name}
- 院系列表页类型: {page_labels}

**各页面院系名称样本 (共 {total_samples} 个):**
{samples_text}

**默认排除关键词 (基础规则,总是包含):**
管理, 行政, 后勤, 财务, 教务, 审计, 资产, 保卫, 招生, 就业, 离退休, OA, 一卡通, VPN, 图书馆, 校医院, 出版社, 新闻, 通知, 公告

**注意: 孔子学院 和 继续教育学院 属于教学相关机构,应保留,不加入排除列表。**

**任务:** 基于样本内容,补充该校特有的排除关键词和URL模式。

**返回纯 JSON:**
{{
  "include_keywords": ["学院","系","研究院","研究所","中心","学部","书院","实验室","基地","医院"],
  "exclude_keywords": ["管理","行政","后勤","财务","教务","审计","资产","保卫","招生","就业","离退休","OA","一卡通","VPN","图书馆","校医院","出版社","新闻","通知","公告"],
  "exclude_url_patterns": ["/news/","/info/","/xw/","/article/","/content/","/detail/"],
  "exclude_name_patterns": [],
  "notes": "如有该校特有的排除项,填在此说明"
}}

只输出 JSON。"""


# ── Step 3b: LLM 生成过滤清单 (filter_list.json) ──
FILTER_LIST_PROMPT = """你是高校院系数据分析师。你的任务是从候选名称中筛选出**承担教育教学或科学研究职能的二级组织**（下一步要去这些组织采集教师信息）。判断标准：**这个组织有自己的教师队伍吗？有→keep，没有→drop。**

**KEEP (教学科研组织 — 有自己的教师/科研人员):**
- 学院、系、教研室、教学部、体育部、外语部、研究院、研究所、研究中心、实验室、书院、学部、基地
- 附属医院、教学医院、实验教学中心、实训中心、实践基地
- 孔子学院、继续教育学院、终身教育学院、国际教育学院
- 卓越工程师学院、未来技术学院等特色学院
- 书院（明德书院/崇实书院等，无 URL 也要保留）
- 无 URL 的学院/系也保留（浙大纯文本学院列表模式）

**DROP (无教师队伍的行政/服务/宣传/导航):**
1. 行政处室: 教务处、人事处、财务处、发展规划处、保卫处、审计处、基建处、离退休处、合作处、国际交流与合作处
2. 办公室: 信息化办公室、党政办公室、校长办公室、学科建设办公室、人才工作办公室
3. 党政机构: 党委组织部/宣传部/统战部/教师工作部/学生工作部、纪委、党校、工会、团委、妇委会、纪检监察
4. 管理导航: 本科生院、研究生院、本科生教育、研究生教育、留学生教育、招生、就业、人才招聘、教师个人主页、人才工作、人事工作、人事管理
5. 服务设施: 图书馆、档案馆、博物馆、校医院、医疗服务、后勤部、网络中心、采购中心、分析测试中心、教师教学发展中心、期刊中心
6. 文化宣传: 学院简介、学院简史、学院概况、学院介绍、历任院长、现任领导、名家名作、大学文化、校史馆、校园服务、通勤车班次、校历、办公电话、心理健康、微博、微信
7. 科研管理导航: 科研基地、科研机构、科研平台、研究平台、重点科技创新平台、大型设备仪器平台、一流学科建设、博士后流动站、自然科学科研信息网、社会科学科研信息网、学报、学术期刊
8. 外联宣传: 校友会、校友总会、教育基金会、基金会、国际交流、国内合作、校际合作、合作办学、产学研合作、服务基础教育、乡村振兴与对口支援、教育出版、大学志愿者服务、专家学者走基层
9. 分类标题: 全国重点实验室、教育部重点实验室、省级重点实验室、市级重点实验室、国家工程研究中心（分类名而非具体实验室）
10. 页面导航: 院系设置、院系部门、学部学院、学部学院（所、中心）、学院（系）、教学机构、科研机构、教学科研机构、机构设置、组织机构、附属医院（作为导航标题时）、研究所（作为导航标题时）
11. 行政中心（非院系）: 信息与网络中心、网络信息中心、教学运行与考务中心、数据中心、信息化办公室、数字化与智算技术中心、西山实验服务中心、心理咨询中心
12. 医院内部栏目页（附属医院站内导航, 非院系）: 医院概况、医院特色、科室介绍、内科部、外科部、检验科、专家介绍、名医传承、院内制剂、重点学科详情、品牌护理详情（凡以"详情/概况/特色"结尾的栏目标题均 drop）
13. 校医院/门诊部/医务室（行政服务）→ drop；**附属医院/直属医院/临床医学院/附属XX医院（有医生队伍）→ keep**
14. 杂项: 标识系统、信息公开、IT服务、校史、图库、校区、教学日历、文明校园、学生工作网站、干部培训、就业创业、中德学院（非教学性质的合作项目）
15. 页面注释文字: 以"注："、"备注："、"说明："开头的名称（如"注：*为非实体学院"）是页面注脚说明文字，不是机构 → drop（即使含"学院/系"等词，因注释只是提到这些词，不代表该组织本身）

**特殊规则:**
- 名称带"学院/系"但实际是管理机构（本科生院、研究生院、培训中心）→ drop
- 名称含"学院/系/医院"等词但本身是导航/分类标题（院系设置、教学科研机构（专业学院））→ drop
- "继续教育学院/国际教育学院/远程教育学院"有教师 → keep；但"继续教育/国际教育"（导航词）→ drop
- 附属医院（有教师队伍）→ keep；校医院（行政服务）→ drop；医院内部栏目页（科室/概况/详情）→ drop
- 名称后面带顿号"、"（如"教务处、"）是导航截断 → drop
- 名称含 ">" 分隔符（"本科教育 > 更多"）→ 去除 ">" 后再判断
- 名称以"注："、"备注："、"说明："开头（如"注：*为非实体学院"）→ 页面注释文字 → drop
- 名称重复机构名（"戏剧教育系戏剧教育系师资队伍建设"）是新闻标题 → drop

**正例 (keep):** 马克思主义学院、机械工程学院、崇实书院、附属龙华医院、运动健身科技教育部重点实验室、文学院（无URL）、体育部
**反例 (drop):** 发展规划处、信息化办公室、教务处、学院简史、历任院长、院系设置、院系部门、学生工作部（学生处）、本科生院、研究生院、分析测试中心、注：*为非实体学院

**候选名称样本:**
{samples_text}

**返回纯 JSON:**
{{
  "drop": ["发展规划处", "信息化办公室", "教务处", "院系设置", "院系部门", "学院简史", ...],
  "keep": ["马克思主义学院", "机械工程学院", "崇实书院", "文学院", ...]
}}

只输出 JSON。drop 是排除清单, keep 是保留清单。名称要原样保留（包括空格和标点）。"""


def llm_generate_filter_list(name: str, all_samples: list[str]) -> dict:
    """agent 阶段用 LLM 生成 drop/keep 过滤清单, 存为 filter_list.json
    带重试机制: JSON 解析失败最多重试 2 次"""
    client = get_llm()
    if not client or not client.available:
        return {"drop": [], "keep": []}

    # 去重样本
    samples = list(dict.fromkeys(s for s in all_samples if s and len(s) >= 2))
    samples_text = "\n".join(f"  - {s}" for s in samples[:100])

    prompt = FILTER_LIST_PROMPT.format(school_name=name, samples_text=samples_text)

    for attempt in range(3):
        try:
            text = client.ask("", prompt, temperature=0.0)
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                result = json.loads(match.group())
                result.setdefault("drop", [])
                result.setdefault("keep", [])
                print(f"   📋 filter_list: drop={len(result['drop'])} keep={len(result['keep'])}")
                return result
            else:
                print(f"   ⚠️ filter_list 第{attempt+1}次解析失败, 重试...")
        except Exception as e:
            print(f"   ⚠️ filter_list 第{attempt+1}次失败: {e}, 重试...")
    return {"drop": [], "keep": []}


def llm_generate_filters(name: str, page_labels: list[str],
                         all_samples: list[str]) -> dict:
    client = get_llm()
    if not client or not client.available:
        return {
            "include_keywords": DEPT_KW,
            "exclude_keywords": ["管理", "行政", "后勤", "财务", "教务", "审计", "资产",
                                "保卫", "招生", "就业", "离退休", "OA", "一卡通", "VPN",
                                "图书馆", "校医院", "出版社", "新闻", "通知", "公告"],
            "exclude_url_patterns": ["/news/", "/info/", "/xw/", "/article/", "/content/", "/detail/"],
            "exclude_name_patterns": [],
            "notes": "无LLM，使用默认规则",
        }

    samples_text = "\n".join(f"  - {s}" for s in all_samples[:40])
    prompt = FILTER_PROMPT.format(
        school_name=name,
        page_labels=", ".join(page_labels),
        total_samples=len(all_samples),
        samples_text=samples_text)

    try:
        text = client.ask("", prompt, temperature=0.0)
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"    ⚠️ LLM过滤规则失败: {e}")

    return {
        "include_keywords": DEPT_KW,
        "exclude_keywords": ["管理", "行政", "后勤", "财务", "教务", "审计", "资产",
                            "保卫", "招生", "就业", "离退休", "OA", "一卡通", "VPN",
                            "图书馆", "校医院", "出版社", "新闻", "通知", "公告"],
        "exclude_url_patterns": ["/news/", "/info/", "/xw/", "/article/", "/content/", "/detail/"],
        "exclude_name_patterns": [],
        "notes": "LLM失败，使用默认规则",
    }


# ── 主逻辑 ──────────────────────────────────────
def analyze_school(school_name: str, school_url: str) -> dict:
    """分析一所学校，返回 spider_config 字典和报告行"""
    _llm = get_llm()
    if _llm:
        _llm.begin_task()  # 任务边界: 每个学校开始前重新均衡分配 token (校内连续调用固定同一 token)
    if not school_url.startswith("http"):
        school_url = "https://" + school_url

    print(f"\n{'='*60}")
    print(f"🔍 {school_name}")
    print(f"   {school_url}")

    # ── 0. 首页抓取 + 反爬检测 ──
    html, fetch_method = smart_fetch(school_url)

    # curl_cffi+RsSpider 都失败 → 逐级尝试 CloakBrowser → CDP
    if not html and fetch_method == "blocked":
        from smart_fetch import _try_drission, _try_cdp
        html, fetch_method = _try_drission(school_url, 20)
    if not html and fetch_method in ("", "blocked"):
        from smart_fetch import _try_cdp
        html, fetch_method = _try_cdp(school_url, 20)

    if not html:
        return {"error": f"homepage fetch failed: {fetch_method}"}

    anti_crawl = "curl_cffi"
    if fetch_method == "ruishu":
        anti_crawl = "ruishu"
    elif fetch_method in ("blocked", "curl_cffi(fail)", "ruishu(fail)"):
        anti_crawl = "blocked"
    print(f"   🌐 反爬: {anti_crawl} ({fetch_method}) | HTML {len(html)} 字符")

    # ── 1. 找院系入口 ──
    entries = find_dept_entries(html, school_url, top_n=15)
    if not entries:
        # 入口为空 → 可能是 SPA/Javascript 渲染
        # 先试 CloakBrowser 重新抓首页
        if anti_crawl == "curl_cffi":
            from smart_fetch import _try_drission
            print(f"   ⚠️ curl_cffi 无入口, 尝试 DrissionPage 渲染...")
            html2, method2 = _try_drission(school_url, 20)
            if html2 and len(html2) > 1000:
                html = html2
                fetch_method = method2
                anti_crawl = "drission"
                print(f"   ✅ DrissionPage 成功 | HTML {len(html)} 字符")
                entries = find_dept_entries(html, school_url, top_n=15)

    if not entries:
        # 仍无入口 → 查 URL_FIXES.json 兜底
        url_fixes = {}
        try:
            fixes_file = BASE_DIR / "URL_FIXES.json"
            if fixes_file.exists():
                url_fixes = json.loads(fixes_file.read_text("utf-8"))
        except Exception:
            pass
        if school_name in url_fixes:
            suggestion = url_fixes[school_name].get("suggestion", "")
            if suggestion:
                print(f"   🔧 URL_FIXES 兜底: {suggestion}")
                entries = [{"url": suggestion, "text": "URL_FIXES",
                            "score": 200, "reason": "manual_fix"}]
        if not entries:
            return {"error": "no dept entry links found on homepage"}

    print(f"   🔗 候选入口: {len(entries)} 个")
    for e in entries[:8]:
        print(f"      [{e['score']:>3}] {e['text']:<12} → {e['url'][:70]}")

    # ── 2. LLM 首页分析 → 多页面识别 ──
    llm_result = llm_analyze_homepage(school_name, school_url, entries)
    entry_pages = llm_result.get("entry_pages", [])

    if not entry_pages:
        # 兜底: 取最高分
        entry_pages = [{"url": entries[0]["url"], "label": entries[0]["text"], "reason": "fallback"}]

    print(f"   📋 LLM 识别入口: {len(entry_pages)} 页")
    for ep in entry_pages:
        print(f"      [{ep['label']}] {ep['url'][:70]}")

    # ── 2a. 入口覆盖自检: LLM 选出的入口 vs 首页全部高分候选 ──
    entry_urls = {_norm_url(ep["url"]) for ep in entry_pages}
    missed = [e for e in entries
              if e["score"] >= 85 and _norm_url(e["url"]) not in entry_urls]
    if missed:
        print(f"   ⚠️ 入口覆盖自检: {len(missed)} 个高分候选未被纳入")
        for e in missed:
            print(f"      [missed {e['score']}] {e['text']} → {e['url'][:60]}")
        # 自动补入高分遗漏（组织类入口; 首页直接列表候选也放行 — 药科大模式）
        org_types = ("学院", "学部", "附属医院", "教学", "科研", "机构", "单位", "研究")
        for e in missed:
            if e["reason"] == "homepage_is_dept_list" or any(k in e["text"] for k in org_types):
                entry_pages.append({"url": e["url"], "label": e["text"],
                                    "reason": f"auto-coverage: 首页高分候选遗漏补入"})
                print(f"      ✅ 补入: {e['text']} → {e['url'][:60]}")

    # ── 2b. 二级页面发现 (BFS 展开: LLM 入口 + 低分组织入口都做一层子页扫描) ──
    entry_pages = expand_source_pages(entry_pages, entries)
    if len(entry_pages) > len(llm_result.get("entry_pages", [])):
        print(f"   🔀 二级展开后: {len(entry_pages)} 页")

    # ── 3. 逐页抓取 + DOM 分析 ──
    source_pages = []
    all_samples = []
    page_labels = []

    for ep in entry_pages:
        url = ep["url"]
        label = ep["label"]
        print(f"   📄 {label}: {url[:70]}")
        page_labels.append(label)

        page_html, page_method = smart_fetch(url)
        if not page_html:
            print(f"      ❌ fetch failed ({page_method})")
            source_pages.append({
                "url": url, "label": label,
                "css_selectors": ["ul li a"],
                "link_attr": "href",
                "fetch_method": page_method,
            })
            continue

        dom = analyze_page_dom(page_html, url)
        print(f"      链接: {dom['link_count']}个院系链接 / {dom['total_links']}总链接")

        best_selectors = [s["selector"] for s in dom.get("selectors", [])]
        if not best_selectors:
            best_selectors = ["ul li a"]
        # ⚠️ 严格约束: 不用全站 a[href] — 避免把导航/新闻/页脚全采进来
        best_selectors = [s for s in best_selectors if s != "a[href]"]
        if not best_selectors:
            best_selectors = ["ul li a"]
        print(f"      选择器: {best_selectors[:2]}")

        source_pages.append({
            "url": url, "label": label,
            "css_selectors": best_selectors,
            "link_attr": "href",
            "fetch_method": page_method,
        })
        all_samples.extend(dom.get("sample_names", []))

    # ── 3b. LLM 生成过滤清单 (filter_list.json: drop/keep) ──
    filter_list = llm_generate_filter_list(school_name, all_samples)

    # ── 4. LLM 过滤规则生成 ──
    filters = llm_generate_filters(school_name, page_labels, all_samples)

    # ── 5. 组装 spider_config ──
    config = {
        "school_name": school_name,
        "school_url": school_url,
        "anti_crawl": anti_crawl,
        "source_pages": source_pages,
        "include_keywords": filters.get("include_keywords", DEPT_KW),
        "exclude_keywords": filters.get("exclude_keywords", []),
        "exclude_url_patterns": filters.get("exclude_url_patterns", []),
        "exclude_name_patterns": filters.get("exclude_name_patterns", []),
        "require_chinese_name": True,
    }

    # ── 6. 生成分析报告行 ──
    report_lines = [
        f"# {school_name} — 院系采集分析报告",
        "",
        f"- 学校网址: {school_url}",
        f"- 反爬类型: {anti_crawl} ({fetch_method})",
        f"- 院系入口: {len(entry_pages)} 个页面",
        f"- LLM分析: {llm_result.get('multi_page_note', '')}",
    ]
    if llm_result.get("special_notes"):
        report_lines.append(f"- 特殊说明: {llm_result['special_notes']}")

    report_lines.extend(["", "## 页面分析", ""])
    for sp in source_pages:
        report_lines.append(f"### {sp['label']}: {sp['url']}")
        report_lines.append(f"- 请求方式: {sp.get('fetch_method', '?')}")
        report_lines.append(f"- CSS 选择器: {', '.join(sp.get('css_selectors', [])[:3])}")
        report_lines.append("")

    report_lines.extend([
        "## 过滤规则",
        f"- 包含关键词: {', '.join(config['include_keywords'])}",
        f"- 排除关键词: {', '.join(config['exclude_keywords'][:20])}",
        f"- 排除URL模式: {', '.join(config['exclude_url_patterns'])}",
        f"- 排除名称模式: {', '.join(config.get('exclude_name_patterns', []))}",
        "",
        f"## 过滤器备注",
        f"{filters.get('notes', '使用默认规则')}",
    ])

    config["_report_md"] = "\n".join(report_lines)
    config["_filter_list"] = filter_list
    return config


# ── 入口 ────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", type=str, help="单所学校名称")
    parser.add_argument("--url", type=str, help="单所学校网址")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    args = parser.parse_args()

    llm = get_llm()
    if not llm or not llm.available:
        print("⚠️ 无 LLM, 将使用启发式兜底（质量下降）")

    # 确定目标学校
    if args.name and args.url:
        targets = [{"学校名称": args.name, "学校网址": args.url}]
    else:
        with open(INPUT_CSV, encoding="utf-8-sig") as f:
            all_schools = list(csv.DictReader(f))
        if args.limit > 0:
            all_schools = all_schools[args.start:args.start + args.limit]
        targets = [{"学校名称": s["学校名称"],
                     "学校网址": s.get("学校网址", "").strip()}
                   for s in all_schools if s.get("学校网址", "").strip()]

    print(f"🎓 auto_analysis — 目标: {len(targets)} 所学校")

    for i, s in enumerate(targets):
        name, url = s["学校名称"], s["学校网址"]
        folder = SCHOOLS_DIR / name
        config_file = folder / "spider_config.json"
        report_file = folder / "analysis_report.md"

        # 跳过已有
        if config_file.exists() and folder.exists():
            print(f"\n  [{i+1}/{len(targets)}] {name} — 跳过（已存在）")
            continue

        try:
            config = analyze_school(name, url)

            if "error" in config:
                print(f"  ❌ {config['error']}")
                continue

            # 写入文件夹
            folder.mkdir(parents=True, exist_ok=True)
            report_md = config.pop("_report_md", "")
            filter_list = config.pop("_filter_list", {"drop": [], "keep": []})
            config_file.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
            report_file.write_text(report_md, "utf-8")
            # ⚠️ 过滤清单单独保存 — spider.py 读取执行过滤
            (folder / "filter_list.json").write_text(
                json.dumps(filter_list, ensure_ascii=False, indent=2), "utf-8")

            print(f"  ✅ {name}: config + report + filter_list 已生成")
            print(f"     入口页: {len(config['source_pages'])} | 反爬: {config['anti_crawl']}")
            print(f"     filter_list: drop={len(filter_list.get('drop', []))} keep={len(filter_list.get('keep', []))}")

        except Exception as e:
            print(f"  ❌ {name}: {e}")
            traceback.print_exc()

        time.sleep(0.5)

    print(f"\n{'='*60}\n完成")


if __name__ == "__main__":
    main()
