#!/usr/bin/env python3
"""
评估修复循环 — 评估→改进→重评, 直到通过(最多3轮)

对每个 FAIL 院系:
  R0: 无 spider.py → 用确定性模板生成 spider.py (保证 teachers.csv 是 spider.py 的产物)
  R1: 运行 spider.py → 生成 teachers.csv
  R2: evaluate_teachers.py 评估 → PASS 停止
  R3: FAIL 时带评估原因 + 页面HTML → LLM 核查改进 spider.py → 回到 R1

用法:
  uv run python fix_loop.py 苏州大学 --limit 5
  uv run python fix_loop.py 苏州大学 --dept 传媒学院

(2026-08-31 拆分重构: 生成器/优化器 → fix_improve.py, 失败队列 → fix_queue.py;
 本文件保留 _norm_name/fix_one/main 并 re-export, 存量 import 零改动。)
"""

import argparse, csv, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from _paths import ROOT, OUT, LOGS  # 先导: sys.path 引导 + .env 加载
_OUT = OUT  # 2026-09-09 _tm_flush 慢站标记用(与模块名 OUT 区分)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

BASE = ROOT  # 兼容旧名(multi_school_batch 用 fl.BASE/OUT/LOGS)
sys.path.insert(0, str(BASE / "templates"))  # templates.* 可解析(与 _paths 双保险)

from collect_teacher import (  # noqa: E402
    fetch, find_faculty_page, get_llm, parse_json, _clean_name,
    record_case, load_kb, llm_ask_with_timeout, NO_TEACHER_ORGS,
)

LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "150"))  # LLM单次调用超时(秒): 网关时快时慢(紧凑spider实测60-125秒),
                    # 150秒覆盖慢波; 网关对极长生成的截断由"紧凑提示词"规避
                    # 2026-08-23: 改读环境变量 — .env 配 210(慢速窗+SEM=2排队 2×82s≈164s)

# ── 院系级并发(线程池, --workers/AGENT_WORKERS 可调) + LLM 限速 ──
import os as _os
import threading as _threading
WORKERS = int(_os.environ.get("AGENT_WORKERS", "4"))
# _LLM_SEM 共享配额实例统一在 collect_teacher(全项目唯一信号量), fix_loop 复用
# ↑ LLM并发配额(默认4, 环境变量 LLM_SEM 可调): 内网网关实测4路并发全成; 报429再降
# spider 子进程运行超时(秒): 慢站点 + Playwright 兜底可能远超 60s; 0=不超时
# 2026-09-07 用户指令: 固定 180s 等待太长 → 60s 基线, 多入口站点按入口数
# 小幅放大(_spider_timeout), 快速失败由队列重试补偿(失败重试便宜, 白等昂贵)。
SPIDER_TIMEOUT = int(os.environ.get("SPIDER_TIMEOUT", "90"))  # 2026-09-10 用户指令加长 60→90
# 2026-09-09 用户指令: 慢站降权阈值 — 单院系耗时超此值仍 RETRY 即挂慢标记,
# 排到队列最后(先易后难)。院内预算 1200s 内, 600s 以上已属慢握手/限速站。
SLOW_DEPT_THRESHOLD = float(os.environ.get("SLOW_DEPT_THRESHOLD", "600"))

# ── 艺术院校特殊采集器守卫 (2026-09-17 整校污染实锤) ──
# 央美/国美/央音/上音/中戏 的院系师资数据由特殊采集器按「院系→正确师资页/校级名录
# 切分」显式映射生成 (script/cafa_collect.py、caa_collect.py、art_college_fix.py),
# 写盘的 spider.py 带溯源标记。发现链再生(多入口/AJAX 触发)与 LLM 改进会把正确
# 映射 spider 换成发现链版本 — 发现链对艺术院校会误选校级页(cn/commun 外籍合作页、
# /sp/2022clinks 校级名录), 导致整校名单重复灌入每个院系(中戏 13 系 × 216 外籍、
# 央美 14 系 × 218 校级名录)。标记 spider 永不再生, 数据归特殊采集器管。
_ART_SPIDER_MARKERS = ("script/art_college_fix.py", "script/cafa_collect.py",
                       "script/caa_collect.py")


def _is_art_spider(spiderf) -> bool:
    try:
        _t = spiderf.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return any(_m in _t for _m in _ART_SPIDER_MARKERS)


def _spider_timeout(n_entries: int) -> int:
    """spider 动态超时 (2026-09-07 用户指令: 基线 60s, 多入口/多分页站点按
    入口数小幅放大 — 北大CS 13 入口 20+ 页请求固定 60s 必截杀且产物全丢)。
    60 + 15s/入口, 上限 300s(院内 900s 预算内, 快速失败由队列重试补偿)。
    2026-09-08 用户指令「瑞数站确实慢, 没有办法, 无代理可用」: 上限 300→480 —
    瑞数 iv8 求解 30s/页×多页直连慢速是常态, 300s 截杀致整 spider 产物全丢
    +重试再截杀磨坊; 480s 让慢瑞数 spider 一次跑完, 院内预算同步放宽至 1200s。
    2026-09-10 用户指令「网络请求等待时间加长」: 基线 60→90 — 慢握手站
    少入口院系的 60s 截杀循环是零老师学院从未产出的原因之一, 90s 给足首轮。"""
    return min(90 + 15 * max(0, n_entries - 3), 480)

# 导航页URL形态(院长信箱/联系/留言/地图): 复用 analysis.md 选定师资页前拒绝
# (2026-09-03 南科大 letter/ 实锤: probe 误选导航页, 复用后 3 轮全 0 教师 →
# URL缺失100% 落终态; 段级匹配避免误伤 /newsletter 等含词路径)
_NAV_PAGE_RE = re.compile(
    r"/(letter|mailbox|contact|feedback|guestbook|suggest|liuyan|xinfang|map)"
    r"(/|[._-]|$)"
    # 2026-09-04 南科大 44×8 研究中心假PASS: 校级 /zh/faculty/ 索引页同款污染
    # (回退链 find_faculty_page 从院系首页"师资队伍"链接直达学校师资总索引)。
    # 只排除裸索引(以 faculty/faculties 结尾), 不影响详情页 /zh/faculties/xxx.html
    # 与院系页 /faculty/master 形态
    r"|/zh/facult(y|ies)/?$", re.I)


from evaluate_teachers import evaluate_dept  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

try:
    from templates.spider_template import NAV_WORDS as _NAV_WORDS
    NAV_WORDS = set(_NAV_WORDS)
except Exception:
    NAV_WORDS = set()

from fix_improve import (  # noqa: E402  R0 生成 / R3 改进 / 溯源补写
    SPIDER_TEMPLATE, generate_spider, llm_improve_spider, _sync_meta, _code_view,
)
from fix_queue import (  # noqa: E402  失败重排队列
    FAIL_QUEUE_PATH, MAX_QUEUE_ATTEMPTS,
    load_fail_queue, save_fail_queue,
    add_to_queue, remove_from_queue, clear_fail_queue,
    _KB_LOCK,
)

_TITLE_SUFFIXES = ["教授级高级工程师", "高级工程师", "博士生导师", "硕士生导师",
                   "副主任医师", "主任医师", "主治医师", "医学博士", "医学硕士",
                   "特聘教授", "副研究员", "助理研究员", "研究员", "助理教授",
                   "副教授", "教授", "讲师", "博导", "硕导", "院士", "博士", "硕士",
                   "职称", "院长", "副院长", "副院", "主任", "医师", "书记", "党委", "助理", "副", "研究"]


# ── 院系耗时统计 (2026-09-03 用户指令: 统计开始到结束各步骤时间, 记录到日志) ──
# 线程级 marks: fix_one 包装器 _tm_start/_tm_flush, 各阶段 _tm_mark; 每院系
# 终态时写一行 logs/_dept_timing.log (各步骤时长 = 相邻 mark 差值)。
_TIMING = {}  # thread ident -> {"school","dept","t0","marks":[(名称, 累计秒)]}
_TIMING_LOCK = _threading.Lock()


def _reg_domain(u):
    """注册域(edu.cn/com.cn 取末3段, 否则末2段): www.seiee.sjtu.edu.cn→sjtu.edu.cn
    (2026-09-28 上交电院实锤: 名录子类在兄弟子域 see./sais.sjtu.edu.cn)"""
    h = urlparse(u).netloc.lower()
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    if len(parts) >= 3 and parts[-2] in ("edu", "com", "net", "org", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_platform_bad(u):
    """全校教师平台链接(无 CollegeTeacherList=按院系列表): faculty.xxx.edu.cn
    首页/拼音列表/units_teacherlist 都是全校数据, 采集必污染
    (2026-09-29 哈工程外语 1334 人实锤: LLM 改进轮把 units_teacherlist 写进
    FACULTY_URLS, 1311 名全校教师进外院名单)"""
    return bool(re.search(r"faculty\.[a-z0-9.-]+\.edu\.cn", u, re.I)
                and "CollegeTeacherList" not in u)


def _sanitize_platform_urls(code, dept_url=None):
    """LLM 改进产物里的链接剥除守卫: ①全校平台 URL(无 CollegeTeacherList)一律
    剔除 — 哈工程外语 1334 人污染实锤; ②非本校注册域 URL 一律剔除 —
    2026-10-01 中科大应用化学 1701 实锤: LLM 把 casad.cas.cn(中科院学部
    院士名录, 726+院士)写进 FACULTY_URLS, 全校院士进化学系名单"""
    _dom = _reg_domain(dept_url) if dept_url else None

    def _drop(m):
        urls = []
        for u in m.group(1).split(","):
            u = u.strip()
            if not u or u in ("''", '""'):
                urls.append(u)
                continue
            if _is_platform_bad(u):
                continue
            if _dom and _reg_domain(u) != _dom:
                continue
            urls.append(u)
        return "FACULTY_URLS = [" + ", ".join(urls) + "]"
    return re.sub(r"FACULTY_URLS = \[([^\]]+)\]", _drop, code)


def _tm_start(school, dept):
    _TIMING[_threading.get_ident()] = {"school": school, "dept": dept,
                                       "t0": time.time(), "marks": []}
    _tm_trace("START")


def _tm_trace(name):
    """实时面包屑 (2026-09-03 波#5 五僵尸实锤): 僵尸线程不经过 _tm_flush,
    内存 marks 全丢、挂点无痕(21:25 进场 900s 静默无法定位)。每次 mark
    即时追加一行 trace — 僵尸的最后一行即挂点下界, 下次僵尸可直接定位。"""
    t = _TIMING.get(_threading.get_ident())
    if not t:
        return
    try:
        with _TIMING_LOCK, open(LOGS / "_dept_trace.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} "
                    f"{t['school']}/{t['dept']} {name}\n")
    except Exception:
        pass


def _tm_mark(name):
    t = _TIMING.get(_threading.get_ident())
    if t:
        t["marks"].append((name, time.time() - t["t0"]))
        _tm_trace(name)


def _tm_flush(result):
    t = _TIMING.pop(_threading.get_ident(), None)
    if not t:
        return
    total = time.time() - t["t0"]
    # 2026-09-09 用户指令: 慢站降权 — 单院系耗时超阈值仍 RETRY(长时间攻不克)
    # 写 .slow_mark, 批次侧入队带 slow 标志排到队列最后(先易后难, 快的先处理);
    # PASS/FAIL 终态不挂慢标记(已有结论, 无排队问题)。
    try:
        if result == "RETRY" and total > SLOW_DEPT_THRESHOLD:
            (_OUT / t["school"] / t["dept"] / ".slow_mark").touch()
    except Exception:
        pass
    segs, prev = [], 0.0
    for name, cum in t["marks"]:
        segs.append(f"{name}:{cum - prev:.1f}s")
        prev = cum
    line = (f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {t['school']}/{t['dept']} "
            f"total={total:.1f}s result={result} {' '.join(segs)}")
    try:
        with _TIMING_LOCK, open(LOGS / "_dept_timing.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _norm_name(nm: str) -> str:
    """剥离括号/空白/职称粘连词, 供质量计数使用(与 LLM 终审判定同一口径)"""
    t = re.sub(r"[（(].*?[）)]", "", nm).replace(" ", "").replace("　", "")
    for suf in _TITLE_SUFFIXES:
        if t.endswith(suf) and len(t) > len(suf) + 1:
            t = t[:-len(suf)]
            break
    return t


# ── 3轮终态分流(2026-08-31 用户指令) ──
# 评估→改进最多3轮, 3轮未通过即终态, 不再入队无限重试(浪费token/时间)。
#   skip = 确实没有数据(页面不可达/无师资页)
#   fail = 有数据但提取失败(选择器/提取规则错误)
# 分析文档 analysis.md 始终保留, 结论写入 test_report.json。
_NO_DATA_HINTS = re.compile(
    r"不可达|unreachable|no_faculty|无师资|未发现师资|连接失败|超时|抓取失败|"
    r"无法访问|404|403|DNS|拒绝连接|空响应")


def _classify_terminal(fac_html: str, reasons: list) -> str:
    """3轮未通过后的终态分类: no_data_skip / fail_extraction。纯函数便于单测。"""
    if not fac_html or len(fac_html) < 500:
        return "no_data_skip"
    for r in reasons or []:
        if _NO_DATA_HINTS.search(r):
            return "no_data_skip"
    return "fail_extraction"


def _seed_backfill(d):
    """2026-10-26 名单不缩减守卫(上外新闻传播学院恢复种子36→重跑PASS5实锤):
    PASS 落盘前, 把旧管线名单(teachers/<学校>/<院系>/teachers_detail.json,
    只增不减同步产物)中当前 teachers.csv 缺失的姓名(有效 http URL)补回 —
    重采集一时拿不到的旧名单姓名不得被 PASS 缩水覆盖。补回行为 [name,'',url]
    最小行, 过 write_outputs 的 clean_name 终检(垃圾名自然被滤)。"""
    try:
        of = BASE.parent / "teachers" / d.parent.name / d.name / "teachers_detail.json"
        if not of.exists():
            return
        o = json.load(open(of, encoding="utf-8"))
        orows = o.get("teachers", o) if isinstance(o, dict) else o
        cur = set()
        cf = d / "teachers.csv"
        if cf.exists():
            for r in csv.reader(open(cf, encoding="utf-8")):
                if len(r) > 2 and r[2]:
                    cur.add(r[2].strip())
        add = []
        for r in orows:
            if not isinstance(r, dict):
                continue
            nm = str(r.get("姓名") or "").strip()
            u = str(r.get("详情页URL") or "").strip()
            if nm and nm not in cur and u.startswith("http"):
                add.append([nm, "", u, nm, u])
        if not add:
            return
        from util.teacher_extract import write_outputs as _wo
        _wo(d.parent.name, d.name, [[r[2], r[3], r[5], r[2], r[6]]
                                    for r in csv.reader(open(cf, encoding="utf-8"))
                                    if len(r) > 6][1:] + add)
        print(f"  🌱 种子回填: +{len(add)}名 (名单不缩减守卫)")
    except Exception as e:
        print(f"  ⚠️ 种子回填跳过: {type(e).__name__}")


def _minor_tolerance_pass(d, reasons) -> bool:
    """2026-10-25 用户指令: retry 3轮后问题不明显 → PASS(容忍少数错误数据),
    不落FAIL。条件: 盘面教师≥5 且全部失败原因均为轻微问题 —
    ①非人名混入总数 ≤ max(3, 3%) ②数量硬规则差异轻微(走查≥70%且CSV≥10)。
    含 URL不合规/无教师/网络类原因不宽容(URL是首要信息, 维持严格)。
    注意: 真名误杀(赵一鸣/何燕李等)已进 REAL_NAME_PROTECT, 下批不再被
    判非人名; 此容忍针对的是残留垃圾词混入等轻微噪声。"""
    try:
        rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
    except Exception:
        return False
    _nr = re.compile(r"^[一-鿿·]{2,4}$")  # 局部定义: _NAME_RE 是函数内局部变量
    clean = sum(1 for r in rows[1:]
                if len(r) > 2 and _nr.match(_norm_name(r[2].strip()))
                and _norm_name(r[2].strip()) not in NAV_WORDS)
    if clean < 5:
        return False
    # 2026-10-26 跨院串线污染护栏(中石油智能科学与技术系 5914人实锤:
    # 校友名录共享页+兄弟学院教师页混入, 被容忍PASS放行): 数量合理性上限 —
    # 系≤300/学院≤600/学部及其他≤1000, 超出即污染嫌疑, 不宽容不PASS
    _cap = 1000
    try:
        _dn = Path(d).name
        if _dn.endswith("系"):
            _cap = 300
        elif _dn.endswith("学院"):
            _cap = 600
    except Exception:
        pass
    if clean > _cap:
        return False
    junk = 0
    for r in reasons or []:
        if re.search(r"详情URL不合规|URL缺失|列表页形态", r):
            return False
        _m = re.search(r"非人名混入 (\d+)个", r)
        if _m:
            junk += int(_m.group(1))
        _m = re.search(r"(\d+)个非人名", r)
        if _m:
            junk += int(_m.group(1))
        _m = re.search(r"学院仅(\d+)人", r)
        if _m:
            _csv_n = int(_m.group(1))
            _m2 = re.search(r"走查\d+页共(\d+)个教师姓名", r)
            _walk = int(_m2.group(1)) if _m2 else 0
            if _csv_n < 10 or _walk < int(_csv_n * 0.7):
                return False
    return junk <= max(3, int(clean * 0.03))


def _url_terminal_grade(d, reasons: list) -> str:
    """2026-09-07 用户规则(URL是首要信息): 详情URL缺失>30%需复核(评估→改进轮次
    即复核), 复核后(3轮耗尽)缺失仍>50%判失败; ≤50%达标 PASS。
    返回 'PASS'/'FAIL'/''(非URL类失败, 走原 _classify_terminal 分流)。
    注: 纯文本页(缺失100%)若通过严格纯文本审查在前置路径已落 no_url/LEGAL_EMPTY,
    不经过本函数; 此处只裁决"有链接形态但采不齐"的院系。"""
    if not reasons or not any(re.search(r"URL缺失率|详情URL全空", r or "") for r in reasons):
        return ""
    try:
        rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
    except Exception:
        return ""
    body = rows[1:]
    n = len(body)
    if n < 3:
        return ""  # 样本太小, 不按URL口径放行/判死
    ok = sum(1 for r in body if len(r) > 5 and (r[5] or "").strip().startswith("http"))
    mr = 1 - ok / n
    if mr > 0.50:
        return "FAIL"
    print(f"  📊 URL复核达标: 缺失{mr:.0%}(≤50%), 按用户规则放行")
    return "PASS"


# 列表页/栏目页形态(非教师详情页) — 与 teachers/roster_qc 同口径
_LIST_URL_RE = re.compile(r"/list\.htm|/list_\d+\.|/szdw\.htm|/js\.htm|"
                          r"/index\.htm|/main\.htm|/default\.htm|"
                          r"/category|/lecture", re.I)


def _detail_url_compliance(d, fac_html, fac_url):
    """2026-10-16 用户指令「工作流增加教师详情URL合规性检测, 不通过带问题RETRY
    完善」: 每轮评估后检测 teachers.csv 详情URL列合规性, 返回问题诊断
    (不合规)或 ''(合规)。三个维度:
      ① 非 http(s) 值 — 字段错位/非链接(院系名/姓名/mailto/javascript/file)
      ② 列表页形态 — /list /index /lecture 等栏目页被当详情页
      ③ 空 URL 大比例且页面确有详情链接 — spider 提取 href 失败(纯文字名单
         页面无链接的不归此项, 由纯文本审查路径处理)
    诊断文本并入评估原因 → LLM 改进轮带着明确问题完善 spider 提取逻辑;
    3 轮修不好由 _url_terminal_grade 终态裁决(已有)。"""
    try:
        rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
    except Exception:
        return ""
    body = rows[1:]
    n = len(body)
    if n < 5:
        return ""  # 样本太小不判合规
    bad = empty = listlike = total_u = 0
    for r in body:
        u = (r[5] or "").strip() if len(r) > 5 else ""
        if not u:
            empty += 1
            continue
        total_u += 1
        if not u.startswith(("http://", "https://")):
            bad += 1
        elif _LIST_URL_RE.search(u):
            listlike += 1
    prob = []
    if bad >= 3:
        prob.append(f"非http详情URL {bad} 条(字段错位/非链接)")
    if total_u and listlike >= 3 and listlike / total_u >= 0.3:
        prob.append(f"列表页形态URL {listlike}/{total_u} 条")
    if n and empty / n >= 0.5 and fac_html and fac_url and len(fac_html) > 500:
        try:
            from evaluate_teachers import verify_no_detail_links
            _dn, _ev2 = verify_no_detail_links(fac_html, fac_url)
            if _dn and _dn >= 5:
                prob.append(f"页面有{_dn}个详情链接但CSV空URL{empty}/{n}"
                            f"(spider未提取href)")
        except Exception:
            pass
    return "; ".join(prob)


# ── 递归循环 Φ: 评估→改进→重评(≤3轮) ──
def _ip_unreachable(url: str, timeout: int = 8, retries: int = 1) -> bool:
    """IP级封禁/站点不可达检测(2026-09-02 用户指令: 连接拒绝直接标 FAIL,
    不浪费 probe/LLM/3 轮重试)。

    判定口径(南师大实测: TCP 通但 TLS 层断连 Broken pipe — 必须 HTTP 层探测):
      直连 curl_cffi **chrome110+Windows Chrome/138 UA**(与 nodenv 链同款最强
      HTTP 指纹 — 兰大/川大 TLS 指纹级拦截只杀 chrome124/131, chrome110
      可拿 412 挑战, 不得误判为封禁) 拿不到任何 HTTP 响应(传输层断连/重置/
      超时) 且 代理也拿不到 <500 的真实响应(502/503 或异常) → True。
      拿到任何状态码(含 412/400 挑战页 = 可用 nodenv/sdenv 纯CPU破解)
      → False(可达, 不误杀)。已知代理可达域名(_PROXY_OK_HOSTS)不误判。"""
    import curl_cffi.requests as cr
    from urllib.parse import urlsplit
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/138.0.0.0 Safari/537.36")}
    # 2026-09-04 用户指令: 直连探测升级为指纹轮换链(chrome110→chrome136→
    # firefox147→safari18_0) — 只拦单一浏览器指纹的 WAF 不再误判 IP 封禁
    try:
        from util.fingerprint_chain import try_impersonations as _try_imps
        _resp, _prof = _try_imps(url, timeout=timeout, headers=headers,
                                 proxies=None, checker=None)
        if _resp is not None:
            return False  # 任一指纹拿到任何状态码 = 网络可达
    except Exception:
        pass
    # 2026-09-04 中大/兰大/石河子实锤: curl_cffi 指纹被站点 WAF 拦(连接重置/
    # 无响应)但系统 libcurl 可达(实测 302/301) — TLS 指纹级拦截 ≠ IP 封禁。
    # 系统 curl 腿: 任何 HTTP 状态码(含挑战页 4xx)=可达, 交正常链路
    # (smart_fetch 自带 _try_system_curl 兜底)采集; 000/超时=真传输层硬墙。
    try:
        import subprocess as _sp
        _r = _sp.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "10", url],
                     capture_output=True, text=True, timeout=15)
        _code = (_r.stdout or "").strip()
        if _code.isdigit() and _code != "000":
            return False
    except Exception:
        pass
    try:
        host = (urlsplit(url).hostname or "").lower()
        from http_client import _build_proxies, _PROXY_OK_HOSTS  # noqa: F401
    except Exception:
        _PROXY_OK_HOSTS, _build_proxies = set(), (lambda: {})
    if host in _PROXY_OK_HOSTS:
        return False  # 已知代理可达 → 交正常链路, 不判封禁
    try:
        proxies = _build_proxies(host)  # 2026-10-24 dps站级粘性: 必须传host
        if proxies:
            r = cr.get(url, timeout=timeout, verify=False, impersonate="chrome110",
                       headers=headers, proxies=proxies)
            # 446/447/460/461/517 = 代理CONNECT失败(目标DNS死/拒连代理), 非
            # 站点真实响应 — 不可判可达(2026-10-24 北化工nyhjgzs DNS死域名
            # 代理446实锤; 南师大kjc死主机dps代理460实锤: 460漏表被误判
            # 可达 → 裸请求挂240s×21次)
            if r.status_code < 500 and r.status_code not in (446, 447, 460, 461, 517):
                return False  # 代理拿到真实响应(200/412/400...) = 可达
    except Exception:
        pass
    return True


# ── 2026-10-25 用户指令: IP封禁/站不可达类 → 搜索引擎兜底找正确URL,
# 正确性交LLM判断(标题/URL/教师信号/状态码四维证据) — 上游URL缺.cn/改版/
# 错位是死站误判主因(北大深圳医院 pkuszh.com 缺 .cn 实锤: .com.cn 200)。──
_SEARCH_FB_LOCK = _threading.Lock()   # cloak免费版1并发限流, 搜索串行


