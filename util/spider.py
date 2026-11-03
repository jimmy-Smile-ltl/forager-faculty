#!/usr/bin/env python3
"""
教师信息采集工具方法 — 复用函数, 供 collect_teacher.py 和 batch_collect.py 共用
"""

import csv, json, re, time, ssl
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ── 关键词 ──
FACULTY_KW_LONG = [
    "师资队伍","师资力量","教师名录","教师队伍","导师介绍","师资建设",
    "师资团队","教师风采","教学团队","教研人员","在职教师","专任教师",
    "人才队伍","队伍建设","教师信息","教授名录","教研队伍","教师列表",
    # 2026-09-24 成都理工传艺实锤(用户点名): "科学研究→导师队伍"板块的
    # 导师名单被漏 — 词表缺"导师队伍/导师风采/导师资源/科研团队"
    "导师队伍","导师风采","导师资源","科研团队",
    # 2026-09-16 西南交大材料实锤: "教职工队伍"是最常见师资锚之一(jsdw.htm),
    # 词表漏了它 → 发现链落到坏掉的 articleinfo.jsp 链接
    "教职工队伍",
    # 2026-09-23 人大国关实锤: "教职员工"栏目=师资入口(72专任教师在
    # 教学与研究子栏目), 词表漏了它(教职员工不含教师连续子串, SHORT表
    # 也命中不了) → 发现链判无师资页
    "教职员工",
    # 2026-10-13 机构页师资形态库(用户指令: 研究院/实验室可能有师资, 但
    # 发现链词表只有学院形态词 — 机构页的「研究人员/专家学者/PI/课题组」
    # 栏目漏检导致「无师资页」误判。山大量域发展研究院实锤: 其师资在
    # 「学者观点」署名文章与「拟聘任公示」里, 不在标准名录)
    "研究人员","研究队伍","专家学者","专家团队","研究人员队伍",
    "学术队伍","学术委员会","团队成员","课题组","研究员",
    # 2026-10-14 医院形态(用户人工分析复旦中山医院/重大仁济实锤): 医院师资
    # 在「学科及专家/专家介绍」类面向患者的栏目, 教育站词表全盲 — 补高信号
    # 医院栏目词(医生/医师即临床教师)
    "学科及专家","专家介绍","医生介绍","医师团队","名医风采","专家门诊",
    "医生团队",
]
FACULTY_KW_SHORT = ["师资","教师","人员","导师","教授","人才","队伍","Faculty","People","教研","团队"]

# ── 导航词汇（这些不是人名）──
NAV_WORDS = {
    "首页","主页","学校","学院","概况","简介","领导","机构","设置","师资",
    "教师","导师","博士后","人才","本科生","研究生","招生","就业","新闻",
    "通知","公告","English","中文","管理","服务","下载","更多","详情","查看",
    "搜索","登录","注册","返回","上一页","第一页","下一页","尾页","跳转",
    "科研","党建","国际","交流","服务","社会","校友","建设","活动","培养",
    "教学","科学","研究","学术","课程","专业","图书馆","联系","电话","邮箱",
    "地址","邮编","部门","党委","行政","工会","团委","合作","创新","实践",
    "学科","实验室","中心","平台","基地","项目","基金","出版","期刊","会议",
    "文化","艺术","体育","校庆","发展","规划","评估","统计","数据","信息",
    "风采","体系","系统","网络","安全","法规","制度","政策",
    "新闻网","媒体","聚焦","要闻","综合","时讯","前沿","医教研","视听",
    "系所","国家级","省部级","地区","扫描","移动","客户端","微信","微博",
    "附件","表格","申请","备案","年度","报告","考核","述职","测评","公示",
    "经费","预算","决算","采购","招标","中标","合同","审计","监督","督查",
    "培训","进修","留学","交换","访问","讲座","论坛","研讨会","沙龙",
    # 2026-09-16 华电体育实锤: 师资类栏目词被当教师提取(师资现状/教师结构/
    # 运动成绩 4字链接) — 师资+现状/结构/简介类组合永不可能是人名
    "师资现状","师资结构","教师结构","教师简介","师资简介","师资队伍简介",
}

