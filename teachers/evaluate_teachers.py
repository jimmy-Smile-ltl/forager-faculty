#!/usr/bin/env python3
"""教师详情采集质量门禁（2026-08-27 循环测试方法论工具化）。

扫描 teachers_detail.json, 自动产出质量报告:
  - 来源分布 (html/pdf/fetch_failed/...)
  - 字段覆盖率 (每字段非空百分比)
  - 污染残留检测: tab 标签词链 / 邮箱防爬 / SPA shell（修复是否生效的量化验证）
  - 疑似漏提检测（发现58）: 本地文件页面文本有字段节线索但字段空
  - 失败清单 + skipped_urls.json 拦截名单（上游修复后重扫依据）

用法:
  uv run python teachers/evaluate_teachers.py --school 武汉大学 --dept 计算机学院
  uv run python teachers/evaluate_teachers.py --school 武汉大学
  uv run python teachers/evaluate_teachers.py --all

报告输出: 院系目录下 evaluation_report.md / evaluation_summary.json
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_COLLEGE_ROOT = Path(__file__).resolve().parent.parent
_THIS_DIR = _COLLEGE_ROOT / "teachers"
_RAW_ROOT = _COLLEGE_ROOT / "tearchers raw data"
sys.path.insert(0, str(_THIS_DIR))

DETAIL_FIELDS = [
    "联系方式", "通讯地址", "职称", "职务", "出生年月",
    "教育经历", "工作经历", "研究方向", "学术成果",
    "基金", "荣誉", "课堂项目", "讲授课程",
]
SUCCESS_TYPES = {"html", "html_fallback", "html_noise", "pdf", "pdf_noise", "html_shell_pdf"}
FAIL_TYPES = {"fetch_failed", "no_url", "error"}
# 字段错位交叉校验（发现22 评估侧）: LLM 归因错位时, 页面结构标签词
# 出现在非所属字段——如教育经历里混入"工作经历"/"讲授课程"/"Email"
# 标签（LLM 整段倾倒, 发现28）。
# 发现28 修正: 只用"结构标签词"做交叉, 不用值词——"本科/硕士/博士"
# 是课程/经历描述的正文内容（"本科生课程""博士研究生"）, 用在交叉
# 校验必然误报（安大哲学学院 9 人全是"研究生《xx》"课程误报）。
# 标签词（工作经历/Email/最高学历…）出现在错误字段才是真错位/倾倒。
# 发现37 修正: 教育经历 不再用 "研究方向" 词做交叉——叙述型简历里
# "获理学硕士学位, 研究方向: 概率论与数理统计" 是教经叙述内的子描述,
# 误报率高（安大材料 12 行纯叙述）。改为两类更精确的信号:
#   1) 其他节标签（学术成果/开授课程/荣誉奖励/工作经历…）= 整段 CV 倾倒;
#   2) "研究方向" 编号列表（"研究方向：1.xxx"）或无教育语境的裸标签
#      （"副理事长 研究方向 软件建模"）= 研究方向独立倾倒。
# 邮箱用正则而非裸 "@"——"Computer Engineering @ NTU" 的 @ 是学位项目
# 名的一部分（武大陈丹误报），真邮箱才有域后缀。
MISALIGN_RULES = [
    ("讲授课程", ("最高学历", "毕业学校", "Email", "出生", "工作经历")),
    # 发现42: "最高学位" 从教经线索移除——表单拆分（_split_form_pairs）
    # 已把 "学历：研究生 最高学位：博士 电话：…" 表单串清成纯学历对
    # （车翔玖）, 此时 "最高学位：博士" 是合法紧凑值, 再判倾倒即误报
    ("教育经历", ("工作经历", "讲授课程", "Email", "邮箱", "联系方式",
                 "学术成果", "开授课程", "荣誉奖励", "获奖情况", "所在学科")),
    ("研究方向", ("Email", "电话", "联系方式", "工作经历")),
    ("联系方式", ("研究方向", "毕业学校", "工作经历")),
    ("职称", ("工作经历", "毕业学校", "Email")),
]
# 发现37: 教育经历 中 "研究方向" 的独立倾倒形态（走 MISALIGN_RULES 之外的
# 专属判定）。教育语境标记=叙述型简历（放行裸标签）; 编号列表=倾倒铁证。
_EDU_CTX_RE = re.compile(r"毕业|学位|博士|硕士|学士|本科|研究生|大学|学院|学校|访学|\d{4}")
_EDU_ZC_NUM = re.compile(r"研究方向[：: ]\s*[1-9]")
_EDU_ZC_BARE = re.compile(r"研究方向\s+\S")
_EDU_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w+")
# 发现58: 疑似漏提检测字段（长内容字段, 空值但有页面线索 = 漏提）。
# 联系方式/职称 噪声高（页脚院系邮箱/正文他人职称）不进此桶。
_LEAK_FIELDS = ("教育经历", "工作经历", "学术成果", "基金",
                "课堂项目", "讲授课程", "研究方向")
# 发现58: 职称线索词表（页含这些词才算"职称可判页"）——与
# detail_scraper FIELD_SPEC 职称 values 同口径。简历/简介型页面不写
# 职称, 全系覆盖率被它们拉低是口径失真（湘潭文新 30 人空职称实测
# 页面 0 职称词）, 闭环判定按"可判页"算提取率
_TITLE_CLUE_RE = re.compile(
    "|".join(re.escape(w) for w in (
        "教授", "副教授", "研究员", "副研究员", "助理研究员", "讲师",
        "助教", "院士", "高级工程师", "工程师", "高级实验师", "实验师",
        "实验员", "教授级高级工程师", "教授级高级实验师")))
# 发现58: 研究方向线索词（可判页口径, 同职称）——东北师大历史学院
# 44 人空方向实测页面无方向词, 全系 50% 线是页面天花板不是漏提。
# 注意: 裸"主要研究"会命中"主要研究成果"（成果节标题, 西北大学马院
# 42 例假线索实测）, 只能取"主要研究方向"完整形态
_DIR_CLUE_RE = re.compile(r"主要研究方向|研究方向|研究领域|科研方向|研究兴趣")


def _is_menu_chain(v: str) -> bool:
    """头部空格/换行分隔的无句读短短语链 ≥6 段 = 导航菜单链。

    南林机电"教学成果\n国际交流项目\n选课指南…"与合工大数学"学科建设
    学科简介 双一流建设…"菜单实测——菜单首项恰是节标签词, 启发式从
    菜单起切, 后续真实正文让全值词密度稀释（只判头部 10 词）。真实
    字段值几乎都含句读/数字/年份/英文（"主持教改项目…（2022）"、
    "机器学习、深度学习"用顿号）, 纯 CJK 短语头是菜单强信号。"""
    tokens = [t for t in re.split(r"\s+", v) if t]
    head = tokens[:10]
    if len(head) < 6:
        return False
    short = sum(1 for t in head
                if len(t) <= 12
                and not re.search(r"[。；;，,：:.、（）()《》【】\dA-Za-z]", t))
    return short >= len(head) * 0.85


def _dir_has_content(text: str) -> bool:
    """方向线索页 = 方向标签后跟实际内容。

    "研究领域与方向: 无"（贵大计算机 77 例模板页实测）是页面自述
    无方向, 不算线索; "无监督学习/机器学习" 的"无"后跟非句读字符
    是真实内容, 照算。标签后紧跟另一结构标签（"研究方向\n发表论文\n
    科研项目" tab 头序列, 上交系统生物医学研究院实测——内容 JS
    加载）同样不算线索。
    """
    from detail_scraper import _SECTION_END_LABELS
    _next_label_re = re.compile(
        "|".join(re.escape(l) for l in
                 sorted(_SECTION_END_LABELS, key=len, reverse=True)))
    for m in _DIR_CLUE_RE.finditer(text):
        tail = text[m.end():m.end() + 30]
        # 剥离复合形态尾（"研究领域"命中在"研究领域与方向"内, tail 从
        # "与方向: 无"起——先剥"与方向"+冒号再判内容）
        tail = re.sub(r"^(?:[与和及或]方向)?[：:\s]*", "", tail)
        # "无"后跟空白/句读/行尾（下一节标题紧跟其后也算）
        # = 页面自述无方向; "无监督学习"的"无"后紧跟内容字, 不算
        if re.match(r"^(?:无|暂无|空缺|空)(?:[\s。；;）)]|$)", tail):
            continue
        # 紧跟另一结构标签 = 空 tab
        if _next_label_re.match(tail):
            continue
        # 翻页词（"上一篇/下一篇"）后无内容 = 空 tab（大连海事公管实测）
        if re.match(r"^(?:上一篇|下一篇|下一篇：|上一篇：)", tail):
            continue
        return True
    return False


def _misalign_hits(t: dict) -> list[tuple[str, str]]:
    """字段错位交叉校验（发现22/28/37）→ [(错位字段, 值片段)]。

    教育经历 的特殊性见 MISALIGN_RULES 注释: "研究方向" 是叙述内子描述,
    只有当它是编号列表 / 无教育语境的裸标签时才判倾倒。
    """
    out = []
    for f, clues in MISALIGN_RULES:
        v = t.get(f) or ""
        if not v:
            continue
        if any(c in v for c in clues):
            out.append((f, str(v)[:40]))
        elif f == "教育经历" and (
            _EDU_EMAIL_RE.search(v)
            or _EDU_ZC_NUM.search(v)
            or (_EDU_ZC_BARE.search(v) and not _EDU_CTX_RE.search(v))
        ):
            out.append((f, str(v)[:40]))
    return out


def load_detail_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def analyze_dept(detail_path: Path, skipped_path: Path | None) -> dict:
    data = load_detail_json(detail_path)
    ts = data.get("teachers", []) if isinstance(data, dict) else data
    total = len(ts)

    # 污染残留检测（复用 detail_scraper 的判定函数, 与采集侧同一套口径）
    from detail_scraper import (_is_tab_label_chain, _EMAIL_SCRAMBLE_RE,
                                _is_nav_noise_value, _is_nav_frag_value,
                                _is_junk_name, _is_table_header_chain,
                                _strip_self_section_label, _unmix_tail_segments,
                                _is_page_dump_value, _is_tiny_fragment)
    tab_polluted = []
    email_scrambled = []
    nav_polluted = []
    table_polluted = []
    self_label_prefixed = []
    tail_leak = []
    tiny_frag = []
    birth_bad = []
    misaligned = []
    for t in ts:
        polluted = False
        # 名单级导航词姓名污染（发现39: 上财"专题学习/教工之家"、云大
        # "教学建设"、安大"研究生院"——导航菜单项整词作教师姓名, URL 指向
        # list 页）。复用采集侧 _is_junk_name, 与 process_teacher 判死同口径。
        name_junk = _is_junk_name(str(t.get("姓名") or ""))
        if name_junk:
            nav_polluted.append((t.get("姓名"), "姓名", "名单导航词"))
            polluted = True
        for f in DETAIL_FIELDS:
            v = t.get(f)
            if v and _is_tab_label_chain(str(v)):
                tab_polluted.append((t.get("姓名"), f, str(v)[:40]))
                polluted = True
                break
        contact = t.get("联系方式") or ""
        if _EMAIL_SCRAMBLE_RE.search(str(contact)) or "请手工替换符号" in str(contact):
            email_scrambled.append((t.get("姓名"), str(contact)[:50]))
            polluted = True
        # 导航词链污染（发现21: 头部/页脚导航词混入字段值, 词表打补丁
        # 赶不上新站点, 评估侧必须检测——否则污染靠人工发现）。
        # 发现38: 导航链接文本碎片（"名师"/"获得者"/"成果" 单/双字值）
        # 同样入导航污染桶。姓名已判垃圾则不再查字段值（同源一行只记一条）。
        if not name_junk:
            for f in DETAIL_FIELDS:
                v = t.get(f)
                if v and (_is_nav_noise_value(str(v))
                          or _is_page_dump_value(str(v))
                          or _is_nav_frag_value(v, f)):
                    nav_polluted.append((t.get("姓名"), f, str(v)[:40]))
                    polluted = True
                    break
        # 表格列头链污染（发现26: 表格型教师主页被 LLM 整表拍平归入字段）
        for f in DETAIL_FIELDS:
            v = t.get(f)
            if v and _is_table_header_chain(str(v)):
                table_polluted.append((t.get("姓名"), f, str(v)[:40]))
                polluted = True
                break
        # 节标签前缀未剥离（发现40: 值带自身节标签前缀, "工作经历： 2004
        # 年…"。与采集侧 _strip_self_section_label 同口径——历史启发式
        # 切块起点=标签首字符所致; 修复后此桶应趋零, 作回归守卫）
        # 比较口径用 strip 后文本——函数内部 .strip() 会清尾部空白,
        # 与原始值直比较会产生尾部空格误报（发现41 王军基金实测）
        for f in DETAIL_FIELDS:
            v = t.get(f)
            if v and _strip_self_section_label(str(v), f) != str(v).strip():
                self_label_prefixed.append((t.get("姓名"), f, str(v)[:40]))
                break
        # 尾漏残留（发现41: 值尾部挂着兄弟节标签段。与采集侧
        # _unmix_tail_segments 同口径, 数据修整后应趋零, 作回归守卫）
        for f in DETAIL_FIELDS:
            v = t.get(f)
            if v:
                _, moves = _unmix_tail_segments(str(v), f)
                if moves:
                    tail_leak.append((t.get("姓名"), f, str(v)[:40]))
                    break
        # 短碎片（发现43: 荣誉/基金/学术成果等 2-3 字碎片值）与
        # 出生年月无年份（"…，安徽望江人" 籍贯片）——数据修整后应趋零
        for f in DETAIL_FIELDS:
            v = t.get(f)
            if not v:
                continue
            if _is_tiny_fragment(str(v), f):
                tiny_frag.append((t.get("姓名"), f, str(v)[:30]))
                break
            if f == "出生年月" and not re.search(r"(19|20)\d{2}", str(v)):
                birth_bad.append((t.get("姓名"), f, str(v)[:30]))
                break
        # 字段错位交叉校验（发现22: LLM 归因错位, 如讲授课程=最高学历）。
        # 整行已被判定污染（导航/表格/邮箱乱码）的不再重复计入——污染行
        # 里的"错位"是同一污染的伴生症状, 同源问题不重复计数（上财导航行）。
        if not polluted:
            for f, sv in _misalign_hits(t):
                misaligned.append((t.get("姓名"), f, sv))
                break

    # ── 发现58 (2026-09-07): 疑似漏提检测 ──
    # 本地文件页面文本存在字段节线索但字段为空 = 提取丢失（某教师类）。
    # 复用采集侧同口径: 启发式能从页面文本切出内容（≥20 字且非导航噪声）
    # 而字段空 = 漏提。同时对含名率做统计——正文文本不含教师姓名的文件
    # 是 JS 壳垃圾文件（湖南大学家族: 447KB 文件正文 178 字, 含名率≈0）。
    # 漏提与垃圾文件是两类问题: 前者重提取可救, 后者上游 URL/渲染才能救。
    from detail_scraper import (DetailScraper, _strip_empty_section_tags,
                                _is_nav_noise_value, _is_tiny_fragment)
    school = detail_path.parent.parent.name
    dept = detail_path.parent.name
    _scraper = DetailScraper(school, dept, str(_RAW_ROOT), fetch_only=True)
    leak = []
    name_files = 0
    checked_files = 0
    title_clue_files = 0
    title_filled_files = 0
    dir_clue_files = 0
    dir_filled_files = 0
    # 2026-10-24 名字-页面校验: 页面 <title> 不含教师姓名 = 名字错或下错页
    # (吴晓球教类新闻页/名字粘连/列表页误下——≥4字名字全量QC实锤 692 例)
    name_mismatch = []
    for t in ts:
        # 发现58: 漏提/可判页检查只看 html/html_fallback——html_noise
        # 是下载阶段已判定的导航噪声页, 启发式会从菜单项切出假内容
        # （宁大力学 107 行 noise 页, "学术成果"菜单链进漏提桶 110 例
        # 假阳性实测）。噪声页提取天然不可靠, 不参与三项口径。
        if t.get("来源类型") not in ("html", "html_fallback"):
            continue
        name = t.get("姓名") or ""
        url = t.get("详情页URL") or ""
        prefix = DetailScraper._make_filename(name, url, "html")[:-5]
        fs = list((_RAW_ROOT / school / dept).glob(f"{prefix}*.html"))
        if not fs:
            continue
        # 病态大文件（北交大 12-18MB 单页, 解析 20s+/个）跳过漏提检查
        if fs[0].stat().st_size > 2_000_000:
            continue
        try:
            html = fs[0].read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        text = DetailScraper.extract_text_from_html(html)
        checked_files += 1
        # 2026-10-24 名字-页面校验: 页面 title 不含姓名(去空格比较)
        # = 名字错误或下错页——进 name_mismatch 桶
        if name:
            m_ttl = re.search(r"<title>([^<]*)</title>", html, re.S)
            page_ttl = (m_ttl.group(1) if m_ttl else "").replace(" ", "")
            if page_ttl and name.replace(" ", "") not in page_ttl:
                name_mismatch.append((name, page_ttl.strip()[:40]))
        # 职称可判页统计（发现58）: 页面文本含职称词 = 可判页,
        # 其中存储职称非空 = 已提取。短文本页也统计（59 字含"教授"
        # 同样是线索）
        if _TITLE_CLUE_RE.search(text):
            title_clue_files += 1
            if t.get("职称"):
                title_filled_files += 1
        if _dir_has_content(text):
            dir_clue_files += 1
            if t.get("研究方向"):
                dir_filled_files += 1
        if not text or len(text) < 60:
            # JS 壳/空页: 计入含名率统计（湖南大学壳家族的信号就是
            # 正文≈0）, 但不做漏提检查（无正文可漏）
            if name and name in text:
                name_files += 1
            continue
        if name and name in text:
            name_files += 1
        if (all(t.get(f) for f in _LEAK_FIELDS) and t.get("出生年月")):
            continue  # 关键字段全有, 无漏提嫌疑
        heu = _scraper._extract_fields_heuristic(_strip_empty_section_tags(text))
        clue = None
        for f in _LEAK_FIELDS:
            if t.get(f):
                continue
            v = str(heu.get(f) or "").strip()
            # 发现58: 导航标记词显式排除——北科大教育研究院 106 例
            # 假漏提实测（"中文 导航栏目 学院概况…教师信息 主页banner"
            # 254 字菜单, _is_nav_noise_value 因词密度稀释漏判）+ 菜单链
            # 检测（南林机电"教学成果\n国际交流项目\n选课指南…"菜单,
            # 首项恰是节标签词）。漏提桶宁缺毋滥, 含菜单信号即不算线索
            if len(v) >= 20 and not _is_nav_noise_value(v) \
                    and not _is_tiny_fragment(v, f) \
                    and not _is_menu_chain(v) \
                    and "导航" not in v and "栏目" not in v \
                    and "banner" not in v.lower() and "版权所有" not in v:
                clue = (f, v[:40])
                break
        if not clue and not t.get("出生年月"):
            m = re.search(
                r"(?:19|20)\d{2}\s*年\s*(?:\d{1,2}\s*月\s*)?(?:出生|生)",
                text[:2000])
            if m:
                clue = ("出生年月", m.group(0))
        if clue:
            leak.append((name, clue[0], clue[1]))
    junk_files = {
        "checked": checked_files,
        "with_name": name_files,
        "rate": round(name_files / checked_files * 100, 1) if checked_files else None,
    }
    title_rate = {
        "clue": title_clue_files,
        "filled": title_filled_files,
        "rate": (round(title_filled_files / title_clue_files * 100, 1)
                 if title_clue_files else None),
    }
    dir_rate = {
        "clue": dir_clue_files,
        "filled": dir_filled_files,
        "rate": (round(dir_filled_files / dir_clue_files * 100, 1)
                 if dir_clue_files else None),
    }

    src = Counter(t.get("来源类型", "?") for t in ts)
    coverage = {}
    for f in DETAIL_FIELDS:
        n = sum(1 for t in ts if t.get(f))
        coverage[f] = round(n / total * 100, 1) if total else 0

    failed = [
        {"姓名": t.get("姓名"), "URL": (t.get("详情页URL") or "")[:60],
         "类型": t.get("来源类型"), "原因": (t.get("失败原因") or "")[:80]}
        for t in ts if t.get("来源类型") in FAIL_TYPES
    ]
    skipped = []
    if skipped_path and skipped_path.exists():
        try:
            skipped = json.loads(skipped_path.read_text(encoding="utf-8"))
        except Exception:
            skipped = []

    return {
        "school": data.get("school_name"),
        "dept": data.get("dept_name"),
        "collected_at": data.get("collected_at"),
        "total": total,
        "source_dist": dict(src),
        "coverage": coverage,
        "tab_polluted": len(tab_polluted),
        "tab_polluted_detail": tab_polluted[:10],
        "email_scrambled": len(email_scrambled),
        "email_scrambled_detail": email_scrambled[:10],
        "nav_polluted": len(nav_polluted),
        "nav_polluted_detail": nav_polluted[:10],
        "table_polluted": len(table_polluted),
        "table_polluted_detail": table_polluted[:10],
        "self_label_prefixed": len(self_label_prefixed),
        "self_label_prefixed_detail": self_label_prefixed[:10],
        "tail_leak": len(tail_leak),
        "tail_leak_detail": tail_leak[:10],
        "tiny_frag": len(tiny_frag),
        "tiny_frag_detail": tiny_frag[:10],
        "birth_bad": len(birth_bad),
        "birth_bad_detail": birth_bad[:10],
        "misaligned": len(misaligned),
        "misaligned_detail": misaligned[:10],
        "leak": len(leak),
        "leak_detail": leak[:10],
        "name_mismatch": len(name_mismatch),
        "name_mismatch_detail": name_mismatch[:10],
        "junk_files": junk_files,
        "title_rate": title_rate,
        "dir_rate": dir_rate,
        "failed": len(failed),
        "failed_detail": failed[:20],
        "skipped_urls": len(skipped),
        "skipped_detail": skipped[:10],
    }


def render_md(r: dict) -> str:
    lines = [
        f"# 质量报告: {r['school']} > {r['dept']}",
        f"采集时间: {r['collected_at']} | 共 {r['total']} 人",
        "",
        "## 来源分布",
        " | ".join(f"{k}={v}" for k, v in r["source_dist"].items()),
        "",
        "## 字段覆盖率",
        "| 字段 | 覆盖率 |",
        "|------|--------|",
    ]
    for f, pct in r["coverage"].items():
        lines.append(f"| {f} | {pct}% |")
    lines += [
        "",
        f"## 污染残留: tab 标签词链 {r['tab_polluted']} 人, 邮箱防爬 {r['email_scrambled']} 人, 导航词链 {r['nav_polluted']} 人, 表格链 {r['table_polluted']} 人, 节标签前缀 {r['self_label_prefixed']} 人, 尾漏 {r['tail_leak']} 人, 短碎片 {r['tiny_frag']} 人, 出生年月异常 {r['birth_bad']} 人, 字段错位 {r['misaligned']} 人",
    ]
    for name, f, v in r["tab_polluted_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, v in r["email_scrambled_detail"]:
        lines.append(f"- {name}: {v}")
    for name, f, v in r["nav_polluted_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["table_polluted_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["self_label_prefixed_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["tail_leak_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["tiny_frag_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["birth_bad_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    for name, f, v in r["misaligned_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    lines += [
        "",
        f"## 疑似漏提 {r['leak']} 人（页面有字段线索但字段空, 重提取可救）",
        f"## 名字-页面不一致 {r['name_mismatch']} 人（页面 title 不含姓名 = 名字错误/下错页, 归上游 QC）",
        f"垃圾文件: 本地文件 {r['junk_files'].get('checked')} 个, 正文含名 {r['junk_files'].get('with_name')} 个, 含名率 {r['junk_files'].get('rate')}%（<20% = JS 壳, 提取无意义, 待渲染/上游）",
        f"职称可判页: 页含职称词 {r['title_rate'].get('clue')} 个, 已提取 {r['title_rate'].get('filled')} 个, 提取率 {r['title_rate'].get('rate')}%（简历型页不写职称不计分母）",
        f"方向可判页: 页含方向词 {r['dir_rate'].get('clue')} 个, 已提取 {r['dir_rate'].get('filled')} 个, 提取率 {r['dir_rate'].get('rate')}%（页面无方向信息不计分母）",
    ]
    for name, pttl in r["name_mismatch_detail"]:
        lines.append(f"- {name}: 页面title={pttl}")
    for name, f, v in r["leak_detail"]:
        lines.append(f"- {name} [{f}]: {v}")
    lines += ["", f"## 失败 {r['failed']} 人"]
    for d in r["failed_detail"]:
        lines.append(f"- {d['姓名']} ({d['类型']}): {d['原因']}")
    lines += ["", f"## 拦截名单 skipped_urls {r['skipped_urls']} 条（上游修复后重扫）"]
    for d in r["skipped_detail"]:
        lines.append(f"- {d.get('姓名')}: {d.get('detail_url', '')[:60]} [{d.get('原因')}]")
    return "\n".join(lines)


def list_dept_details(school: str | None, dept: str | None) -> list[tuple[Path, Path | None]]:
    out = []
    if school:
        sroot = _THIS_DIR / school
        if not sroot.is_dir():
            print(f"❌ 无此学校目录: {sroot}")
            sys.exit(1)
        if dept:
            p = sroot / dept / "teachers_detail.json"
            if p.exists():
                out.append((p, sroot / dept / "skipped_urls.json"))
        else:
            for sub in sorted(sroot.iterdir()):
                p = sub / "teachers_detail.json"
                if p.is_file():
                    out.append((p, sub / "skipped_urls.json"))
    else:
        for p in sorted(_THIS_DIR.glob("*/")):
            for sub in sorted(p.iterdir()):
                jp = sub / "teachers_detail.json"
                if jp.is_file():
                    out.append((jp, sub / "skipped_urls.json"))
    return out


def main():
    ap = argparse.ArgumentParser(description="教师详情质量门禁")
    ap.add_argument("--school", default=None)
    ap.add_argument("--dept", default=None)
    ap.add_argument("--all", action="store_true", help="扫描全部院系")
    args = ap.parse_args()

    targets = list_dept_details(args.school, args.dept)
    if not targets:
        print("❌ 未找到任何 teachers_detail.json")
        sys.exit(1)

    summaries = []
    for detail_path, skipped_path in targets:
        r = analyze_dept(detail_path, skipped_path)
        md = render_md(r)
        (detail_path.parent / "evaluation_report.md").write_text(md, encoding="utf-8")
        (detail_path.parent / "evaluation_summary.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(md)
        print("\n" + "=" * 60)
        summaries.append(r)

    # 全量汇总
    if len(summaries) > 1:
        agg = {
            "depts": len(summaries),
            "total": sum(r["total"] for r in summaries),
            "tab_polluted": sum(r["tab_polluted"] for r in summaries),
            "email_scrambled": sum(r["email_scrambled"] for r in summaries),
            "nav_polluted": sum(r["nav_polluted"] for r in summaries),
            "table_polluted": sum(r["table_polluted"] for r in summaries),
            "self_label_prefixed": sum(r["self_label_prefixed"] for r in summaries),
            "tail_leak": sum(r["tail_leak"] for r in summaries),
            "tiny_frag": sum(r["tiny_frag"] for r in summaries),
            "birth_bad": sum(r["birth_bad"] for r in summaries),
            "misaligned": sum(r["misaligned"] for r in summaries),
            "leak": sum(r["leak"] for r in summaries),
            "failed": sum(r["failed"] for r in summaries),
            "skipped_urls": sum(r["skipped_urls"] for r in summaries),
        }
        print(f"\n📊 汇总: {agg['depts']} 院系 / {agg['total']} 人 | "
              f"tab链 {agg['tab_polluted']} | 邮箱防爬 {agg['email_scrambled']} | "
              f"导航词链 {agg['nav_polluted']} | 表格链 {agg['table_polluted']} | 节标签前缀 {agg['self_label_prefixed']} | 尾漏 {agg['tail_leak']} | 短碎片 {agg['tiny_frag']} | 出生年月异常 {agg['birth_bad']} | 字段错位 {agg['misaligned']} | 疑似漏提 {agg['leak']} | "
              f"失败 {agg['failed']} | 拦截名单 {agg['skipped_urls']}")


if __name__ == "__main__":
    main()
