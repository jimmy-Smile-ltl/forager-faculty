#!/usr/bin/env python3
"""师资名单「前端 AJAX/JSON 下拉加载」通用适配 (2026-09-01 上交 airs/shss 样本)。

两类实测形态:
  A. JSON 无限滚动(airs 区域国别研究院 /faculty/byxz):
     页面内嵌 __FACULTY_PAGE__ = {searchUrl, detailUrlBase}; 滚动 sentinel
     触发 GET searchUrl?page=N&limit=10 → {"data":{"data":[{name_cn,title,
     title_desc,id,email...}]}}; 详情 = detailUrlBase + '/' + id。
  B. HTML 片段 AJAX(shss 人文学院 /Faculty/1-2):
     页面职称/字母 tab 是 javascript:; 点击调 GET /Web/AjaxFaculty
     ?t=1&d=2&title=<职称>&time=... → 返回 HTML 片段塞 #divtable。
     最优路径: 每系页 title=''(全部) 一次请求, 不要职称逐个请求(用户经验:
     「可以选择全部, 不用一个职称一个职称请求」)。

通用协议: detect_faculty_api 从师资页 HTML 检测端点与参数; fetch_faculty_api
按 kind 分页拉取并归一化为 [[name, title, detail_url, anchor], ...]。
"""
import json
import re
from urllib.parse import urljoin, urlparse

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# A 类: __FACULTY_PAGE__ 配置块
_FP_SEARCH = re.compile(r"searchUrl\s*[:=]\s*[\"']([^\"']+)")
_FP_DETAIL = re.compile(r"detailUrlBase\s*[:=]\s*[\"']([^\"']+)")
_FP_LIMIT = re.compile(r"var\s+limit\s*=\s*(\d+)")
# B 类: AjaxFaculty 式端点
_AJAX_RE = re.compile(r'''url\s*:\s*['"]([^'"]*Ajax[A-Za-z]*)['"]''', re.I)
_AJAX_PARAMS = re.compile(
    r"data\s*:\s*\{([^}]*?)\}", re.S)
# C 类: 校级教师主页平台(清华 faculty.dpi 族, 2026-09-16 用户提供接口实锤)
_TEACHER_JSP_RE = re.compile(
    r'https?://[^"\'<>\s]*teacher\.jsp[^"\'<>\s]*', re.I)

# ── L2 级探测: 外链 JS 文件扫描 + 候选端点试请求 (2026-09-16 用户指令:
# "如何探测出 json api 请求老师信息" — 端点常在 JS 文件里由 ajax/fetch
# 动态构造, 静态 HTML 看不到; 扫描页面引用的 JS 挖 URL 模式, 逐个 HTTP
# 试请求, 响应含教师 JSON 特征即命中 — 零浏览器纯 HTTP) ──
_JS_SRC_RE = re.compile(r'src\s*=\s*["\']([^"\']+\.js[^"\']*)["\']', re.I)
_JS_ENDPOINT_RE = re.compile(
    r"""["']([^"']*(?:\.jsp|\.php|\.aspx|\.action|\.do|\.json)[^"']*)["']"""
    r"""\s*[,\)]""", re.I)
_JS_AJAX_RE = re.compile(
    r"""(?:ajax|get|post|fetch)\s*\(\s*["']([^"']+)["']""", re.I)
# 教师名单 JSON 响应特征(命中任一即视为教师 API)
# 2026-09-16 北理马院实锤: gpowersoft CMS {"data":[{title:姓名,url,channelName,
# properties:{教研室…}}]} — data 数组内是 title 不是 name, 原特征漏;
# 中文 2-4 字 title 是教师姓名的强特征
_TEACHER_JSON_RE = re.compile(
    r'"totalnum"|"teacherData"|"teacherlist"|"teacher[s]?"\s*:\s*\[|'
    r'"teachers"\s*:\s*\[|"data"\s*:\s*\[\s*\{\s*"[^"]*name|'
    r'"title"\s*:\s*"[一-鿿·]{2,4}"', re.I)

# url: "../x.json" 键值形态(ajax 配置对象) — ajax/get 前缀正则抓不到
_JS_URLKEY_RE = re.compile(r'url\s*[:\=]\s*["\']([^"\']*?\.json[^"\']*)["\']', re.I)

# L2 探测 host 级缓存(2026-09-16 资源约束): 同 host 接口形态共享 —
# 清华各院系共用 faculty.dpi 平台, 首个院系探测后其余直接复用
_JS_PROBE_CACHE = {}