# ── HTTP ──
def fetch_page(url, use_playwright=False):
    """获取 HTML。curl_cffi → requests → Playwright(可选)"""
    # 1) curl_cffi
    try:
        import curl_cffi.requests as cr
        r = cr.get(url, timeout=15, verify=False, impersonate="chrome124",
                   headers={"User-Agent":"Mozilla/5.0 Chrome/138"})
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except: pass

    # 2) requests with SSL workaround
    try:
        import requests as req
        from requests.adapters import HTTPAdapter
        class _IA(HTTPAdapter):
            def init_poolmanager(self,*a,**kw):
                c=ssl.create_default_context();c.check_hostname=False
                c.verify_mode=ssl.CERT_NONE;c.set_ciphers("DEFAULT:@SECLEVEL=1")
                kw["ssl_context"]=c;return super().init_poolmanager(*a,**kw)
        s=req.Session();s.mount("https://",_IA());s.verify=False
        r=s.get(url,timeout=15,headers={"User-Agent":"Mozilla/5.0 Chrome/138"})
        if r.status_code==200 and len(r.text)>500:
            return r.text
    except: pass

    # 3) Playwright (heavy — only when needed)
    if use_playwright:
        try:
            from browser_budget import BrowserBudgetExceeded, browser_budget
            with browser_budget():  # 并发预算: 满则跳过, 返回 None
                # 2026-08-24 池化: 不再 chromium.launch 新建浏览器 — 连接常驻
                # 9222 CDP (cdp_util), 只租 tab, 用完自动 page.close()
                from cdp_util import cdp_page
                with cdp_page() as (page, ctx):
                    page.goto(url, timeout=30000, wait_until='load')
                    time.sleep(4)
                    html = page.content()
                    if len(html) > 500:
                        return html
        except BrowserBudgetExceeded:
            pass  # 并发超限: 降级
        except Exception:
            pass
    return None

# ── 师资页发现 ──
def _is_news_anchor(t):
    # 2026-09-16 宁大人文民实锤: "X学部召开全体教职工大会" 新闻锚(无新闻词)
    # — 大会/召开/举办 是新闻标题惯用语
    return bool(re.search(r"新闻|通知|公告|动态|要闻|资讯|讣告|大会|召开|举办", t))


def _is_single_article(url):
    """单篇详情页形态(新闻/文章)。注意部分 CMS 的 list 页也走 /info/N/M.htm,
    所以只作'非首选'信号, 不作硬排除。"""
    return bool(re.search(r"/info/\d+/\d+\.(?:htm|html)$", url))


# 2026-09-16 反误选 title 校验(新疆大学马院 fac=本科人才培养页 实锤):
# 选中页 title 含培养/招生/课程类词且无师资类词 = 发现链定位错页。
# 「人才」不进 GOOD(与「人才培养」冲突 — bksrcpy 误通过实锤), 师资类 title
# 靠 师资/教师/教授/导师/学者/团队 足够; 高层次人才页 BAD 不命中自然通过
_TITLE_GOOD = re.compile(r"师资|教师|教授|导师|学者|团队|队伍")
_TITLE_BAD = re.compile(r"培养|招生|课程|新闻|通知|公告|简介|概况|领导|活动|"
                       r"研究生|本科生|研究生教育|本科生教育|招聘|喜报|系统提示|提示|错误|404|"
                       # 2026-09-16 华电体育实锤: 裸"教学"把院名"体育教学部"当
                       # 教学栏目页拒掉(其 title 模板=纯院名) — 教学只拒栏目
                       # 词组合, 院名教学部/教学院合法
                       r"教学活动|教学成果|教学管理|教学工作|教学培养|教学改革|"
                       r"教学研究|教学资源")


