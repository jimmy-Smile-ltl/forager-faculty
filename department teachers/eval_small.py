#!/usr/bin/env python3
"""数量硬规则: 小组织严谨核实 — 2026-08-31 从 evaluate_teachers 拆出。

"怀疑漏采"不直接 FAIL, 先三查——①翻页全走查 ②其他分类子页面 ③是否只采一种
职称/类型。核实确为小组织才允许 PASS(写 verified_small_center); 核实不过保留
FAIL 并附证据。
"""
import re, sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from collect_teacher import fetch, find_faculty_page  # noqa: E402
from eval_elements import teacher_name_set  # noqa: E402  跨页去重教师姓名(数量核实用)


def _walk_fetch(u, timeout=15):
    """2026-10-09 走查链对齐: 数量核实的页面抓取必须与 spider 同链 —
    collect_teacher.fetch 不解 AES-GCM(中南财大公管实锤: 核实看密文0元素
    判漏采, spider 解码出 15 人) — 走查统一用 smart_fetch 全层级。"""
    try:
        from util.smart_fetch import smart_fetch
        _h, _m = smart_fetch(u, timeout=timeout)
        if _h and len(_h) > 500:
            return _h
    except Exception:
        pass
    try:
        return fetch(u)
    except Exception:
        return ""
# ── 数量硬规则: 严谨核实(2026-08-28) ──
# "怀疑漏采"不直接 FAIL, 先三查——①翻页全走查 ②其他分类子页面 ③是否只采一种职称/类型。
# 核实确为小组织才允许 PASS(写 verified_small_center); 核实不过保留 FAIL 并附证据。
_SMALL_VERIFY_CAT_KW = ("兼职", "退休", "博士后", "荣休", "名誉", "顾问", "客座",
                        "外聘", "教授", "导师", "职工", "员工", "名录", "名册",
                        "在职", "专任", "团队",
                        # 2026-10-15 教研室误判根修(石油大马院实锤): 组织型
                        # 师资页教师全在教研室/教研部/教学部子页里, 走查器认不出
                        # 这6个子菜单 → 主师资页0教师名 → 走查1人 vs CSV 28人
                        # → 误判漏采 RETRY循环(PASS 1%主堵点)。发现链
                        # collect_group_pages 词表早有(马院6子页全发现), 走查
                        # 判定词表没同步 — 补齐同口径
                        "教研室", "教研部", "教学部", "教研组", "教研中心")
# 教师/师资 必须复合下列词才算分类页(防 "教师法/师德师风学习手册" 等误判)
_SMALL_VERIFY_TEACH_COMPOUND = ("队伍", "主页", "名录", "名单", "简介", "风采",
                                "人员", "人才", "详情", "专栏", "力量", "介绍",
                                "一览", "列表", "团队", "信息", "中心")
# 负向过滤: 命中分类词但属政策/规章/新闻页(南京邮电 海外教育学院 被
# "教师法/师德师风学习手册" 误判未覆盖分类 → 小组织误FAIL 的教训)
_SMALL_VERIFY_CAT_BAD = ("办法", "条例", "规定", "手册", "法规", "制度", "师德",
                         "失范", "学习", "通知", "公告", "公约", "守则", "规范",
                         "动态", "工作", "要求", "指南", "细则", "招聘", "答辩",
                         "准则", "曝光", "案例", "印发", "党支部", "在行动",
                         "进行时", "教育部", "工会", "专栏", "思考", "模式",
                         "管理", "专家组", "委员会", "总结", "心得", "经验",
                         "选集", "作品", "书画", "摄影", "选集", "活动")
# 跨域跟随白词: 同济医学院 师资页选成"杰出人才"子页(jcrc.htm 仅3院士),
# 真全量在跨域"教师主页"(faculty.hust.edu.cn) — 同域限制漏掉 → 误判小组织
_SMALL_VERIFY_CROSS_HOST = ("教师主页", "师资", "教师队伍", "人才", "人员",
                            "faculty", "teacher", "staff", "people")
_SMALL_VERIFY_MAX_PAGES = 10  # 核实走查页数上限(分页+子页, 防失控)
_SMALL_VERIFY_MAX_SUB = 8     # 分类子页入队上限


