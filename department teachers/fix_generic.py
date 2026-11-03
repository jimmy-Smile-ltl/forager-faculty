#!/usr/bin/env python3
"""
通用确定性采集器 — 不变量: teachers.csv 必须是运行 spider.py 的产物

流程:
  1. 确定性探测: 抓院系首页 → 找师资页URL (探测阶段不写任何数据文件)
  2. 生成 spider.py: 把确定性提取逻辑(内联自包含) + 探明的师资页URL 写入院系目录
  3. 运行 spider.py: 由 spider.py 自己抓取并写出 teachers.csv / teachers.json

用法:
  uv run python fix_generic.py --queue logs/FAKE_PURE_TEXT.json
  uv run python fix_generic.py --school 天津医科大学
"""

import argparse, csv, json, re, subprocess, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"
sys.path.insert(0, str(BASE))
from collect_teacher import fetch, find_faculty_page  # noqa: E402

try:
    from templates.spider_template import NAV_WORDS as _NAV_WORDS
    NAV_WORDS = set(_NAV_WORDS)
except Exception:
    NAV_WORDS = set()

NAME_RE = re.compile(r"^([一-龥·]{2,4})$")
TITLE_RE = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士")
SUBDIR_HINT = re.compile(r"系$|教研室|教研部|研究所|教授$|副教授$|讲师$|博导|硕导|导师|名师|团队|队伍", re.I)

