#!/usr/bin/env python3
"""
师资页分类发现模块 — 解决 Step 3 只抓一类教师导致的严重遗漏。

能力:
  1. clean_name: 拆解名字与职称粘连（'陈招斌高' → 陈招斌; '陈招斌 高级工程师' → 陈招斌+高级工程师）
  2. 从院系官网/已知师资页出发, 发现师资分类导航（职称分类/院系分类/职务分类等）
  3. 逐个分类页抓取教师列表（含分页遍历）
  4. 与 Step 3 数据合并去重, 输出完整教师清单 + 发现报告

网络请求统一走 DetailScraper（util/ 瑞数方案）。
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ── 分类导航关键词（分类页链接的文本特征）──
CATEGORY_KEYWORDS = [
    "领导", "负责人", "人员", "退休", "委员会", "顾问", "教授", "副教授",
    "讲师", "工程师", "实验师", "技师", "助教", "研究员", "教师", "导师",
    "团队", "博导", "硕导", "博士后", "在编", "兼职", "客座", "名誉",
    "实验技术", "课程", "教学", "事务", "创新", "指导", "教职工",
]

# ── 排除的导航词（不是教师分类）──
EXCLUDE_NAV = [
    "首页", "新闻", "通知", "公告", "下载", "English", "中文", "简介",
    "概况", "招生", "就业", "服务", "更多", "详情", "查看", "返回",
    "登录", "注册", "联系", "地图", "友情", "站内", "搜索",
    # 2026-10-24 ≥4字名字全量QC扩充(550个"其他"家族高频导航词,
    # 人名绝不可能含这些词——零误杀):
    "走进", "机构", "治理", "科研", "视频", "工作", "资料", "项目",
    "验证", "快讯", "专题", "门户", "讯息", "展示", "掠影", "设施",
    "主修", "课程", "毕业", "办证", "成员", "学者", "校区", "分院",
    "学部", "专科", "门诊", "党建", "报道", "主题", "祝福", "足球",
    "奥运", "班级", "办学", "罐笼", "箕斗", "硕士", "本科",
    # 2026-10-24 二轮收割(其他550家族): 栏目名/学科词/设备词——
    # 人名绝不含: 学家(生理学家/泌尿学家=学科栏目)、新书发布/身份认定
    # /科研规范/机构框架/数据资料/用户入驻/海报模版/微博(栏目)、
    # 绳罐笼/绳箕斗(矿机设备)
    "学家", "新书发布", "身份认定", "科研规范", "机构框架",
    "数据资料", "用户入驻", "海报模版", "微博", "杰出青年",
]

# ── 职称词表（按长度降序匹配，用于剥离粘连职称）──
TITLE_WORDS = [
    "教授级高级工程师", "教授级高级实验师", "教授级高工",
    "高级工程师", "高级实验师", "助理研究员", "助理实验师",
    "副研究员", "研究员", "工程师", "实验师", "实验员",
    "教授", "副教授", "讲师", "助教", "院士", "博士后", "博导",
    "硕导", "高工", "技师", "会计师", "医师", "博士",
    "译审", "研究馆员", "编审",
    # 2026-10-26 长聘/准聘粘连(北大人文学部"马楠长聘"实锤, util.teacher_extract
    # 已修本表漏修——两处 clean_name 必须同口径, 否则提取侧剥不掉):
    "长聘副教授", "长聘教授", "准聘副教授", "准聘教授",
    "长聘", "准聘",
]
# 职务词（非职称但会与名字粘连: '吴晓萍党总支' = 吴晓萍+党总支书记截断;
# 人名绝不可能以这些词结尾——安全剥离, 与 2.5 步标签词同性质）
GLUE_TITLE_SUFFIX = [
    "党总支书记", "党支部书记", "党总支", "党委副书记", "党委书记",
    "系主任", "副主任", "主任", "书记", "院长", "副院长",
    "青年教师", "青年",
]


def clean_name(raw: str) -> tuple[str, str]:
    """拆解名字与职称粘连。

    返回 (姓名, 职称):
      '陈招斌'                     → ('陈招斌', '')
      '陈招斌 高级工程师'           → ('陈招斌', '高级工程师')
      '陈招斌高'  (截断粘连)        → ('陈招斌高', '')  # 单字一律不剥
      '李纯高'                      → ('李纯高', '')     # 真名保真
      '潘蕊实验'                    → ('潘蕊', '')       # 双字前缀仍剥
      '汤　儆 教授' (全角空格)        → ('汤儆', '教授')

    2026-10-24 一致性优化（用户纠偏）: 同一名字的判定必须唯一, 不能依赖
    title 字段的有无（「陈招斌高」无 title 保真、有 title=高工 就剥 = 自相
    矛盾）。全库数据铁证: 单字尾+同首字 title 的组合仅 2 个可疑样本
    （吴晓球教+教授/行文智教+教授）, 而单字尾真名几百个（X高 224/X实 162/
    X助 20…）——单字剥离收益成本比倒挂, **彻底移除**。粘连残留归上游修
    （数据源头）, 本地不猜。双字前缀（实验/助理等, 双字结尾真名几乎不存在）
    保留。
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    # 名字内部的全角空格/不换行空格归一（'汤　儆' → '汤儆'）
    raw = raw.replace("　", "").replace("\xa0", "")

    # 1) 空白/分隔符拆分: 首段为名字，余段为职称
    parts = re.split(r"[\s,，、|·]+", raw)
    name = parts[0]
    title = " ".join(p for p in parts[1:] if p)

    # 2) 名字尾部粘连完整职称
    for t in sorted(TITLE_WORDS, key=len, reverse=True):
        if name.endswith(t) and len(name) - len(t) >= 2:
            return name[: -len(t)], t

    # 2.5) 名字尾部粘连字段标签词（北大历史学系: '曹寅职称'/'包茂红职'，
    #       名单页提取把姓名与下一列"职称"标签粘在一起）
    #       标签词绝非人名组成部分，作为后缀剥离安全
    #       2026-10-24 全量检查扩充: '鲍杰职务'38个/'XX老师'8个/'XX主页'5个
    #       (≥4字名字全量QC实锤) — 称呼/页面词同样绝非遗人名
    for lbl in ("职称", "职务", "学历", "学位", "职", "老师", "主页"):
        if name.endswith(lbl) and len(name) - len(lbl) >= 2:
            return name[: -len(lbl)], title

    # 2.6) 职务词后缀剥离（'吴晓萍党总支'→吴晓萍+党总支书记截断粘连;
    #       '刘红柱译审' 译审已入 TITLE_WORDS 由第2步剥）
    for gw in sorted(GLUE_TITLE_SUFFIX, key=len, reverse=True):
        if name.endswith(gw) and len(name) - len(gw) >= 2:
            return name[: -len(gw)], title

    # 3) 名字尾部粘连职称前缀（截断: '潘蕊实验'→'实验师'前缀'实验'）
    #    剥离后剩余须仍是 2-4 个纯汉字。
    #    2026-10-24 单字前缀(k=1)已移除——收益(全库仅 2 个可疑样本)远小于
    #    误杀真名成本(几百个 X高/X实 真名), 只保留 k>=2 双字前缀。
    for t in sorted(TITLE_WORDS, key=len, reverse=True):
        for k in range(len(t) - 1, 1, -1):
            prefix = t[:k]
            if name.endswith(prefix):
                rest = name[:-k]
                if 2 <= len(rest) <= 4 and all("一" <= c <= "鿿" for c in rest):
                    return rest, title

    # 3.5) 名字前缀粘连职称（'教授巨鹏'/'讲师郭静'/'博士李清' — 提取列序
    #       颠倒, 职称在前）。职称词作为名字开头绝不可能是真名, 剥后剩余
    #       2-4 纯汉字即安全剥离; 完整职称词存入 title。
    #       最长匹配失败即止: '博士后李' 剥「博士后」剩 1 字无效 → 直接
    #       放弃(不能继续试「博士」剥出「后李」——2026-10-24 实测 bug)。
    for t in sorted(TITLE_WORDS, key=len, reverse=True):
        if name.startswith(t):
            rest = name[len(t):]
            if 2 <= len(rest) <= 4 and all("一" <= c <= "鿿" for c in rest):
                return rest, t
            break

    return name, title