def _verify_faculty_title(url):
    """title 校验: 师资页 title 应含师资类词; 含培养/招生/课程类且无师资类
    词 → 误选(人才培养/新闻栏目页)。抓取失败/无 title 不误判(交后续层)。"""
    try:
        import curl_cffi.requests as cr
        r = cr.get(url, impersonate="chrome110", timeout=12, verify=False)
        html = r.text if r.status_code == 200 else ""
    except Exception:
        return True
    if not html:
        return True
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if not m:
        return True
    t = m.group(1)
    # 2026-09-16 北师大数学实锤: "袁荣教授讣告" — 讣告/喜报/招聘类 title
    # 无论是否含教授/教师强词都不是名单页, 硬拒(强词放行规则对它们不适用)
    if re.search(r"讣告|喜报|招聘|奖学金", t):
        return False
    # 2026-09-16 成中医临床实锤: "领导团队-成都中医药大学附属医院" — BAD 领导
    # 命中但 GOOD 的"团队"放行, 领导团队页被当师资页选中。团队/队伍太弱,
    # BAD 命中时须靠 师资/教师/教授/导师/学者 级强词才放行
    if _TITLE_BAD.search(t) and not re.search(r"师资|教师|教授|导师|学者|名录", t):
        return False
    return True



def _safe_urljoin(base, rel):
    """2026-09-22 郑大实锤: 院系 URL 无尾斜杠(www5.zzu.edu.cn/jyx)时
    urljoin 把末段当文件名丢弃 — jyx/szdw1/jyx.htm 变 szdw1/jyx.htm 404。
    base 末段非文件形态(无 .htm/.jsp 等后缀)时补尾斜杠再 join。"""
    if base and rel and not base.endswith("/"):
        _tail = base.rstrip("/").rsplit("/", 1)[-1]
        if not re.search(r"\.(html?|jsp|php|aspx?)$", _tail):
            base = base + "/"
    return urljoin(base, rel)


