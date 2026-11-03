#!/usr/bin/env python3
"""spider_generator.py v2 — 软过滤 + LLM决断 + 审计链路"""

import argparse, json, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHOOLS_DIR = BASE_DIR / "output"


def generate_spider(config_path: Path) -> str:
    config = json.loads(config_path.read_text("utf-8"))

    sn = config["school_name"]
    su = config["school_url"]
    ac = config.get("anti_crawl", "curl_cffi")

    # 清理 source_pages
    sp = []
    for p in config.get("source_pages", []):
        item = {"url": p["url"], "label": p["label"],
                "css_selectors": p.get("css_selectors", ["a[href]"]),
                "fetch_method": p.get("fetch_method", "smart")}
        # 透传扩展开关字段 (keep_js_inst / extract_text_nodes / extract_title_card / discover_sub_pages 等)
        for k in ("keep_js_inst", "extract_text_nodes", "extract_title_card",
                  "discover_sub_pages", "link_attr"):
            if k in p:
                item[k] = p[k]
        sp.append(item)

    ikey = config.get("include_keywords", ["学院","系","研究院","研究所","中心","学部","书院","实验室","基地","医院","部"])
    ekey = config.get("exclude_keywords", ["管理","行政","后勤","财务","教务","审计","资产","保卫","招生","就业","离退休","OA","一卡通","VPN","图书馆","校医院","出版社","新闻","通知","公告"])
    eurl = config.get("exclude_url_patterns", ["/news/","/info/","/xw/","/article/","/content/","/detail/"])
    enam = config.get("exclude_name_patterns", [])

    return SPIDER_TEMPLATE.format(
        sn=sn, su=su, ac=ac,
        sp=repr(sp),
        ikey=json.dumps(ikey, ensure_ascii=False),
        ekey=json.dumps(ekey, ensure_ascii=False),
        eurl=json.dumps(eurl, ensure_ascii=False),
        enam=json.dumps(enam, ensure_ascii=False),
    )