def detect_faculty_api(html: str, page_url: str) -> dict:
    """从师资页 HTML 检测名单 AJAX/JSON 端点。返回 dict 或 None。"""
    if not html:
        return None
    # A 类: __FACULTY_PAGE__ JSON 无限滚动
    m = _FP_SEARCH.search(html)
    if m:
        # 分页参数名: 站点JS里 params = { offset: ... } 用 offset, 否则 page
        _pname = "page"
        if re.search(r"\{\s*offset\s*:", html):
            _pname = "offset"
        cfg = {
            "kind": "json",
            "url": urljoin(page_url, m.group(1).replace("\\/", "/")),
            "method": "GET",
            "params": {"offset" if _pname == "offset" else "page": 1,
                       "limit": 10},
            "page_param": _pname,
            "fields": {"name": "name_cn", "title": "title_desc",
                       "fallback_title": "title", "id": "id"},
            "list_path": ("data", "data"),
        }
        m2 = _FP_DETAIL.search(html)
        if m2:
            cfg["detail_base"] = urljoin(page_url, m2.group(1).replace("\\/", "/"))
        m3 = _FP_LIMIT.search(html)
        if m3:
            cfg["params"]["limit"] = int(m3.group(1)) or 10
        return cfg
    # B 类: AjaxFaculty HTML 片段
    m = _AJAX_RE.search(html) if "Ajax" in html else None
    if m:
        cfg = {
            "kind": "html",
            "url": urljoin(page_url, m.group(1)),
            "method": "GET",
            "params": {"time": "0"},
        }
        m2 = _AJAX_PARAMS.search(html)
        if m2:
            for kv in re.findall(r"(\w+)\s*:\s*([^,}]+)", m2.group(1)):
                k = kv[0].strip()
                v = kv[1].strip().strip('"\'')
                if k in ("t", "d", "title", "name", "page", "id"):
                    cfg["params"][k] = v
        # 最优路径: title 全部(空), 不用职称逐个请求
        cfg["params"].pop("title", None)
        cfg["params"].pop("name", None)
        return cfg
    # C 类: 校级教师主页平台 XHR(2026-09-16 清华精仪实锤, 用户提供接口):
    # 院系页有 teacher.jsp?wbtreeid=NNN「教职员工」链接 → teacher.jsp 内有
    # resetCollege(N)/siteOwner/viewid 平台参数 → advancesearch.jsp 返回
    # JSON 全量名单(totalnum=89)。同厂商平台(tsites/jmyq 族)路径通用。
    m = _TEACHER_JSP_RE.search(html) if "teacher.jsp" in html else None
    if m:
        try:
            import curl_cffi.requests as cr
            _tj_url = m.group(0)
            _tj = cr.get(_tj_url, impersonate="chrome110", timeout=20,
                         verify=False).text
            _cm = re.search(r"resetCollege\(\s*(\d+)", _tj)
            _sm = re.search(r'siteOwner["\']?\s*[:=]\s*(\d+)', _tj)
            _vm = re.search(r'viewid["\']?\s*[:=]\s*(\d+)', _tj)
            if not _vm:
                _vm = re.search(r'id="(\d{6,})[A-F0-9]{20,}"', _tj)
            _km = re.search(r"resetCategory\(\s*(\d+)", _tj)
            if _cm:
                _host = _tj_url.split("/")[2]
                _q = ("collegeid={}&disciplineid=0&enrollid=0&pageindex=1"
                      "&pagesize=100000&rankid=0&degreeid=0&honorid=0&pinyin="
                      "&profilelen=100&teacherName=&searchDirection="
                      "&categoryid={}&viewmode=8&viewid={}&siteOwner={}"
                      "&viewUniqueId={}&showlang=zh_CN&ispreview=false"
                      "&basenum=0&ellipsis=&alignright=false&productType=0"
                      "&tutorType=").format(
                    _cm.group(1), _km.group(1) if _km else "1022",
                    _vm.group(1) if _vm else "", _sm.group(1) if _sm else "0",
                    _vm.group(1) if _vm else "")
                return {
                    "kind": "tsites",
                    "url": f"https://{_host}/system/resource/tsites/jmyq/"
                           f"advancesearch.jsp?{_q}",
                    "params": {},
                }
        except Exception:
            pass
    # C2 类: VSB+tsites 组合(2026-09-16 南农马院实锤, 用户提供接口):
    # D 类: Vue模板 + $.ajax API 驱动(2026-10-25 南科大海洋系实锤, 用户人工
    # 分析): {{item.member_name}} 插值 → url: HOST_URL + "/public/api/member/
    # getMemberListByCid", data:{cid,page,limit} — 模板字段名即 API 返回
    # 字段; 详情页模板从 :href="'../views/Teacher_information.html?id='+item.id" 抓。
    if "{{item." in html and "/api/" in html:
        _ax = re.search(r'url\s*:\s*[\w.]+\s*\+\s*["\']([^"\']*/api/[^"\']+)["\']', html)
        if not _ax:
            _ax = re.search(r'["\']([^"\']*/api/[^"\']*(?:member|teacher|staff|list|get)[^"\']*)["\']', html, re.I)
        if _ax:
            # HOST_URL 变量是站点基础路径(南科大 = .../ocean), 必须以其为
            # 基准拼接 — urljoin(page_url) 会丢 /ocean/ 段(404 实测)
            _mh = re.search(r'HOST_URL\s*[:=]\s*["\']([^"\']+)["\']', html)
            _api_base = _mh.group(1) if _mh else page_url
            cfg = {"kind": "vue_api",
                   "url": urljoin(_api_base, _ax.group(1)),
                   "method": "GET",
                   "params": {"page": 1, "limit": 36},
                   "fields": {"name": "member_name",
                              "title": "member_title", "id": "id"},
                   "detail_tpl": ""}
            # data 块必须锚定到同一 $.ajax(API URL 后 400 字内), 防串到
            # 页面里其他 ajax 的 data 块(实测: 全局正则抓到别的块丢了 cid)
            _i = html.find(_ax.group(1))
            _blk = html[_i:_i + 500] if _i >= 0 else ""
            _db = re.search(r'data\s*:\s*\{(.*?)\n\s*\}', _blk, re.S)
            if _db:
                for _kv in re.findall(r"(\w+)\s*:\s*([^,\n]+)", _db.group(1)):
                    _k, _v = _kv[0].strip(), _kv[1].strip().strip("'\"")
                    # 只收白名单键且值为字面量(数字/简单串), 变量引用跳过
                    if _k in ("cid", "page", "limit", "language", "size", "type") \
                            and re.fullmatch(r"[0-9A-Za-z_\-]+", _v or "") \
                            and not re.fullmatch(r"vim\w*", _v or ""):
                        cfg["params"][_k] = _v
                    # language:vim.lang 变量形态 → 默认 cn(南科大 500 实测:
                    # 缺 language 参数 API 直接 500)
                    if _k == "language" and _v and not re.fullmatch(r"[0-9A-Za-z_\-]+", _v or ""):
                        cfg["params"]["language"] = "cn"
            _dt = re.search(r"([^'\"]*\.html)\?id=[^+]*\+\s*item\.id", html)
            if _dt:
                cfg["detail_tpl"] = urljoin(page_url,
                                            _dt.group(1) + "?id={id}")
            return cfg
    # 无 teacher.jsp 链接 — 页面直接内嵌 tsites 组件(queryteacher.js/
    # tsites_advance_search) + setCollege(N)/siteOwner/viewid 参数,
    # 接口在同域 /system/resource/tsites/portal/queryteacher.jsp
    _c2 = (("tsites_advance_search" in html or "queryteacher.js" in html
            or "advancesearch.js" in html or "tsites_load_data" in html)
           and re.search(r"(?:setCollege|resetCollege)\(\s*(\d+)", html))
    if _c2:
        _cm = re.search(r"(?:setCollege|resetCollege)\(\s*(\d+)", html)
        _sm = re.search(r'siteOwner["\']?\s*[:=]\s*(\d+)', html) or \
            re.search(r"_jsq_\([^,]+,[^,]+,[^,]+,\s*(\d+)", html)
        _vm = re.search(r'id="(\d{6,})[A-F0-9]{20,}"', html) or \
            re.search(r'viewid["\']?\s*[:=]\s*(\d+)', html)
        _qpath = "portal/queryteacher.jsp"
        _pm = re.search(r'/system/resource/tsites/([^"\']*\.jsp)', html)
        if _pm:
            _qpath = _pm.group(1)
        _host = urlparse(page_url).netloc
        _q = ("collegeid={}&isshowpage=false&postdutyid=0&postdutyname="
              "&facultyid=&disciplineid=0&rankcode=&ranklevel=&jobtypecode="
              "&enrollid=0&pageindex=1&pagesize=70&login=true&profilelen=100"
              "&honorid=0&pinyin=&rankid=0&isbd=0&atschool=0&teacherName="
              "&searchDirection=&viewmode=10&viewOwner=&viewid={}&siteOwner={}"
              "&viewUniqueId=u9&showlang=zh_CN&ellipsis=&actiontype=advancesearch").format(
            _cm.group(1), _vm.group(1) if _vm else "",
            _sm.group(1) if _sm else "0")
        return {"kind": "tsites",
                "url": f"https://{_host}/system/resource/tsites/{_qpath}?{_q}",
                "params": {}}
    # D 类: 同方知网 CNKI 学者库(2026-09-16 地大北京 scholar.cugb 实锤):
    # /scholar/list/<机构ID>/N.shtml 检索壳, 数据走 POST
    # scholarList_new.shtml?pageNo=&pageSize=(空 filter 返回全校学者,
    # 按机构列过滤) — pageSize=500 实测可到 500/页
    if "/scholar/list/" in page_url or "scholarList_new.shtml" in html:
        _host = urlparse(page_url).netloc
        return {"kind": "cnki_scholar",
                "url": f"https://{_host}/scholar/scholarList_new.shtml",
                "params": {}}
    # E 类: 苏迪 wp3 CMS generalQuery 查询(2026-09-16 东南艺术实锤 +
    # 上财前沿交叉扩展): 页面引 getData.js, 端点 /_wp3services/generalQuery
    # ?queryObj=teacherHome|articles, POST(siteId/columnId/pageIndex/rows)
    # 返回 {total, data:[{title姓名, career职称, cnUrl|wapUrl详情, profile,
    # headerPic|mircImgPath}]} — 全校模板共用。端点/siteId/queryObj 只在
    # getData.js 里, columnId 在页面 col-id 属性 → 抓 JS+页面属性拼装
    # 2026-09-17 北交卡死根因(faulthandler 栈实锤): [^"']*getData\.js[^"']*
    # 在无匹配长文本上灾难回溯 O(n^2) → 100% CPU 死循环持 GIL 饿死心跳。
    # 子串预检 O(n) 短路, 命中才跑正则(有锚点不会爆炸)
    _wp3_js = None
    if "getData.js" in html:
        _wp3_js = re.search(r'([^"\']*getData\.js[^"\']*)', html)
    elif "faculty.js" in html:
        # 2026-09-22 复旦数院实锤: 苏迪 wp3 名单 JS 命名变体 faculty.js
        # (jzyg/list.htm 页) — generalQuery 端点/siteId/columnId 在 JS 里,
        # 页面 HTML 只有 script src 引用, 旧检测(getData.js)失明 → 8/144 漏采
        _wp3_js = re.search(r'([^"\']*faculty\.js[^"\']*)', html)
    _wp3_hit = ("queryObj=teacherHome" in html
                or ("generalQuery" in html and "teacher" in html))
    if _wp3_js and not _wp3_hit:
        try:
            import curl_cffi.requests as _cr
            _ju = urljoin(page_url, _wp3_js.group(1))
            _jc = _cr.get(_ju, impersonate="chrome110", timeout=10,
                          verify=False).text
            _wp3_hit = bool(re.search(r"queryObj=(teacherHome|articles|teacher)",
                                      _jc))
            if not _wp3_hit:
                _jc = ""
        except Exception:
            _jc = ""
    else:
        _jc = ""
    if _wp3_hit:
        _host = urlparse(page_url).netloc
        _sid = None
        _m = re.search(r'siteId["\']?\s*[:=]\s*["\']?(\d+)', html)
        if _m:
            _sid = int(_m.group(1))
        if not _sid and _jc:
            _m2 = re.search(r'var\s+siteId\s*=\s*(\d+)', _jc)
            if _m2:
                _sid = int(_m2.group(1))
        # columnId: 页面 col-id 属性(上财)或 columnId 变量(getData.js)
        _cid = None
        _m = re.search(r'col-id="(\d+)"', html)
        if _m:
            _cid = int(_m.group(1))
        if not _cid and _jc:
            _m2 = re.search(r'var\s+columnId\s*=\s*(\d+)', _jc)
            if _m2:
                _cid = int(_m2.group(1))
        _qo = "teacherHome"
        _mq = re.search(r"queryObj=(teacherHome|articles|teacher)", (_jc or html))
        if _mq:
            _qo = _mq.group(1)
        if _sid:
            return {"kind": "wp3_teacher",
                    "url": f"https://{_host}/_wp3services/generalQuery"
                           f"?queryObj={_qo}",
                    "params": {"siteId": _sid, "columnId": _cid}}
    # F 类: PeopleSoft WEBSITE_FACULTY 服务(2026-09-16 上交安泰实锤):
    # IScript_Dispatcher?Resource=WEBSITE_FACULTY&OperateType=QG 端点,
    # comParams all_flag=Y 返回全部在职教师 {root:[{key:系,list:[{name,
    # title, department, webpage}]}]} — 183 人一次拿全, 筛选条件繁多
    # (系/职称/类型) 但空条件+all_flag=Y 即全量
    _psc = re.search(r'(/psc/[^"\'\s]*IScript_Dispatcher\?Resource=WEBSITE_FACULTY)',
                     html)
    if _psc:
        _host = urlparse(page_url).netloc
        return {"kind": "psc_faculty",
                "url": f"https://{_host}{_psc.group(1)}",
                "params": {}}
    return None