def find_faculty_page(dept_url, html=None):
    """从院系首页找师资页URL。
    2026-09-15 能力强化(上游判责结论落地: 316/335 条'错配'实为下游定位缺陷):
    1) 词变体长词优先; 2) 锚文本含新闻/通知类词跳过(防选中新闻页);
    3) 新增后缀兜底: href 含 js/jsml/szdw/rymd/szll 等师资页惯用路径时,
       即使锚文本弱(书院概况页的 ../js.htm 案例)也能命中;
    4) 单篇 /info/N/M.htm 形态仅在锚文本强师资信号时接受(新闻页误选 72 条)。"""
    if not html:
        html = fetch_page(dept_url)
    if not html: return None

    soup = BeautifulSoup(html, "lxml")

    # 0.5 入口选择页跟随 (2026-09-18 重大土木实锤: 首页=语言/版本选择页 —
    # 只有 2 个图片链接(无锚文本): 中文→xzwb/sy.htm / English→ecivil 外域;
    # 医院类站点同款=专业版/科研版/普通版选择。检测: 锚极少(≤6)且无任何
    # 师资词; 跟随第一个本站内部入口(中文/专业版主入口)重新发现(递归一层,
    # 真实首页锚多不会再触发本分支, 无环))
    _sel_anchors = list(soup.find_all("a", href=True)) + list(soup.find_all("area", href=True))
    if 0 < len(_sel_anchors) <= 6:
        _has_fac_kw = any(
            any(_kw in a.get_text(strip=True) for _kw in FACULTY_KW_LONG + FACULTY_KW_SHORT)
            for a in _sel_anchors)
        if not _has_fac_kw:
            _entry = None
            for _a in _sel_anchors:
                _h = (_a.get("href") or "").strip()
                if not _h or _h.startswith(("javascript", "#", "mailto:", "tel:")):
                    continue
                if _h.startswith("http"):
                    # 2026-09-23 浙大农生实锤: 语言选择页(首页3个空文本图片链接)
                    # 中文站链接是同域绝对URL(cab.zju.edu.cn/chinese), 被当外域
                    # 跳过 → 入口跟随失效 → 双查无师资。同域绝对URL放行
                    from urllib.parse import urlparse as _up
                    if _up(_h).netloc == _up(dept_url).netloc:
                        _entry = _h
                        break
                    continue  # 真外域(English/科研版外站)跳过, 优先本站主入口
                _entry = _safe_urljoin(dept_url, _h)
                break
            if _entry and _entry != dept_url:
                # 入口页可能同样在瑞数墙后(重大同款) — 用 smart_fetch 全链
                # (curl_cffi→系统curl→iv8→nodenv)而非裸 curl_cffi
                try:
                    from util.smart_fetch import smart_fetch as _sf
                    _sub_html, _ = _sf(_entry, timeout=40)
                except Exception:
                    _sub_html = None
                if not _sub_html:
                    _sub_html = fetch_page(_entry)  # 兜底裸请求
                if _sub_html and len(_sub_html) > 500:
                    _sub_fac = find_faculty_page(_entry, _sub_html)
                    if _sub_fac:
                        return _sub_fac
                    return None  # 真实首页也找不到 → 让上层按无师资页处理


    def scan(pred, allow_article):
        best_article = None
        cands = []
        # 2026-09-28 浙大化工实锤: 语言/版本选择页是 <map><area> 图片热区
        # (coords+href, 无文字) — find_all("a") 扫不到, 发现链全盲。
        # area 无文本, t 取 alt/title/空(靠 href 后缀兜底词表)
        for a in list(soup.find_all("a", href=True)) + list(soup.find_all("area", href=True)):
            t = a.get_text(strip=True); h = a.get("href", "").strip()
            if not t and a.name == "area":
                t = (a.get("alt") or a.get("title") or "").strip()
            if not h or h.startswith("javascript") or h == "#":
                continue
            # 2026-09-16 东北大学/北科实锤: 教师邮箱(邮件系统)、.doc/.pdf
            # 文件下载链接被当师资页 — 系统/文件类链接硬拒
            if re.search(r"^https?://mail\.|mail\.[a-z0-9.-]+\.edu|"
                         r"\.(docx?|pdf|xlsx?|zip|rar)(\?|$)", h, re.I):
                continue
            if not pred(t, h):
                continue
            if _is_news_anchor(t):
                continue
            # 2026-09-23 全校教师平台误选实锤×3(西电网安/西南交大土木/
            # 南信大马院879人): 院系首页「教师主页/教师查询」链接指向
            # 全校平台首页/拼音列表(faculty.xxx.edu.cn 根/拼音PinYinList)
            # = 全校教师, 非本系名单。按院系列表(CollegeTeacherList&id=)
            # 才是正确入口 — 平台首页/拼音列表硬拒, 院系列表放行
            if (re.search(r'faculty\.[a-z0-9.-]+\.edu\.cn', h, re.I)
                    and not re.search(r'CollegeTeacherList', h)):
                continue
            cand = _safe_urljoin(dept_url, h)
            if _is_single_article(cand):
                if allow_article and best_article is None:
                    best_article = cand  # 无更好选择时的后备
                continue
            cands.append(cand)
        # 2026-09-16 反误选: 候选逐个 title 校验(人才培养/新闻页误选实锤),
        # 返回第一个 title 通过者; 全失败退回 article 后备
        for c in cands:
            if _verify_faculty_title(c):
                return c
        return best_article

    # 1) 长词(强信号) — 单篇形态可接受(长词几乎必是师资页)
    for kw in FACULTY_KW_LONG:
        r = scan(lambda t, h, kw=kw: kw in t, allow_article=True)
        if r: return r
    # 2) 短词(中等信号) — 单篇形态不接受
    # 2026-09-16 西南交大生科实锤: "人才培养" 含短词"人才"被当师资页选中
    # (rcpy.htm 无 title, title 校验拦不住); 青海新农村"人才培训"同理 —
    # 培养/招聘/引进/计划/招生/培训/领导/奖学金/资格类词是导航栏目
    # (南开国教"国际中文教师奖学金"、湖师大教院"教师资格证考试"实锤)
    for kw in FACULTY_KW_SHORT:
        r = scan(lambda t, h, kw=kw: kw in t and len(t) <= 15
                 and not re.search(r"培养|招聘|引进|计划|招生|培训|领导|奖学金|资格|课程|邮箱|邮件", t),
                 allow_article=False)
        if r: return r
    # 3) 后缀兜底(弱锚文本) — href 惯用师资路径
    def suffix_pred(t, h):
        if len(t) > 18:
            return False
        return bool(re.search(r"(?:^|/)(js|jsml|jsdw|szdw|rymd|szll|teacher|teachers)(?:/|\.|$)",
                              h, re.I))
    r = scan(suffix_pred, allow_article=False)
    if r: return r
    # 4) JS 菜单站受限 URL 探测(2026-09-16 新疆大学马院实锤: 首页「师资团队」
    # href=# JS 展开菜单, 静态 HTML 拿不到真师资页 sztd.htm — 探测常见师资
    # 目录形态(≤8 个, 404 风暴可控), title 校验命中即返回)
    _js_menu = any(a.get_text(strip=True).strip() in ("师资团队", "师资队伍", "师资力量", "教师队伍")
                   and (a.get("href", "") or "").strip() in ("#", "", "javascript:;")
                   for a in soup.find_all("a", href=True))
    if _js_menu:
        _base = dept_url.rstrip("/")
        _root = re.sub(r"/[^/]+\.(?:html?|htm)$", "", _base)  # 去末页名到目录
        for _c in ("sztd.htm", "szdw.htm", "szll.htm", "js.htm", "jsdw.htm",
                   "szdw/index.htm", "js/list.htm", "szdw/list.htm"):
            _u = f"{_root}/{_c}"
            if _verify_faculty_title(_u):
                return _u
    # 5) HTML 注释内的链接兜底 (2026-09-18 温哥华电影学院实锤: 站点对不同 UA
    # 返回不同导航变体, 我方 UA 的桌面导航把「师资力量→/site/teacher」注释掉了,
    # 页面真实存在但 BeautifulSoup 只解析出 Comment — 剥注释再扫长词)
    from bs4 import Comment as _Comment
    for _c in soup.find_all(string=lambda s: isinstance(s, _Comment)):
        try:
            _sub = BeautifulSoup(str(_c), "lxml")
        except Exception:
            continue
        for _a in _sub.find_all("a", href=True):
            _t = _a.get_text(strip=True); _h = (_a.get("href") or "").strip()
            if not _h or _h.startswith(("javascript", "#")):
                continue
            if any(_kw in _t for _kw in FACULTY_KW_LONG):
                _u = _safe_urljoin(dept_url, _h)
                if _verify_faculty_title(_u):
                    return _u
    return None

