#!/usr/bin/env python3
"""
僵尸学院测试循环(自我进化引擎) — 2026-09-15 用户指令:
"那么多僵尸学院, 搭建测试僵尸学院, 改进本阶段工作流设计这样的循环改进, 自我进化流程"

工作流(每轮):
  1. 取样: 从失败队列分层取样 N 个僵尸(attempts 降序优先=最难啃的)
  2. 轻量诊断: 每样本跑确定性核心路径(首页→发现链→师资页→分页→提取),
     不跑 LLM 改进轮(网关波动+慢, 改进期只修确定性环节), 单样本 ≤90s
  3. 形态分类: net_fail / no_faculty / fac_net / fac_empty / few_teachers /
     pollution / ok — 每形态即一类"待修复缺陷"
  4. jsonl 落盘(断点续跑) + 汇总报告
  5. 修复 → 回归(回归集防退化) → 沉淀(修复固化进代码/词表/规则)

用法:
  uv run python improve_zombie_loop.py --n 12        # 取 12 个未诊断僵尸测试
  uv run python improve_zombie_loop.py --report      # 只出已诊断汇总
  uv run python improve_zombie_loop.py --regression  # 回归集(已修复院系)防退化
  uv run python improve_zombie_loop.py --dept 学校 院系  # 单院系诊断
"""
import argparse, json, re, subprocess, sys, time, threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from _paths import ROOT, LOGS  # noqa: E402

# 2026-09-16 浏览器渲染验证并发节流: 9222 tab 有限, 20 并发全开会挤爆
_RENDER_SEM = threading.Semaphore(4)

sys.path.insert(0, str(ROOT.parent))  # util 引导(与 fix_loop 同款)

RESULTS = LOGS / "_zombie_test_results.jsonl"
QUEUE_F = LOGS / "_fix_loop_queue.json"
FREEZE_F = LOGS / "_zombie_freeze.json"

# 回归集: 已修复并验证的院系(每轮防退化) — 上科大/北化工 2026-09-15 修复验证
# 2026-09-18 十批攻坚扩充: 温影(发现链HTML注释导航) + 天大生科(姓名前置标签链)
REGRESSION_SET = [
    ("上海科技大学", "人文科学研究院", "https://ih.shanghaitech.edu.cn/"),
    ("北京化工大学", "机电工程学院", "https://mech.buct.edu.cn/"),
    ("上海大学", "上海温哥华电影学院", "http://www.shvfs.cn/"),
    ("天津大学", "医学部-生命科学学院", "http://smxy.tju.edu.cn/"),
]
# 期望形态表(非 ok 回归目标): 温影=发现链修复目标(师资页可找到即达标,
# 该页真实发布仅2名, few_teachers 是正确基线而非退化)
REGRESSION_EXPECT = {
    ("上海大学", "上海温哥华电影学院"): ("few_teachers", 1),
}


def _reg_pass(r):
    _exp = REGRESSION_EXPECT.get((r["school"], r["dept"]))
    if _exp:
        return r["form"] == _exp[0] and r["n"] >= _exp[1]
    return r["form"] == "ok"

_SAMPLE_TIMEOUT = 90  # 单样本诊断硬边界(秒)


def _fetch(url, timeout=20):
    """轻量抓取: curl_cffi; JS 跳转链跟随(北理工化学 szdw→gccrc 实锤);
    JS 壳(loader/content_container)走 AES-GCM 纯CPU破解; 微型壳走
    smart_fetch 破解链+接口探测"""
    try:
        import curl_cffi.requests as cr
        r = cr.get(url, impersonate="chrome110", timeout=timeout, verify=False)
        if r.status_code == 200 and len(r.text) > 500:
            # 2026-09-16 JS 跳转壳: window.location.href 逐级跟随(≤6 级,
            # 人大财金 szdw→zzjs→axm→qb 四级链实锤 — 3 级上限停在 axm 壳)
            _hop = 0
            while _hop < 6:
                _m = re.search(r"""window\.location\.href\s*=\s*['"]([^'"]+)""", r.text)
                if not _m:
                    break
                try:
                    _nu = urljoin(url, _m.group(1))
                    _r2 = cr.get(_nu, impersonate="chrome110", timeout=timeout, verify=False)
                    if _r2.status_code == 200 and len(_r2.text) > 500:
                        r = _r2
                        url = _nu
                        _hop += 1
                        continue
                except Exception:
                    pass
                break
            try:
                from util.loader_challenge import is_shell as _is_shell
                if _is_shell(r.text):
                    from util.loader_challenge import fetch as _lc_fetch
                    _h = _lc_fetch(url)
                    if _h and len(_h) > 1000:
                        return _h
            except Exception:
                pass
            # 2026-09-16 微型壳(JS渲染空壳) → smart_fetch 全链(破解链+渲染)
            if len(r.text) < 3000:
                try:
                    from util.smart_fetch import smart_fetch
                    _h, _m = smart_fetch(url, timeout=25)
                    if _h and len(_h) > 3000:
                        return _h
                except Exception:
                    pass
            return r.text
    except Exception:
        pass
    return None


