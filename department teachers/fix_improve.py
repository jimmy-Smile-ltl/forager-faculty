#!/usr/bin/env python3
"""生成器+优化器 — 2026-08-31 从 fix_loop 拆出。

SPIDER_TEMPLATE + generate_spider: R0 确定性 spider 薄壳(请求走 util.smart_fetch,
提取/写盘/分页走 util.teacher_extract);
_code_view: 代码视图裁剪(头600+尾2400, 避开 NAV_WORDS 中段);
llm_improve_spider: R3 带评估原因 + analysis.md 上下文让 LLM 改进 spider;
_sync_meta: 溯源一致性补写 spider_meta.json(hash 校验)。
"""
import csv, hashlib, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from collect_teacher import get_llm, parse_json, llm_ask_with_timeout  # noqa: E402
# ── 生成器 G: 确定性 spider 模板 ──
SPIDER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教师采集 spider — {school} {dept} (薄壳模板: 通用能力全部复用 util)

设计约定: spider 只写站点特定逻辑; 请求走 util.smart_fetch,
提取/写盘/分页走 util.teacher_extract — 禁止内联实现通用代码"""
import sys as _sys
from pathlib import Path as _P
for _p in [_P(__file__).resolve()] + list(_P(__file__).resolve().parents):
    if (_p / "util" / "smart_fetch.py").exists():
        _sys.path.insert(0, str(_p))
        break
from util.smart_fetch import smart_fetch
from util.teacher_extract import NAV_WORDS, clean_name, extract_teachers_multi, write_outputs, list_pages, pager_pages

SCHOOL = {school!r}
DEPT = {dept!r}
FACULTY_URLS = {fac_urls!r}
DEPARTMENT_URL = {dept_url!r}
# 2026-08-26: POST AJAX 教师接口(交大计院类 JS 空壳站: HTML 无教师, 数据走
# /active/ajax_teacher_list.html 等接口)。probe 已写入 analysis.md **AJAX教师API**。
FACULTY_API = {faculty_api!r}


def fetch(url, timeout=45):
    # 2026-09-10 用户指令"网络请求等待时间加长": 20→45 — 慢握手/限速站
    # 20s 内拿不到首字节, 放宽后瑞数求解与慢页有完整窗口完成

    """统一请求层: smart_fetch 四级降级(见 util/smart_fetch.py)。
    2026-08-31 三层增强: ①正方CMS频道树 JS 跳转链跟随(北理工机电:
    szml→wrfhgcx→qb01 逐级 location.href 跳, 终页才含名单, 纯 HTTP 跟随)
    ②loader 挑战纯 HTTP 破解(西交"正在加载"壳) ③Playwright 渲染兜底。"""
    import re as _re
    from urllib.parse import urljoin as _urljoin
    html, _m = smart_fetch(url, timeout=timeout)
    # ① JS 跳转链: window.location.href 逐级跟随(≤3 级, 终页为名单页)
    _hop, _sess = 0, None
    while html and _hop < 3:
        _m2 = _re.search(r"""window\\.location\\.href\\s*=\\s*=['"]([^'"]+)""", html)
        if not _m2:
            break
        try:
            import curl_cffi.requests as _cr
            if _sess is None:
                _sess = _cr.Session()
            _r = _sess.get(_urljoin(url, _m2.group(1)),
                           headers={{"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"}},
                           impersonate="chrome110", timeout=20)
            if _r.status_code == 200 and _r.text:
                html = _r.text
                _hop += 1
                continue
        except Exception:
            pass
        break
    # 2026-08-31: 四类情况转纯 CPU 兜底(2026-09-10 起禁浏览器) —
    # ①JS挑战加载器壳("正在加载"); ②瑞数/WAF 掐断(blocked);
    # ③空回复; ④微型错误页(403/404 96B 等) — 走 smart_fetch 破解链
    if (_m == "blocked" or not html or len(html) < 500
            or (len(html) < 8000
                and ("正在加载" in html or 'class="loader"' in html))):
        html = _render_js(url) or html
    return html


