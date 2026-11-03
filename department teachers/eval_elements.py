#!/usr/bin/env python3
"""评估维度: 页面对照计数与纯文本严格审查 — 2026-08-31 从 evaluate_teachers 拆出。

count_teacher_elements: 统计师资页教师级元素数(详情链接+卡片+名职称对),
作"应有教师数"上界, 返回 (n, clickable);
verify_no_detail_links: 严格纯文本审查 — 全形态扫描(a[href]/a[title]/img[alt]/
onclick/内嵌URL)确认确无详情链接才认可纯文本(2026-08-30 用户要求: 纯文本是
罕见例外, 必须严格审查)。
NAME_RE/FOREIGN_RE 为姓名正则单一来源(evaluate_teachers 也从此导入)。
"""
import re, sys
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

NAME_RE = re.compile(r"^[一-鿿·]{2,4}$")
FOREIGN_RE = re.compile(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'\.\-\s]{2,40}$")

try:
    from templates.spider_template import NAV_WORDS as _NAV
    NAV_WORDS = set(_NAV)
except Exception:
    NAV_WORDS = set()
# ── 评估维度: 页面对照计数 ──
def count_teacher_elements(html, base_url):
    """统计师资页中教师级元素数(链接名+卡片+名+职称对), 作为'应该有多少教师'的上界估计。
    严格版: 链接必须带详情URL特征, 避免把导航链接(医院概况/历任院长)算成教师。
    返回 (总元素数, 可点击链接数) — 纯文本页可点击=0, 用于区分'漏抓'和'合法纯文本'"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    n = 0
    clickable = 0
    seen = set()
    DETAIL_HINT = re.compile(
        r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
        r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
        r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
        r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
        r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
        r"/web\\.[^/]+/[a-z]{4,}/?|"
        r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/|"
        r"/[a-z]{2,}/[a-z]{2,}/[a-z]{3,}\.htm", re.I)
    # 教师级链接(带详情URL特征)
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        # 2026-08-31 北理工形态: 姓名+职位标签粘连("安丰江职位：副教授...")
        if "职位：" in t or "职称：" in t:
            _t2 = re.split(r"职位：|职称：", t, 1)[0].strip()
            if re.fullmatch(r"[一-鿿·]{2,4}", _t2):
                t = _t2
        # 2026-08-30 补: 姓名在 title 属性(清华AI学院形态:
        # <a title="张钹" href=".../info/1121/3552.htm"><div>复杂结构</div></a>),
        # get_text 拿到的是多行混合文本不匹配 NAME_RE → 漏数 → 漏抓误判合法空
        if not (NAME_RE.match(t) and len(t) >= 2 and t not in NAV_WORDS):
            tt = (a.get("title") or "").strip()
            if NAME_RE.match(tt) and len(tt) >= 2 and tt not in NAV_WORDS:
                t = tt
        if not (NAME_RE.match(t) and len(t) >= 2 and t not in NAV_WORDS):
            # 2026-08-31 嵌套子元素姓名形态(清华集成电路 jyry.htm, 与
            # util.teacher_extract 同口径): 姓名在 <a> 子元素内带"姓名："前缀
            for child in a.find_all(["div", "span", "p", "li"]):
                ct = child.get_text(strip=True)
                ct = re.sub(r"^(姓名|教师|导师)\s*[：:]\s*", "", ct)
                if (2 <= len(ct) <= 4 and re.fullmatch(r"[一-鿿·]{2,4}", ct)
                        and ct not in NAV_WORDS):
                    t = ct
                    break
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        full = urljoin(base_url, hr)
        if NAME_RE.match(t) and len(t) >= 2 and t not in NAV_WORDS \
           and DETAIL_HINT.search(full) and hr not in seen:
            seen.add(hr)
            n += 1
            clickable += 1
    # 卡片式: 详情链接的a(名字可能在子元素)
    # 注意: 卡片内 <a> 也满足锚点循环条件(名字在 span/img alt 时可能没进 seen),
    # 必须用 seen 去重, 否则同一教师被锚点循环+卡片循环统计两次, 上界虚高
    for card_sel in ["div.pic-item", "[id^=doctor-item]", "ul#pic_list li"]:
        for card in soup.select(card_sel):
            a = card.find("a", href=True)
            if a and DETAIL_HINT.search(urljoin(base_url, a.get("href", ""))):
                hr = a.get("href", "").strip()
                if hr in seen:
                    continue
                seen.add(hr)
                n += 1
                clickable += 1
    # 纯文本: 名+职称相邻行
    if n < 5:
        # 2026-08-31: 剥离导航/页脚容器(哲学系"近期焦点"误配对教训)
        for _sel in ["nav", ".nav", "#nav", ".menu", "#menu", ".header", "#header",
                     ".footer", "#footer", ".sidebar", ".col_menu", ".wp-panel",
                     ".sub-menu", ".site-lang", ".breadcrumb", ".crumbs", ".path"]:
            for el in soup.select(_sel):
                el.decompose()
        for t in soup.find_all(["script", "style"]):
            t.decompose()
        lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
        title_re = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士|医师")
        contact_re = re.compile(r"邮箱|研究室|电话|Email|信箱")  # 2026-08-31 哲学系形态
        for i, l in enumerate(lines):
            if (NAME_RE.match(l) and i + 1 < len(lines)
                    and (title_re.search(lines[i + 1]) or contact_re.search(lines[i + 1]))):
                n += 1
    return n, clickable


# 模块级详情URL特征正则(与 count_teacher_elements 内联版同口径)
_DETAIL_HINT_RE = re.compile(
    r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
    r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
    r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
    r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
    r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
    r"/web\\.[^/]+/[a-z]{4,}/?|"
    r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/|"
    r"/[a-z]{2,}/[a-z]{2,}/[a-z]{3,}\.htm", re.I)


def teacher_name_set(html, base_url):
    """页面教师姓名集合(跨页去重计数用, 2026-08-31 清华建管系):
    详情URL链接名 + title属性 + 卡片 + 名职对 — 同一教师出现在列表页与
    分类子页(研究所页等子集)时只计一次, 修正小组织核实把子集页重复累加、
    把 12 人真实名单误判成"31/53 元素漏采"的缺陷。
    另按 NAV_SUFFIX_RE 排除栏目尾词(清华建管系导航链接 建管概况/建管新闻
    的 href 恰命中详情URL尾pattern, 会被误当教师姓名)。"""
    from util.teacher_extract import NAV_SUFFIX_RE
    soup = BeautifulSoup(html, "lxml")
    names = set()

    def _add(t):
        t = (t or "").strip()
        if (NAME_RE.match(t) and len(t) >= 2 and t not in NAV_WORDS
                and not NAV_SUFFIX_RE.search(t)):
            names.add(t)

    for a in soup.find_all("a", href=True):
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        if not _DETAIL_HINT_RE.search(urljoin(base_url, hr)):
            continue
        _t = a.get_text(strip=True)
        if "职位：" in _t or "职称：" in _t:  # 2026-08-31 北理工职位粘连
            _t2 = re.split(r"职位：|职称：", _t, 1)[0].strip()
            if re.fullmatch(r"[一-鿿·]{2,4}", _t2):
                _t = _t2
        _add(_t)
        _add(a.get("title") or "")
        # 嵌套子元素姓名形态(与 count_teacher_elements 同口径, 2026-08-31)
        for child in a.find_all(["div", "span", "p", "li"]):
            ct = child.get_text(strip=True)
            ct = re.sub(r"^(姓名|教师|导师)\s*[：:]\s*", "", ct)
            if (2 <= len(ct) <= 4 and re.fullmatch(r"[一-鿿·]{2,4}", ct)
                    and ct not in NAV_WORDS):
                names.add(ct)
                break
    for card_sel in ["div.pic-item", "[id^=doctor-item]", "ul#pic_list li"]:
        for card in soup.select(card_sel):
            a = card.find("a", href=True)
            if a is not None and _DETAIL_HINT_RE.search(urljoin(base_url, a.get("href", ""))):
                _add(a.get_text(strip=True))
                _add(a.get("title") or "")
    if len(names) < 5:  # 纯文本名职对兜底(与 count_teacher_elements 同门槛)
        for _sel in ["nav", ".nav", "#nav", ".menu", "#menu", ".header", "#header",
                     ".footer", "#footer", ".sidebar", ".col_menu", ".wp-panel",
                     ".sub-menu", ".site-lang", ".breadcrumb", ".crumbs", ".path"]:
            for el in soup.select(_sel):
                el.decompose()
        for t in soup.find_all(["script", "style"]):
            t.decompose()
        lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
        title_re = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士|医师")
        contact_re = re.compile(r"邮箱|研究室|电话|Email|信箱")
        for i, l in enumerate(lines):
            if (NAME_RE.match(l) and i + 1 < len(lines)
                    and (title_re.search(lines[i + 1]) or contact_re.search(lines[i + 1]))):
                _add(l)
    return names


def verify_no_detail_links(html, base_url):
    """严格纯文本审查 (2026-08-30 用户要求: 纯文本是罕见例外, 必须严格审查):
    全形态扫描页面确认确实不存在任何教师详情链接, 才可认可"纯文本名单"。
    扫描形态: ①a[href]文本 ②a[title]属性 ③a内img[alt] ④onclick/js跳转
    ⑤文本内嵌详情特征URL(JS渲染名单/JSON数据)。
    返回 (detail_candidates, evidence) — 0 个候选才可认可纯文本。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    n = 0
    DETAIL_HINT = re.compile(
        r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
        r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
        r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
        r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
        r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
        r"/web\\.[^/]+/[a-z]{4,}/?|"
        r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/|"
        r"/[a-z]{2,}/[a-z]{2,}/[a-z]{3,}\.htm", re.I)

    def _nm(t):
        t = (t or "").strip()
        return t if (NAME_RE.match(t) and len(t) >= 2 and t not in NAV_WORDS) else ""

    # ① a[href] 的文本 ② a[title] 属性
    for a in soup.find_all("a", href=True):
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        nm = _nm(a.get_text(strip=True)) or _nm(a.get("title"))
        if nm and DETAIL_HINT.search(urljoin(base_url, hr)) and hr not in seen:
            seen.add(hr)
            n += 1
    # ③ a 内 img[alt] 姓名
    for a in soup.find_all("a", href=True):
        img = a.find("img", alt=True)
        nm = _nm(img["alt"]) if (img and img.get("alt")) else ""
        hr = a.get("href", "").strip()
        if nm and hr.startswith("http") and DETAIL_HINT.search(urljoin(base_url, hr)) and hr not in seen:
            seen.add(hr)
            n += 1
    # ④ onclick/js 跳转
    for el in soup.find_all(onclick=True):
        oc = el.get("onclick", "")
        if re.search(r"location|window\.open|showDetail|teacherInfo|detail|openWin", oc, re.I):
            nm = _nm(el.get_text(strip=True)) or _nm(el.get("title"))
            if nm:
                n += 1
    # ⑤ 文本内嵌详情特征URL(JS渲染名单/JSON数据)
    txt = soup.get_text(" ", strip=True)[:200000]
    for u in set(re.findall(r"https?://[^\s\"'<>]{10,120}", txt)):
        if DETAIL_HINT.search(u):
            n += 1
    return n, f"全形态扫描(5类): {n}个详情链接候选"