def collect_group_pages(fac_url, fac_html, exclude=()):
    """师资页同组分类页扫描(发现链增强, 与 fix_loop 分类扫描单源同口径)。
    2026-09-15 从 fix_loop 抽取共享: 覆盖 ①职称/教师类链接(正高级/副高级/
    中级及其他/高层次人才 — 北化工机电 164 人名单页入口实锤) ②select/option
    下拉分类(央美?xy= 国美?at= 类) ③裸系名/专业名 tab ④教师条目链接跳过
    (姓名+空白+职称粘连, 上科大实锤) ⑤分页页并入(发现链前置分页探测)。
    返回去重 URL 列表(不含 fac_url 自身与 exclude 内 URL)。"""
    try:
        from util.group_words import GROUP_KW as _GROUP_KW, GROUP_EXC as _GROUP_EXC
    except Exception:
        return []
    from util.teacher_extract import pager_pages as _pp
    soup = BeautifulSoup(fac_html, "lxml")
    _fac_host = urlparse(fac_url).netloc
    extra = []
    _seen_g = {fac_url.rstrip("/")} | {u.rstrip("/") for u in exclude}
    for a in soup.find_all("a", href=True):
        _t = a.get_text(strip=True)
        _hr = a.get("href", "").strip()
        if not _hr or _hr.startswith(("javascript", "#", "mailto", "tel:")):
            continue
        if len(_t) > 24 or len(_t) < 2:
            continue
        # 教师条目跳过: 姓名后必有空白(分类页锚文本姓名职称间无空白)。
        # 2026-09-16 合工大电气实锤: "教授 研究员"/"副教授 副研究员 高级实验师"
        # 纯职称组合分类tab — "教授"+空格被误判教师条目跳过, 分类页全漏;
        # 纯职称词组合(片段全是职称词)放行
        _t_pure = re.sub(r"[\s/]+", "", _t)
        if re.fullmatch(r"(?:教授|研究员|副教授|副研究员|实验师|高级实验师|讲师|"
                        r"助理研究员|助教|研究实习员|助理实验师|博士生导师|硕士生导师|"
                        r"博导|硕导|工程师|高级工程师)+", _t_pure):
            pass
        elif re.match(r"^[一-鿿·]{2,4}\s+", _t) and re.search(
                r"教授|讲师|助教|研究员|博士后|工程师|博导|硕导", _t):
            continue
        # 2026-09-15 地大(北京)工程技术实锤: "正高职称教师/副高职称教师/中级职称教师"
        # 职称档变体 — 通用形态 (正高|副高|中级)(职称)?(教师|及其他)?
        _t_ok = (_GROUP_KW.search(_t)
                 or re.search(r"教师$|科研人员|科研团队|研究员", _t)
                 or re.fullmatch(r"(正高级|正高|副高级|副高|中级)(职称)?(教师|及其他)?|高层次人才|初中级", _t))
        # 2026-09-16 辽大外院实锤: "英语系师资队伍简介" 是真实名单文章页
        # (4 个系各一页), EXC 的"简介"把它全滤 — 与"师资概况"同源问题:
        # 含师资/教师/导师/教授词的简介/概况页是名单页, EXC 只滤纯栏目页
        _t_bad = _GROUP_EXC.search(_t) and not re.search(
            r"师资|教师|导师|教授|队伍", _t)
        _t_bad = _t_bad or re.search(r"平台|中心|课题组|实验室|基地|实验", _t)
        if not _t_ok or _t_bad:
            continue
        _u = urljoin(fac_url, _hr)
        if _u.rstrip("/") in _seen_g:
            continue
        extra.append(_u)
    # select/option 下拉分类
    for _sel in soup.find_all("select"):
        for _op in _sel.find_all("option"):
            _t = _op.get_text(strip=True)
            _v = (_op.get("value") or "").strip()
            if not _v or _v.startswith(("javascript", "#")):
                continue
            if not _GROUP_KW.search(_t) and not re.search(r"系$|专业$|学部$", _t):
                continue
            _u = urljoin(fac_url, _v)
            if _u.rstrip("/") not in _seen_g:
                extra.append(_u)
    # 裸系名/专业名 tab — 2026-09-15 矿大机电实锤: 分类链接带括号说明
    # ("机械电子工程系（机械学科）"), fullmatch 要求整串以系结尾 → 全漏;
    # 先剥括号再匹配, 上限放宽到 12 字("智能控制与机器人系"9字)
    for a in soup.find_all("a", href=True):
        _t = a.get_text(strip=True)
        _hr = a.get("href", "").strip()
        if not _hr or _hr.startswith(("javascript", "#", "mailto", "tel:")):
            continue
        _t2 = re.sub(r"[（(].*?[）)]", "", _t).strip()
        # 2026-09-16 武理艾克斯马赛实锤: "法语教学/基础课教学/专业课教学"
        # 是按授课类型分的师资分类页 — 教学 尾词并入裸 tab 规则;
        # 南航自动化实锤: "控制科学与工程" 系级 tab(无系字尾, 82 人名单页)
        # — 工程/技术 尾词并入(专业名 tab 在师资页语境=系级名单页);
        # 广西艺术实锤: "美术系教师/音乐系教师" 系名+教师 尾词组合
        if not re.fullmatch(r"[一-鿿·]{2,12}(系|专业|方向|学科|学部|书院|教研室|研究所|教学|工程|技术)(教师|老师|名单|名录|成员)?", _t2):
            continue
        if _GROUP_EXC.search(_t):
            continue
        _u = urljoin(fac_url, _hr)
        if urlparse(_u).netloc != _fac_host or _u.rstrip("/") in ("/", ""):
            continue
        if _u.rstrip("/") not in _seen_g:
            extra.append(_u)
    # 分页页并入(发现链前置分页探测, 2026-09-15)
    try:
        for _u in _pp(fac_url, fac_html)[1:]:
            if _u.rstrip("/") not in _seen_g:
                extra.append(_u)
    except Exception:
        pass
    seen, out = set(), []
    for u in extra:
        # 站点坏链接归一: 绝对路径误带域名 "/mech.buct.edu.cn/14905/list.htm"
        # urljoin 会拼成 host/host/path 双重域名 404(北化工机电实测)
        _host = _fac_host
        u = re.sub(r"^(https?://" + re.escape(_host) + r")/" + re.escape(_host) + r"/",
                   r"\1/", u)
        k = u.rstrip("/")
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