def _small_verify_is_cat(t: str) -> bool:
    """分类子页链接判定: 命中分类词且非政策/新闻标题(标题通常很长 → 截断)。"""
    t = (t or "").strip()
    if not t or len(t) > 30:
        return False
    if any(b in t for b in _SMALL_VERIFY_CAT_BAD):
        return False
    if "职称" in t or "电话" in t or "邮箱" in t:
        return False  # 教师行内职称/联系方式链接, 非分类页(2026-08-31 清华建管系误报)
    if t.endswith(("法", "意见", "方案")):
        return False
    if "教师" in t or "师资" in t:
        # 教师/师资 单独出现可能是 教师法/教师招聘/师德 等 — 必须复合名单词
        return any(w in t for w in _SMALL_VERIFY_TEACH_COMPOUND)
    return any(k in t for k in _SMALL_VERIFY_CAT_KW)


def verify_small_department(dept_url, rows, dept_type="", d=None):
    """数量硬规则严谨核实 v2(2026-09-01 假阳性收紧, 用户反馈: 提高门槛, 严格化,
    对网站更多的网页结构进行分析评估)。

    confirmed=True 必须满足(静态路径): 走查元素 total 满足
    csv_n <= total <= csv_n+5 且 total<20(走查比CSV还少 = 没走到真名单页);
    教学组织(学院/学部/系) total>=5(电院假阳性: CSV2 走查3 误确认),
    其他组织 total>=1; 无未覆盖分类子页; 走查页均非壳/渲染失败页
    (0元素壳页与CSV≈0假自洽误确认 — 碳中和发展研究院教训)。
    权威名单路径: XHR 接口(复旦wp3/SCUT)与前端API(JSON下拉/AjaxFragment,
    上交 airs/shss 教训)检出即以其全量名单为准 — 远大于CSV=漏采不确认,
    与CSV接近=真小组织确认; 检出端点但拉取失败=JS驱动页, 静态走查不可信
    → 不确认(区域国别研究院 静态4元素/API 30人 误确认教训)。
    返回 {"confirmed": bool, "evidence": str, "pages": int, "elements": int}。"""
    from util.teacher_extract import (pager_pages,
                                      query_embedded_teacher_roster)
    from bs4 import BeautifulSoup
    try:
        from util.loader_challenge import is_shell
    except Exception:
        is_shell = None
    # 师资页候选: CSV 第7列(采集时的师资页URL) + find_faculty_page(dept_url) 双路,
    # 防 CSV 里存了错误页面类型(如广州中医药 国学院 存了院系主页新闻站, 0元素)
    facs = []
    if len(rows) > 1 and len(rows[1]) > 6:
        _c7 = (rows[1][6] or "").strip()
        if _c7.startswith("http"):
            facs.append(_c7)
    if dept_url:
        try:
            home = fetch(dept_url)
            _f2 = find_faculty_page(dept_url, home) if home else ""
            if _f2.startswith("http"):
                facs.append(_f2)
        except Exception:
            pass
    if not facs:
        return {"confirmed": False, "evidence": "无法定位师资页, 无法核实",
                "pages": 0, "elements": 0}
    base_host = urlparse(facs[0]).netloc
    seen, queue = set(), facs[:]
    all_names = set()   # 跨页去重教师姓名(2026-08-31: 列表页与分类子页是子集关系,
                        # 逐页累加会把 12 人真实名单误算成 31/53 → 假漏采)
    empty_streak = 0
    bad_pages = 0       # 壳/渲染失败页数 — 不计元素(2026-09-01 质量门)
    rendered_pages = 0  # 2026-10-09 JS壳走查盲区修复: 经浏览器渲染后走查的页数
    cats_found = set()
    cats_followed = set()  # 已跟随走查过的分类页(其元素已计入 all_names)
    _disc_pager = set()  # 2026-09-15 三查: 发现的全部分页页(走查结束核对遗漏)
    _disc_cats = set()   # 2026-09-15 三查: 发现的全部分类子页(走查结束核对遗漏)
    page_htmls = []        # 已抓 HTML(质量通过), 用于内嵌师资接口(SCUT deptTeacherList 在师资子页,
                           # 首页没有 — 必须扫全部走查页)与前端API检测(2026-09-01)
    page_urls = []         # 与 page_htmls 对齐的页面URL(前端API端点按页相对解析)
    while queue and len(seen) <= _SMALL_VERIFY_MAX_PAGES:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            h = _walk_fetch(u)
        except Exception:
            continue
        if not h or len(h) < 500:
            continue
        # ① 质量门(2026-09-01): 壳/渲染失败页元素≈0, 与 CSV≈0 假自洽会误确认
        #    (碳中和发展研究院 0元素壳页教训) — 该类页面不计元素
        try:
            if is_shell and is_shell(h):
                # 2026-10-09 JS壳走查盲区修复: 静态壳页改用浏览器渲染HTML走查
                # (全库119例「走查0元素」= 静态盲, 渲染后可见真名单 — 用户指令
                # 充分利用浏览器核实结果)。渲染仍无则维持原质量门不计元素。
                try:
                    from util.smart_fetch import _try_drission
                    _rh, _ = _try_drission(u, timeout=25)
                    if _rh and len(_rh) > 1000:
                        h = _rh
                        rendered_pages += 1
                    else:
                        bad_pages += 1
                        continue
                except Exception:
                    bad_pages += 1
                    continue
        except Exception:
            pass
        page_htmls.append(h)
        page_urls.append(u)
        new_names = teacher_name_set(h, u) - all_names
        all_names |= new_names
        if not new_names:
            empty_streak += 1
            if empty_streak >= 2:
                # 连续2页无新教师(空翻页链, 如清华建管系 js1/2..9.htm), 终止走查。
                # 2026-09-22 中大新闻实锤: 分类页之间教师无重复(教授页/副教授页
                # 各自独立名单), 连续空页不代表 queue 里未走查的分类页也空 —
                # 原 break 把分类页一起放弃 → 「发现7走查6」永久漏采 FAIL。
                # break 前检查: queue 里还有非「同序列页码变体」页则继续走查
                def _is_page_seq(a, b):
                    _strip = lambda x: re.sub(r"(\d+)(?=\.htm)|[?&](page|p|pn)=\d+",
                                              "", x or "")
                    return _strip(a) == _strip(b)
                _q_others = [q for q in queue if not _is_page_seq(u, q)]
                if not _q_others:
                    break
        else:
            empty_streak = 0
        # ① 翻页全走查: 真实分页链接(forward-only) + URL形态兜底
        # 2026-09-15 三查强化(用户指令: 核查重点=有无遗漏分页): 发现的全部
        # 分页页记录在案, 走查结束逐一核对未覆盖 → 有遗漏不确认
        try:
            # 2026-08-31 修复: 移除 list_pages URL形态猜测兜底 — 纯URL编号猜页
            # 会跨进无关页面(清华建管系 js1/2..9.htm 是空页/学院主页, 假53元素
            # 误判漏采); 真实翻页由 pager_pages(forward链接+信号)覆盖
            _fwd = pager_pages(u, h)
            _disc_pager |= set(_fwd)
            for p in _fwd:
                if (p not in seen and p not in queue
                        and urlparse(p).netloc == base_host):
                    queue.append(p)
        except Exception:
            pass
        # ①b 2026-09-15 三查强化: 子页/职称档页收集走 collect_group_pages
        # (发现链单源: 职称档词/括号分类/select下拉/裸系名tab 全形态覆盖) —
        # 与发现链同口径, 修复的形态即时生效; 发现的子页记录在案逐一核对
        try:
            from util.spider import collect_group_pages as _cgp
            _gps = _cgp(u, h, exclude=seen | queue)
            _disc_cats |= set(_gps)
            for _g in _gps:
                if urlparse(_g).netloc == base_host and len(queue) < _SMALL_VERIFY_MAX_SUB:
                    queue.append(_g)
        except Exception:
            pass
        # ② 分类子页面: 文本命中分类词且非政策/新闻页;
        #    跨域仅跟随强师资词链接(教师主页/师资/队伍/人才), 防漂移到无关站点
        try:
            soup = BeautifulSoup(h, "lxml")
            for a in soup.find_all("a", href=True):
                t = a.get_text(strip=True)
                hr = a.get("href", "").strip()
                if not hr or hr.startswith(("javascript", "#", "mailto")):
                    continue
                if not _small_verify_is_cat(t):
                    continue
                p = urljoin(u, hr)
                if p in seen or p in queue:
                    cats_followed.add(t)  # 目标已访问/已在队 → 视为覆盖
                    continue
                same_host = urlparse(p).netloc == base_host
                strong = any(k in t for k in _SMALL_VERIFY_CROSS_HOST)
                if same_host or strong:
                    cats_found.add(t)
                    if len(queue) < _SMALL_VERIFY_MAX_SUB:
                        cats_followed.add(t)
                        queue.append(p)
        except Exception:
            pass
    # ③ 单一类型: 未覆盖分类 = 发现但未能跟随(队列满/跨域非强词)的分类页;
    #    已跟随的分类页元素已计入 total, 不再重复算"未覆盖"(复旦 ai 学院
    #    "师资队伍"链接指回已走查页 → 不误报)
    # ③ 单一类型: 未覆盖分类 = 发现但未能跟随(队列满/跨域非强词)的分类页;
    #    已跟随的分类页元素已计入 total, 不再重复算"未覆盖"(复旦 ai 学院
    #    "师资队伍"链接指回已走查页 → 不误报)
    csv_titles = {r[3].strip() for r in rows[1:] if len(r) > 3 and r[3].strip()}
    uncovered = [c for c in cats_found if c not in cats_followed
                 and not any(c in ct or ct in c for ct in csv_titles)]
    # ⑥ 2026-09-15 三查核对(用户指令): 发现的分页页/子页未走查 = 遗漏,
    # evidence 写明遗漏明细(评估日志可见「确认无遗漏」依据); 明细文本先
    # 存 note, ev 构建后拼接(ev 在下方构建)
    _pager_miss = [p for p in _disc_pager if p not in seen]
    _cats_miss = [c for c in _disc_cats if c not in seen]
    _three_note = ""
    if _pager_miss:
        _three_note += f"; 遗漏分页{len(_pager_miss)}页(发现{len(_disc_pager)}走查{len(_disc_pager & seen)})"
    if _cats_miss:
        _three_note += f"; 遗漏子页{len(_cats_miss)}个(发现{len(_disc_cats)}走查{len(_disc_cats & seen)})"
    if not _pager_miss and not _cats_miss and _disc_pager:
        _three_note += f"; 分页{len(_disc_pager)}页全走查无遗漏"
    if _disc_cats and not _cats_miss:
        _three_note += f"; 分类子页{len(_disc_cats)}个全覆盖"
    # 职称档页核对: 发现链职称档词(正高级/副高级/中级/高层次人才)页必须走查
    _rank_pages = [c for c in _disc_cats
                   if re.search(r"正高级|副高级|中级|高层次人才|正高|副高", c.split("/")[-1] or "")]
    _rank_miss = [c for c in _rank_pages if c not in seen]
    if _rank_pages:
        _three_note += f"; 职称档页{len(_rank_pages)}个{'全覆盖' if not _rank_miss else f'遗漏{len(_rank_miss)}'}"
    total = len(all_names)  # 跨页去重教师姓名数(2026-08-31)
    # ④ 内嵌师资接口名单(2026-08-28): 静态页可能是 XHR/JSONP 驱动空壳
    #    (复旦 ai 学院 289人 静态页仅2; SCUT 设计学院 62人 静态页4 — 用户指出
    #    核实未识别该特点)。扫描全部走查页: sudy-wp-siteId / 内嵌 deptTeacherList
    #    接口 → 拉权威名单数。
    xhr_total, xhr_note = None, ""
    # ⑤ 2026-10-09 用户指令「最终判定有没有老师必须浏览器打开确认」:
    #    JSON/XHR 动态渲染名单静态走查 0 元素(中南财大公管 AES壳15人实锤) —
    #    两条兼容性补强(纯增量, 不影响既有通过/否决路径):
    #    a) AJAX POST 接口检测(probe detect_ajax_api 同源逻辑) → 权威名单数
    #    b) 静态 0 元素时浏览器渲染走查(渲染 DOM 已执行 XHR, 名单现身;
    #       真无数据则渲染后仍 0 — 浏览器确认才是最终判词)
    if xhr_total is None and page_htmls:
        _csvn0 = max(0, len(rows) - 1)
        # a2) wp3 articles 平台接口(苏迪CMS按钮分页, 2026-10-09 中南财大公管
        # 实锤: DOM 15人 vs 接口 total 56) — 直查权威总数, 兼容嵌入接口口径
        try:
            from util.teacher_extract import wp3_articles_roster
            _w = wp3_articles_roster(facs[0], page_htmls[0])
            if _w.get("ok") and len(_w.get("teachers") or []) > 0:
                # 用接口去重人数而非 total 字段: 多栏重复发布同批名单时
                # total=各栏和会虚高(知产学院 17076/17115 各23人完全重复,
                # total 46 误判漏采实锤) — 去重后的 teachers 才是真实人数
                _wt = len(_w.get("teachers") or [])
                if _wt >= max(_csvn0, total) - 5:
                    xhr_total = _wt
                    xhr_note = f"wp3 articles接口去重名单{_wt}人"
        except Exception:
            pass
        for _ph, _pu in zip(page_htmls[:3], page_urls[:3]):
            try:
                import importlib.util as _ilu
                _psp = Path(__file__).resolve().parent / "script" / "probe_site.py"
                _spec = _ilu.spec_from_file_location("probe_site", str(_psp))
                _pm = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_pm)
                _aj = _pm.detect_ajax_api(_ph, _pu)
                if _aj and _aj.get("count", 0) > 0:
                    _ajc = _aj["count"]
                    # 端点远小于CSV=可能只返首页分页或端点非全量, 不采信
                    # (权威确认仅当与CSV/走查接近 — 兼容嵌入接口同口径)
                    if _ajc >= max(_csvn0, total) - 5:
                        xhr_total = _ajc
                        xhr_note = f"AJAX接口POST名单{_ajc}人"
                    else:
                        xhr_note = f"AJAX接口名单{_ajc}人远小于CSV(不采信)"
                    break
            except Exception:
                pass
    # 2026-10-09 渲染走查条件放宽: 原条件 total==0 只救静态全盲页 — 批次2实锤
    # JS 站提取 23 人而静态走查只见 1-14 人(石大北京系/北理工生命/北体竞技) —
    # 「走查远少于CSV」同样需要浏览器渲染复核(渲染后走查可见真名单);
    # 渲染后仍少才是真「没走到真名单页」。
    if total == 0 or (max(0, len(rows) - 1) > 0 and total < max(0, len(rows) - 1) - 2):
        for _pu in page_urls[:4]:
            try:
                from util.smart_fetch import _try_drission
                _rh, _m = _try_drission(_pu, timeout=25)
                if _rh and len(_rh) > 1000:
                    _new = teacher_name_set(_rh, _pu) - all_names
                    all_names |= _new
                    rendered_pages += 1
                    if _new:
                        break
            except Exception:
                pass
        total = len(all_names)
    if total < 8:
        for _h in page_htmls:
            _xr = query_embedded_teacher_roster(facs[0], _h)
            if _xr.get("ok"):
                xhr_total = len(_xr.get("teachers") or [])
                xhr_note = f"; {_xr.get('mode','')}接口名单{xhr_total}人(静态页仅{total}元素)"
                break
            elif _xr.get("reason") != "no_roster_api":
                xhr_note = f"; 师资接口拉取失败({_xr.get('reason')})"
                break
    # ⑤ 前端API权威检测(2026-09-01 上交 airs/shss 教训): JSON 下拉加载 /
    #    AjaxFaculty 片段页静态走查只能看到壳或少数卡片 — 检测到端点必须
    #    拉全量名单; 拉取失败(<3人)说明 JS 驱动页面, 静态走查计数不可信。
    api_total, api_failed, api_note = None, False, ""
    for _u, _h in zip(page_urls, page_htmls):
        try:
            from util.faculty_api import (detect_faculty_api, fetch_faculty_api,
                                          probe_js_endpoints)
            _cfg = detect_faculty_api(_h, _u)
            if not _cfg:
                # 2026-10-26 国科大实锤: 端点在外部 JS → L2 扫描兜底
                _cfg = probe_js_endpoints(_h, _u)
            if not _cfg:
                continue
            _rows = fetch_faculty_api(_cfg, _u)
            if len(_rows) >= 3:
                api_total = len(_rows)
                api_note = f"; 前端API名单{api_total}人(静态页仅{total}元素)"
            else:
                api_failed = True
                api_note = "; 检测到前端API端点但名单拉取失败(JS驱动页, 静态走查不可信)"
        except Exception:
            continue
        break  # 任一页检测到 API 端点即停止(以该端点为准)
    csv_n = len(rows) - 1
    # 教学组织(学院/学部/系)静态确认至少 5 个元素(电院假阳性: CSV2 走查3);
    # 研究院/中心/实验室等小组织 1 个即可(真2人实验室走查恰2人)。
    min_el = 5 if dept_type in ("学院", "学部", "系") else 1
    if xhr_total is not None and xhr_total > csv_n + 5:
        confirmed = False          # XHR 权威名单远大于 CSV → 漏采(静态页只是空壳)
    elif xhr_total is not None and xhr_total > 0:
        confirmed = True           # XHR 名单非空且与CSV接近 → 权威小组织
    elif api_total is not None and api_total > csv_n + 5:
        confirmed = False          # 前端API全量名单远大于 CSV → 漏采
    elif api_total is not None and api_total < csv_n - 5:
        confirmed = False          # API名单远小于CSV → 端点非全量名单, 静态走查不可信
    elif api_total is not None:
        confirmed = True           # 前端API名单与CSV接近 → 权威小组织
    elif api_failed:
        confirmed = False          # JS 驱动页拉不到名单 → 无法核实
    else:
        # 静态走查确认(2026-09-01 严格化): 走查元素不得少于CSV(更少=没走到
        # 真名单页), 不得多于CSV+5(更多=漏采); 教学组织≥5元素; 0元素绝不确认;
        # 无未覆盖分类子页。2026-09-15 用户新规则: 阈值 20→30 —
        # "反复确认后老师数量确实小于30算完成, 确认没有缺失算通过";
        # 三查(分页/子页/职称档)任一有遗漏 → 不确认(用户指令: 重点核查
        # 有无遗漏分页/遗漏子页面/不同职称)
        # 2026-10-09 容差修正: csv_n<=total 改为 -2 容差 — 浏览器渲染走查
        # 实锤(中南财大公管: 渲染见14人 vs CSV15, 分页全走查无遗漏)走查少
        # 1-2 人是姓名识别差异(英文名/4字名/性别后缀), 非漏采; 走错页的
        # 形态是 0 或远少, 2 人容差不构成假阳性风险
        confirmed = (min_el <= total < 30 and csv_n - 2 <= total <= csv_n + 5
                     and not uncovered and not _pager_miss and not _cats_miss)
        # 2026-10-10 研究机构渲染复核确认(用户规则: 研究机构可能确实没有老师;
        # 且「最终判定有没有老师必须浏览器打开确认」): 非教学组织渲染走查后
        # 仍 0 元素 + 走查完整 = 浏览器确认真无师资 → 确认(复旦核物理实验室
        # 2人/新农村发展研究院1人 渲染复核 0 元素反复 FAIL 实锤)
        if (not confirmed and total == 0 and rendered_pages > 0
                and dept_type not in ("学院", "学部", "系")
                and not uncovered and not _pager_miss and not _cats_miss):
            confirmed = True
    ev = f"全走查{len(seen)}页共{total}个教师姓名(去重, CSV {csv_n}人)"
    if len(seen) > 1:
        ev += f"(含{len(seen)-1}个分页/子页)"
    if bad_pages:
        ev += f"; {bad_pages}页低质量(壳/渲染失败)不计元素"
    if rendered_pages:
        ev += f"; {rendered_pages}页经浏览器渲染走查"
    if xhr_note:
        ev += xhr_note
    if api_note:
        ev += api_note
    if _three_note:
        ev += _three_note
    if total == 0:
        ev += "; 未找到教师级元素(页面类型存疑, 无法确认)"
    if not confirmed and total < min_el:
        _org = "教学组织" if dept_type in ("学院", "学部", "系") else "组织"
        ev += f"; {_org}走查元素{total}低于确认门槛{min_el}"
    if (not confirmed and api_total is not None
            and api_total < csv_n - 5):
        ev += f"; 前端API名单{api_total}远小于CSV{csv_n}(端点非全量名单)"
    if uncovered:
        ev += f"; 未覆盖分类: {'/'.join(sorted(uncovered)[:3])}"
    if not confirmed:
        ev += " → 核实未通过, 判漏采"
    # 2026-10-09 走查发现页旁路: 提取器注入用(fix_loop 读此文件把走查页/
    # 遗漏分页子页并入 fac_urls) — 提取与核实同源(上体体育教育 CSV1 vs
    # 走查57 实锤)。d 为可选参数, 不传则只评估不写盘(兼容旧调用)。
    if d is not None:
        try:
            import json as _json
            _vp = {"pages": list(dict.fromkeys(page_urls))[:20],
                   "miss": list(dict.fromkeys(_pager_miss + _cats_miss))[:10]}
            (Path(d) / "_verify_pages.json").write_text(
                _json.dumps(_vp, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return {"confirmed": confirmed, "evidence": ev,
            "pages": len(seen), "elements": total}