def is_valid_name(name: str) -> bool:
    """2-4 个纯汉字，非组织/栏目词。"""
    if not name or not (2 <= len(name) <= 4):
        return False
    if any(c.isascii() and c.isalnum() for c in name):
        return False
    if not all("一" <= c <= "鿿" for c in name):
        return False
    return True


# 2026-10-24 URL 结构黑名单（用户建议）: 新闻/科室/栏目/微信文章路径即使
# 含数字串(满足白名单 /\d{4,}/)也判非教师页——吴晓球教类新闻 hash URL
# (nvwang.sf.ruc.edu.cn/xwzx/13036...htm) 与科室页(gyfwyy.com/ks/lc/...)
# 通过白名单实锤。路径词取全库 QC 高频(≥5 个): xwzx 58/ks 53/微信 9 等。
_NON_TEACHER_PATH_RE = re.compile(
    r"/(xwzx|news|xwdt|xinwen|notice|ggtz)/"     # 新闻中心/通知公告
    r"|/(ksdh?)/"                                # 医院科室导航(ks=科室)
    r"|mp\.weixin\.qq\.com"                      # 微信公众号文章
    r"|/(rcpy|xsgz|topic|about|leadership|jiuzhen|zhaosheng|jiuye|"
    r"jxky|kxyj|djgz)/"                          # 栏目(人才培养/学生工作/专题/领导/就医)
)