def _write_dept_url(d: Path, new_url: str):
    """写回院系URL(纪律: 修复必须写回名单条目否则不生效) — info.csv 与
    departments.csv 同步更新。"""
    try:
        ic = d / "info.csv"
        if ic.exists():
            rows = list(csv.DictReader(open(ic, encoding="utf-8")))
            if rows:
                for r in rows:
                    r["院系URL"] = new_url
                with open(ic, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
    except Exception:
        pass
    try:
        dc = d.parent / "departments.csv"
        if dc.exists():
            rows = list(csv.DictReader(open(dc, encoding="utf-8")))
            if rows:
                for r in rows:
                    if r.get("院系名称", "").strip() == d.name:
                        r["院系URL"] = new_url
                with open(dc, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
    except Exception:
        pass


def _search_url_fallback(school, dept, dead_url, d=None):
    """死站搜索引擎兜底: 返回验证过的正确院系URL或None。每户只搜一次
    (.url_search_done 文件标记防每批重复烧), cloak 1并发下全局锁串行;
    候选验证(标题/状态/师资信号)后 LLM 终审判别正确性(用户指令)。"""
    if d is not None and (d / ".url_search_done").exists():
        return None
    if d is not None:
        try:
            (d / ".url_search_done").write_text(
                datetime.now().strftime("%F %T"), encoding="utf-8")
        except Exception:
            pass
    try:
        with _SEARCH_FB_LOCK:
            try:
                import sys as _s
                _s.path.insert(0, str(Path(__file__).resolve().parent / "script"))
                from find_url_via_search import _browser_search, _candidates
            except Exception:
                return None
            cands = []
            for _eng in ("baidu", "bing"):
                try:
                    _html, _e = _browser_search(f"{school} {dept} 师资队伍",
                                                engine=_eng)
                    if _html:
                        cands = _candidates(_html, school, dept)
                        if cands:
                            break
                except Exception:
                    continue
        if not cands:
            return None
        # 候选取证(短超时): 标题/状态/师资信号 — LLM 判别证据
        ev = []
        for _t, _u in cands[:5]:
            if _u.rstrip("/") == (dead_url or "").rstrip("/"):
                continue
            try:
                _h = fetch(_u, timeout=12)
                if _h and len(_h) > 300:
                    _n = len(re.findall(r"师资|教师|教授|讲师|研究员|导师", _h))
                    ev.append(((_t or "")[:60], _u, 200, _n))
                else:
                    ev.append(((_t or "")[:60], _u, 0, 0))
            except Exception:
                ev.append(((_t or "")[:60], _u, 0, 0))
        if not ev:
            return None
        # 正确性交给LLM判断(用户指令)
        try:
            _llm = get_llm()
            _prompt = ("你是高校院系官网URL判别器。给定院系、已死URL和搜索候选, "
                       "选出最可能是该院系官网的URL(域名与学校相关、标题/路径含院系"
                       "或师资信号); 没有把握输出 NONE。只输出URL或NONE。\n"
                       f"学校: {school}\n院系: {dept}\n已死URL: {dead_url}\n候选:\n")
            for _t, _u, _code, _sig in ev:
                _prompt += f"- {_u} | 标题: {_t} | 状态:{_code} | 师资信号:{_sig}\n"
            _ans = (_llm.ask("只输出URL或NONE。", _prompt, temperature=0.1) or "").strip()
            _m = re.search(r"https?://[^\s\"'）)]+", _ans)
            if _m:
                _pick = _m.group(0).rstrip("。.,，")
                if _pick and not _bounded_ip_check(_pick):
                    return _pick
        except Exception:
            pass
        return None
    except Exception:
        return None


def _bounded_ip_check(url, timeout=35):
    """IP 预检硬边界 (2026-09-03 波#5 五僵尸实锤: 南科大4实验室+上科大
    终极能源中心 21:25 进场后 900s 全静默变僵尸, 排查排除入口等待/LLM/
    溯源调和/probe(均有打印), 唯一无打印网络调用即 _ip_unreachable —
    转发器(15819)卡死时 curl 超时存在不生效的挂起形态)。设计上限 ~24s
    (直连 2×8s + 代理 8s), 35s 边界覆盖; 守护线程 join(timeout) 挂起时
    放弃检查假定可达, 交正常链路自己的超时(不误杀可达站点)。"""
    if not url:
        return False
    _box = {}
    _t = _threading.Thread(target=lambda: _box.setdefault("r", _ip_unreachable(url)),
                            daemon=True)
    _t.start()
    _t.join(timeout)
    if _t.is_alive():
        print(f"  ⏱️ IP预检超时({timeout}s)仍挂起, 假定可达继续正常链路(防入口挂死成僵尸)")
        return False
    return bool(_box.get("r", False))


_FETCH_TIMEOUT = object()  # fetch 硬边界超时哨兵(区别于真实 None=不可达)
_FETCH_BOUND = 240  # 秒: 师资页/首页 fetch 硬边界 (2026-09-10 用户指令加长: 120→240, 慢握手站有完整等待窗口)
# 2026-10-25 登录墙/考试平台URL预检(兰大 yxytk smuexam/login 烧240s×N实锤)
# 核查修正: /auth 会误杀 author 内容页(知网/IEEE作者页) — 收紧为边界锚定
# (auth/authorized 后必须跟 / 或 ?), /exam 同理防 example 误杀
_LOGIN_URL_RE = re.compile(r"/login|/signin|smuexam|/(exam|exams)(/|\?)|passport|"
                           r"/cas/|/sso|/(auth|authorized)(/|\?)|/oauth|"
                           r"/logins?$|login\.|\.do\?.*action=login", re.I)


def _bounded_fetch(url, timeout=_FETCH_BOUND):
    """fetch 硬边界 (2026-09-03/04 华工机械与汽车 22:33/23:20 两挂同点实锤:
    prep→faculty页 区间唯一无界调用即 fetch — 波期转发器/渲染层个别形态挂死
    时内部层超时不生效, 900s 僵尸每批重演)。守护线程 join(timeout) 兜底;
    超时返回 _FETCH_TIMEOUT 哨兵(调用方按需转 RETRY, 防误落 SKIP 终态)。
    2026-10-25 登录墙/考试平台URL预检: 兰大 yxytk.../smuexam/login 实锤
    烧 240s×N — 发现链跟到登录页, fetch 层直接短路不进 240s 等待。"""
    if _LOGIN_URL_RE.search(url or ""):
        return None
    _box = {}
    def _run():
        try:
            _box["v"] = fetch(url)
        except Exception:
            _box["v"] = None
    _t = _threading.Thread(target=_run, daemon=True)
    _t.start()
    _t.join(timeout)
    if _t.is_alive():
        print(f"  ⏱️ fetch 超时({timeout}s 硬边界): {url[:80]}")
        return _FETCH_TIMEOUT
    return _box.get("v")


def _run_spider(d: Path, timeout=SPIDER_TIMEOUT):
    """运行院系 spider.py, stdout/stderr 落盘文件而非管道 (2026-09-03 厦大4僵尸
    实锤): capture_output 管道被 spider 拉起的浏览器孙进程继承写端 — spider 退出
    后 communicate() 等 EOF 永久阻塞, fix_one 挂死 900s 变僵尸(时有时无: 取决于
    spider 退出时浏览器是否还活着)。文件 fd 无 EOF 等待, 进程退出即返回;
    顺带留 spider 末日输出(_spider_out/err.log)供僵尸事后解剖。

    2026-09-10 孤儿窗口根修: 超时先 SIGTERM 再宽限 SIGKILL — 此前
    subprocess.run(timeout) 超时直接 SIGKILL, spider 的 atexit 窗口池关闭
    不执行 → DrissionPage 窗口成孤儿(9-11 进程/个) → watchdog 再杀 → 新
    spider 再建, 一天 200+ 轮「建窗→杀窗」的内存螺旋主因。SIGTERM 让
    spider 优雅退出自行关窗, 宽限期后 SIGKILL 兜底只留给真挂死进程。
    """
    _out = d / "_spider_out.log"
    _err = d / "_spider_err.log"
    _fo = _out.open("wb")
    _fe = _err.open("wb")
    try:
        _env = {**_os.environ, "PYTHONPATH": _os.pathsep.join(
                    [str(d.resolve().parents[4]),
                     str(d.resolve().parents[4] / "util")]),
                # 2026-09-10 铁律: spider 子进程强制纯 CPU 链 — 无论父进程
                # 环境如何, spider 内的 smart_fetch 都到不了浏览器层
                # (浏览器是全局最后手段, 并发采集期恒 off)
                "BROWSER_TIERS": "off"}
        p = subprocess.Popen([sys.executable, "spider.py"], cwd=str(d),
                             env=_env, stdout=_fo, stderr=_fe)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.terminate()  # 优雅退出: atexit 关窗口池, 不留孤儿窗口
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()   # 真挂死兜底: 窗口由 watchdog 孤儿清理接管
                p.wait()
            _repair_json_from_csv(d)
            return None, _out, _err
        _repair_json_from_csv(d)  # 2026-09-04: 撕裂写/错名CSV的 JSON 一致性收敛
        return p, _out, _err
    except Exception:
        _repair_json_from_csv(d)  # SIGKILL 超时路径正是 CSV新/JSON旧 高发场景
        return None, _out, _err
    finally:
        _fo.close()
        _fe.close()


def _repair_json_from_csv(d: Path):
    """CSV/JSON 一致性修复 (2026-09-04): hint_prompt 路径的 LLM spider 自行分步
    写 CSV→JSON, 非原子 — 180s SIGKILL 落在两写之间即 CSV 新/JSON 旧
    (协和生医工 CSV 38 vs JSON 3 后, 又见 CSV 5 vs JSON 9 类)。评估 QC 1c 与
    下游提取均以 CSV 为第一记录: 行数不一致时以 CSV 为准原子重生成 teachers.json
    (anchor 字段在不一致场景本就不可信, 置空)。在 _run_spider 尾部执行, 覆盖
    R0/R1/R3 全部调用点; restore_best 复活存量不一致快照也会在下一轮被收敛。"""
    cf = d / "teachers.csv"
    jf = d / "teachers.json"
    try:
        if not cf.exists():
            return False
        rows = list(csv.reader(open(cf, encoding="utf-8")))
        n = sum(1 for r in rows[1:] if len(r) > 2 and r[2].strip())
        if jf.exists():
            try:
                if len(json.load(open(jf, encoding="utf-8")).get("teachers", [])) == n:
                    return False
            except Exception:
                pass  # JSON 损坏 → 同样走重生成
        school = rows[1][0].strip() if len(rows) > 1 and rows[1] else ""
        dept = (rows[1][1].strip() if len(rows) > 1 and len(rows[1]) > 1 else "") or d.name
        teachers = [{"name": r[2].strip(),
                     "title": r[3].strip() if len(r) > 3 else "",
                     "detail_url": r[5].strip() if len(r) > 5 else "",
                     "evidence": {"page": r[6].strip() if len(r) > 6 else "",
                                  "anchor": "", "selector": "faculty-page"}}
                    for r in rows[1:] if len(r) > 2 and r[2].strip()]
        _tmp = d / "teachers.json.tmp"
        _tmp.write_text(json.dumps({"school": school, "dept": dept,
                                    "total": len(teachers), "teachers": teachers},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(_tmp, jf)
        return True
    except Exception:
        return False


def _write_ip_block_mark(d: Path):
    """IP 封禁账本 (2026-09-02 错峰复查): .ip_block_mark 行1=最近封禁时间戳,
    行2=累计封禁次数。供 script/retry_ip_blocked.py 冷却期后复活 FAIL 院系
    (南师大类封禁解封后自动重采, 上限内不无限重试)。"""
    import time as _t
    att = 1
    mp = d / ".ip_block_mark"
    if mp.exists():
        try:
            _lines = mp.read_text(encoding="utf-8").strip().splitlines()
            _prev = int(_lines[1]) if len(_lines) > 1 else 0
            att = _prev + 1
        except Exception:
            att = 1  # 账本损坏 → 重写为第 1 次
    try:
        mp.write_text(f"{int(_t.time())}\n{att}", encoding="utf-8")
    except Exception:
        pass


def _llm_breaker_open():
    """LLM 熔断是否打开(惰性导入 util.llm_gate, 失败即 False 不阻塞采集)。"""
    try:
        from util.llm_gate import breaker_open as _brk
        return bool(_brk())
    except Exception:
        return False


def _llm_breaker_wait(max_wait=720):
    """熔断打开时院系入口等待网关恢复(2026-09-03 提案②): 不进场→不烧队列
    attempts、不落 fail 终态。30s 轮询; 熔断冷却(240s 基数)到期半开即放行。
    波动长(冷却退避至 960s)时最多等 12min(< 900s 院系僵尸预算), 到期返回
    False 由包装器返回 None(批次侧不打标不入队, 恢复后下批重选)。"""
    if not _llm_breaker_open():
        return True
    print("⏳ LLM 熔断打开, 院系入口等待网关恢复(30s 轮询, 最长 12min)...", flush=True)
    waited = 0.0
    while _llm_breaker_open() and waited < max_wait:
        time.sleep(30)
        waited += 30
    ok = not _llm_breaker_open()
    if not ok:
        print(f"⏳ 熔断仍打开(已等 {waited:.0f}s), 本批放弃该院系(不打标不入队, 下批重选)", flush=True)
    return ok


def _recent_wave(window=300):
    """本进程内最近 window 秒是否观察到波动签名(熔断打开/快速失败/调用超时)。
    15 worker 共享同一熔断器: 波动期在跑院系的 LLM 调用被快速失败或挂起,
    3 轮评估结论不可信 — 完成时熔断可能已半开/关闭, 仅查 breaker_open()
    会漏判(2026-09-03 山大耳鼻喉实锤: 完成时半开, URL缺失100% FAIL 落终态)。
    window=300s ≥ 240s 首次冷却, 保证半开期完成的 FAIL 也转 RETRY;
    波结束后快速失败停止 → 恢复正常终态。"""
    if _llm_breaker_open():
        return True
    try:
        from util.llm_gate import last_wave_fail_ts as _lwft
        return (time.time() - _lwft()) < window
    except Exception:
        return False


def _mark_pass(d, n=0, reason=""):
    """2026-10-10 台账口径修复: PASS 必须落盘状态(此前只返回不写
    test_report → 800院系600+ PASS 在未完成台账上隐形)。"""
    try:
        json.dump({"status": "collected", "passed": True,
                   "reason": reason or f"PASS {n}人",
                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def _mark_retry_reason(d, reason):
    """2026-09-04 无效重试封顶(用户指令): 稳定失败原因写 _retry_reason.txt,
    批次端入队时带进队列做 streak 计数(同原因3轮→zombie); 波动类瞬态传 "" 清除,
    不计 streak。

    2026-10-15 RETRY 机制改进(用户指令「按照你的设计改进retry机制」):
    ①reason 为空(波期/瞬态)时写 .retry_wave 标记 — 波期 RETRY 是网关环境问题
    不是院系问题(近3天实锤: 波期占 RETRY 33%/880次/273院系, 被当真堵点烧
    .retry_count), 批次侧见此标记不累计 3 次上限; reason 非空(真堵点)时清标记。
    ②reason 非空时同步落盘 .retry_context.md(跨批反思上下文, ReAct 式): 下批
    fix_one 入场读入注入 LLM 改进提示词 — 带着上一次的问题重新分析, 而不是
    同代码同环境盲重跑(实测翻盘率 0.8%, 翻盘全部来自环境变化或复核成功;
    注入历史原因让 LLM 跨批积累诊断, 复核类翻盘率最高 3-4%)。"""
    try:
        rf = d / "_retry_reason.txt"
        wf = d / ".retry_wave"
        if reason:
            rf.write_text(reason, encoding="utf-8")
            wf.unlink(missing_ok=True)
            # 跨批反思账本: 追加式(上限3条防膨胀), 每条带时间戳
            try:
                cf = d / ".retry_context.md"
                _ts = datetime.now().strftime("%m-%d %H:%M")
                _prev = cf.read_text(encoding="utf-8").strip() if cf.exists() else ""
                _lines = [l for l in _prev.split("\n") if l.strip()]
                _entry = f"[{_ts}] {reason}"
                if not _lines or _lines[-1] != _entry:
                    _lines.append(_entry)
                    cf.write_text("\n".join(_lines[-3:]) + "\n", encoding="utf-8")
            except Exception:
                pass
        else:
            rf.unlink(missing_ok=True)
            wf.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:
        pass


def _sys_curl_ok(url, timeout=12):
    """2026-09-18 十批攻坚改进#1: 系统 curl 分诊(与 curl_cffi 指纹路径独立)。
    返回 (code, ok) — 412=瑞数墙 / 000=不可达 / 2xx-3xx=可达。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), "-L", url],
            capture_output=True, text=True, timeout=timeout + 5)
        code = r.stdout.strip()
        return code, (code.startswith("2") or code.startswith("3"))
    except Exception:
        return "000", False


def _url_variants(fac_url, dept_url="", dept_dir=None):
    """2026-09-20 用户指令「不可达判定依据要充分」: 旧URL死≠站点死
    (重大改版实锤: www.cmse.cqu.edu.cn 死但 cmse.cqu.edu.cn 活;
    male.cqu.edu.cn 死但 mkszy.cqu.edu.cn 活 — 用户浏览器可访问而
    系统判 subnet_dead, 根因=只测了一个旧URL)。

    交叉验证变体(按命中概率排序):
    1. 剥/加 www(同scheme)
    2. scheme 切换(http↔https)
    3. 剥www+scheme切换组合
    4. 主机根路径
    5. 院系主页(info.csv 起点 URL)
    6. departments.csv 同名院系的其他URL记录(域名改版场景:
       "重庆大学马克思主义学院" ← "马克思主义学院: mkszy.cqu.edu.cn")
    """
    import urllib.parse as _up
    out = []
    p = _up.urlparse(fac_url)
    host = p.hostname or ""
    if host:
        if host.startswith("www."):
            out.append(_up.urlunparse(p._replace(netloc=host[4:])))
            out.append(_up.urlunparse(p._replace(netloc=host[4:], scheme="https" if p.scheme == "http" else "http")))
        else:
            out.append(_up.urlunparse(p._replace(netloc="www." + host)))
        out.append(_up.urlunparse(p._replace(scheme="https" if p.scheme == "http" else "http")))
        out.append(_up.urlunparse(p._replace(path="/", params="", query="", fragment="")))
    if dept_url and dept_url.startswith("http") and dept_url != fac_url:
        out.append(dept_url)
    # departments.csv 交叉比对: 同名/互相包含的院系行 URL(改版换域名的救星)
    if dept_dir is not None:
        try:
            import csv as _csv
            _dcsv = dept_dir.parent / "departments.csv"
            if _dcsv.exists():
                _dn = dept_dir.name
                for _row in _csv.DictReader(open(_dcsv, encoding="utf-8")):
                    _n = (_row.get("院系名称") or "").strip()
                    _u = (_row.get("院系URL") or "").strip()
                    if _u.startswith("http") and _n and _n != _dn \
                            and (_n in _dn or _dn in _n or _n.rstrip("（)(") == _dn.rstrip("（)(")):
                        out.append(_u)
        except Exception:
            pass
    seen, res = set(), []
    for u in out:
        if u != fac_url and u not in seen:
            seen.add(u)
            res.append(u)
    return res[:8]


def _root_url(url):
    """院系子域 → 学校根域(www 前缀): art.cnu.edu.cn → https://www.cnu.edu.cn/"""
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


_BROWSER_VERIFY_SEM = _threading.Semaphore(3)  # 9222 tab 有限, 分诊浏览器确认节流
_CLOAK_LOCK = _threading.Lock()  # 2026-09-21 CloakBrowser 免费版 1 并发纪律:
# 全部 cloak 调用全局串行(与 9222 CDP 并发无关) — 实测 25 workers 下合法空
# 复核 2-3 并发 launch(免费版限流风险, 且互相挤占会话), 串行后每样本
# 复核链 ~30-90s 可预测; _fac_family_render 顺序调用无重入问题


def _cloak_fetch(url, timeout=30):
    """2026-09-21 专题研究「无头浏览器能否解决瑞数」结论: 裸 headless
    (9222) 被瑞数识别(45s空白39字节), 但 CloakBrowser headless +
    stealth_args 反检测抹掉 headless 痕迹 → 5-8s 过挑战(数统/材料/马院
    三站实测 20-30KB 真页面)。用于瑞数站浏览器兜底。
    纪律: 免费版 1 并发会话 — 必须串行(调用方节流) + 用后 close。
    2026-09-21 兰大实锤: http 端间歇 301/无响应而 https 直连渲染成功
    (marx.lzu.edu.cn http→0B, https→55KB 真页面) — http 失败自动补试
    https 变体(http死https活是院系URL缺陷家族常见形态)。"""
    _urls = [url]
    if url.startswith("http://"):
        _urls.append("https://" + url[len("http://"):])
    with _CLOAK_LOCK:
        for _u in _urls:
            # 2026-09-22 挂起根治: cloak 同步调用(page.goto/content)在
            # 渲染进程崩溃/CDP断连时永久阻塞(0% CPU) — daemon 线程隔离,
            # 主线程 join 超时放弃; 死线程无害, 残留浏览器由调用方/批次
            # 结束统一清理(浏览器纪律)。挂起连发 2 次判定该 URL 死。
            _r = _cloak_guarded(_u, timeout)
            if _r:
                return _r
    return None


def _render_fetch(url, timeout=35):
    """2026-09-23 用户纠偏: 浏览器分级 — 9222(无并发限制+stealth注入)
    优先, cloak(免费版1并发串行)是最后手段。旧实现 cloak 优先导致
    10 并发下全部排队饿死(批次4/5/6 连片「浏览器渲染均失败」)。
    2026-09-24 长安公路实锤: 9222 对部分站(highway.chd.edu.cn)被
    headless 检测 — JS 表格数据不加载, 返回15KB导航壳(非None)则
    cloak 兜底永不触发 → 名单提取0。壳检测: 有师资栏目词但无教师
    信号 → fallback cloak(反检测)渲染"""
    _h = _browser_fetch(url, timeout=min(timeout, 30))
    if _h and not _is_js_shell(_h):
        return _h
    _ch = _cloak_fetch(url, timeout)
    if _ch:
        return _ch
    return _h


def _is_js_shell(html):
    """JS壳页判定(长安公路型): 有师资/教师栏目词但无教师信号(名职对/
    带链接姓名/姓名网格)。不再要求分页信号 — 9222渲染后的壳可能连
    分页控件都没有(JS数据未加载), 只按「有师资词无名单」判定;
    真无名单页误判壳的代价=一次cloak渲染(结果仍空), 可接受"""
    try:
        # 2026-10-14 字母索引页豁免(云大经济学院 jsjs.htm 实锤): 含 ≥5 个
        # 单字母链接的页面是教师字母索引页(名单在 A-Z 子页), 不是 JS 壳 —
        # 判壳会导致渲染兜底放弃索引页, 字母子页永远不跟进
        from bs4 import BeautifulSoup as _BS4
        _soup = _BS4(html or "", "lxml")
        _letters = {a.get_text(strip=True) for a in _soup.find_all("a", href=True)
                    if re.fullmatch(r"[A-Za-z]", a.get_text(strip=True))}
        if len(_letters) >= 5:
            return False
    except Exception:
        pass
    try:
        txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", html or "", flags=re.S)
        txt = re.sub(r"<[^>]+>", "\n", txt)
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        if not any(re.search(r"师资|教师|导师", l) and len(l) <= 16 for l in lines):
            return False
        for i, l in enumerate(lines):
            # 2026-09-24 修正: 导航词行相邻(师资队伍→硕士导师)会误命中
            # 「导师」判为名职对 — 姓名行必须不在导航词表
            if l in NAV_WORDS or not re.fullmatch(r"[一-鿿·]{2,4}", l):
                continue
            if i + 1 < len(lines) \
                    and re.search(r"教授|副教授|讲师|研究员|导师", lines[i + 1]):
                return False
        return True
    except Exception:
        pass
    return False


def _cloak_guarded(url, timeout=30):
    """daemon 线程隔离跑 _cloak_once, join 超时放弃挂死的调用。"""
    import threading as _th
    _box = {}

    def _run():
        try:
            _box["h"] = _cloak_once(url, timeout)
        except Exception:
            _box["h"] = None

    _t = _th.Thread(target=_run, daemon=True)
    _t.start()
    _t.join(timeout + 15)
    if _t.is_alive():
        return None  # 挂起: 放弃(死线程无害)
    return _box.get("h")


def _cloak_once(url, timeout=30):
    import time as _t
    try:
        import cloakbrowser
    except Exception:
        return None
    b = None
    try:
        b = cloakbrowser.browser.launch(headless=True, stealth_args=True)
        ctx = b.new_context()
        page = ctx.new_page()
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        except Exception:
            pass
        # 等瑞数挑战执行(实测 5-8s; 最多 timeout)
        _deadline = _t.time() + min(timeout, 40)
        _last_len2 = 0
        _stable2 = 0
        while _t.time() < _deadline:
            _t.sleep(3)
            try:
                _c = page.content()
            except Exception:
                break
            if _c and len(_c) > 500:
                # 2026-09-22 南信大实锤: 名单在 iframe(t-inner namexs.jsp)
                # — page.content() 只含主文档; 枚举 frames 合并(跳过主文档)
                try:
                    for _fr in page.frames:
                        try:
                            _fc = _fr.content()
                        except Exception:
                            continue
                        if _fc and len(_fc) > 800 and _fc != _c:
                            _c += "\n<!--frame-->\n" + _fc
                except Exception:
                    pass
            if _c and len(_c) > 500:
                # 2026-09-21 错误页过滤加固: Chromium DNS/连接错误页 title 是
                # 纯域名(不命中下方词表)但必含 main-frame-error 标记 — 实测
                # 东林旧域名 jidian.nefu.edu.cn DNS死 → 187KB 错误页伪装成
                # 有效渲染, find_faculty_page 全失明, 污染「渲染成功但0」结论
                if "main-frame-error" in _c:
                    return None
                _m = re.search(r"<title>(.*?)</title>", _c, re.S)
                if _m and any(_k in _m.group(1) for _k in (
                        "隐私设置错误", "无法访问", "无法连接", "ERR_", "错误页",
                        "安全检查", "网络错误", "404", "页面未找到", "找不到页面",
                        "已被删除")):
                    return None
                # 2026-09-24 长安公路实锤: 壳(15KB)>500即返回, JS表格未加载;
                # 改「长度稳定」判定(连续2次采样不变=DOM停止变化=JS完成)
                if len(_c) == _last_len2:
                    _stable2 += 1
                    if _stable2 >= 2:
                        return _c
                else:
                    _last_len2 = len(_c)
                    _stable2 = 0
        return None
    except Exception:
        return None
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:
                pass


def _browser_fetch(url, timeout=30):
    """2026-09-20 用户指令「不可达的最终确认」: 浏览器打开URL拿渲染后HTML
    (9222 CDP 兜底) — CPU链(curl_cffi/nodenv/系统curl)全失败≠真不可达,
    浏览器渲染是最终仲裁: 浏览器拿到有效页面=站点活着(CPU链指纹/网络
    层误判), 浏览器也打不开(DNS失败/连接拒绝)=真不可达才冻结。
    返回 HTML 或 None。
    2026-09-21 兰大实锤: http 端间歇 301/无响应 — http 失败自动补试
    https 变体(与 _cloak_fetch 同口径)。

    瑞数挑战页实测: goto domcontentloaded 时页面还是 39 字节空白壳
    (挑战JS执行中, 几秒后替换为真内容), goto 也可能被挑战中断 — 故
    goto 异常不立即放弃, 轮询 content 等挑战完成后拿真页面(≤12s)。"""
    _urls = [url]
    if url.startswith("http://"):
        _urls.append("https://" + url[len("http://"):])
    for _u in _urls:
        _r = _browser_once(_u, timeout)
        if _r:
            return _r
    return None


def _browser_once(url, timeout=30):
    import time as _t
    try:
        from util.cdp_util import cdp_page
    except Exception:
        return None
    try:
        with cdp_page() as (page, ctx):
            try:
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            except Exception:
                pass  # 瑞数挑战可能中断导航, 页面稍后稳定 — 交给下方轮询判定
            _deadline = _t.time() + 12
            _last_len = 0
            _stable = 0
            while _t.time() < _deadline:
                try:
                    _c = page.content()
                except Exception:
                    return None
                if _c and len(_c) > 500:
                    # 浏览器错误页过滤(实测: 死域名被DNS劫持成 138KB「隐私设置
                    # 错误」拦截页, 不是站点内容 — 判失败; 同理 net::ERR 类);
                    # 2026-09-21 加固: main-frame-error 标记必判(错误页 title
                    # 是纯域名, 词表不命中 — 东林旧域名 DNS死 187KB 伪装渲染)
                    if "main-frame-error" in _c:
                        return None
                    _m = re.search(r"<title>(.*?)</title>", _c, re.S)
                    if _m and any(_k in _m.group(1) for _k in (
                            "隐私设置错误", "无法访问", "无法连接", "ERR_", "错误页",
                            "安全检查", "网络错误", "404", "页面未找到", "找不到页面",
                            "已被删除")):
                        return None
                    # 2026-09-24 长安公路实锤: domcontentloaded 时 JS 表格
                    # 未渲染(15KB壳>500即返回→提取0), 名单在数秒后 JS
                    # fetch 填充(172KB)。改「长度稳定」判定: 连续2次采样
                    # 长度不变=DOM停止变化=JS完成, 才返回内容
                    if len(_c) == _last_len:
                        _stable += 1
                        if _stable >= 2:
                            return _c
                    else:
                        _last_len = len(_c)
                        _stable = 0
                _t.sleep(1.5)
            return None
    except Exception:
        return None


def _extract_all_teachers(page_url, html, max_pages=8):
    """2026-09-22 生产全量提取(测试链 _extract_all 的生产版): 当前页+
    分页闭包合并去重 — 上科大创业管理实锤 CPU 链采 3 人而浏览器渲染
    全量 30 人, 浏览器兜底提取必须带分页闭包。分类页由调用方的
    _fac_family_render 家族循环兜底, 此处只做分页。"""
    from util.teacher_extract import (pager_pages, extract_teachers_multi,
                                      NAV_WORDS)
    all_t = {}

    def _add(ts):
        for t in ts:
            if t and t[0] and t[0] not in NAV_WORDS:
                all_t.setdefault(t[0], t)

    _add(extract_teachers_multi(html, page_url))
    try:
        _pgs = pager_pages(page_url, html, max_pages=max_pages)
    except Exception:
        _pgs = [page_url]
    for _pu in _pgs[1:max_pages + 1]:
        _ph = _render_fetch(_pu, timeout=30)
        if not _ph or len(_ph) <= 500:
            continue
        _add(extract_teachers_multi(_ph, _pu))
    return list(all_t.values())


def _fac_family_render(fac_url, fac_html, max_pages=3):
    """2026-09-21 合法空误判第三层修复: find_faculty_page 命中的常是
    「师资概况/介绍」叙述页(东林生命科学学院: szjs.htm 只说教授31人副教授
    32人, 真名单在 xyjs1.htm 学院教师页) — 渲染提取 0 不是无数据, 是
    找错了层。fac 页提取 0 时枚举页内师资家族链接(教授/讲师/名单/教师/
    师资/队伍/人员/院士/研究员/导师/名录/带头人等), 逐个渲染提取。
    2026-09-22 北中医中药学院实锤三层导航: 师资→分类页(bssds 博士生导师
    索引)→深层名单页(lczyxbsds_xxx) — 家族页本身是分类页时递归枚举一层。
    返回 [(url, html)] 家族子页列表(渲染成功的), 调用方逐个提取。"""
    from urllib.parse import urljoin

    def _sju(base, rel):
        # 2026-09-22 郑大实锤: base 无尾斜杠丢段(目录形式 URL)
        if base and rel and not base.endswith("/"):
            _tl = base.rstrip("/").rsplit("/", 1)[-1]
            if not _re.search(r"\.(html?|jsp|php|aspx?)$", _tl):
                base = base + "/"
        return urljoin(base, rel)

    def _links(base_url, html):
        out = []
        try:
            for _m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                  html or "", re.S):
                _txt = re.sub(r"<[^>]+>", "", _m.group(2)).strip()
                if not _txt or len(_txt) > 20:
                    continue
                if not (re.search(r"(教授|讲师|名单|教师|师资|队伍|人员|院士|研究员|导师|名录|带头人|一览|名册|成员|专家)", _txt)
                    or (not re.search(r"课程|培养方案|教学大纲|制度|章程|体系|方案|计划", _txt)
                        and bool(re.fullmatch(r"[一-鿿·]{2,8}(系|专业|方向|学科|学部|书院|教研室|研究所|史|学)", _txt)))):
                    continue
                _u = _sju(base_url, _m.group(1))
                if not _u.startswith("http") or _u.rstrip("/") == base_url.rstrip("/"):
                    continue
                if re.search(r"\.(pdf|docx?|xlsx?|jpg|png|gif|zip)$", _u, re.I):
                    continue
                if _u not in out:
                    out.append(_u)
        except Exception:
            pass
        return out

    _out, _seen = [], set()
    for _u in _links(fac_url, fac_html):
        if _u in _seen:
            continue
        _seen.add(_u)
        if len(_out) >= max_pages:
            break
        _h = _render_fetch(_u, timeout=30)
        if _h and len(_h) > 500:
            _out.append((_u, _h))
    # 递归两层: 家族页本身是分类页(其内还有师资分类链接)时跟到深层名单页
    # (2026-09-23 吉大历史实锤: yxrc→系页→学科页 三层导航, 一层不够)
    for _depth in range(2):
        for _pu, _ph in list(_out):
            if len(_out) >= max_pages + 2 + _depth:
                break
            for _su in _links(_pu, _ph)[:2]:
                if _su in _seen:
                    continue
                _seen.add(_su)
                _sh = _render_fetch(_su, timeout=30)
                if _sh and len(_sh) > 500:
                    _out.append((_su, _sh))
                    break
    return _out


def _mark_freeze(school, dept, form="", rounds=2):
    """2026-09-18 十批攻坚改进#1/#5: 形态化深冻账本条目 — 写入 logs/
    _zombie_freeze.json (rounds=2 立即冻结), form 字段供批次端分级 TTL
    (subnet_dead/ruishu_412/js_spa/js_menu=7天, 默认24h)。"""
    try:
        import fcntl as _fc
        _fp = Path("logs/_zombie_freeze.json")
        with open(str(_fp) + ".lock", "w") as _lk:
            _fc.flock(_lk, _fc.LOCK_EX)
            try:
                _fz = json.load(open(_fp, encoding="utf-8"))
            except Exception:
                _fz = {}
            _fz.setdefault(school, {})[dept] = {
                "rounds": rounds,
                "last": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "form": form,
            }
            json.dump(_fz, open(_fp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            _fc.flock(_lk, _fc.LOCK_UN)
    except Exception:
        pass


def fix_one(school, dept_dir, dept_url, max_rounds=3):
    """耗时统计包装 (2026-09-03 用户指令): 各步骤时间写 logs/_dept_timing.log。
    2026-09-04 波期转下载(用户指令): 熔断打开不再12min空等(昨晚3h空转) —
    R1 spider 是确定性 fetch+提取(不依赖LLM), 波期照样落盘数据; 评估/改进
    (全靠LLM)由 _wave_entry 守卫跳过转 RETRY, 波后健康期重跑评估拿终态。"""
    _tm_start(school, dept_dir.name)
    _wave_entry = bool(_llm_breaker_open())
    if _wave_entry:
        print("  🌊 波期转下载: 熔断打开不空等, 直接进场跑R1下载(LLM环节波后补)", flush=True)
    # 2026-09-04 厦大能源/眼科实锤: 入场先等熔断(481s)再 LLM 慢燃(238s)即被
    # 900s 斩 — 等待不占工作预算: touch .work_start 供批次侧把预算锚点顺延到
    # 真正开跑(等待 ≤720s 有界, 真挂死仍有 pick-up+900s 硬底线兜底)。
    try:
        (dept_dir / ".work_start").touch()
    except OSError:
        pass
    res = "EXC"
    try:
        res = _fix_one_impl(school, dept_dir, dept_url, max_rounds,
                            wave_entry=_wave_entry)
        if res == "PASS":
            # 2026-10-25 成功案例库: PASS户沉淀(同域后续户改进提示词注入参考)
            try:
                from collect_kb import record_success_case
                _s_url, _s_n = "", 0
                try:
                    _ir = list(csv.DictReader(open(dept_dir / "info.csv", encoding="utf-8")))
                    _s_url = (_ir[0].get("院系URL") or "") if _ir else ""
                except Exception:
                    pass
                try:
                    _sr = list(csv.reader(open(dept_dir / "teachers.csv", encoding="utf-8")))
                    _s_n = len(_sr) - 1 if _sr else 0
                except Exception:
                    pass
                record_success_case(f"{school}/{dept_dir.name}", {
                    "dept_url": _s_url, "n": _s_n,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            except Exception:
                pass
        if res == "FAIL" and _recent_wave():
            res = "RETRY"
    finally:
        _tm_flush(res)
    return res


def _fix_one_impl(school, dept_dir, dept_url, max_rounds=3, wave_entry=False):
    # 任务边界: 新学院重新均衡分配 token, 学院内连续调用固定同一 token (命中网关缓存省钱)
    # 并发提示: begin_task 只影响当前线程; main() 串行遍历院系, 各院系在本函数开头调用即可。
    _llm0 = get_llm()
    if _llm0 is not None:
        _llm0.begin_task(school)  # 学院亲和: 固定token槽位, 跨批次命中prompt缓存(2026-08-23)
    dept = dept_dir.name
    d = dept_dir
    print(f"\n{'─'*56}\n🔧 {dept}\n{'─'*56}")

    # 2026-09-02 IP级封禁前置检测(用户指令: 连接拒绝/超时直接 FAIL, 不浪费
    # probe/LLM/重试 — 南师大实测 全层 blocked 28s 纯浪费): TCP 层直连+代理
    # 双核查, 全不通 → 立即 FAIL 终态(留 test_report 证据), 跳过后续全部流程。
    if dept_url:
        try:
            if _bounded_ip_check(dept_url):
                # 2026-10-25 用户指令: 死站类搜索引擎兜底 — 直接FAIL前先搜
                # 「学校 院系 师资」找正确URL(缺.cn/改版/错位是死站主因),
                # 正确性交LLM判断; 命中写回名单转RETRY, 未命中才落FAIL
                _fb = _search_url_fallback(school, dept, dept_url, d)
                if _fb:
                    _write_dept_url(d, _fb)
                    print(f"  🔍 搜索兜底命中: {dept_url[:55]} → {_fb[:60]}")
                    print(f"  ↪ 已写回院系URL, 转 RETRY 待重测")
                    try:
                        json.dump({"status": "pending_recollect", "passed": False,
                                   "reason": f"搜索引擎兜底修正院系URL: {dept_url} → {_fb}",
                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                  open(d / "test_report.json", "w"),
                                  ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    return "RETRY"
                print(f"  🚫 IP级封禁/站点不可达(TCP直连+代理均不通) → 直接 FAIL 终态: "
                      f"{dept_url[:70]}")
                try:
                    json.dump({"status": "fail_ip_blocked", "passed": False,
                               "reason": "IP级封禁/站点不可达(TCP连接拒绝/超时, 直连+代理均不通), "
                                         "按用户指令直接 FAIL 不重试",
                               "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                              open(d / "test_report.json", "w"),
                              ensure_ascii=False, indent=2)
                except Exception:
                    pass
                _write_ip_block_mark(d)  # 2026-09-02 错峰复查账本
                return "FAIL"
        except Exception:
            pass

    # 2026-08-31 学院/学部/系先验: 教学组织必有师资页, "无师资页/无教师信号"
    # 几乎必然是壳/超时/网络问题(北理工机电/自动化/管理/马院 0人合法空误判教训)
    # — 教学组织不落合法空终态, 转 RETRY 入队重试; 书院/医院/继续教育/中心才允许。
    dept_type = ""
    try:
        dcsv = d.parent / "departments.csv"
        for _r in csv.DictReader(open(dcsv, encoding="utf-8")):
            if _r.get("院系名称", "").strip() == dept:
                dept_type = (_r.get("院系类型") or "").strip()
                break
    except Exception:
        pass
    _teaching_org = ((dept_type in ("学院", "学部", "系")
                      or (dept_type == "部"
                          and any(k in dept for k in ("教学", "教研", "体育"))))
                     and dept not in NO_TEACHER_ORGS)


    def _org_faculty_expectation(dept_type: str, dept: str) -> str:
        """2026-10-12 用户指令(机构师资预期分级, 用户原话: 书院/继续教育大概率
        没有; 研究院/实验室可能有; 医院一般是有的):
          likely   = 大概率有师资(医院/附属医院) → 双查无师资转人工复核终态,
                    不可达转 RETRY, 绝不落无师资 SKIP/合法空
          may      = 可能有(研究院/实验室/研究所/中心/基地) → 无师资判定必须
                    浏览器渲染确认, 渲染失败(浏览器不可用)一律 RETRY 待复核
          unlikely = 大概率无(书院/继续教育/培训/荣誉/国际学院等) → 双查失败
                    可落无师资终态(维持旧行为)
        教学组织(学院/学部/系)由 _teaching_org 分支处理, 不进此函数。"""
        n = (dept_type or "") + (dept or "")
        if "医院" in n:
            return "likely"
        # 2026-10-13 体育教学研究部实锤(新疆大学): 「部」类含教学/教研/体育
        # = 教学组织形态(体育教师/公共课教师), 不能按 unlikely 落无师资
        if "教学部" in n or "教研部" in n or "体育部" in n:
            return "may"
        if any(k in n for k in ("研究院", "实验室", "研究所", "中心", "基地")):
            return "may"
        return "unlikely"

    # R-1: 溯源调和 — 起点必须自洽(hash(spider.py)==meta.sha256), 否则快照/回滚
    # 会把不一致状态原样恢复, 评估器"产物溯源"必败。调和: 重跑当前 spider.py
    # 让 CSV/JSON 重新成为 spider 的产物, 再补写 meta(老模板不自写)。
    try:
        _mp = d / "spider_meta.json"
        if (d / "spider.py").exists() and _mp.exists():
            _want = json.load(open(_mp, encoding="utf-8")).get("spider_sha256")
            if _want and hashlib.sha256((d / "spider.py").read_bytes()).hexdigest() != _want:
                print("  🔁 溯源调和: 重跑当前spider使CSV/JSON/meta自洽")
                _run_spider(d)  # 落盘文件而非管道: 防浏览器孙进程持管道写端致 communicate() 死锁
                _sync_meta(d, school, dept)
    except Exception:
        pass

    # 备份已有最佳状态(单调改进: 循环结果绝不差于起点)
    # 4 个产物文件一起快照/恢复: 只恢复CSV会导致 spider_meta.json 溯源 hash
    # 与恢复后的 spider.py 失配, evaluate 的"产物溯源"检查会误报
    best_snapshot: dict[str, bytes] = {}
    best_csv_rows = []
    for fn in ("spider.py", "teachers.csv", "teachers.json", "spider_meta.json"):
        p = d / fn
        if p.exists():
            best_snapshot[fn] = p.read_bytes()
    if (d / "teachers.csv").exists():
        try:
            best_csv_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
        except Exception:
            best_csv_rows = []
    best_n = len(best_csv_rows) - 1 if best_csv_rows else 0
    # 2026-10-08 渲染优先标记按次生效: 上次运行的渲染兜底产物已进入本次
    # 入场快照(自然受回滚保护), 旧标记不延续 — 否则后续运行永久跳过LLM改进轮
    try:
        (d / ".render_priority").unlink(missing_ok=True)
    except Exception:
        pass
    _NAME_RE = re.compile(r"^[一-鿿·]{2,4}$")
    # P0: 跨轮积累评估器的非名清单(LLM终审+启发式), 质量计数与判定同口径
    _bad_names = set()
    best_clean = sum(1 for r in best_csv_rows[1:]
                     if len(r) > 2
                     and _NAME_RE.match(_norm_name(r[2].strip()))
                     and _norm_name(r[2].strip()) not in NAV_WORDS)

    def restore_best():
        # 2026-10-08 渲染产物优先: 浏览器兜底写出产物后, 无脑豁免回滚会让
        # 「spider全败渲染兜底」用更小的渲染产物覆盖更大的历史CSV(全库-15
        # 实锤) — 豁免必须带质量比较: 渲染产物不低于快照才跳过回滚(单调改进)
        if (d / ".render_priority").exists():
            try:
                _cur = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
                _cur_n = len(_cur) - 1 if _cur else 0
                _cur_clean = sum(1 for r in _cur[1:]
                                 if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                                 and _norm_name(r[2].strip()) not in NAV_WORDS)
                _cur_score = _cur_clean * 2 - (_cur_n - _cur_clean)
                _best_score = best_clean * 2 - (best_n - best_clean)
                if _cur_score >= _best_score - 1:
                    print("     🌐 渲染产物不劣于快照 — 豁免回滚")
                    return
                print(f"     🌐 渲染产物劣于快照({_cur_score}<{_best_score}) — 按正常回滚")
            except Exception:
                return
        if best_n >= 3:
            for fn, data in best_snapshot.items():
                (d / fn).write_bytes(data)
            print(f"     🔄 恢复最佳状态({best_n}人)")

    _tm_mark("prep")
    # 找师资页（回退链: analysis.md选定页 → 存量产物 → 首页关键词发现）
    # probe 的选定页带 BFS+教师信号证据, 优于首页关键词匹配 — 首页"师资队伍"
    # 常指向空壳页而 probe 已找到"专职教师"详情页(文化产业管理学院 0 人教训)
    # 深度分析判定: probe 产物含 选定师资页 标记; 浅层/缺失则先补跑 probe,
    # 保证"新学校无人干预"时 probe→fix 顺序正确(浅层分析曾让8个CUC院系盲试)
    _af0 = d / "analysis.md"
    _deep = _af0.exists() and "选定师资页" in _af0.read_text(encoding="utf-8", errors="ignore")
    # 2026-10-08 提速: probe 空探(未找到选定师资页)24h 内不重探 — 硬院系
    # 每次 4h 重试都重跑 600s probe 子进程, 30 并发下并发 probe 是内存墙
    # 主源(资源守卫 red 回收 9222 振荡实锤), 空探结果 24h 内复用
    _pe0 = d / ".probe_empty"
    # 2026-10-25 串行结构优化(用户指令「串行轮次改进了吗」): probe 改后台
    # Popen 与 R1/R2 并行 — 分析产物只在改进轮消费(见 analysis_ctx 读取点),
    # 慢 probe(≤600s)不再挡住房子的前两轮; 改进轮前 wait(≤300s) 收割
    _pp = None
    _pp_files = None
    if not _deep:
        _probe = BASE / "script" / "probe_site.py"
        if _probe.exists() and not (_pe0.exists()
                                    and (time.time() - _pe0.stat().st_mtime) < 24 * 3600):
            print("  🔍 后台 probe 与 R1 并行 ...")
            try:
                _po = open(d / "_probe_out.log", "wb")
                _pe = open(d / "_probe_err.log", "wb")
                _pp = subprocess.Popen([sys.executable, str(_probe), school, "--dept", dept],
                                       stdout=_po, stderr=_pe)
                _pp_files = (_po, _pe)
                # 孤儿兜底(核查修正): 早期 return 的户不会走到改进轮收割 —
                # 守护线程 600s 后强杀, 防后台 probe 孤儿累积
                def _kill_probe_later(proc, secs=600):
                    def _f():
                        time.sleep(secs)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    _threading.Thread(target=_f, daemon=True).start()
                _kill_probe_later(_pp)
            except Exception:
                _pp = None
        elif _pe0.exists():
            print("  ⏭️ 24h内probe已空探, 跳过重探(2026-10-08提速)")
    fac_url = ""
    fac_urls = []          # 同组采集多入口(武大计算机 szdw/ 四分类等)
    fac_api = None         # POST AJAX 教师接口 {"url":..., "data":{...}} (空壳站)
    fac_html_early = None
    try:
        af = d / "analysis.md"
        if af.exists():
            _af_text = af.read_text(encoding="utf-8")
            # 2026-08-26: AJAX 教师接口(probe 检测到空壳站 POST API 时写入)
            _am = re.search(r"\*\*AJAX教师API\*\*:\s*(\S+)", _af_text)
            if _am and "||" in _am.group(1):
                _aurl, _adata = _am.group(1).split("||", 1)
                fac_api = {"url": _aurl,
                           "data": {_kv.split("=", 1)[0]: _kv.split("=", 1)[1]
                                    for _kv in _adata.split("&") if "=" in _kv}}
                print(f"  📡 AJAX教师API: {_aurl[:70]} data={_adata[:60]}")
            m = re.search(r"\*\*选定师资页\*\*:\s*(\S+)", _af_text)
            if m and m.group(1).startswith("http"):
                cand = m.group(1)
                if _NAV_PAGE_RE.search(cand):
                    print(f"  ⚠️ analysis.md 选定师资页疑似导航页({cand[:60]}), 不复用走回退链")
                    # 2026-09-04 南科大第三波假PASS: 污染spider硬编码导航页
                    # (FACULTY_URLS=['/zh/faculty/',...]), spider.py 存在即复用 →
                    # 44人假PASS复发。拒用导航页时同步作废旧spider, 强制从
                    # 重新发现的师资页重生(发现失败则按无师资页走, 不跑旧spider)
                    for _sf in ("spider.py", "spider_meta.json"):
                        (d / _sf).unlink(missing_ok=True)
                else:
                    _h = _bounded_fetch(cand)
                    if _h is _FETCH_TIMEOUT:
                        _h = None
                    if _h:
                        try:
                            from eval_elements import count_teacher_elements as _cte
                            _ne0, _ = _cte(_h, cand)
                        except Exception:
                            _ne0 = -1  # 计数异常不拦(不阻塞复用)
                        if _ne0 == 0:
                            # 复用页无任何教师信号(壳/导航/空页), 钉死它只会
                            # 3 轮全空→URL缺失100%落终态; 走回退链由同组扫描/
                            # 确定性分类页重选
                            print(f"  ⚠️ analysis.md 选定师资页无教师信号({cand[:60]}), 不复用走回退链")
                        else:
                            fac_url = cand
                            fac_html_early = _h
                            print(f"  📄 复用 analysis.md 选定师资页: {fac_url[:60]}")
                    else:
                        print(f"  ⚠️ analysis.md 选定师资页不可达({cand[:60]}), 走回退链")
            gm = re.search(r"\*\*同组采集\*\*:\s*(.+)", _af_text)
            if gm:
                for u in gm.group(1).split(","):
                    u = u.strip()
                    if u.startswith("http") and u != fac_url and not _NAV_PAGE_RE.search(u):
                        # 2026-09-28 川大艺术实锤: probe 同组采集把页脚外站
                        # (mct.gov.cn 文化部等)并入 fac_urls → spider 白抓外站。
                        # 同组入口必须同注册域
                        if _reg_domain(u) != _reg_domain(dept_url or fac_url):
                            continue
                        # 2026-09-29 华中农大外语450实锤: probe 同组把兄弟学院
                        # 裸域名(my./emc.hzau.edu.cn)并入 → 全校名单全采。
                        # 跨子域 URL 须含名单页特征才收(系子站首页由枚举链的
                        # 系锚另行加入)
                        _dup_net = urlparse(u).netloc != urlparse((dept_url or fac_url)).netloc
                        if _dup_net:
                            # 校主站 www 硬拒(南大智能软件275: www.nju.edu.cn 院士列表混入)
                            if urlparse(u).netloc.startswith("www."):
                                continue
                            if not re.search(
                                    r"jsml|jzml|minglu|jslb|szml|faculty|szgk|list", u, re.I):
                                continue
                        fac_urls.append(u)
                if fac_urls:
                    print(f"  📄 同组采集多入口({len(fac_urls)} 个): {', '.join(x[:50] for x in fac_urls[:3])}...")
    except Exception:
        pass
    # 2026-08-26 确定性同组分类页扫描(用户4案例: 交大环院/药院/上大马院只抓到部分):
    # probe 的"同组采集"按教师信号验证, 职称分类页(教授/副教授/讲师/博士后)信号弱
    # 或被过滤时会漏 — 马院只抓到教授(js.htm), 副教授/讲师/博士后 3 页全丢。
    # 这里直接按链接文本规则并入 fac_urls, 不依赖 LLM/信号。过滤 招聘/概况/风采/
    # 宣传/新闻 类(用户明示: 过滤招聘与宣传性质栏目)。
    if fac_html_early:
        from util.group_words import GROUP_KW as _GROUP_KW, GROUP_EXC as _GROUP_EXC, is_group_link
        _gsoup = BeautifulSoup(fac_html_early, "lxml")
        _extra = []
        for _a in _gsoup.find_all("a", href=True):
            _t = _a.get_text(strip=True)
            _hr = _a.get("href", "").strip()
            if not _hr or _hr.startswith(("javascript", "#", "mailto", "tel:")):
                continue
            if len(_t) > 24 or len(_t) < 2:
                continue
            # 2026-09-15 上科大测试实锤: "张蕴贤 教学副教授" 这类教师条目链接
            # (姓名+空白+职称粘连) 会被职称词误收为分类页, 污染 fac_urls。
            # 教师条目形态=姓名后必有空白; 分类页锚文本(常任序列教授/访问教授)
            # 姓名与职称间无空白 — 若用 \s* 会把真分类页一并跳过(实测教训),
            # 必须 \s+ 且职称词作整体后缀匹配(覆盖 教学副教授/常任序列教授)。
            # 2026-09-16 合工大电气实锤: "教授 研究员" 纯职称组合分类tab
            # 被误当教师条目跳过 — 纯职称词组合放行
            _t_pure = re.sub(r"[\s/]+", "", _t)
            if not re.fullmatch(r"(?:教授|研究员|副教授|副研究员|实验师|高级实验师|"
                                r"讲师|助理研究员|助教|研究实习员|助理实验师|博士生导师|"
                                r"硕士生导师|博导|硕导|工程师|高级工程师)+", _t_pure):
                if re.match(r"^[一-鿿·]{2,4}\s+", _t) and re.search(
                        r"教授|讲师|助教|研究员|博士后|工程师|博导|硕导", _t):
                    continue
            # 2026-09-15 北化工机电实锤: 职称档名单页(正高级/副高级/中级及其他/
            # 高层次人才)是唯一教师名单入口, KW 表无这些词 → 发现链只收中心
            # 文章列表页(0教师), 164 人全丢 — 职称档精确词补入;
            # 地大(北京)工程技术再实锤: "正高职称教师/副高职称教师/中级职称教师"
            # 变体 — 通用形态 (正高|副高|中级)(职称)?(教师|及其他)?
            _t_ok = (_GROUP_KW.search(_t)
                     or re.search(r"教师$|科研人员|科研团队|研究员", _t)
                     # 2026-10-25 北师教育学部实锤: 师资栏目 index 是壳, 教师名录
                     # 在「教学科研系列」等二级子页 — 「系列」尾词收为分类页
                     or re.search(r"系列$", _t)
                     or re.fullmatch(r"(正高级|正高|副高级|副高|中级)(职称)?(教师|及其他)?|高层次人才|初中级", _t))
            _t_bad = _GROUP_EXC.search(_t) or re.search(r"平台|中心|课题组|实验室|基地|实验", _t)
            if not _t_ok or _t_bad:
                continue
            _u = urljoin(fac_url, _hr)
            if _u in (_seen_g := {fac_url.rstrip("/")} | set(fac_urls)):
                continue
            # 2026-09-29 华中农大外语450实锤同源: 确定性同组分类页扫描也会
            # 收到兄弟学院裸域名 — 跨子域且无名单特征不收
            if (urlparse(_u).netloc != urlparse(fac_url).netloc
                    and not re.search(r"jsml|jzml|minglu|jslb|szml|faculty|szgk|list",
                                      _u, re.I)):
                continue
            _extra.append(_u)
        # 2026-09-15 用户指出「分职称/分专业展示时很可能遗漏」, 三类补漏:
        # ① 下拉式分类(<select><option>) — 央美 ?xy= 国美 ?at= 类职称/系别筛选,
        #    无 <a> 链接, 历史扫描完全看不见; ② 裸系名/专业名 tab 链接
        #    (如 数学系/物理系/应用统计 2-8 字, 同域名内) — 分专业展示的入口;
        #    ③ 职称 tab 文本可能是按钮式 span 内嵌 a(文本仅"教授"两字, 原 KW
        #    已覆盖, 但 len<2 被拦过 — 已由 ≥2 兜住)。
        _fac_host = urlparse(fac_url).netloc
        for _sel in _gsoup.find_all("select"):
            for _op in _sel.find_all("option"):
                _t = _op.get_text(strip=True)
                _v = (_op.get("value") or "").strip()
                if not _v or _v.startswith(("javascript", "#")):
                    continue
                if not _GROUP_KW.search(_t) and not re.search(r"系$|专业$|学部$", _t):
                    continue
                _u = urljoin(fac_url, _v)
                if _u in (_seen_g := {fac_url.rstrip("/")} | set(fac_urls)):
                    continue
                _extra.append(_u)
        for _a in _gsoup.find_all("a", href=True):
            _t = _a.get_text(strip=True)
            _hr = _a.get("href", "").strip()
            if not _hr or _hr.startswith(("javascript", "#", "mailto", "tel:")):
                continue
            # 2026-09-15 矿大机电实锤: "机械电子工程系（机械学科）" 括号说明
            # 形态, fullmatch 整串以系结尾 → 全漏; 先剥括号再匹配, 上限 12 字
            _t2 = re.sub(r"[（(].*?[）)]", "", _t).strip()
            if not re.fullmatch(r"[一-鿿·]{2,12}(系|专业|方向|学科|学部|书院|教研室|研究所)", _t2):
                continue
            if _GROUP_EXC.search(_t):
                continue
            _u = urljoin(fac_url, _hr)
            # 同域且非导航根, 防组织机构导航误入
            if urlparse(_u).netloc != _fac_host or _u.rstrip("/") in ("/", ""):
                continue
            if _u in (_seen_g := {fac_url.rstrip("/")} | set(fac_urls)):
                continue
            _extra.append(_u)
        if _extra:
            fac_urls = fac_urls + _extra[:12]
            print(f"  📂 确定性同组分类页({len(_extra[:12])} 个): "
                  f"{', '.join(x[:44] for x in _extra[:4])}...")
        # 2026-09-15 用户指令: 分页探测前置到发现链 — 师资页存在分页时
        # (上科大 crjs 2页/847 3页 "下一页>>"/"尾页") 把分页页显式并入
        # fac_urls, 不依赖 spider 运行时跟随(LLM 改进产物可能丢分页跟随,
        # 入口兜底防翻页漏采)
        try:
            if fac_html_early:
                from util.teacher_extract import pager_pages as _pp
                _pgs = _pp(fac_url, fac_html_early)
                _paged = [u for u in _pgs[1:] if u not in set(fac_urls)]
                if _paged:
                    fac_urls = fac_urls + _paged[:8]
                    print(f"  📄 分页探测: 师资页含 {len(_paged[:8])} 个翻页页 → 并入入口")
        except Exception:
            pass
    if not fac_url:
        # 回退1: 复用已有产物记录的师资页URL(teachers.csv 第7列 / teachers.json.faculty_url)
        try:
            if (d / "teachers.csv").exists():
                for row in csv.reader(open(d / "teachers.csv", encoding="utf-8")):
                    if len(row) > 6 and row[6].strip().startswith("http"):
                        fac_url = row[6].strip()
                        if _NAV_PAGE_RE.search(fac_url):
                            # 2026-09-04 南科大量子点706人/太阳能395人假PASS实锤:
                            # 08-30污染CSV记录的 letter/ 师资页被复用, 实验室被
                            # 全校字母索引页顶替 — 弃用走发现链(失败则按无师资页)
                            print(f"  ⚠️ teachers.csv 师资页URL是导航页({fac_url[:60]}), 弃用")
                            fac_url = ""
                            break
                        print(f"  📄 复用 teachers.csv 师资页URL: {fac_url[:60]}")
                        # 2026-09-15 归属校验(上游判责: 书院类全串到校级名师页/串到
                        # 别家教学组织域名): 复用页标题若含同校其他院系名且不含
                        # 本院系名 → 判串页, 弃用走发现链(判责两步法第1步落地)。
                        try:
                            _other_names = []
                            _dcsv = d.parent / "departments.csv"
                            if _dcsv.exists():
                                for _r in csv.DictReader(open(_dcsv, encoding="utf-8")):
                                    _n = (_r.get("院系名称") or "").strip()
                                    if len(_n) >= 4 and _n != dept:
                                        _other_names.append(_n)
                            _rh = _bounded_fetch(fac_url)
                            if _rh and _rh is not _FETCH_TIMEOUT:
                                _rs = BeautifulSoup(_rh, "lxml")
                                _title = ((_rs.title.get_text(strip=True) if _rs.title else "")
                                          + " " + _rs.get_text(" ", strip=True)[:300])
                                if dept not in _title:
                                    for _n in _other_names[:80]:
                                        if _n in _title:
                                            print(f"  ⚠️ 复用页归属校验失败: 内容属「{_n}」非「{dept}」, 弃用走发现链")
                                            fac_url = ""
                                            break
                        except Exception:
                            pass
                        break
        except Exception:
            pass
        if not fac_url:
            try:
                jf = d / "teachers.json"
                if jf.exists():
                    fu = (json.load(open(jf, encoding="utf-8")) or {}).get("faculty_url", "")
                    if str(fu).startswith("http"):
                        fac_url = str(fu)
                        if _NAV_PAGE_RE.search(fac_url):
                            print(f"  ⚠️ teachers.json faculty_url是导航页({fac_url[:60]}), 弃用")
                            fac_url = ""
                        else:
                            print(f"  📄 复用 teachers.json faculty_url: {fac_url[:60]}")
            except Exception:
                pass
    # 2026-09-13: fac_url 走 teachers.csv/json 复用路径时下面 if 不执行, home 未赋值;
    # 而 analysis.md 缺失的院系会在生成块读 home → UnboundLocalError 每轮白烧 attempts。
    # 前置默认 None, 复用路径跳过「首页分析」小节(本来就没有首页素材)。
    home = None
    if not fac_url:
        # 回退2: 首页关键词发现(最弱, 仅兜底)
        home = _bounded_fetch(dept_url) if dept_url.startswith("http") else None
        if home is _FETCH_TIMEOUT:
            home = None
        if home:
            fac_url = find_faculty_page(dept_url, home)
            if fac_url and _NAV_PAGE_RE.search(fac_url):
                print(f"  ⚠️ 发现链误选导航页({fac_url[:60]}), 弃用按无师资页处理")
                fac_url = ""
    if not fac_url:
        # 2026-09-24 用户点名能力拓展: 师资入口不只挂首页/「学院概况」,
        # 科学研究板块下常有导师队伍(成都理工传艺 kxyj/dsdw.htm 实锤,
        # 首页JS菜单不露链接, CPU链在首页找不到就放弃了)。
        # CPU链加二级栏目枚举: 抓首页「概况/科研」类栏目链接(≤5个),
        # 在这些栏目页上 find_faculty_page(词表已含导师队伍/科研团队)
        try:
            from urllib.parse import urljoin as _uj2
            _sub2 = []
            for _m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                  home or "", re.S):
                _tx2 = re.sub(r"<[^>]+>", "", _m.group(2)).strip()
                if re.search(r"(学院概况|关于我们|组织机构|学院简介|本系概况|学部概况"
                             # 2026-10-24 医院专题: 医院首页导航是"专家介绍/医师团队"
                             # 而非师资 — 二级栏目枚举补医院词(高校页无此词无影响)
                             r"|科学研究|科研概况|科研|学术研究|导师|师资|专家|医师|医疗)", _tx2) and len(_tx2) <= 12:
                    _su2 = _uj2(dept_url if dept_url.endswith("/") else dept_url + "/",
                                _m.group(1))
                    if _su2.startswith("http") and _su2 not in _sub2:
                        _sub2.append(_su2)
            for _su2 in _sub2[:5]:
                _sh2 = _bounded_fetch(_su2)
                if not _sh2 or _sh2 is _FETCH_TIMEOUT or len(_sh2) < 500:
                    continue
                _sf2 = find_faculty_page(_su2, _sh2)
                if _sf2 and not _NAV_PAGE_RE.search(_sf2):
                    print(f"  🌐 CPU栏目页({_su2[:45]}…)发现师资入口({_sf2[:50]}) → 采用")
                    fac_url = _sf2
                    break
        except Exception:
            pass
    if not fac_url:
        # 2026-09-21 误判根修: 兜底浏览器必须 CloakBrowser(反检测)优先 —
        # 9222 裸 headless 对反爬站(非瑞数也有headless检测)返回0字节空白,
        # 与CPU链同归于尽 → 「双查无师资」全批误判(东林机电/人大党史党建等
        # 5样本实测9222全0字节、cloak全成功)。cloak免费版1并发, 由信号量节流。
        # 2026-09-21 SEM饥饿修复: cloak 直调(锁内置, 不抢 SEM 槽); 9222 兜底走 SEM
        _bh = _render_fetch(dept_url)
        if not _bh and _BROWSER_VERIFY_SEM.acquire(timeout=30):
            try:
                _bh = _browser_fetch(dept_url, timeout=25)
            finally:
                _BROWSER_VERIFY_SEM.release()
        if _bh and len(_bh) > 500:
            _bfac = find_faculty_page(dept_url, _bh)
            if _bfac and not _NAV_PAGE_RE.search(_bfac):
                print(f"  🌐 浏览器渲染发现师资页({_bfac[:60]}), CPU链不可见 → 采用")
                fac_url = _bfac
            elif _bfac and _NAV_PAGE_RE.search(_bfac):
                pass
            else:
                # 2026-09-22 西南交大化学实锤: 师资入口在「学院概况/关于我们」
                # 二级栏目页(首页无直接师资链接, xygk.htm→xygk/szdw.htm 27人)
                # — 枚举首页栏目页渲染后递归 find_faculty_page(限3个)
                from urllib.parse import urljoin as _uj
                _sub = []
                try:
                    for _m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                          _bh, re.S):
                        _tx = re.sub(r"<[^>]+>", "", _m.group(2)).strip()
                        # 2026-09-24 用户点名能力拓展: 师资入口不只挂「学院概况」,
                        # 科学研究板块下常有导师队伍(成都理工传艺 kxyj/dsdw.htm
                        # 实锤) — 枚举词加「科学研究/科研/科研概况/研究」,
                        # 二级页递归 find_faculty_page 会命中新增的导师词表
                        if re.search(r"(学院概况|关于我们|组织机构|学院简介|本系概况|学部概况"
                                     r"|科学研究|科研概况|科研|学术研究)", _tx) and len(_tx) <= 12:
                            _su = _uj(dept_url + "/" if not dept_url.endswith("/") and not _re.search(r"\.(html?|jsp|php|aspx?)$", dept_url.rstrip("/").rsplit("/", 1)[-1]) else dept_url, _m.group(1))
                            if _su.startswith("http") and _su not in _sub:
                                _sub.append(_su)
                except Exception:
                    _sub = []
                for _su in _sub[:5]:  # 2026-09-24 概况+科研板块词拓展后候选增多, 上限3→5
                    _sh = _render_fetch(_su, timeout=30)
                    if not _sh or len(_sh) <= 500:
                        continue
                    _sf = find_faculty_page(_su, _sh)
                    if _sf and not _NAV_PAGE_RE.search(_sf):
                        print(f"  🌐 二级栏目页({_su[:40]}…)发现师资入口({_sf[:50]}) → 采用")
                        fac_url = _sf
                        break
    if not fac_url:
        # 2026-10-08 资源守卫回收死窗重试: red 档内存回收 9222 时渲染请求
        # 撞进死窗(南大管理/经济「浏览器不可用」RETRY 实锤, 一小时3次回收) —
        # 死窗 2-5s 即被按需重启恢复, 补一次 10s 后重渲染+家族枚举可救回
        if not _bh or len(_bh) <= 500:
            time.sleep(10)
            _bh = _render_fetch(dept_url, timeout=30)
            if _bh and len(_bh) > 500:
                for _fu, _fh in _fac_family_render(dept_url, _bh):
                    _sf = find_faculty_page(_fu, _fh)
                    if _sf and not _NAV_PAGE_RE.search(_sf):
                        fac_url = _sf
                        print(f"  🌐 死窗恢复后家族枚举命中师资入口({_sf[:50]}) → 采用")
                        break
        if not fac_url:
            print("  ❌ 无师资页(发现链+浏览器渲染均失败)")
            _bh_ok = bool(_bh and len(_bh) > 500)
            _bh_status = "渲染后确认无师资" if _bh_ok else "渲染失败(浏览器不可用,需复查)"
            if _teaching_org:
                # 2026-09-21 方案A撤销(用户人工验证10样本全部有师资): 教学组织
                # 双查无师资绝不落终态/SKIP — 与评估链「学院型合法空不落终态」
                # (1669行浏览器复核)同口径: RETRY 待复核(attempts超限走深冻队尾,
                # 不会无限磨坊)。原方案A残留(SKIP+合法空文案)已随方案撤销删除。
                # 2026-09-23 批次8实锤: 9222 中途死亡时「渲染后无师资」文案与
                # 「渲染失败(浏览器不可用)」混淆 — 区分两者(处置同为RETRY安全,
                # 但诊断信息必须准确: 渲染失败=浏览器问题, 非页面无师资)
                _mark_retry_reason(d, f"教学组织双查无师资({_bh_status}), 待复核")
                print(f"  🔁 教学组织双查无师资({_bh_status})不落终态, RETRY 待复核")
                restore_best()
                return "RETRY"
            # 2026-10-12 用户指令(机构师资预期分级): 医院大概率有/研究院实验室
            # 可能有 — 渲染失败≠无师资(JS站/瑞数/浏览器故障均可致渲染空),
            # 一律 RETRY 待复核; 渲染确认无师资才按类型处置。
            _exp = _org_faculty_expectation(dept_type, dept)
            if _exp == "likely":
                # 医院类: 大概率有师资 — 双查失败转人工复核终态, 绝不 SKIP
                try:
                    json.dump({"status": "pending_human_analysis", "passed": False,
                               "reason": f"医院类机构双查无师资({_bh_status}), "
                                         "医院大概率有师资(用户指令), 需人工复核",
                               "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                              open(d / "test_report.json", "w"),
                              ensure_ascii=False, indent=2)
                except Exception:
                    pass
                print(f"  👤 医院类双查无师资({_bh_status}) → 人工复核终态, 不 SKIP")
                restore_best()
                return "SKIP"
            if _exp == "may" or not _bh_ok:
                # 研究院/实验室等: 渲染失败一律 RETRY; 渲染确认无师资也
                # RETRY 一轮留给渲染复核路径(评估链浏览器复核可判合法空)
                # 2026-10-16 用户指令「专门处理未完成的」: 渲染确认无师资(浏览器
                # 真看过页面, HTML>500字节无教师信号)+已 RETRY 过 ≥1 批(账本
                # .retry_count)→ 双重确认直接落 no_teacher_org 终态, 不再等第
                # 3 批。渲染失败(浏览器不可用)仍 RETRY(资源问题非结论依据)。
                _prior_rc = 0
                try:
                    _prior_rc = int((d / ".retry_count").read_text(
                        encoding="utf-8").strip() or 0)
                except Exception:
                    _prior_rc = 0
                if _bh_ok and _exp == "may" and _prior_rc >= 1:
                    try:
                        json.dump({"status": "no_teacher_org", "passed": False,
                                   "reason": ("机构类(may)双查无师资双重确认: 首批渲染确认无师资+"
                                              "复测渲染仍确认无师资(HTML>500字节无教师信号) — "
                                              "确实没有教师名录(终态结论)"),
                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                  open(d / "test_report.json", "w"),
                                  ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    (d / ".skip_mark").write_text(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S") +
                        " 机构类(may)双查无师资双重确认", encoding="utf-8")
                    print("  ✅ 机构类(may)渲染确认无师资×2 → no_teacher_org 终态(双重确认)")
                    restore_best()
                    return "SKIP"
                _mark_retry_reason(d, f"机构类(may)双查无师资({_bh_status}), 待复核")
                print(f"  🔁 机构类(may)双查无师资({_bh_status})不落SKIP, RETRY 待复核")
                restore_best()
                return "RETRY"
            # unlikely 类(书院/继续教育): 渲染确认无师资 → 落分类终态
            # 2026-10-13 SKIP分类化(用户指令「把skip分成具体类型」): 旧行为
            # 只写 .skip_mark 不写 test_report → 状态悬空每批重选重烧。现在:
            # 渲染确认无师资 = 「确实没有」类 → no_teacher_org 终态(不再重烧);
            # 渲染失败/不可达 = 「请求失败」类 → 上面已转 RETRY, 不落 SKIP。
            try:
                json.dump({"status": "no_teacher_org", "passed": False,
                           "reason": "机构类(unlikely)发现链+浏览器渲染确认无师资"
                                     "(渲染HTML>500字节无教师信号) — 确实没有教师",
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w"),
                          ensure_ascii=False, indent=2)
            except Exception:
                pass
            (d / ".skip_mark").write_text(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 无师资页(发现链+浏览器渲染双查失败)", encoding="utf-8")
            restore_best()
            return "SKIP"

    # 2026-09-18 重大土木实锤(用户纠错, 要求反思强化发现能力): 分组页(教授/
    # 副教授/讲师/博导/硕导/实验中心)挂在**首页菜单**上, 师资页(szgk)无互链 —
    # 只采师资概况=漏掉一大半教师(61人 vs 实际263人)。两层扫描:
    # ①任何首页(含普通页)的「师资家族菜单」(短锚+师资词前缀, 滤 招聘/news/
    #   info详情/外域)并入 fac_urls; ②选择页形态(首页锚≤6=语言/版本选择页)
    #   跟进入口页再扫其菜单(重大土木: 选择页→xzwb/sy.htm 真实首页)。
    def _menu_groups(html, base):
        try:
            _ms = BeautifulSoup(html, "lxml")
            _out = []
            for _ga in _ms.find_all("a", href=True):
                _gt = _ga.get_text(strip=True)
                _ghr = (_ga.get("href") or "").strip()
                if not _ghr or _ghr.startswith(("javascript", "#", "mailto:", "tel:")):
                    continue
                if len(_gt) > 12 or len(_gt) < 2 or "招聘" in _gt:
                    continue
                if "info/" in _ghr or _ghr.startswith("http"):
                    continue  # 教师详情/新闻页/外域不入入口
                if not re.match(r"^(师资|教师|教授|副教授|讲师|博导|硕导|导师|"
                                r"研究员|辅导员|实验|行政|博士|硕士)", _gt):
                    continue
                _gu = urljoin(base, _ghr)
                if _gu.rstrip("/") in ({fac_url.rstrip("/")} | {x.rstrip("/") for x in fac_urls}):
                    continue
                _out.append(_gu)
            return _out
        except Exception:
            return []

    if home:
        _m1 = _menu_groups(home, dept_url)
        if _m1:
            fac_urls = fac_urls + _m1
            print(f"  📂 首页菜单分组({len(_m1)} 个): "
                  f"{', '.join(x[:44] for x in _m1[:6])}...")
        # 选择页跟进入口页
        try:
            _hsoup = BeautifulSoup(home, "lxml")
            if 0 < len(_hsoup.find_all("a", href=True)) <= 6:
                for _sel_a in _hsoup.find_all("a", href=True):
                    _sel_h = (_sel_a.get("href") or "").strip()
                    if (not _sel_h or _sel_h.startswith(("javascript", "#", "mailto:", "tel:"))
                            or _sel_h.startswith("http")):
                        continue
                    _entry = urljoin(dept_url, _sel_h)
                    _ehtml = _bounded_fetch(_entry)
                    if not _ehtml or _ehtml is _FETCH_TIMEOUT or len(_ehtml) < 500:
                        continue
                    _m2 = _menu_groups(_ehtml, _entry)
                    if _m2:
                        fac_urls = fac_urls + _m2
                        print(f"  📂 入口页菜单分组({len(_m2)} 个): "
                              f"{', '.join(x[:44] for x in _m2[:6])}...")
                    break
        except Exception:
            pass

    # 2026-09-24 南信大马院实锤: 名单在 iframe(t-inner namexs.jsp) —
    # 发现链只见外壳误选全校平台(879人)。iframe 检测: fac_url 页含
    # <iframe src> 且 iframe 内有教师信号(名职对/姓名) → 用 iframe URL
    # 替换 fac_url(名单实际在 iframe 里)
    if fac_url:
        try:
            _fi0 = _bounded_fetch(fac_url)
            if _fi0 and _fi0 is not _FETCH_TIMEOUT:
                for _fm0 in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', _fi0, re.I):
                    _fsrc = urljoin(fac_url, _fm0.group(1))
                    if not _fsrc.startswith("http"):
                        continue
                    _fih = _bounded_fetch(_fsrc)
                    if not _fih or _fih is _FETCH_TIMEOUT or len(_fih) < 800:
                        continue
                    _fit = re.sub(r"<script.*?</script>|<style.*?</style>", "", _fih, flags=re.S)
                    _fit = re.sub(r"<[^>]+>", "\n", _fit)
                    _fils = [l.strip() for l in _fit.split("\n") if l.strip()]
                    _fin = sum(1 for i, l in enumerate(_fils)
                               if re.fullmatch(r"[一-鿿·]{2,4}", l) and i + 1 < len(_fils)
                               and re.search(r"教授|副教授|讲师|研究员|导师", _fils[i + 1]))
                    if _fin >= 3:
                        print(f"  🖼️ iframe嵌套名单({_fsrc[:55]}, {_fin}+人) → 采用")
                        fac_url = _fsrc
                        break
        except Exception:
            pass

    # 2026-09-24 吉大历史实锤(用户指令「特别细分」): 师资队伍页内含
    # 系/学科子栏目(中国史系→中国古代史/中国近现代史/专门史, 世界史系…),
    # 名单在各学科页 — 发现链只采师资页外壳(4人)漏全量(31人)。
    # 师资页子栏目枚举: fac_url 页内 2-10字锚文本的本站子页链接,
    # 非导航词/非招聘, 并入 fac_urls
    # 2026-09-30 上交电气实锤: 学部子学院选中博士后页(bsh.html 30人), 教师
    # 名录页(jiaoshiml, 内联AJAX 200人)在 fac_urls 里却没被选 — 名录优先
    if fac_url and re.search(r"bsh|博士后|postdoc", fac_url, re.I):
        _alt = next((u for u in fac_urls
                     if re.search(r"jsml|jzml|minglu|jiaoshi|faculty", u, re.I)), None)
        if _alt:
            print(f"  📇 师资页是博士后名录({fac_url[:45]}) → 改用教师名录 {_alt[:45]}")
            fac_url = _alt
    # 2026-09-29 哈工程外语1334实锤: fac_url 本身被选为全校教师平台首页
    # (faculty.hrbeu.edu.cn/ 无 CollegeTeacherList) — 平台过滤只清 fac_urls
    # 不清 fac_url。fac_url 是平台链接时改用 fac_urls 里第一个非平台页,
    # 都没有则作废(走无师资页链, 绝不跑全校平台)
    if fac_url and _is_platform_bad(fac_url):
        _alt = next((u for u in fac_urls if not _is_platform_bad(u)), None)
        # 平台首页列全院名单(哈工程: 外国语学院 id=1162 70人) — 按院系名
        # 匹配本院 CollegeTeacherList id(错拿他院 id = 全校合并污染)
        try:
            _ph = _bounded_fetch(fac_url)
            if _ph and _ph is not _FETCH_TIMEOUT:
                _ps = BeautifulSoup(_ph, "lxml")
                for _a in _ps.find_all("a", href=True):
                    _t = _a.get_text(strip=True)
                    _h = _a.get("href", "").strip()
                    if _t and dept and (_t == dept or (dept in _t and len(_t) <= len(dept) + 6)):
                        if re.search(r"CollegeTeacherList", _h, re.I):
                            _durl = urljoin(fac_url, _h)
                            print(f"  🎯 平台按院系名匹配: {_t} → {_durl[:60]}")
                            _alt = _durl
                            break
        except Exception:
            pass
        if _alt:
            print(f"  🚫 选定师资页是全校平台({fac_url[:50]}) → 改用 {_alt[:60]}")
            fac_url = _alt
        else:
            # 2026-10-03 西南交大马院实锤: 作废为 None 会崩下游
            # (NoneType.startswith/subscriptable 多处) — 退回院系首页,
            # 下游 fetch/render 全兼容, 走自然失败路径
            print(f"  🚫 选定师资页是全校平台({fac_url[:50]}), 无替代 → 退回院系首页")
            fac_url = dept_url or ""
            fac_html_early = None
    if fac_url:
        try:
            # 2026-09-28 中科大物理实锤: fac_url 被选为文章页(2026/0414/…)时,
            # 院系首页(dept_url)导航里的系子域锚(物理系→phys.ustc.edu.cn)是
            # 唯一入口 — 枚举源 = fac_url + dept_url 首页, 去重
            _enum_srcs = []
            for _src0 in dict.fromkeys([(fac_url or "").rstrip("/"),
                                        (dept_url or "").rstrip("/")]):
                if not _src0.startswith("http"):
                    continue
                _eh0 = _bounded_fetch(_src0)
                if _eh0 and _eh0 is not _FETCH_TIMEOUT:
                    _enum_srcs.append((_src0, _eh0))
            for _src0, _fh0 in _enum_srcs:
                _fs0 = BeautifulSoup(_fh0, "lxml")
                for _fa0 in _fs0.find_all("a", href=True):
                    _ft0 = _fa0.get_text(strip=True)
                    _fh0r = (_fa0.get("href") or "").strip()
                    if not _fh0r or _fh0r.startswith(("javascript", "#", "mailto:", "tel:")):
                        continue
                    # 2026-09-28 川大艺术实锤: 双语导航锚「师资队伍Faculty」
                    # (13字)超长度限制被跳 → 名录页(xygk/szdw/szgk.htm)进不了
                    # 枚举。判定用中文部分(剥尾随ASCII), 全英锚剥空自然不过
                    _ft0c = re.sub(r"[A-Za-z0-9\s._-]+$", "", _ft0).strip()
                    if not (2 <= len(_ft0c) <= 10):
                        continue
                    # 2026-09-28 上交电院实锤: 「教师名录」在 NAV_WORDS 里
                    # (26812词), 枚举直接跳过 → 名录壳页(szgk_jsml)进不了
                    # fac_urls, 名单在兄弟子域的 jiaoshiml 页更够不着。
                    # 豁免: ①名录/名单类锚且 href 是名单页特征时放行
                    # ②单段路径栏目页(学院概况→xygk.htm, 川大艺术实锤: 师资
                    #   队伍Faculty 藏在栏目页下一层) — 二级递归再找信号锚
                    # ③系名锚(2-6字含系, 无学院)指向同注册域兄弟子域
                    #   (中科大物理实锤: 物理系→phys.ustc.edu.cn 名单在系站)
                    if _ft0c in NAV_WORDS:
                        _t0_abs = _fh0r.startswith(("http://", "https://"))
                        _is_sect = bool(re.fullmatch(r"[A-Za-z0-9_-]+\.s?html?", _fh0r))
                        _is_dept_sub = (_t0_abs and 2 <= len(_ft0c) <= 6
                                        and "系" in _ft0c and "学院" not in _ft0c
                                        and urlparse(_fh0r).netloc != urlparse(_src0).netloc
                                        and _reg_domain(_fh0r) == _reg_domain(_src0))
                        if not (_is_sect or _is_dept_sub or re.search(
                                r"jsml|jzml|minglu|jslb|szml|faculty|szgk|list|teacher|/ml|/md",
                                _fh0r, re.I)):
                            continue
                    # 2026-09-25 中科大地空1935实锤: 新闻栏目(学习园地/科技
                    # 进展)被宽枚举并进 fac_urls → 新闻页人名大杂烩。收紧:
                    # 锚文本须含师资/学科信号词才收
                    # 2026-09-28 同济外语实锤: 名录按语种分类(英语/日语/德语
                    # /法语/其他语种) — 信号词补 语|言
                    if not re.search(r"系|学|史|方向|团队|组|中心|教师|导师|教授|"
                                     r"人员|队伍|人才|师资|教学|语|言", _ft0c):
                        continue
                    if re.search(r"招聘|下载|通知|公告|新闻|动态|首页|English|"
                                 r"培养|招生|就业|(?:研究生|本科生|硕士生|博士生)(?!导师)|学生", _ft0):
                        continue
                    if "info/" in _fh0r:
                        continue
                    # 2026-09-28 上交电院实锤: 名录壳页(szgk_jsml.html)的子类
                    # 名单在兄弟子域(see./sais.sjtu.edu.cn/jiaoshiml.html) —
                    # 绝对URL仅同注册域放行(NAV_WORDS 已挡校主页栏目词)
                    # 2026-09-28 中科大物理实锤: 物理学院五系名单在系子站
                    # (phys./mphy./ooe./astro./eap.ustc.edu.cn) — 系锚放行
                    if _fh0r.startswith("http"):
                        if _reg_domain(_fh0r) != _reg_domain(_src0):
                            continue
                        # 2026-09-29 南大智能软件275实锤: 校主站(www.nju.edu.cn
                        # 院士列表98)/分校区站(njusz 苏州校区97)被同注册域放行
                        # → 全校名单混入。跨子域目标: www 主站硬拒, 校级导航锚
                        # (大学/学校/主页/校区/门户)硬拒
                        if urlparse(_fh0r).netloc != urlparse(_src0).netloc:
                            if urlparse(_fh0r).netloc.startswith("www."):
                                continue
                            if re.search(r"大学|学校|主页|校区|门户", _ft0c):
                                continue
                            # 2026-09-29 太原理工创新创业78实锤: 学生处/学工部
                            # 站(student.tyut.edu.cn)名单混入 — 校级部门锚硬拒
                            if re.search(r"学生处|学工部|学生工作处|学生工作|团委|党委|工会|研究生院|教务处|人事处", _ft0c):
                                continue
                        # 2026-09-29 华中农大外语450实锤: 页脚兄弟学院链接
                        # (马克思主义学院→my.hzau.edu.cn 裸域名) 被同注册域
                        # 放行 → 全校兄弟学院名单全采。跨子域学院锚的目标
                        # href 须含名单页特征(上交电院 see.sjtu.edu.cn/
                        # jiaoshiml.html ✓, 裸域名 ✗); 系锚(中科大物理
                        # 物理系→phys.ustc.edu.cn)不受限
                        if (urlparse(_fh0r).netloc != urlparse(_src0).netloc
                                and "学院" in _ft0c and "系" not in _ft0c
                                and not re.search(r"jsml|jzml|minglu|jslb|szml|faculty|szdw|list|jiaoshi|teacher",
                                                  _fh0r, re.I)):
                            continue
                        if re.search(r"校友|图书馆|基金会|招生|下载|地图|邮箱", _ft0):
                            continue
                    _fu0 = urljoin(_src0, _fh0r)
                    if _fu0.rstrip("/") in ({fac_url.rstrip("/")} | {x.rstrip("/") for x in fac_urls}):
                        continue
                    fac_urls.append(_fu0)
                # 2026-09-24 第二层递归: 系页(zgsx.htm)内含学科页
                # (zggds/zgjxds/zms) — 枚举出的子页再枚举一层(≤10页,
                # 2026-09-28 上交电院实锤: 名录壳页(szgk_jsml)在 DOM 后段,
                # 6页截断漏掉它 → 兄弟子域名单页(see.sjtu.edu.cn/jiaoshiml)
                # 进不了枚举; 名录/名单/faculty 锚的页优先递归)
                _snap_l1 = set(fac_urls)
                _lvl1 = [u for u in fac_urls if u != fac_url]
                _lvl1.sort(key=lambda u: 0 if re.search(r"jsml|minglu|faculty|szgk|list|md|ml",
                                                        u, re.I) else 1)
                _lvl1 = _lvl1[:10]
                for _fl1 in _lvl1:
                    _fh1 = _bounded_fetch(_fl1)
                    if not _fh1 or _fh1 is _FETCH_TIMEOUT or len(_fh1) < 500:
                        _fh1 = _bounded_fetch(_fl1)  # 2026-09-28 川大艺术实锤: 限速站偶发空回, 重试一次
                    if not _fh1 or _fh1 is _FETCH_TIMEOUT or len(_fh1) < 500:
                        continue
                    _fs1 = BeautifulSoup(_fh1, "lxml")
                    for _fa1 in _fs1.find_all("a", href=True):
                        _ft1 = _fa1.get_text(strip=True)
                        _fh1r = (_fa1.get("href") or "").strip()
                        if not _fh1r or _fh1r.startswith(("javascript", "#", "mailto:", "tel:")):
                            continue
                        _ft1c = re.sub(r"[A-Za-z0-9\s._-]+$", "", _ft1).strip()
                        if not (2 <= len(_ft1c) <= 10):
                            continue
                        if _ft1c in NAV_WORDS:
                            _t1_abs = _fh1r.startswith(("http://", "https://"))
                            _is_sect1 = bool(re.fullmatch(r"[A-Za-z0-9_-]+\.s?html?", _fh1r))
                            _is_dept_sub1 = (_t1_abs and 2 <= len(_ft1c) <= 6
                                             and "系" in _ft1c and "学院" not in _ft1c
                                             and urlparse(_fh1r).netloc != urlparse(_fl1).netloc
                                             and _reg_domain(_fh1r) == _reg_domain(_fl1))
                            if not (_is_sect1 or _is_dept_sub1 or re.search(
                                    r"jsml|jzml|minglu|jslb|szml|faculty|szgk|list|teacher|/ml|/md",
                                    _fh1r, re.I)):
                                continue
                        if not re.search(r"系|学|史|方向|团队|组|中心|教师|导师|教授|"
                                         r"人员|队伍|人才|师资|教学|语|言", _ft1c):
                            continue
                        if re.search(r"招聘|下载|通知|公告|新闻|动态|首页|English|"
                                     r"培养|招生|就业|(?:研究生|本科生|硕士生|博士生)(?!导师)|学生", _ft1):
                            continue
                        if "info/" in _fh1r:
                            continue
                        if _fh1r.startswith("http"):
                            if _reg_domain(_fh1r) != _reg_domain(_fl1):
                                continue
                            # 校主站/分校区站跨子域防线(南大智能软件275实锤)
                            if urlparse(_fh1r).netloc != urlparse(_fl1).netloc:
                                if urlparse(_fh1r).netloc.startswith("www."):
                                    continue
                                if re.search(r"大学|学校|主页|校区|门户", _ft1c):
                                    continue
                                if re.search(r"学生处|学工部|学生工作处|学生工作|团委|党委|工会|研究生院|教务处|人事处", _ft1c):
                                    continue
                            # 兄弟学院跨子域链接目标 href 须含名单特征
                            # (华中农大外语450实锤), 系锚不受限
                            if (urlparse(_fh1r).netloc != urlparse(_fl1).netloc
                                    and "学院" in _ft1c and "系" not in _ft1c
                                    and not re.search(r"jsml|jzml|minglu|jslb|szml|faculty|szdw|list|jiaoshi|teacher",
                                                      _fh1r, re.I)):
                                continue
                            if re.search(r"校友|图书馆|基金会|招生|下载|地图|邮箱", _ft1):
                                continue
                        _fu1 = urljoin(_fl1, _fh1r)
                        if _fu1.rstrip("/") in ({fac_url.rstrip("/")} | {x.rstrip("/") for x in fac_urls}):
                            continue
                        fac_urls.append(_fu1)
                # 2026-09-28 川大艺术实锤: 名录壳页嵌套可深达4层(首页→学院
                # 概况xygk.htm→师资队伍szgk.htm→职称分类页 教授js.htm/副教授
                # fjs.htm/思政教师szjs.htm) — 且 fac_url 被选为新闻文章时链
                # 更深。固定两级递归不够: 迭代加深, 每轮对新加入的页再枚举
                # (≤8页/轮, ≤3轮)。三级起只收职称/类别/单段路径名单入口,
                # 不再收栏目页
                _seen_enum = _snap_l1 | set(_lvl1) | {fac_url}
                for _er in range(3):
                    _pending = [u for u in fac_urls if u not in _seen_enum][:8]
                    if not _pending:
                        break
                    for _fl2 in _pending:
                        _seen_enum.add(_fl2)
                        _fh2 = _bounded_fetch(_fl2)
                        if not _fh2 or _fh2 is _FETCH_TIMEOUT or len(_fh2) < 500:
                            _fh2 = _bounded_fetch(_fl2)  # 限速站偶发空回, 重试一次
                        if not _fh2 or _fh2 is _FETCH_TIMEOUT or len(_fh2) < 500:
                            continue
                        _fs2 = BeautifulSoup(_fh2, "lxml")
                        for _fa2 in _fs2.find_all("a", href=True):
                            _ft2 = _fa2.get_text(strip=True)
                            _fh2r = (_fa2.get("href") or "").strip()
                            if not _fh2r or _fh2r.startswith(("javascript", "#", "mailto:", "tel:")):
                                continue
                            _ft2c = re.sub(r"^[\s\-—–·|▪•●▫○◆■□]+|[A-Za-z0-9\s._-]+$", "", _ft2).strip()
                            if not (2 <= len(_ft2c) <= 10):
                                continue
                            _is_t3 = bool(re.search(r"教授|讲师|教师|导师|研究员|院士|"
                                                    r"博后|博士后|助教|名录|名单|队伍|专家|学者",
                                                    _ft2c))
                            _is_sect3 = bool(re.fullmatch(r"[A-Za-z0-9_-]+\.s?html?", _fh2r))
                            if not (_is_t3 or _is_sect3):
                                continue
                            if re.search(r"招聘|下载|通知|公告|新闻|动态|首页|English|"
                                         r"联系|简介|组织|领导|校友|活动|培养|招生|就业|"
                                         r"(?:研究生|本科生|硕士生|博士生)(?!导师)|学生", _ft2):
                                continue
                            if "info/" in _fh2r:
                                continue
                            if _fh2r.startswith("http") and _reg_domain(_fh2r) != _reg_domain(_fl2):
                                continue
                            _fu2 = urljoin(_fl2, _fh2r)
                            if _fu2.rstrip("/") in ({fac_url.rstrip("/")} | {x.rstrip("/") for x in fac_urls}):
                                continue
                            fac_urls.append(_fu2)
                if len(fac_urls) > 1:
                    print(f"  📂 师资页子栏目枚举({len(fac_urls)} 入口): "
                          f"{', '.join(x[:36] for x in fac_urls[:6])}...")
        except Exception:
            pass

    # 2026-09-24 厦大民族研究中心1225人实锤: fac_urls 组装链(同组分类页/
    # 分页探测/子栏目枚举)会把全校平台拼音列表并进来 — probe 硬拒挡不住
    # 此处。统一过滤: faculty平台链接仅 CollegeTeacherList(按院系列表)
    # 放行, 拼音列表/人员列表/平台首页一律剔除
    try:
        _plat_bad = [u for u in fac_urls
                     if re.search(r'faculty\.[a-z0-9.-]+\.edu\.cn', u, re.I)
                     and 'CollegeTeacherList' not in u]
        if _plat_bad:
            fac_urls = [u for u in fac_urls if u not in _plat_bad]
            print(f"  🚫 fac_urls 剔除平台拼音列表 {len(_plat_bad)} 个(全校教师)")
        # 2026-09-29 哈工程外语1332实锤: 平台 CollegeTeacherList 多 id(每个
        # id 一个学院) 全采 = 全校合并 — 只留第一个
        _ct = [u for u in fac_urls
               if re.search(r'faculty\.[a-z0-9.-]+\.edu\.cn', u, re.I)
               and 'CollegeTeacherList' in u]
        if len(_ct) > 1:
            fac_urls = [u for u in fac_urls if u == _ct[0] or 'CollegeTeacherList' not in u]
            print(f"  🚫 平台按院系多id({len(_ct)}) → 只留1个({_ct[0][:55]})")
    except Exception:
        pass

    # 2026-10-09 走查发现页注入: 上轮核实的走查页/遗漏分页子页并入 fac_urls —
    # 提取与核实同源(上体体育教育 CSV1 vs 走查57人 实锤: 核实器知道老师在哪,
    # 提取器没走到) — 注入后 spider 直接从真名单页提取
    try:
        _vp = json.load(open(d / "_verify_pages.json", encoding="utf-8"))
        _vp_add = 0
        for _pu in (_vp.get("pages") or []) + (_vp.get("miss") or []):
            if (isinstance(_pu, str) and _pu.startswith("http")
                    and _pu not in fac_urls and len(fac_urls) < 12):
                fac_urls.append(_pu)
                _vp_add += 1
        if _vp_add:
            print(f"  🔍 注入核实发现页 {_vp_add} 个(上轮走查证据)")
    except Exception:
        pass

    # 2026-09-28 上交电院实锤: 名录壳页(see.sjtu.edu.cn/jiaoshiml.html)教师
    # 数据在页内 $.ajax POST 接口(ajax_teacher_list.html, type=1 返回159人) —
    # probe 的 BFS 限本校域, 兄弟子域页进不了 probe; 发现链必须自己扫
    # fac_url/fac_urls 页的内联 AJAX 接口(探测逻辑复用 probe 的 detect_ajax_api)
    if not fac_api:
        try:
            from script.probe_site import detect_ajax_api as _daj
            # 名单壳页优先(jsml/faculty/名录) — 接口都挂在名单页内联 JS 里
            _aj_cands = [fac_url] + [u for u in fac_urls if u != fac_url]
            _aj_cands.sort(key=lambda u: 0 if re.search(
                r"jsml|minglu|jslb|szml|faculty|szgk|list|/ml|/md", u, re.I) else 1)
            for _au in _aj_cands[:10]:
                _ah = fac_html_early if (_au == fac_url and fac_html_early) else _bounded_fetch(_au)
                if not _ah or _ah is _FETCH_TIMEOUT:
                    continue
                _api = _daj(_ah, _au)
                if _api:
                    fac_api = {"url": _api["url"], "data": _api["data"]}
                    print(f"  📡 内联AJAX教师接口({_api['url'][:58]}, {_api['count']}人) → 采用")
                    break
        except Exception:
            pass

    # R0: 无 spider.py → 生成确定性 spider + analysis.md
    # 薄壳判定: 旧模板/LLM 内联产物(内联词表/写盘逻辑)不复用 util, 其内联词表缺失
    # 新词(复旦大学/科学 等仍被提取)且写盘易不一致 — 直接重生为薄壳, 不浪费LLM轮次
    spiderf = d / "spider.py"
    _is_slim = spiderf.exists() and "util.teacher_extract" in spiderf.read_text(encoding="utf-8", errors="ignore")
    _is_art = _is_art_spider(spiderf)
    # 2026-08-26: 发现 AJAX 教师接口 → 强制重生(旧 spider 无 API 分支, 只抓空壳页必然 0 人)
    # 2026-08-26: 同组多入口(职称分类页) → 旧薄壳单入口必漏采(上大马院只抓教授页) → 强制重生
    _multi = len(fac_urls) > 1
    if (not spiderf.exists() or spiderf.stat().st_size < 100 or not _is_slim
            or fac_api or _multi) and not _is_art:
        if spiderf.exists() and (not _is_slim or fac_api or _multi):
            print(f"  ♻️ {'AJAX接口发现→' if fac_api else ('同组多入口→' if _multi else '')}重生为薄壳(复用util)")
        code = generate_spider(school, dept, dept_url, fac_url, fac_urls, fac_api)
        spiderf.write_text(_sanitize_platform_urls(code, dept_url), encoding="utf-8")
        print(f"  🆕 生成薄壳spider.py ({len(code)}B)")
    elif _is_art:
        # 2026-09-17 整校污染防护: 艺术院校映射型 spider 发现链再生即错位校级页
        print("  🎨 艺术院校映射型spider(归特殊采集器管), 跳过发现链再生")
    # analysis.md: 深度网页结构分析(评估器硬性要求)
    # 链路: info.csv院系URL首页 → 师资页发现路径 → 师资页DOM/分组/陷阱 → 提取规则 → 测试验证
    analysisf = d / "analysis.md"
    if not (analysisf.exists() and analysisf.stat().st_size > 100):
        fac_html0 = _bounded_fetch(fac_url)
        if fac_html0 is _FETCH_TIMEOUT:
            print("  ⏱️ analysis.md 素材 fetch 超时, 空骨架继续(师资页抓取稍后再试)")
            fac_html0 = None
        soup0 = BeautifulSoup(fac_html0, "lxml") if fac_html0 else None
        L = []
        L += [f"# {dept} — 网站结构分析", "",
              f"- **分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
              f"- **起点URL(info.csv)**: {dept_url}",
              f"- **师资页**: {fac_url}", ""]
        # 1. 首页发现路径
        if home:
            hsoup = BeautifulSoup(home, "lxml")
            L += ["## 1. 从 info.csv 的 URL 出发 — 首页分析", ""]
            found = []
            for a in hsoup.find_all("a", href=True):
                t = a.get_text(strip=True)
                hr = a.get("href", "").strip()
                if not hr or hr.startswith(("javascript", "#")):
                    continue
                if re.search(r"师资|教师|队伍|人员|名师|导师|团队|专家|医师", t) and len(t) <= 14:
                    full = urljoin(dept_url, hr)
                    found.append((t, full))
            for t, u in found[:8]:
                L.append(f"- `{t}` → {u}{' ← 师资页入口' if u == fac_url else ''}")
            L += ["", f"**发现路径**: 首页导航 → 师资入口 → {fac_url}", ""]
        # 2. 师资页深度分析
        if soup0:
            L += [f"## 2. 师资页 ({fac_url}) 深度分析", ""]
            headers = []
            for el in soup0.find_all(["h2", "h3", "h4", "span", "b", "strong", "p", "div"]):
                t = el.get_text(strip=True)
                if re.match(r"^(教授|副教授|助理教授|讲师|研究员|副研究员|高级工程师|工程师|博士|博士后|院士|青年)[^，。]{0,10}$", t) and len(t) <= 14:
                    if t not in headers:
                        headers.append(t)
            teacher_links, seen_href = [], set()
            for a in soup0.find_all("a", href=True):
                t = a.get_text(strip=True)
                hr = a.get("href", "").strip()
                if not hr or hr.startswith(("javascript", "#", "mailto")):
                    continue
                nm = t.rstrip("、，, ")
                if re.match(r'^[一-鿿·]{2,4}$', nm) and nm not in NAV_WORDS and hr not in seen_href:
                    seen_href.add(hr)
                    teacher_links.append((nm, urljoin(fac_url, hr)))
            L += [f"### 2.1 页面组织: {'分组式(职称/科室标题)' if headers else '平铺列表'}",
                  f"- 分组标题: {headers[:10]}" if headers else "",
                  f"- 教师级链接约 **{len(teacher_links)}** 个", ""]
            url_modes = {}
            for nm, u in teacher_links:
                m = re.search(r"https?://[^/]+(/\w+/\d+/\d+\.htm|/[A-Za-z0-9]+/zh_CN/index\.htm)", u)
                key = m.group(1) if m else "其他"
                url_modes.setdefault(key, []).append(nm)
            L += ["### 2.2 详情URL模式"]
            for mode, nms in list(url_modes.items())[:5]:
                L.append(f"- `{mode}`: {len(nms)}人 (样本: {nms[:3]})")
            traps = []
            for a in soup0.find_all("a", href=True):
                t = a.get_text(strip=True)
                if re.match(r"^[、，,]+$", t):
                    traps.append(f"纯顿号/逗号链接 `{t}` (必须跳过)")
                    break
            for a in soup0.find_all("a", href=True):
                t = a.get_text(strip=True)
                if re.match(r"^([一-鿿·]{2,4})[、，,]$", t):
                    traps.append(f"姓名带尾随顿号 `{t}` (提取前需 rstrip('、，, '))")
                    break
            if not traps:
                traps.append("未发现明显陷阱")
            L += ["### 2.3 陷阱"] + [f"- {t}" for t in traps]
            pagination = []
            for a in soup0.find_all("a", href=True):
                t = a.get_text(strip=True)
                if t in ("下一页", "下页", "尾页") or (re.match(r"^\d+$", t) and len(t) <= 2):
                    pagination.append((t, urljoin(fac_url, a.get("href", ""))))
            L += ["### 2.4 分页/子页",
                  f"- 分页: {pagination[:4] if pagination else '无'}", ""]
            # 2026-09-16 用户指令: 拿到官网后要确定"哪些页面包含老师信息"
            # (系/所/研究院/职称等多种分类 + 分页页), 页面包含要全面 —
            # 每个入口页实测: 声明教师数(共N条) vs 通用提取器提取数,
            # 通用覆盖(<80% 记"需定制") — analysis.md 作为采集方案验收记录
            L += ["### 2.5 教师信息页面清单(入口全查)", ""]
            try:
                from util.teacher_extract import extract_teachers_multi as _etm
                _ent = ([fac_url] + list(fac_urls))[:10]
                _decl_sum = _extr_sum = 0
                for _ei, _eu in enumerate(_ent):
                    _eh = fac_html0 if _eu == fac_url else None
                    if _eh is None:
                        _eh = _bounded_fetch(_eu)
                    if not _eh or len(_eh) < 500:
                        L.append(f"- {_ei + 1}. `{_eu[:60]}`: 抓取失败")
                        continue
                    _em = re.search(r"共\s*(\d+)\s*条", _eh)
                    _decl = int(_em.group(1)) if _em else 0
                    try:
                        _ext = len(_etm(_eh, _eu))
                    except Exception:
                        _ext = -1
                    _decl_sum += _decl
                    _extr_sum += max(_ext, 0)
                    _mode = ("需定制(通用提取不足)" if _decl and _ext < _decl * 0.8
                             else "通用规则覆盖")
                    L.append(f"- {_ei + 1}. `{_eu[:60]}`: 声明{_decl}人 通用提取{_ext}人 → {_mode}")
                L.append(f"- 合计: 声明 {_decl_sum} 人, 通用提取 {_extr_sum} 人")
            except Exception:
                L.append("- 入口清单统计失败(继续)")
        L += ["## 3. 提取规则设计", "",
              "0. **通用 vs 定制判定(2026-09-16 用户指令)**: 通用提取器(util.teacher_extract)已覆盖"
              " 15+ 形态(CSS容器优先/卡片/表格/JS数组/外文名/职称粘连/dash纯文本/称号前缀/嵌套子元素);"
              " 2.5 清单中标记「通用规则覆盖」的入口直接复用 util, 标记「需定制」的入口"
              " 才在 spider 内写站点特定选择器 — 能通用绝不单独设计",
              "1. 只从 `<a>` 提取(或卡片容器), 不扫全页文本(避免分组标题内姓名重复/栏目混入)",
              "2. 姓名严格 2-4 字中文(或外国名), 先 rstrip('、，, ') 去粘连, 排除导航词;"
              " **简介前缀形态(2026-09-22 北中医中药实锤)**: 锚文本「中药学院导师简介-张媛」"
              " = 前缀(导师简介/简介/简历/介绍)+姓名 — 剥前缀取尾段姓名, 全站统一前缀"
              " 时这是名单页主形态(URL 是详情 htm, 如 bc704afc...htm)",
              "3. 详情URL必须命中特征模式(/info/数字, faculty平台等), 严格排除栏目链接",
              "4. CSV与JSON由同一spider运行写出, teachers数组内容一致",
              "5. 分页/子分类页显式遍历",
              "",
              "## 4. 测试验证",
              "- 规则预跑教师数与页面统计一致",
              "- 干扰项(顿号链接/导航链接)被排除",
              "- 零容忍 LLM 姓名终审通过"]
        (d / "analysis.md").write_text("\n".join(L), encoding="utf-8")
        print(f"  📄 生成analysis.md")

    fac_html = fac_html_early or (_bounded_fetch(fac_url) if fac_url else None)
    if fac_html is _FETCH_TIMEOUT:
        # 2026-09-04 华工机械与汽车两挂实锤: fetch 挂死非"不可达" — 不可达早已
        # 被 _bounded_ip_check 捕获落 FAIL; 此处超时是波期瞬态, 落 SKIP 会误杀
        print(f"  ⏱️ 师资页 fetch 超时({_FETCH_BOUND}s 硬边界), 转 RETRY 重试")
        # 2026-10-16 波期豁免语义收窄: 站点瞬态≠LLM波期 — 真原因占计数
        # +进反思账本(持续超时3次该出清; 空原因会被批次侧误读波期豁免)
        _mark_retry_reason(d, f"师资页fetch超时({_FETCH_BOUND}s硬边界), 瞬态网络待重试")
        return "RETRY"
    if not fac_html:
        # 2026-09-22 海南食品实锤: 发现链浏览器渲染已确认师资页, 但 CPU 链
        # 抓 fac_url 失败后分诊只查 CPU 变体 — cloak 能打开的页被判
        # 「URL失效(404)」修URL转RETRY白跑一轮(404≠变体活着)。浏览器渲染
        # 是分诊第一步(对所有 code 形态), 渲染成功直接用, 不再走 CPU URL
        # 验证(CPU 链指纹被拦时浏览器照样能打开 — 与不可达最终仲裁同逻辑)
        # 2026-10-03 西南交大马院实锤: fac_url 被平台作废(None)后此处渲染
        # 崩溃(NoneType.startswith) — 平台作废时直接走网络分诊
        _ch = _render_fetch(fac_url) if fac_url else None
        if _ch and len(_ch) > 500:
            print(f"  🌐 不可达分诊: cloak渲染成功({len(_ch)}字节), 采用渲染页继续")
            fac_html = _ch
    if not fac_html:
        # 2026-09-18 十批攻坚改进#1: 网络失败系统curl分诊(诊断器实证: 网络失败
        # 50%是瞬时问题可回队; 412=瑞数墙政策冻; 子站死根域活=校方网络7天账本)
        # 2026-10-03 平台作废(fac_url=None)防护
        if fac_url:
            _code, _ok = _sys_curl_ok(fac_url)
        else:
            _code, _ok = "000", False
        if _code == "412":
            # 2026-09-21 专题研究「无头能否解瑞数」结论: CloakBrowser headless
            # + stealth_args 5-8s 过挑战(三站实证) — nodenv CPU链失败的瑞数站
            # 用 cloak 浏览器兜底取真页面继续采集, 取不到才按旧政策深冻
            # 2026-09-21 SEM饥饿修复: cloak 串行锁内置, 不抢 SEM 槽
            # (25 workers 下 SEM(3)+timeout30 曾致浏览器复核大面积跳过)
            _ch = _render_fetch(fac_url)
            if _ch and len(_ch) > 500:
                print(f"  🛡️ 瑞数412 → CloakBrowser反检测通过({len(_ch)}字节), 采用渲染页继续")
                fac_html = _ch
            if not fac_html:
                print("  🛡️ 瑞数412挑战(cloak兜底也失败) → 深冻账本")
                _mark_freeze(school, dept_dir.name, form="ruishu_412")
                # 2026-10-16 波期豁免语义收窄: 瑞数挑战是站点侧问题非LLM波期
                _mark_retry_reason(d, "瑞数412挑战(cloak兜底也失败), 深冻账本待错峰")
                return "RETRY"
        if _ok:
            print("  🔁 系统curl可达 → 瞬时网络问题, 转 RETRY(不计streak)")
            # 2026-10-16 波期豁免语义收窄: 瞬时网络是站点侧, 真原因占计数
            _mark_retry_reason(d, "瞬时网络问题(系统curl可达), 待重试")
            return "RETRY"
        # 2026-09-20 用户指令「不可达判定依据要充分」: 旧URL死≠站点死 —
        # 域名变体交叉验证(剥/加www、scheme切换、主机根、院系主页、
        # departments.csv同名记录)。判定标准: code != "000" 即「域名活着」
        # — 412 也是活(瑞数墙有响应, DNS/服务器正常), 只有 000(DNS失败/
        # 连接拒绝/超时)才是真不可达。任一变体活着 = URL失效 → 修URL回队,
        # 不落 subnet_dead 冻结(重大 www.cmse/male 旧域名 000 vs 新域名
        # cmse/mkszy 412 实锤: 旧判"子站死"实为旧域名死)。
        for _v in _url_variants(fac_url, dept_url, d):
            _vc, _vok = _sys_curl_ok(_v)
            # 2026-09-22 郑大实锤: 404 是页面级死(URL形态错/页面删除),
            # 不是「域名活着」 — 旧判定 code!="000" 把 404 当活变体,
            # 修URL到死页白跑一轮。只有 200/3xx/412 类(服务器有内容
            # 响应)才算活; 404 与 000 同属死(继续找下一变体)
            if _vc not in ("000", "404"):
                print(f"  🔧 URL失效(原:{fac_url[:50]} 死) 变体活着({_v[:60]} code={_vc}) → 修URL转RETRY")
                _mark_retry_reason(d, "URL失效已修(变体活着)")
                try:
                    from fix_queue import load_fail_queue as _lq, save_fail_queue as _sq
                    _q = _lq()
                    _e = _q.setdefault(school, {}).get(dept_dir.name)
                    if _e is not None:
                        _e["url"] = _v
                        _e["zombie"] = False
                        _e["attempts"] = 0
                        _sq(_q)
                except Exception:
                    pass
                return "RETRY"
        _, _root_ok = _sys_curl_ok(_root_url(fac_url))
        if _root_ok and _code == "000":
            # 2026-09-20 用户指令「不可达的最终确认必须浏览器兜底」: 变体全失败
            # 也还不是终点 — 浏览器渲染最终仲裁(CPU链指纹/网络层误判时浏览器
            # 照样能打开); 浏览器也打不开才是真不可达
            # 2026-09-21 SEM饥饿修复: cloak 直调(锁内置); 9222 才走 SEM
            _bh = _render_fetch(fac_url)
            if not _bh and _BROWSER_VERIFY_SEM.acquire(timeout=30):
                try:
                    _bh = _browser_fetch(fac_url)
                finally:
                    _BROWSER_VERIFY_SEM.release()
            if _bh and len(_bh) > 500:
                print("  🌐 浏览器渲染可达(CPU链误判, 站点活着) → 转 RETRY 不冻结")
                _mark_retry_reason(d, "浏览器可达, CPU链网络误判")
                return "RETRY"
            print("  🧊 子站公网不可达(学校根域活, 域名变体+浏览器渲染均失败) → 7天账本冻结")
            _mark_freeze(school, dept_dir.name, form="subnet_dead")
            # 2026-10-16 波期豁免语义收窄: 子站不可达是站点侧问题非LLM波期
            # (0015 批大连海事体育工作部实锤: 空原因被批次侧读成波期豁免)
            _mark_retry_reason(d, "子站公网不可达(域名变体+渲染均失败), 7天冻结账本")
            return "RETRY"
        print("  ❌ 师资页不可达(变体+根域均失败)")
        if _teaching_org:
            # 2026-08-31 教学组织先验: 不可达多为瞬态(壳/超时), 转 RETRY 不落 SKIP
            print("  ⚠️ 教学组织师资页不可达存疑, 转 RETRY 重试")
            _mark_retry_reason(d, "教学组织师资页不可达存疑")
            return "RETRY"
        # 2026-10-12 用户指令(机构师资预期分级): 医院/研究院/实验室不可达
        # 是网络问题≠无师资 — 同样转 RETRY, 只有 unlikely 类才落 SKIP
        if _org_faculty_expectation(dept_type, dept) in ("likely", "may"):
            print("  ⚠️ 机构类(likely/may)师资页不可达存疑, 转 RETRY 重试")
            _mark_retry_reason(d, "机构类(likely/may)师资页不可达存疑")
            return "RETRY"
        # 2026-10-13 SKIP分类化(用户指令「把skip分成具体类型」): 不可达
        # =「请求失败」类 ≠ 「确实没有」— unlikely(书院/继续教育)不可达也
        # 转 RETRY + 深冻账本(7天冷却防死循环), 绝不落无师资 SKIP
        print("  ⚠️ 机构类(unlikely)师资页不可达存疑, 转 RETRY 深冻")
        _mark_freeze(school, dept_dir.name, form="subnet_dead")
        _mark_retry_reason(d, "机构类(unlikely)师资页不可达存疑")
        return "RETRY"

    _tm_mark("faculty页")
    # 收集页面教师级链接样本(供LLM改进时参考)
    page_links = []
    try:
        soup = BeautifulSoup(fac_html, "lxml")
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            h = a.get("href", "").strip()
            if re.match(r'^[一-鿿·]{2,4}$', t) and h and not h.startswith(("javascript", "#")):
                page_links.append({"text": t, "href": h[:90]})
    except Exception:
        pass

    _tm_mark("samples")
    best_total = -1  # 历史上最好的人数(用于回滚判定)
    crash_hint = ""  # 跨轮携带: R1/R3 发现崩溃 → R2 注入评估原因喂给 LLM
    # 有效轮次记账: LLM 网关截杀波/断供时段, 改进轮白耗在"模板兜底=现状"上
    # (自动化的教训: 第2/3轮重复同一模板spider空转)。LLM 失败轮次不消耗
    # 有效轮次, 而是消耗自由轮次(上限2) — 网关恢复窗口内仍能获得真实改进尝试。
    free_left = 2
    # 跨轮累积失败诊断(2026-08-31 用户指令: 重试必须带着问题重新分析 —
    # 第N轮改进提示词注入第1..N-1轮全部评估原因, 而非只看本轮)
    all_reasons = []
    _round_reasons = []  # 批内评估原因(隔离版): 只喂终态分类, 不含历史注入
    _sig = ""  # 2026-10-25 P0-2: 本轮评估原因签名(跨轮稳定性检测)
    # 2026-10-15 跨批反思(用户指令「改进retry机制」, ReAct 式): 批次间也是
    # 一个反思循环 — 上批的 RETRY 原因已沉淀在 .retry_context.md(_mark_retry_reason
    # 落盘), 入场即读入 as 首批诊断, LLM 改进提示词与终态分类都能看到历史;
    # 批内轮次的 reasons 再继续累积(跨批+跨轮同一条链)。上限15条复用原有约束。
    # 隔离声明: 注入 LLM 的是全量 all_reasons; 终态分类(_classify_terminal/
    # _url_terminal_grade)只消费批内轮次的评估原因 — 历史"双查无师资(待复核)"
    # 命中 _NO_DATA_HINTS 会把本批有数据院系误分流 no_data_skip(污染实锤),
    # 批内原因单独记 _round_reasons 传给终态分流。
    try:
        _cf = d / ".retry_context.md"
        if _cf.exists():
            for _l in _cf.read_text(encoding="utf-8").split("\n"):
                _l = _l.strip()
                if _l and _l not in all_reasons and len(all_reasons) < 8:
                    all_reasons.append(_l)
    except Exception:
        pass

    for rnd in range(max_rounds):
        # 2026-09-03 波#4 修复: 熔断打开时不再启动新轮次 — 在途院系骑波跑满3轮
        # 烧到900s僵尸(山大5例: 东北亚/信息学院/可持续制造/国际问题/智能创新,
        # 均在 21:07 进场、21:14 波起时在途); 第1轮放行(spider 数据有独立价值,
        # LLM 环节快速失败不占预算), 第2轮起直接 RETRY — 预算从 ~900s 压到
        # ~550s, 波后健康期重跑拿回数据
        if rnd >= 1 and _llm_breaker_open():
            _tm_mark(f"R{rnd}.wave_abort")
            # 2026-09-03 波#10 实锤: 早退前须恢复入场快照 — 厦大经济学院入场
            # 304人, R1 蜘蛛(未拿到LLM改进)覆写117人, 早退后CSV停在117,
            # 全库 teachers -187 覆盖丢失。只在下滑时恢复(同终态口径: 净+2/脏-1),
            # 不覆盖真正更好的本轮产物; 恢复的是入场4件套(含meta/JSON防hash失配)
            _cur_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
            _cur_n = len(_cur_rows) - 1 if _cur_rows else 0
            _cur_clean2 = sum(1 for r in _cur_rows[1:]
                              if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                              and _norm_name(r[2].strip()) not in NAV_WORDS
                              and _norm_name(r[2].strip()) not in _bad_names)
            _cur_score2 = _cur_clean2 * 2 - (_cur_n - _cur_clean2)
            _best_score2 = best_clean * 2 - (best_n - best_clean)
            if best_n >= 3 and _cur_score2 < _best_score2 - 1:
                restore_best()
            print(f"  🌊 熔断打开, 第{rnd+1}轮早退转 RETRY(避免骑波烧预算成僵尸)")
            _mark_retry_reason(d, "")
            return "RETRY"
        # R1: 运行 spider.py
        print(f"  ▶️ 运行spider.py (第{rnd+1}轮) ...")
        _sp_err = None
        try:
            r, _sp_out, _sp_err = _run_spider(d, timeout=_spider_timeout(len(fac_urls)))
            if r is not None:
                for l in _sp_out.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[-2:]:
                    print(f"     {l[:80]}")
        except Exception as e:
            print(f"     💥 {str(e)[:60]}")
            r = None
        # LLM生成的spider可能写错文件名(如 学校_学院_teachers.csv), 修正回标准名
        for alt in d.glob("*teachers.csv"):
            if alt.name != "teachers.csv":
                alt.rename(d / "teachers.csv")
        _repair_json_from_csv(d)  # 错名CSV改名后同样收敛一次(JSON可能仍指向旧状态)
        _sync_meta(d, school, dept)
        _tm_mark(f"R{rnd+1}.spider")
        # spider 崩溃 → 崩溃信息反馈给 LLM(下轮提示词)。否则产物是旧CSV,
        # 评估原因不变, 循环空转(新闻学院 import util 崩溃的教训)
        if r is not None and r.returncode != 0:
            try:
                _err = (_sp_err.read_text(encoding="utf-8", errors="ignore") or "").strip()[-300:]
            except Exception:
                _err = ""
            print(f"     💥 spider崩溃(exit {r.returncode}): {_err[:80]}")
            crash_hint = f"上次spider.py运行崩溃(exit {r.returncode}): {_err}"
        if not (d / "teachers.csv").exists():
            # spider 失败未产出 (2026-09-10 后无浏览器守卫拒绝路径)
            _berr = ""
            try:
                _berr = (_sp_err.read_text(encoding="utf-8", errors="ignore") or "") if _sp_err else ""
            except Exception:
                _berr = ""
            # (2026-09-10 架构升级: 「个人 Chrome 未运行守卫拒绝→WAIT_BROWSER
            # 挂起」分支已删 — 独立 Chromium 下无此错误签名, 挂起机制整套退役)
            # 2026-09-21 浏览器兜底前移: spider 提取全败(无CSV)时先浏览器
            # 渲染兜底提取 — 清华机械类旧 spider 提取逻辑失效但页面有名单,
            # 此前该分支直接 RETRY, 而 R1 回退块(浏览器渲染)以 CSV 存在
            # 为前提永远没机会跑, 反复 RETRY 烧轮次(70+ 次实测)
            _bgot2 = False
            # 2026-09-24 长安公路实锤: spider全败时渲染兜底只渲染 fac_url
            # 单页(队伍结构=壳) — 名单在同组多入口(博士导师/硕士导师)JS
            # 表格页。遍历同组入口渲染提取(≤4页), 任一>0即落盘
            _rh_pages2 = [fac_url] + [u for u in fac_urls if u != fac_url] if fac_url else list(fac_urls)
            _rh_pages2 = [u for u in _rh_pages2 if u]
            for _fu2 in _rh_pages2[:4]:
                if _bgot2:
                    break
                _rh2 = None
                # 2026-09-21 SEM饥饿修复: render_and_probe(9222)走 SEM,
                # cloak 兜底直调(锁内置)
                if _BROWSER_VERIFY_SEM.acquire(timeout=30):
                    try:
                        try:
                            from util.faculty_api import render_and_probe
                            _rh2, _rcfg, _rcfgs = render_and_probe(_fu2, timeout=30)
                        except Exception:
                            _rh2 = None
                    finally:
                        _BROWSER_VERIFY_SEM.release()
                if not _rh2 or len(_rh2) <= 3000 or _is_js_shell(_rh2):
                    _rh2 = _render_fetch(_fu2)
                print(f"  🔍 渲染兜底[{_fu2.split('/')[-1]}]: len={len(_rh2 or '')} shell={_is_js_shell(_rh2) if _rh2 else '?'}")
                if _rh2 and len(_rh2) > 3000:
                    try:
                        from util.teacher_extract import extract_teachers_multi  # NAV_WORDS 用模块级(函数内import会遮蔽成局部变量→NameError, 2026-09-21实锤批量异常)
                        _bts2 = [t for t in _extract_all_teachers(_fu2, _rh2)
                                 if t and t[0] and t[0] not in NAV_WORDS
                                 # 2026-10-14 医院科室名过滤(中山128人8噪声实锤):
                                 # 渲染兜底不走util merge, 此处同口径补滤
                                 and not re.fullmatch(r"[一-鿿]{2,4}(?:外科|内科)|[一-鿿]{3,4}科", t[0])]
                        print(f"  🔍 [{_fu2.split('/')[-1]}] 提取{len(_bts2)}人")
                        if not _bts2:
                            # 2026-10-14 字母索引页跟进(云大经济学院 jsjs.htm
                            # 实锤): 索引页本身无教师行, 名单在 A-Z 子页 —
                            # 渲染字母子页提取, 任一有名单即采用
                            # 2026-10-14 科室页跟进扩展(中山医院 5018 实锤):
                            # 学科及专家页只有科室链接(心内科等), 医生卡片在
                            # 科室页 — 科室页有 jp 壳 CPU 链拿不到, 渲染跟进
                            # 提取 talent-li 卡片
                            try:
                                from bs4 import BeautifulSoup as _BS
                                from urllib.parse import urljoin as _uj
                                _lset = set()
                                for _la in _BS(_rh2 or "", "lxml").find_all("a", href=True):
                                    _lt = _la.get_text(strip=True)
                                    if (re.fullmatch(r"[A-Za-z]", _lt)
                                            or re.search(r"(?:外科|内科|科室|科)$", _lt)):
                                        _lset.add(_uj(_fu2, _la.get("href", "")))
                                if len(_lset) >= 5:
                                    for _lu in sorted(_lset):
                                        _lh = _render_fetch(_lu)
                                        if not _lh or len(_lh) <= 3000:
                                            continue
                                        _bts2 = [t for t in extract_teachers_multi(_lh, _lu)
                                                 if t and t[0] and t[0] not in NAV_WORDS
                                                 and not re.fullmatch(r"[一-鿿]{2,4}(?:外科|内科)|[一-鿿]{3,4}科", t[0])]
                                        if _bts2:
                                            fac_url = _lu
                                            print(f"  🔤 子页[{_lu.split('/')[-1]}] 提取{len(_bts2)}人")
                                            break
                            except Exception:
                                pass
                            for _fu, _fh in _fac_family_render(_fu2, _rh2):
                                _bts2 = [t for t in extract_teachers_multi(_fh, _fu)
                                         if t and t[0] and t[0] not in NAV_WORDS]
                                if _bts2:
                                    fac_url = _fu
                                    break
                        if _bts2:
                            from util.teacher_extract import write_outputs as _wo3
                            try:
                                _wo3(school, d.name, [list(t) + [fac_url] for t in _bts2])
                                _bgot2 = True
                                (d / ".render_priority").write_text(
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" 渲染兜底{len(_bts2)}人", encoding="utf-8")
                                print(f"  🌐 spider全败浏览器渲染兜底: 提取 {len(_bts2)} 人 → 落盘继续")
                            except Exception as _e3:
                                print(f"  🔍 写盘异常: {str(_e3)[:80]}")
                    except Exception as _e4:
                        print(f"  🔍 提取异常: {str(_e4)[:80]}")
            if not _bgot2:
                print("  ⚠️ spider未生成teachers.csv")
                restore_best()
                with _KB_LOCK:
                    record_case(f"{school}/{dept}", {"error": "spider未生成teachers.csv",
                                                     "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                # 瞬态失败(崩溃/未产出): 走队列重试, 不算3轮终态(2026-08-31 用户指令:
                # 终态只给"评估→改进3轮未通过"的院系; 崩溃类重试不烧LLM改进token)
                _mark_retry_reason(d, "spider未生成teachers.csv")
                return "RETRY"

        # (R1.5 净化兜底已移除: sanitize_teachers_csv 事后改 CSV 不改 JSON,
        #  造成 CSV/JSON 不一致; 且遮蔽 spider 缺陷(重跑即回归), 违背
        #  "CSV必须是spider.py的产物"不变量 — 脏数据必须由 LLM 改进 spider 解决)

        # (R1.6 2026-08-26: spider 成功产出非空 CSV → 清掉 probe 残留的
        #  no_faculty_page 状态(test_report.json 旧文件, 如 AJAX 空壳站早期
        #  误判"无师资页" — 评估器读它直接判 LEGAL_EMPTY, 数据被误杀)
        try:
            _rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
            if len(_rows) > 1:
                _tr = d / "test_report.json"
                if _tr.exists():
                    _j0 = json.load(open(_tr, encoding="utf-8"))
                    if _j0.get("status") in ("no_faculty_page", "no_faculty", "unreachable_or_no_faculty",
                                             "no_url"):
                        json.dump({"status": "collected", "reason": "spider产出非空CSV, 覆盖probe旧误判",
                                   "rows": len(_rows) - 1,
                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                  open(_tr, "w"), ensure_ascii=False, indent=2)
        except Exception:
            pass

        # R2: 评估
        # 2026-09-04 无效重试封顶(用户指令): R1 spider 0 教师 → 提取全败/页面无数据,
        # LLM 改进轮(300s×N+token)没有可改进对象, 直接 RETRY — 每轮只烧 spider 段;
        # 有历史好数据(best_n>=3)时除外, 由评估轮正常比较回滚
        if rnd == 0 and best_n < 5:
            try:
                _r0 = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
            except Exception:
                _r0 = []
            # 2026-09-29 新疆外语实锤: VSB JS 壳页静态只出3人(江秋丽等), 走查
            # 渲染后 9 页有 31 人 — 渲染兜底只在 CSV 空时触发漏掉 3-4 人小产
            # 出壳页; 阈值放宽到 ≤3 人(带节流, 见下)
            if len(_r0) <= 4:
                # 2026-09-29 西北钱学森 EXC 实锤: fac_url 被平台作废(None)后
                # 渲染兜底拿它当 URL → None.startswith 崩溃。fac_url 为空时
                # 渲染对象只剩同组页(全空则走 RETRY)
                _rh = None
                if fac_url:
                    # 2026-09-21 SEM饥饿修复: render_and_probe(9222)走 SEM
                    if _BROWSER_VERIFY_SEM.acquire(timeout=30):
                        try:
                            try:
                                from util.faculty_api import render_and_probe
                                _rh, _rcfg, _rcfgs = render_and_probe(fac_url, timeout=30)
                            except Exception:
                                _rh = None
                        finally:
                            _BROWSER_VERIFY_SEM.release()
                    # 2026-09-21 误判根修: 9222 渲染空白时 cloak 反检测兜底
                    if not _rh or len(_rh) <= 3000:
                        _rh = _render_fetch(fac_url)
                # 2026-09-24 长安公路实锤: CPU壳名单分散多个JS渲染页(博士导师
                # bsds.htm+硕士导师 ssds.htm), 渲染兜底只渲染 fac_url 单页
                # → 只捞到1人。改为遍历同组入口(≤4页)逐个渲染提取,
                # 任一人数>0即落盘
                _bgot, _bts = False, []
                _rh_pages = [(_u, _rh if _u == fac_url else None)
                             for _u in ([fac_url] + list(fac_urls)) if _u]
                for _i3, (_fu3, _fh3) in enumerate(_rh_pages[:4]):
                    if not _fh3:
                        _fh3 = _render_fetch(_fu3)
                    if not _fh3 or len(_fh3) <= 3000:
                        continue
                    try:
                        _bts = [t for t in _extract_all_teachers(_fu3, _fh3)
                                if t and t[0] and t[0] not in NAV_WORDS]
                    except Exception:
                        _bts = []
                    if not _bts:
                        continue
                    _bgot = True
                    fac_url = _fu3
                    break
                if _bgot:
                    from util.teacher_extract import write_outputs as _wo
                    try:
                        _wo(school, d.name, [list(t) + [fac_url] for t in _bts])
                        (d / ".render_priority").write_text(
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" 渲染兜底{len(_bts)}人", encoding="utf-8")
                        print(f"  🌐 浏览器渲染提取 {len(_bts)} 人(CPU链全败) → 落盘继续")
                    except Exception:
                        _bgot = False
                if not _bgot:
                    _mark_retry_reason(d, "R1 spider 0 教师")
                    print("  ⏭️ R1 spider 0 教师(浏览器渲染也0), 跳过LLM改进轮直接 RETRY(无效重试封顶)")
                    return "RETRY"
        # 2026-09-04 波期转下载(用户指令): 入口时熔断打开的院系, R1(确定性
        # fetch+提取)完成后网关仍不可用才跳过评估。复盘修正(12:45 用户质疑1小时
        # 零进展): 原用 _recent_wave()(300s 窗)在振荡期(开-关-开)几乎恒真 —
        # 健康窗口被浪费(12:04/12:28 的 PASS 全来自非波期入口院系); 改查当前
        # 熔断状态: 此刻关=健康窗口立即评估(评估中窗口再关有"网关不可用→RETRY"
        # 兜底); 此刻开才跳过。数据已落盘, 波后健康期重跑评估拿终态
        if wave_entry and rnd == 0 and _llm_breaker_open():
            _tm_mark(f"R{rnd+1}.wave_download_done")
            # 2026-09-04 补漏: 波期 R1 重跑可能产出劣于入场快照的 CSV(限速站
            # 抓半页/网络抖动 — 中大国际翻译 90→7 人实测, 教师总数倒挂 -78)。
            # 无评估轮比较时劣 CSV 一直留在盘上; 与 R2+ 早退同口径: 劣于 best
            # 即 restore_best(4件套), 好数据保底, 波后健康期重跑再评估
            try:
                _rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
                _n = len(_rows) - 1 if _rows else 0
                _clean = sum(1 for r in _rows[1:]
                             if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                             and _norm_name(r[2].strip()) not in NAV_WORDS)
                if best_n >= 3 and _clean * 2 - (_n - _clean) < (best_clean * 2 - (best_n - best_clean)) - 1:
                    restore_best()
            except Exception:
                pass
            _mark_retry_reason(d, "")
            print("  🌊 波期转下载: R1数据已落盘, 熔断仍开跳过评估直接 RETRY(波后评估)")
            return "RETRY"
        ev = evaluate_dept(school, d, dept_url)
        if ev["grade"] == "LEGAL_EMPTY":
            # 2026-09-07 用户指令: 学院型纯文本名单(no_url)不得落终态 — 学院/大学
            # 应有带链接入口, no_url = 发现链失败(81 存量已撤销重采)。转 RETRY
            # 由渲染验证+重试链再找链接入口; 小中心/实验室类维持原终态语义
            _tr = d / "test_report.json"
            _st = ""
            if _tr.exists():
                try:
                    _st = json.load(open(_tr, encoding="utf-8")).get("status", "") or ""
                except Exception:
                    pass
            # 2026-09-21 合法空误判根修(测试实锤): 55样本 69% 能采出教师 —
            # 误判不分组织类型(非学院型同样大量误判: 上海工程技术研究中心/
            # 江苏省大气实验室/南大思想家中心等 FIXED; 评估器日志自身标
            # 「疑似误判需人工复核」却被无视落终态, 半小时误杀~1700个)。
            # 任何组织类型的合法空都必须过浏览器渲染复核(CloakBrowser
            # 反检测+渲染提取+家族枚举): 有教师落盘复评估; 无教师 — 学院型
            # RETRY 待复核(绝不落终态), 非学院型带浏览器证据链才落
            # LEGAL_EMPTY(浏览器兜底全盲的仍转 RETRY, 不落终态)。
            _teaching = bool(re.search(r"(学院|大学|学部)$", dept))
            _rechecked = False
            # 2026-09-21 SEM饥饿修复: cloak 直调(锁内置)优先, 9222 走 SEM 兜底
            _ch = _render_fetch(dept_url)
            if not _ch and _BROWSER_VERIFY_SEM.acquire(timeout=30):
                try:
                    _ch = _browser_fetch(dept_url, timeout=30)
                finally:
                    _BROWSER_VERIFY_SEM.release()
            if _ch and len(_ch) > 500:
                try:
                    from util.teacher_extract import extract_teachers_multi  # NAV_WORDS 用模块级(函数内import会遮蔽成局部变量→NameError, 2026-09-21实锤批量异常)
                    # 两级复核: 渲染首页 → 找师资入口 → 渲染入口页提取
                    _bfac = find_faculty_page(dept_url, _ch)
                    _targets = [(dept_url, _ch)]
                    if _bfac and not _NAV_PAGE_RE.search(_bfac):
                        _fh = _render_fetch(_bfac)
                        if not _fh and _BROWSER_VERIFY_SEM.acquire(timeout=30):
                            try:
                                _fh = _browser_fetch(_bfac, timeout=30)
                            finally:
                                _BROWSER_VERIFY_SEM.release()
                        if _fh and len(_fh) > 500:
                            _targets.append((_bfac, _fh))
                    _bts = []
                    _final_url = _targets[-1][0]
                    for _tu, _th in _targets:
                        _bts = [t for t in _extract_all_teachers(_tu, _th)
                                if t and t[0] and t[0] not in NAV_WORDS]
                        if _bts:
                            _final_url = _tu
                            break
                    if not _bts and _bfac and _fh:
                        # 2026-09-21 第三层修复: 概况页→名单页家族枚举
                        for _fu, _fh2 in _fac_family_render(_bfac, _fh):
                            _bts = [t for t in extract_teachers_multi(_fh2, _fu)
                                    if t and t[0] and t[0] not in NAV_WORDS]
                            if _bts:
                                _final_url = _fu
                                break
                    if not _bts:
                        # 2026-09-22 复旦数院实锤: 发现链可能选错页(人才工作
                        # 小组≠名单页), fac家族0后枚举首页候选(教师队伍/专任
                        # 教师/学术带头人等链接)
                        for _fu, _fh2 in _fac_family_render(dept_url, _ch):
                            _bts = [t for t in extract_teachers_multi(_fh2, _fu)
                                    if t and t[0] and t[0] not in NAV_WORDS]
                            if _bts:
                                _final_url = _fu
                                break
                    if _bts:
                        from util.teacher_extract import write_outputs as _wo2
                        try:
                            _wo2(school, d.name, [list(t) + [_final_url] for t in _bts])
                            _rechecked = True
                            (d / ".render_priority").write_text(
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" 渲染复核{len(_bts)}人", encoding="utf-8")
                            print(f"  🌐 合法空浏览器复核: 渲染提取 {len(_bts)} 人 → 落盘")
                        except Exception:
                            pass
                except Exception:
                    pass
            if _rechecked:
                ev = evaluate_dept(school, d, dept_url)
                if ev["grade"] == "PASS" \
                        and not _detail_url_compliance(d, fac_html, fac_url):
                    _mark_pass(d, ev.get("total", 0))
                    return "PASS"
            if _teaching:
                _mark_retry_reason(d, "学院型合法空待复核(浏览器复核后)")
                print("  🔁 学院型合法空不落终态(误判教训), 浏览器复核后 RETRY 待复核")
                return "RETRY"
            print(f"  📋 评估判定合法空(浏览器复核后): {ev['reasons'][0] if ev.get('reasons') else ''}")
            return "LEGAL_EMPTY"
        # 2026-10-16 用户指令: 详情URL合规检测 — PASS 前最后一道拦截,
        # 不合规不PASS, 诊断并入评估原因走改进轮「带着明确的问题去完善」
        _uv = _detail_url_compliance(d, fac_html, fac_url)
        if ev["grade"] == "PASS":
            if _uv:
                print(f"  ⚠️ 评估PASS但详情URL不合规({_uv}) → 拦截PASS, 带问题完善")
            else:
                # 2026-10-26 名单不缩减守卫(上外新闻 36种子→5 实锤): PASS 前
                # 把旧管线名单中当前产物缺失的姓名补回, 重采集拿不到的旧名单
                # 姓名不得被 PASS 落盘缩水覆盖
                _seed_backfill(d)
                _mark_pass(d, ev.get("total", 0))
                print(f"  ✅ PASS ({ev['total']}人)")
                return "PASS"
        _tm_mark(f"R{rnd+1}.eval")

        reasons = [x for x in ev["reasons"] if x]
        if _uv:
            reasons.append(f"详情URL不合规: {_uv}")
        # 跨轮累积诊断(去重保序, 上限15条防提示词膨胀)
        for _r in reasons:
            if _r not in all_reasons and len(all_reasons) < 15:
                all_reasons.append(_r)
            if _r not in _round_reasons and len(_round_reasons) < 15:
                _round_reasons.append(_r)
        # 2026-10-25 P0-2: 原因签名(前4条各截60字) — 跨轮稳定性检测
        _prev_sig = _sig
        _sig = ";".join(r[:60] for r in reasons[:4])
        # 2026-10-25 P0-3: 共享URL族(≥90%列表页形态URL, E类上游缺陷)早判 —
        # LLM改spider无法修复上游填充的名单页URL(华科未来266/266三轮实锤),
        # 跳过剩余轮次直接终态, 不烧LLM改进token
        _shu = next((r for r in reasons if "列表页形态URL" in r), "")
        if _shu:
            _sm = re.search(r"(\d+)/(\d+)", _shu)
            if _sm and int(_sm.group(2)) > 0 and int(_sm.group(1)) >= int(_sm.group(2)) * 0.9:
                print("  ⚡ 共享URL族(≥90%列表页形态, E类上游缺陷): 跳过剩余轮次直接终态")
                restore_best()
                try:
                    json.dump({"status": "fail_extraction",
                               "reason": f"共享URL族(E类上游缺陷): {_shu[:120]} — 上游名单页URL批量填充, 本地改spider无效(已反馈上游)",
                               "rounds": rnd + 1,
                               "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                              open(d / "test_report.json", "w"),
                              ensure_ascii=False, indent=2)
                except Exception:
                    pass
                return "FAIL"
        # P0: 评估器的非名清单跨轮积累, 质量计数与 LLM 判定同口径
        _bad_names |= set(ev.get("bad_names") or []) | set(ev.get("llm_non_names") or [])
        # 词表自我进化: 捕获的非名沉淀进 knowledge_nav_words.json,
        # util.teacher_extract 加载时合并 — 站点专名第二次即被排除
        # 并发写盘必须加锁(实测多worker互覆导致JSON损坏)
        # 2026-10-09 真名污染根治(6143条实锤): 2-3字纯中文无栏目尾词的
        # 条目大概率是真名(丁辉/吴萌/王婧被LLM终审误判沉淀后全站误杀) —
        # 入词表守卫: 真名形态(2-3字纯汉字, 无 技术/学院/中心/招生 等栏目尾词,
        # 无数字开头)一律不沉淀; 栏目词几乎都是4+字或带特征尾词。
        _WT_TAIL = re.compile(r"(技术|学院|中心|招生|高考|通知|公告|新闻|系统|平台|"
                              r"办公室|建设|管理|服务|部|处|室|组|队|系|所|馆|站|网|"
                              r"委员会|研究院|协会|指导|课程|专业|方向|学科|培养|教育|"
                              r"研究|科学|中心|基地|平台|基金|计划|项目|青年)$")
        _WT_NUM = re.compile(r"[0-9一二三四五六七八九十]+")
        _bad_wt = set()
        for _w in _bad_names:
            if (isinstance(_w, str) and 2 <= len(_w) <= 3
                    and re.fullmatch(r"[一-鿿]+", _w or "")
                    and not _WT_TAIL.search(_w)
                    and not _WT_NUM.search(_w)):
                continue  # 真名形态 — 不入词表
            _bad_wt.add(_w)
        try:
            with _KB_LOCK:
                _ep = BASE.parent / "knowledge_nav_words.json"
                _old_w = json.load(open(_ep, encoding="utf-8")) if _ep.exists() else []
                _new_w = sorted(set(_old_w) | _bad_wt)
                if len(_new_w) > len(_old_w):
                    _ep.write_text(json.dumps(_new_w, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        if crash_hint:
            reasons.append(crash_hint)
            crash_hint = ""  # 已消费, 避免下一轮重复注入同一崩溃信息
        # 学习闭环: 失败案例沉淀进 knowledge/teacher_patterns.json(failed_cases),
        # 后续 llm_improve_spider 提示词会注入历史失败模式
        try:
            with _KB_LOCK:
                record_case(f"{school}/{dept}", {
                    "faculty_url": fac_url,
                    "reasons": reasons[:8],
                    "round": rnd + 1,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception:
            pass
        print(f"  ❌ FAIL: {'; '.join(reasons)[:100]}")
        if (d / ".render_priority").exists():
            # 2026-10-08 渲染产物优先终态: 渲染兜底产物在盘(CPU链无法超越),
            # 跳过LLM改进轮(改进只会把CPU链spider的低产覆盖回去) — 终态前
            # 先按质量回滚守卫: 渲染产物劣于入场快照则恢复快照(单调改进)
            restore_best()
            _urlg_rp = _url_terminal_grade(d, reasons)
            if _urlg_rp == "PASS":
                _mark_pass(d, 0, "渲染产物URL复核达标")
                print("  ✅ 渲染产物 URL复核达标(缺失≤50%), PASS")
                return "PASS"
            _rp_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
            _rp_n = len(_rp_rows) - 1 if _rp_rows else 0
            # 2026-10-25 用户指令: 3轮后问题不明显→PASS(容忍少数错误数据)
            if _minor_tolerance_pass(d, reasons):
                _mark_pass(d, 0, "终轮容忍PASS: 问题不明显(非人名混入/数量轻微差异)")
                print("  ✅ 终轮容忍PASS: 问题不明显, 按用户指令容忍少数错误数据")
                return "PASS"
            try:
                json.dump({"status": "fail_extraction",
                           "reason": (f"渲染兜底产物({_rp_n}人)评估未通过: " + "; ".join(reasons)[:380])
                                     if reasons else f"渲染兜底产物({_rp_n}人)评估未通过",
                           "rounds": rnd + 1,
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
            except Exception:
                pass
            print(f"  🏁 渲染产物优先终态: fail_extraction({_rp_n}人保留在盘)")
            return "FAIL"
        # 2026-10-25 P0-2: 连续两轮原因一致(上轮改进未触根因) → 提前终态,
        # 不烧剩余轮次(华科未来同因三轮原地打转实锤)
        if rnd == max_rounds - 1 or (rnd >= 1 and _sig and _sig == _prev_sig):
            if rnd < max_rounds - 1:
                print("  🔁 连续两轮评估原因一致(改进未触根因) → 提前终态")
            else:
                print(f"  ⚠️ {max_rounds}轮未通过, 放弃")
            cur_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
            cur_n = len(cur_rows) - 1 if cur_rows else 0
            # 合法空收敛: probe 全站无强教师信号(详情/卡片/JS数组>0 或 名职对>=2)
            # 且(净姓名0 或 起止<10且评估已证实有非人名/数量不足) → 网站未发布教师名录。
            # 注意: 净姓名>0 也可能是新闻/活动报道里的人名(海南国际学院: 18名志愿者
            # 新闻被误当教师声明, 首页人名是学生), 无教师信号验证则一并按合法空处理。
            # 门槛设计: "数量硬规则+非人名混入"双确认避免把真教师数据误清空。
            try:
                cur_clean = sum(1 for r in cur_rows[1:]
                                if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                                and _norm_name(r[2].strip()) not in NAV_WORDS
                                and _norm_name(r[2].strip()) not in _bad_names)
                _rs = "; ".join(reasons) if reasons else ""
                _flagged = bool(re.search(r"非人名混入 \d+个", _rs))
                _few = bool(re.search(r"数量硬规则", _rs))
                if ((cur_clean == 0 and best_clean < 10)
                        or (cur_clean < 10 and best_clean < 10 and _flagged and _few)):
                    af2 = d / "analysis.md"
                    _an = af2.read_text(encoding="utf-8") if af2.exists() else ""
                    if _an and not re.search(r"详情[1-9]|卡片[1-9]|JS数组[1-9]|名职对[2-9]", _an):
                        if _teaching_org:
                            # 2026-08-31 教学组织先验: 学院/学部/系判"无教师信号"
                            # 是壳/超时蒙蔽, 转 RETRY(8次封顶)不落合法空终态
                            # 2026-10-25 矿藏户增量修复(用户「数量没有增长」): 教学组织
                            # 判无教师信号常是「师资菜单指向简介页」(北大信科实锤:
                            # eecs.pku.edu.cn 师资队伍→szdw/jj.htm 简介页无教师链接) —
                            # 转RETRY前先搜索引擎兜底找真实师资页URL, 命中写回
                            _fb2 = _search_url_fallback(school, dept, fac_url or dept_url, d)
                            if _fb2 and _fb2.rstrip("/") != (fac_url or "").rstrip("/"):
                                _write_dept_url(d, _fb2)
                                print(f"  🔍 教学组织搜索兜底命中师资页: {_fb2[:60]}")
                                print(f"  ↪ 已写回院系URL, 转 RETRY 待重测")
                                _mark_retry_reason(d, f"搜索兜底修正URL: {fac_url} → {_fb2}")
                                return "RETRY"
                            print("  ⚠️ 教学组织判无教师信号存疑, 转 RETRY 重试")
                            _mark_retry_reason(d, "教学组织判无教师信号存疑")
                            return "RETRY"
                        if _recent_wave():
                            # 2026-09-03 波#4 修复: 研究所类组织波动期落 LEGAL_EMPTY
                            # 三例(山大先进射流526s/移民所746s/高等技术研究院797s) —
                            # improve 被烧(251-414s), 蜘蛛从未拿到改进后的规则,
                            # "多轮均无教师信号"结论不可信; 与 FAIL→RETRY 同口径
                            # (完成落在波动签名 300s 窗内) 转重试, 波后重跑复核
                            print("  ⚠️ 波动期无教师信号结论不可信(improve被烧), 转 RETRY")
                            _mark_retry_reason(d, "")
                            return "RETRY"
                        # 2026-10-12 用户指令(机构师资预期分级): 医院大概率有师资
                        # 不落合法空转人工复核; 研究院/实验室等(may)须浏览器
                        # 渲染确认无师资才可落合法空(probe 是 CPU 链, JS站会被
                        # 误判为无教师信号)
                        _exp = _org_faculty_expectation(dept_type, dept)
                        if _exp == "likely":
                            try:
                                json.dump({"status": "pending_human_analysis",
                                           "passed": False,
                                           "reason": "医院类机构无教师信号, 医院大概率"
                                                     "有师资(用户指令), 需人工复核",
                                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                          open(d / "test_report.json", "w"),
                                          ensure_ascii=False, indent=2)
                            except Exception:
                                pass
                            print("  👤 医院类无教师信号 → 人工复核终态, 不落合法空")
                            return "LEGAL_EMPTY"
                        if _exp == "may":
                            _vh = _render_fetch(fac_url or dept_url, timeout=30)
                            _has_signal = bool(_vh and len(_vh) > 500 and (
                                re.search(r"教授|副教授|研究员|讲师|博导|硕导|师资", _vh)
                                and len(re.findall(r'href="[^"]*"', _vh)) > 5))
                            if _has_signal:
                                print("  🔁 机构类(may)渲染见教师信号, 合法空结论不成立 → RETRY")
                                _mark_retry_reason(d, "机构类(may)渲染见教师信号, 合法空不成立")
                                return "RETRY"
                            print("  🌐 机构类(may)渲染确认无教师信号 → 可落合法空")
                        # unlikely 类(书院/继续教育)维持原判定直接落合法空
                        hdr = ["大学", "学院", "教师姓名", "职务职称", "所属院系", "个人详情URL", "师资页URL", "采集时间"]
                        with open(d / "teachers.csv", "w", encoding="utf-8", newline="") as f:
                            csv.writer(f).writerow(hdr)
                        json.dump({"school": school, "dept": dept, "total": 0, "teachers": []},
                                  open(d / "teachers.json", "w"), ensure_ascii=False, indent=2)
                        json.dump({"status": "no_teacher_org",
                                   "reason": "全站BFS+信号枚举无教师元素(详情/卡片/JS数组/名职对均0), "
                                             f"{max_rounds}轮提取均无教师信号验证 — 网站未发布教师名录",
                                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                  open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
                        print("  📋 判定合法空(no_teacher_org): 网站未发布教师名录")
                        return "LEGAL_EMPTY"
            except Exception:
                pass
            # 2026-08-30 严格纯文本审查出口: URL缺失类FAIL且多轮重扫无果 →
            # 全形态扫描(5类链接形态)确认页面确无任何教师详情链接, 才标
            # no_url(合法纯文本, 罕见例外); 扫描到候选则继续FAIL入队重试。
            _rs2 = "; ".join(reasons) if reasons else ""
            _url_fail = bool(re.search(r"URL缺失率|详情URL全空", _rs2))
            if _url_fail and fac_url and fac_html and len(fac_html) > 500:
                try:
                    from evaluate_teachers import verify_no_detail_links, count_teacher_elements
                    _dn, _ev = verify_no_detail_links(fac_html, fac_url)
                    if _dn == 0:
                        # 2026-08-30 数量校验: 纯文本页无链接是"形式合法",
                        # 但教师元素必须与CSV人数接近 — 页面大量纯文本教师而
                        # CSV只提了一小半 = 漏采(清华工业工程系: 52元素 vs CSV4人),
                        # 不得以"纯文本"放行
                        _ne, _ce = count_teacher_elements(fac_html, fac_url)
                        _csvn = max(0, len(cur_rows) - 1)
                        if _ne >= 5 and _csvn < _ne * 0.5:
                            print(f"  🔍 纯文本审查附加数量校验: 页面{_ne}元素 vs CSV {_csvn}人 → 漏采, 不得放行")
                        elif _teaching_org:
                            # 2026-09-22 复旦数院误判实锤: no_url 出口无教学组织
                            # 拦截 — 概况页(教授31人副教授32人叙述)被「严格纯文本
                            # 审查0详情链接」放行落终态, 但名单在更深页(师资队伍
                            # 子页)。学院型纯文本审查绝不落终态 → RETRY 待复核
                            _mark_retry_reason(d, "学院型no_url疑似概况页误判(复旦数院教训), 待复核")
                            print("  🔁 学院型no_url不落终态(复旦数院误判教训), RETRY 待复核")
                            return "RETRY"
                        else:
                            json.dump({"status": "no_teacher_org",
                                       "reason": f"严格纯文本审查通过({_ev}), 页面确无教师详情链接 — 名单为纯文本",
                                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                      open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
                            print(f"  📋 严格审查认可纯文本(no_url): {_ev}")
                            return "LEGAL_EMPTY"
                    else:
                        print(f"  🔍 纯文本审查未通过: {_ev} — 继续重试")
                except Exception as _e:
                    print(f"  ⚠️ 纯文本审查异常: {_e}")
            # 单调改进: 按质量评分(净+2/脏-1)恢复最佳 — 只按行数会把
            # 更干净的少行结果误回滚(新闻学院: 15净0脏 被 17行2脏 顶掉)
            cur_clean2 = sum(1 for r in cur_rows[1:]
                             if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                             and _norm_name(r[2].strip()) not in NAV_WORDS
                             and _norm_name(r[2].strip()) not in _bad_names)
            cur_score = cur_clean2 * 2 - (cur_n - cur_clean2)
            best_score = best_clean * 2 - (best_n - best_clean)
            if best_n >= 3 and cur_score < best_score - 1:
                restore_best()
            # ── 3轮终态分流(2026-08-31 用户指令) ──
            # 不再入队无限重试(浪费token/时间): 保留分析文档 analysis.md,
            # 结论写 test_report.json — skip(确实没有数据) / fail(有数据提取失败)。
            # 2026-09-02 网关波动防护: 评估期 LLM 终审因网关不可用未完成 → 属瞬态,
            # 不得落 3 轮终态(误杀有数据院系, 2026-09-02 22:00 波实测 10 例);
            # 转 RETRY 入队, 网关恢复后重评。
            if any("网关不可用" in (r or "") for r in all_reasons):
                print("  ⚠️ 评估期网关不可用(人名终审未完成), 转 RETRY 不落终态")
                _mark_retry_reason(d, "")
                return "RETRY"
            # 2026-09-07 用户规则: URL缺失>30%已复核(3轮评估→改进), 复核后
            # 缺失≤50% → PASS; >50% → FAIL 终态(不再入队磨坊)
            _urlg = _url_terminal_grade(d, _round_reasons)
            if _urlg == "PASS":
                _mark_pass(d, 0, "URL复核达标")
                print("  ✅ URL复核达标(缺失≤50%), PASS")
                return "PASS"
            _kind = _classify_terminal(fac_html, _round_reasons)
            if _urlg == "FAIL":
                print("  ❌ URL复核缺失仍>50%, 判失败(fail_extraction)")
                _kind = "fail_extraction"
            # 2026-10-25 用户指令: 3轮后问题不明显→PASS(容忍少数错误数据)
            if _kind == "fail_extraction" and _minor_tolerance_pass(d, _round_reasons):
                _mark_pass(d, 0, "终轮容忍PASS: 问题不明显(非人名混入/数量轻微差异)")
                print("  ✅ 终轮容忍PASS: 问题不明显, 按用户指令容忍少数错误数据")
                return "PASS"
            _br_reason = ""
            if _kind == "no_data_skip":
                # 2026-10-09 用户指令「最终判定有没有老师必须浏览器打开确认」:
                # JSON/XHR 动态渲染名单与瑞数WAF 静态链不可见, 渲染才现名单 —
                # 终态「网站未发布教师名录」落盘前浏览器打开最终确认,
                # 渲染见教师名单则改判 fail_extraction(数据存在, 提取待改进)
                _cand_u = fac_url or dept_url
                if _cand_u:
                    _bh = _render_fetch(_cand_u, timeout=30)
                    if _bh and len(_bh) > 3000:
                        try:
                            from util.teacher_extract import extract_teachers_multi
                            _bts = [t for t in extract_teachers_multi(_bh, _cand_u)
                                    if t and t[0] and t[0] not in NAV_WORDS]
                            if len(_bts) >= 2:
                                _kind = "fail_extraction"
                                _br_reason = (f"浏览器渲染确认存在教师名单({len(_bts)}人)"
                                              f"(静态链不可见), 判失败待改进")
                                print(f"  🌐 浏览器最终确认: 渲染见 {len(_bts)} 人 → 改判 fail_extraction")
                        except Exception:
                            pass
            _term_reason = ("; ".join(all_reasons)[:500] if all_reasons
                            else f"{max_rounds}轮评估→改进未通过, 无评估原因")
            if _br_reason:
                _term_reason = _br_reason
            try:
                json.dump({"status": _kind,
                           "reason": _term_reason,
                           "rounds": max_rounds,
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
            except Exception:
                pass
            print(f"  🏁 3轮终态判定: {_kind} — {_term_reason[:70]}")
            return "SKIP" if _kind == "no_data_skip" else "FAIL"

        # R3: LLM 改进 (带回滚: 新spider评估更差则恢复旧spider)
        print(f"  🧠 LLM改进spider ...")
        if _is_art_spider(spiderf):
            # 2026-09-17 整校污染防护: 艺术院校映射型 spider 归特殊采集器管 —
            # LLM 改进/模板兜底再生会把正确映射页换成发现链校级名录串页版本
            # (中戏 13 系 × 216 外籍名单/央美 14 系 × 218 校级名录 实锤)。
            # 评估不过只 RETRY 不动 spider, 真故障由特殊采集器重跑修复
            # (特殊采集器幂等全量覆盖, 是唯一合法写路径)。
            print("  🎨 艺术院校映射型spider — 转终态 art_mapper_pending(数据归特殊采集器)")
            # 2026-10-15 出清(#197)+波期豁免污染根修: 此路径原 _mark_retry_reason(d,"")
            # 空 reason 与波期语义撞车 → 批次侧误豁免不占计数 → 无限 RETRY 纯烧槽位
            # (2218 批国美基础部/央音继教院实锤)。转专用终态: 数据在盘, 等特殊
            # 采集器(art_college_fix.py 幂等覆盖); 选批器 _POOL 不含此状态不再重选。
            try:
                _n_art = 0
                if (d / "teachers.csv").exists():
                    import csv as _cv
                    _rs = list(_cv.reader(open(d / "teachers.csv", encoding="utf-8")))
                    _n_art = len(_rs) - 1 if _rs else 0
                json.dump({"status": "art_mapper_pending",
                           "reason": (f"艺术院校映射型(在盘{_n_art}人), 数据归特殊采集器"
                                      "art_college_fix.py 幂等覆盖; 批次LLM改进会错位校级"
                                      "名录(2026-09-17整校污染防护), 转终态不再重试"),
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                for _cl in (".retry_wave", ".retry_count", "_retry_reason.txt"):
                    (d / _cl).unlink(missing_ok=True)
            except Exception:
                pass
            return "ART_MAPPER_PENDING"
        current = spiderf.read_text(encoding="utf-8")
        backup = current
        backup_teachers = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
        # 产物快照: 回滚时 spider_meta.json / teachers.json 一起恢复, 否则溯源hash失配
        backup_meta = (d / "spider_meta.json").read_text(encoding="utf-8") if (d / "spider_meta.json").exists() else None
        backup_json = (d / "teachers.json").read_text(encoding="utf-8") if (d / "teachers.json").exists() else None
        # 注入 analysis.md 上下文(网页结构分析是改进依据)
        # 2026-10-25 收割后台 probe(≤300s): 改进轮消费分析产物前确保落盘
        if _pp is not None:
            try:
                _pp.wait(timeout=300)
            except Exception:
                try:
                    _pp.kill()
                except Exception:
                    pass
            try:
                if _pp_files:
                    _pp_files[0].close()
                    _pp_files[1].close()
            except Exception:
                pass
            _af1 = d / "analysis.md"
            if _af1.exists() and "选定师资页" not in _af1.read_text(encoding="utf-8", errors="ignore"):
                try:
                    _pe0.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " probe未找到选定师资页",
                                    encoding="utf-8")
                except Exception:
                    pass
        analysis_ctx = ""
        try:
            af = d / "analysis.md"
            if af.exists():
                analysis_ctx = af.read_text(encoding="utf-8")[:4000]
        except Exception:
            pass
        _tm_mark(f"R{rnd+1}.llm")  # LLM 改进起点: 慢失败(无进展)路径的耗时也归入本段, 不再污染下一轮 spider 段
        # 2026-09-03 波#10 实锤: 半开期改进调用挂满300s(R1.llm_done:303.0s 成批,
        # 15并发=75 工分/波白烧), 且改进没拿到时下一轮spider产物常劣于入场存量。
        # 波动签名未消(300s内有超时/快失败或熔断开)直接 RETRY: R1 spider 数据
        # 已落盘(独立价值已拿), 改进留波后健康期; 早退前恢复入场快照防覆盖
        # 丢失(厦大经济学院 304→117 实锤)。
        if _recent_wave():
            _w_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
            _w_n = len(_w_rows) - 1 if _w_rows else 0
            _w_clean = sum(1 for r in _w_rows[1:]
                           if len(r) > 2 and _NAME_RE.match(_norm_name(r[2].strip()))
                           and _norm_name(r[2].strip()) not in NAV_WORDS
                           and _norm_name(r[2].strip()) not in _bad_names)
            if best_n >= 3 and _w_clean * 2 - (_w_n - _w_clean) < (best_clean * 2 - (best_n - best_clean)) - 1:
                restore_best()
            print("  🌊 波动签名未消(300s内超时/快失败), 跳过LLM改进直接 RETRY(波后健康期重跑)")
            _tm_mark(f"R{rnd+1}.llm_done")
            _mark_retry_reason(d, "")
            return "RETRY"
        # 2026-10-25 P0-1: 部分成功信息注入 — 改进提示词告知已提取到的人数,
        # 已通过的部分保持不动只修失败原因(防把已好的部分改坏)
        _p_ok = ""
        try:
            _p_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8")))
            _p_n = len(_p_rows) - 1 if _p_rows else 0
            if _p_n > 0:
                _p_ok = (f"当前已提取到 {_p_n} 人——这些已提取出的教师保持不动, "
                         f"只针对失败原因修复遗漏/错误部分, 不要改动已生效的提取逻辑")
        except Exception:
            pass
        new_code = llm_improve_spider(school, dept, dept_url, fac_url, fac_html, current,
                                      all_reasons if all_reasons else reasons,
                                      analysis_ctx, fac_urls, fac_api, round_no=rnd + 1,
                                      partial_ok=_p_ok)
        if not new_code or len(new_code) < 300:
            # 2026-09-08 波动期烧时止血(实锤: R1.llm_done 303-541s 成批, 15 worker
            # 每波白烧 5-9min 主因=主改进失败后 300s 样本重试第二把火): 主改进
            # 失败往往意味着网关已进挂起波 — 立刻复查波动签名, 命中即跳过样本
            # 重试直接 RETRY(波后健康期重跑改进); 不命中才重试(空响应/401 浪恢复快)。
            if _recent_wave():
                print("     🌊 主改进已暴露波动(300s内超时/快失败), 跳过样本重试直接 RETRY")
                _tm_mark(f"R{rnd+1}.llm_done")
                _mark_retry_reason(d, "")
                return "RETRY"
            # LLM失败 → 注入页面教师级链接样本重试
            print("     ⚠️ 注入页面链接样本重试 ...")
            hint_prompt = f"""你是爬虫专家。为下面页面生成教师采集spider.py。

院系: {dept}
师资页: {fac_url}
评估失败原因: {'; '.join(reasons)}
页面中已探明的教师级链接样本(名字+URL):
{json.dumps(page_links[:25], ensure_ascii=False, indent=2)}

要求:
1. 写一个完整的独立运行spider.py, 含 fetch/clean_name/extract_teachers/main
2. 直接把这些教师级链接提取出来(名字取<a>文本, URL取href绝对化)
3. 排除导航词(首页/学院概况/师资队伍等), 只保留真实教师
4. 采集结果统一调用 util.teacher_extract.write_outputs(school, dept, teachers) 落盘 — teachers 元素 [name, title, detail_url, anchor, page]; 该函数原子写 teachers.csv/teachers.json/spider_meta.json 三件套, 禁止自行分步写这三个文件(分步写在 180s 超时 SIGKILL 时会造成 CSV 新/JSON 旧 的不一致 FAIL)
5. fetch 必须调用 util.smart_fetch.smart_fetch(url, timeout=N) — 只接受 url/timeout 两个参数, 不支持 charset 等其他参数; 文件头必须带 sys.path 引导: import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parents[4])); 严禁 import playwright/DrissionPage/selenium 或以任何方式启动浏览器 — 瑞数/JS 挑战由 smart_fetch 内部纯 CPU 链(iv8/nodenv/sdenv)自动破解, 浏览器是最后手段, spider 内禁用
6. 代码必须紧凑: 极少注释, 总长不超过 4000 字符(网关对约100秒以上的长生成会截断为空响应)
7. 学校={school}, 学院={dept}

返回JSON: {{"spider_code": "完整Python代码"}}
只输出JSON。"""
            # timeout=300: 波动期改进挂满全局 LLM_TIMEOUT(420s, 为健康探针长生成
            # 设的)会烧院系预算 — 实测 420s×1 + 90s verify ≈ 780s llm_done, 加
            # spider 即超 900s 僵尸(2026-09-03 波#3: 南科大技术转移/药理学系
            # 876.6/886.2s 濒临僵尸, 南邮3院系实僵尸)。健康期改进 56s 完成,
            # 300s 安全; 波动期省 120s×3 轮 = 360s 僵尸窗口。
            # timeout=120 (2026-09-08 修正): 旧注释的 300 是 LLM_TIMEOUT=420 时代
            # 遗留 — 现全局 120s, 300s 挂起=波动期白烧第二把火(18:02 实测每院系
            # llm_done 恒定 300s, 探针却 139s READY → 挂起发生在改进调用层);
            # 120s 与主调用同口径, 挂满即退。
            r2 = llm_ask_with_timeout("只输出JSON。", hint_prompt, timeout=420)
            res2 = parse_json(r2) if r2 else None
            new_code = res2.get("spider_code") if res2 else None
        if not (new_code and len(new_code) > 300):
            # 修正C: LLM失败 → 确定性模板兜底(不直接放弃)
            print("     ❌ LLM改进失败 → 确定性模板兜底")
            new_code = generate_spider(school, dept, dept_url, fac_url, fac_urls, fac_api)
        if new_code and len(new_code) > 300:
            if new_code == current:
                # 无进展: LLM失败且模板兜底与现状相同 → 继续硬跑也是空转
                # (网关断供时 自动化系 第2/3轮重复同一模板spider, 白耗时间)。
                # 但断供是暂时的: 消耗自由轮次重试, 网关恢复即获真实改进;
                # 自由轮次用尽才终止 — 保证 LLM 失败不偷走全部有效轮次。
                if free_left == 0:
                    print("     ⏹️ 无进展: 新代码与当前spider相同(LLM失败且模板无变化), 自由轮次用尽, 提前终止")
                    _tm_mark(f"R{rnd+1}.llm_done")  # 提前终止路径也收束 llm 段
                    break
                free_left -= 1
                print(f"     ⏳ 无进展(LLM失败/网关波动), 消耗自由轮次(剩{free_left}), 下轮重试 ...")
                _tm_mark(f"R{rnd+1}.llm_done")  # 无进展路径收束: [llm→llm_done]=改进挂起/失败段
                continue
            # 2026-09-07 用户指令(强化正确性): 写入前 ast.parse 语法门 —
            # 语法不过直接当崩溃处理回滚, 省一次子进程试运行(历史上 LLM 生成
            # 代码反复语法错误被回滚)
            try:
                import ast as _ast
                _ast.parse(new_code)
            except SyntaxError as _se:
                print(f"     💥 新spider语法错误({_se.msg[:50]}), 回滚")
                _tm_mark(f"R{rnd+1}.llm_done")
                continue
            spiderf.write_text(_sanitize_platform_urls(new_code, dept_url), encoding="utf-8")
            print(f"     ✅ 新spider {len(new_code)}B")
            # 立即试运行新spider并按质量对比(修正B: 净人数+脏名数, 非纯人数)
            try:
                r3, _sp3_out, _sp3_err = _run_spider(d, timeout=_spider_timeout(len(fac_urls)))
                for alt in d.glob("*teachers.csv"):
                    if alt.name != "teachers.csv":
                        alt.rename(d / "teachers.csv")
                _sync_meta(d, school, dept)
                if r3 is not None and r3.returncode != 0:
                    # 崩溃spider绝不可保留: 产物是旧CSV, 质量对比会误判相等
                    try:
                        _err3 = (_sp3_err.read_text(encoding="utf-8", errors="ignore") or "").strip()[-300:]
                    except Exception:
                        _err3 = ""
                    print(f"     💥 新spider崩溃(exit {r3.returncode}): {_err3[:80]}")
                    crash_hint = f"上次spider.py运行崩溃(exit {r3.returncode}): {_err3}"
                    print("     🔄 回滚: 新spider崩溃, 恢复旧spider")
                    spiderf.write_text(backup, encoding="utf-8")
                    with open(d / "teachers.csv", "w", encoding="utf-8", newline="") as f:
                        csv.writer(f).writerows(backup_teachers)
                    if backup_meta is not None:
                        (d / "spider_meta.json").write_text(backup_meta, encoding="utf-8")
                    if backup_json is not None:
                        (d / "teachers.json").write_text(backup_json, encoding="utf-8")
                    _tm_mark(f"R{rnd+1}.llm_done")  # 崩溃回滚路径收束 llm 段
                    continue
                new_rows = list(csv.reader(open(d / "teachers.csv", encoding="utf-8"))) if (d / "teachers.csv").exists() else []
                NAME_RE_ = re.compile(r"^[一-鿿·]{2,4}$")
                def _quality(rows):
                    clean, bad = 0, 0
                    for r in rows[1:]:
                        if len(r) < 3 or not r[2].strip():
                            continue
                        nm = _norm_name(r[2].strip())
                        # P0: 归一化(剥职称粘连) + LLM非名清单 + 导航词 三方一致
                        if NAME_RE_.match(nm) and nm not in NAV_WORDS and nm not in _bad_names:
                            clean += 1
                        else:
                            bad += 1
                    return clean, bad
                old_clean, old_bad = _quality(backup_teachers)
                new_clean, new_bad = _quality(new_rows)
                # 质量评分: 净姓名+2分/脏名-1分 — 净姓名大幅增加时应保留。
                # (旧逻辑 new_bad > old_bad 会把 净40/脏2 误判为差于 净0/脏1 而回滚,
                #  文产学院确定性模板40人因此被丢弃)
                old_score = old_clean * 2 - old_bad
                new_score = new_clean * 2 - new_bad
                clearly_worse = new_score < old_score - 1
                if clearly_worse:
                    print(f"     🔄 回滚: 新spider净{new_clean}/脏{new_bad} 差于 旧净{old_clean}/脏{old_bad}, 恢复旧spider")
                    spiderf.write_text(backup, encoding="utf-8")
                    with open(d / "teachers.csv", "w", encoding="utf-8", newline="") as f:
                        csv.writer(f).writerows(backup_teachers)
                    if backup_meta is not None:
                        (d / "spider_meta.json").write_text(backup_meta, encoding="utf-8")
                    if backup_json is not None:
                        (d / "teachers.json").write_text(backup_json, encoding="utf-8")
                else:
                    print(f"     ✓ 新spider净{new_clean}/脏{new_bad} (旧净{old_clean}/脏{old_bad})")
            except Exception:
                pass
        else:
            print(f"     ❌ 确定性兜底也失败")
        _tm_mark(f"R{rnd+1}.llm_done")  # 成功路径收束: [llm→llm_done]=改进+验证段; 无进展路径无此 mark, llm 段延续至下一 mark
    # 兜底(理论不可达: 末轮终态块已 return, 仅异常吞没时走到): 同口径终态分流
    # 2026-09-02 网关波动防护: 同上, 评估期网关不可用属瞬态 → RETRY。
    if any("网关不可用" in (r or "") for r in all_reasons):
        print("  ⚠️ 评估期网关不可用(人名终审未完成), 转 RETRY 不落终态")
        return "RETRY"
    # 2026-09-07 用户规则: URL缺失>30%复核后 ≤50% PASS / >50% FAIL 终态
    _urlg = _url_terminal_grade(d, _round_reasons)
    if _urlg == "PASS":
        print("  ✅ URL复核达标(缺失≤50%), PASS")
        return "PASS"
    _kind = _classify_terminal(fac_html, _round_reasons)
    if _urlg == "FAIL":
        _kind = "fail_extraction"
    try:
        json.dump({"status": _kind,
                   "reason": ("; ".join(all_reasons)[:500] if all_reasons else "未通过"),
                   "rounds": max_rounds,
                   "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    return "SKIP" if _kind == "no_data_skip" else "FAIL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("school")
    ap.add_argument("--dept")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="跳过已 PASS(有 .fix_done 标记)的院系")
    ap.add_argument("--queue", action="store_true", help="只处理 _fix_loop_queue.json 中的失败院系")
    ap.add_argument("--clear-queue", action="store_true", help="清空失败队列")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"院系级并发数(默认{WORKERS}, 环境变量 AGENT_WORKERS)")
    ap.add_argument("--allow-no-llm", action="store_true",
                    help="跳过LLM网关健康检查, 断供期强制运行(仅模板兜底, 慎用)")
    args = ap.parse_args()

    if args.clear_queue:
        clear_fail_queue()
        print("✅ 失败队列已清空")
        return

    # LLM 网关健康门控: 修复循环高度依赖 LLM 改进路径, 断供期跑批只是空转。
    # 短输出健康≠可用 — 网关有"长生成截杀波"(实测: 同样提示词, 健康期125s出
    # 2752字符, 截杀波134s返回空串), 必须用长输出探测: 要求~1KB响应验证。
    if not args.allow_no_llm:
        _llmh = get_llm()
        _ok = False
        if _llmh is not None:
            try:
                _r = _llmh.ask("只输出JSON。",
                               '写一个Python函数fib(n)求斐波那契, 加中文注释说明每行作用, '
                               '至少40行, 返回JSON: {"code": "..."}。')
                _j = parse_json(_r) if _r else None
                _ok = bool(_j) and len(str(_j.get("code") or "")) > 500
            except Exception:
                _ok = False
        if not _ok:
            print("⛔ LLM网关长生成不可用(截杀波/降级), 中止批次。网关恢复后重跑; 或 --allow-no-llm 强制(仅模板兜底)。")
            return

    school_dir = OUT / args.school
    dcsv = school_dir / "departments.csv"
    dept_urls = {}
    if dcsv.exists():
        dept_urls = {r["院系名称"]: r.get("院系URL", "").strip()
                     for r in csv.DictReader(open(dcsv, encoding="utf-8"))}

    results = []
    _MARK = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜", "LEGAL_EMPTY": "📋", "RETRY": "🔁"}

    def _report(name, status):
        print(f"  {_MARK.get(status, '?')} {name[:30]:30s} {status}")
        return (name, status)

    # ── 院系级并发: 各院系是独立文件域, 线程安全前提:
    #    LLM 调用已加 _LLM_SEM(2) 限速; 队列/知识库读改写已加锁;
    #    evaluate 的 llm_name_cache 自带锁; smart_fetch 的 DrissionPage 已加锁
    def _run_with_pool(tasks):
        nw = max(1, min(args.workers, len(tasks))) if tasks else 1
        with ThreadPoolExecutor(max_workers=nw) as pool:
            futs = [pool.submit(fn, *a) for fn, a in tasks]
            for fut in as_completed(futs):
                results.append(_report(*fut.result()))

    if args.queue:
        # ── 队列模式: 只处理历史失败院系, PASS 即移出队列 ──
        q = load_fail_queue()
        todo = list(q.get(args.school, {}).items())
        print(f"📋 队列模式: {args.school} 待处理 {len(todo)} 个失败院系, 并发 {args.workers}")

        def _run_queue(dept, info):
            d = school_dir / dept
            if not d.is_dir():
                remove_from_queue(args.school, dept)
                return (dept, "SKIP")
            if (d / ".wait_browser").exists():  # 2026-09-08 浏览器守卫挂起, 等窗口
                return (dept, "WAIT_BROWSER")
            url = info.get("url") or dept_urls.get(dept, "")
            try:
                status = fix_one(args.school, d, url)
            except Exception as e:
                import traceback
                print(f"  💥 {dept} 线程异常: {e}")
                traceback.print_exc()
                status = "RETRY"  # 瞬态异常: 保留队列重试, 不算终态
            if status in ("PASS", "LEGAL_EMPTY", "SKIP", "FAIL"):
                remove_from_queue(args.school, dept)  # 终态一律出队
            if status in ("PASS", "LEGAL_EMPTY"):
                (d / ".fix_done").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            elif status == "SKIP":
                (d / ".skip_mark").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 无数据", encoding="utf-8")
            elif status == "FAIL":
                (d / ".fail_mark").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 3轮提取失败", encoding="utf-8")
            return (dept, status)

        _run_with_pool([(_run_queue, (dept, info)) for dept, info in todo])
    else:
        # ── 全校遍历模式(默认) ──
        dept_dirs = [d for d in sorted(school_dir.iterdir())
                     if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")]
        if args.dept:
            dept_dirs = [d for d in dept_dirs if d.name == args.dept]
        dept_dirs = [d for d in dept_dirs
                     if dept_urls.get(d.name, "").startswith("http")]
        dept_dirs = [d for d in dept_dirs
                     if not (d / ".wait_browser").exists()]  # 2026-09-08 浏览器守卫挂起, 等窗口
        if args.resume:
            dept_dirs = [d for d in dept_dirs if not (d / ".fix_done").exists()]
        if args.limit:
            dept_dirs = dept_dirs[:args.limit]
        print(f"🔧 {args.school}: {len(dept_dirs)} 院系, 并发 {args.workers}")

        def _run_dept(d):
            url = dept_urls.get(d.name, "")
            try:
                status = fix_one(args.school, d, url)
            except Exception as e:
                import traceback
                print(f"  💥 {d.name} 线程异常: {e}")
                traceback.print_exc()
                status = "RETRY"  # 瞬态异常: 入队重试, 不算终态
            if status in ("PASS", "LEGAL_EMPTY"):
                (d / ".fix_done").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            elif status == "SKIP":
                # 确实没有数据(2026-08-31 用户指令): 标记skip终态, 不再重选
                (d / ".skip_mark").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 无数据", encoding="utf-8")
            elif status == "FAIL":
                # 3轮评估→改进未通过: 有数据但提取失败 — 标记fail终态,
                # 不再自动入队无限重试(2026-08-31 用户指令: 浪费token/时间)
                (d / ".fail_mark").write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 3轮提取失败", encoding="utf-8")
            elif status == "RETRY":
                add_to_queue(args.school, d.name, url)
            return (d.name, status)

        _run_with_pool([(_run_dept, (d,)) for d in dept_dirs])

    ok = sum(1 for _, s in results if s == "PASS")
    le = sum(1 for _, s in results if s == "LEGAL_EMPTY")
    print(f"\n{'='*56}")
    print(f"{args.school}: {len(results)}院系 | ✅PASS {ok} | ❌FAIL {sum(1 for _,s in results if s=='FAIL')} | ⬜SKIP {sum(1 for _,s in results if s=='SKIP')} | 📋合法空 {le}")


if __name__ == "__main__":
    main()
