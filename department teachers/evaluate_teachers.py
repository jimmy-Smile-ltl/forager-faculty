#!/usr/bin/env python3
"""
teachers.csv 质量评估器 — 评估→修复→重评 循环的第一环

对单个学校的每个院系评估:
  1. 不变量检查: 有 teachers.csv 就必须有 spider.py (teachers.csv 是 spider.py 的产物)
  2. 人名准确性: 名字必须是2-4字中文(或外国名字母形式), 不含导航词
  3. 数量合理性: 重抓师资页, 统计教师级元素数, CSV行数 < 页面元素的30% 判"明显偏少"
  4. URL覆盖: 详情URL数/总人数
  5. 重复名: 同院系重复教师

评级:
  PASS  — 通过
  FAIL  — 不通过, 携带原因(供 fix_loop.py 让LLM核查改进)
  SKIP  — 无法评估(页面不可达等)

用法:
  uv run python evaluate_teachers.py 苏州大学
  uv run python evaluate_teachers.py 苏州大学 --dept 计算机科学与技术学院

(2026-08-31 拆分重构: 页面对照计数/纯文本审查 → eval_elements.py, 小组织核实
 → eval_small.py; 本文件保留 LLM 终审缓存/CSV 读取/evaluate_dept/main 并 re-export。)
"""

import argparse, csv, json, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from _paths import ROOT, OUT  # 先导: sys.path 引导 + .env 加载

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

BASE = ROOT

from collect_teacher import (fetch, find_faculty_page, get_llm, parse_json,
                             _LLM_SEM, llm_ask_with_timeout)  # noqa: E402

# 评估器版本: 升级评估器(补盲区/改规则)后必须+1, 旧评估结论作废需重评
EVAL_VERSION = "3.0"

# ── 院系级并发: 线程池(默认4, --workers/AGENT_WORKERS 可调) ──
import os
import threading
WORKERS = int(os.environ.get("AGENT_WORKERS", "4"))
# _LLM_SEM 共享配额实例统一在 collect_teacher(全项目唯一信号量), 此处复用
_LLM_CACHE_LOCK = threading.Lock()  # LLM 终审缓存读写锁(并发防丢失更新)

try:
    from templates.spider_template import NAV_WORDS as _NAV
    NAV_WORDS = set(_NAV)
except Exception:
    NAV_WORDS = set()

# 页面对照计数/纯文本审查/小组织核实 — 拆出后从新模块 re-export(存量 import 兼容)
from eval_elements import (  # noqa: E402
    NAME_RE, FOREIGN_RE, count_teacher_elements, verify_no_detail_links,
)
from eval_small import verify_small_department, _small_verify_is_cat  # noqa: E402

# 专业方向/栏目词 + 名字粘连: 单源统一 — 与 util.teacher_extract 提取层同口径,
# 评估器与爬虫必须用同一规则集, 否则提取层放行/评估层误杀或相反(车某技术曾两处不一致)
try:
    from util.teacher_extract import SUBJECT_RE as SUBJECT_HINT
    from util.teacher_extract import GLUE_RE as GLUE_HINT
    from util.teacher_extract import NAV_SUFFIX_RE as NAV_SUFFIX_HINT
    from util.teacher_extract import REAL_NAME_PROTECT  # 2026-10-09 终审豁免
except Exception:
    SUBJECT_HINT = re.compile(
        r"(指挥|理论|作曲|音乐|舞蹈|美术|设计|摄影|表演|导演|编剧|钢琴|声乐|器乐|"
        r"管理|教育|研究|科学|工程|技术|学科|专业|方向|教学|实验|实践|基础|临床|"
        r"医学|药学|法学|经济|金融|会计|市场|新闻|传播|广告|文学|历史|哲学)$")
    GLUE_HINT = re.compile(
        r"^([一-龥·]{2,3})(技术|医学|科学|教学|研究|中心|学院|管理|工程|基地|平台|"
        r"培养|教育|服务|建设|职称|部门|办公室)$")
    NAV_SUFFIX_HINT = re.compile(r"(首页|主页|平台|知识库|系统|门户|概况|简介|新闻|通知|公告|动态|下载)$")