SPIDER_TEMPLATE = '''#!/usr/bin/env python3
"""{sn} 院系采集 — 软过滤 + LLM决断 + 审计链路"""

import argparse, csv, json, os, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent       # output/<school>/
BASE_DIR = HERE.parent.parent                 # colleges department/
UTIL_DIR = BASE_DIR.parent / "util"
JSONL_FILE = BASE_DIR / "depart_985211.jsonl"
CSV_FILE = BASE_DIR / "depart_985211.csv"

sys.path.insert(0, str(UTIL_DIR))
from smart_fetch import smart_fetch, _try_drission, _try_cdp
from bs4 import BeautifulSoup

SCHOOL_NAME = {sn!r}
SCHOOL_URL  = {su!r}
ANTI_CRAWL  = {ac!r}
SOURCE_PAGES = {sp}
INCLUDE_KW = {ikey}
EXCLUDE_KW = {ekey}
EXCLUDE_URL = {eurl}
EXCLUDE_NAME = {enam}

# ── 过滤清单 (agent 阶段 LLM 生成, spider 只读取执行) ──
FILTER_LIST_FILE = HERE / "filter_list.json"
DROP_LIST = set()  # 明确排除的机构名
KEEP_LIST = set()  # 明确保留的机构名(可选,覆盖默认规则)
try:
    _fl = json.loads(FILTER_LIST_FILE.read_text("utf-8")) if FILTER_LIST_FILE.exists() else {{}}
    DROP_LIST = set(_fl.get("drop", []))
    KEEP_LIST = set(_fl.get("keep", []))
except Exception:
    pass

# ── 教学单位保护: filter_list drop 不得删除具体教学单位 (防 LLM 把系列表误判为导航)
# 东农 72 系/重大 78 系教训: "农学系""英语系"等系级单位被 LLM 误加入 drop。
# 保护规则: "系/书院/学部"结尾(≥3字) 与 "学院"结尾(≥5字) 的 drop 项一律放行,
#   但分类标题 (独立学院/二级学院/专业学院等4字词, "注:*为..."注释, HARD_DROP 导航词) 除外。
_DROP_CLASSIFY = {{
    "独立学院", "二级学院", "专业学院", "学院（部）", "学院(部)", "学院（系）", "学院(系)",
    "学院学部", "学部学院", "重点实验室", "全国重点实验室", "教育部重点实验室", "省级重点实验室",
    "市级重点实验室", "国家工程研究中心", "省部级实验室", "人才培养基地", "科研基地",
    "研究所（中心）", "研究院所", "跨学院研究机构", "学院与学科", "学院（研究院）",
    "附属医院", "教学部", "教研部",
}}
DROP_LIST = {{d for d in DROP_LIST
             if d not in _DROP_CLASSIFY
             and not d.startswith(("注", "备注", "说明"))          # 页面注释文字 (F6 双保险)
             and not (d.endswith("系") and len(d) >= 3)            # "农学系""英语系"是真系
             and not (d.endswith(("学院", "书院", "学部", "研究院", "研究所", "实验室",
                                  "教学部", "教研部", "附属医院")) and len(d) >= 4)}}

STRONG_KEEP = ["学院", "系", "研究院", "研究所", "实验室", "书院", "学部", "基地"]
STRONG_EXCLUDE = ["管理处", "办公室", "委员会", "党组", "纪委", "机关党委",
                  "招标", "采购", "财务处", "审计处", "人事处", "保卫处",
                  "后勤", "OA系统", "VPN", "一卡通", "校园卡", "邮箱登录",
                  "校医院", "出版社",
                  "社保",
                  "财务部", "宣传部", "组织部", "统战部", "武装部", "保卫部",
                  "学科建设", "招生", "工作通知", "通知公告", "新闻中心",
                  "邮件系统", "邮件服务", "邮箱系统", "办公系统", "信息门户",
                  "系统登录", "用户登录", "统一认证",
                  "核算中心", "采购中心", "事务中心", "首页", "交流动态", "国际交流", "联系我们",
                  "反腐倡廉", "校史", "图库", "校区", "教学日历", "世纪杯",
                  "创新创业教育综合服务平台",
                  # 师资介绍/人物荣誉类 (非机构)
                  "院士", "候选人", "杰出青年", "优秀青年", "百千万人才",
                  "突出贡献", "专业技术人才", "新世纪优秀人才", "政府特殊津贴",
                  "教学名师", "会士", "黄大年", "优秀教师", "劳动奖章",
                  "塔尖", "杰出人才", "学术和技术带头人", "天府学者", "师德标兵",
                  "四有", "劳动模范", "教书育人名师",
                  # 期刊/学报类
                  "学报", "《", "Planet",
                  # 教学项目/专业类
                  "专业综合改革", "卓越工程师教育培养计划", "卓越教育培养计划试点专业",
                  # 导航类
                  "成理要闻", "成理科研", "成理教育", "成理人物", "媒体成理",
                  "成理融媒", "广招英才", "成理专题", "来华申请", "出国申请",
                  "中外合作", "创新创业", "博士后流动站", "仪器共享", "博物馆藏",
                  "国际教育", "建校初期",
                  # 全站导航/页面标题类 (从大量学校实战积累)
                  "院系设置", "院系部门", "学部学院", "学院（系）", "教学机构", "科研机构",
                  "标识系统", "信息公开", "IT服务", "文明校园", "学生工作网站",
                  "干部培训", "就业创业", "科研信息网", "人事管理", "国际交流",
                  "国内合作", "校友会", "教育基金会", "校园服务", "通勤车班次",
                  "校历", "办公电话", "心理健康", "医疗服务", "运动与健康",
                  "教师个人主页", "人才工作", "人事工作", "人才招聘", "教师教学发展中心",
                  "分析测试中心", "期刊中心", "学报", "学术期刊", "大型设备仪器平台",
                  "一流学科建设", "重点科技创新平台", "服务基础教育", "乡村振兴与对口支援",
                  "成果转化", "合作办学", "专家学者走基层", "大学志愿者服务", "教育出版",
                  "档案馆", "博物馆", "校史馆", "图书馆", "官网", "官方微信", "官方微博",
                  "校友", "基金会", "本科生院", "研究生院", "培训中心", "学报编辑部",
                  "校报编辑部", "学习强国", "党委宣传部", "党委组织部", "党委统战部",
                  "党委教师工作部", "党委学生工作部", "武装部", "党校", "工会",
                  "团委", "妇委会", "迎新"]

# ═══ 硬排除层 (导航标题/分类标题/行政中心/医院内部页) ═══
# 这些名称会误触 STRONG_KEEP 的"系/学院/医院"等词 (如"院系设置"含"系"),
# 且 filter_list.json 的 keep 清单也不应覆盖 — 它们根本不是二级组织。
HARD_DROP_SUBSTR = [
    # 导航/分类标题
    "院系设置", "院系导航", "院系部门", "学院部门", "院系机构", "院系中心", "学部学院", "学院（系）",
    "学院学部", "学部院系", "教学机构", "科研机构", "教学科研机构", "机构设置", "组织机构",
    "机构导航", "机构部门", "校内科研基地",
    "支撑体系",  # 厦大 jg-title 区块标题 ("支撑体系/部、系/学部院系"并列词)
    # 行政/服务/IT 中心 (用户原则: 行政中心不是院系)
    "信息与网络中心", "网络信息中心", "网络与信息中心", "信息化办公室", "信息化建设", "信息化管理",
    "信息化教学中心", "教学运行", "考务", "智慧校园", "服务中心", "心理健康教育与咨询", "智算", "超算",
    "现代教育技术中心", "电化教育", "融媒体中心",
    # 医院内部栏目页 (附属医院站内导航, 非院系)
    "医院概况", "医院特色", "医院介绍", "医院文化", "就医指南", "预约挂号",
    "专家介绍", "名医传承", "品牌护理", "院内制剂", "科室介绍", "专科介绍",
    "医疗团队", "护理团队", "重点学科详情", "特色诊疗详情", "专家介绍详情",
    "名医传承工作室详情", "品牌护理详情", "院内制剂详情",
    # 校医院 / 党组织 / 系统入口
    "校医院", "门诊部", "医务室", "卫生所", "医疗服务中心", "党委", "监委", "党工委",
    "管理系统",
    # 新闻标题特征
    "国家自然科学奖",
]
# 精确匹配的通用分类名 (子串会误伤具体机构, 如"信息中心"会命中"世界亚洲研究信息中心")
HARD_DROP_EXACT = {{
    "院系设置", "院系导航", "院系部门", "院系机构", "学部学院", "学院（系）", "学院学部", "学部院系",
    "教学机构", "科研机构", "教学科研机构", "机构设置", "组织机构", "机构导航", "机构部门",
    "校内科研基地", "教育部野外科学观测研究站", "国家野外科学观测研究站", "野外科学观测研究站",
    "医院", "校医院", "门诊部", "数据中心", "信息中心", "计算中心", "心理咨询中心",
    "更多", "查看更多", "更多详情",
    # 自然/人文科学分类栏目 (子串会误伤真实机构: 自然科学学部/自然科学基础实验中心/人文科学研究院)
    "自然科学", "人文科学", "自然科学科研基地", "自然科学研究机构",
    "自然科学成果", "自然科学项目", "自然科学类", "自然科学科研信息网",
    # 裸分类标题 (东农等: "研究所（中心）"tab 标题/"重点实验室"裸词 — 具体"XX重点实验室"不受影响)
    "研究所（中心）", "重点实验室",
    # 省/市级科研平台分类 tab 裸词 (海大等: 页面顶部"山东省重点实验室/青岛市工程研究中心"等分类导航)
    "人文社会科学科研基地", "国家工程技术研究中心", "前沿科学中心", "教育部工程研究中心",
    "农业农村部重点实验室", "教育部重点实验室", "山东省重点实验室", "山东省技术创新中心", "山东省工程研究中心",
    "山东省工程技术研究中心", "科技部国际科技合作基地", "教育部国际合作联合实验室",
    "山东省国际科技合作示范基地", "高等学校学科创新引智基地", "青岛市重点实验室",
    "青岛市技术创新中心", "青岛市工程研究中心", "青岛工程技术研究中心",
    "校地共建科研基地", "校际共建科研基地", "校企共建科研基地",
    # 纪检监察: 子串会误杀"纪检监察学院"(人大/西政真实学院) — 仅精确匹配独立标签
    "纪检监察", "纪检监察室", "纪检监察部",
    # 东大/宁夏等: 实验室/基地列表页裸分类词 (具体"XX重点实验室"不受影响)
    "基地", "生态环境部重点实验室", "国家矿山安全监察局重点实验室", "国家（地方联合）工程实验室",
}}

# 医院临床科室 ("内科部""检验科"等; 仅 部/科 结尾时命中, "学院/系/研究院/附属XX医院"不受影响)
CLINICAL_KW = ["内科", "外科", "妇产", "妇儿", "儿科", "妇科", "产科", "骨科", "眼科",
    "耳鼻喉", "口腔科", "麻醉", "病理", "检验", "放射", "影像", "药剂", "药学",
    "护理", "急诊", "重症", "急重症", "五官", "医技", "肿瘤", "神经", "心血管",
    "呼吸", "消化", "泌尿", "内分泌", "血液", "风湿免疫", "中医科", "针灸",
    "推拿", "康复", "皮肤", "整形", "烧伤", "急救", "预防保健", "体检", "营养", "感染"]
CLINICAL_SUFFIX = ("部", "科")


def _is_clinical_dept(name):
    # "XX学部"(医药学部/理学部)是教学单位, 含"学"字豁免; 医院部门"药学部/护理部"不含"学"
    if name.endswith("部") and "学" in name:
        return False
    return name.endswith(CLINICAL_SUFFIX) and any(k in name for k in CLINICAL_KW)


def _hard_drop_reason(name):
    """返回硬排除理由; 空串表示不排除。"""
    # 页面注释文字 (清华"注：*为非实体学院"教训) — 注释只是提到机构词, 不代表组织本身
    if re.match(r'^(注|备注|说明)\\s*[:：]', name):
        return "硬排除: 页面注释文字"
    # 新闻标题特征 (北中医教训: "282026-05我校中药学院团队…" = 2026-05-28 新闻日期前缀)
    if re.match(r'^\\d{{1,2}}\\d{{4}}-\\d{{2}}', name) or ("我校" in name and len(name) > 20):
        return "硬排除: 新闻标题(日期前缀/我校开头)"
    # 孔子学院: 外派/合作办学机构, 非本校二级教学组织 (天大 gjjl/kzxy.htm 教训)
    if name == "孔子学院" or name.startswith("孔子学院") or "孔子学院办公室" in name:
        return "硬排除: 孔子学院(外派合作机构,非本校二级组织)"
    # 导航栏目词: 无独立意义的栏目/导航文本 (天大"学院（部）"文本节点教训)
    if name in ("学院（部）", "学院(部)", "学院（系）", "学院(系)", "院系设置", "院系介绍", "院系部门", "教学单位",
                "科研单位", "附属医院", "直属医院", "二级学院", "专业学院"):
        return "硬排除: 导航栏目词"
    # 只检查主名 (去括号): 括号内是分类标注/注释, 不是机构名的一部分
    # "文物与博物馆学系（教学科研机构）" → 主名"文物与博物馆学系" 不被"教学科研机构"硬排除
    main = re.sub(r'[（(].*?[)）]', '', name).strip()
    if any(k in main for k in HARD_DROP_SUBSTR):
        return "硬排除: 导航/分类标题/行政中心/医院内部"
    if main in HARD_DROP_EXACT:
        return "硬排除: 通用分类/导航名"
    # 省/市级科研平台分类 tab 裸词 ("山东省重点实验室"/"青岛市工程研究中心"; 具体"XX省XX重点实验室"不匹配)
    if re.fullmatch(r'[一-鿿]{{2,5}}(省|市|自治区)(国际联合实验室|工程实验室|高校重点实验室|重点实验室|工程研究中心|技术创新中心|工程技术研究中心|国际科技合作示范基地)', main):
        return "硬排除: 省/市级科研平台分类标题"
    if main.endswith(("详情", "概况", "特色", "简介", "领导", "机构", "动态", "新闻", "公告")):
        return "硬排除: 栏目页标题(XX详情/概况/简介/领导等)"
    if _is_clinical_dept(name):
        return "硬排除: 医院临床科室(XX部/XX科)"
    return ""


# 弱排除词: 名称同时含强教学词("学院/研究院/研究所"等)时豁免排除,
# 如"国际交流学院""校园卡标准化研究所"是真机构; "校园卡管理中心"(仅含中心)不豁免
_WEAK_EXCLUDE = ("国际交流", "国际教育", "国内合作", "中外合作", "创新创业",
                 "本科教学", "研究生教育", "继续教育", "远程教育", "网络教育", "就业创业",
                 "校园卡", "一卡通", "图书馆", "出版社")
_STRONG_TEACH_WORD = ("学院", "书院", "系", "学部", "研究院", "研究所", "实验室",
                      "教学部", "教研部", "附属医院")
_TEACH_END = ("学院", "书院", "系", "学部", "研究院", "研究所",
              "实验室", "教学部", "教研部", "附属医院")


def _main_ends_teaching(name):
    """去括号后主名以教学词结尾 → 真教学单位 (STRONG_EXCLUDE 豁免)。
    "文物与博物馆学系（教学科研机构）" → 主名"文物与博物馆学系" → 豁免;
    "校园卡管理中心" → 主名不以教学词结尾 → 不豁免"""
    main = re.sub(r'[（(].*?[)）]', '', name).strip()
    return main.endswith(_TEACH_END)


def _norm(url):
    # 归一化: 去尾斜杠、统一 http/https 为 https
    # ⚠️ 保留 query string — 央美等站点用 ?j=101 区分不同院系
    u = url.rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u


def _inst_endswith(name):
    """名称以机构词结尾 — 同 URL 多文本时判断哪个更像机构"""
    return name.endswith(("学院", "书院", "系", "学部", "研究院", "研究所",
                          "实验室", "教学部", "教研部", "中心", "基地", "医院", "部", "所"))


def _dedup_inst(seen, seen_names, out, key, c):
    """同 key 去重(机构词优先):
    同名重复 → 丢弃; 机构词覆盖非机构词(辽大"留学辽大"→"国际教育学院");
    双机构词不同名 → 并列保留(云大"云南研究院"+"历史文化研究中心"; 药科 hub 页 26 机构)"""
    name = c.get("name", "")
    if key in seen:
        _old = seen[key]
        _names = seen_names.setdefault(key, set())
        if name in _names:
            return
        if _inst_endswith(name) and not _inst_endswith(_old.get("name", "")):
            out[out.index(_old)] = c
            seen[key] = c
        elif (_inst_endswith(name) and _inst_endswith(_old.get("name", ""))
              and name != _old.get("name", "")):
            out.append(c)
            seen[key] = c
        else:
            return
        _names.add(name)
        return
    seen[key] = c
    seen_names.setdefault(key, set()).add(name)
    out.append(c)


def _label(url):
    return urlparse(url).netloc.split(".")[0]


def _collect_containers(soup, selectors):
    """css_selectors 命中的容器元素集合 — 宽提取分支只扫容器内部 (提取严格化)"""
    els = set()
    for sel in selectors:
        try:
            for e in soup.select(sel):
                els.add(e)
                # 选择器命中 <a> 时, 其祖先(li/ul/div…)也是容器 — 无链接文本节点按容器归属判定
                for p in e.parents:
                    if p.name in ("html", "body"):
                        break
                    els.add(p)
        except Exception:
            pass
    return els


def _related(el, containers):
    """候选元素与容器集合有祖先-后代关系 (候选在容器内 或 候选本身就是容器)"""
    if el in containers:
        return True
    return any(p in containers for p in el.parents)


# Step 1: CSS 提取全部候选（不做关键词过滤）
def _extract_all(page):
    fm = page.get("fetch_method", "smart")
    if fm == "cloakbrowser" or fm == "drission":
        html, method = _try_drission(page["url"], 20)
    elif fm == "cdp":
        html, method = _try_cdp(page["url"], 20)
        # CDP 失败(挂起资源/连接异常) → 降级 smart_fetch(curl_cffi 直连), 避免整页 0 候选
        if not html:
            html, method = smart_fetch(page["url"])
    else:
        html, method = smart_fetch(page["url"])
    if not html:
        print(f"  [fetch fail] {{page['url'][:60]}} ({{method}})")
        return []
    soup = BeautifulSoup(html, "html.parser")

    # ═══ 删除 footer/header/nav 等非正文区域 ═══
    # 1. <footer> 标签
    for f in soup.find_all(["footer"]):
        f.decompose()
    # 2. class/id 含 footer 的元素 (如 class="wrap-footer clearFix")
    for f in soup.find_all(True, attrs={{"class": lambda v: v and "footer" in " ".join(v).lower()}}):
        f.decompose()
    for f in soup.find_all(True, attrs={{"id": lambda v: v and "footer" in v.lower()}}):
        f.decompose()
    # 3. HTML 注释标记区域: <!-- footer开始 --> 到 <!-- footer结束 --> 之间的兄弟节点
    #    (BeautifulSoup 中注释是 Comment 对象)
    from bs4 import Comment
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment) and "footer开始" in str(s)):
        node = comment
        while node is not None:
            nxt = node.next_sibling
            node.extract()
            if isinstance(nxt, Comment) and "footer结束" in str(nxt):
                nxt.extract()
                break
            node = nxt
    # 4. 导航菜单: <nav> 及 class 含 nav/menu 但非 content/main/article 的容器
    #    例外: 容器内 ≥60% 链接以机构词结尾 → 是"导航即学院列表"站点(南林大下拉学院菜单), 保留
    _nav_cands = list(soup.find_all(["nav"]))
    _nav_cands += [f for f in soup.find_all(True, attrs={{"class": lambda v: v and ("nav" in " ".join(v).lower() or "menu" in " ".join(v).lower())
                                                          and not any(k in " ".join(v).lower() for k in ["content", "main", "article"])}})]
    for f in _nav_cands:
        _links = f.find_all("a")
        _inst = sum(1 for a in _links
                    if len(a.get_text(strip=True)) >= 4
                    and a.get_text(strip=True).endswith(("学院", "书院", "研究院", "研究所", "实验室",
                                                         "学部", "系", "教学部", "中心", "基地")))
        if _links and (_inst >= 10 or _inst / len(_links) >= 0.6):
            continue  # 机构列表导航(学院下拉≥10个机构链接, 南林大等) — 保留
        f.decompose()

    out, seen, seen_names = [], {{}}, {{}}
    # 源页面 URL 集合 — 用于排除"指向源页面自身"的导航链接
    for sel in page.get("css_selectors", ["a[href]"]):
        try:
            for a in soup.select(sel):
                name = a.get_text(strip=True)
                # 清洗: 面包屑/导航分隔符 ">" ("本科教育 > 更多" → "本科教育更多")
                name = re.sub(r'[>＞»›⟶→]', '', name)
                # 清洗: 全角空格、首尾空白
                name = name.strip()
                # 清洗: 名称内部的空格/全角空格("教 务 处" → "教务处");
                #   含 ASCII 字母的名称保留英文单词间隔 (上外"卓越学院 / Honors College"中英双语链接)
                if re.search(r'[A-Za-z]', name):
                    name = re.sub(r'[\\s　]+', ' ', name).strip()
                else:
                    name = re.sub(r'[\\s　]+', '', name)
                # 清洗: 列表符号 ■ ◆ ● ▶ ▲ ▼ • · 等
                name = re.sub(r'[■◆●▶▲▼•·✦✧➤]', '', name)
                # 清洗: 转义标签残留 ("未来农业研究院<br>（学科交叉中心）" — HTML实体转义的字面标签)
                name = re.sub(r'<br', '', name)
                name = name.lstrip('/').strip()
                # 尾部顿号/逗号残留("教务处、""信息化办公室、")是导航截断 → 清掉标点
                name = name.rstrip('、，,；;')
                # 全括号子标题 ("（境外学生事务中心）"是主机构下的子项, 剥括号后按内容判断; 真机构名不会整名带括号)
                if name.startswith("（") and name.endswith("）") and len(name) > 4:
                    name = name[1:-1].strip()
                # 编号前缀 ("三、教育部重点实验室"等列表序号残留; 真机构名不会以编号开头)
                name = re.sub(r'^(?:[一二三四五六七八九十百]+|[0-9]+)[、．.、]', '', name)
                # 前导连字符 ("－电影学院" 全角/半角连字符列表标记)
                name = re.sub(r'^[－-]+', '', name)
                # 首尾边界清洗: 特殊字符与空白 ("> - 、·，。：…" 列表标记/导航残留)
                name = re.sub(r'^[-\\s>》－+|/、·,，。．:：…*]+', '', name)
                name = re.sub(r'[-\\s>》－+|/、·,，。．:：…*]+$', '', name)
                # 半角方括号栏目标记 ("[分析测试中心]")
                name = re.sub(r'^\\[(.*)\\]$', r'\1', name)
                # 尾部 "更多" 残留 ("本科教育更多" → "本科教育")
                if name.endswith("更多") and len(name) > 4:
                    name = name[:-2].rstrip('、，,；;')
                # 尾部批准年份 ("XX重点实验室2019年" → "XX重点实验室"; 哈工程实验室列表年份标注)
                name = re.sub(r'\\d{{4}}年$', '', name)
                # "请访问"提示文字前缀 ("了解更多科研信息，请访问XX研究院" → "XX研究院")
                name = re.sub(r'^.+?请访问', '', name)
                # 孤立前导括号 ("（轻合金研究院" 链接文本自带左括号 → 剥掉; 真机构名不会以括号开头)
                name = re.sub(r'^[（(]+', '', name).strip()
                href = a.get("href", "")
                src_tag = "css"
                # javascript: 链接 — 真机构名不会用 js 链接(中戏新闻列表特征);
                # 例外: keep_js_inst=True 时, js 链接但名称是短机构名(海大"深圳研究院"等官网列出无网站的单位)
                if href.startswith("javascript"):
                    if not (page.get("keep_js_inst", False)
                            and len(name) <= 12
                            and any(k in name for k in ["学院", "书院", "系", "研究院", "研究所", "实验室", "学部", "教学部"])):
                        continue
                    src_tag = "js"
                    href = ""  # 无 URL 机构
                # 无 href 但名称含教学机构关键词 → 保留为无URL院系(浙大纯文本学院列表)
                if not href and not any(k in name for k in ["学院", "书院", "系", "研究院",
                                                            "研究所", "实验室", "学部", "教学部"]):
                    continue
                # 新闻标题特征: "系名系名标题" 重复机构名 (中戏模式)
                # 例外: 以机构关键词结尾的是并列/别名真机构名(集成电路学院（微电子学院）), 跳过检测
                #   剥尾部括号/斜杠再判断 — 循环剥完整括号组, 支持嵌套 ("X重点实验室（Y实验室（筹））"
                #   → 剥"（筹）"再剥"（Y实验室）" → 尾"实验室"是机构词, 不误杀)
                _tail = name
                while True:
                    # 嵌套括号叠合 ("（筹））"): 先剥尾部括号字符, 再剥残留"（xxx"开放组
                    _t2 = re.sub(r'[（）()/、\\s]+$', '', _tail)
                    _t2 = re.sub(r'[（(][^（()）]*$', '', _t2)
                    if _t2 == _tail:
                        break
                    _tail = _t2
                if len(name) > 10 and not re.search(r'[A-Za-z]', name) and not _tail.endswith(("学院", "书院", "系", "研究院", "研究所",
                                                                                             "实验室", "学部", "教学部", "中心", "基地")):
                    for kw in ["系", "学院", "研究院", "研究所", "实验室", "书院"]:
                        if name.count(kw) >= 2:
                            name = ""
                            break
                if not name or len(name) < 2 or len(name) > 80:
                    continue
                # 天津医科大学模式: 表格两列"机构名 | 访问网址"(链接文本是URL字符串)
                #   <tr><td>基础医学院</td><td><a href="http://bms.tmu.edu.cn/">http://bms.tmu.edu.cn/</a></td></tr>
                #   → 机构名取同行前一个单元格, 链接保留为URL (否则被下方"无中文"规则拦掉)
                if re.match(r'^https?://', name):
                    _tm_row = a.find_parent("tr")
                    if _tm_row is not None:
                        _tm_tds = _tm_row.find_all("td")
                        _tm_a_td = next((i for i, _td in enumerate(_tm_tds) if a in _td.descendants), None)
                        if _tm_a_td is not None and _tm_a_td > 0:
                            _tm_lname = _tm_tds[_tm_a_td - 1].get_text(strip=True)
                            if _tm_lname and not re.match(r'^https?://', _tm_lname):
                                name = _tm_lname
                if not re.search(r'[一-鿿]', name):
                    continue
                if href.startswith("#"):
                    continue
                # 残缺 URL: "http:/x" (截断) / "http:///x" (三斜杠) — 畸形链接不产生机构候选
                _hp = href.lower()
                if _hp.startswith(("http:", "https:")) and not _hp.startswith(("http://", "https://")):
                    continue
                if _hp.startswith(("http:///", "https:///")):
                    continue
                if any(p in href.lower() for p in EXCLUDE_URL):
                    continue
                # 新闻 URL 硬排除: /YYYY/MMDD/ 或 /info/数字/ 模式
                if re.search(r'/\\d{{4}}/\\d{{4}}/', href) or re.search(r'/info/\\d+/', href):
                    continue
                if EXCLUDE_NAME and any(re.match(p, name) for p in EXCLUDE_NAME):
                    continue
                # link.jsp?bmmc=xxx → 真实子域 xxx.<domain>
                if "link.jsp" in href and "bmmc=" in href:
                    bm = re.search(r'bmmc=([\\w-]+)', href)
                    if bm:
                        host = urlparse(page["url"]).netloc
                        parts = host.split(".")
                        if len(parts) >= 3:
                            domain = ".".join(parts[1:])  # 去掉 www
                        else:
                            domain = host
                        full = f"https://{{bm.group(1)}}.{{domain}}/"
                    else:
                        continue
                else:
                    full = urljoin(page["url"], href) if href else ""
                # 同 URL 可能并列多个机构 (云大"云南研究院/历史文化研究中心"都指向机构目录页自身;
                # 辽大"留学辽大/国际教育学院"共用一个链接) — 同 URL 去重, 机构词优先:
                #   机构词文本覆盖非机构词 / 双机构词不同名都保留, 交 LLM 层判断
                key = _norm(full) if full else "TEXT:" + name
                c = {{"name": name, "url": full, "source_page": page["label"], "src": src_tag}}
                c["is_source_page"] = bool(full) and _norm(full) == _norm(page["url"])
                c["is_nav_hub"] = bool(full) and _is_nav_hub(name, full, page["url"])
                _dedup_inst(seen, seen_names, out, key, c)
        except Exception:
            pass

    # ── JS 内嵌数据提取 (浙大模式: list._college = [[title:'文学院', link:'http://www.lit.zju.edu.cn/'], ...]) ──
    # 高校导航/学院列表常以内嵌 JS 数组渲染(React/Vue/原生JS), 链接不在 <a href> 中;
    # 仅取 title 以机构词结尾的条目, 排除导航/栏目类标题(附属医院/学校机构/直属单位等)
    _JS_NAV_SUBSTR = ("附属", "机构", "单位", "企业", "设置", "列表", "栏目", "首页", "概况", "简介",
                      "招聘", "校友", "党建", "图书", "专题", "站点", "关于", "后勤", "机关", "财务")
    for _m in re.finditer(r"title:\\s*['\\\"]([^'\\\"]{{2,16}})['\\\"]\\s*,?\\s*[\\s\\S]{{0,120}}?link:\\s*['\\\"]([^'\\\"]+)['\\\"]",
                          html):
        _jn = _m.group(1).strip()
        _jl = _m.group(2).strip()
        if not _jn or not re.search(r'[一-鿿]', _jn):
            continue
        if not _jn.endswith(("学院", "书院", "系", "学部", "研究院", "研究所", "实验室",
                             "教学部", "中心", "基地", "医院")):
            continue
        if any(_s in _jn for _s in _JS_NAV_SUBSTR):
            continue
        _jn = re.sub(r'^[－-]+', '', _jn).strip()
        if len(_jn) < 2:
            continue
        if _jl.startswith(("javascript", "#")) or not _jl.startswith(("http://", "https://", "/")):
            continue
        _jfull = urljoin(page["url"], _jl) if _jl.startswith("/") else _jl
        # 同名候选去重: JS 数据版(有 URL)覆盖无 URL 版; 同名已有 URL → 跳过
        _dup = next((i for i, _o in enumerate(out) if _o.get("name") == _jn), None)
        if _dup is not None:
            if out[_dup].get("url"):
                continue
            out[_dup] = {{"name": _jn, "url": _jfull, "source_page": page["label"], "src": "jsdata"}}
            out[_dup]["is_source_page"] = bool(_jfull) and _norm(_jfull) == _norm(page["url"])
            out[_dup]["is_nav_hub"] = _is_nav_hub(_jn, _jfull, page["url"])
            continue
        _jc = {{"name": _jn, "url": _jfull, "source_page": page["label"], "src": "jsdata"}}
        _jc["is_source_page"] = bool(_jfull) and _norm(_jfull) == _norm(page["url"])
        _jc["is_nav_hub"] = _is_nav_hub(_jn, _jfull, page["url"])
        _dedup_inst(seen, seen_names, out, _norm(_jfull), _jc)

    # ── 无链接的院系文本提取 (人大模式: <h5><i>■</i>新民书院</h5> 无 <a>) ──
    # 查找含教学机构关键词但无 <a> 子元素的容器元素
    # 容器约束: 默认只扫 css_selectors 命中容器内部 (提取严格化); config 显式 "page" 时整页扫描
    tn_mode = page.get("extract_text_nodes", True)
    if tn_mode:
        _containers = None if tn_mode == "page" else _collect_containers(
            soup, page.get("css_selectors", ["a[href]"]))
        text_tags = ["h5", "h4", "h3", "li", "span", "div", "p", "dd"]
        for tag in text_tags:
            for el in soup.find_all(tag):
                if _containers is not None and not _related(el, _containers):
                    continue
                # 跳过含 <a> 的元素(已在上面处理过)
                if el.find("a"):
                    continue
                name = el.get_text(strip=True)
                if not name or len(name) < 3 or len(name) > 40:
                    continue
                # 只保留纯机构名(不含长句子)
                name = re.sub(r'[>＞»›⟶→]', '', name)
                # 含 ASCII 字母的名称保留英文单词间隔 (上外中英双语链接)
                if re.search(r'[A-Za-z]', name):
                    name = re.sub(r'[\\s　]+', ' ', name).strip()
                else:
                    name = re.sub(r'[\\s　]+', '', name)
                name = re.sub(r'[■◆●▶▲▼•·✦✧➤]', '', name).strip()
                # 清洗: 转义标签残留 ("未来农业研究院<br>（学科交叉中心）")
                name = re.sub(r'<br', '', name)
                # 编号前缀 ("三、教育部重点实验室"列表序号残留)
                name = re.sub(r'^(?:[一二三四五六七八九十百]+|[0-9]+)[、．.、]', '', name)
                # 前导连字符 ("－电影学院" 全角/半角连字符列表标记)
                name = re.sub(r'^[－-]+', '', name)
                # 首尾边界清洗: 特殊字符与空白 ("> - 、·，。：…" 列表标记/导航残留)
                name = re.sub(r'^[-\\s>》－+|/、·,，。．:：…*]+', '', name)
                name = re.sub(r'[-\\s>》－+|/、·,，。．:：…*]+$', '', name)
                # 尾部批准年份 ("XX重点实验室2019年" → "XX重点实验室")
                name = re.sub(r'\\d{{4}}年$', '', name)
                # "请访问"提示文字前缀 ("了解更多科研信息，请访问XX研究院" → "XX研究院")
                name = re.sub(r'^.+?请访问', '', name)
                # 孤立前导括号 ("（华西医院" 文本节点自带左括号 → 剥掉)
                name = re.sub(r'^[（(]+', '', name).strip()
                # 必须含教学机构关键词且是短名称
                if not any(k in name for k in ["学院", "书院", "系", "研究院", "研究所", "实验室", "学部", "教学部"]):
                    continue
                # 纯并列分类词排除 (厦大 jg-title 教训: "部、系" "部、系、中心" —
                #   分隔符连接的组织词并列, 无实体名 → 跳过)
                #   只按"段"判定, 不按字符: "中医系"/"实验医学系"是实体名不受影响
                #   ("学部院系"等无分隔符并列词由 HARD_DROP 硬排除层处理)
                _ORG_ONLY_WORDS = {{"学院", "书院", "系", "研究院", "研究所", "实验室",
                                   "学部", "教学部", "中心", "基地", "医院", "部",
                                   "科", "室", "所", "教研室", "教研部", "学系"}}
                if any(s in name for s in "、,，"):
                    _parts = [p for p in re.split(r"[-－、,，/／]", name) if p]
                    if _parts and all(p in _ORG_ONLY_WORDS for p in _parts):
                        continue
                # 长名称(>10字)必须以机构关键词结尾 ("实验艺术与科技艺术学院"✓, "电影电视系学生在拍摄影片"✗)
                #   嵌套括号名("X重点实验室（Y实验室（筹））")剥完整括号组后再判断, 避免"（筹）"误杀
                _t3 = name
                while True:
                    _t4 = re.sub(r'[（）()/、\\s]+$', '', _t3)
                    _t4 = re.sub(r'[（(][^（()）]*$', '', _t4)
                    if _t4 == _t3:
                        break
                    _t3 = _t4
                if len(name) > 10 and not _t3.endswith(("学院", "书院", "系", "研究院", "研究所",
                                                         "实验室", "学部", "教学部", "基础部", "中心", "基地")):
                    continue
                # 排除长句(新闻标题)
                if len(name) > 20:
                    continue
                c = {{"name": name, "url": "", "source_page": page["label"],
                     "is_text_node": True, "src": "text"}}
                # 文本节点没有 URL,用名称去重
                if name not in seen:
                    seen[name] = c
                    out.append(c)

    # ── 标题+介绍卡片模式 (北林大模式: <li><h2>林学院</h2><a>介绍文...</a></li>) ──
    # h2/h3/h5 是机构名, 同级 <a> 是官网链接, a 文本是长介绍
    # 容器约束: 默认只扫 css_selectors 命中容器内部; config 显式 "page" 时整页扫描
    tc_mode = page.get("extract_title_card", True)
    if tc_mode:
        _containers = None if tc_mode == "page" else _collect_containers(
            soup, page.get("css_selectors", ["a[href]"]))
        for h in soup.find_all(["h2", "h3", "h4", "h5"]):
            if _containers is not None and not _related(h, _containers):
                continue
            title = h.get_text(strip=True)
            title = re.sub(r'[>＞»›⟶→]', '', title)
            # 含 ASCII 字母的名称保留英文单词间隔
            if re.search(r'[A-Za-z]', title):
                title = re.sub(r'[\\s　]+', ' ', title).strip()
            else:
                title = re.sub(r'[\\s　]+', '', title)
            title = re.sub(r'[■◆●▶▲▼•·✦✧➤]', '', title).strip()
            # 清洗: 转义标签残留 ("未来农业研究院<br>（学科交叉中心）")
            title = re.sub(r'<br', '', title)
            # 编号前缀 ("三、教育部重点实验室"列表序号残留)
            title = re.sub(r'^(?:[一二三四五六七八九十百]+|[0-9]+)[、．.、]', '', title)
            # 前导连字符 ("－电影学院" 全角/半角连字符列表标记)
            title = re.sub(r'^[－-]+', '', title)
            # 首尾边界清洗: 特殊字符与空白 ("> - 、·，。：…" 列表标记/导航残留)
            title = re.sub(r'^[-\\s>》－+|/、·,，。．:：…*]+', '', title)
            title = re.sub(r'[-\\s>》－+|/、·,，。．:：…*]+$', '', title)
            # 尾部批准年份 ("XX重点实验室2019年" → "XX重点实验室")
            title = re.sub(r'\\d{{4}}年$', '', title)
            # "请访问"提示文字前缀 ("了解更多科研信息，请访问XX研究院" → "XX研究院")
            title = re.sub(r'^.+?请访问', '', title)
            # 孤立前导括号 ("（XX学院" 标题自带左括号 → 剥掉)
            title = re.sub(r'^[（(]+', '', title).strip()
            if not title or len(title) < 3 or len(title) > 25:
                continue
            if not any(k in title for k in ["学院", "书院", "研究院", "研究所", "实验室", "系", "学部", "教学部"]):
                continue
            # 官网链接: 优先 h 内部的 <a> (厦大模式: <h2 class="tit"><a href="...">药学院</a></h2>)
            # 回退 h 父容器内第一个 <a> (北林大模式: <li><h2>林学院</h2><a>官网</a></li>)
            container = h.parent
            link_url = ""
            a = h.find("a", href=True)
            if not a and container:
                a = container.find("a", href=True)
            if a and a["href"] and not a["href"].startswith(("#", "javascript")):
                # 残缺 URL ("http:/x") 不产生链接
                _hp = a["href"].lower()
                if _hp.startswith(("http:", "https:")) and not _hp.startswith(("http://", "https://")):
                    link_url = ""
                elif _hp.startswith(("http:///", "https:///")):
                    link_url = ""
                else:
                    link_url = urljoin(page["url"], a["href"])
                # 跳过新闻/文章链接
                if link_url and (re.search(r'/\\d{{4}}/\\d{{4}}/', link_url) or re.search(r'/info/\\d+/', link_url)):
                    link_url = ""
            c = {{"name": title, "url": link_url, "source_page": page["label"], "src": "title"}}
            key = _norm(link_url) if link_url else "TITLE:" + title
            _dedup_inst(seen, seen_names, out, key, c)


    return out


def _backfill_intro_urls(candidates):
    """介绍页 URL 回填 (天大模式): 列表页链接指向 /info/ 文章页时,
    抓文章页提取"学院网址"字段作为真实独立站 URL。失败保持原值。"""
    import concurrent.futures as cf
    intro = [c for c in candidates
             if "/info/" in (c.get("url") or "") or "/content/" in (c.get("url") or "")
             or "/article/" in (c.get("url") or "")]
    if not intro:
        return 0
    print(f"  介绍页回填: {{len(intro)}} 条候选, 抓取'学院网址'字段...")
    done = 0

    def backfill(c):
        nonlocal done
        try:
            html, _m = smart_fetch(c["url"], timeout=12)
            if not html:
                return
            m = re.search(r'学院网址.*?([a-zA-Z0-9][a-zA-Z0-9.\-/:]{{4,80}})', html, re.S)
            if not m:
                return
            val = m.group(1).strip().rstrip("/")
            if not val.startswith("http"):
                val = "http://" + val
            if re.match(r"^https?://[a-zA-Z0-9.\-]+", val):
                c["url"] = val
                c["intro_backfilled"] = True
                done += 1
        except Exception:
            pass

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(backfill, intro))
    print(f"  介绍页回填完成: {{done}}/{{len(intro)}} 命中'学院网址'字段")
    return done


def _resolve_redirect_urls(candidates):
    """跳转参数 URL 解析 (上中医模式): _redirect/link.jsp 等跳转链接 → 跟随重定向取真实子域 URL。"""
    import subprocess as _sp, concurrent.futures as cf
    redirs = [c for c in candidates
              if any(k in (c.get("url") or "") for k in ("_redirect", "link.jsp",
                                                          "redirect", "jump"))]
    if not redirs:
        return 0
    print(f"  跳转URL解析: {{len(redirs)}} 条候选, 跟随重定向...")
    done = 0

    def resolve(c):
        nonlocal done
        try:
            r = _sp.run(["curl", "-sL", "-m", "12", "-o", "/dev/null",
                         "-w", "%{{url_effective}}", c["url"]],
                        capture_output=True, text=True, timeout=18)
            eff = r.stdout.strip()
            if eff and re.match(r"^https?://", eff) and eff != c["url"]:
                c["url"] = eff
                c["redirect_resolved"] = True
                done += 1
        except Exception:
            pass

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(resolve, redirs))
    print(f"  跳转解析完成: {{done}}/{{len(redirs)}} 解析出真实子域")
    return done


def _is_nav_hub(name, full_url, source_url):
    """判断是否为导航 hub 页链接（如协和的'直属医院'/'医院'/'研究所'/'基地平台'）。
    特征: URL 与源页面同目录/同站、名称是通用分类词而非具体机构名。"""
    # 通用导航词 — 扩展: 含侧边栏/顶部菜单的泛化分类词
    nav_words = ["直属医院", "医院", "研究所", "学院", "研究院", "基地平台", "实验室",
                 "教学单位", "科研单位", "附属医院", "直属所院", "培养单位", "院系中心",
                 "研究院所", "重点实验室",
                 "教育教学", "本科教育", "研究生教育", "继续教育", "临床医疗",
                 "智库建设", "学术咨询", "科学研究", "师资队伍", "人才建设",
                 "信息公开", "合作交流", "院校概况", "组织机构", "党的建设"]
    if name not in nav_words:
        return False
    # URL 指向主站 hub 页（含 /index 或 /list 或与源页面同目录）
    from urllib.parse import urlparse as _up
    src = _up(source_url)
    dst = _up(full_url)
    if src.netloc != dst.netloc:
        return False
    if "/index" in dst.path or "/list" in dst.path or dst.path.rstrip("/") == src.path.rstrip("/"):
        return True
    # 主站泛化路径: 二级目录如 /jyjx/index.htm /lcyl/index.htm /xszxwyh/index.htm
    parts = dst.path.strip("/").split("/")
    if len(parts) >= 2 and parts[-1].startswith("index"):
        return True
    return False


# Step 2: 关键词建议标注
def _kw_suggest(candidates):
    for c in candidates:
        name = c["name"]
        # 硬排除优先 (先于 STRONG_KEEP, 避免"院系设置"含"系"被标成保留)
        if _hard_drop_reason(name):
            c["kw_suggest"] = {{"label": "warning", "reason": "硬排除(导航/行政/医院内部)"}}
        # 强排除词 (弱排除词与强教学词共存时豁免: "国际交流学院"是真学院;
        # 主名以教学词结尾也豁免: "文物与博物馆学系"含"博物馆"但它是真系)
        elif any(k in name for k in STRONG_EXCLUDE) and not (
                any(w in name for w in _WEAK_EXCLUDE) and any(t in name for t in _STRONG_TEACH_WORD)
        ) and not _main_ends_teaching(name):
            c["kw_suggest"] = {{"label": "warning", "reason": "强排除词"}}
        elif any(k in name for k in STRONG_KEEP):
            c["kw_suggest"] = {{"label": "checkmark", "reason": "强保留词"}}
        elif any(k in name for k in EXCLUDE_KW):
            c["kw_suggest"] = {{"label": "warning", "reason": "排除词命中也可能误杀"}}
        elif any(k in name for k in INCLUDE_KW):
            c["kw_suggest"] = {{"label": "checkmark", "reason": "包含词命中"}}
        else:
            c["kw_suggest"] = {{"label": "unsure", "reason": "无明确关键词"}}
    return candidates


# Step 3: 过滤清单判断 (agent 阶段 LLM 生成 filter_list.json, 这里只执行)
def _llm_final_filter(candidates):
    """基于 filter_list.json (drop/keep 清单) + 关键词规则执行过滤。"""
    all_d, kept = [], []
    for c in candidates:
        name = c["name"]
        # 预过滤: 导航 hub 页
        if c.get("is_source_page") or c.get("is_nav_hub"):
            c["llm_keep"] = False
            c["llm_reason"] = "导航hub页(指向源页面/分类汇总页),非具体机构"
            c["type"] = ""
            all_d.append(c)
            continue
        # 0. 硬排除 (优先于 filter_list, 导航标题/行政中心/医院内部页不保留)
        hard = _hard_drop_reason(name)
        if hard:
            c["llm_keep"] = False
            c["llm_reason"] = hard
            c["type"] = ""
            all_d.append(c)
            continue
        # 0.5 组合名拆分: "动物科技学院、动物医学院" 多机构并列 → 保留
        # (任一段以教学词结尾即真机构; 任一段命中硬排除 → 不豁免, 走后续过滤)
        combo_parts = [p.strip() for p in re.split(r'[、·]', name) if p.strip()]
        if len(combo_parts) >= 2 and not any(_hard_drop_reason(p) for p in combo_parts) \
                and any(_main_ends_teaching(p) for p in combo_parts):
            c["llm_keep"] = True
            c["llm_reason"] = "组合名拆分(多机构并列),含教学单位"
            c["type"] = _guess_type(name)
            all_d.append(c)
            kept.append(c)
            continue
        # 1. 明确排除清单 (agent 阶段 LLM 判定)
        if name in DROP_LIST:
            c["llm_keep"] = False
            c["llm_reason"] = "filter_list drop"
            c["type"] = ""
            all_d.append(c)
            continue
        # 2. 明确保留清单 (agent 阶段 LLM 判定)
        if name in KEEP_LIST:
            c["llm_keep"] = True
            c["llm_reason"] = "filter_list keep"
            c["type"] = _guess_type(name)
            all_d.append(c)
            kept.append(c)
            continue
        # 3. 关键词规则兜底
        kw_label = c.get("kw_suggest", {{}}).get("label", "unsure")
        # 行政后缀硬排除 (教学单位以 学院/系/学部/研究院 等结尾的不受影响)
        admin_suffix = any(name.endswith(s) for s in ["处", "室", "会", "馆", "委", "组"])
        teaching_kw = any(k in name for k in ["学院", "研究院", "研究所", "实验室",
                                              "书院", "学部", "基地", "医院", "系"])
        teaching_end = name.endswith(("学院", "书院", "系", "学部", "研究院", "研究所",
                                      "实验室", "教学部", "教研部", "附属医院"))
        # 教育导航词
        edu_nav_kw = any(k in name for k in ["本科生教育", "本科生教学", "研究生教育",
                                             "留学生教育", "培训与继续教育", "成人教育",
                                             "网络教育", "开放教育", "科研系统", "校际关系",
                                             "国际教育", "招生", "本科教学", "研究生培养",
                                             "本科教育"])
        # 教育导航词豁免: 含真教学单位词尾 ("本科生院"含"本科生教育"但它是机构;
        # "研究生院"同理; 研究院/研究所/学部/书院 一并豁免)
        if edu_nav_kw and any(t in name for t in ("学院", "系", "研究院", "研究所",
                                                  "学部", "书院")):
            edu_nav_kw = False
        # 分类标题
        category_kw = name in ["全国重点实验室", "教育部重点实验室", "省级重点实验室",
                               "市级重点实验室", "国家工程研究中心", "省部级实验室",
                               "人才培养基地", "科研基地", "辽宁省实验室", "辽宁省重点实验室",
                               "辽宁省工程（技术）研究中心、工程实验室",
                               "国家地方联合工程研究中心（工程实验室）"]
        # 文化/宣传类
        culture_kw = any(k in name for k in ["校园服务", "社区", "教工之家", "讲座", "社团",
                                             "科技创", "社会实", "志愿", "体育活动", "文艺",
                                             "大学文化", "新视点", "校训", "光影", "文化",
                                             "活动", "继续教育", "院系中心", "校园生活",
                                             "实践体系", "成果发布", "建设与管理中心"])
        valid_dept_name = teaching_kw or name.endswith(("学院","书院","系","部","所","中心","基地","实验室"))
        if admin_suffix and not teaching_end:
            c["llm_keep"] = False
            c["llm_reason"] = "行政后缀(处/室/会/馆/委/组),非教学科研"
        elif edu_nav_kw:
            c["llm_keep"] = False
            c["llm_reason"] = "教育层次/系统导航,非院系机构"
        elif category_kw:
            c["llm_keep"] = False
            c["llm_reason"] = "分类标题(非具体机构)"
        elif culture_kw and not teaching_kw:
            c["llm_keep"] = False
            c["llm_reason"] = "文化/生活/宣传类,非教学科研"
        # 行政部 (以"部"结尾但无教学关键词: 合作与发展部/发展规划部等; INCLUDE_KW 的裸"部"会误标 checkmark)
        # "学部"结尾豁免 — "自然科学学部"是真实学部 (teaching_end 也认"学部")
        elif name.endswith("部") and not name.endswith("学部") and "管理" not in name and not any(k in name for k in ["教学", "教研", "体育", "外语",
                                                                                                                       "基础", "思政", "课", "通识",
                                                                                                                       "人文", "社科", "数理", "美育",
                                                                                                                       "军事", "艺术"]):
            c["llm_keep"] = False
            c["llm_reason"] = "行政部(非教学单位): 合作与发展部等"
        # 部级教学单位 (体育部/外语部/基础部/思政教研部/公共课部等; 行政部如"合作与发展部"不命中)
        elif name.endswith("部") and "管理" not in name and any(k in name for k in ["教学", "教研", "体育", "外语",
                                                                                   "基础", "思政", "课", "通识",
                                                                                   "人文", "社科", "数理", "美育",
                                                                                   "军事", "艺术"]):
            c["llm_keep"] = True
            c["llm_reason"] = "教学单位(部级): 体育部/外语部/教研部等"
        elif kw_label == "warning":
            c["llm_keep"] = False
            c["llm_reason"] = "关键词规则排除"
        elif kw_label == "checkmark" and valid_dept_name:
            c["llm_keep"] = True
            c["llm_reason"] = "关键词规则保留"
        else:
            c["llm_keep"] = False
            c["llm_reason"] = "未在保留清单且无教学关键词,排除"
        c["type"] = _guess_type(name) if c["llm_keep"] else ""
        all_d.append(c)
        if c["llm_keep"]:
            kept.append(c)
    return kept, all_d


def _guess_type(name):
    for kw in ["学院","研究院","研究所","学部","书院","实验室","基地","医院","系","中心","部"]:
        if kw in name:
            return kw
    return "学院"


# Step 4: 审计文件
def _write_filter_log(all_candidates, kept, page_labels, start_time):
    n_total = len(all_candidates)
    kw_keep = sum(1 for c in all_candidates if c.get("kw_suggest",{{}}).get("label")=="checkmark")
    kw_excl = sum(1 for c in all_candidates if c.get("kw_suggest",{{}}).get("label")=="warning")
    kw_unc  = sum(1 for c in all_candidates if c.get("kw_suggest",{{}}).get("label")=="unsure")
    n_kept = len(kept)
    n_drop = n_total - n_kept
    kw_fp = [c for c in all_candidates if c.get("kw_suggest",{{}}).get("label")=="warning" and c.get("llm_keep")]
    kw_fn = [c for c in all_candidates if c.get("kw_suggest",{{}}).get("label")=="checkmark" and not c.get("llm_keep")]

    md = []
    md.append("# {sn} — 院系过滤审计")
    md.append("")
    md.append(f"- 采集时间: {{start_time}}")
    md.append(f"- 源页面: {{len(SOURCE_PAGES)}}个 ({{', '.join(page_labels)}})")
    md.append(f"- CSS提取: {{n_total}} 候选")
    md.append(f"- LLM保留: {{n_kept}} | LLM排除: {{n_drop}}")
    filter_rate = n_drop / n_total if n_total else 0
    if filter_rate > 0.4:
        md.append(f"- ⚠️ 过滤率 {{filter_rate:.0%}} — 选择器偏宽, 建议回环节1收窄 css_selectors")
    elif filter_rate > 0.15:
        md.append(f"- ⚠️ 过滤率 {{filter_rate:.0%}} — 偏宽, 可考虑收窄选择器")
    else:
        md.append(f"- ✅ 过滤率 {{filter_rate:.0%}} — 提取健康, 过滤仅为兜底")
    md.append("")
    md.append("## 统计")
    md.append(f"| 指标 | 数量 |")
    md.append(f"|------|:--:|")
    md.append(f"| 关键词建议保留 | {{kw_keep}} |")
    md.append(f"| 关键词建议排除 | {{kw_excl}} |")
    md.append(f"| 关键词待定 | {{kw_unc}} |")
    md.append(f"| LLM 最终保留 | {{n_kept}} |")
    md.append(f"| 关键词误杀 | {{len(kw_fp)}} |")
    md.append(f"| 关键词漏网 | {{len(kw_fn)}} |")
    md.append("")
    if kw_fp:
        md.append("## 关键词误杀（LLM纠正）")
        for c in kw_fp:
            md.append(f"- {{{{c['name']}}}}: 关键词建议排除, LLM: {{{{c.get('llm_reason','')}}}}")
        md.append("")
    if kw_fn:
        md.append("## 关键词漏网（LLM纠正）")
        for c in kw_fn:
            md.append(f"- {{{{c['name']}}}}: 关键词建议保留, LLM: {{{{c.get('llm_reason','')}}}}")
        md.append("")
    md.append("## 完整过滤记录")
    md.append("| # | 名称 | 关键词建议 | LLM决策 | LLM理由 | 类型 |")
    md.append("|---|------|:--:|:--:|------|------|")
    for i, c in enumerate(all_candidates, 1):
        kwl = c.get("kw_suggest",{{}}).get("label","unsure")
        llm_mark = "keep" if c.get("llm_keep") else "drop"
        md.append(f"| {{i}} | {{c['name'][:30]}} | {{kwl}} | {{llm_mark}} | {{c.get('llm_reason','')[:30]}} | {{c.get('type','')}} |")
    md.append("")
    (HERE / "_filter_log.md").write_text("\\n".join(md), "utf-8")

    logj = {{"school": SCHOOL_NAME, "time": start_time,
        "stats": {{"total":n_total,"kw_keep":kw_keep,"kw_exclude":kw_excl,"kw_uncertain":kw_unc,
                   "llm_kept":n_kept,"llm_dropped":n_drop,"kw_false_positive":len(kw_fp),"kw_false_negative":len(kw_fn),
                   "extracted":n_total,"filter_rate":round(filter_rate,3),"selector_too_wide":filter_rate>0.4}},
        "candidates": [{{"name":c["name"],"url":c["url"],"src":c.get("src",""),"kw_label":c.get("kw_suggest",{{}}).get("label","unsure"),
            "llm_keep":c.get("llm_keep"),"llm_reason":c.get("llm_reason",""),"type":c.get("type","")}} for c in all_candidates]}}
    (HERE / "_filter_log.json").write_text(json.dumps(logj, ensure_ascii=False, indent=2), "utf-8")


# Step 5: 更新全局大表
def _update_globals(depts, method, score):
    rec = {{"school_name": SCHOOL_NAME, "school_url": SCHOOL_URL,
        "dept_page_url": SOURCE_PAGES[0]["url"] if SOURCE_PAGES else "",
        "method": method, "departments": depts,
        "validation": {{"ok": score>=60, "score": score, "issues": []}}}}
    records = {{}}
    if JSONL_FILE.exists():
        for line in JSONL_FILE.read_text("utf-8").strip().split("\\n"):
            if line.strip():
                try:
                    r = json.loads(line)
                    records[r["school_name"]] = r
                except json.JSONDecodeError:
                    pass
    records[SCHOOL_NAME] = rec
    with open(JSONL_FILE, "w", encoding="utf-8") as f:
        for name in sorted(records):
            f.write(json.dumps(records[name], ensure_ascii=False) + "\\n")
    all_r = list(records.values())
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["学校名称","学校网址","院系名称","院系URL","院系类型","采集方式","验证分数"])
        for r in all_r:
            m = r.get("method","")
            vs = str(r.get("validation",{{}}).get("score",""))
            if r.get("departments"):
                for d in r["departments"]:
                    w.writerow([r["school_name"],r["school_url"],d["name"],d["url"],d.get("type",""),m,vs])
            else:
                w.writerow([r["school_name"],r["school_url"],"","","",m,vs])


def main():
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{{SCHOOL_NAME}} | pages:{{len(SOURCE_PAGES)}} | anti:{{ANTI_CRAWL}}")
    all_raw = []
    page_labels = []
    for pg in SOURCE_PAGES:
        print(f"  [{{pg['label']}}] {{pg['url'][:60]}}")
        c = _extract_all(pg)
        print(f"    CSS extract: {{len(c)}} links")
        all_raw.extend(c)
        page_labels.append(pg['label'])
    seen = {{}}
    seen_names = {{}}
    uniq = []
    for d in all_raw:
        if d.get("url"):
            # 同 URL 可能并列多个机构 (云大"云南研究院/历史文化研究中心"都指向机构目录页自身;
            # 辽大"留学辽大/国际教育学院"共用一个链接) — 同 URL 去重, 机构词优先:
            #   机构词覆盖非机构词 / 双机构词不同名都保留, 交 LLM 层判断
            k = _norm(d.get("url", ""))
        else:
            # 无 URL 的文本节点用 name 去重
            k = "TEXT:" + d.get("name", "")
        _dedup_inst(seen, seen_names, uniq, k, d)
    print(f"  dedup: {{len(uniq)}}")
    # 自身引用过滤: URL 指向任一入口页自身且名称≠入口label = 导航残留 (天大"学院（部）"→xy_b_.htm)
    # 名称==入口label (如"分子聚集态科学研究院"文章页被当入口) → 真实机构, 保留
    _src_map = {{p["url"].split("#")[0].rstrip("/"): p.get("label", "") for p in SOURCE_PAGES}}
    _tmp = []
    for _c in uniq:
        _u = _c.get("url", "")
        _base = _u.split("#")[0].rstrip("/") if _u else ""
        if _base in _src_map and _c.get("name", "") != _src_map[_base]:
            continue
        _tmp.append(_c)
    uniq = _tmp
    # 介绍页 URL 回填: /info/ 文章页 → 抓取"学院网址"字段得真实独立站 (天大/公大模式)
    _backfill_intro_urls(uniq)
    # 跳转参数 URL 解析: _redirect/link.jsp → 跟随重定向取真实子域 (上中医模式)
    _resolve_redirect_urls(uniq)
    uniq = _kw_suggest(uniq)
    kw_ok = sum(1 for c in uniq if c.get("kw_suggest",{{}}).get("label")=="checkmark")
    kw_ng = sum(1 for c in uniq if c.get("kw_suggest",{{}}).get("label")=="warning")
    kw_qu = sum(1 for c in uniq if c.get("kw_suggest",{{}}).get("label")=="unsure")
    print(f"  kw suggest: checkmark={{kw_ok}} warning={{kw_ng}} unsure={{kw_qu}}")
    print(f"  LLM filtering...")
    kept, all_d = _llm_final_filter(uniq)
    # 最终去重: 同名称优先保留有 URL 的版本
    by_name = {{}}
    for c in kept:
        n = c.get("name", "")
        if n not in by_name:
            by_name[n] = c
        elif c.get("url") and not by_name[n].get("url"):
            by_name[n] = c  # 有 URL 的覆盖无 URL 的
    kept = list(by_name.values())
    # 无 URL 且名称含活动/新闻动词 → 最终硬过滤
    news_verbs = ["获奖", "调研", "检查", "活动", "课程", "演出", "研讨会", "聘任",
                  "排练", "展演", "汇报", "拍摄", "实践", "培训", "会议", "大戏",
                  "剧照", "设计：", "设计:", "培养项目", "教学成果", "演出设计"]
    kept = [c for c in kept
            if c.get("url") or not any(v in c.get("name", "") for v in news_verbs)]
    # 再按 URL 去重 (保留 query string, 但 http/https 归一; 同 URL 多机构名分别保留 — 云大目录页并列机构)
    seen_kept = {{}}
    seen_kept_names = {{}}
    kept_uniq = []
    for c in kept:
        if c.get("url"):
            k = _norm(c.get("url", ""))
        else:
            k = "TEXT:" + c.get("name", "")
        _dedup_inst(seen_kept, seen_kept_names, kept_uniq, k, c)
    kept = kept_uniq
    # ═══ 新增: 名称相似度去重 (同机构 http/https 双链接/不同路径) ═══
    # 名称完全相同但 URL 不同 → 保留 https 优先
    final_by_name = {{}}
    for c in kept:
        n = c.get("name", "")
        if not n:
            continue
        if n not in final_by_name:
            final_by_name[n] = c
        else:
            existing = final_by_name[n]
            # https 优先于 http; 否则保留第一个
            if c.get("url", "").startswith("https://") and not existing.get("url", "").startswith("https://"):
                final_by_name[n] = c
    kept = list(final_by_name.values())
    # 名称模糊去重: 去除括号注释差异 (如 "法学院（纪检监察学院）" vs "法学院";
    # "电视学院（出版学院）" vs "电视学院出版学院" — 括号内容直接拼接的变体)
    fuzzy_kept = []
    fuzzy_seen = {{}}  # 键 -> 已保留的候选
    for c in kept:
        raw = c.get("name", "")
        base_name = re.sub(r'[（(].*?[）)]', '', raw).strip()
        # 去括号后的完整拼写 ("电视学院（出版学院）" → "电视学院出版学院")
        full_name = re.sub(r'[（(）)]', '', raw).strip()
        keys = {{k for k in (base_name, full_name) if k}}
        hit = None
        if keys:
            for k in keys:
                if k in fuzzy_seen:
                    hit = fuzzy_seen[k]
                    break
        if hit is not None:
            # 有 URL 的覆盖无 URL 的 (无 URL 文本节点先出现时)
            if c.get("url") and not hit.get("url"):
                idx = fuzzy_kept.index(hit)
                fuzzy_kept[idx] = c
                for k in keys:
                    fuzzy_seen[k] = c
            # 两者都有 URL 且不同 → 独立网站的并列机构 (辽大"历史学部（历史学院）"ls.lnu.edu.cn
            # vs 学部本级"历史学部"lsxb.lnu.edu.cn), 括号别名是真机构, 不合并
            elif c.get("url") and hit.get("url") and _norm(c.get("url")) != _norm(hit.get("url")):
                fuzzy_kept.append(c)
                for k in keys:
                    fuzzy_seen[k] = c
            continue
        fuzzy_kept.append(c)
        for k in keys:
            fuzzy_seen[k] = c
    kept = fuzzy_kept
    # ═══ 同 URL 后缀去重: 同一网址下 "XX大学环境研究院" vs "环境研究院" → 保留全称 ═══
    #   页面同时出现全称/简称两种链接文本 (山大/上海大学/贵州大学等官网列表);
    #   裸分类词 ("国家工程实验室"/"协同创新中心"/"产业技术研究院") 一并合并进全称条目。
    #   仅同 URL 触发 — 不同 URL 的同名机构是真并列 (辽大"历史学部（历史学院）"vs 学部本级)。
    by_url = {{}}
    for _idx, c in enumerate(kept):
        u = _norm(c.get("url", "")) if c.get("url") else ""
        if u:
            by_url.setdefault(u, []).append((_idx, c))
    suffix_drop = set()
    for u, items in by_url.items():
        if len(items) < 2:
            continue
        names = [re.sub(r'[（(].*?[）)]', '', it[1].get("name", "")).strip() for it in items]
        for i in range(len(items)):
            for j in range(len(items)):
                if i == j:
                    continue
                a, b = names[i], names[j]
                if not a or not b or a == b:
                    continue
                if len(b) > len(a):
                    short, si = a, i
                else:
                    short, si = b, j
                if len(short) >= 4 and (a.endswith(b) or b.endswith(a)):
                    suffix_drop.add(items[si][0])  # 组内索引 → 全局索引
    if suffix_drop:
        kept = [c for idx, c in enumerate(kept) if idx not in suffix_drop]
        print(f"  URL后缀去重: drop {{len(suffix_drop)}} 条简称/裸分类词")
    print(f"  LLM kept: {{len(kept)}} (URL去重后) | dropped: {{len(all_d)-len(kept)}}")
    _fr = (len(all_d) - len(kept)) / len(all_d) if all_d else 0
    if _fr > 0.4:
        print(f"  ⚠️ 过滤率 {{_fr:.0%}} — 选择器偏宽, 建议回环节1收窄 css_selectors")
    elif _fr > 0.15:
        print(f"  ⚠️ 过滤率 {{_fr:.0%}} — 偏宽, 可考虑收窄选择器")
    else:
        print(f"  ✅ 过滤率 {{_fr:.0%}} — 提取健康, 过滤仅为兜底")
    _write_filter_log(all_d, kept, page_labels, start_time)
    print(f"  _filter_log.md + .json")

    # ── 数量先验检查: 综合性大学院系过少 → 疑似采漏 ──
    score = 100
    issues = []
    is_special = any(t in SCHOOL_NAME for t in ["音乐","戏剧","美术","体育","语言","警察","公安","民族","外交"])
    if not is_special:
        n = len(kept)
        if n < 10:
            score -= 40
            issues.append(f"院系过少:{{n}}个,疑似入口遗漏")
        elif n < 15:
            score -= 20
            issues.append(f"院系偏少:{{n}}个,可能有入口遗漏")
        elif n < 20:
            score -= 5
            issues.append(f"院系略少:{{n}}个")
    if issues:
        print(f"  ⚠️ 数量先验: {{'; '.join(issues)}}")

    method = f"rule+LLM ({{len(kept)}}/{{len(all_d)}} kept)"
    (HERE/"departments.json").write_text(json.dumps({{"school_name":SCHOOL_NAME,"school_url":SCHOOL_URL,
        "dept_page_url":SOURCE_PAGES[0]["url"] if SOURCE_PAGES else "",
        "method":method,"departments":kept,"validation":{{"ok":score>=60,"score":score,"issues":issues}}}},
        ensure_ascii=False, indent=2), "utf-8")
    with open(HERE/"departments.csv","w",newline="",encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["学校名称","学校网址","院系名称","院系URL","院系类型","采集方式","验证分数"])
        for d in kept:
            w.writerow([SCHOOL_NAME,SCHOOL_URL,d["name"],d["url"],d.get("type",""),method,str(score)])
    _update_globals(kept, method, score)
    tc = Counter(d.get("type","?") for d in kept)
    print(f"  {{SCHOOL_NAME}}: {{len(kept)}} depts")
    for t,c in tc.most_common():
        print(f"    {{t}}: {{c}}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", type=str)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.name:
        cp = SCHOOLS_DIR / args.name / "spider_config.json"
        if not cp.exists():
            print(f"no config: {{cp}}")
            sys.exit(1)
        (cp.parent / "spider.py").write_text(generate_spider(cp), "utf-8")
        print(f"ok {args.name}/spider.py")

    elif args.all:
        cnt = 0
        for d in sorted(SCHOOLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            cp = d / "spider_config.json"
            if not cp.exists():
                continue
            (d / "spider.py").write_text(generate_spider(cp), "utf-8")
            cnt += 1
        print(f"ok {cnt} spiders")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