def _render_js(url):
    # 2026-09-10 铁律: spider 禁止启动任何浏览器(playwright/DrissionPage/
    # Chrome/9222/9333) — 15 并发下每窗口 9-11 进程, 内存直接爆炸; 浏览器
    # 是全局「最后手段」, 由采集侧统一控制, spider 内绝不直接使用。
    # 瑞数/JS 挑战改走 util.smart_fetch 纯 CPU 破解链: iv8(V8 原生解瑞数,
    # 优先级最高) → nodenv(补环境) → RsSpider sdenv; 浏览器层由
    # BROWSER_TIERS 总开关控制(默认 off, 并发采集期恒 off)。
    try:
        from util.smart_fetch import smart_fetch
        _html, _m = smart_fetch(url, timeout=60)
        if _html and len(_html) > 500:
            return _html
    except Exception:
        pass
    return None


def fetch_api(url, data, timeout=25):
    """POST 教师列表 AJAX 接口 — 空壳站教师数据全量在此(308人/交大计院实测)。
    响应常为 JSON 内嵌转义 HTML(tab_html/content 字段), 先解码成真实 HTML 再提取。"""
    import requests
    try:
        # 2026-10-02 上交电气实锤: 端点要求 PHPSESSID 会话 — 先 GET 页面建会话
        # 再 POST, 无 cookie 返回空名单
        _sess = requests.Session()
        try:
            _sess.get(FACULTY_URLS[0] if FACULTY_URLS else url, timeout=10,
                      headers={{'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'}})
        except Exception:
            pass
        r = _sess.post(url, data=data, timeout=timeout,
                       headers={{'Referer': (FACULTY_URLS[0] if FACULTY_URLS else url),
                                 'X-Requested-With': 'XMLHttpRequest',
                                 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'}})
        if not (r.ok and r.text):
            return ""
        try:
            import json as _json
            _j = _json.loads(r.text)
            if isinstance(_j, dict):
                return " ".join(v for v in _j.values() if isinstance(v, str))
        except Exception:
            pass
        return r.text
    except Exception:
        return ""


def main():
    teachers, seen = [], set()
    _all_html = []  # 已抓 HTML, 用于内嵌师资接口兜底(wp3/SCUT deptTeacherList)
    # ── AJAX 接口优先: 空壳站 HTML 提取必然为 0, 直接走数据接口 ──
    if FACULTY_API:
        _api_url = FACULTY_API.get('url', '')
        _api_data = dict(FACULTY_API.get('data', {{}}))
        for _page in range(1, 6):
            _html = fetch_api(_api_url, dict(_api_data, page=_page))
            if not _html:
                break
            _got = extract_teachers_multi(_html, _api_url)
            _new = [t for t in _got if t[0] not in seen]
            for t in _new:
                seen.add(t[0])
                teachers.append([t[0], t[1], t[2], t[3], _api_url])
            if not _got:
                break
        if teachers:
            write_outputs(SCHOOL, DEPT, teachers)
            print(f"Found {{len(teachers)}} teachers (api)")
            return
        print("api empty, fallback to pages")
    for fu in FACULTY_URLS:
        empty_streak = 0
        _visited = set()
        # 分页(2026-08-28): 真实分页链接优先(下页/尾页/数字页码, 含 "下一页>>" 装饰),
        # 无真实链接时 list_pages 兜底(URL形态: listN/子目录N/后缀编号); 沿真实链接
        # 逐页前进(多页站点), 连续2空页或请求失败立即break — 禁止只抓第一页
        _html0 = fetch(fu)
        if not _html0:
            print(f"fetch failed: {{fu}}")
            continue
        _all_html.append(_html0)
        # 2026-09-15 用户指令强化: 分页探测结果直接进队列 — 旧写法
        # `_cand=[fu]; if len(pager_pages)<=1: +=list_pages` 把 pager_pages
        # 首页探测结果丢弃, 首页若为空壳/JS页(提取0人触发 empty_streak
        # 熔断)则下一页永远走不到; 现改为首页分页链接(下一页/尾页/数字)
        # 直接入队列, 分页跟随不依赖首页提取成功(上科大 crjs 2页/847 3页
        # 实测: pager_pages 对 "下一页>>"/"尾页" 均正确闭包)
        _pg0 = pager_pages(fu, _html0)
        _cand = _pg0 if len(_pg0) > 1 else [fu] + list_pages(fu)[1:]
        while _cand:
            pg = _cand.pop(0)
            if pg in _visited:
                continue
            _visited.add(pg)
            html = _html0 if pg == fu else fetch(pg)
            if not html:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            if pg != fu:
                _all_html.append(html)
            got = extract_teachers_multi(html, pg)
            # 2026-09-01 前端 AJAX/JSON 下拉加载(上交 airs/shss): 静态模板只有
            # 默认tab子集(如特聘教授), 检测到 searchUrl/AjaxFaculty 端点即
            # 以 API 全量为权威(纯HTTP), 静态提取仅作 API 缺失时的兜底
            try:
                from util.faculty_api import (detect_faculty_api, fetch_faculty_api,
                                              probe_js_endpoints)
                _cfg = detect_faculty_api(html, pg)
                # 2026-10-26 国科大基础教育研究院实锤: 名单端点在外部 JS
                # (zrjs_search.js → /common/zrjsTeacher.json), detect 只看
                # 静态 HTML 会漏 — 补 L2 JS 扫描兜底
                if not _cfg:
                    _cfg = probe_js_endpoints(html, pg)
                if _cfg:
                    _rows = fetch_faculty_api(_cfg, pg)
                    if len(_rows) >= 3:
                        got = [list(r[:4]) for r in _rows]
                    elif _rows:
                        _have = set(g[0] for g in got)
                        got += [list(r[:4]) for r in _rows if r[0] not in _have]
            except Exception:
                pass
            if not got:
                # 2026-08-31 北理工系站: 正常页(无壳)但名单懒加载/滚动后才出现
                # → 渲染+滚动重取一次
                _h2 = _render_js(pg)
                if _h2:
                    got = extract_teachers_multi(_h2, pg)
            new = []
            for t in got:
                if t[0] not in seen:
                    seen.add(t[0])
                    new.append(t)
                    continue
                # 2026-09-04 协和生医工实锤: 同名已在但旧行无详情URL、新行带URL
                # (名单位无href, 详情链接在其它分页) — 旧逻辑保留首次出现,
                # URL 永远补不进来; 此处升级旧行实现跨页URL补齐
                if t[2]:
                    for _i, _old in enumerate(teachers):
                        if _old[0] == t[0] and not _old[2]:
                            teachers[_i] = [t[0], t[1], t[2], t[3], pg]
                            break
            for t in new:
                teachers.append([t[0], t[1], t[2], t[3], pg])
            if not new:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
                # 沿真实链接前进: 每页的下一页/尾页链接续走
                # 2026-09-15: 续走页不降级收分类页(crjs/list2 末页降级收进
                # 843课程体系 实锤) — 只跟真实分页链接
                _fwd = pager_pages(pg, html, cat_fallback=False)
                for _m in _fwd[1:]:
                    if _m not in _visited and _m not in _cand:
                        _cand.append(_m)
    # ── 内嵌师资接口兜底(2026-08-28): 万鹏wp3/SCUT deptTeacherList 等 XHR/JSONP
    #    名单站静态页无教师(复旦ai 289人/华工设计62人 静态仅个位数) — 探测并拉取 ──
    if len(teachers) < 8 and _all_html:
        try:
            from util.teacher_extract import (query_embedded_teacher_roster,
                                              extract_teachers_from_api)
            _base = FACULTY_URLS[0] if FACULTY_URLS else ""
            for _h in _all_html:
                _xr = query_embedded_teacher_roster(_base, _h)
                if not _xr.get("ok"):
                    continue
                _api_teachers = extract_teachers_from_api(_xr.get("teachers") or [], _base)
                if len(_api_teachers) >= 8:
                    # API 名单权威全量(复旦wp3 289/华工embedded 62) — 整体替换,
                    # 静态页个位数结果多为分类入口/职称档脏数据(复旦"初中级"教训)
                    teachers = [[t[0], t[1], t[2], t[3], _xr.get("mode", "api")]
                                for t in _api_teachers]
                    seen = {{t[0] for t in teachers}}
                else:
                    _new = []
                    for t in _api_teachers:
                        if t[0] not in seen:
                            seen.add(t[0])
                            _new.append(t)
                            continue
                        # 2026-09-04 同主循环: 同名已存在但旧行无详情URL、API行带URL
                        # → 升级旧行(URL 补齐), 不再保留首次出现的无URL行
                        if t[2]:
                            for _i, _old in enumerate(teachers):
                                if _old[0] == t[0] and not _old[2]:
                                    teachers[_i] = [t[0], t[1], t[2], t[3], _xr.get("mode", "api")]
                                    break
                    for t in _new:
                        teachers.append([t[0], t[1], t[2], t[3], _xr.get("mode", "api")])
                print(f"Found {{len(teachers)}} teachers ({{_xr.get('mode','api')}} api)")
                break
        except Exception:
            pass
    if not teachers:
        print("fetch failed or no teachers")
        return
    write_outputs(SCHOOL, DEPT, teachers)
    print(f"Found {{len(teachers)}} teachers")


if __name__ == "__main__":
    main()
'''


def generate_spider(school, dept, dept_url, fac_url, fac_urls=None, faculty_api=None):
    """生成确定性 spider.py(薄壳: 通用能力在 util, 见 SPIDER_TEMPLATE)
    fac_urls: 同组采集多入口(武大计算机 szdw/ 四分类等), 默认单入口 [fac_url]
    faculty_api: POST AJAX 教师接口 {"url":..., "data":{...}} (空壳站, 2026-08-26)"""
    if not fac_urls:
        fac_urls = [fac_url]
    else:
        # 2026-09-07 去重保序(北大CS 21 个入口含 bsh/grtx 重复 3 次 → spider
        # 超时主因之一); 去重后再前置选定页
        _seen = set()
        fac_urls = [u for u in fac_urls if not (u in _seen or _seen.add(u))]
        if fac_url not in fac_urls:
            # 2026-09-07 用户指令: 选定师资页(最高信号)必须与同组分类页一并采集 —
            # 上科大创艺 jxjs(教学教授 8人)是选定页, 同组 crjs/tpjs 等存在时
            # 旧逻辑 `if not fac_urls` 直接丢弃 fac_url → 最大分类页漏采
            fac_urls = [fac_url] + fac_urls
    # 2026-10-26 护栏(国科大资环实锤: 改进轮反复生成 FACULTY_URLS=[] 空蜘蛛,
    # 跑一轮 0 提取全靠落盘并集保名单): 生成器永不输出空列表 —
    # fac_url 为空时回退院系首页(dept_url), 保证 spider 至少有起点
    fac_urls = [u for u in fac_urls if u and str(u).startswith("http")]
    if not fac_urls:
        fac_urls = [dept_url] if dept_url and str(dept_url).startswith("http") else []
    return SPIDER_TEMPLATE.format(school=school, dept=dept, fac_urls=fac_urls,
                                  dept_url=dept_url, faculty_api=faculty_api)


# ── 优化器 O: LLM 带原因改进 spider ──
def _code_view(code: str) -> str:
    """代码视图: 头600+尾2400 — 模板生成的 spider 头部是 ~2.5KB 的 NAV_WORDS
    词表, 只截前2500字会让 LLM 完全看不到 fetch/extract/main 主体
    (实测 LLM 拒绝: "未提供spider.py代码，无法修正。")"""
    if len(code) <= 3200:
        return code
    return code[:600] + "\n# ... (中段 NAV_WORDS 等省略) ...\n" + code[-2400:]


def llm_improve_spider(school, dept, dept_url, fac_url, fac_html, current_code, fail_reasons, analysis_ctx="", fac_urls=None, faculty_api=None, round_no=0, partial_ok=""):
    """LLM 带评估原因+analysis.md 上下文改进 spider.py。
    2026-10-25 用户指令(强化retry): round_no 注入「第N轮改进」语义 —
    上轮已按相同原因修复过未解决, 本轮必须换方案, 不得重复同款修改;
    partial_ok 注入部分成功信息 — 已提取出的教师保持不动只修遗漏。"""
    def _ilog(msg):
        try:
            with open("/tmp/llm_improve.log", "a", encoding="utf-8") as _f:
                _f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass
    _ilog(f"ENTER {dept} llm={type(get_llm()).__name__ if get_llm() else 'None'} "
          f"fac_html_len={len(fac_html) if fac_html else 0} analysis_len={len(analysis_ctx)}")
    llm = get_llm()
    if not llm:
        _ilog("EXIT get_llm None")
        return None
    soup = BeautifulSoup(fac_html, "lxml")
    for t in soup.find_all(["script", "style", "noscript"]):
        t.decompose()
    body_text = soup.body.get_text()[:2500] if soup.body else ""
    links = []
    for a in soup.find_all("a", href=True)[:60]:
        t = a.get_text(strip=True)
        h = a.get("href", "")
        if t and len(t) <= 20:
            links.append({"text": t[:25], "href": h[:90]})
            if len(links) >= 25:
                break
    # 知识库失败案例(自我进化沉淀, 见 collect_teacher.record_case)。
    # 聚类注入: 全部历史案例按失败模式聚合取top3 — 只塞最近3条原始记录时,
    # LLM 只能看到当下的个例, 学不到 85 条案例里的共性(表格漏提/分页缺失等)。
    kb_ctx = ""
    try:
        kb = load_kb()
        cases = kb.get("failed_cases", {})
        if cases:
            _pat_rules = [
                ("数量不足(<20人或<3)", ["数量硬规则", "教师数<", "少于20", "教师数不足"]),
                ("非人名混入(栏目词/导航词/粘连)", ["非人名", "LLM人名终审"]),
                ("无师资页/师资页定位失败", ["无师资页"]),
                ("spider未生成CSV/崩溃", ["spider未生成", "崩溃"]),
                ("JSON/CSV不一致", ["JSON", "内容不一致"]),
                ("无教师信号(疑似合法空)", ["无教师信号", "名职对"]),
                ("LLM终审失败", ["LLM验证失败", "终审"]),
                ("教师性证据不足(疑似新闻页)", ["教师性证据", "疑似新闻"]),
            ]
            _clusters = []
            for _label, _kws in _pat_rules:
                _hits = [k for k, v in cases.items()
                         if any(_kw in json.dumps(v, ensure_ascii=False) for _kw in _kws)]
                if _hits:
                    _ex = _hits[0].split("/", 1)[-1]
                    _ex2 = _hits[1].split("/", 1)[-1] if len(_hits) > 1 else ""
                    _clusters.append(f"- [{len(_hits)}次] {_label}: 例 {_ex}{(', '+_ex2) if _ex2 else ''}")
            _clusters.sort(key=lambda s: -int(s.split("[")[1].split("次")[0]))
            kb_ctx = "历史失败模式(按频次, 避免重犯):\n" + "\n".join(_clusters[:3])
    except Exception:
        pass
    # 2026-10-25 成功案例库: 同域已PASS院系注入(同站院系页面结构相似,
    # 已成功案例是最强参考 — 比失败案例库更有引导力)
    succ_ctx = ""
    try:
        from collect_kb import load_kb as _lk
        from urllib.parse import urlparse as _up
        _host = _up(fac_url or "").netloc
        _dom = ".".join(_host.split(".")[-2:]) if _host else ""
        _same = []
        for _k, _v in (_lk().get("success_cases", {}) or {}).items():
            _vu = str(_v.get("dept_url") or "")
            _vd = ".".join(_up(_vu).netloc.split(".")[-2:]) if _vu else ""
            if _dom and _vd and _dom == _vd:
                _same.append((_k, _vu, _v.get("n", 0)))
        if _same:
            _same = _same[:3]
            succ_ctx = ("同域已成功采集案例(参考其页面结构, 不要照抄URL):\n"
                        + "\n".join(f"- {_k}: 院系URL {_u} ({_n}人)"
                                   for _k, _u, _n in _same))
    except Exception:
        pass
    prompt = f"""为下面院系生成教师采集spider.py。

院系: {dept}
师资页: {fac_url}
{'第%d轮改进: 下列是历轮评估失败原因 — 上一轮已按这些原因修复过但未解决, 本轮必须换方案(换容器/换分页策略/换URL来源), 不得重复同款选择器微调:' % round_no if round_no else '评估失败原因:'}
{chr(10).join('- ' + r[:120] for r in fail_reasons[:6])}

网页结构分析(分析含选定师资页/DOM样本/子分类页面, 逐一抓取合并):
{analysis_ctx[:2200] if analysis_ctx else '(无)'}

{kb_ctx if kb_ctx else ''}
{succ_ctx if succ_ctx else ''}
{('当前进度(重要): ' + partial_ok) if partial_ok else ''}
页面教师级链接样本(名字+URL):
{json.dumps(links[:25], ensure_ascii=False)}

参考骨架(基于它修改, 只动 FACULTY_URL/PAGES 与站点特定逻辑):
{generate_spider(school, dept, dept_url, fac_url, fac_urls, faculty_api)}

要求:
1. 依据 网页结构分析 与 失败原因 修正选择器/提取逻辑
2. CSV表头保持: 大学,学院,教师姓名,职务职称,所属院系,个人详情URL,师资页URL,采集时间
3. 保留 fetch/clean_name/extract_teachers/main 结构
4. 教师详情URL必须从页面链接提取, 禁止留空填充页面URL
5. 写出 teachers.json 时必须包含 teachers 数组(与CSV内容一致)
6. fetch 必须调用 util.smart_fetch.smart_fetch(url, timeout=N) — 该函数只接受 url/timeout 两个参数, 不支持 charset 等其他参数, 传了会直接崩溃; **该函数返回元组 (html, method), 必须解包接收: `html, _m = smart_fetch(url, timeout=N)`; 严禁单变量接收 `html = smart_fetch(url)`(html 会变成元组, 传给 re.findall/extract 直接 TypeError: got 'tuple' 崩溃, 2026-09-04 华工/山大/哈工程/中国农大多例实测)**; 文件头必须带 sys.path 引导(否则 import util 失败): import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parents[4]))
7. 若失败原因含"数量硬规则"且页面存在 下一页/尾页 分页控件, 必须循环翻页收集所有页(翻到无下一页为止): 优先用 util.teacher_extract.pager_pages(page_url, html) 跟随真实分页链接(含 "下一页>>"/"<<上一页" 装饰文本), 无分页链接用 list_pages(page_url) 兜底; 连续 2 个空页或请求失败立即 break; 若 网页结构分析 的"子分类/参数枚举"表中有多个含教师数的页面, 必须逐一抓取合并
7.5 【栏目全采-硬性 2026-10-26】师资页通常含多个栏目(岗位教师/研究生导师/教授/副教授/院士专家/教育管理干部等), 每栏目常是独立页面且人数可能巨大 — 网页结构分析里所有「师资候选/子菜单/师资分类」栏目的页面 URL 必须**全部**加入 FACULTY_URLS, 严禁只采其中一个子栏目。国科大资源与环境学院实锤: 只采「岗位教师」12 个子页 317 人, 漏掉「研究生导师」单页 2217 人(占 87%) — 导师类栏目往往是最大来源, 缺它名单严重缩减
8. 下一页/尾页/上一页/首页/更多/详情 等翻页控件与导航文本绝不能作为教师姓名提取, 必须显式排除(加入 STOP_NAMES 或在提取前过滤)
8.5 【机构/栏目URL过滤-硬性 2026-10-13】提取教师行时, 链接 href 命中栏目/列表特征(/list.htm|/index.htm$|/jgjj/|/szdwjj|/gzzz|login|enroll|thesis|questionnaire|interview|oauth|passport|/$) 且**不含**教师详情特征(/info/\\d+|/\\d{6,}\\.htm|/[a-z0-9]{20,}\\.htm|/\\d+/\\d+\\.htm) 的行绝不能当教师提取 — 必须 continue 跳过。机构/研究院的栏目链接文本常是 2-4 字, 全部跳过才能避免「机构页URL占比」FAIL(1130批环境与资源学院275/275实锤: LLM生成spider无此过滤, 整页栏目当教师)
9. 代码必须紧凑: 极少注释、去掉冗余变量, 总长不超过 4000 字符(网关对超过约100秒的长生成会截断为空响应, 紧凑是硬要求)
10. 通用能力必须复用 util, 禁止内联实现: 请求用 util.smart_fetch.smart_fetch(url, timeout=N); 姓名清洗/链接提取/分页枚举/CSV+JSON+meta写盘用 util.teacher_extract 的 clean_name/extract_teachers/pager_pages(真实分页链接跟随, 优先)/list_pages(URL形态兜底)/write_outputs; 文件头用向上查找 util 的 sys.path 引导(import sys as _sys; from pathlib import Path as _P; for _p in [_P(__file__).resolve()] + list(_P(__file__).resolve().parents): if (_p / \"util\" / \"smart_fetch.py\").exists(): _sys.path.insert(0, str(_p)); break)。spider 内只写站点特定的选择器/页面列表, 典型结构: main() 里 pager_pages(html,url) 分页跟随 + list_pages 兜底循环 + extract_teachers + write_outputs
11. 必须基于下面给出的骨架代码修改, 只改动 FACULTY_URL/PAGES 和站点特定提取逻辑, 不得改动骨架结构(历史上凭空重写曾反复出现语法错误被回滚)
12. 【改动最小化 2026-10-25】当前进度信息中已提取出的教师保持不动 — 只针对失败原因修复遗漏/错误部分, 不要改动已生效的提取逻辑(防把已好的部分改坏)

───── 2026-09-07 用户指令: 强化约束 (正确性/准确性/规范性/可阅读性/逻辑完备) ─────
12. 【正确性-硬性】代码必须一次可运行: 语法正确(禁止中文标点混入代码/字符串未闭合/
    缩进错误)、所有 import 完整、所有变量先定义后使用、函数参数数量匹配、
    无未定义引用。你写出的每一行都假设会立刻被执行并崩溃检查。
13. 【网络请求统一-硬性】所有网络请求只允许调用 util.smart_fetch.smart_fetch(url,
    timeout=N) 或复用模板 fetch(url, timeout=N)。严禁 import requests/urllib/
    httpx/curl_cffi/selenium/playwright/DrissionPage, 严禁 subprocess/os.system
    发请求, 严禁以任何方式启动浏览器(Chrome/9222/9333/CDP) — 浏览器是全局
    最后手段, spider 内禁用(并发下内存爆炸); 瑞数/JS 挑战由 smart_fetch
    内部纯 CPU 链自动破解(iv8 V8原生 → nodenv 补环境 → RsSpider sdenv),
    内联请求必然被 WAF 拦且无法统一治理。
14. 【逻辑完备】main 流程必须闭环: 每个 FACULTY_URLS 入口都要 fetch→提取→
    分页跟随; 每个 fetch 结果要判空(空/None 跳过, 不能把 None 传给 BeautifulSoup
    或 re); 所有 for/while 循环要有明确的终止条件; 异常用 try/except 兜底且
    except 块至少 pass。单院系请求量小(<10 个 URL), 按序串行即可,
    禁止多线程/异步/并发优化 — 简单可读优先于性能。
15. 【规范性】通用能力(请求/提取/分页/写盘)全部复用 util, 只写站点特定选择器;
    变量命名一致(fu=入口URL, pg=当前页URL, html=页面文本); 不得复制骨架中
    已存在的函数逻辑; write_outputs 必须且只能由骨架调用链执行一次。
16. 【可阅读性】代码结构清晰: 站点特定逻辑集中在 main/extract_teachers 两个
    函数内; 保留关键注释(每个选择器的目标); 长正则拆行; 不留死代码/未用变量。
17. 【准确性】选择器必须与 网页结构分析 的 DOM 样本一致; 教师姓名只从真实
    姓名元素提取(翻页控件/导航/研究所名/栏目词不得入 CSV); 详情URL必须来自
    页面真实链接, 不得拼接猜测。

返回JSON: {{"spider_code": "完整Python代码"}}
只输出JSON。"""
    # timeout=300: 波动期改进挂满全局 LLM_TIMEOUT(420s, 为健康探针长生成设的)
    # 会烧院系预算 (2026-09-03 波#4 高等技术研究院 R1.llm_done 414.2s 满挂;
    # 健康期生成 ~56-100s, 300s 安全, 与 fix_loop 注入重试路径同口径)
    r = llm_ask_with_timeout("只输出JSON。", prompt, timeout=300)
    if r:
        result = parse_json(r)
        if result and result.get("spider_code"):
            return result["spider_code"]
        # 网关 prompt 缓存会命中"截杀波"时期的坏响应(相同prompt秒回空/残缺JSON,
        # 实测批量全灭而手工略改的prompt正常) — 加时间扰动强制真实生成
        import time as _t
        print("     ⚗️ 疑似命中坏缓存响应, 加扰动重试 ...")
        r = llm_ask_with_timeout("只输出JSON。", prompt + f"\n[调用扰动: {int(_t.time() * 1000)}]", timeout=300)
    if not r:
        return None
    result = parse_json(r)
    if result and result.get("spider_code"):
        return result["spider_code"]
    return None


def _sync_meta(d, school, dept):
    """溯源一致性: 运行 spider 后若 spider_meta.json 缺失或 hash 失配则补写。
    新模板 spider 自写 meta(hash==当前spider) → 本函数不动, 避免 run_at 晚于
    CSV 采集时间被 rerun_stale 误判过期。老模板 spider 不自写 → 用 CSV 的
    采集时间作 run_at 补写, 保证 CSV 是当前 spider 的产物(溯源成立)。"""
    sp, mp = d / "spider.py", d / "spider_meta.json"
    if not sp.exists() or sp.stat().st_size < 100:
        return
    cur = hashlib.sha256(sp.read_bytes()).hexdigest()
    want = None
    if mp.exists():
        try:
            want = json.load(open(mp, encoding="utf-8")).get("spider_sha256")
        except Exception:
            pass
    if want == cur:
        return
    # 老模板不自写 meta: 用 CSV 的采集时间作 run_at, 避免 string 比较误判过期
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
        if len(rows) > 1 and len(rows[1]) > 7 and rows[1][7]:
            run_at = rows[1][7]
    except Exception:
        pass
    json.dump({"spider_sha256": cur, "run_at": run_at,
               "school": school, "dept": dept},
              open(mp, "w"), ensure_ascii=False, indent=2)
    print(f"  📌 补写spider_meta.json (hash {cur[:8]})")