# ── LLM 人名终审结果缓存(降本: 同院系重跑不重复调用 LLM) ──
_LLM_CACHE_PATH = BASE / "logs" / ".llm_name_cache.json"


def load_llm_cache() -> dict:
    try:
        return json.load(open(_LLM_CACHE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_llm_cache(cache: dict):
    try:
        _LLM_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── CSV 读取 ──
def read_teachers_csv(csv_path):
    """读取CSV, 返回 (rows, name_idx, url_idx, title_idx) 或 None"""
    if not csv_path.exists() or csv_path.stat().st_size <= 50:
        return None
    try:
        rows = list(csv.reader(open(csv_path, encoding="utf-8")))
    except Exception:
        return None
    if len(rows) < 2:
        return rows, None, None, None
    hdr = rows[0]
    name_idx = url_idx = title_idx = None
    for i, h in enumerate(hdr):
        if "姓名" in h or h.strip().lower() == "name":
            name_idx = i
        if "详情" in h and "URL" in h:
            url_idx = i
        if "职称" in h or "职务" in h:
            title_idx = i
    return rows, name_idx, url_idx, title_idx


# ── 单院系评估(8维) ──
def evaluate_dept(school, dept_dir, dept_url=None):
    """评估单个院系, 返回 dict"""
    # 任务边界: 新学院重新均衡分配 token, 学院内连续调用固定同一 token (命中网关缓存省钱)
    # 并发提示: begin_task 只影响当前线程; main() 的院系级并发在线程池 worker 中执行本函数,
    # 各 worker 线程自行调用即可, SimpleLLM 线程隔离, 不会串 token, 无需加锁。
    _llm0 = get_llm()
    if _llm0 is not None:
        _llm0.begin_task(school)  # 学院亲和: 评估与采集同槽位token, 命中采集阶段prompt缓存(2026-08-24)
    result = {
        "school": school, "dept": dept_dir.name,
        "grade": None, "reasons": [], "total": 0, "url_ok": 0,
        "bad_names": [], "dup": 0, "spider_exists": False,
        "page_elements": None,
    }
    d = dept_dir
    csvf = d / "teachers.csv"
    spiderf = d / "spider.py"
    result["spider_exists"] = spiderf.exists() and spiderf.stat().st_size > 100

    # 合法空组织豁免: test_report.json 有特殊状态标记
    try:
        tr = json.load(open(d / "test_report.json", encoding="utf-8"))
        if tr.get("status") in ("no_teacher_org", "unreachable_or_no_faculty",
                                "no_faculty_page", "no_url"):
            # 注意: pending_recollect(批次中止待重采) 不是结论, 不得按合法空放行
            result["grade"] = "LEGAL_EMPTY"
            result["reasons"].append(f"合法空: {tr.get('reason', tr.get('status'))}")
            return result
        # 已验证的小型中心(人员少但已核实) → 跳过数量偏少检查
        if tr.get("status") == "verified_small_center" and tr.get("passed"):
            result["verified_small"] = True
    except Exception:
        pass

    read = read_teachers_csv(csvf)
    # pending_recollect 是过程状态: 数据不合格就 FAIL, 不得静默
    try:
        tr2 = json.load(open(d / "test_report.json", encoding="utf-8"))
        if tr2.get("status") == "pending_recollect":
            result["pending_recollect"] = True
    except Exception:
        pass
    if read is None:
        if not result["spider_exists"]:
            result["grade"] = "SKIP"
            result["reasons"].append("无CSV无spider(未采集)")
        else:
            result["grade"] = "FAIL"
            result["reasons"].append("有spider.py但未生成有效CSV")
        return result
    rows, name_idx, url_idx, title_idx = read
    if len(rows) < 2:
        result["grade"] = "FAIL"
        result["reasons"].append("CSV只有表头, 0个教师")
        return result

    # 1) 不变量: 有CSV必须有spider.py + analysis.md
    if not result["spider_exists"]:
        result["grade"] = "FAIL"
        result["reasons"].append("有teachers.csv但无spider.py(违反不变量: CSV必须是spider.py的产物)")

    # 1a) 产物溯源: spider_meta.json 记录 spider 自身hash, CSV 必须由当前 spider 版本产生
    try:
        metaf = d / "spider_meta.json"
        if metaf.exists():
            meta = json.load(open(metaf, encoding="utf-8"))
            want = meta.get("spider_sha256")
            if want:
                import hashlib
                cur = hashlib.sha256(spiderf.read_bytes()).hexdigest()
                if cur != want:
                    result["reasons"].append(
                        f"产物溯源失败: spider.py已变更(hash {cur[:8]}≠meta {want[:8]}), CSV非当前spider产物, 需重跑")
    except Exception:
        pass

    # 1b) analysis.md: 有数据CSV必须有网站结构分析
    analysisf = d / "analysis.md"
    if not (analysisf.exists() and analysisf.stat().st_size > 100):
        result["reasons"].append("缺失analysis.md(需先分析网页结构再设计spider)")

    # 1c) CSV/JSON 内容一致性
    try:
        jf = d / "teachers.json"
        if jf.exists():
            jdata = json.load(open(jf, encoding="utf-8"))
            j_teachers = jdata.get("teachers", [])
            csv_n = 0
            for r in rows[1:]:
                if name_idx is not None and name_idx < len(r) and r[name_idx].strip():
                    csv_n += 1
            if csv_n > 0 and not (isinstance(j_teachers, list) and j_teachers):
                result["reasons"].append("JSON缺teachers数组(CSV有数据但JSON无内容, 违反内容一致性)")
            elif isinstance(j_teachers, list) and j_teachers:
                j_n = len(j_teachers)
                if csv_n != j_n:
                    result["reasons"].append(f"CSV/JSON不一致: CSV {csv_n}人 vs JSON {j_n}人")
    except Exception:
        pass

    # 1d) 教师性证据: 数据来源页必须是带教师信号的页面(probe BFS 表记录
    #     详情/卡片/JS数组/名职对信号)。新闻/导航页提取的人名不是教师
    #     (海南国际学院教训: 首页新闻里的学生人名全过人名检查, 但非教师数据)
    try:
        af2 = d / "analysis.md"
        if af2.exists():
            _an = af2.read_text(encoding="utf-8")
            _sel_path = None
            _sm = re.search(r"\*\*选定师资页\*\*:\s*(\S+)", _an)
            if _sm:
                _sel_path = urlparse(_sm.group(1)).path.rstrip("/")
            _no_sig = set()
            for _line in _an.splitlines():
                _m = re.match(
                    r"\|\s*[^|]*\|\s*(\S+)\|\s*[^|]*\|\s*详情(\d+)\s+卡片(\d+)\s+JS数组(\d+)\s+名职对(\d+)",
                    _line)
                if _m and int(_m.group(2)) == 0 and int(_m.group(3)) == 0 \
                        and int(_m.group(4)) == 0 and int(_m.group(5)) == 0:
                    _no_sig.add(urlparse(_m.group(1)).path.rstrip("/"))
            if _no_sig:
                _pages = []
                try:
                    jf2 = d / "teachers.json"
                    if jf2.exists():
                        _jd = json.load(open(jf2, encoding="utf-8"))
                        for _t in _jd.get("teachers", []):
                            _p = ((_t.get("evidence") or {}).get("page") or "").strip()
                            if _p:
                                _pages.append(urlparse(_p).path.rstrip("/"))
                except Exception:
                    pass
                _bad_pages = [p for p in set(_pages)
                              if p and p in _no_sig and p != _sel_path]
                if _bad_pages:
                    result["reasons"].append(
                        f"教师性证据不足: {len(_bad_pages)}个来源页无教师信号"
                        f"(详情0卡片0JS数组0名职对0, 疑似新闻/导航页): "
                        f"{[p.split('/')[-1] for p in _bad_pages][:3]}")
    except Exception:
        pass

    # 1e) 表头契约校验(数据统一性的最后防线, 规范见 templates/contract.py)
    # 硬规则: Step 4 只认「教师姓名/姓名」与「个人详情URL/详情页URL」列,
    # 连这些列都识别不出(英文/异常表头) → 后续步骤必然丢数据, 判 FAIL
    if rows and len(rows) >= 1:
        if name_idx is None:
            result["reasons"].append(
                "CSV表头未识别姓名列(疑似英文/异常表头) — 需运行 normalize_teachers_output.py")
        if url_idx is None:
            result["reasons"].append(
                "CSV表头未识别详情URL列(Step 4 依赖) — 需运行 normalize_teachers_output.py")

    # 2) 人名准确性
    seen = set()
    names = []
    # URL结构信号: 教师详情URL特征(区别于机构/栏目页)
    DETAIL_HINT = re.compile(
        r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
        r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
        r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
        r"t\d{8}_\d+\.shtml|"  # 2026-09-23 武理工教工简介逐师详情
        r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
        r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
        r"/web\\.[^/]+/[a-z]{4,}/?|"
        r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/", re.I)  # 苏大 web.suda.edu.cn/拼音/ 个人主页
    # 机构/栏目页信号(注意: /$ 会匹配个人主页根目录, 必须 DETAIL 优先)
    ORG_HINT = re.compile(
        r"/jgjj/|/szdwjj|/gzzz|/list\.htm|/index\.htm$|"
        r"login|enroll|thesis|questionnaire|interview|oauth|passport|/$", re.I)
    for r in rows[1:]:
        if name_idx is None or name_idx >= len(r):
            continue
        name = r[name_idx].strip()
        if not name:
            continue
        names.append(name)
        result["total"] += 1
        # 名字净化后判断
        clean = re.sub(r"[（(].*?[）)]", "", name).strip()
        if not (NAME_RE.match(clean) or FOREIGN_RE.match(clean)):
            result["bad_names"].append(name)
        if clean in NAV_WORDS or name in NAV_WORDS:
            result["bad_names"].append(name)
        # 专业方向词(整词即词表词)不是教师名 — match 整词语义与
        # util.teacher_extract.clean_name 一致; search 会误杀组合词
        # ("学科建设" 命中 "建设$"), 曾造成评估器与爬虫规则不一致
        if SUBJECT_HINT.match(clean):
            result["bad_names"].append(name)
        # 名字粘连栏目词(车某技术/实验教学/药学管理)
        if GLUE_HINT.match(clean):
            result["bad_names"].append(name)
        if name in seen:
            result["dup"] += 1
        seen.add(name)
        url = r[url_idx].strip() if url_idx is not None and url_idx < len(r) else ""
        if url.startswith("http"):
            result["url_ok"] += 1
            # 详情URL是机构页(非教师详情) → 该行可疑(教师名可能是栏目名)
            # DETAIL优先: 命中详情特征=教师详情(即使同时含/或list)
            if DETAIL_HINT.search(url):
                pass
            elif ORG_HINT.search(url):
                result["org_urls"] = result.get("org_urls", 0) + 1

    bad_rate = len(result["bad_names"]) / result["total"] if result["total"] else 0
    dup_rate = result["dup"] / result["total"] if result["total"] else 0
    # 零容忍: 任何非人名混入即 FAIL (不再按比例放行)
    if result["bad_names"]:
        result["reasons"].append(f"非人名混入 {len(result['bad_names'])}个(零容忍): {result['bad_names'][:5]}")
    if dup_rate > 0.05:
        result["reasons"].append(f"重复名 {result['dup']}/{result['total']} ({dup_rate:.0%})")
    # 机构URL占比高 → 疑似把栏目当教师(如厦大学部的"党委人才/机构简介")
    org_rate = result.get("org_urls", 0) / result["total"] if result["total"] else 0
    if org_rate > 0.5:
        result["reasons"].append(
            f"机构页URL占比 {result.get('org_urls', 0)}/{result['total']} ({org_rate:.0%}): 疑似把栏目当教师")

    # 3a) 数量硬规则 → 严谨核实(2026-08-28): "怀疑漏采"不直接 FAIL。
    #     先三查——①翻页全走查 ②其他分类子页面 ③是否只采一种职称/类型;
    #     核实确为小组织才写 verified_small_center 标记 PASS, 核实不过保留 FAIL 并附证据。
    dept_type = ""
    try:
        dcsv = d.parent / "departments.csv"
        if dcsv.exists():
            def _norm(s):
                # 2026-09-01: 目录名与CSV 全/半角斜杠、空格宽度不一致
                # ("英语学院 ／ ..." vs "英语学院 / ...") → 归一化后比较
                return (s or "").replace("／", "/").replace("　", " ").strip()
            _names = []
            for dr in csv.DictReader(open(dcsv, encoding="utf-8")):
                _nm = _norm(dr.get("院系名称"))
                if _nm == _norm(d.name):
                    dept_type = dr.get("院系类型", "")
                    break
                if len(_nm) >= 4:
                    _names.append((_nm, dr.get("院系类型", "")))
            else:
                # 2026-09-01: 目录名被加装饰后缀(如"董浩云…（中美物流研究院）")时
                # 精确匹配失败 → 类型未知 → 学院被当非教学组织走 1 元素门槛
                # 误确认。前缀回退恢复类型。
                for _nm, _t in _names:
                    if _norm(d.name).startswith(_nm):
                        dept_type = _t
                        break
    except Exception:
        pass
    # 2026-08-30 用户规则: 学院/学部教师数 <20 必须复核(大学院系通常几十~几百人,
    # 少于20疑似漏采) — 严谨核实(翻页走查/分类子页/接口探测)确为小组织才PASS,
    # 核实不过保留 FAIL。原阈值 <5 按用户要求扩至 <20。
    # 2026-09-15 用户新规则: "反复确认后老师数量确实小于30, 算完成不算失败;
    # 复核结论一致后确认没有缺失后算通过" — 阈值 20→30, 核实确认后 verified_small
    # 落 PASS(不是跳过, 是确认无缺失即通过)
    if (result["total"] > 0 and result["total"] < 30
            and dept_type in ("学院", "学部", "系") and not result.get("verified_small")):
        _v = verify_small_department(dept_url, rows, dept_type, d=dept_dir)
        if _v["confirmed"]:
            result["verified_small"] = True
            result["verify_evidence"] = _v["evidence"]
            try:
                json.dump({"status": "verified_small_center", "passed": True,
                           "reason": f"严谨核实确为小组织: CSV {result['total']}人, {_v['evidence']}",
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
            except Exception:
                pass
        else:
            result["reasons"].append(
                f"数量硬规则: {dept_type}仅{result['total']}人; 严谨核实未通过({_v['evidence']})")

    # 3b) 数量合理性: 重抓师资页统计教师级元素(已核实小组织不再判"明显偏少")
    if dept_url and not result.get("verified_small"):
        try:
            home = fetch(dept_url)
            fac = find_faculty_page(dept_url, home) if home else None
            if fac:
                fhtml = fetch(fac)
                if fhtml and len(fhtml) > 500:
                    n, clickable = count_teacher_elements(fhtml, fac)
                    result["page_elements"] = n
                    result["page_clickable"] = clickable
                    if n >= 5 and result["total"] < n * 0.3:
                        result["reasons"].append(
                            f"数量明显偏少: 页面约{n}个教师级元素, CSV仅{result['total']}人")
        except Exception as e:
            pass

    # 4) URL覆盖
    if result["total"] >= 3:
        url_rate = result["url_ok"] / result["total"]
        clickable = result.get("page_clickable", 0) or 0
        # 兜底: 用CSV自带的师资页URL重抓统计(避免find_faculty_page未发现正确师资页)
        # 2026-09-23 云大建筑实锤: 名单在3篇导师表格文章里, find_faculty_page
        # 只找到列表页(jxtd.htm, 0元素0链接) → 缺失率82%误判FAIL。名单页是
        # 表格文本(无详情链接), 原兜底条件 url_rate==0 漏掉部分有URL的形态
        # (38人中7人有个人主页链接) → 改为 clickable<10 即重抓CSV师资页URL
        # (首行)统计: 表格文章 n2≥5且c2=0 → 纯文本豁免成立 → PASS
        if clickable < 10:
            try:
                page_u = None
                if len(rows) > 1 and len(rows[1]) > 6:
                    page_u = rows[1][6].strip()  # 第7列=师资页URL
                if page_u and page_u.startswith("http"):
                    phtml = fetch(page_u)
                    if phtml and len(phtml) > 500:
                        n2, c2 = count_teacher_elements(phtml, page_u)
                        clickable = c2
                        result["page_elements"] = n2
            except Exception:
                pass
        # 漏抓判定: 页面有可点击教师详情链接但CSV的URL=0
        if url_rate == 0 and clickable >= 10:
            result["reasons"].append(
                f"详情URL全空但页面有{clickable}个可点击教师链接(漏抓)")
        # 2026-08-30 用户规则(最终版): 缺失率<10%通过; 10-30%复核(清单);
        # >30%复核重试(FAIL重采)。纯文本页=100%缺失必然落入>30%重试,
        # 严格出口在 fix_loop 的 no_url_confirmed 审查(多次重扫+全形态扫描
        # +数量校验确认无漏采才认可, 纯文本是罕见例外)。
        missing_rate = 1 - url_rate
        # 2026-09-22 川大华西药学院实锤: 纯文本名单页(xsdtr.htm 20人无详情
        # 链接, 页面形态如此)被缺失率40%判FAIL — 无链接≠漏抓。教师元素达标
        # 且可点击链接稀少=纯文本名单, 豁免缺失率判定(严格出口在 fix_loop
        # no_url 纯文本审查: 全形态扫描+数量校验)。
        # 2026-09-22 新疆化工实锤补丁: 叙述型名单页(57人全无链接)元素计数
        # 为0(计数只认带链接元素) — 豁免条件扩展: CSV≥10人且url全空且
        # 页面可点击<10 即纯文本形态(漏抓的判别是页面有链接没抓到,
        # 页面本身没链接=形态如此)
        # 2026-09-23 央财政府管理实锤: 平铺块状名单页(57人=12个带名详情链接
        # +45个纯文本姓名块, 页面形态如此)被缺失率79%判FAIL — 页面带名详情
        # 链接已全部采到(CSV的URL数≥页面可点击数)即无漏抓, 其余为文本块形态。
        _elements = result.get("page_elements", 0) or 0
        _links_all_captured = (clickable >= 1 and result["url_ok"] >= clickable
                               and result["total"] >= 10)
        _pure_text = ((_elements >= 5 and clickable < 10)
                      or (clickable < 10 and result["total"] >= 10
                          and url_rate == 0)
                      or _links_all_captured)
        if missing_rate > 0.30 and not _pure_text:
            result["reasons"].append(
                f"详情URL缺失率{missing_rate:.0%}>30%({result['url_ok']}/{result['total']}), 需复核重采")
        elif missing_rate > 0.10 and not _pure_text:
            try:
                with open("logs/url_review_10_30.tsv", "a", encoding="utf-8") as _f:
                    _f.write(f"{school}\t{d.name}\t{missing_rate:.3f}\t{result['url_ok']}/{result['total']}\n")
            except Exception:
                pass

    # 5) LLM 人名终审(零容忍): 只审规则未判 FAIL 的姓名(规则已FAIL则跳过, 省一次调用)
    #    结果缓存到 logs/.llm_name_cache.json —— fix_loop 多轮重评时第 2 轮起命中缓存
    try:
        # 规则已判出非人名 → FAIL 已是定局, 终审不再影响判定 → 跳过(降本)
        if result["total"] >= 3 and not result["bad_names"]:
            llm = get_llm()
            if llm:
                # 证据: 从 teachers.json 取每条 (name, anchor原文) — 判断切分正确性而非孤词
                anchors = {}
                urls = {}
                try:
                    jd = json.load(open(d / "teachers.json", encoding="utf-8"))
                    for t in jd.get("teachers", []):
                        if isinstance(t, dict) and t.get("name"):
                            if t.get("evidence"):
                                anchors[t["name"]] = t["evidence"].get("anchor", "")
                            _tu = t.get("url") or t.get("detail_url") or ""
                            if _tu:
                                urls[t["name"]] = _tu
                except Exception:
                    pass
                # 2026-09-18 重大电气实锤: 真名「向学位」(职称:副研究员)被 LLM
                # 终审误判非人名(向学位读似"面向学位") — 个人详情页拼音 URL
                # (/jslb/xxw.htm 姓名拼音首字母) 是强人名证据, 此类姓名豁免
                # LLM 非人名判定(页面有学历/职称卡片上下文, 孤词误判风险高)
                _PERSONAL_URL = re.compile(r"/jslb/[a-z_]{2,12}\.htm")
                _person_evid = {n for n, u in urls.items() if _PERSONAL_URL.search(u)}
                with _LLM_CACHE_LOCK:
                    cache = load_llm_cache()
                ckey = f"{school}/{d.name}"
                verdict = cache.get(ckey, {})  # {姓名: True=非人名}
                # 待审: 规则 OK 的姓名中, 缓存未命中部分
                # 2026-10-09: 真名保护名单豁免终审(陈鹏/吴静等常见名孤词被
                # 零容忍误杀整院实锤) — 保护名单不进待审、不采信历史缓存误判
                suspect = [n for n in names if n not in result["bad_names"]]
                todo = [n for n in suspect if n not in verdict
                        and n not in REAL_NAME_PROTECT]
                # 只统计当前名单内被缓存的非人名(名单外旧缓存不得复报)
                non_names = [n for n in verdict if verdict[n] and n in names
                             and n not in _person_evid and n not in REAL_NAME_PROTECT]
                batches = [todo[i:i + 60] for i in range(0, len(todo), 60)]
                llm_verify_failed = 0  # 网关降级时未验证的姓名数(fail-safe: 未验证≠通过)
                done_names = set()     # 本轮已验证的姓名(坏响应不缓存)
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
                for bi, batch in enumerate(batches):
                    items = []
                    for n in batch:
                        a = anchors.get(n, "")
                        if a and a != n:
                            items.append(f'"{n}" (页面原文: "{a}")')
                        else:
                            items.append(f'"{n}"')
                    prompt = f"""你是中文人名识别专家。下面是从"{school} {d.name}"网页提取的教师姓名(第{bi+1}/{len(batches)}批, {len(batch)}个)。带"(页面原文: ...)"的条目请核对姓名是否从原文中切分正确; 不带原文的按人名常识判断。

{"; ".join(items)}

判断每个是不是真实教师姓名。非人名包括: 栏目名(首页/学院概况/联系我们)、专业方向名(合唱指挥/音乐理论)、机构名、被截断的词(如"胡文平中"、"陈招斌高"——原文"陈招斌 高级工程师"截取前4字)。

返回JSON:
{{"non_names": ["不是人名的词"]}}

只输出JSON。"""
                    r = llm_ask_with_timeout("你是人名识别专家。只输出JSON。", prompt,
                                              timeout=90, tag="verify")
                    llm_result = parse_json(r) if r else None
                    # 网关降级(401/空响应) → 不得静默放行: 未验证的姓名必须计数并判FAIL,
                    # 且绝不把"全部通过"的假 verdict 写入缓存(否则永久固化掩蔽)
                    if (llm_result and isinstance(llm_result.get("non_names"), list)
                            and not llm_result.get("degraded") and not llm_result.get("error")):
                        for n in batch:
                            verdict[n] = n in llm_result.get("non_names", [])
                        non_names += [n for n in llm_result.get("non_names", [])
                                      if isinstance(n, str) and n not in _person_evid
                                      and n not in REAL_NAME_PROTECT]
                        done_names.update(batch)
                    else:
                        llm_verify_failed += len(batch)
                if todo:
                    with _LLM_CACHE_LOCK:
                        # 只缓存已验证过的姓名
                        cache[ckey] = {n: v for n, v in verdict.items() if n in done_names}
                        save_llm_cache(cache)  # 增量落盘, 中断也不丢
                if non_names:
                    result["reasons"].append(
                        f"LLM人名终审: {len(non_names)}个非人名(零容忍): {non_names[:5]}")
                    result["llm_non_names"] = non_names
                if llm_verify_failed:
                    # fail-safe: 网关降级导致姓名未验证 — 不得按通过放行
                    result["reasons"].append(
                        f"LLM人名终审未完成: {llm_verify_failed}个姓名未验证(网关不可用), 零容忍检查无法确认")
                    result["llm_verify_failed"] = llm_verify_failed
    except Exception:
        pass

    # ── 判定 ──
    reasons = [x for x in result["reasons"] if x]
    if not reasons and result["total"] < 3 and not result.get("verified_small"):
        # 小组织豁免(2026-09-01 严格化, 用户反馈: 假阳性, 提高门槛): 不再看
        # page_elements 直接豁免 — 电院(JS驱动页)与碳中和发展(0元素壳页)被
        # "元素<5 即确认"误放行; 改为走严谨核实(翻页走查/分类子页/前端API/
        # XHR接口), 核实确为小组织才 PASS, 否则 FAIL(可能是网络问题/漏采)。
        _v = verify_small_department(dept_url, rows, dept_type, d=dept_dir)
        if _v["confirmed"]:
            result["verified_small"] = True
            result["verify_evidence"] = _v["evidence"]
            try:
                json.dump({"status": "verified_small_center", "passed": True,
                           "reason": f"严谨核实确为小组织: CSV {result['total']}人, {_v['evidence']}",
                           "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          open(d / "test_report.json", "w"), ensure_ascii=False, indent=2)
            except Exception:
                pass
        else:
            reasons.append(f"教师数<3 ({result['total']}); 严谨核实未通过({_v['evidence']})")
    if reasons:
        result["grade"] = "FAIL"
    else:
        result["grade"] = "PASS"
    result["reasons"] = reasons
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("school")
    ap.add_argument("--dept", help="只评估单个院系")
    ap.add_argument("--json", action="store_true", help="输出EVALUATION_LOOP.json")
    ap.add_argument("--workers", type=int, default=WORKERS, help="院系级并发数(默认4)")
    args = ap.parse_args()

    school_dir = OUT / args.school
    if not school_dir.exists():
        print(f"❌ 学校不存在: {args.school}")
        return
    dcsv = school_dir / "departments.csv"
    dept_urls = {}
    if dcsv.exists():
        dept_urls = {r["院系名称"]: r.get("院系URL", "").strip()
                     for r in csv.DictReader(open(dcsv, encoding="utf-8"))}

    dept_dirs = [d for d in sorted(school_dir.iterdir())
                 if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")]
    if args.dept:
        dept_dirs = [d for d in dept_dirs if d.name == args.dept]

    results = []
    # 院系级并发: 每个院系是独立文件域, 线程安全(LLM 已加限速+缓存锁)
    from concurrent.futures import ThreadPoolExecutor
    if len(dept_dirs) > 1 and args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(evaluate_dept, args.school, d, dept_urls.get(d.name, "")): d
                    for d in dept_dirs}
            for fut in futs:
                r = fut.result()
                results.append(r)
                mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜", "LEGAL_EMPTY": "📋"}.get(r["grade"], "?")
                print(f"{mark} {r['dept'][:30]:30s} {r['total']:4d}人 {'; '.join(r['reasons'][:2])[:70]}")
    else:
        for d in dept_dirs:
            url = dept_urls.get(d.name, "")
            r = evaluate_dept(args.school, d, url)
            results.append(r)
            mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜", "LEGAL_EMPTY": "📋"}.get(r["grade"], "?")
            print(f"{mark} {r['dept'][:30]:30s} {r['total']:4d}人 {'; '.join(r['reasons'][:2])[:70]}")

    stats = Counter(r["grade"] for r in results)
    print(f"\n{'='*60}")
    print(f"{args.school}: 共{len(results)}院系 | ✅PASS {stats['PASS']} | ❌FAIL {stats['FAIL']} | ⬜SKIP {stats['SKIP']} | 📋合法空 {stats['LEGAL_EMPTY']}")

    if args.json:
        out = {
            "school": args.school,
            "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_version": EVAL_VERSION,
            "results": results,
        }
        (school_dir / "EVALUATION_LOOP.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📄 {school_dir/'EVALUATION_LOOP.json'}")


if __name__ == "__main__":
    main()
