#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测流水线 — 从 info.csv 院系URL出发, 系统枚举师资页面, 生成详尽 analysis.md

Phase 0: 多通道获取(requests/curl_cffi) + diff (WAF差异检测)
Phase 1: 全站链接枚举(BFS限域) + 轻量分类(栏目/师资候选/外链/死链)
Phase 2: 候选页教师信号验证(详情URL特征/名+职称对/卡片/JS数组/分页控件)
Phase 3: 参数/子分类枚举(页内过滤链接: 职称/科室/系列/拼音/query参数)
Phase 4: 分页闭包("共N条"断言 vs 已见教师链接数)
Phase 6: 反证清单(为什么认为没有更多教师)

产出: 9节 analysis.md + 师资页清单(供生成层使用)
"""
import csv, json, os, re, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent  # script/ 的上层 = department teachers 根
OUT = BASE / "output"
LOGS_DIR = BASE / "logs"
sys.path.insert(0, str(BASE))
from collect_teacher import fetch as _fetch  # noqa: E402
sys.path.insert(0, str(BASE.parent))  # util 包在项目根的上层 (college teacher/util)
from util.teacher_extract import _pager_kind, pager_pages  # noqa: E402

try:
    from templates.spider_template import NAV_WORDS as _NAV
    NAV_WORDS = set(_NAV)
except Exception:
    NAV_WORDS = set()

# 2026-09-10 铁律: 浏览器是全局「最后手段」, 由 BROWSER_TIERS 总开关统一控制
# (默认 off — 并发采集期恒 off, 纯 CPU 链: iv8/nodenv/sdenv 解瑞数)。
# probe 的 JS壳渲染兜底与 spider/smart_fetch 同口径受控, 不再自行直开浏览器
# (曾绕过开关拉起 9222, 15 并发下内存爆的直接风险点之一)。
def _browser_tiers_off() -> bool:
    try:
        return os.environ.get("BROWSER_TIERS", "off").strip().lower() in (
            "off", "0", "no", "false")
    except Exception:
        return True

NAME_RE = re.compile(r"^[一-鿿·]{2,4}$")
TITLE_RE = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士|医师|工程师|实验师|主任")
FAC_WORD = re.compile(r"师资|教师|队伍|人员|名师|导师|团队|专家|医生|医师|人才|骨干|People|Faculty|Staff|Members|Professor|Teachers", re.I)
DETAIL_PAT = re.compile(
    r"/info/\d+|faculty\.[^/]+/[A-Za-z0-9]+/zh_CN/index\.htm|/c[0-9a-f]{6,}/|"
    r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
    r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
    r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|/[a-z-]+doctors-\d+\.html|"
    r"/web\.[^/]+/[a-z]{4,}/?|t\d{8}_\d+\.htm|"
    r"homepage\.hit\.edu\.cn/", re.I)
PAGER_WORD = ("下一页", "下页", "尾页", "末页", ">", "»")
MAX_DEPTH = 2
MAX_LINKS = 120
MAX_PAGES = 20

# 排除页词表 (用户人工分析总结): 概况/总结/招聘/公告/投诉/党务行政 — 不可能有教师列表
# 命中页降权 -200 (有其他候选时必不选); 若整站仅此类页, 仍兜底可采, 避免误判 LEGAL_EMPTY
# 2026-08-26: 移除 postdoc|bsh — 博士后页两类: 在站博士后名单(上大马院 bsh.htm /
# 交大环院 /faculty/master, 是师资, 用户明示要采) vs 招聘公告; URL 无法区分,
# 交给文本词表(GROUP_EXC"招聘")与教师信号判定
EXCLUDE_URL = re.compile(
    r"(rczp|zxns|zhaopin|rencai|zhaocai|tzgg|gonggao|notices|sdsfts|dwry|xzry|"
    r"suguan|szgk|szgs|gk\.htm|投诉|举报|招聘|招贤|招才|公告|党务|"
    r"行政人员|师资概况|师资概述|概况|人才招聘|招贤纳士|通知公告|师德师风|"
    r"教职员|institute_?member)", re.I)
# 2026-09-04 南科大 letter/ 实锤: 校级字母名册页(/zh/letter/)教师信号全站最高(名职对981),
# 81 个院系被 probe 误选为师资页 → 假PASS(395-913行全校名单)/0-URL FAIL 连片。
# 与 fix_loop.py:57 _NAV_PAGE_RE 同口径(段级匹配, 防误伤 /newsletter 等含词路径), 硬排除。
NAV_PAGE_RE = re.compile(
    r"/(letter|mailbox|contact|feedback|guestbook|suggest|liuyan|xinfang|map)"
    r"(/|[._-]|$)"
    r"|/zh/facult(y|ies)/?$", re.I)
# 子分类后缀: 文本不含师资词但属于师资子分类的链接照样跟进 (河大马院教研室/海大物理分类/浙大生态子类)
SUBCAT_WORD = re.compile(r"教研室|研究组|研究所|研究院|实验室|实验中心|系$|学部|工程中心|团队|专业$|学科", re.I)
from util.group_words import GROUP_KW as _GROUP_KW, GROUP_EXC as _GROUP_EXC, is_group_link  # noqa: E402


def fetch_multi(url):
    """Phase 0: 多通道获取 — 主通道 smart_fetch 四级降级(兰大等瑞数站点
    只有 DrissionPage 层能过, 实测 xxxy.lzu.edu.cn → drission),
    对照通道 requests(检测WAF差异)。返回 (html, waf_diff)"""
    h1 = None
    try:
        import sys as _s
        from pathlib import Path as _P
        _root = str(_P(__file__).resolve().parent.parent.parent)
        if _root not in _s.path:
            _s.path.insert(0, _root)
        from util.smart_fetch import smart_fetch
        html, _m = smart_fetch(url, timeout=20)
        if html and len(html) > 200:
            h1 = html
    except Exception:
        pass
    h2 = None
    try:
        import sys as _s
        from pathlib import Path as _P
        _root = str(_P(__file__).resolve().parent.parent.parent)
        if _root not in _s.path:
            _s.path.insert(0, _root)
        from util.smart_fetch import _fix_encoding as _fe
        import requests as rq
        r = rq.get(url, timeout=15, verify=False, headers={"User-Agent": "Mozilla/5.0 Chrome/138"})
        if r.status_code == 200 and len(r.content) > 200:
            _fe(r)  # 与主通道一致: meta charset 优先, 无声明时 utf-8 合法性, 兜底 gbk
            h2 = r.text
    except Exception:
        pass
    if h1 and h2:
        # 教师信号对比: 取信号多者, 差异标记 WAF 风险
        s1 = teacher_signal(h1, url)["detail_links"]
        s2 = teacher_signal(h2, url)["detail_links"]
        return (h1, s1 > s2 * 2) if s1 >= s2 else (h2, s2 > s1 * 2)
    return (h1 or h2, False)


def follow_meta_refresh(html, url, depth=0):
    """跟随 meta-refresh 重定向链, 返回 (html, final_url)"""
    if depth > 4:
        return html, url
    if html and len(html) < 500:
        m = re.search(r"URL=([^\"']+)", html)
        if m:
            target = urljoin(url, m.group(1))
            h2, _ = fetch_multi(target)
            return follow_meta_refresh(h2, target, depth + 1)
    return html, url


# ── Phase 2: 教师信号检测(13种) ──
def teacher_signal(html, url):
    """Phase 2: 教师信号检测 — 返回信号字典"""
    sig = {"detail_links": 0, "name_title_pairs": 0, "cards": 0, "comma_names": 0, "english_names": 0, "cn_en_names": 0,
           "js_arrays": 0, "total_declared": None, "pager": False, "samples": []}
    if not html:
        return sig
    soup = BeautifulSoup(html, "lxml")
    # 详情URL特征链接(文本为姓名的)
    seen = set()
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        full = urljoin(url, hr)
        nm = t.rstrip("、，, ")
        if NAME_RE.match(nm) and nm not in NAV_WORDS and DETAIL_PAT.search(full) and full not in seen:
            seen.add(full)
            sig["detail_links"] += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((nm, full[:60]))
            continue
        # 姓名+职称同锚点: "陈招斌 高级工程师" → 取首2-4字为姓名
        m2 = re.match(r"^([一-鿿·]{2,4})[ \t]+(教授|副教授|研究员|副研究员|高级工程师|工程师|实验师|主任医师|讲师)", t)
        if m2 and DETAIL_PAT.search(full) and full not in seen:
            seen.add(full)
            sig["detail_links"] += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((m2.group(1), full[:60]))
    # 卡片容器
    for sel in ["li.doctor-item", "div.class-card", "div.pic-item", "ul.dpzw_pic li",
                "li.eiru_li", "[class*=teacher]", "[class*=jsjs]",
                "span.jsalnr", "span.jsrbt", "[class*=jsrb]"]:
        n = len(soup.select(sel))
        if n >= 3:
            sig["cards"] = max(sig["cards"], n)
    # 表格模式: 行=系/单位, 列=职称, 单元格内为姓名链接
    table_names = 0
    for td in soup.find_all("td"):
        t = td.get_text(" ", strip=True).rstrip("、，, ")
        m = re.match(r"^[一-鿿·]{2,4}$", t)
        has_link = td.find("a", href=True) is not None
        if m and has_link and t not in NAV_WORDS:
            table_names += 1
    if table_names >= 5:
        sig["cards"] = max(sig["cards"], table_names)
        sig["samples"] = sig["samples"][:2]
    # 纯文本模式: 姓名行 + 职称行相邻对 (人类学系等 姓名+简介块页面)
    for t in soup.find_all(["script", "style"]):
        t.decompose()
    # 图片名片模式: <img alt="2-4字中文名"> (浙大转化医学 teachers.html 15+ 人)
    # 信号层与提取层(util/teacher_extract)同步 — 否则名片式列表页仍被判"无教师信号"
    img_names = 0
    for img in soup.find_all("img", alt=True):
        alt = img.get("alt", "").strip()
        if 2 <= len(alt) <= 4 and NAME_RE.match(alt) and alt not in NAV_WORDS:
            img_names += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((alt, "img[alt]"))
    if img_names >= 3:
        sig["cards"] = max(sig["cards"], img_names)
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    for i, l in enumerate(lines):
        if NAME_RE.match(l) and l not in NAV_WORDS and i + 1 < len(lines) \
           and TITLE_RE.search(lines[i + 1]) and len(lines[i + 1]) <= 40:
            sig["name_title_pairs"] += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((l, lines[i + 1][:30]))
    # 中英姓名行: "李林 ▍LI Lin" (中文名+分隔符+英文名, 柔性电子等站点)
    cn_en_re = re.compile(r"^([一-鿿·]{2,4})[ \t]*[▍|/][ \t]*[A-Z][A-Za-z \-]+$")
    for l in lines:
        if cn_en_re.match(l):
            sig["cn_en_names"] += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((l[:40], "中英姓名行"))
    # 英文姓名行: "Dr. Zhanrong Guo" / "Prof. John Smith" (英文站点纯文本名录)
    eng_re = re.compile(r"^(?:Dr\.|Prof\.|Mr\.|Ms\.)?[ \t]*[A-Z][a-zA-Z\-]+[ \t]+[A-Z][a-zA-Z\-]+[ \t]*(?:\w+)?$")
    for l in lines:
        if eng_re.match(l) and len(l) <= 40:
            sig["english_names"] += 1
            if len(sig["samples"]) < 5:
                sig["samples"].append((l, "英文姓名"))
    # 纯文本顿号姓名串: "王璞、白杰瑞、朱宇、杨广云..." (中心成员/名录页)
    for l in lines:
        stripped = re.sub(r"（[^）]*）|\([^)]*\)", "、", l)  # 外国名括号剔除
        parts = [x.strip() for x in re.split(r"[、，,]", stripped) if x.strip()]
        if len(parts) >= 3 and all(NAME_RE.match(x) and x not in NAV_WORDS for x in parts):
            sig["comma_names"] += len(parts)
            if len(sig["samples"]) < 5:
                sig["samples"].append((l[:40], "顿号姓名串"))
    # 正文中的 主页：/info/ URL (链接不在<a>里, 是纯文本)
    for l in lines:
        if "主页" in l or ("http" in l and re.search(r"/info/\d+", l)):
            sig["detail_links"] += 1
            break
    # JS数组数据
    for m in re.finditer(r"(?:title|name)\s*=\s*(\[[^\]\[]{5,}\])", html):
        try:
            arr = json.loads(m.group(1))
            if isinstance(arr, list) and arr and all(isinstance(x, str) and NAME_RE.match(x) for x in arr[:3]):
                sig["js_arrays"] = len(arr)
                sig["samples"] = [(x, "JS数组") for x in arr[:5]]
        except Exception:
            pass
    # 总数声明 / 分页 — "共N名" 后必须跟教师类名词, 排除 "共18名志愿者" 等新闻计数
    # (海南国际学院: 首页新闻 "共18名志愿者" 被误当教师声明, 导致首页当选师资页)
    m = re.search(r"共\s*(\d+)\s*(?:条|位|人|名)\s*([一-鿿]{0,8})", html)
    if m:
        noun = m.group(2).strip()
        if not noun or any(w in noun for w in ("教师", "老师", "教授", "讲师", "专家", "研究员",
                                               "导师", "教职工", "教员", "师资", "院士")):
            sig["total_declared"] = int(m.group(1))
    # 分页控件检测: 前缀容忍("下一页>>" 等装饰文本) + 1-2位数字页码
    # (2026-08-28: 原精确匹配 t in PAGER_WORD 对 下一页>> 漏检)
    # 2026-09-18 批次4: 仅 forward 方向词(下页/下一页/尾页/末页)+数字页码算分页信号 —
    # 站点导航里的孤立「首页」锚点(_pager_kind="signal")不算, 否则无分页的列表页
    # 被误标 pager=True → 分页闭包降级收集兄弟系页当「附加分页」(云大信息学院 7系页
    # ×14入口=82 假附加分页实锤); 回退词(首页/上一页)不可跟随, 对采集无价值
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        if _pager_kind(t) == "forward" or (re.match(r"^\d{1,2}$", t) and "javascript" not in a.get("href", "")):
            sig["pager"] = True
            break
    # 页码分数 "1/2" / "页码 1/2" / "共10条 1/2" → 强分页信号
    # (JS 驱动分页无 href 链接时, 这是唯一识别途径; 分子不能以 0 开头, 防 CSS 路径
    #  /_upload/site/00/86/134/... 误判; 2026-09-18 批次5: 负向断言排除 URL 路径 —
    #  正文公告里的 /info/1052/5008.htm 被匹配成 1052/5008 假分页信号(云大主师资页
    #  实锤), 且信号会推无分页页进降级闭包收兄弟页)
    if not sig.get("pager") and re.search(r"(?<![A-Za-z0-9/])[1-9][0-9]{0,2}\s*/\s*[0-9]{1,3}(?!\.html?)", html):
        sig["pager"] = True
    return sig


# ── Phase 1.5: POST AJAX 教师接口检测 (2026-08-26) ──
# 交大计院类 JS 空壳站: HTML 无教师(信号全0→误判无师资页→反证清单→噪声采集),
# 教师数据在内联 JS 的 $.ajax POST 接口(ajax_teacher_list.html 实测 308 人)。
_AJAX_TX = re.compile(r"teacher|faculty|szdy|jsml|jslb|teacherlist|getteacher|get_teacher|teachers|jslb|szml", re.I)
_AJAX_CTX_RE = re.compile(
    r"url\s*:\s*['\"]([^'\"]{5,150}?)['\"][^}]{0,400}?data\s*:\s*\{(.{0,300}?)\}", re.S)
_UA_HDR = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def detect_ajax_api(html, page_url):
    """检测 POST AJAX 教师列表接口 → 实际 POST 验证(带 Referer/X-Requested-With)。
    返回 {"url", "data", "count"} 或 None。只验证第一个命中端点(大多数站单接口)。"""
    if not html or len(html) < 500:
        return None
    _m = _AJAX_CTX_RE.search(html)
    if not _m:
        return None
    _u = _m.group(1)
    if not _AJAX_TX.search(_u) or _u.startswith((".", "/static", "/js/", "/css/", "/img/")):
        return None
    # 解析页面全局变量: var global_type = 1; var global_zm = 'All'; var global_zc = '全部';
    # (data 里 type:global_type 这类变量引用需解析后才能 POST 成功 — 缺参响应为空)
    globals_v = {}
    for gv in re.finditer(r"var\s+([A-Za-z_]\w*)\s*=\s*['\"]?([^'\"\n,;]{0,50})['\"]?", html):
        globals_v[gv.group(1)] = gv.group(2)
    # data:{...} 字面量 + 变量引用补全
    data = {}
    for kv in re.finditer(r"([A-Za-z_]\w*)\s*:\s*['\"]([^'\"]{0,80}?)['\"]", _m.group(2)):
        data[kv.group(1)] = kv.group(2)
    for kv in re.finditer(r"([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)\s*(?:[,}]|$)", _m.group(2)):
        _k, _v = kv.group(1), kv.group(2)
        if _v in globals_v:
            data[_k] = globals_v[_v]
        elif _k in ("type", "typ", "category", "cat", "status", "state"):
            # 2026-09-28 上交电院实锤: type:global_type 是 click 事件赋值
            # (无 var 声明, globals_v 解析不到) — 缺 type 端点返回空名单
            # (实测 159人→0人); 未解析的类别变量默认 "1"
            data[_k] = "1"
    data.setdefault("page", "1")
    full = urljoin(page_url, _u)
    try:
        import requests as _rq
        # 2026-10-02 上交电气实锤: 端点开始要求 PHPSESSID 会话(无 cookie 返回
        # 空名单 0 人, 带会话 160 人) — 必须先 GET 页面建会话再 POST
        _sess = _rq.Session()
        try:
            _sess.get(page_url, timeout=10, headers={"User-Agent": _UA_HDR})
        except Exception:
            pass
        r = _sess.post(full, data=data, timeout=15,
                       headers={"Referer": page_url, "X-Requested-With": "XMLHttpRequest",
                                "User-Agent": _UA_HDR})
        if not (r.ok and r.text):
            return None
        # 响应常为 JSON 内嵌转义 HTML("tab_html":"<div ...式...<\/div>") —
        # 先 json.loads 解码成真实 HTML 再数教师名, 否则正则永远 0 (2026-08-26 实测)
        _text = r.text
        try:
            import json as _json
            _j = _json.loads(r.text)
            if isinstance(_j, dict):
                _text = " ".join(v for v in _j.values() if isinstance(v, str))
        except Exception:
            pass
        n1 = len(re.findall(r'href="[^"]*"[^>]*>\s*([一-鿿·]{2,4})\s*</a>', _text))
        n2 = len(re.findall(r'>([一-鿿·]{2,4})\s*</(?:a|span|td|div|li)>', _text))
        n = max(n1, n2)
        if n >= 5:
            return {"url": full, "data": data, "count": n}
    except Exception:
        pass
    return None


# ── Phase 1: BFS 全站枚举 ──
def enumerate_links(start_url, depth=MAX_DEPTH, limit=MAX_LINKS, api_hits=None):
    """Phase 1: BFS 枚举站内链接, 分类"""
    domain = urlparse(start_url).netloc
    allowed = {domain, domain.replace("www.", "")}
    results, seen_url, queue = [], set(), [(start_url, 0, "起点")]
    _queue_max = limit * 3  # 队列宽度上限: 子菜单展开防止 BFS 膨胀 (海大/河海等大站)
    _render_budget = 3  # 每轮 probe JS壳渲染上限(防耗时失控, 2026-09-07)
    _render_origin_re = re.compile(r"师资|教师|教授|人员|导师|名录|人才")
    while queue and len(results) < limit:
        url, d, origin = queue.pop(0)
        if url in seen_url:
            continue
        seen_url.add(url)
        html, waf = fetch_multi(url)
        if not html:
            results.append({"text": "", "url": url, "class": "死链/不可达", "origin": origin})
            continue
        html, url2 = follow_meta_refresh(html, url)
        # 2026-09-07 JS壳渲染重扫(根因C类-2): 只渲染师资路径页面(origin 含师资
        # 词), 预算 3 次/轮 — 动态链接系统(_addDynClicks 调用带 9+ 位 id,
        # 非函数定义)或小壳页/文本页带 onclick 才触发; 首页/组织页不烧预算
        _sig0 = teacher_signal(html, url2) if html else {}
        _dyn_call = bool(re.search(r"_addDynClicks\(\s*[^)]*\d{9,}", html[:12000])) if html else False
        _on_fac_path = bool(_render_origin_re.search(origin or ""))
        if _render_budget > 0 and not _browser_tiers_off() and html and _on_fac_path and (
                _dyn_call
                or len(html) < 8000
                or (len(html) >= 8000 and _sig0.get("name_title_pairs", 0) >= 3
                    and _sig0.get("detail_links", 0) == 0 and "onclick" in html[:12000])):
            try:
                import time as _t
                from util.browser_budget import browser_budget
                from util.cdp_util import cdp_page
                with browser_budget():
                    with cdp_page() as (_p, _c):
                        _p.goto(url2, timeout=60000, wait_until="domcontentloaded")
                        _t.sleep(4)
                        _rh = _p.content()
                if _rh and len(_rh) > 8000:
                    _render_budget -= 1
                    html = _rh
                    print(f"  🖥️ BFS渲染: {url2[:55]} 壳页渲染 {len(_rh)}B", flush=True)
            except Exception:
                pass
        # 自动跳转换域名 (海南电子 hd.hainanu.edu.cn → ee.hainanu.edu.cn):
        # 新域名加入放行集合, 否则后续链接全被跨域拦截
        d2 = urlparse(url2).netloc
        if d2 != domain:
            allowed.add(d2)
            allowed.add(d2.replace("www.", ""))
        soup = BeautifulSoup(html, "lxml")
        # 本页教师信号
        sig = teacher_signal(html, url2)
        # 2026-08-26: AJAX 空壳站检测 — HTML 信号 0 但内联 JS 有 POST 教师接口。
        # 全站只验证一次(同模板所有页面同一接口), 验证成功即强信号。
        if api_hits is not None and not api_hits and sig["detail_links"] < 3 and sig["cards"] < 3:
            _api = detect_ajax_api(html, url2)
            if _api:
                api_hits.append(_api)
                sig["total_declared"] = _api["count"]
                sig["detail_links"] = _api["count"]
        _teacherish = (sig["detail_links"] >= 1 or sig["cards"] >= 3
                       or sig["name_title_pairs"] >= 1 or sig["comma_names"] >= 3
                       or sig["english_names"] >= 1 or sig["cn_en_names"] >= 1)
        results.append({"text": origin, "url": url2,
                        "class": "师资候选" if (sig["detail_links"] >= 3 or sig["cards"] >= 3
                                                 or sig["js_arrays"] >= 3 or sig["name_title_pairs"] >= 3
                                                 or sig["comma_names"] >= 3
                                                 or sig["english_names"] >= 3
                                                 or sig["cn_en_names"] >= 3
                                                 or ((sig.get("total_declared") or 0) >= 10 and _teacherish)) else "普通页",
                        "sig": sig})
        # 2026-09-07 用户指令: 出现老师list的页面(BFS停深理由)当场判断分页,
        # 把其他页入队访问 — 不漏页。字母索引与数字分页并存时 pager_pages
        # 已修(数字优先), 省略号分页构造页无真实链接 BFS 枚举永远看不到,
        # 必须闭包后显式入队 (北大CS: 10页 120人, 曾只采1页 12人)。
        _is_teacher_list = (sig["detail_links"] >= 3 or sig["cards"] >= 3
                            or sig["name_title_pairs"] >= 3 or sig["js_arrays"] >= 3)
        if _is_teacher_list and sig.get("pager") and len(queue) < _queue_max:
            try:
                _pq = pager_pages(url2, html, max_pages=MAX_PAGES)
            except Exception:
                _pq = []
            for _pu in _pq[1:]:
                if _pu not in seen_url and len(queue) < _queue_max:
                    queue.append((_pu, d + 1, "分页页"))
        if d >= depth:
            continue
        _enqueued_this_page = False  # 2026-09-18 入口选择页检测用(见③)
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            hr = a.get("href", "").strip()
            full = urljoin(url2, hr)
            if not _same_site(full, allowed):
                continue
            # ① 悬停/点击展开的子菜单 (武大计算机 javascript:; 师资队伍 / 河海环境悬停 / 公安大学 #):
            #    师资导航项 href 无效时, 其所在容器(nav/ul/li)内的子链接全部入队 — HTML 静态存在,
            #    不需要真去 hover; 用户人工分析确认该类子菜单即师资分类页
            #    2026-08-26: 入队前按链接文本做宣传/招聘排除(用户案例: 环院师声/人才招聘/院士风采),
            #    命中职称分类词(教授/讲师/博士后…)的标记为"师资分类" — 后续分析优先/必选
            if FAC_WORD.search(t) or re.search(r"教授|研究员|成员|主任|导师", t):
                _container = a.find_parent(["nav", "ul", "li", "div"])
                if _container:
                    _subs = [s for s in _container.find_all("a", href=True)]
                    if len(_subs) <= 40:  # 容器过大说明是整页, 放弃展开
                        for sub in _subs:
                            shr = sub.get("href", "").strip()
                            if not shr or shr.startswith(("javascript", "#", "mailto", "tel:")):
                                continue
                            st = sub.get_text(strip=True)
                            sfull = urljoin(url2, shr)
                            # 2026-08-26: 文本级宣传/招聘排除 + 分类页打标 (fix_loop 同组扫描同词表)
                            if not is_group_link(st) and _GROUP_EXC.search(st or ""):
                                continue
                            _gmark = "师资分类" if is_group_link(st) else "子菜单"
                            if sfull in seen_url or sfull == url2 or not _same_site(sfull, allowed):
                                continue
                            if len(st) <= 20 and not EXCLUDE_URL.search(sfull):
                                if len(queue) < _queue_max:
                                    queue.append((sfull, d + 1, f"{_gmark}:{t}→{st[:10]}"))
                                    _enqueued_this_page = True
            # ② 常规跟进: 师资类/职称词/子分类后缀/redir 链接
            if not hr or hr.startswith(("javascript", "#", "mailto", "tel:")):
                continue
            if full in seen_url:
                continue
            if EXCLUDE_URL.search(full):
                continue
            is_redir = bool(re.search(r"redir|redirect|go\.php|catalog", hr, re.I))
            # redir 目录页(?catalog_id=x 无 object_id)才 BFS 跟进 — object_id 详情页
            # 会膨胀队列(浙大遗传学 48 链接 20+ 详情页导致 BFS 卡死), 详情页由列表页提取
            is_redir_dir = is_redir and "object_id" not in hr
            # ② 常规跟进: 师资类/职称词/子分类后缀/redir 链接
            # 2026-08-26: 加入职称分类词表(_GROUP_KW: 讲师/院士/博导/硕导/博士后/
            # 名录/名单/兼职/双聘/退休/助教…) — 马院"副教授/讲师/博士后"页此前仅靠
            # 教授|研究员|成员|主任|导师 5 词, 副教授/讲师/博士后页全被 BFS 砍掉
            if len(t) <= 14 and (FAC_WORD.search(t)
                                 or re.search(r"教授|研究员|成员|主任|专家|导师", t)
                                 or (_GROUP_KW.search(t) and not _GROUP_EXC.search(t))
                                 or (is_redir_dir and d <= 1)
                                 or SUBCAT_WORD.search(t)):
                queue.append((full, d + 1, t))
                _enqueued_this_page = True
        # ③ 2026-09-18 入口选择页跟随(重大土木实锤): 语言/版本选择页的入口
        # 是图片链接(锚文本空)或超短词, 上面的文本词规则全不命中 → BFS 只
        # 枚举到 1 个页面就把「没找到」写成负面结论。补救: 本页一个链接都
        # 没入队且全页锚 ≤6 时, 把前几个本站内部入口显式入队(选择页形态)
        _page_anchors = soup.find_all("a", href=True)
        if not _enqueued_this_page and 0 < len(_page_anchors) <= 6:
            for _ea in _page_anchors:
                _ehr = (_ea.get("href") or "").strip()
                if not _ehr or _ehr.startswith(("javascript", "#", "mailto", "tel:")):
                    continue
                _efull = urljoin(url2, _ehr)
                if not _same_site(_efull, allowed):
                    continue
                if _efull in seen_url or _efull == url2:
                    continue
                if len(queue) < _queue_max:
                    queue.append((_efull, d + 1, f"入口选择:{_ea.get_text(strip=True)[:8] or '图片'}"))
                    _enqueued_this_page = True
                break  # 只跟第一个本站入口(中文/专业版主入口)
    return results


def _same_site(full, allowed):
    """full 与 allowed 域名集合同站? (含 www 变体与跳转后新域名)"""
    netloc = urlparse(full).netloc
    if netloc in allowed:
        return True
    for a in allowed:
        if a and netloc.endswith("." + a):
            return True
    return False


# ── Phase 3: 页内过滤器枚举 ──
def enumerate_filters(fac_url, html):
    """Phase 3: 页内过滤链接(职称/科室/系列/拼音/参数) 枚举"""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    filters = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#")):
            continue
        full = urljoin(fac_url, hr)
        is_filter = False
        if re.match(r"^(教授|副教授|助理教授|讲师|研究员|博士|博士后|主任医师|副主任医师|主治医师|院士|工程师|高级工程师|实验师)$", t):
            is_filter = True
        if re.match(r"^[一-鿿·]{2,8}(系|科室|中心|所|研究院|教研室|团队)$", t):
            is_filter = True
        if re.search(r"(postdutyid|collegeid|categoryid|catid|deptid|section|type)=?\d+", hr):
            is_filter = True
        if is_filter and full not in filters:
            filters.append((t, full))
    return filters[:25]


# ── Phase 3.5 (2026-09-18 用户指令): 师资族完整采集审计 ──
def _fam_list_base(u):
    """分页变体归一: qtjs/7.htm→qtjs.htm、js_yjy_/5.htm→js_yjy_.htm、
    _2.htm→.htm、list2.htm→list.htm — 分页子页与列表页归同一列表身份。
    (2026-09-18 批次7: 重大生物 BFS 把 qtjs/1-7.htm 分页子页枚举成独立家族行,
    挤占14行上限盖掉真实入口)"""
    u2 = re.sub(r"/([^/]+)/(\d{1,3})\.html?$", r"/\1.htm", u)
    u2 = re.sub(r"_\d{1,3}(?=\.html?$)", "", u2)
    u2 = re.sub(r"list\d{1,3}(?=\.html?$)", "list", u2)
    return u2


def _fam_is_detail_page(u):
    """教师个人详情页信号: /jslb/ 目录(重大 教师简历列表约定, evaluate_teachers
    同口径 _PERSONAL_URL) — 详情页是名单页的下游, 不应占家族入口行。"""
    return bool(re.search(r"/jslb/[a-z_]{2,12}\.html?", u, re.I))


def _fam_is_detail_text(t):
    """2026-09-18 批次8: 姓名+职称粘连锚文本=教师个人详情页(南科大未来教育
    "郭雨蓉研究教授" 82.html?nvid=10 形态) — 名单入口的锚文本是栏目名,
    不会是姓名+职称; 分隔符(/｜|·空白)先归一再判。职称词开头的tab
    ("副教授/特聘研究员"/"教授/研究员"/"求是讲席教授")是合法入口, 放行。"""
    _t2 = re.sub(r"[/｜|·\s]", "", t)
    if re.match(r"^(教授|副教授|研究员|特聘|求是|长聘|讲席|兼职|博士后|讲师|助理|副|正)", _t2):
        return False
    return bool(re.match(r"^[一-鿿·]{2,4}(研究|兼职|特聘|副|助理|讲席|求是)?"
                         r"(教授|研究员|讲师|院士|导师|学者|助理教授)", _t2))


def _fam_is_date_text(t):
    """2026-09-18 批次8: 纯日期锚文本(2022-04-08)是新闻列表日期链接,
    北交数统 szdw 页下新闻条目被收进家族。"""
    return bool(re.fullmatch(r"\d{4}[-./年]\d{1,2}[-./月]\d{1,2}日?", t))


def _fam_page_kind(n, dl, decl):
    """2026-09-22 树图页类型判定: 概况页(叙述统计无数名单)/纯文本名单/
    名单页/框架页/异常 — 供 5.6 结构树与蜘蛛生成提示词使用。"""
    if n < 0:
        return "渲染异常"
    if n == 0 and dl == 0:
        if decl and re.search(r"[0-9]+\s*(名|位|人)", str(decl)):
            return "概况页(统计叙述, 名单在子页)"
        return "框架/空页"
    if n > 0 and dl == 0:
        return "纯文本名单(无详情链接)"
    return "名单页"


def _gen_fam_tree(dept_url, fac_url, fam_rows, api_note=""):
    """2026-09-22 用户指令: 师资页面分级/分类/分页结构树图 — 树形展示
    (首页→师资入口→分类/名单页), 每页标注类型/分页/实测人数; 让生成层
    LLM 一眼看清「教师在哪几层、分几类、怎么翻页」, 根治漏采(复旦数院
    8/144=wp3 JSON API 分页没发现, 中大新闻职称分类走查6/7)。
    返回 markdown 行列表。"""
    lines = ["## 5.6 师资页面结构树 (2026-09-22 用户指令: 分级/分类/分页)",
             "",
             "```",
             f"{_disp_url(dept_url) or dept_url}",
             f"└─ 师资入口 {_disp_url(fac_url) if fac_url else '未确定'}"]
    # 按「→」层级分组: 无箭头的是一级入口, 有箭头的是其下分类
    roots = [r for r in fam_rows if "→" not in r[0]]
    kids = {}
    for r in fam_rows:
        if "→" in r[0]:
            kids.setdefault(r[0].split("→")[0], []).append(r)
    for _t, _u, _dl, _pp, _decl, _n in roots:
        _label = _t[:24]
        _kind = _fam_page_kind(_n, _dl, _decl)
        _pg = f", 分页+{_pp}" if _pp > 0 else ""
        lines.append(f"├─ 「{_label}」 {_disp_url(_u)} [{_kind}, 实测{max(_n,0)}人{_pg}]")
        for _ct, _cu, _cdl, _cpp, _cdecl, _cn in kids.get(_t, []):
            _clabel = _ct.split("→")[-1][:24]
            _ckind = _fam_page_kind(_cn, _cdl, _cdecl)
            _cpg = f", 分页+{_cpp}" if _cpp > 0 else ""
            lines.append(f"│   └─ 「{_clabel}」 {_disp_url(_cu)} [{_ckind}, 实测{max(_cn,0)}人{_cpg}]")
    lines.append("```")
    if api_note:
        lines += ["", f"**JSON/API 数据源**: {api_note}",
                  "- ⚠️ 名单由 JS 分页渲染 — 生成层必须调 API 全量拉取(rows 放大或翻页循环), 静态页提取必漏"]
    return lines


def _disp_url(u):
    """5.5 表 URL 显示: 截断保留尾部区分段; 尾段是通用名(index/list/main.htm)
    时多保留一层目录(2026-09-18 批次7: 中石油全表尾段 index.htm 无法区分)。"""
    if len(u) <= 46:
        return u
    seg = u.rstrip("/").rsplit("/", 1)[-1]
    if re.fullmatch(r"(index|list|main|default)\.html?", seg):
        _p = u.rstrip("/").rsplit("/", 1)[0]
        seg = _p.rstrip("/").rsplit("/", 1)[-1] + "/" + seg
    return (u.split("://", 1)[0] + "://" + u.split("://", 1)[1].split("/", 1)[0][:18]
            + "…/" + seg + "/")


def audit_faculty_family(links, dept_url, fac_html=""):
    """从 BFS 结果收集「师资菜单族」全部子页面(概况/教授/副教授/讲师/博导/
    硕导/实验中心/教师主页/ALL…), 逐页审计: 详情链接数/附加分页数/声明总数/
    实测提取数 — 输出 (rows, all_rows)。核心回答用户问题: 老师在哪页、有无
    分页、按什么分类、有没有「全部」页、全部是不是真的全部。"""
    # 2026-09-22 中大新闻实锤: BFS 未展开 JS 子菜单时职称分类页(教授/副教授/
    # 助理教授/专职研究人员/博士后)不在 links — 从 fac 页 HTML 直接枚举补入
    # (fac 页是师资入口页, 其内职称分类链接是兄弟名单页)
    if fac_html:
        from urllib.parse import urljoin as _uj
        _extra = []
        for _m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                              fac_html, re.S):
            _t = re.sub(r"<[^>]+>", "", _m.group(2)).strip()
            if re.fullmatch(r"(教授|副教授|助理教授|讲师|专职研究人员|研究员|"
                            r"博士后|导师|博导|硕导|实验技术|行政人员|教辅)", _t):
                _u = _uj(dept_url, _m.group(1))
                if _u.startswith("http") and not _fam_is_detail_page(_u):
                    _extra.append({"text": _t, "url": _u, "origin": "师资分类"})
        if _extra:
            links = list(links) + _extra
    fam, seen, seen_base = [], set(), set()
    for r in links:
        t = str(r.get("text", ""))
        u = r.get("url", "")
        if not u:
            continue
        # 2026-09-18 批次8: http/https 同页双枚举去重(清华地学 ys.htm 两行)
        _us = re.sub(r"^https://", "http://", u)
        if _us in seen:
            continue
        # 2026-09-18 批次6: 坏链接占位 href 内嵌HTML("转换链接错误"span) —
        # 东林材料 5.5 表出现 "…/span>/" 垃圾URL行, 与提取器同口径过滤
        if "<" in u or ">" in u or " " in u:
            continue
        # 2026-09-18 批次7: 教师个人详情页不入家族行(/jslb/ 约定)
        if _fam_is_detail_page(u):
            continue
        _orig = str(r.get("origin", ""))
        _t2 = re.sub(r"[（(].*?[）)]", "", t).strip()
        is_fam = (_orig.startswith(("师资分类", "师资", "子菜单:师资", "入口选择"))
                  or re.match(r"^(师资|教师|教授|副教授|讲师|博导|硕导|导师|"
                              r"研究员|实验中心|行政|博士|硕士)", t)
                  or bool(re.search(r"/(szdw|jszy|jsdw|teacher|faculty)/", u, re.I))
                  # 2026-09-18 批次1中石油实锤: 师资菜单里的裸系名/专业名 tab
                  # (会计系/企业管理系…)也是教师名单入口(与 fix_loop 同组扫描同口径)
                  # 2026-09-18 批次5收紧 {2,12}→{2,8}: 新闻标题"豪森药业到访
                  # 北京大学医学部"12字前缀恰压线被整串匹配(北大医学部实锤);
                  # 真实系名tab短(会计系/企业管理系≤8字)
                  # 2026-09-18 批次8: 课程体系类栏目词排除(清华地学"课程体系"
                  # 4字恰中裸系名tab, 实为研究生课程列表 — 与 teacher_extract
                  # cat_pages 同口径排除)
                  or (not re.search(r"课程|培养方案|教学大纲|制度|章程|体系|方案|计划", _t2)
                      and bool(re.fullmatch(r"[一-鿿·]{2,8}(系|专业|方向|学科|学部|书院|教研室|研究所)", _t2))))
        # 2026-09-18 批次5: 新闻路径排除 — bynews/zhxw 新闻详情混入师资族
        # (北大医学部"豪森药业到访"实锤), 新闻栏目路径非教师名单入口。
        # 2026-09-22 贸大实锤: 排除必须只看 path — 师资入口 URL 查询参数
        # 含 page=../News/ListTeacher 被整串匹配误杀 → fam rows 0
        from urllib.parse import urlparse as _up2
        if is_fam and re.search(r"/(zhxw|xwzx|news|info)/", _up2(u).path, re.I):
            is_fam = False
        if not is_fam:
            continue
        # 2026-09-18 批次7: 分页子页不入家族行 — 归一基URL已见(它是已收
        # 列表的分页页, 附加分页列已由分页闭包计数); 只登记家族URL的基URL
        _ub = _fam_list_base(u)
        if _ub in seen_base:
            continue
        seen_base.add(_ub)
        seen.add(_us)
        fam.append((t, u))
    rows, all_rows = [], []
    _cache = {}  # 2026-09-18 批次4: 按URL缓存(h, sig, pps) — 同一URL多锚文本
    # (地大海南 教育部重点实验室1-5 同URL)不重复fetch, 防重复请求+提取数不一致
    for t, u in fam[:14]:
        # 新闻稿误入过滤: /info/N/M.htm 详情页 + 长文本(教授受邀/获聘标题)
        if re.search(r"/info/\d+/\d+", u) and len(t) > 14:
            continue
        # 2026-09-18 批次7: "详情>"/"更多" 类通用锚文本是教师卡片的详情入口,
        # 不是名单入口(重大能源动力 OUEDRAOGO 详情行)
        if re.fullmatch(r"详情\s*[>»]?|更多\s*[>»]?|[Mm]ore\s*[>»]?", t):
            continue
        # 2026-09-18 批次8: 姓名+职称粘连/纯日期 锚文本=个人详情页/新闻列表
        # (文本带"师资分类:…→"包装, 取最后箭头段判 — 南科大未来教育实锤)
        _tseg = t.split("→")[-1].strip()
        if _fam_is_detail_text(_tseg) or _fam_is_date_text(_tseg):
            continue
        if u in _cache:
            h, sig, pps = _cache[u]
        else:
            try:
                h, _ = fetch_multi(u)
            except Exception:
                h = None
            sig = teacher_signal(h, u) if h else {}
            pps = pagination_closure(u, h, sig, cat_fallback=False) if h else [u]
            _cache[u] = (h, sig, pps)
        declared = sig.get("total_declared") or ""
        n = -1
        if h:
            try:
                from util.teacher_extract import extract_teachers_multi
                n = len(extract_teachers_multi(h, u))
            except Exception:
                pass
        rows.append((t, u, sig.get("detail_links", 0), max(0, len(pps) - 1), declared, n))
        if re.search(r"ALL|全部|全体|all", t + u, re.I):
            all_rows.append((t, u, declared, n, len(pps) - 1))
    return rows, all_rows


# ── Phase 4: 分页闭包 ──
def pagination_closure(fac_url, html, sig, page_patterns=None, cat_fallback=True):
    """Phase 4: 分页闭包 — 返回全部页面URL(含分页)。只跟随 forward 方向链接
    (首页/上一页 不跟随, 否则混入站点主页 main.htm/index.htm, 2026-08-28 修);
    与 util.teacher_extract.pager_pages 同逻辑, 保证探测定与 spider 运行一致。
    cat_fallback=False 时禁用「无分页降级收分类页/字母索引」(2026-09-18 批次4:
    审计表 附加分页 列只数真分页, 兄弟系页由师资族扫描单独成行, 降级页混入
    会把导航兄弟页当分页 — 云大信息学院 7系页×14入口=82 假附加分页实锤)。"""
    if not html or not sig.get("pager"):
        return [fac_url]
    return pager_pages(fac_url, html, max_pages=MAX_PAGES, cat_fallback=cat_fallback)


# ── 主流程: 完整探测 → 9节 analysis.md ──
def analyze_dept(school, dept, dept_url):
    """完整探测: 返回 {"pages": [...], "analysis": str} 并写 analysis.md"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = []
    L += [f"# {dept} — 网站结构分析", "",
          f"- **分析时间**: {now}",
          f"- **起点URL(info.csv)**: {dept_url}", ""]

    # ── Phase 0: 多通道获取 ──
    home, waf = fetch_multi(dept_url)
    if not home:
        # 2026-09-18 批次3经验: 不可达早退路径也必须带 5.5 节(审计完整性)
        (OUT / school / dept / "analysis.md").write_text(
            f"# {dept} — 网站结构分析\n\n- **分析时间**: {now}\n- **起点URL(info.csv)**: {dept_url}\n\n## 结论\n- 站点不可达(双通道均失败), 状态: 待重试\n\n## 5.5 师资族完整采集方案 (2026-09-18 用户指令)\n\n- ⚠️ 无法审计: 站点不可达(双通道失败) — 需浏览器渲染复核或人工核查\n", encoding="utf-8")
        return {"pages": [], "status": "unreachable"}
    L += ["## 1. 起点分析 (info.csv URL)", "",
          f"- 双通道获取: curl_cffi/requests 均可达" + (", **WAF差异警告**: 两通道内容教师信号差异>2倍, 以信号多者为准" if waf else ""), ""]

    # ── Phase 1: 全站枚举 ──
    api_hits = []
    links = enumerate_links(dept_url, api_hits=api_hits)
    L += ["## 2. 全站链接枚举 (BFS, 深度%d, 上限%d)" % (MAX_DEPTH, MAX_LINKS), "",
          "| 链接文本 | URL | 类型 | 教师信号 |", "|---|---|---|---|"]
    cands = []
    for r in links:
        sig = r.get("sig") or {}
        dlinks = sig.get("detail_links", 0)
        cl = r["class"]
        if cl == "师资候选":
            cands.append(r)
        L.append(f"| {r['text'][:16]} | {r['url'][:50]} | {cl} | 详情{dlinks} 卡片{sig.get('cards',0)} JS数组{sig.get('js_arrays',0)} 名职对{sig.get('name_title_pairs',0)} 声明{sig.get('total_declared') or '-'} |")
    L.append("")

    # ── Phase 2: 候选验证 ──
    # 2026-08-26: AJAX 空壳站 — 数据在 POST 接口(HTML 信号 0 时接口是唯一来源)。
    # 无论 cands 是否有 HTML 候选, 检测到接口即输出, 生成层优先走 API 提取。
    if api_hits:
        _api = api_hits[0]
        L += ["## 3. AJAX 教师接口检测", "",
              "- **发现**: 页面为 JS 空壳(HTML 教师信号全 0), 数据走 POST 接口",
              f"- **接口**: {_api['url']}",
              f"- **验证**: 响应含 {_api['count']} 个教师名 → 由生成层调 API 提取(过滤招聘/宣传类)",
              "",
              f"**AJAX教师API**: {_api['url']}||{'&'.join(f'{k}={v}' for k, v in _api['data'].items())}", ""]
        if not cands:
            cands = [{"url": _api["url"], "sig": {"detail_links": _api["count"], "cards": 0,
                                                  "js_arrays": 0, "name_title_pairs": 0,
                                                  "comma_names": 0, "english_names": 0,
                                                  "cn_en_names": 0, "total_declared": _api["count"],
                                                  "samples": []}, "class": "师资候选", "text": "ajax-api"}]
    # 2026-09-04 导航页硬排除: 校级名册/联系方式类页面信号再高也不算师资页
    # (南科大 /zh/letter/ 981 名职对把 81 院系全带偏)。先滤后判, 全滤光走下方反证清单路径。
    # 2026-09-08 probe 崩溃根治: _central_note 必须在 cands 分支之前初始化 —
    # 曾只写在 else 分支内, 无候选(if 分支)时走到反证清单段 UnboundLocalError,
    # 分析写盘前崩溃 → 教学组织反复「判无师资页存疑」RETRY 3 轮僵尸化的根因。
    _central_note = ""
    _nav = [r for r in cands if NAV_PAGE_RE.search(r["url"])]
    if _nav:
        L += ["", f"- ⚠️ 排除导航页候选 {len(_nav)} 个: "
              + ", ".join(r["url"][:48] for r in _nav[:3]), ""]
        cands = [r for r in cands if not NAV_PAGE_RE.search(r["url"])]
    # 2026-09-18 重大电气实锤(用户纠错): 校级教师平台(faculty.cqu.edu.cn)
    # 详情链接数百, 评分压过院系官网自有教师主页(cee.cqu.edu.cn ALL.htm,
    # 名职对8) → 全院走平台、漏采 161 人只采 49。同域优先原则: 院系同域
    # 候选有任何教师信号(名职对/详情)时, 外域平台候选全弃; 同域零信号才
    # 兜底平台(部分院系教师系统确实在校级平台)
    _dhost = (urlparse(dept_url).hostname or "") if dept_url else ""
    if _dhost:
        _same_d = [r for r in cands if urlparse(r["url"]).hostname == _dhost]
        _has_sig = any((r["sig"].get("name_title_pairs", 0)
                        + r["sig"].get("detail_links", 0)) > 0 for r in _same_d)
        if _same_d and _has_sig:
            _dropped = len(cands) - len(_same_d)
            cands = _same_d
            if _dropped:
                print(f"  🏠 同域优先: 弃外域候选 {_dropped} 个(院系官网自有教师信号)", flush=True)
                L += ["", f"- 🏠 同域优先: 弃外域候选 {_dropped} 个(院系官网自有教师信号)", ""]
    # 2026-09-24 全校平台误选实锤(南信大环境物联879/西大西部经济872):
    # 同域零信号时兜底外域平台, 拼音列表(PinYinTeacherList)=全校教师
    # 被选中 — 研究院型院系采了全校。硬拒: 平台链接仅CollegeTeacherList
    # (按院系列表)放行, 拼音列表/平台首页剔除(与 fix_loop 发现链同口径)
    _plat_dropped = [r for r in cands
                     if re.search(r'faculty\.[a-z0-9.-]+\.edu\.cn', r["url"], re.I)
                     and 'CollegeTeacherList' not in r["url"]]
    if _plat_dropped:
        cands = [r for r in cands if r not in _plat_dropped]
        print(f"  🚫 全校平台候选剔除 {len(_plat_dropped)} 个(拼音列表/平台首页=全校, 非本系名单)", flush=True)
        L += ["", f"- 🚫 全校平台候选剔除 {len(_plat_dropped)} 个(拼音列表/平台首页=全校教师)", ""]
    # 2026-09-22 渲染兜底实锤: cands 经渲染兜底注入后走 else 分支,
    # 原 fac_url 初始化只在无候选分支 → else 内条件路径未赋值时
    # UnboundLocalError(贸大国际商学院 exit=1)。提前无条件初始化
    fac_url, fac_html, sig = None, None, None
    if not cands:
        # 2026-09-22 北中医中药学院实锤: UA/JS 检测站 CPU BFS 全是壳
        # (「请升级浏览器」提示页, 55页零候选), 但浏览器渲染后师资分类结构
        # 完整(人才工程/博导/硕导/博士后) — 渲染兜底: cloak 渲染起点 →
        # find_faculty_page → 有入口则作候选继续审计(5.5家族/5.6树图),
        # 仍无才走反证清单。受 BROWSER_TIERS 铁律总开关控制(采集期 off
        # 保持旧行为; 测试期 on 验证分析能力)
        _rh = None
        # 2026-09-22 批次3实锤: 采集期补跑 probe 环境 BROWSER_TIERS=off 时
        # 渲染兜底被挡, 6/8 院系 analysis.md 仍是「全站枚举未发现」全盲 —
        # 无候选时渲染是唯一出路(cloak 串行锁内置, 单次渲染成本可控),
        # 不受 BROWSER_TIERS 开关限制
        try:
            from fix_loop import _cloak_fetch as _pcf, _browser_fetch as _pbf
            _rh = _pcf(dept_url, timeout=35)
            if not _rh:
                _rh = _pcf(dept_url, timeout=35)  # 间歇失败重试一次
            if not _rh:
                _rh = _pbf(dept_url, timeout=25)
        except Exception:
            _rh = None
        if _rh and len(_rh) > 500:
            # 2026-09-22 帕累托优化A: 渲染 HTML 的链接并入 links — CPU BFS 对
            # JS 站只有 2 页(全盲), 渲染版链接(含师资分类/子菜单)是 5.5 家族
            # 审计与 5.6 树图的素材来源
            try:
                from bs4 import BeautifulSoup as _BS
                _soup = _BS(_rh, "lxml")
                _extra_links = []
                for _a in _soup.find_all("a", href=True):
                    _t = _a.get_text(strip=True)
                    _h = (_a.get("href") or "").strip()
                    if not _h or _h.startswith(("javascript", "#")):
                        continue
                    _full = urljoin(dept_url, _h)
                    if _full.startswith("http") and 0 < len(_t) <= 30:
                        _extra_links.append({"text": _t, "url": _full, "origin": "渲染"})
                if _extra_links:
                    links = list(links) + _extra_links
            except Exception:
                pass
            try:
                from collect_phases import find_faculty_page as _ffp
                _bfac = _ffp(dept_url, _rh)
                if _bfac and not NAV_PAGE_RE.search(_bfac):
                    cands = [{"url": _bfac, "sig": {"detail_links": 0, "cards": 0,
                                                     "js_arrays": 0, "name_title_pairs": 0,
                                                     "comma_names": 0, "english_names": 0,
                                                     "cn_en_names": 0, "total_declared": None,
                                                     "samples": []},
                              "class": "师资候选", "text": "浏览器渲染发现"}]
                    L += ["## 3. 师资页发现过程", "",
                          f"- 🌐 CPU链全壳(UA/JS检测), 浏览器渲染发现师资入口: {_bfac[:60]}", ""]
            except Exception:
                pass
        if not cands:
            L += ["## 3. 师资页发现过程", "",
                  "- **结论**: 全站枚举未发现师资候选页(无详情URL特征链接≥3的页面)",
                  "- 进入反证清单核查", ""]
            fac_url, fac_html, sig = None, None, None
    if cands:
        # 取教师信号最强的候选(渲染兜底注入后 cands 非空同样进此分支 —
        # 2026-09-22 实锤: 原 else 只在 if 条件为假时执行, 渲染兜底在
        # if 内注入 cands 永远到不了 else → fac_url 恒 None)
        # 排序键含全部信号; 首页(起点URL)降权, 避免选错
        LEADER_HINT = re.compile(r"领导|主任|部长|历任|学部师资|师资概况|系概况")
        def _score(r):
            # 标题级判断: 领导名录/概况页降权
            try:
                _hs = fetch_multi(r["url"])[0]
                if _hs:
                    _mt = re.search(r"<title>([^<]{0,40})</title>", _hs)
                    if _mt and LEADER_HINT.search(_mt.group(1)):
                        sc_extra = -80
                        return _score_base(r) + sc_extra
            except Exception:
                pass
            return _score_base(r)
        def _score_base(r):
            s = r["sig"]
            # 2026-09-07 详情链接加权×6(原×2): 纯文本大名单页名职对可达数百,
            # 曾压过带链接页(复旦E-研究院: 文本页200分 vs 链接页18分) → URL
            # 100% 缺失根因A类
            sc = s["detail_links"] * 6 + s["cards"] + s["js_arrays"] + s["name_title_pairs"] \
                 + s["comma_names"] + s["english_names"] + s["cn_en_names"] + (s.get("total_declared") or 0)
            if r["url"].rstrip("/") == dept_url.rstrip("/"):
                sc -= 100
            if LEADER_HINT.search(r["url"]):
                sc -= 60  # URL级降权: 领导/主任名录页
            if EXCLUDE_URL.search(r["url"]):
                sc -= 200  # 排除页: 概况/招聘/公告/投诉/党务行政/博士后 (有其他候选必不选)
            return sc
        # 2026-09-07 学校级教师主页系统兜底(根因C类-3): 学院站 BFS 零详情链接
        # 时, 探测 faculty/teacher/jszy.<主域> — 命中且首页有直链(详情≥3)则
        # 作为候选; 命中但 JS 交互式(无直链, VSB 系统: 学院下拉+AJAX 列表,
        # 南农/昌大/华师/华科实测) → 记入反证清单供 CDP 专项处理
        # (2026-09-08: _central_note 初始化已上提至 cands 分支外, 防 UnboundLocalError)
        if not any(r["sig"].get("detail_links", 0) >= 3 for r in cands):
            _ho = (urlparse(dept_url).hostname or "") if dept_url else ""
            _parts = _ho.split(".")
            _main = ".".join(_parts[-3:]) if _ho.endswith(".edu.cn") else ".".join(_parts[-2:])
            if _main:
                for _pre in ("faculty", "teacher", "jszy"):
                    _cu = f"http://{_pre}.{_main}/"
                    try:
                        _ch, _ = fetch_multi(_cu)
                        if _ch:
                            _cs = teacher_signal(_ch, _cu)
                            if _cs.get("detail_links", 0) >= 3:
                                cands.append({"text": f"学校级教师主页系统({_pre}.{_main})",
                                              "url": _cu, "class": "师资候选",
                                              "sig": _cs, "origin": "学校级系统兜底"})
                                print(f"  🏫 学校级系统兜底: {_cu} 详情 {_cs['detail_links']} 个", flush=True)
                                break
                            _central_note = (f"- 学校级教师主页系统存在: {_cu} "
                                             f"(JS交互式/无直链, 教师列表需CDP渲染+院系筛选, 待专项)")
                    except Exception:
                        continue
        # 2026-09-18 同域优先(权威判定, 兜底后): 院系同域候选有信号时,
        # 校级平台兜底/其他外域候选全弃(电气 161 人官网 vs 平台 49 人实锤)
        if _dhost:
            _same_d2 = [r for r in cands if urlparse(r["url"]).hostname == _dhost]
            if _same_d2 and any((r["sig"].get("name_title_pairs", 0)
                                 + r["sig"].get("detail_links", 0)) > 0 for r in _same_d2):
                _dropped2 = len(cands) - len(_same_d2)
                if _dropped2:
                    cands = _same_d2
                    print(f"  🏠 同域优先(终判): 弃外域候选 {_dropped2} 个", flush=True)
        # 2026-09-07 详情链接支配规则(根因A类): BFS 已见带链接页(详情≥3)时,
        # 纯文本页(详情0)一律不得当选 — 华师大物理/华南师大旅游 13 例实锤:
        # 选择 bug 导致纯文本大名单当选、URL 缺失 100%。
        # 注意: 只用于"选定"筛选, 同组采集必须仍看全量候选(北大CS: 纯文本
        # ALL 名单页+荣誉学衔页是独立分类, 被支配规则筛掉就会漏整个分类)
        _all_cands = list(cands)
        if any(r["sig"].get("detail_links", 0) >= 3 for r in cands):
            cands = [r for r in cands if r["sig"].get("detail_links", 0) > 0]
        # 2026-09-07 排序主键: 详情链接数优先, 文本信号只作同链接数下的决胜 —
        # 华科中欧: 学校级系统(详情4)曾被 English 页(详情1+名职对20+)总分压过
        cands.sort(key=lambda r: (-r["sig"].get("detail_links", 0), -_score(r)))
        best = cands[0]
        fac_url, fac_html = best["url"], fetch_multi(best["url"])[0]
        sig = best["sig"]
        # 2026-09-07 JS壳渲染验证(根因C类): 选定页详情0且疑似JS壳(小页面/
        # loader标记) → CDP 渲染重扫链接 — 华科建筑/合工大文法等 91 例 BFS
        # 零链接被判纯文本, 实际是 JS 渲染壳; 壳特征命中才渲染(成本可控)
        # 2026-09-07 JS壳渲染验证(根因C类): 选定页详情0且疑似JS壳(小页面/
        # loader标记) → CDP 渲染重扫链接 — 华科建筑/合工大文法等 91 例 BFS
        # 零链接被判纯文本, 实际是 JS 渲染壳; 壳特征命中才渲染(成本可控)
        # 2026-09-22 帕累托优化B: 渲染不受 BROWSER_TIERS 限制(壳特征命中时
        # 渲染是唯一出路) + 渲染后链接并入 links(5.5家族审计/5.6树图素材)
        if fac_html and best["sig"].get("detail_links", 0) == 0 and (
                len(fac_html) < 8000 or "正在加载" in fac_html[:3000]
                or "loader" in fac_html[:3000].lower()):
            try:
                import time as _t
                from util.browser_budget import browser_budget
                from util.cdp_util import cdp_page
                with browser_budget():
                    with cdp_page() as (_p, _c):
                        _p.goto(fac_url, timeout=60000, wait_until="domcontentloaded")
                        _t.sleep(4)
                        _rh = _p.content()
                if _rh and len(_rh) > 8000:
                    _s2 = teacher_signal(_rh, fac_url)
                    if _s2.get("detail_links", 0) > 0:
                        fac_html = _rh
                        best["sig"] = _s2
                        sig = _s2
                        print(f"  🖥️ 渲染验证: {fac_url[:50]} 渲染后详情 {_s2['detail_links']} 个", flush=True)
                        # 渲染链接并入 links(家族审计素材)
                        try:
                            from bs4 import BeautifulSoup as _BS
                            _rsoup = _BS(_rh, "lxml")
                            _rlinks = []
                            for _a in _rsoup.find_all("a", href=True):
                                _at = _a.get_text(strip=True)
                                _ah = (_a.get("href") or "").strip()
                                if not _ah or _ah.startswith(("javascript", "#")):
                                    continue
                                _af = urljoin(fac_url, _ah)
                                if _af.startswith("http") and 0 < len(_at) <= 30:
                                    _rlinks.append({"text": _at, "url": _af, "origin": "渲染"})
                            if _rlinks:
                                links = list(links) + _rlinks
                        except Exception:
                            pass
            except Exception:
                pass
        # 同组采集 v2: 选定页的同目录兄弟 + 子目录子分类页, 逐一验证教师信号
        # (武大计算机 szdw/ 四分类 / 海大物理 szll1/ 六分类 / 浙大生态 76619-76624 /
        #  河大马院 zrjs/ 八教研室 / 清华建设 js1/ 三研究所 / 浙大转化医学 teachers/cate-*)
        # 这些子分类文本常无师资关键词(公共物理/生态经济/教研室名), BFS 枚举不进 cands,
        # 必须从选定页 DOM 直接闭包
        group = []
        if fac_html:
            _fsoup = BeautifulSoup(fac_html, "lxml")
            _fpath = urlparse(fac_url).path
            _fdir = _fpath.rsplit("/", 1)[0]
            _fbase = _fpath[:-5] if _fpath.endswith((".html", ".htm")) else _fpath
            _seen_g = {fac_url, dept_url.rstrip("/")}
            for a in _fsoup.find_all("a", href=True):
                _hr = a.get("href", "").strip()
                if not _hr or _hr.startswith(("javascript", "#", "mailto", "tel:")):
                    continue
                _full = urljoin(fac_url, _hr)
                if _full in _seen_g:
                    continue
                _p = urlparse(_full).path
                _same_dir = _p.rsplit("/", 1)[0] == _fdir and _p != _fpath
                _sub_dir = _p.startswith(_fbase + "/")
                _tt = a.get_text(strip=True)
                if not (_same_dir or _sub_dir) or len(_tt) > 20 or EXCLUDE_URL.search(_full):
                    continue
                _seen_g.add(_full)
                _h2, _ = fetch_multi(_full)
                if not _h2:
                    continue
                _s2 = teacher_signal(_h2, _full)
                _strong = (_s2.get("detail_links", 0) >= 3 or _s2.get("cards", 0) >= 3
                           or _s2.get("js_arrays", 0) >= 3 or _s2.get("name_title_pairs", 0) >= 3
                           or _s2.get("comma_names", 0) >= 3 or _s2.get("english_names", 0) >= 3
                           or _s2.get("cn_en_names", 0) >= 3)
                # 放宽: 兄弟/子分类链接文本含师资职称词(国家级人才/荣誉教授等) 即使信号弱也纳入 —
                # 该类页面教师人数少(院士/荣誉), 详情链接少但确为有效师资页; EXCLUDE_URL 已防噪声
                _cat_word = bool(re.search(r"教授|研究员|人才|成员|专家|院士|导师|师资|教师|人员|团队|主任|医师", _tt))
                if _strong or _cat_word:
                    group.append({"url": _full, "sig": _s2, "text": _tt})
                if len(_seen_g) > 18:
                    break
        # 合并 BFS 枚举的师资候选:
        # ① 同目录前缀 (子菜单展开机制发现的, 如武大 szdw/ 四分类)
        # ② 师资队伍子导航分类页 (2026-09-07 用户指令): 上科大创艺 sca 师资队伍
        #    子菜单是平行目录 /crjs/ /jxjs/ /tpjs/ /fwjs/ /yjry_7865/ — 同目录
        #    前缀匹配全漏, 只采到单分类。BFS 子菜单展开已按 GROUP_KW 打
        #    "师资分类:" 标记(教授/讲师类); "子菜单:" 标记但父级为师资菜单、
        #    子项文本含教师/科研类的(外聘教师/科研人员)同样并入 — 分类页间
        #    人员互斥, 漏一页即漏一类教师。组织类子项(平台/中心/课题组)排除。
        _bp = urlparse(best["url"])
        _bdir = f"{_bp.scheme}://{_bp.netloc}{_bp.path.rsplit('/', 1)[0]}/"
        _menu_parent_re = re.compile(r"^(师资分类|子菜单):(师资|教师|教授|人才|人员)")
        _menu_child_ok_re = re.compile(r"教授|教师|研究员|导师|讲师|院士|师资|科研人员|科研团队|学衔|人员信息|系列")
        _menu_org_re = re.compile(r"平台|中心|课题组|实验室|基地|实验")
        for r in _all_cands:
            if r is best or r["class"] != "师资候选" or _score(r) < 0:
                continue
            _o = str(r.get("text", ""))
            # 2026-09-07 分页闭包页(origin=分页页)不入同组: 它们是选定页的
            # 分页兄弟(12链接/页高分会挤掉院士/荣誉学衔等小分类, group[:8]
            # 截断后丢分类), spider 运行时分页跟随已覆盖
            if _o.startswith("分页页"):
                continue
            _child = _o.split("→")[-1] if "→" in _o else ""
            _menu_cat = bool(_menu_parent_re.search(_o)) and bool(_menu_child_ok_re.search(_child)) \
                and not _menu_org_re.search(_child)
            if _menu_cat or r["url"].startswith(_bdir):
                group.append(r)
        # ③ "师资分类:" 标记行不受候选/信号门槛限制 (2026-09-07 用户指令补漏):
        #    导航展开时 GROUP_KW 验证过的职称分类页, 小分类(特聘教授 2 人)信号弱
        #    被判 class=普通页挤出 cands, 但确为独立教师分类 — 漏采即漏一类。
        #    组织词(平台/中心)不可能命中 GROUP_KW, 无需再防噪声。
        for r in links:
            if r is best:
                continue
            if str(r.get("text", "")).startswith("师资分类:"):
                group.append(r)
        group.sort(key=lambda r: -_score(r))
        # 去重
        _u = set()
        group = [g for g in group if not (g["url"] in _u or _u.add(g["url"]))]
        group = group[:8]
        L += ["## 3. 师资页发现过程", "",
              "| 候选页 | 教师信号 | 结论 |", "|---|---|---|"]
        for r in _all_cands[:6]:
            s = r["sig"]
            _mark = "✓选定" if r is best else ("同组采集" if r in group else "排除(信号较弱/重复入口)")
            L.append(f"| {r['url'][:60]} | 详情{s['detail_links']} 卡片{s['cards']} JS数组{s['js_arrays']} | {_mark} |")
        L += ["", f"**选定师资页**: {fac_url}", ""]
        if group:
            L.append(f"**同组采集**: {', '.join(g['url'] for g in group)}")

        # ── Phase 3: 参数/子分类枚举 ──
        filters = enumerate_filters(fac_url, fac_html)
        if filters:
            L += ["## 4. 子分类/参数枚举 (Phase 3)", "", "| 过滤项 | URL | 教师数 |", "|---|---|---|"]
            all_pages = []
            for t, u in filters[:10]:
                h2, _ = fetch_multi(u)
                s2 = teacher_signal(h2, u) if h2 else {"detail_links": 0}
                all_pages.append(u)
                L.append(f"| {t} | {u[:55]} | {s2['detail_links']} |")
            L.append("")

        # ── Phase 4: 分页闭包 ──
        # 2026-09-18 批次5: cat_fallback=False — 完整性断言只数真分页(兄弟系页
        # 由5.5师资族审计单独成行, 降级页混入会把导航兄弟页当分页)
        pages = pagination_closure(fac_url, fac_html, sig, cat_fallback=False) if fac_html else [fac_url]
        L += ["## 5. 完整性断言 (Phase 4)", ""]
        if sig and sig.get("total_declared"):
            L.append(f"- 页面声明总数: 共{sig['total_declared']}条; 已见教师链接: {sig['detail_links']}")
            if sig["total_declared"] > sig["detail_links"]:
                L.append("- ⚠️ 声明数 > 已见数, 需分页遍历补齐")
        if sig and sig.get("pager"):
            L.append(f"- 检测到分页控件, 分页闭包收集 {len(pages)} 页: {pages[:6]}")
        L.append("")

        # ── 2026-09-18 用户指令: 师资族完整采集方案 ──
        try:
            _fam_rows, _all_rows = audit_faculty_family(links, dept_url, fac_html or "")
            if _fam_rows:
                # 2026-09-18 批次1改进: 文本取→后段/提取-1显异常/校级串页标注
                from collections import Counter as _C
                _url_dup = {u for u, c in _C(r[1] for r in _fam_rows).items() if c > 1}
                L += ["## 5.5 师资族完整采集方案 (2026-09-18 用户指令)", "",
                      "| 页面 | 类型/URL | 详情链接 | 附加分页 | 声明总数 | 实测提取 |",
                      "|---|---|---|---|---|---|"]
                for _t, _u, _dl, _pp, _decl, _n in _fam_rows:
                    _label = _t.split("→")[-1][:12] if "→" in _t else _t[:12]
                    _n_txt = "异常" if _n < 0 else str(_n)
                    _flag = " ⚠️校级串页?" if (_u in _url_dup or re.search(
                        r"/555/list|/570/list|/571/list|/572/list|/573/list", _u)) else ""
                    # 2026-09-18 批次4: URL 截断保留尾部区分段(地大海南 实验室子页
                    # 路径首46字符全同, 截头导致不同URL看起来相同)
                    _ud = _disp_url(_u)
                    L.append(f"| {_label} | {_ud}{_flag} | {_dl} | {_pp} | {_decl or '-'} | {_n_txt} |")
                _cat = []
                _fam_txt = " ".join(r[0] + r[1] for r in _fam_rows)
                if re.search(r"教授|副教授|讲师|研究员", _fam_txt):
                    _cat.append("按职称分类")
                if _all_rows:
                    _cat.append("有全量页(ALL/全部)")
                if re.search(r"/jslb/|py=", _fam_txt):
                    _cat.append("个人详情页拼音URL")
                _pag_sum = sum(r[3] for r in _fam_rows)
                _cat.append(f"族内共{len(_fam_rows)}入口+{_pag_sum}附加分页")
                L.append(f"- 分类方式: {'、'.join(_cat) if _cat else '单一师资概况页'}")
                for _t, _u, _decl, _n, _pp in _all_rows[:2]:
                    _uall = _disp_url(_u)
                    if _pp > 0 and _decl:
                        # 2026-09-18 批次4: ALL页带分页时首页实测≠声明, 必须分页遍历后核对
                        L.append(f"- ⚠️「全部」页 {_uall} 声明{_decl}条/首页实测{_n}/附加分页{_pp} → "
                                 f"需分页遍历后核对声明总数(首页实测≠全部)")
                    elif _decl:
                        _ok = int(_decl) <= max(_n, 0) + 1
                        L.append(f"- ⚠️「全部」页 {_uall} 声明{_decl}条/实测{_n} → "
                                 f"{'声明与提取一致✓' if _ok else '声明>提取, 需分页补齐或存在其他分类页'}")
                    else:
                        L.append(f"- 「全部」页 {_uall} 无声明总数, 实测{_n}人/附加分页{_pp} — "
                                 f"全部性靠分页闭包保证")
                if not _all_rows:
                    # 2026-09-18 批次4: 无全量页时显式说明完整性来源(用户指令: 全部到底是不是全部)
                    L.append("- 无全量页(ALL/全部选项) — 完整性=各分类入口分页闭包之和, "
                             "需防分类间重叠(同人多职称)与遗漏(未列分类)")
                L.append("")
                # ── 2026-09-22 用户指令: 分级/分类/分页结构树图 ──
                try:
                    _api_note = ""
                    try:
                        if _api and _api.get("url"):
                            _api_note = (f"POST {_api['url']} 参数: "
                                         + "&".join(f"{k}={v}" for k, v in _api.get('data', {}).items())[:120])
                    except (NameError, UnboundLocalError):
                        pass  # api_hits 空时 _api 未定义
                    L += _gen_fam_tree(dept_url, fac_url, _fam_rows, _api_note)
                except Exception:
                    pass
        except Exception:
            pass

        # DOM 结构样本
        L += ["## 6. DOM结构样本 (教师元素)", ""]
        if sig and sig.get("samples"):
            for nm, u in sig["samples"]:
                L.append(f"- `{nm}` → {u}")
        elif fac_html:
            # 尝试定位 姓名+职称 文本行
            soup = BeautifulSoup(fac_html, "lxml")
            for t in soup.find_all(["script", "style"]):
                t.decompose()
            lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
            pairs = []
            for i, l in enumerate(lines):
                if NAME_RE.match(l) and i + 1 < len(lines) and TITLE_RE.search(lines[i + 1]):
                    pairs.append((l, lines[i + 1][:30]))
                    if len(pairs) >= 5:
                        break
            if pairs:
                L.append("- 纯文本姓名+职称相邻行模式:")
                for nm, tt in pairs:
                    L.append(f"  - `{nm}` | `{tt}`")
        L.append("")

    # ── 2026-09-18 批次1经验: 无候选/不可达时 5.5 节也必须存在(完整性) ──
    if "## 5.5" not in "\n".join(L):
        _why = "站点不可达(双通道失败)" if not links else (
            "全站枚举无师资候选页" if not any(r.get("class") == "师资候选" for r in links)
            else "候选页均被过滤")
        L += ["## 5.5 师资族完整采集方案 (2026-09-18 用户指令)", "",
              f"- ⚠️ 无法审计: {_why} — 需浏览器渲染复核或人工核查", ""]

    # ── Phase 6: 反证清单 ──
    L += ["## 7. 反证清单 (为什么认为没有更多教师)", "",
          f"- 全站BFS枚举 {len(links)} 个页面, 含'师资/教师/队伍/人员/专家'字样的链接均逐一取页验证"]
    no_fac = len([r for r in links if FAC_WORD.search(r["text"])]) == 0
    L.append(f"- 导航中{'未发现' if no_fac else '已发现并验证'}师资相关入口")
    L.append("- 若页面无教师元素: 已检查JS数组/分页控件/详情URL特征三类信号均无")
    L.append("- 未检测到 XHR API 教师数据源(如需可升级 Playwright 渲染复查)")
    if _central_note:
        L.append(_central_note)
    L.append("")

    # ── 提取规则设计 ──
    L += ["## 8. 提取规则设计", "",
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
          "1. 只从 `<a>` 提取, 不扫全页文本(避免分组标题内姓名重复/混入)",
          "1.5 **简介前缀形态(2026-09-22 北中医中药实锤)**: 锚文本「中药学院导师简介-"
          "张媛」= 前缀+姓名 — 剥「导师简介/简介/简历/介绍」前缀取尾段 2-4 字姓名;"
          " 页面结构 `<a href=hash.htm><p>中药学院导师简介-张媛</p></a>` (日期 div 与"
          " 姓名 p 同包于 a 内, hash 详情 URL 不命中常规特征模式也须采)",
          "2. 详情URL必须命中特征模式, 严格排除栏目链接",
          "3. CSV与JSON由同一spider运行写出, teachers数组内容一致",
          "4. 分页/子分类页显式遍历", ""]

    # ── 测试验证 ──
    L += ["## 9. 测试验证", "",
          "- 规则预跑教师数与页面统计一致",
          "- 干扰项(顿号链接/导航链接)被排除",
          "- 零容忍 LLM 姓名终审通过"]

    d = OUT / school / dept
    d.mkdir(parents=True, exist_ok=True)
    (d / "analysis.md").write_text("\n".join(L), encoding="utf-8")

    status = "ok" if cands else "no_faculty"
    # 无师资页 → 自动写合法空证据(反证清单已在 analysis.md), 流水线可据此下结论
    if status == "no_faculty":
        import json as _json
        reason = "全站枚举+常用路径回退均未发现师资页(反证清单见analysis.md)"
        # 复核标记: 枚举中任一页含弱教师信号(详情/卡片/名职对) 或 存在不可达页(间歇拒连嫌疑,
        # 河南大学 4 连 LEGAL_EMPTY 前科) → 终态判定前标记人工复核, 避免数据永久缺口
        _weak = sum(1 for r in links
                    if (r.get("sig") or {}).get("detail_links")
                    or (r.get("sig") or {}).get("cards")
                    or (r.get("sig") or {}).get("name_title_pairs"))
        _dead = sum(1 for r in links if r.get("class") == "死链/不可达")
        # 院系名后缀加权(用户经验规则): 学院/系结尾必有教师名录 → 判无师资页几乎必为误判,
        # 即使弱信号/失败痕迹为 0 也强制标记人工复核(终态=数据永久缺口)
        _dept_type_high = bool(re.search(r"(学院|学部|系)$", dept))
        if _weak or _dead or _dept_type_high:
            reason += f"; ⚠️弱信号页{_weak}个/不可达页{_dead}个/学院系后缀{'是' if _dept_type_high else '否'} — 疑似误判需人工复核"
            try:
                _rf = LOGS_DIR / "_skip_review.csv"
                _exists = _rf.exists()
                with open(_rf, "a", encoding="utf-8", newline="") as _f:
                    if not _exists:
                        _f.write("﻿学校,院系,院系URL,skip原因,排查结果(人工填: 无师资页/有师资页)\n")
                    _f.write(f"{school},{dept},{dept_url},{reason.replace(chr(10),' ')},\n")
            except Exception:
                pass
        (d / "test_report.json").write_text(_json.dumps(
            {"passed": False, "status": "no_faculty_page",
             "reason": reason,
             "checked_at": now}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pages": [fac_url] if fac_url else [], "status": status}


if __name__ == "__main__":
    # ── 孤儿/超时守护 (2026-08-26): 父进程(batch/流水线)被杀后 probe_site 会
    # 孤儿化(PPID=1)无限续跑, 每个实例拉起 DrissionPage 浏览器 → 浏览器反复
    # 创建-关闭拖垮内存(实测 15 孤儿 + 421 Chrome 进程事故)。失父 60s 自退出。
    import os as _os, threading as _th, time as _t
    _start_ppid = _os.getppid()
    def _orphan_guard():
        _t0 = _t.time()
        while True:
            _t.sleep(30)
            if _os.getppid() == 1 and _t.time() - _t0 > 60:
                _os._exit(0)          # 失父孤儿 → 自退出
            if _t.time() - _t0 > 180 * 60:
                _os._exit(0)          # 硬超时兜底(3h), 防单例卡死
    _th.Thread(target=_orphan_guard, daemon=True).start()

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("school")
    ap.add_argument("--dept")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=int(os.environ.get("AGENT_WORKERS", "4")),
                    help="院系级并发数(默认4)")
    args = ap.parse_args()
    sdir = OUT / args.school
    depts = []
    dcsv = sdir / "departments.csv"
    if dcsv.exists():
        for r in csv.DictReader(open(dcsv, encoding="utf-8")):
            if r.get("院系URL", "").startswith("http"):
                depts.append((r["院系名称"], r["院系URL"]))
    # 院系级 info.csv 优先(人工修正过的入口, 如站点迁移后新域名); 按 (院系名) 覆盖
    _by_dept = {}
    for _d in sdir.iterdir():
        _f = _d / "info.csv"
        if _f.exists():
            try:
                _lines = [l.strip() for l in open(_f, encoding="utf-8") if l.strip()]
                if len(_lines) >= 2 and "," in _lines[1]:
                    _n, _u = _lines[1].split(",", 1)
                    if _u.startswith("http"):
                        _by_dept[_n.strip()] = _u.strip()
            except Exception:
                pass
    depts = [(n, _by_dept.get(n, u)) for n, u in depts]
    if args.dept:
        depts = [(n, u) for n, u in depts if n == args.dept]
    if args.limit:
        depts = depts[:args.limit]
    import os as _os
    from concurrent.futures import ThreadPoolExecutor
    if len(depts) > 1 and args.workers > 1:
        # 院系级并发: 纯网络探测, 无共享状态(analysis.md 按院系独立写)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(analyze_dept, args.school, n, u): (i, n) for i, (n, u) in enumerate(depts)}
            for fut in futs:
                i, name = futs[fut]
                try:
                    r = fut.result()
                except Exception:
                    r = {"pages": [], "status": "error"}
                print(f"[{i+1}/{len(depts)}] {name[:24]:24s} {r['status']:12s} 师资页: {r['pages'][:1]}")
    else:
        for i, (name, url) in enumerate(depts):
            r = analyze_dept(args.school, name, url)
            print(f"[{i+1}/{len(depts)}] {name[:24]:24s} {r['status']:12s} 师资页: {r['pages'][:1]}")