# ── 人名提取 ──
def extract_teacher_names_from_page(html, page_url):
    """从HTML中提取教师姓名+链接（过滤导航词）"""
    soup = BeautifulSoup(html, "lxml")
    # 去除导航/页脚/侧边栏区域
    for sel in ["nav", ".nav", "#nav", ".menu", "#menu", ".header", "#header",
                ".footer", "#footer", ".sidebar", ".side", ".col_menu", ".wp_listcolumn",
                ".wp-panel", ".wp-navi", ".sub-menu", ".head-left", ".head-right",
                ".site-lang", ".col_news_head", ".col_path", ".col_metas"]:
        for el in soup.select(sel): el.decompose()

    teachers = []
    seen = set()
    for a in soup.find_all("a", href=True):
        name = a.get_text(strip=True)
        href = a.get("href", "").strip()
        # 必须像人名：2-4字纯中文
        if not re.match(r'^[一-鿿]{2,4}$', name): continue
        if not href or href.startswith("javascript") or href == "#": continue
        if name in NAV_WORDS: continue
        full_url = urljoin(page_url, href)
        key = (name, full_url)
        if key not in seen:
            seen.add(key)
            teachers.append({"name": name, "title": "", "detail_url": full_url})
    return teachers

# ── LLM 请求 ──
_llm = None
def get_llm():
    global _llm
    if _llm is None:
        try:
            import sys
            from util.llm import get_llm as _shared_llm
            _llm = _shared_llm()
        except: _llm = None
    # 不得按 available 过滤(线程局部未初始化→False→静默短路, 批量LLM路径全灭的根因)
    return _llm