def _sample_zombies(n, all_remaining=False):
    """取样: 队列 zombie 按 attempts 降序, 跳过已诊断(断点续跑)"""
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                _r = json.loads(line)
                done.add((_r.get("school"), _r.get("dept")))
            except Exception:
                pass
    q = json.loads(QUEUE_F.read_text(encoding="utf-8"))
    rows = [(d.get("attempts", 0), sch, dn, d.get("url", ""))
            for sch, depts in q.items() for dn, d in depts.items()
            if d.get("zombie") and (sch, dn) not in done]
    rows.sort(key=lambda x: -x[0])
    return [(s, d, u, a) for a, s, d, u in rows[:n]] if not all_remaining \
        else [(s, d, u, a) for a, s, d, u in rows]


_DEPT_TYPE_CACHE = None


def _load_dept_types():
    """departments.csv 院系名→类型映射(全/半角斜杠归一, 与 evaluate 同口径)"""
    global _DEPT_TYPE_CACHE
    if _DEPT_TYPE_CACHE is not None:
        return _DEPT_TYPE_CACHE
    m = {}
    try:
        import csv as _csv
        from _paths import ROOT as _ROOT
        p = _ROOT.parent / "colleges department" / "depart_985211.csv"
        if p.exists():
            for r in _csv.DictReader(open(p, encoding="utf-8-sig", errors="replace")):
                nm = (r.get("院系名称") or "").replace("／", "/").replace("　", " ").strip()
                m[nm] = r.get("院系类型", "")
    except Exception:
        pass
    _DEPT_TYPE_CACHE = m
    return m


def _dept_type(dept):
    m = _load_dept_types()
    k = (dept or "").replace("／", "/").replace("　", " ").strip()
    if k in m:
        return m[k]
    # 前缀回退(目录名装饰后缀, 与 evaluate 同)
    for nm, t in m.items():
        if len(nm) >= 4 and k.startswith(nm):
            return t
    return ""