def _is_detail_url(href: str) -> bool:
    """判断链接是否为教师详情页（而非导航/分类页）。

    与 evaluate_teachers/count_teacher_elements 的 DETAIL_HINT 保持一致:
    同时认 /c\\d+a\\d+/ 与 /c[0-9a-f]{6,}/ 两种 CMS 详情URL形式。
    2026-10-24 增加 URL 路径黑名单优先判定（新闻/科室/栏目/微信）。
    """
    if _NON_TEACHER_PATH_RE.search(href.lower()):
        return False
    return bool(
        re.search(r"/info/\d+", href)
        or re.search(r"/c\d+a\d+", href)
        or re.search(r"/c[0-9a-f]{6,}", href)
        or re.search(r"/\d{4,}/", href)
    )


class FacultyDiscovery:
    """师资分类发现。"""

    def __init__(self, scraper):
        self.scraper = scraper  # DetailScraper 实例（网络请求统一走它）

    # ═══════════════════════════════════════════════════════════════
    #  师资页发现
    # ═══════════════════════════════════════════════════════════════

    def find_faculty_url(self, dept_url: str, known_faculty_url: str = "") -> str:
        """从院系官网找师资页。已知 Step 3 师资页 URL 优先使用。"""
        if known_faculty_url:
            return known_faculty_url

        html = self.scraper.fetch_page(dept_url)
        if not html:
            return ""

        soup = BeautifulSoup(html, "lxml")
        best = ""
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = urljoin(dept_url, a["href"])
            if not text or any(k in text for k in EXCLUDE_NAV):
                continue
            # 关键词 + 路径特征
            if any(k in text for k in ["师资", "教师", "队伍", "人员", "人才"]):
                if "szdw" in href.lower() or "teacher" in href.lower() or "faculty" in href.lower():
                    return href
                if not best:
                    best = href
        return best

    # ═══════════════════════════════════════════════════════════════
    #  分类链接发现
    # ═══════════════════════════════════════════════════════════════

    def find_category_links(self, html: str, base_url: str) -> list[dict]:
        """从师资页/首页导航中提取分类页链接。

        过滤规则:
          - 与师资页同目录（如 /szdw/）的链接 → 强信号，保留
          - 其他目录的链接 → 文本须含强分类词（领导/负责人/委员会/退休/人员/教授等）
          - 排除 教学理念/教学论文 等泛内容页（'教学'词太宽，不作分类词）
        """
        soup = BeautifulSoup(html, "lxml")
        host = urlparse(base_url).hostname
        faculty_dir = urlparse(base_url).path.rsplit("/", 1)[0]  # 师资页所在目录
        strong_kw = [
            "领导", "负责人", "委员会", "退休", "顾问", "人员", "教授",
            "副教授", "讲师", "工程师", "实验师", "技师", "研究员", "导师",
        ]
        categories = []
        seen = set()

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = urljoin(base_url, a["href"]).split("#")[0]  # 去锚点
            if not text or len(text) > 20:
                continue
            if urlparse(href).hostname != host:
                continue
            if _is_detail_url(href):
                continue
            if any(k in text for k in EXCLUDE_NAV):
                continue
            if href in seen:
                continue

            link_dir = urlparse(href).path.rsplit("/", 1)[0]
            same_dir = link_dir == faculty_dir

            if same_dir:
                # 同目录: 文本含任意分类词即可
                if any(k in text for k in CATEGORY_KEYWORDS):
                    seen.add(href)
                    categories.append({"name": text, "url": href})
            else:
                # 异目录: 必须含强分类词
                if any(k in text for k in strong_kw):
                    seen.add(href)
                    categories.append({"name": text, "url": href})

        return categories

    # ═══════════════════════════════════════════════════════════════
    #  列表页教师提取 + 分页
    # ═══════════════════════════════════════════════════════════════

    def extract_teachers_from_list(self, html: str, base_url: str) -> list[dict]:
        """从列表页提取教师: 2-12字中文文本 + 详情页链接。名字职称自动拆分。"""
        soup = BeautifulSoup(html, "lxml")
        teachers = []
        seen = set()

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = urljoin(base_url, a["href"])
            if not text or not (2 <= len(text) <= 12):
                continue
            if not any("一" <= c <= "鿿" for c in text):
                continue
            if any(k in text for k in EXCLUDE_NAV):
                continue
            # 分类导航链接（无详情页特征）跳过
            if not _is_detail_url(href):
                continue

            name, title = clean_name(text)
            if not is_valid_name(name):
                continue
            if href in seen:
                continue
            seen.add(href)
            teachers.append({"name": name, "title": title, "detail_url": href})

        return teachers

    def extract_teachers_from_text(self, html: str) -> list[dict]:
        """从纯文本提取无链接教师（光荣退休/委员会等页面只有文字名单）。

        模式: 2-4个汉字（允许内部全角空格）+ 职称词。
        两遍扫描:
          Pass A 无分隔符拼接（'林敏教授级高级实验师'）→ 名字首字过滤结构词
          Pass B 单元格分隔符 '|'（'吕鑫|教授' 跨单元格）→ 名字职称间允许 '|'
        """
        soup = BeautifulSoup(html, "lxml")
        # 正文容器优先
        content = None
        for sel in [".content", "#vsb_content", ".read", ".wp_articlecontent",
                    ".article-content", "table"]:
            content = soup.select_one(sel)
            if content and len(content.get_text(strip=True)) > 20:
                break
        if content is None:
            content = soup.find("body") or soup

        title_alt = "|".join(
            t for t in sorted(TITLE_WORDS, key=len, reverse=True)
        )
        # 结构词: 名字不能以这些字开头（'员张剑荣' 的 '员'）
        _struct_start = set("员委任秘书主兼和及与、，丨|")
        _struct_words = ["主任", "委员", "主席", "秘书", "老师", "同志", "首席", "执行"]

        teachers = []
        seen = set()

        def _collect(text: str, allow_pipe: bool):
            gap = r"[\s|]*" if allow_pipe else r"\s*"
            name_re = re.compile(rf"([一-鿿]{{2,4}}){gap}({title_alt})")
            for m in name_re.finditer(text):
                name = m.group(1)
                title = m.group(2)
                if name[0] in _struct_start:
                    continue
                if any(k in name for k in _struct_words):
                    continue
                if not is_valid_name(name):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                teachers.append({"name": name, "title": title, "detail_url": ""})

        # Pass A: 无分隔符
        text_a = content.get_text(separator="").replace("　", "").replace("\xa0", "")
        _collect(text_a, allow_pipe=False)

        # Pass B: 单元格分隔符（跨单元格 名字|职称）
        text_b = content.get_text(separator="|").replace("　", "").replace("\xa0", "")
        _collect(text_b, allow_pipe=True)

        return teachers

    def _find_next_page(self, html: str, base_url: str) -> str:
        """检测下一页链接。"""
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            cls = " ".join(a.get("class", []) or [])
            if text in ("下一页", "下页", ">", "»", ">>") or "next" in cls.lower():
                href = urljoin(base_url, a["href"])
                if _is_detail_url(href) is False:
                    return href
        return ""

    def crawl_category(self, cat_url: str, faculty_url: str, max_pages: int = 20) -> dict:
        """抓取一个分类页（含分页遍历）。404 时尝试师资页目录前缀回退。"""
        result = {"url": cat_url, "teachers": [], "pages": 0, "fallback_used": False}

        html = self.scraper.fetch_page(cat_url)
        if not html and faculty_url:
            # 导航 href 可能是坏链（如根级 /zxld.htm），用师资页所在目录前缀重试
            alt = urljoin(faculty_url, cat_url.rstrip("/").rsplit("/", 1)[-1])
            if alt != cat_url:
                html = self.scraper.fetch_page(alt)
                if html:
                    cat_url = alt
                    result["fallback_used"] = True

        if not html:
            result["error"] = "fetch_failed"
            return result

        seen = set()
        for page in range(1, max_pages + 1):
            teachers = self.extract_teachers_from_list(html, cat_url)
            new = 0
            for t in teachers:
                key = t["detail_url"] or t["name"]
                if key not in seen:
                    seen.add(key)
                    result["teachers"].append(t)
                    new += 1

            # 无链接页面（光荣退休/委员会等纯文字名单）→ 文本提取兜底
            if new == 0:
                text_teachers = self.extract_teachers_from_text(html)
                for t in text_teachers:
                    if t["name"] not in seen:
                        seen.add(t["name"])
                        result["teachers"].append(t)
                        new += 1

            result["pages"] = page
            if new == 0 and page > 1:
                break

            next_url = self._find_next_page(html, cat_url)
            if not next_url or next_url == cat_url:
                break
            cat_url = next_url
            html = self.scraper.fetch_page(cat_url)
            if not html:
                break

        return result

    # ═══════════════════════════════════════════════════════════════
    #  主流程: 发现 + 合并
    # ═══════════════════════════════════════════════════════════════

    def discover(
        self,
        step3_teachers: list[dict],
        dept_url: str,
        known_faculty_url: str = "",
    ) -> tuple[list[dict], dict]:
        """发现全部分类教师并与 Step 3 合并。

        Returns: (merged_teachers, report)
        """
        faculty_url = self.find_faculty_url(dept_url, known_faculty_url)

        report = {
            "dept_url": dept_url,
            "faculty_url": faculty_url,
            "categories": [],
            "step3_count": len(step3_teachers),
        }

        category_results = []
        all_discovered = []

        if faculty_url:
            html = self.scraper.fetch_page(faculty_url)
            if html:
                cats = self.find_category_links(html, faculty_url)
                if not cats:
                    # 线索页本身就是教师名单页（吉大 zky.jlu.edu.cn/szdw1/js.htm
                    # 这类"分类页直链"没有子分类链接）→ 直接当名单页爬
                    res = self.crawl_category(faculty_url, faculty_url)
                    res["name"] = "名单页"
                    for t in res["teachers"]:
                        t["category"] = "名单页"
                    category_results.append(res)
                    all_discovered.extend(res["teachers"])
                for cat in cats:
                    res = self.crawl_category(cat["url"], faculty_url)
                    res["name"] = cat["name"]
                    # 给教师打上来源分类标签（供分层抽样）
                    for t in res["teachers"]:
                        t["category"] = cat["name"]
                    category_results.append(res)
                    all_discovered.extend(res["teachers"])

        report["categories"] = [
            {
                "name": r.get("name", ""),
                "url": r["url"],
                "teachers": len(r["teachers"]),
                "pages": r.get("pages", 0),
                "fallback_used": r.get("fallback_used", False),
                "error": r.get("error", ""),
            }
            for r in category_results
        ]
        report["discovered_count"] = len(all_discovered)

        merged = self.merge(step3_teachers, all_discovered)
        report["merged_count"] = len(merged)
        report["new_vs_step3"] = len(merged) - len(step3_teachers)
        return merged, report

    @staticmethod
    def merge(step3: list[dict], discovered: list[dict]) -> list[dict]:
        """合并去重（按 detail_url）。Step 3 名字做粘连清洗。"""
        by_url = {}
        for t in step3:
            name, title = clean_name(t.get("name", ""))
            url = t.get("detail_url", "")
            key = url or f"{name}|{title}"
            by_url[key] = {
                "name": name,
                "title": title or t.get("title", ""),
                "detail_url": url,
                "department": t.get("department", ""),
                "category": t.get("category", "Step3"),
            }
        for t in discovered:
            key = t.get("detail_url", "") or t.get("name", "")
            if key not in by_url:
                by_url[key] = {
                    "name": t.get("name", ""),
                    "title": t.get("title", ""),
                    "detail_url": t.get("detail_url", ""),
                    "department": "",
                    "category": t.get("category", ""),
                }
            else:
                # 已存在则补职称
                if t.get("title") and not by_url[key]["title"]:
                    by_url[key]["title"] = t["title"]
                if not by_url[key].get("category") and t.get("category"):
                    by_url[key]["category"] = t["category"]
        return list(by_url.values())