def llm_extract_teachers(dept_name, url, html):
    """LLM 从页面文本提取教师。过滤导航词。"""
    soup = BeautifulSoup(html, "lxml")
    for sel in ["nav", ".nav", "#nav", ".menu", "#menu", ".header", "#header",
                ".footer", "#footer", ".sidebar", ".side", ".col_menu", ".wp_listcolumn",
                ".wp-panel", ".wp-navi", ".sub-menu"]:
        for el in soup.select(sel): el.decompose()

    body = soup.find("body") if soup.find("body") else soup
    for t in body.find_all(["script","style","noscript"]): t.decompose()
    text = body.get_text()[:6000]

    links = "\n".join(
        f"  {a.get_text(strip=True)[:30]} -> {urljoin(url, a.get('href',''))[:80]}"
        for a in body.find_all("a", href=True)[:100]
        if a.get_text(strip=True) and len(a.get_text(strip=True)) <= 25
    )

    prompt = f"""提取教师名单(排除导航/菜单/新闻链接,只要人名)。
院系:{dept_name} URL:{url}
页面文本(含括号职称):{text[:5000]}
链接:{links[:3000]}
返回JSON:{{"teachers":[{{"name":"","title":"","url":""}}]}}
规则:
- name必须是2-4字中文姓名(刘德华,张三丰不是域名!过滤"学校主页/学院概况/机构设置/首页/English/下一页/跳转"这些不是人名的词)
- title从括号提取(教授/副教授/讲师/系主任等)
- url是教师详情页链接
- 如果页面是按系/所/中心分类的子目录,用"subdirs":["完整URL1","完整URL2"]
只输出JSON。"""
    llm = get_llm()
    if not llm: return None

    r = llm.ask("你是教师信息提取专家。只输出JSON。", prompt)
    try: return json.loads(r)
    except:
        m = re.search(r'\{[\s\S]*\}', r)
        if m:
            try: return json.loads(m.group())
            except: pass
    return None

# ── 输出 ──
def write_teacher_csv(out_dir, school_name, dept_name, dept_url, faculty_url, teachers):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(out_dir/"teachers.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["大学","学院","教师姓名","职务职称","所属院系","个人详情URL","师资页URL","采集时间"])
        for t in teachers:
            w.writerow([school_name, dept_name, t.get("name",""), t.get("title",""),
                       t.get("department",""), t.get("detail_url",""), faculty_url, ts])

    with open(out_dir/"teachers.json", "w", encoding="utf-8") as f:
        json.dump({"school":school_name,"dept":dept_name,"total":len(teachers),"teachers":teachers,"at":ts}, f, ensure_ascii=False, indent=2)

    with open(out_dir/"test_report.json", "w", encoding="utf-8") as f:
        json.dump({"passed":len(teachers)>=3,"score":100 if len(teachers)>=3 else 50,"total":len(teachers)}, f, ensure_ascii=False)