def _session():
    import curl_cffi.requests as cr
    return cr.Session()


def probe_js_endpoints(html: str, page_url: str, max_js: int = 10,
                       max_cands: int = 8) -> dict:
    """L2 级探测(2026-09-16): 扫描页面引用的 JS 文件挖端点 URL 模式
    (ajax/fetch/get/post 调用 + .jsp/.php/.json 形态), 逐个纯 HTTP 试请求,
    响应含教师 JSON 特征即返回 cfg。零浏览器。

    资源约束(用户质疑"逐个试请求浪费资源", 2026-09-16 优化):
    - host 级缓存: 同 host 探测结果缓存(清华 20 院系共用 faculty.dpi 平台,
      首个院系探测后其余直接复用, 均摊成本 ~2 请求/院系);
    - 候选过滤排序: 排除静态资源/跨域第三方, 教师关键词 URL 优先试;
    - 命中即停 + 快超时 8s。"""
    if not html:
        return None
    import curl_cffi.requests as cr
    from urllib.parse import urlparse as _up
    _host = _up(page_url).netloc
    if _host in _JS_PROBE_CACHE:
        return _JS_PROBE_CACHE[_host]
    cands, seen = set(), set()

    def _add(u):
        if u and u not in seen and not u.startswith(("javascript", "#", "data:")):
            seen.add(u)
            _full = urljoin(page_url, u)
            # 只试同域(教师接口在站内; 第三方 CDN/统计脚本跳过)
            if _up(_full).netloc == _host:
                cands.add(_full)

    # ① 内嵌 <script> 里的 ajax/fetch URL + url:"x.json" 键值形态
    for _m in _JS_AJAX_RE.finditer(html):
        _add(_m.group(1))
    for _m in _JS_URLKEY_RE.finditer(html):
        _add(_m.group(1))
    # ② 外链 JS 文件: 抓回来再挖 — 教师关键词命名的 JS 优先(2026-10-26
    # 国科大基础教育研究院实锤: zrjs_search.js 在页面第 6+ 位, max_js 截断
    # 导致 /common/zrjsTeacher.json 漏检; 关键词排序后首批必抓)
    _js_files = [_JS_SRC_RE.sub(lambda m: m.group(1), m.group(0))
                 for m in _JS_SRC_RE.finditer(html)]
    _jskw = re.compile(r"teacher|faculty|staff|people|szdw|search|list|member", re.I)
    _js_files.sort(key=lambda u: 0 if _jskw.search(u) else 1)
    for _jsu in _js_files[:max_js]:
        _full_js = urljoin(page_url, _jsu)
        try:
            _jr = cr.get(_full_js, impersonate="chrome110", timeout=8, verify=False)
            _jsc = _jr.text if _jr.status_code == 200 else ""
        except Exception:
            _jsc = ""
        for _m in _JS_AJAX_RE.finditer(_jsc):
            _add(urljoin(_full_js, _m.group(1)))
        for _m in _JS_URLKEY_RE.finditer(_jsc):
            _add(urljoin(_full_js, _m.group(1)))
        for _m in _JS_ENDPOINT_RE.finditer(_jsc):
            _add(urljoin(_full_js, _m.group(1)))
    # ③ 候选排序: 教师关键词 URL 优先(提高命中率, 少试无关接口);
    # .json 静态数据文件优先(北理 gpowersoft szdw.json 实锤, 一次请求全量)
    _kw = re.compile(r"teacher|faculty|staff|people|szdw|advance|search|list|college|\.json", re.I)
    _cands = sorted(cands, key=lambda u: 0 if _kw.search(u) else 1)
    # ④ 试请求(排除静态资源, 快超时, 命中即停)
    for _c in _cands[:max_cands]:
        if _c.endswith((".css", ".png", ".jpg", ".ico", ".gif", ".js")):
            continue
        try:
            _r = cr.get(_c, impersonate="chrome110", timeout=8, verify=False,
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Accept": "application/json, text/javascript, */*",
                                 "Referer": page_url})
            if _r.status_code != 200 or len(_r.text) < 200:
                continue
            if "<" in _r.text[:100] and "<!DOCTYPE" in _r.text[:300].upper():
                continue  # HTML 页非 JSON
            if _TEACHER_JSON_RE.search(_r.text[:4000]):
                _cfg = {"kind": "raw_json", "url": _c,
                        "params": {}, "_sample": _r.text[:4000]}
                _JS_PROBE_CACHE[_host] = _cfg
                return _cfg
        except Exception:
            continue
    _JS_PROBE_CACHE[_host] = None  # 阴性结果也缓存(避免同 host 重复探测)
    return None


def dynamic_probe(page_url: str, timeout: int = 25) -> dict:
    """L3 级探测(2026-09-16 用户指令: "可能需要打开真实浏览器分析"):
    CDP 打开页面 + Network 响应监听, 收集页面 JS 执行后发起的 XHR/fetch
    JSON 响应, 教师名单特征匹配 → 返回 cfg(端点 URL 直接纯 HTTP 重放,
    之后无需再开浏览器)。依赖 9222 CDP 可用; 不可用返回 None(调用方降级)。"""
    try:
        from util.cdp_util import cdp_page
        import time as _time
    except Exception:
        return None
    captured = []
    try:
        with cdp_page() as (page, ctx):
            page.on("response", lambda r: captured.append(r))
            try:
                page.goto(page_url, timeout=timeout * 1000, wait_until="domcontentloaded")
                _time.sleep(4)  # 等 JS 发 XHR
            except Exception:
                pass
    except Exception:
        return None
    for _r in captured:
        try:
            _ct = (_r.headers or {}).get("content-type", "")
            if "json" not in _ct and "text/plain" not in _ct:
                continue
            _body = _r.text()
            if not _body or len(_body) < 100:
                continue
            if _TEACHER_JSON_RE.search(_body[:4000]):
                return {"kind": "raw_json", "url": _r.url, "params": {},
                        "_sample": _body[:4000]}
        except Exception:
            continue
    return None


def render_and_probe(page_url: str, timeout: int = 35, idle_sec: float = 3.0):
    """浏览器渲染验证(2026-09-16 用户指令: 区分"真无老师"与"API 渲染"):
    打开页面, 等网络空闲(连续 idle_sec 无新响应, 页面稳定), 返回
    (rendered_html, api_cfg or None, captured_cfgs)。
    - rendered_html: 渲染后 DOM — 供调用方兜底提取(方案2: 浏览器打开提取)
    - api_cfg: 捕获 XHR 中教师 JSON 特征端点 — 复现 API 优先(方案1)
    依赖 9222 CDP; 不可用返回 (None, None, [])。"""
    import time as _time
    captured = []
    try:
        from util.cdp_util import cdp_page
    except Exception:
        return None, None, []
    try:
        with cdp_page() as (page, ctx):
            def _on_resp(r):
                captured.append(r)
            page.on("response", _on_resp)
            try:
                page.goto(page_url, timeout=timeout * 1000,
                          wait_until="domcontentloaded")
            except Exception:
                pass
            # 等网络空闲: 连续 idle_sec 无新响应; 总上限 timeout+10s
            _t0, _last_n, _deadline = _time.time(), 0, _time.time() + timeout + 10
            while _time.time() < _deadline:
                _time.sleep(0.5)
                if len(captured) > _last_n:
                    _last_n = len(captured)
                    _t0 = _time.time()
                elif _time.time() - _t0 >= idle_sec:
                    break
            _html = page.content()
    except Exception:
        return None, None, []
    _cfg, _cfgs = None, []
    for _r in captured:
        try:
            _ct = (_r.headers or {}).get("content-type", "")
            _url = _r.url
            if "json" in _ct or "text/plain" in _ct \
                    or re.search(r"\.json(\?|$)", _url):
                _body = _r.text()
                if _body and len(_body) > 100 \
                        and _TEACHER_JSON_RE.search(_body[:4000]):
                    _c = {"kind": "raw_json", "url": _url, "params": {},
                          "_sample": _body[:4000]}
                    _cfgs.append(_c)
                    if _cfg is None:
                        _cfg = _c
        except Exception:
            continue
    return _html, _cfg, _cfgs


def parse_raw_teacher_json(text: str) -> list:
    """L2 探测到的原始教师 JSON → 归一化 rows。递归找含 name 字段的
    对象数组(teacherlist/teachers/data 等任意嵌套)。"""
    try:
        j = json.loads(text)
    except Exception:
        return []
    rows, seen = [], set()

    def walk(node):
        if isinstance(node, list):
            for it in node:
                walk(it)
        elif isinstance(node, dict):
            nm = node.get("name") or node.get("teacherName") or node.get("showName")
            # 2026-09-16 北理马院实锤: gpowersoft CMS 姓名在 title 字段
            # ({"title":"陈海若",url,channelName,properties}) — title 须 2-4
            # 字中文才认(新闻 title 长句不认)
            if not nm:
                _t = node.get("title")
                if isinstance(_t, str) and re.fullmatch(r"[一-鿿·]{2,4}", _t.strip()):
                    nm = _t.strip()
            if isinstance(nm, str) and nm.strip():
                nm2 = nm.strip()
                if nm2 not in seen and 2 <= len(nm2) <= 24:
                    seen.add(nm2)
                    _t = node.get("title")
                    _ttl = str(node.get("prorank") or node.get("channelName") or "").strip()
                    # title 与姓名同字段家族(gpowersoft)不取; name 字段家族
                    # title 是职称
                    if not _ttl and isinstance(_t, str) and _t.strip() != nm2:
                        _ttl = _t.strip()[:40]
                    _url = str(node.get("url") or node.get("detail_url")
                               or node.get("summary") or "").strip()
                    _unit = str(node.get("unit") or "").strip()
                    if not _unit:
                        _p = node.get("properties")
                        if isinstance(_p, dict):
                            _unit = str(_p.get("教研室") or _p.get("单位") or _p.get("院系") or "").strip()
                    rows.append([nm2, _ttl, _url, _unit])
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(j)
    return rows


def fetch_faculty_api(cfg: dict, page_url: str, max_pages: int = 50,
                      dept_filter: str = ""):
    """按配置拉取名单, 返回归一化 [[name, title, detail_url, anchor], ...]。"""
    sess = _session()
    rows, seen = [], set()

    def _get(params):
        return sess.get(cfg["url"], params=params,
                        headers={"User-Agent": _UA, "Referer": page_url,
                                 "X-Requested-With": "XMLHttpRequest"},
                        impersonate="chrome110", timeout=20)

    if cfg["kind"] == "raw_json":
        # 2026-09-16 L2 探测: 原始教师 JSON 直接解析
        try:
            r = sess.get(cfg["url"],
                         headers={"User-Agent": _UA, "Referer": page_url,
                                  "X-Requested-With": "XMLHttpRequest",
                                  "Accept": "application/json"},
                         impersonate="chrome110", timeout=25)
            rows.extend(parse_raw_teacher_json(r.text))
        except Exception:
            pass
        return rows
    if cfg["kind"] == "tsites":
        # 2026-09-16 tsites 教师平台族: ①清华 teacherData=[{collegeName,
        # teacherlist:[...]}] 分组结构 ②南农 teacherData=[教师对象] 扁平结构
        # (VSB+tsites 组合, 用户提供接口实锤) — 两形态都兼容
        try:
            r = sess.get(cfg["url"],
                         headers={"User-Agent": _UA, "Referer": page_url,
                                  "X-Requested-With": "XMLHttpRequest",
                                  "Accept": "application/json"},
                         impersonate="chrome110", timeout=25)
            j = r.json()
            items = []
            for grp in j.get("teacherData") or []:
                if isinstance(grp, dict) and grp.get("teacherlist"):
                    items.extend(grp["teacherlist"])
                elif isinstance(grp, dict):
                    items.append(grp)  # 扁平结构(南农)
            for it in items:
                name = str(it.get("name") or it.get("showName") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                rows.append([name,
                             str(it.get("prorank") or "").strip(),
                             str(it.get("url") or "").strip(),
                             str(it.get("unit") or it.get("collegeName") or "").strip()])
        except Exception:
            pass
        return rows
    if cfg["kind"] == "cnki_scholar":
        # 2026-09-16 地大北京实锤: CNKI 同方知网学者库 — POST 空 filter 拿
        # 全校学者(HTML 片段), 按机构列过滤出本院系; pageSize=500 实测有效
        from bs4 import BeautifulSoup as _BS
        for page in range(1, 30):
            try:
                r = sess.post(cfg["url"] + f"?pageNo={page}&pageSize=500",
                              data={"nfilter": "[]", "gfilter": "[]",
                                    "sfilter": "[]", "sortBy": "",
                                    "order": "", "isDefault": "true"},
                              headers={"User-Agent": _UA, "Referer": page_url,
                                       "X-Requested-With": "XMLHttpRequest"},
                              impersonate="chrome110", timeout=30)
            except Exception:
                break
            if r.status_code != 200 or len(r.text) < 500:
                break
            _s = _BS(r.text, "lxml")
            _rows = 0
            for _a in _s.find_all("a", href=True):
                _nm = _a.get_text(strip=True)
                if not re.fullmatch(r"[一-鿿·]{2,4}", _nm):
                    continue
                _m = re.search(r"/scholarDetail/\d+\.shtml", _a.get("href", ""))
                if not _m:
                    continue
                # 行容器找职称/机构
                _row = _a.find_parent("li") or _a.find_parent("tr")
                _rt = _row.get_text(" ", strip=True) if _row else ""
                _zc = re.search(r"(教授|研究员|副教授|副研究员|讲师|高级工程师|"
                                r"工程师|博士)(?=\s|总成果|$)", _rt)
                _unit = re.search(r"([一-鿿·]{2,20}(?:学院|学部|研究院|系|中心|处|室))",
                                  _rt)
                # 院系过滤: 诊断侧传 dept 名(括号装饰剥除), 机构列匹配才收
                # (全校学者一次拿全, 不滤会整校混入)
                if dept_filter:
                    _dk = re.sub(r"[（(].*?[）)]", "", dept_filter).strip()
                    _un = _unit.group(1) if _unit else ""
                    if not _un or (_dk not in _un and _un not in _dk):
                        continue
                if _nm in seen:
                    continue
                seen.add(_nm)
                rows.append([_nm, _zc.group(1) if _zc else "",
                             urljoin(page_url, _m.group(0)),
                             _unit.group(1) if _unit else ""])
                _rows += 1
            if _rows < 500:
                break
        return rows
    if cfg["kind"] == "wp3_teacher":
        # 2026-09-16 东南艺术(teacherHome)+上财前沿交叉(articles)实锤:
        # 苏迪 wp3 CMS generalQuery — POST 条件(空过滤=全量), rows 拉大
        # 一次拿全; 2026-09-22 复旦数院实锤 queryObj=articles 模板(文章型
        # 名单): returnInfos=title/f1(英文名)/f2(职称)/f4(系)/f9-f12(系/办公室/
        # 电话/邮箱)/shortTitle(职称档), conditions=scope+f1 like+shortTitle
        # like+f4 like, orders=f1 asc — 与 teacherHome 模板(东南/上财:
        # career/profile 字段)不同, 按 queryObj 分派
        import json as _json
        from urllib.parse import urlparse as _up, parse_qs as _pq
        _sid = cfg["params"]["siteId"]
        _cid = cfg["params"].get("columnId")
        _qo = _pq(_up(cfg["url"]).query).get("queryObj", ["teacherHome"])[0]
        if _qo == "articles":
            # 文章型名单模板(复旦数院 144人 实锤)
            _ret = _json.dumps([
                {"field": "title", "name": "title"},
                {"field": "publishTime", "name": "publishTime"},
                {"field": "f1", "name": "f1"},
                {"field": "f2", "name": "f2"},
                {"field": "f4", "name": "f4"},
                {"field": "f9", "name": "f9"},
                {"field": "f10", "name": "f10"},
                {"field": "f11", "name": "f11"},
                {"field": "f12", "name": "f12"},
                {"field": "shortTitle", "name": "shortTitle"},
                {"field": "imgPath", "name": "imgPath"}])
            _cond = _json.dumps([
                {"field": "scope", "value": 1, "judge": "="},
                {"field": "f1", "value": "%", "judge": "like"},
                {"field": "shortTitle", "value": "", "judge": "like"},
                {"field": "f4", "value": "", "judge": "like"}])
            _orders = _json.dumps([{"field": "f1", "type": "asc"}])
            _data = {"siteId": _sid, "pageIndex": 1, "rows": 999,
                     "returnInfos": _ret, "conditions": _cond,
                     "orders": _orders}
            if _cid:
                _data["columnId"] = _cid
            try:
                r = sess.post(cfg["url"], data=_data,
                              headers={"User-Agent": _UA, "Referer": page_url,
                                       "X-Requested-With": "XMLHttpRequest"},
                              impersonate="chrome110", timeout=30)
                j = r.json()
                for it in j.get("data") or []:
                    name = str(it.get("title") or "").strip()
                    # 清洗「（双聘）」等括号后缀(姓名列保持纯姓名)
                    name = re.sub(r"[（(][^）)]*[）)]$", "", name).strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    # 职称: f2(详细档「教授（博士生导师）」)优先, shortTitle 兜底
                    _tt = str(it.get("f2") or it.get("shortTitle") or "").strip()
                    _du = str(it.get("url") or "").strip()
                    rows.append([name, _tt[:40], _du,
                                 str(it.get("f4") or it.get("f9") or "")[:40]])
            except Exception:
                pass
            return rows
        _ret = _json.dumps([
            {"field": "title", "name": "title"},
            {"field": "cnUrl", "name": "cnUrl"},
            {"field": "wapUrl", "name": "wapUrl"},
            {"field": "headerPic", "name": "headerPic"},
            {"field": "mircImgPath", "name": "mircImgPath"},
            {"field": "career", "name": "career"},
            {"field": "profile", "name": "profile"},
            {"field": "f8", "name": "f8"}])
        _cond = _json.dumps([
            {"field": "language", "value": "1", "judge": "="},
            {"field": "career", "value": "", "judge": "="},
            {"field": "exField1", "value": "", "judge": "="},
            {"field": "exField2", "value": "", "judge": "="},
            {"orConditions": [{"field": "title", "value": "", "judge": "like"},
                              {"field": "exField3", "value": "", "judge": "like"}]}])
        _orders = _json.dumps([{"field": "title", "type": "asc"},
                               {"field": "letter", "type": "asc"},
                               {"field": "exField2", "type": "asc"}])
        try:
            _data = {"siteId": _sid, "pageIndex": 1, "rows": 999,
                     "returnInfos": _ret, "conditions": _cond,
                     "orders": _orders, "articleType": 1, "level": 1}
            if _cid:
                _data["columnId"] = _cid
            r = sess.post(cfg["url"], data=_data,
                          headers={"User-Agent": _UA, "Referer": page_url,
                                   "X-Requested-With": "XMLHttpRequest"},
                          impersonate="chrome110", timeout=30)
            j = r.json()
            for it in j.get("data") or []:
                name = str(it.get("title") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                # 详情: wapUrl(resume 页, 上财)优先, cnUrl(栏目页, 东南)兜底
                _du = str(it.get("wapUrl") or it.get("cnUrl") or "").strip()
                rows.append([name,
                             str(it.get("career") or "").strip()[:40],
                             _du,
                             str(it.get("title") or "")[:40]])
        except Exception:
            pass
        return rows
    if cfg["kind"] == "psc_faculty":
        # 2026-09-16 上交安泰实锤: PeopleSoft WEBSITE_FACULTY — all_flag=Y
        # 全量在职教师(系/职称筛选条件繁多, 空条件即全量), 详情=webpage
        try:
            r = sess.get(
                cfg["url"] + "&OperateType=QG&comParams=" +
                '{"type":"A","search_type":"D","all_flag":"Y","language":"ZHS"}',
                headers={"User-Agent": _UA, "Referer": page_url},
                impersonate="chrome110", timeout=30)
            j = r.json()
            for grp in (j.get("data") or {}).get("root") or []:
                for it in grp.get("list") or []:
                    name = str(it.get("name") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    _wp = str(it.get("webpage") or "").strip()
                    if _wp and not _wp.startswith("http"):
                        _wp = urljoin(page_url, _wp)
                    rows.append([name,
                                 str(it.get("title") or "").strip()[:40],
                                 _wp,
                                 str(it.get("department") or "").strip()])
        except Exception:
            pass
        return rows
    if cfg["kind"] == "vue_api":
        # 2026-10-25 D 类处理: Vue模板+API 站点(南科大海洋系实锤) — 分页拉
        # JSON, member_name/member_title(list需join)/id 字段, 详情页按
        # detail_tpl({id}占位) 构造; 页数据 < limit 即停。
        # HOST_URL 上下文路径不在页面时(定义在外链JS), 用子域首标签作
        # 上下文候选(南科大 ocean 子域 → /ocean/ 上下文, 404 实测后补出)
        _cands = [cfg["url"]]
        _hu = urlparse(cfg["url"])
        _lbl = (_hu.hostname or "").split(".")[0]
        if _lbl and len(_lbl) >= 2 and not _hu.path.startswith(f"/{_lbl}"):
            _cands.append(f"{_hu.scheme}://{_hu.netloc}/{_lbl}{_hu.path}")
        for _cu in _cands:
            for page in range(1, max_pages + 1):
                try:
                    _p = dict(cfg.get("params") or {})
                    _p["page"] = page
                    r = sess.get(_cu, params=_p,
                                 headers={"User-Agent": _UA, "Referer": page_url,
                                          "X-Requested-With": "XMLHttpRequest"},
                                 impersonate="chrome110", timeout=20,
                                 verify=False)
                    j = r.json()
                    items = j.get("data") or []
                except Exception:
                    items = []
                if not items:
                    break
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("member_name")
                               or it.get("name") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    _t = it.get("member_title")
                    title = "、".join(_t) if isinstance(_t, list) else str(_t or "")
                    _iid = it.get("id") or it.get("member_id") or ""
                    url = ""
                    if _iid and cfg.get("detail_tpl"):
                        url = cfg["detail_tpl"].replace("{id}", str(_iid))
                    rows.append([name, title[:40], url, name])
                if len(items) < int(cfg.get("params", {}).get("limit") or 20):
                    break
            if rows:
                break
        return rows
    if cfg["kind"] == "json":
        fields = cfg["fields"]
        detail_base = cfg.get("detail_base", "")
        for page in range(1, max_pages + 1):
            params = dict(cfg.get("params") or {})
            params[cfg.get("page_param", "page")] = page
            try:
                r = _get(params)
                j = r.json()
            except Exception:
                break
            items = j
            total = None
            for k in cfg.get("list_path", ("data", "data")):
                if isinstance(items, dict):
                    total = items.get("total")
                    items = items.get(k)
                else:
                    items = None
            if not items:
                break
            for it in items:
                name = str(it.get(fields.get("name", "name_cn")) or "").strip()
                title = str(it.get(fields.get("title", "title_desc")) or ""
                            or it.get(fields.get("fallback_title", "title")) or "").strip()
                iid = it.get(fields.get("id", "id"))
                detail = (detail_base.rstrip("/") + "/" + str(iid)) if detail_base and iid else ""
                if not name or name in seen:
                    continue
                seen.add(name)
                rows.append([name, title, detail, name])
            if len(items) < int(cfg.get("params", {}).get("limit", 10)):
                break
            if total is not None and len(rows) >= total:
                break
    else:  # html 片段
        try:
            r = _get(cfg["params"])
            if r.status_code == 200 and len(r.text) > 100:
                from util.teacher_extract import extract_teachers
                for t in extract_teachers(r.text, cfg["url"]):
                    if t[0] not in seen:
                        seen.add(t[0])
                        rows.append([t[0], t[1], t[2], t[3]])
        except Exception:
            pass
    return rows
