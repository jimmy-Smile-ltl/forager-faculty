#!/usr/bin/env python3
"""教师采集 - {school} {dept}"""
import csv, json, re, time
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ── 统一网络请求层: 所有spider的网络请求必须走 util/smart_fetch.py ──
# 目的: 代理/瑞数绕过/UA 等配置集中在 util 一处, 修改无需逐个改 spider

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent.parent.parent))
from util import teacher_extract as _util

FETCH_BLOCK = '''import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[4]))
try:
    from util.smart_fetch import smart_fetch as _smart_fetch

    def fetch(url):
        """统一请求层: curl_cffi → 瑞数 → DrissionPage → CDP 四级降级(见 util/smart_fetch.py)"""
        try:
            html, _method = _smart_fetch(url)
            return html if html else None
        except Exception:
            return None
except Exception:
    def fetch(url):
        try:
            import requests as _rq
            r = _rq.get(url, timeout=15, verify=False, headers={"User-Agent": "Mozilla/5.0 Chrome/138"})
            return r.text if r.status_code == 200 and len(r.text) > 500 else None
        except Exception:
            return None
'''

SCHOOL = "{school}"
COLLEGE = "{dept}"
FACULTY_URL = "{faculty_url}"

def fetch(url):
    """三层回退 + JS跳转跟随(最多3跳) — 处理 window.location.href 壳页"""
    html = _fetch_raw(url)
    if html:
        for _ in range(3):
            m = re.search(r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", html[:2000])
            if not m:
                break
            target = urljoin(url, m.group(1).strip().strip("'\""))
            if target == url:
                break
            h2 = _fetch_raw(target)
            if not h2:
                break
            url, html = target, h2
    return html

def _fetch_raw(url):
    # 0) 统一请求层: util/smart_fetch 四级降级(curl_cffi→瑞数→DrissionPage→CDP)。
    #    自带 代理 WAF 拦截(202/412/452)→直连兜底 与 挑战页检测(R9/R12 同步,
    #    2026-08-23: 兰大/南师大 代理出口被瑞数拦截整链白跑——必须走统一层)
    try:
        from util.smart_fetch import smart_fetch as _sf
        html, _m = _sf(url, timeout=20)
        if html:
            return html
    except Exception:
        pass
    try:
        from util.http_client import _build_proxies as _bp, _proxy_fused as _pf
        import curl_cffi.requests as cr
        r = cr.get(url, timeout=15, verify=False, impersonate="chrome124",
                   headers={"User-Agent": "Mozilla/5.0 Chrome/138"},
                   proxies=None if _pf() else _bp())
        if r.status_code == 200 and len(r.text) > 500: return r.text
    except: pass
    try:
        from util.browser_budget import BrowserBudgetExceeded, browser_budget
        import socket as _sock
        _pargs = []
        try:  # 本地代理转发器(local_proxy.py → PROXY_URL)可用才走代理
            with _sock.create_connection(("127.0.0.1", 15819), timeout=2):
                _pargs = ["--proxy-server=http://127.0.0.1:15819"]
        except Exception:
            pass
        with browser_budget():  # 浏览器并发上限内, 超限立即降级(不排队)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled","--no-sandbox","--ignore-certificate-errors"] + _pargs)
                c = b.new_context(ignore_https_errors=True, user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
                try:
                    p = c.new_page(); p.goto(url, timeout=30000, wait_until="load"); time.sleep(3)
                    h = p.content()
                    if len(h) > 500: return h
                finally:  # 用完即关: goto 抛异常也不泄漏浏览器
                    try: c.close()
                    except Exception: pass
                    try: b.close()
                    except Exception: pass
    except BrowserBudgetExceeded:
        pass  # 浏览器并发超限: 降级到下一级
    except: pass
    try:
        from util.http_client import _build_proxies as _bp, _proxy_fused as _pf
        import requests as rq
        r = rq.get(url, timeout=15, verify=False, headers={"User-Agent": "Mozilla/5.0 Chrome/138"},
                   proxies=None if _pf() else _bp())
        if r.status_code == 200 and len(r.text) > 500: return r.text
    except: pass
    return None

NAV_WORDS = _util.NAV_WORDS  # 词表单源: college teacher/util/teacher_extract.py (新增词只改那里)


def detect_json_api(soup, base_url):
    """检测页面是否通过 JSON API 加载教师数据(监听常见API路径)"""
    from util.http_client import _build_proxies as _bp, _proxy_fused as _pf
    api_candidates = [
        "/common/shizi.json",      # 北京大学模式
        "/common/szdw.json",
        "/jsdw/data.json",
        "/api/teacher/list",
    ]
    for api in api_candidates:
        url = urljoin(base_url, api)
        try:
            import requests as _rq
            r = _rq.get(url, verify=False, headers={"User-Agent":"Mozilla/5.0 Chrome/138"}, timeout=8,
                        proxies=None if _pf() else _bp())
            if r.status_code == 200 and len(r.text) > 100:
                try:
                    data = r.json()
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list) and items:
                        # 检查是否含教师字段
                        if any(k in items[0] for k in ("title","name","url")):
                            return items
                except: pass
        except: pass
    return None

def extract_from_json(items):
    """从JSON API数据提取教师"""
    teachers = []
    seen = set()
    for t in items:
        name = t.get("title") or t.get("name") or ""
        name = clean_name(name)
        url = t.get("url") or t.get("href") or ""
        if not (2 <= len(name) <= 4): continue
        if not re.match(r'^[一-鿿]{2,4}$', name): continue
        if name in NAV_WORDS or name in seen: continue
        seen.add(name)
        teachers.append({
            "name": name,
            "title": t.get("viceTitle") or t.get("title") or "",
            "detail_url": url,
        })
    return teachers

def detect_pagination(soup, current_url):
    """检测分页机制, 返回所有页面URL列表"""
    page_urls = [current_url]

    # 1) "共N页" / "x/y页"
    body_text = soup.get_text()
    total_pages = 0
    m = re.search(r'共\s*(\d+)\s*页', body_text)
    if m: total_pages = int(m.group(1))
    m2 = re.search(r'(\d+)\s*/\s*(\d+)\s*页', body_text)
    if m2: total_pages = int(m2.group(2))

    # 2) 收集页码链接
    page_links = {}  # page_num -> url
    next_url = None
    last_url = None
    for a in soup.find_all('a', href=True):
        t = a.get_text(strip=True)
        h = a.get('href','').strip()
        if not h or h.startswith('javascript'): continue
        if re.match(r'^\d+$', t) and 1 <= int(t) <= 200:
            page_links[int(t)] = urljoin(current_url, h)
        elif t in ('下一页','下页'):
            next_url = urljoin(current_url, h)
        elif t in ('尾页','末页'):
            last_url = urljoin(current_url, h)

    if not page_links and not next_url:
        return [current_url]

    # 3) 模式判断
    if next_url:
        # 沿"下一页"链遍历 (带防死循环上限)
        urls = [current_url]
        cur = next_url
        visited = {current_url}
        for _ in range(200):
            if cur in visited: break
            visited.add(cur)
            urls.append(cur)
            # 抓下一页的"下一页"
            html = fetch(cur)
            if not html: break
            s2 = BeautifulSoup(html, "lxml")
            nxt = None
            for a2 in s2.find_all('a', href=True):
                if a2.get_text(strip=True) in ('下一页','下页'):
                    h2 = a2.get('href','').strip()
                    if h2 and not h2.startswith('javascript'):
                        nxt = urljoin(cur, h2)
                        break
            if not nxt: break
            cur = nxt
        return urls

    if page_links:
        max_page = max(page_links.keys())
        # 数字倒序模式 (北大: zzjs.htm → zzjs/5.htm → zzjs/4.htm → ... → zzjs/1.htm)
        # 数字正序模式 (list_1.htm, list_2.htm, ...)
        urls = [current_url]
        for p in range(max_page, 0, -1):
            if p in page_links and page_links[p] not in urls:
                urls.append(page_links[p])
        # 正序尝试
        if len(urls) <= 1:
            urls = [current_url]
            for p in range(2, max_page + 1):
                if p in page_links and page_links[p] not in urls:
                    urls.append(page_links[p])
        return urls

    return [current_url]

# 职称后缀(按长度降序,避免"副教授"被"教授"先截断)
TITLE_SUFFIXES = sorted(["教授","副教授","助理教授","讲师","研究员","副研究员",
    "助理研究员","院士","博导","硕导","工程师","高级工程师","实验师","高级实验师",
    "助教","博士后"], key=len, reverse=True)

# 2026-10-13 机构页栏目行提取层过滤 — 与 evaluate_teachers 的
# DETAIL_HINT/ORG_HINT 同口径(评估层140个「机构页URL占比」FAIL族根修):
# 栏目/列表/登录等ORG特征URL的行不落教师, DETAIL优先(教师详情即使含
# list特征也保留)
_DETAIL_URL_RE = re.compile(
    r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
    r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
    r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
    r"t\d{8}_\d+\.shtml|"
    r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
    r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
    r"/web\\.[^/]+/[a-z]{4,}/?|"
    r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/", re.I)
_ORG_URL_RE = re.compile(
    r"/jgjj/|/szdwjj|/gzzz|/list\.htm|/index\.htm$|"
    r"login|enroll|thesis|questionnaire|interview|oauth|passport|/$", re.I)

def clean_name(text):
    """去掉职称后缀, 提取纯姓名"""
    for suf in TITLE_SUFFIXES:
        text = text.replace(suf, "")
    text = re.sub(r'[\s·•]', '', text.strip())
    m = re.match(r'^([一-鿿]{2,4})', text)
    name = m.group(1) if m else ""
    # 2026-10-13 ABAB重复拼接清洗(赵娟赵娟→赵娟): 提取器/LLM spider把姓名
    # 连写两遍的形态(评估器35个重复拼接FAIL族)。4字ABAB非人名结构 —
    # 中国4字名(欧阳XX/司马XX/复姓)均非ABAB, 切半还原安全
    if len(name) == 4 and name[:2] == name[2:]:
        name = name[:2]
    return name


# ── 教师类别标签 ──
# 顺序重要: URL特征精确匹配优先, 页面文本"博士后/退休"等词常出现在导航菜单, 放最后
CATEGORY_PATTERNS = [
    ("去世", ["yyhn", "memorial", "memoriam", "永远怀念"]),
    ("退休", ["ltx", "txjs", "rxjs", "retire", "荣休", "离退休"]),
    ("名誉", ["myjs", "honorary", "emeritus", "名誉教授"]),
    ("顾问", ["gwjs", "advisor", "顾问教授"]),
    ("兼职", ["jzjs", "parttime", "adjunct", "兼职教师", "客座教授"]),
    ("博士后", ["bsh", "postdoc", "博士后名录", "博士后流动站"]),
    ("在职", ["zzjs", "zzjg", "zjjs", "在职教师", "师资队伍", "教师队伍"]),
]

def detect_category(url, link_text=""):
    """判断教师类别: URL特征优先, 其次父页链接文本(短文本,不含导航干扰)
    注意: 不要用整页body文本! 导航菜单含所有类别词,必然误判"""
    url_lower = url.lower()
    # 1) URL特征(最可靠)
    for cat, features in CATEGORY_PATTERNS:
        for f in features:
            if f in url_lower:
                return cat
    # 2) 父页链接文本(如"在职教师""荣休教师""兼职教师")
    if link_text:
        text = link_text
        for cat, features in CATEGORY_PATTERNS:
            for f in features:
                if len(f) >= 3 and f in text:
                    return cat
    return "在职"  # 默认

# ── 纯文本教师提取 ──
def extract_text_teachers(html, category="在职"):
    """纯文本页: 姓名\n职称：xxx 模式(无链接)"""
    soup = BeautifulSoup(html, "lxml")
    for sel in ["nav",".nav","#nav",".menu","#menu",".header","#header",
                ".footer",".sidebar",".col_menu",".wp_listcolumn"]:
        for el in soup.select(sel): el.decompose()
    text = soup.body.get_text() if soup.body else ""
    teachers = []
    seen = set()
    # 模式1: 姓名\n职称：xxx
    for m in re.finditer(r'([一-鿿]{2,4})\s*\n\s*职称[：:]\s*([^\n]{2,30})', text):
        name = m.group(1); title = m.group(2).strip()
        if name in NAV_WORDS or name in seen: continue
        seen.add(name)
        teachers.append({"name": name, "title": title, "detail_url": "", "category": category})
    return teachers

def extract_teachers(html):
    """提取教师 — 正确的判断逻辑:
    1. 先在HTML结构层面找 <a> 链接型教师(姓名在<a>内 → 有详情URL)
    2. 只有确认姓名不在任何<a>内, 才用纯文本提取
    3. 绝不能用 get_text() 的"姓名\n职称"文本模式来判断纯文本页!
       (get_text()扁平化HTML, 丢弃href信息, 会误判有链接的页面)
    """
    soup = BeautifulSoup(html, "lxml")
    for sel in ["nav",".nav","#nav",".menu","#menu",".header","#header",
                ".footer",".sidebar",".col_menu",".wp_listcolumn",
                ".wp-panel",".wp-navi",".sub-menu",".head-left",".head-right",
                ".site-lang",".col_path",".col_metas",".col_menu_head",".col_menu_con"]:
        for el in soup.select(sel): el.decompose()

    teachers = []
    seen = set()

    # 策略1: <a> 链接型教师(姓名在<a>内, 包括<a><span>姓名</span></a>和<a title="姓名">姓名</a>)
    # 这是第一优先! 大多数"看似纯文本"的页面其实每个姓名都是<a>
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).replace(' ', '').replace('​', '')
        # 也检查 title 属性(有些网站 <a title="王金柱">)
        title_attr = a.get('title', '').strip().replace(' ', '')
        name = clean_name(text)
        if not (2 <= len(name) <= 4) or not re.match(r'^[一-鿿]{2,4}$', name):
            # 文本不合格时用title属性
            name = clean_name(title_attr)
            if not (2 <= len(name) <= 4) or not re.match(r'^[一-鿿]{2,4}$', name):
                continue
        href = a.get("href","").strip()
        if name in NAV_WORDS or name in seen: continue
        # 2026-10-13 机构页栏目行过滤: 栏目/列表URL特征的行不落教师
        # (DETAIL优先, 教师详情即使含list特征也保留)
        if href and not _DETAIL_URL_RE.search(href) and _ORG_URL_RE.search(href):
            continue
        if href.startswith("javascript") or href == "#":
            href = ""
        seen.add(name)
        teachers.append({"name": name, "title": "", "category": detect_category(FACULTY_URL + href),
                         "detail_url": urljoin(FACULTY_URL, href) if href else ""})

    # 策略2: 纯文本姓名(父节点不是<a>) — 只有策略1没找到任何链接型教师时才可能发生
    # 关键改进: 逐个姓名检查父节点, 而不是依赖 get_text() 全文正则
    if not teachers:
        # 找所有可能是姓名的span/div/li, 检查其祖先链是否含<a>
        for span in soup.find_all(["span", "div", "li", "td", "h3", "h4", "p"]):
            text = span.get_text(strip=True).replace(' ', '')
            if not (2 <= len(text) <= 4): continue
            if not re.match(r'^[一-鿿]{2,4}$', text): continue
            if text in NAV_WORDS or text in seen: continue
            # 检查祖先链
            ancestor = span
            in_link = False
            while ancestor is not None:
                if ancestor.name == 'a':
                    in_link = True
                    break
                ancestor = ancestor.parent
            if in_link: continue  # 已在策略1处理
            # 排除容器(含多个子元素, 是文本块不是单个姓名)
            if span.find(['a', 'span', 'div']): continue
            seen.add(text)
            teachers.append({"name": text, "title": "", "category": "在职", "detail_url": ""})

    return teachers

def main():
    html = fetch(FACULTY_URL)
    if not html: print("获取页面失败"); return

    # ── JSON API 检测(优先,一次全量) ──
    soup0 = BeautifulSoup(html, "lxml")
    json_items = detect_json_api(soup0, FACULTY_URL)
    if json_items:
        all_teachers = extract_from_json(json_items)
        print(f"📡 JSON API: {len(all_teachers)} 名教师")
    else:
        # ── 多级目录递归采集 ──
        def crawl(url, depth=0, category="在职", visited=None):
            """递归抓取: 目录页→子分类→教师。depth≤3"""
            if visited is None: visited = set()
            if depth > 3 or url in visited: return []
            visited.add(url)

            page_html = fetch(url)
            if not page_html: return []

            soup = BeautifulSoup(page_html, "lxml")
            page_text = soup.body.get_text() if soup.body else ""

            # 页面类别: 传入的category优先(来自父页链接文本), URL特征次之
            page_category = category
            url_cat = detect_category(url, "")
            if url_cat != "在职" or (category == "在职" and url_cat == "在职"):
                pass
            # 若父级传入的category是默认"在职"且URL有更精确特征, 用URL的
            if category == "在职" and url_cat != "在职":
                page_category = url_cat

            # 本页教师(链接型)
            teachers = extract_teachers(page_html)
            for t in teachers:
                t["category"] = page_category

            # 纯文本教师(无链接)
            if not teachers:
                teachers = extract_text_teachers(page_html, page_category)

            # 子目录链接(教研室/专业/教授分类等) — 携带链接文本作为类别提示
            # 限制: 只进入 list.htm 目录页, 不进入新闻详情页
            sub_cat_keywords = ["教研室", "专业", "教授", "副教授", "讲师", "队伍",
                               "教师", "团队", "系", "中心", "所", "部", "人才",
                               "博士后", "退休", "荣休", "兼职", "名誉", "顾问",
                               "方向", "材料", "哲学", "心理", "经济", "统计", "数学", "物理"]
            sub_links = set()  # (url, link_text)
            if len(teachers) < 5 or depth == 0:
                for a in soup.find_all('a', href=True):
                    t = a.get_text(strip=True); h = a.get('href','').strip()
                    if not h or h.startswith('javascript') or h == '#': continue
                    if t in NAV_WORDS or len(t) > 15: continue
                    # 目录页(list.htm/index)或短路径tab页(如/lzxxcl/)都进
                    # 排除新闻详情(含日期模式2022/0318或长数字ID)
                    is_detail = bool(re.search(r'/\d{4}/\d{4}/|/c\d+', h))
                    if is_detail: continue
                    if not ('list.htm' in h or 'index' in h or len(h.split('/')) <= 3): continue
                    if any(kw in t for kw in sub_cat_keywords):
                        sub_links.add((urljoin(url, h), t))

            all_found = list(teachers)
            for su, link_text in sorted(sub_links, key=lambda x: x[1]):
                if len(all_found) >= 200: break
                # 用链接文本推断子页类别(父页链接文本最可靠)
                sub_cat = detect_category(su, link_text)
                if sub_cat == "在职" and page_category != "在职":
                    sub_cat = page_category  # 继承父类别
                sub_teachers = crawl(su, depth+1, sub_cat, visited)
                all_found.extend(sub_teachers)
                time.sleep(0.5)

            return all_found

        all_teachers = crawl(FACULTY_URL)

    if not all_teachers: print("未提取到教师"); return

    # 去重(按姓名)
    seen = set()
    deduped = []
    for t in all_teachers:
        if t["name"] in seen: continue
        seen.add(t["name"])
        deduped.append(t)
    all_teachers = deduped

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("teachers.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["大学","学院","教师姓名","教师类别","职务职称","所属院系","个人详情URL","师资页URL","采集时间"])
        for t in all_teachers:
            w.writerow([SCHOOL, COLLEGE, t["name"], t.get("category","在职"), t.get("title",""), COLLEGE, t.get("detail_url",""), FACULTY_URL, ts])
    with open("teachers.json", "w", encoding="utf-8") as f:
        json.dump({"school":SCHOOL,"dept":COLLEGE,"total":len(all_teachers),"faculty_url":FACULTY_URL,"teachers":all_teachers,"at":ts}, f, ensure_ascii=False, indent=2)
    with open("test_report.json", "w", encoding="utf-8") as f:
        json.dump({"passed":len(all_teachers)>=3,"score":100 if len(all_teachers)>=3 else 50,"total":len(all_teachers)}, f, ensure_ascii=False)
    print(f"✅ {len(all_teachers)} 名教师 -> teachers.csv + teachers.json")

if __name__ == "__main__": main()