def _sys_curl_ok(url, timeout=12):
    """系统 curl 探测(与 _fetch 的 curl_cffi 指纹路径独立): 区分
    「站点真死」与「我方请求栈问题」。返回 (code, ok)"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), "-L", url],
            capture_output=True, text=True, timeout=timeout + 5)
        code = r.stdout.strip()
        return code, (code.startswith("2") or code.startswith("3"))
    except Exception:
        return "000", False


def _root_url(url):
    """院系子域 → 学校根域(www 前缀): art.cnu.edu.cn → https://www.cnu.edu.cn/
    *.edu.cn 等三标签后缀取末 3 段, 其余取末 2 段"""
    import urllib.parse as _up
    host = _up.urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("edu", "com", "net", "org", "gov") and parts[-1] == "cn":
        root = ".".join(parts[-3:])
    elif len(parts) >= 2:
        root = ".".join(parts[-2:])
    else:
        return url
    return f"https://www.{root}/"


def _classify(r):
    """失败形态分类 → form 字段"""
    if not r["home_ok"]:
        # 2026-09-18 批次6: net_fail 四分 — 系统curl可得状态码区分:
        # 可达=瞬时网络(回队) / 412=瑞数挑战族(09-11政策维持blocked不投代码)
        # / 子站死根域活=公网不可达(账本) / 全校不可达=真net_fail
        code, ok = _sys_curl_ok(r["url"])
        if ok:
            return "net_recovered"
        if code == "412":
            return "ruishu_412"
        # 2026-09-20 用户指令「不可达判定依据要充分」: 域名变体交叉验证 —
        # 变体 code!=000(含412)即域名活着 = URL失效(修URL回队)而非子站死。
        # 与 fix_loop._url_variants 同口径(www/scheme切换/csv同名记录)。
        try:
            from fix_loop import _url_variants as _fv
            from pathlib import Path as _P
            _d = _P("output") / r["school"] / r["dept"]
            for _v in _fv(r["url"], r["url"], _d):
                _vc, _vok = _sys_curl_ok(_v)
                if _vc != "000":
                    print(f"    🔧 {r['school']}/{r['dept']}: URL失效已修(变体活着 {_v[:50]} code={_vc})")
                    return "net_recovered"  # 修URL后回队, 落账本为回队而非冻结
        except Exception:
            pass
        _, root_ok = _sys_curl_ok(_root_url(r["url"]))
        if root_ok and code == "000":
            return "subnet_dead"
        return "net_fail"
    if not r["fac_url"]:
        # 2026-09-18 批次3/7: JS-SPA(0锚首页)与 JS菜单族(js锚菜单) 与
        # no_faculty 区分 — 浏览器渲染族在纯CPU期空转无意义
        if r.get("js_spa"):
            return "js_spa"
        if r.get("js_menu"):
            return "js_menu"
        return "no_faculty"
    if not r["fac_ok"]:
        return "fac_net"
    if r.get("js_shell"):
        return "js_api"
    if r["n"] == 0:
        return "fac_empty"
    if r["pollution"]:
        return "pollution"
    if r["n"] < 10:
        # 2026-09-15 分层(用户改进意见): 系/学部/中心级 <10 人 = 真小组织
        # (中石油各系3人实锤), 走小组织核实不深挖; 学院级 <10 = 漏采嫌疑优先深挖
        if r.get("js_menu") and r.get("dept_type") in ("学院", ""):
            # 2026-09-18 批次5: 学院级静态零职称模式 = 名单在JS子菜单, 浏览器族
            return "js_menu"
        return "small_org" if r.get("dept_type") not in ("学院", "") else "few_teachers"
    return "ok"


def diagnose(school, dept, dept_url, label=""):
    """核心路径诊断: 首页→发现链→师资页→分页→提取(CSS容器优先+多形态)"""
    t0 = time.time()
    r = {"school": school, "dept": dept, "url": dept_url, "label": label,
         "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "dept_type": _dept_type(dept),
         "home_ok": False, "fac_url": None, "fac_ok": False, "n": 0,
         "pages": 0, "sample": [], "pollution": [], "form": None}
    from util.spider import find_faculty_page, collect_group_pages
    from util.teacher_extract import (extract_teachers_multi, pager_pages,
                                      list_pages, NAV_WORDS)
    home = _fetch(dept_url)
    r["home_ok"] = bool(home)
    if not home:
        r["form"] = _classify(r)
        return r
    fac = find_faculty_page(dept_url, home)
    if not fac:
        # 2026-09-18 批次3: JS-SPA 族检测(上交传媒/南信大地理实锤) — 首页
        # 无任何 <a> 锚(菜单全由 JS 从数据渲染), 静态发现链必败, 归 js_spa
        # (浏览器渲染族, 纯CPU期不该空转)
        try:
            from bs4 import BeautifulSoup as _BS3
            _spa = _BS3(home, "lxml")
            _as = _spa.find_all("a", href=True)
            if len(_as) == 0 and len(home) > 1000:
                r["js_spa"] = True
            else:
                # 2026-09-18 批次7: JS菜单族(no_faculty 池 31/60 实锤) — 有锚
                # 但菜单是 javascript:/# 短锚(≥3个)且真实锚零师资词 → 师资菜单
                # 由JS展开, 静态发现链拿不到 → 归 js_menu 浏览器族
                from util.spider import FACULTY_KW_LONG as _FKL, FACULTY_KW_SHORT as _FKS
                _jsm = [a for a in _as
                        if (a.get("href") or "").startswith(("javascript", "#"))
                        and len(a.get_text(strip=True)) <= 8]
                _fac_a = [a for a in _as
                          if not (a.get("href") or "").startswith(("javascript", "#"))
                          and any(_k in a.get_text(strip=True) for _k in _FKL + _FKS)]
                if len(_jsm) >= 3 and not _fac_a:
                    r["js_menu"] = True
        except Exception:
            pass
    r["fac_url"] = fac
    if not fac:
        r["form"] = _classify(r)
        return r
    fhtml = _fetch(fac)
    r["fac_ok"] = bool(fhtml)
    if not fhtml:
        r["form"] = _classify(r)
        return r
    # 同组分类页 + 分页页(发现链单源: collect_group_pages 与 fix_loop 同口径)
    cand = [fac]
    try:
        _groups = collect_group_pages(fac, fhtml, exclude=[fac])
        cand += _groups[:6]
    except Exception:
        pass
    # 2026-09-16 北师大哲学实锤: 师资子栏目链接在**首页** JS 菜单 HTML 里
    # (9 个研究所), fac 壳页(1565B)无子链接 — 首页也跑分类收集(研究所/系/
    # 职称档全形态), 判 js_api 前先看首页子菜单
    try:
        _hg = collect_group_pages(dept_url, home, exclude=[fac])
        cand += [u for u in _hg[:8] if u not in cand]
    except Exception:
        pass
    # 分页闭包(起点真实分页 + 无分页时 list_pages 兜底)
    # 2026-09-16 北师大哲学实锤: cat_fallback=True 把 9 个研究所导航页互相当
    # 分页后续页(45 人重复提取); 分类收集已由 collect_group_pages 负责,
    # pager 一律 cat_fallback=False 只跟真实分页链接
    pg = pager_pages(fac, fhtml, cat_fallback=False)
    if len(pg) <= 1:
        cand += list_pages(fac)[1:]
    else:
        cand += [u for u in pg[1:] if u not in cand]
    cand = cand[:8]
    r["pages"] = len(cand)
    seen, names, bad = set(), [], []
    declared = 0  # 页面"共N条"声明数(验收核对: 提取数 vs 声明数)
    _visited = set()
    for u in cand:
        if time.time() - t0 > _SAMPLE_TIMEOUT:
            r["timeout"] = True
            break
        if u in _visited:
            continue
        _visited.add(u)
        html = fhtml if u == fac else _fetch(u)
        if not html:
            continue
        # 声明数(共N条/分N页)
        try:
            _m = re.search(r"共\s*(\d+)\s*条", html)
            if _m:
                declared += int(_m.group(1))
        except Exception:
            pass
        # 2026-09-16 三级目录结构(中南财经政法 学院→系→教研室实锤): 每页再
        # 收集分类子页入队(总页数上限 15 防爆炸), 教研室层教师不漏
        try:
            if len(_visited) < 15:
                _sub = collect_group_pages(u, html, exclude=_visited | set(cand))
                for _s in _sub[:4]:
                    if _s not in _visited and _s not in cand:
                        cand.append(_s)
        except Exception:
            pass
        # 每入口也跟分页(与真实 spider 一致, 矿大机电系页 22 人分 2 页实锤)
        pages = [u] + [x for x in pager_pages(u, html, cat_fallback=False)[1:] if x != u][:3]
        # 2026-09-16 南信大马院实锤: 教师名单在 iframe 内嵌平台页
        # (t-inner.nuist.edu.cn namexs.jsp, 31 人) — iframe src 入队跟随;
        # src 内 &amp; 是 HTML 转义, 须 unescape 否则请求 404
        try:
            import html as _html
            from bs4 import BeautifulSoup as _BS
            for _if in _BS(html, "lxml").find_all("iframe", src=True):
                _is = urljoin(u, _html.unescape(_if.get("src", "").strip()))
                if len(_is) > 12 and not _is.startswith("javascript") \
                        and _is not in _visited and _is not in cand \
                        and len(_visited) < 15:
                    cand.append(_is)
        except Exception:
            pass
        # 2026-09-16 校级教师平台 API(清华精仪 89 人实锤): 页含 teacher.jsp
        # 链接 → tsites advancesearch 接口全量名单 — js_api 类救命路径
        try:
            from util.faculty_api import (detect_faculty_api, fetch_faculty_api,
                                          probe_js_endpoints)
            _cfg = detect_faculty_api(html, u)
            if not _cfg:
                # L2: JS 文件扫描 + 候选端点试请求(零浏览器, 2026-09-16 用户
                # 指令: 探测 json api 请求老师信息 — 端点常在 JS 动态构造)
                _cfg = probe_js_endpoints(html, u)
            if _cfg:
                for _t in fetch_faculty_api(_cfg, u, dept_filter=dept):
                    if _t[0] and _t[0] not in NAV_WORDS and _t[0] not in seen:
                        seen.add(_t[0])
                        names.append(_t)
        except Exception:
            pass
        for p in pages:
            if time.time() - t0 > _SAMPLE_TIMEOUT:
                r["timeout"] = True
                break
            # 2026-09-16 bug修复: 入口页 u 已在 _visited(循环开头 add), 但 pages[0]
            # 就是 u — 旧检查 `if p in _visited: continue` 把入口页提取整个跳过
            # (163 个 few_teachers 退化成 0 提取的根因); 入口页必须提取
            if p != u and p in _visited:
                continue
            _visited.add(p)
            ph = html if p == u else _fetch(p)
            if not ph:
                continue
            # 2026-09-16 北科工程技术研究院实锤(用户指令: 排除领导): fac 跳转
            # 到"现任领导"页, 领导姓名混入教师名单 — 领导页不提取(分组收集
            # 照跑, 名单在学者/梯队子页)
            _pt = re.search(r"<title>(.*?)</title>", ph, re.S)
            if _pt and re.search(r"现任领导|领导班子|学院领导|领导团队|领导介绍",
                                 _pt.group(1)):
                continue
            for t in extract_teachers_multi(ph, p):
                if not t or not t[0]:
                    continue
                if t[0] in NAV_WORDS:
                    bad.append(t[0])
                    continue
                if t[0] not in seen:
                    seen.add(t[0])
                    names.append(t)
    r["n"] = len(names)
    r["sample"] = [t[0] for t in names[:5]]
    r["pollution"] = bad[:5]
    # 2026-09-16 用户指令: 判"空"前必须浏览器渲染验证 — 静态提取 0 时
    # 打开浏览器等网络空闲(页面稳定), ①捕获 XHR 教师 JSON 端点复现 API
    # (优先) ②渲染后 HTML 兜底提取 ③两者皆空 = 真无教师数据(browser
    # verified empty, 验证环节结论)。信号量节流(浏览器 tab 有限, 20 并发
    # 全开会挤爆 9222), 开发期跑完统一清理浏览器
    if r["n"] == 0 and fhtml and not r.get("js_shell"):
        try:
            if _RENDER_SEM.acquire(timeout=20):
                try:
                    from util.faculty_api import render_and_probe, fetch_faculty_api
                    _rh, _rcfg, _rcfgs = render_and_probe(fac)
                    if _rh and len(_rh) > 3000:
                        r["rendered"] = True
                        r["api_cfgs"] = len(_rcfgs)
                        if _rcfg:
                            for _t in fetch_faculty_api(_rcfg, fac, dept_filter=dept):
                                if _t[0] and _t[0] not in NAV_WORDS \
                                        and _t[0] not in seen:
                                    seen.add(_t[0])
                                    names.append(_t)
                        if not names:
                            for _t in extract_teachers_multi(_rh, fac):
                                if _t[0] and _t[0] not in NAV_WORDS \
                                        and _t[0] not in seen:
                                    seen.add(_t[0])
                                    names.append(_t)
                        if not names:
                            r["browser_verified_empty"] = True
                finally:
                    _RENDER_SEM.release()
        except Exception:
            pass
        r["n"] = len(names)
        r["sample"] = [t[0] for t in names[:5]]
    # 2026-09-16 验收核对(用户指令: 确保无遗漏): 页面声明数 vs 提取数
    r["declared"] = declared
    if declared > 0 and len(names) < declared * 0.8:
        r["under_declared"] = True
    # 2026-09-15 JS 壳检测(地大北京 scholar.cugb 实锤): 教师列表数据 JS 加载,
    # 静态 HTML 只有搜索框壳(学者成果库/检索/登录) — 归 js_api 形态,
    # 与 fac_empty(目录页没收集子页)区分, 走 smart_fetch 渲染链/VSB 专项
    if r["n"] == 0 and fhtml:
        try:
            _s = BeautifulSoup(fhtml, "lxml")
            _t = _s.get_text(" ", strip=True)
            _short = [a for a in _s.find_all("a", href=True)
                      if len(a.get_text(strip=True)) <= 6]
            if re.search(r"检索|搜索|登录|成果库|学者", _t) and len(_short) < 20 \
                    and not re.search(r"下一页|尾页", _t):
                r["js_shell"] = True
        except Exception:
            pass
    # 2026-09-18 批次5: JS菜单族检测(南林马院 szgk/协和群公卫 szdw1 实锤) —
    # 学院级师资页静态 HTML 零「姓名+职称」模式(名单在 JS 子菜单/子页),
    # 与真小院系区分: 小院系页至少有几条职称姓名行
    if 0 < r["n"] < 10 and fhtml and _dept_type(dept) in ("学院", ""):
        try:
            from bs4 import BeautifulSoup as _BS5
            _s5 = _BS5(fhtml, "lxml")
            for _tag in _s5(["script", "style"]):
                _tag.decompose()
            _txt5 = _s5.get_text(" ", strip=True)
            if not re.search(r"[一-鿿·]{2,4}\s*(?:教授|副教授|讲师|助教|研究员|高级工程师|博导|硕导)", _txt5):
                r["js_menu"] = True
        except Exception:
            pass
    r["form"] = _classify(r)
    # 2026-09-15 工作流自改进: 低产形态(fac_empty/few_teachers)自动收集线索
    # (标题/子菜单/职称信号行/分页控件), 批后直接看 jsonl 定位形态,
    # 不必再逐样本手动 curl 深挖
    if r["form"] in ("fac_empty", "few_teachers"):
        try:
            _s = BeautifulSoup(fhtml, "lxml")
            _cl = {"title": (_s.title.get_text(strip=True) if _s.title else "")[:50]}
            _sub = []
            for _a in _s.find_all("a", href=True):
                _t = _a.get_text(strip=True)
                if re.search(r"教授|导师|教师|博士后|讲师", _t) and len(_t) <= 16:
                    _sub.append(_t)
                    if len(_sub) >= 6:
                        break
            _cl["submenu"] = _sub
            _lines = [l.strip() for l in _s.get_text("\n", strip=True).split("\n") if l.strip()]
            _cl["title_lines"] = [l[:46] for l in _lines
                                  if re.search(r"教授|导师|博士", l)][:4]
            from util.teacher_extract import _is_pager_text
            _cl["has_pager"] = any(_is_pager_text(_a.get_text(strip=True))
                                   for _a in _s.find_all("a"))
            r["clues"] = _cl
        except Exception:
            pass
    r["secs"] = round(time.time() - t0, 1)
    return r


def _latest_rows():
    """每样本取最新一行(重测记录覆盖旧诊断), 统计口径=样本最新状态"""
    latest = {}
    for l in RESULTS.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            r = json.loads(l)
            latest[(r["school"], r["dept"])] = r
        except Exception:
            pass
    return list(latest.values())


def _report():
    from collections import Counter
    rows = _latest_rows()
    c = Counter(r["form"] for r in rows)
    print(f"已诊断 {len(rows)} 个僵尸院系(每样本最新状态):\n")
    for form, n in c.most_common():
        print(f"  {form:14s} {n}")
    for form in ("no_faculty", "fac_empty", "few_teachers", "small_org", "pollution",
                 "fac_net", "js_api", "js_spa", "js_menu", "net_recovered", "subnet_dead"):
        subs = [r for r in rows if r["form"] == form][:6]
        if not subs:
            continue
        print(f"\n── {form} 样本 ──")
        for r in subs:
            print(f"  {r['school']}/{r['dept']}: fac={r.get('fac_url','-') or '-'} "
                  f"n={r.get('n',0)} 样本={r.get('sample','')[:3]} 污染={r.get('pollution','')}")
            if r.get("clues"):
                c = r["clues"]
                print(f"    线索: title={c.get('title','')!r} 子菜单={c.get('submenu','')[:4]} "
                      f"分页={c.get('has_pager')} 信号行={c.get('title_lines','')[:2]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--loop", type=int, default=0, help="自动连跑 N 批(每批10个)")
    ap.add_argument("--all", action="store_true", help="全量测试剩余僵尸(20并发)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--regression", action="store_true")
    ap.add_argument("--retest", action="store_true", help="重测已诊断的低产形态(修复后验证)")
    ap.add_argument("--reap", action="store_true", help="摘除诊断 ok 的僵尸(闭环收割)")
    ap.add_argument("--terminalize-empty", action="store_true",
                    help="browser_verified_empty 僵尸终态化(skip: 浏览器三查确认无教师数据)")
    ap.add_argument("--readmit-fails", action="store_true", help="fail 终态院系回队列重试")
    ap.add_argument("--dept", nargs=2, metavar=("学校", "院系"))
    args = ap.parse_args()

    if args.report:
        _report()
        return
    if args.regression:
        rows = []
        for sch, dept, url in REGRESSION_SET:
            r = diagnose(sch, dept, url, label="regression")
            rows.append(r)
            flag = "✅" if _reg_pass(r) else "⚠️"
            print(f"{flag} {sch}/{dept}: {r['form']} n={r['n']} "
                  f"污染={r.get('pollution','')}")
        bad = [r for r in rows if not _reg_pass(r)]
        print("\n回归通过" if not bad else f"\n⚠️ 回归退化 {len(bad)} 个, 需查")
        return
    if args.reap:
        # 闭环收割: 诊断 ok 的僵尸**解除僵尸标记**(不是删除) — 诊断只验证了
        # 发现+提取核心路径, 完整 fix_one(评估+LLM终审)需恢复采集后跑一轮确认;
        # 解标记+attempts清零 → 恢复采集时按普通队列条目正常处理
        import fcntl
        ok = [r for r in _latest_rows() if r["form"] == "ok"]
        with open(QUEUE_F, "r+", encoding="utf-8") as fq:
            fcntl.flock(fq, fcntl.LOCK_EX)
            q = json.load(fq)
            n_zombie = n_reset = 0
            for r in ok:
                sch, dept = r["school"], r["dept"]
                if sch in q and dept in q[sch]:
                    d = q[sch][dept]
                    if d.get("zombie"):
                        d["zombie"] = False
                        n_zombie += 1
                    d["attempts"] = 0
                    n_reset += 1
            fq.seek(0)
            fq.truncate()
            json.dump(q, fq, ensure_ascii=False, indent=1)
            fcntl.flock(fq, fcntl.LOCK_UN)
        print(f"✅ 收割: 解除 {n_zombie} 个僵尸标记, 清零 {n_reset} 个 attempts"
              f" (诊断 ok, 恢复采集后正常处理验证)")
        return

    if args.terminalize_empty:
        # 2026-09-18 批次4: browser_verified_empty = 静态+API+浏览器渲染三查皆空
        # 的真无数据终态(render-verify 工作流判定) — 僵尸池 35 个此类院系
        # 若继续重试纯属空转, 终态化 skip 止损(与 --reap 解标记不同, 这里直接
        # 落 .skip_mark 出队; 站点未来上线名单时清除 skip 重采即可)
        import fcntl
        out = Path('output')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ve = [r for r in _latest_rows() if r.get("browser_verified_empty")]
        n = 0
        with open(QUEUE_F, "r+", encoding="utf-8") as fq:
            fcntl.flock(fq, fcntl.LOCK_EX)
            q = json.load(fq)
            for r in ve:
                sch, dept = r["school"], r["dept"]
                if sch not in q or dept not in q[sch]:
                    continue
                if not q[sch][dept].get("zombie"):
                    continue
                del q[sch][dept]
                if not q[sch]:
                    del q[sch]
                dd = out / sch / dept
                dd.mkdir(parents=True, exist_ok=True)
                (dd / ".skip_mark").write_text(
                    now + " 浏览器三查确认无教师数据(静态/API/渲染均空, 攻坚批次4)", encoding="utf-8")
                n += 1
            fq.seek(0)
            fq.truncate()
            json.dump(q, fq, ensure_ascii=False, indent=1)
            fcntl.flock(fq, fcntl.LOCK_UN)
        print(f"✅ 终态化: {n} 个 browser_verified_empty 僵尸 → skip 出队")
        return

    if args.readmit_fails:
        # 2026-09-15 核查实锤: 210 个 .fail_mark 终态院系 **全部不在失败队列**
        # (在队列 0/210) — fail 终态=一次性判死永不重试, 与"重试确认"矛盾:
        # 191/210 CSV 里有教师数据(质量不达标被数量硬规则判漏采), 16 个纯
        # IP 封禁(网络类误落终态)。改进期修复的提取器缺陷对它们同样生效,
        # 必须回队列重试 — fail 语义从"终态"修正为"待重试"
        import csv as _csv
        out = Path('output')
        q = json.loads(QUEUE_F.read_text(encoding="utf-8"))
        n_new = n_exists = 0
        for f in out.glob('*/*/.fail_mark'):
            sch, dept = f.parent.parent.name, f.parent.name
            if sch in q and dept in q[sch]:
                n_exists += 1
                continue
            # 从 info.csv / departments.csv 找 URL
            url = ""
            try:
                infof = f.parent / 'info.csv'
                if infof.exists():
                    for r in _csv.DictReader(open(infof, encoding='utf-8', errors='replace')):
                        if (r.get('dept') or r.get('院系名称') or '').strip() == dept:
                            url = (r.get('url') or r.get('院系URL') or '').strip()
                            break
            except Exception:
                pass
            if not url:
                # 从上游 CSV 按院系名找
                try:
                    with open('../colleges department/depart_985211.csv',
                              encoding='utf-8-sig', errors='replace') as _f:
                        for r in _csv.DictReader(_f):
                            if (r.get('院系名称') or '').strip() == dept:
                                url = (r.get('院系URL') or '').strip()
                                break
                except Exception:
                    pass
            if not url:
                continue
            q.setdefault(sch, {})[dept] = {"url": url, "at": "",
                                           "zombie": True, "attempts": 0,
                                           "reason": "fail终态重读(2026-09-15核查: 回队列重试)",
                                           "streak": 0, "slow": False}
            n_new += 1
        QUEUE_F.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ fail 终态重读: 回队列 {n_new} 个 (已在队列 {n_exists}, 无URL跳过 {210-n_new-n_exists})")
        return

    if args.dept:
        sch, dept = args.dept
        q = json.loads(QUEUE_F.read_text(encoding="utf-8"))
        url = ""
        for _sch, depts in q.items():
            if _sch == sch and dept in depts:
                url = depts[dept].get("url", "")
        if not url:
            print(f"队列中无 {sch}/{dept}")
            return
        r = diagnose(sch, dept, url)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if args.retest:
        # 2026-09-16 工作流自改进: ①重测前先行回归集(修复 bug 当场暴露,
        # 163 样本退化教训) ②退化断言(有教师→0提取 的翻转自动告警)
        from concurrent.futures import ThreadPoolExecutor
        _reg_ok = True
        for _sch, _dept, _url in REGRESSION_SET:
            _rr = diagnose(_sch, _dept, _url, label="regression")
            _flag = "✅" if _reg_pass(_rr) else "🚨"
            if not _reg_pass(_rr):
                _reg_ok = False
            print(f"{_flag} 回归 {_sch}/{_dept}: {_rr['form']} n={_rr['n']}", flush=True)
        if not _reg_ok:
            print("🚨 回归集退化, 修复代码再重测 — 中止本轮 retest", flush=True)
            return
        rows = _latest_rows()
        low = [r for r in rows if r.get("form") in ("fac_empty", "few_teachers", "small_org", "pollution", "no_faculty")]
        print(f"重测 {len(low)} 个低产样本(修复后复测, 20 并发):\n", flush=True)
        n_deg = 0
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(diagnose, r["school"], r["dept"], r["url"], label="retest"): r
                    for r in low}
            with open(RESULTS, "a", encoding="utf-8") as f:
                for fu in futs:
                    try:
                        r2 = fu.result()
                    except Exception:
                        continue
                    r0 = futs[fu]
                    r2["prev_form"] = r0.get("form")
                    f.write(json.dumps(r2, ensure_ascii=False) + "\n")
                    f.flush()
                    chg = "" if r2["form"] == r0.get("form") else f"  ← 原 {r0.get('form')}"
                    # 退化断言: 原样本有教师(n>0) 复测变 0 提取 → 告警(代码引入
                    # bug 的典型信号, 2026-09-16 三级递归 visited 误跳过实锤)
                    if r0.get("n", 0) > 0 and r2["n"] == 0:
                        n_deg += 1
                        print(f"  🚨退化 {r2['school']}/{r2['dept']}: "
                              f"{r0.get('form')} n={r0.get('n')} → fac_empty 0{chg}")
                        continue
                    print(f"  {r2['form']:12s} n={r2['n']:3d} {r2['school']}/{r2['dept']}{chg}")
        if n_deg:
            print(f"\n🚨 退化告警: {n_deg} 个样本 有教师→0提取, 检查本次代码改动")
        return

    if args.all:
        # 2026-09-15 用户指令: "fail 与僵尸全部重启加入测试循环, 20并发" —
        # 全量跑完剩余未测僵尸(20 并发), 断点续跑(已测跳过)
        from collections import Counter
        from concurrent.futures import ThreadPoolExecutor
        samples = _sample_zombies(0, all_remaining=True)
        print(f"全量测试: {len(samples)} 个剩余僵尸, 20 并发", flush=True)
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(diagnose, s, d, u): (s, d, u, a)
                    for s, d, u, a in samples}
            done_n = 0
            with open(RESULTS, "a", encoding="utf-8") as f:
                for fu in futs:
                    try:
                        r = fu.result()
                    except Exception:
                        continue
                    r["attempts"] = futs[fu][3]
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    f.flush()
                    done_n += 1
                    if done_n % 50 == 0:
                        rows = _latest_rows()
                        c = Counter(x["form"] for x in rows)
                        print(f"[{done_n}/{len(samples)}] 累计 {len(rows)}: "
                              + " ".join(f"{k}={v}" for k, v in c.most_common()),
                              flush=True)
        rows = _latest_rows()
        c = Counter(x["form"] for x in rows)
        print(f"完成 {done_n} 个, 累计 {len(rows)}: "
              + " ".join(f"{k}={v}" for k, v in c.most_common()), flush=True)
        return

    if args.loop:
        # 自动连跑 N 批(每批 10 个): 用户指令"每批次10个, 先测试10个批次"
        # 批间形态修复由人工/agent 介入, 修复后 --retest 验证复活
        # 2026-09-15 提速: 3 并发诊断(轻量 curl 无资源压力, 90s 超时样本
        # 不再串行阻塞整批)
        from collections import Counter
        from concurrent.futures import ThreadPoolExecutor
        for bi in range(1, args.loop + 1):
            samples = _sample_zombies(10)
            if not samples:
                print("样本池耗尽或全部已诊断")
                break
            with ThreadPoolExecutor(max_workers=20) as ex:
                futs = {ex.submit(diagnose, s, d, u): (s, d, u, a)
                        for s, d, u, a in samples}
                with open(RESULTS, "a", encoding="utf-8") as f:
                    for fu in futs:
                        r = fu.result()
                        r["attempts"] = futs[fu][3]
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                        f.flush()
            rows = _latest_rows()
            c = Counter(x["form"] for x in rows)
            print(f"[批 {bi}] 累计 {len(rows)}: "
                  + " ".join(f"{k}={v}" for k, v in c.most_common()), flush=True)
        return

    samples = _sample_zombies(args.n)
    if not samples:
        print("无未诊断僵尸(全部已测过) — 可用 --report 看汇总")
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(diagnose, s, d, u): (s, d, u, a)
                for s, d, u, a in samples}
        with open(RESULTS, "a", encoding="utf-8") as f:
            for fu in futs:
                r = fu.result()
                r["attempts"] = futs[fu][3]
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                print(f"  {r['form']:12s} att={futs[fu][3]:3d} {r['school']}/{r['dept']} "
                      f"n={r['n']} fac={(r.get('fac_url') or '-')[:52]}")
    print("\n── 本轮形态汇总 ──")
    _report()


if __name__ == "__main__":
    main()