# ── 自包含 spider.py 模板(确定性提取逻辑内联, 网络请求统一走 util/smart_fetch) ──
SPIDER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教师采集 spider — {school} {dept} (确定性模板)
不变量: 本脚本运行后写出 teachers.csv / teachers.json"""
import csv, json, re
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

SCHOOL = {school!r}
DEPT = {dept!r}
PAGES = {pages!r}

NAV_WORDS = {nav_words!r}
NAME_RE = re.compile(r"^([一-龥·]{{2,4}})$")
TITLE_RE = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士|医师")
DETAIL_PAT = re.compile(
    r"/info/\\d+|shi\\.buaa|teachershouw|/jszy/|/c[0-9a-f]{{6,}}/|"
    r"/\\d{{4,}}/[a-z0-9]+/index\\.htm|/\\d{{6,}}\\.htm|/[a-z0-9]{{20,}}\\.htm|"
    r"/\\d+/\\d+\\.htm|/[a-z]{{1,3}}/\\d+\\.htm|"
    r"/ykzj/[a-z0-9]+/\\d{{4}}_\\d+_\\d+\\.html|"
    r"/[a-z-]+doctors-\\d+\\.html|expert\\d+-\\d+\\.html|"
    r"/web\\.[^/]+/[a-z]{{4,}}/?", re.I)
EXCLUDE_PAT = re.compile(
    r"index\\.(htm|html|shtml)$|list\\.htm|/xwzx/|/tzgg/|/xygk/|/zzjg/|/news/|"
    r"/login|/download|/map|javascript|mailto:|^#", re.I)
SUBDIR_HINT = re.compile(r"系$|教研室|教研部|研究所|教授$|副教授$|讲师$|博导|硕导|导师|名师|团队|队伍", re.I)

__FETCH__


def clean_name(text):
    t = re.sub(r"[（(].*?[）)]", "", text)
    t = t.replace(" ", "").replace("　", "").replace("\\n", "").replace("\\t", "")
    for suf in ["长聘副教授", "长聘教授", "助理研究员", "助理教授", "副研究员", "副主任医师",
                "副教授", "研究员", "教授", "主任医师", "主治医师", "讲师", "博导", "硕导",
                "博士生导师", "硕士生导师", "院士", "博士", "硕士", "医师"]:
        t = t.replace(suf, "")
    t = t.replace("/", "").replace("、", "").replace("，", "")
    m = re.match(r"^([一-龥·]{{2,4}})", t)
    return m.group(1) if m else ""


def extract_teachers_from_page(html, page_url):
    soup = BeautifulSoup(html, "lxml")
    teachers, seen = [], set()
    # 方式1: 教师级链接(带详情URL特征)
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        full = urljoin(page_url, hr)
        name = clean_name(t)
        if not name or name in NAV_WORDS or len(name) < 2:
            continue
        if EXCLUDE_PAT.search(full):
            continue
        if DETAIL_PAT.search(full) and name not in seen:
            seen.add(name)
            teachers.append([name, "", full])
    # 方式2: 卡片式(容器内=教师卡)
    for sel in ["ul.dpzw_pic li p a", "[class*=teacher] a", "[class*=js] li a",
                "div[class*=list] ul li a", "ul[class*=list] li a",
                "div.pic-item .pic-img a", "[id^=doctor-item] .kszj_pic a",
                "ul#pic_list li a", "li.wow", "li[class*=emcu]"]:
        for card in soup.select(sel):
            name_el = card.select_one(".emcu_li_name_text") or card.select_one(".pic-title01")
            t = name_el.get_text(" ", strip=True) if name_el else card.get_text(" ", strip=True)
            name = clean_name(t)
            a = card if card.name == "a" else card.find("a", href=True)
            hr = a.get("href", "").strip() if a else ""
            full = urljoin(page_url, hr) if hr else ""
            if name and name not in seen and len(name) >= 2 and name not in NAV_WORDS:
                seen.add(name)
                teachers.append([name, "", full if full.startswith("http") else ""])
    # 方式3: 纯文本 — 姓名+职称相邻行
    if len(teachers) < 3:
        for t in soup.find_all(["script", "style", "noscript"]):
            t.decompose()
        lines = [l.strip() for l in soup.get_text("\\n", strip=True).split("\\n") if l.strip()]
        for i, l in enumerate(lines):
            if NAME_RE.match(l) and i + 1 < len(lines) and TITLE_RE.search(lines[i + 1]):
                name = clean_name(l)
                if name and name not in seen and name not in NAV_WORDS and len(name) >= 2:
                    seen.add(name)
                    teachers.append([name, lines[i + 1][:30], ""])
    return teachers


def find_subdir_links(html, page_url):
    soup = BeautifulSoup(html, "lxml")
    subs, seen = [], set()
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#")):
            continue
        full = urljoin(page_url, hr)
        if SUBDIR_HINT.search(t) and len(t) <= 12 and full not in seen and full != page_url:
            seen.add(full)
            subs.append(full)
    return subs[:20]


def main():
    all_teachers, seen = [], set()
    for p in PAGES:
        h = fetch(p)
        if not h:
            continue
        # 子页递归(深度1)
        pages = [p] + find_subdir_links(h, p)[:8]
        for pg in pages:
            h2 = fetch(pg)
            if not h2:
                continue
            for name, title, url in extract_teachers_from_page(h2, pg):
                if name not in seen:
                    seen.add(name)
                    all_teachers.append([name, title, url])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("teachers.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["大学", "学院", "教师姓名", "职务职称", "所属院系", "个人详情URL", "师资页URL", "采集时间"])
        for name, title, url in all_teachers:
            w.writerow([SCHOOL, DEPT, name, title, DEPT, url, PAGES[0], ts])
    json.dump({{"school": SCHOOL, "dept": DEPT, "total": len(all_teachers),
               "teachers": [{{"name": n, "title": t, "detail_url": u}} for n, t, u in all_teachers]}},
              open("teachers.json", "w"), ensure_ascii=False, indent=2)
    print(f"Found {{len(all_teachers)}} teachers")


if __name__ == "__main__":
    main()
'''


# ── 探测: 师资页发现 ──
def find_faculty_pages(school, dept, dept_url=None):
    """探测师资页URL(不写数据文件)。返回页面URL列表或[]"""
    if not dept_url:
        dcsv = OUT / school / "departments.csv"
        if dcsv.exists():
            for r in csv.DictReader(open(dcsv, encoding="utf-8")):
                if r["院系名称"] == dept:
                    dept_url = r.get("院系URL", "").strip()
                    break
    if not dept_url or not dept_url.startswith("http"):
        return []
    home = fetch(dept_url)
    if not home:
        return []
    fac = find_faculty_page(dept_url, home)
    if not fac:
        return []
    pages = [fac]
    fac_html = fetch(fac)
    if fac_html:
        # 子分类链接
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(fac_html, "lxml")
        seen = set()
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            hr = a.get("href", "").strip()
            if not hr or hr.startswith(("javascript", "#")):
                continue
            full = urljoin(fac, hr)
            if SUBDIR_HINT.search(t) and len(t) <= 12 and full not in seen and full != fac:
                seen.add(full)
                pages.append(full)
                if len(pages) >= 6:
                    break
    return pages[:6]


# ── 分析: 生成9节 analysis.md ──
def analyze_page_structure(school, dept, pages, dept_url=""):
    """深度分析网页结构, 生成 analysis.md
    完整链路: info.csv的院系URL首页 → 师资页发现路径 → 师资页DOM结构/分组/陷阱 → 提取规则 → 测试验证"""
    from bs4 import BeautifulSoup
    d = OUT / school / dept.replace("/", "／").replace(":", "：")
    d.mkdir(parents=True, exist_ok=True)
    if not dept_url:
        dcsv = OUT / school / "departments.csv"
        if dcsv.exists():
            for r in csv.DictReader(open(dcsv, encoding="utf-8")):
                if r["院系名称"] == dept:
                    dept_url = r.get("院系URL", "").strip()
                    break
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [f"# {dept} — 网站结构分析", "",
             f"- **分析时间**: {now}",
             f"- **起点URL(info.csv)**: {dept_url or '未知'}",
             f"- **师资页**: {pages[0] if pages else '未知'}", ""]

    # ── 1. 起点: 院系首页分析 (从info.csv的URL出发, 找师资入口) ──
    if dept_url and dept_url.startswith("http"):
        home = fetch(dept_url)
        if home:
            hsoup = BeautifulSoup(home, "lxml")
            lines += ["## 1. 从 info.csv 的 URL 出发 — 首页分析", ""]
            fac_links = []
            for a in hsoup.find_all("a", href=True):
                t = a.get_text(strip=True)
                hr = a.get("href", "").strip()
                if not hr or hr.startswith(("javascript", "#")):
                    continue
                if SUBDIR_HINT.search(t) or re.search(r"师资|教师|队伍|人员|名师|导师", t):
                    full = urljoin(dept_url, hr)
                    if len(t) <= 14 and full not in [x[1] for x in fac_links]:
                        fac_links.append((t, full))
            if fac_links:
                lines.append("首页中与师资相关的链接:")
                for t, u in fac_links[:8]:
                    hit = " ← 师资页入口" if u == (pages[0] if pages else "") else ""
                    lines.append(f"- `{t}` → {u}{hit}")
                lines.append("")
            lines.append(f"**发现路径**: 首页导航 → 师资入口 → {pages[0] if pages else '(未发现, 需人工指定)'}")
            lines.append("")

    # ── 2. 师资页深度分析 ──
    for p in pages[:3]:
        h = fetch(p)
        if not h:
            continue
        soup = BeautifulSoup(h, "lxml")
        lines.append(f"## 2. 师资页 ({p}) 深度分析")
        lines.append("")
        # 2.1 分组/标题
        headers = []
        for el in soup.find_all(["h2", "h3", "h4", "span", "b", "strong", "p", "div"]):
            t = el.get_text(strip=True)
            if re.match(r"^(教授|副教授|助理教授|讲师|研究员|副研究员|高级工程师|工程师|博士|博士后|院士|青年)[^，。]{0,10}$", t) and len(t) <= 14:
                if t not in headers:
                    headers.append(t)
        # 2.2 教师级链接样本
        teacher_links, seen_href = [], set()
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            hr = a.get("href", "").strip()
            if not hr or hr.startswith(("javascript", "#", "mailto")):
                continue
            nm = t.rstrip("、，, ")
            if NAME_RE.match(nm) and nm not in NAV_WORDS and hr not in seen_href:
                seen_href.add(hr)
                teacher_links.append((nm, urljoin(p, hr)))
        lines.append(f"### 2.1 页面组织: {'分组式(职称/科室标题)' if headers else '平铺列表'}")
        if headers:
            lines.append(f"- 分组标题: {headers[:10]}")
        lines.append(f"- 教师级链接约 **{len(teacher_links)}** 个")
        lines.append("")
        # 2.3 详情URL模式
        url_modes = {}
        for nm, u in teacher_links:
            m = re.search(r"https?://[^/]+(/\w+/\d+/\d+\.htm|/[a-zA-Z0-9]+/zh_CN/index\.htm)", u)
            key = m.group(1) if m else u.split("/")[-2][:20] if "/" in u else "其他"
            url_modes.setdefault(key, []).append(nm)
        lines.append("### 2.2 详情URL模式")
        for mode, nms in list(url_modes.items())[:5]:
            lines.append(f"- `{mode}`: {len(nms)}人 (样本: {nms[:3]})")
        # 2.4 陷阱
        traps = []
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            if re.match(r"^[、，,]+$", t):
                traps.append(f"纯顿号/逗号链接 `{t}` (必须跳过)")
                break
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            m = re.match(r"^([一-龥·]{2,4})[、，,]$", t)
            if m:
                traps.append(f"姓名带尾随顿号 `{t}` (提取前需 rstrip('、，, '))")
                break
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            if len(t) >= 2 and t not in NAV_WORDS and not NAME_RE.match(t.rstrip("、，, ")) and ("/info/" in a.get("href", "") or "faculty" in a.get("href", "")):
                traps.append(f"详情URL链接文本非纯姓名 `{t[:15]}` (需导航词/格式双重校验)")
                break
        if not traps:
            traps.append("未发现明显陷阱")
        lines.append("### 2.3 陷阱")
        lines += [f"- {t}" for t in traps]
        # 2.5 分页/子页
        pagination = []
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            if t in ("下一页", "下页", "尾页") or (re.match(r"^\d+$", t) and len(t) <= 2):
                pagination.append((t, urljoin(p, a.get("href", ""))))
        subpages = []
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            if SUBDIR_HINT.search(t) and len(t) <= 12:
                subpages.append((t, urljoin(p, a.get("href", ""))))
        lines.append("### 2.4 分页/子页")
        lines.append(f"- 分页: {pagination[:4] if pagination else '无'}")
        lines.append(f"- 子页: {[(t, u[:50]) for t, u in subpages[:4]] if subpages else '无'}")
        lines.append("")

    # ── 3. 提取规则 ──
    lines += ["## 3. 提取规则设计", "",
              "```python",
              "def extract(html, page_url):",
              "    soup = BeautifulSoup(html, 'lxml')",
              "    teachers, seen = [], set()",
              "    for a in soup.find_all('a', href=True):",
              "        t = a.get_text(strip=True).rstrip('、，, ')  # 去尾随顿号",
              "        hr = a.get('href', '')",
              "        if not DETAIL_RE.search(hr):   # 只认详情URL特征模式",
              "            continue",
              "        if not NAME_RE.match(t):        # 严格2-4字中文名",
              "            continue",
              "        if t in NAV_WORDS:             # 导航词排除",
              "            continue",
              "        if t not in seen:",
              "            seen.add(t)",
              "            teachers.append([t, '', urljoin(page_url, hr)])",
              "    return teachers",
              "```", "",
              "1. 只从 `<a>` 提取, 不扫全页文本(避免分组标题内的姓名重复/混入)",
              "2. 详情URL必须命中特征模式(/info/数字, faculty平台, 拼音目录等), 严格排除栏目链接",
              "3. CSV与JSON由同一spider运行写出, teachers数组内容一致",
              "4. 分页/子分类页显式遍历"]

    (d / "analysis.md").write_text("\n".join(lines), encoding="utf-8")
    return d


# ── 生成+运行: spider.py → teachers.csv/json ──
def gen_and_run_spider(school, dept, pages):
    """生成 spider.py 并运行, 返回教师数。
    流程: 分析网页结构→写analysis.md→生成spider.py→运行→产出CSV+JSON(一致)"""
    from templates.spider_template import NAV_WORDS as _NAV, FETCH_BLOCK as _FETCH
    nav = sorted(_NAV)
    # 1) 先分析网页结构, 写 analysis.md
    analyze_page_structure(school, dept, pages)
    code = SPIDER_TEMPLATE.format(school=school, dept=dept, pages=pages,
                                  nav_words=nav)
    # 2) 网络请求统一注入 util/smart_fetch.py 的请求层
    code = code.replace("__FETCH__", _FETCH)
    d = OUT / school / dept.replace("/", "／").replace(":", "：")
    d.mkdir(parents=True, exist_ok=True)
    (d / "spider.py").write_text(code, encoding="utf-8")
    # 2) 运行 spider.py (teachers.csv + teachers.json 是它的产物)
    r = subprocess.run([sys.executable, "spider.py"], cwd=str(d),
                       capture_output=True, text=True, timeout=90)
    out = (r.stdout or "").strip()
    m = re.search(r"Found (\d+) teachers", out)
    if m:
        return int(m.group(1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", help="FAKE_PURE_TEXT.json 等清单文件")
    ap.add_argument("--school")
    ap.add_argument("--dept")
    args = ap.parse_args()

    jobs = []
    if args.queue:
        qf = Path(args.queue)
        if qf.exists():
            for x in json.load(open(qf, encoding="utf-8")):
                jobs.append((x["school"], x["dept"]))
    elif args.school:
        dcsv = OUT / args.school / "departments.csv"
        if dcsv.exists():
            for r in csv.DictReader(open(dcsv, encoding="utf-8")):
                jobs.append((args.school, r["院系名称"]))
        if args.dept:
            jobs = [(args.school, args.dept)]
    if not jobs:
        print("用法: uv run python fix_generic.py --school 天津医科大学")
        return

    print(f"确定性采集 {len(jobs)} 个院系 (spider.py 运行产生 CSV)")
    ok = 0
    for i, (school, dept) in enumerate(jobs):
        try:
            pages = find_faculty_pages(school, dept)
            if not pages:
                print(f"[{i+1}/{len(jobs)}] ⬜ {school[:8]:8s}/{dept[:20]:20s} 无师资页")
                continue
            n = gen_and_run_spider(school, dept, pages)
            mark = "✅" if n >= 3 else "❌"
            if n >= 3:
                ok += 1
            print(f"[{i+1}/{len(jobs)}] {mark} {school[:8]:8s}/{dept[:20]:20s} {n}人")
        except Exception as e:
            print(f"[{i+1}/{len(jobs)}] 💥 {school}/{dept}: {str(e)[:60]}")
    print(f"\n完成: {ok}/{len(jobs)} 成功")


if __name__ == "__main__":
    main()
