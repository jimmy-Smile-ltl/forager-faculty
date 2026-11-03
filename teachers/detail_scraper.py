#!/usr/bin/env python3
"""
教师详情页采集核心模块。
负责：获取详情页 HTML → 检测/下载 PDF → 提取文本 → LLM 提取 13 个字段 → 保存原始数据。

核心设计:
  - FIELD_SPEC: 13 字段的单一事实来源（含义/常见标签/取值词表/格式/示例），
    同时渲染进 LLM 提示词（render_field_spec_prompt）与驱动启发式提取
  - process_teacher: 单教师完整流程。Step 0 本地文件断点判断（有 HTML/PDF
    即零网络复用）→ 抓取 → PDF 检测/下载 → 字段提取（LLM + 启发式逐字段合并）
  - 自适应 PDF 提取: 表格简历页压平成 "标签：值" 行，论文页走线性
    （回归测试: pdf_extract_test.py）
  - 健壮性: 断点续传（resume 参数，RESUME=1 跳过已有结果）、
    LLM 熔断（连续超时 ≥3 次 → 5 分钟快速失败走启发式）
  - 输出契约: 25 列 = 12 元数据列（姓名/URL/来源类型/原始文件/头像/has_pdf/
    pdf_url/pdf_path/html_path/采集时间/失败原因/重试次数）+ 13 字段

网络请求统一委托 util/http_client.py（代理/瑞数降级/错误页过滤集中管理）。
本模块不直接 import requests/curl_cffi。
"""

import csv
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# ── 环境变量（college teacher/.env，由 python-dotenv 加载）──
try:
    from dotenv import load_dotenv
    _shell_proxy_mode = os.environ.get("PROXY_MODE")  # shell 显式设置优先（download_loop.sh PROXY_MODE=off）
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)  # .env 强制覆盖
    if _shell_proxy_mode is not None:
        os.environ["PROXY_MODE"] = _shell_proxy_mode  # 防 .env 覆盖 shell（2026-09-03: 快代理失效期全直连）
except Exception:
    pass

DOWNLOAD_PDF = os.environ.get("DOWNLOAD_PDF", "1").strip() in ("1", "true", "True", "yes")
RATE_LIMIT = float(os.environ.get("RATE_LIMIT", "0.3"))
LLM_TIMEOUT = min(float(os.environ.get("LLM_TIMEOUT", "120")), 180)
# 发现17（2026-08-28）: .env 曾 LLM_TIMEOUT=420 使超时熔断形同虚设,
# 实测单次慢调用卡 363s。慢熔断负责慢调用场景（3 次触发黑名单快速失败）,
# 超时只是最后兜底——clamp 上限 180s, 防止残留环境变量造成单点卡死窗口。
LLM_SLOW_THRESHOLD = float(os.environ.get("LLM_SLOW_THRESHOLD", "30"))
LLM_SLOW_TRIGGER = int(os.environ.get("LLM_SLOW_TRIGGER", "3"))
LLM_SLOW_BLACKOUT = float(os.environ.get("LLM_SLOW_BLACKOUT", "60"))
# 网络/下载失败重试: 最大尝试次数（含首次）与退避基数（秒）
RETRY_MAX = int(os.environ.get("RETRY_MAX", "3"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "1.0"))

# ── 请求节流门（进程级全局, 2026-08-25）──
# 原实例级锁时 --all 并发 N 个学校 = N×rate_limit 请求速率叠加;
# 提升为模块级后同进程所有 scraper 实例共享一个节流门（对齐 department
# 侧"全局共享资源"范式: 信号量/预算/节流都必须是进程内唯一实例）。
_THROTTLE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_SKIPPED_URLS_LOCK = threading.Lock()  # 拦截名单回写锁（并发路径多线程写）
# TCP 预检 memo（2026-09-28 死 IP 家族）: host → 端口可达性, 进程内一次
_TCP_PROBE_MEMO: dict[str, bool] = {}
_TCP_PROBE_LOCK = threading.Lock()
# 2026-10-26 Vue 壳渲染限流: 9222 渲染重操作(每页 10-45s), 提取侧多路
# 并发同时触发会打爆 CDP——信号量 2 路(历史: 渲染型站预算须 ≥ 熔断尝试期)
_VUE_RENDER_SEM = threading.Semaphore(2)

# ── LLM 全局并发总闸（借鉴 department teachers 范式, 2026-08-25）──
# 内层 per-token 槽（util/llm_client._token_sems, LLM_PER_TOKEN=2）是模块级
# dict, 天然跨 SimpleLLM 实例共享; 本总闸补充: ① 容量感知（容量 = 池有效
# token 数 × per-token, 300s 自动刷新）; ② 手动总闸 LLM_SEM=N（运维/网关
# 异常时强制限流, 实验对照）; ③ 防御上限 LLM_CONCURRENCY_CAP。
# 教训（department 侧）: 同进程多模块各自建信号量会叠加打爆网关, 必须单一
# 共享实例 —— _extract_fields_llm 的所有 LLM 调用必须经 _LLM_SEM 放行。
LLM_CONCURRENCY_CAP = int(os.environ.get("LLM_CONCURRENCY_CAP", "64"))
_LLM_PER_TOKEN = int(os.environ.get("LLM_PER_TOKEN", "2"))


class _LLMDynamicSem:
    """动态容量信号量（与 collect_teacher._DynamicSem 同型）: 容量跟随
    token 池有效数, 后台线程周期性刷新; 池新增/失效时自动调整, 不重启批次。"""

    def __init__(self, cap):
        self._cond = threading.Condition()
        self._cap = max(1, cap)
        self._held = 0

    def set_cap(self, cap):
        with self._cond:
            self._cap = max(1, cap)
            self._cond.notify_all()

    def acquire(self):
        with self._cond:
            while self._held >= self._cap:
                self._cond.wait()
            self._held += 1

    def release(self):
        with self._cond:
            self._held -= 1
            self._cond.notify()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()


def _llm_active_token_count() -> int:
    """探测 token 池有效(ok)数量 — 读 token manager 镜像（进程内池实例
    可能未初始化/不存在, 镜像是最稳来源, 与 collect_teacher 兜底同源）。"""
    mirror = Path(__file__).resolve().parent.parent / "token manager" / "tokens.json"
    try:
        d = json.loads(mirror.read_text(encoding="utf-8"))
        return sum(1 for t in d.get("tokens", []) if t.get("status") == "ok")
    except Exception:
        return 0


def _init_llm_sem_cap():
    """返回 (初始容量, 是否自动模式)。LLM_SEM 手动指定则不跟随池。"""
    manual = os.environ.get("LLM_SEM")
    if manual is not None and manual.strip() not in ("", "0"):
        return int(manual), False
    n = _llm_active_token_count()
    total = n * _LLM_PER_TOKEN if n else 4
    return max(2, min(total, LLM_CONCURRENCY_CAP)), True


_LLM_SEM_CAP, _LLM_SEM_AUTO = _init_llm_sem_cap()
_LLM_SEM = _LLMDynamicSem(_LLM_SEM_CAP)


def _llm_sem_refresher():
    """自动模式每 300s 跟随 token 池容量（池刷新 TTL 也是 300s）。"""
    while True:
        time.sleep(300)
        try:
            n = _llm_active_token_count()
            total = n * _LLM_PER_TOKEN if n else 4
            _LLM_SEM.set_cap(max(2, min(total, LLM_CONCURRENCY_CAP)))
        except Exception:
            pass


if _LLM_SEM_AUTO:
    threading.Thread(target=_llm_sem_refresher, daemon=True).start()

# ── LLM 客户端路径 (SimpleLLM 统一在 college teacher/util/llm_client.py) ──
_LLM_UTIL = Path(__file__).resolve().parent.parent / "util"

# ── 瑞数绕过工具路径 ──
_UTIL_ROOT = Path(__file__).resolve().parent.parent / "util"

# ── 提取字段规格（单一事实来源）──
# 含义/常见标签/输出格式/示例 —— 渲染进 LLM 提示词模板，启发式提取也复用 labels。
FIELD_SPEC = [
    {
        "key": "联系方式",
        "meaning": "教师的电话、手机、邮箱、传真等所有联系渠道",
        "labels": ["电话", "Tel", "Phone", "手机", "邮箱", "Email", "E-mail", "传真", "Fax", "联系电话", "电子邮箱"],
        "format": "保留原始标签和数值，多个渠道用分号分隔",
        "example": "办公电话：0431-85160000；电子邮箱：caoyang@jlu.edu.cn",
    },
    {
        "key": "通讯地址",
        "meaning": "邮寄地址或办公地址（含邮编）",
        "labels": ["地址", "通讯地址", "通信地址", "联系地址", "办公地址", "邮寄地址", "邮编", "邮政编码"],
        # 2026-10-24 北大数学田刚「镜春园 79105」形态: 建筑名+连写数字串
        # = 楼号+房间号连写, LLM 应按上下文拆分为「镜春园 79 号 105」
        "format": "完整地址原文，含邮编；「建筑名+连写数字串」（如 镜春园 79105）按上下文拆分还原为「镜春园 79 号 105」",
        "example": "北京市海淀区颐和园路5号，北京大学中文系 邮政编码：100871",
    },
    {
        "key": "职称",
        "meaning": "学术职称：教授/副教授/讲师/研究员/副研究员/高级工程师/工程师/实验师等",
        "labels": ["职称", "技术职称", "现任职称"],
        # 职称值词表（启发式全文扫描用，最长优先）
        "values": [
            "教授级高级工程师", "教授级高级实验师", "高级工程师",
            "高级实验师", "副研究员", "助理研究员", "副教授", "研究员", "教授",
            "工程师", "实验师", "实验员", "讲师", "助教", "院士",
        ],
        "format": "仅职称词本身（如 教授、副教授、高级实验师），无则空",
        "example": "教授",
    },
    {
        "key": "职务",
        "meaning": "行政或学术职务，不含职称：系主任/副院长/实验室主任/支部书记/学会理事等",
        "labels": ["职务", "现任职务", "行政职务", "主任", "院长", "系主任"],
        "format": "多个职务用分号分隔，保留时间信息",
        "example": "系主任；曾任数学科学学院副院长（2018-2022）",
    },
    {
        "key": "出生年月",
        "meaning": "出生日期",
        "labels": ["出生", "出生日期", "出生年月", "生于"],
        "format": "原文日期，如 YYYY年 或 YYYY年MM月",
        "example": "1954年",
    },
    {
        "key": "教育经历",
        "meaning": "本科、硕士、博士、博士后等就读学校、专业、学位、起止时间",
        "labels": ["教育经历", "教育背景", "学习经历", "学历", "学位"],
        "format": "按时间倒序分条，保留学校/专业/学位/导师，分号分隔",
        "example": "2004.09-2007.06 南开大学陈省身数学研究所 博士；2001.09-2004.06 江西师范大学 硕士",
    },
    {
        "key": "工作经历",
        "meaning": "过往任职经历：单位、职位、起止时间",
        "labels": ["工作经历", "工作履历", "任职经历", "职业经历"],
        "format": "按时间倒序分条，保留单位/职位/时间，分号分隔",
        "example": "2016.12-至今 吉林大学数学学院 教授；2009.12-2016.12 南开大学数学科学学院 副教授",
    },
    {
        "key": "研究方向",
        "meaning": "研究领域、科研方向",
        "labels": ["研究方向", "研究领域", "科研方向", "研究兴趣",
                   # 发现58: 复合形态"研究领域与/和/或方向："（贵大计算机、
                   # 上交系统生物医学研究院、中国海洋大学化学化工学院实测）
                   # 与"主要研究方向与成果："（中科大精准智能化学实验室）
                   # ——裸"研究方向"命中后只抓到"与成果："碎片被清空
                   "研究领域与方向", "研究领域和方向", "研究领域或方向",
                   "主要研究方向与成果", "研究方向与成果"],
        "format": "原文短语，多个方向用顿号或分号分隔",
        "example": "算子理论、复分析",
    },
    {
        "key": "学术成果",
        "meaning": "发表的论文、出版的专著、获得的专利、软件著作权等",
        "labels": ["学术成果", "论文", "专著", "专利", "代表性成果", "科研成果", "著作"],
        "format": "保留成果标题与出处，多项用分号分隔；过长时截取代表性条目",
        "example": "专著：《明清鼎革中的心灵史》，天津人民出版社2008年；论文：《XXX》，《中国文化》2017年",
    },
    {
        "key": "基金",
        "meaning": "承担或参与的科研基金、科研项目（如国家自然科学基金）",
        "labels": ["基金", "项目", "课题", "国家自然科学基金", "重点项目", "社科基金"],
        "format": "保留基金/项目全称、编号、立项时间，分号分隔",
        "example": "国家社会科学基金重大项目“XXX”（批准号15ZDB065）；国家自然科学基金面上项目（11471133）",
    },
    {
        "key": "荣誉",
        "meaning": "获得的奖项、荣誉称号、人才称号（如杰青、长江学者、院士）",
        "labels": ["荣誉", "获奖", "奖项", "人才称号", "荣誉称号", "奖励"],
        "format": "保留奖项全称与年份，分号分隔",
        "example": "2015年 北京大学人文杰出青年学者奖；长江学者特聘教授（2018）",
    },
    {
        "key": "课堂项目",
        "meaning": "主持或参与的教学项目、教改项目",
        "labels": ["课堂项目", "教学项目", "教改项目", "课程建设"],
        "format": "保留项目全称与编号，分号分隔",
        "example": "主持省级教改项目“大学数学课程改革”（2022）",
    },
    {
        "key": "讲授课程",
        "meaning": "主讲或参与讲授的课程名称",
        "labels": ["讲授课程", "主讲课程", "承担课程", "课程", "教学"],
        "format": "课程名称列表，分号分隔",
        "example": "数学分析；高等代数；概率论",
    },
    {
        "key": "attr",
        "meaning": ("兜底字段: 页面中出现的、不属于上述字段但具有信息价值的内容"
                    "(兼职/客座教授、办公室房间号、社会任职、招生方向、个人简介要点等)"),
        "labels": ["兼职", "客座教授", "办公室", "社会任职", "兼任", "社会兼职"],
        "format": ("JSON 对象: 键为简短类别名(如 兼职、办公室、社会职务)，"
                   "值为该类别原文文本; 无此类信息时输出空对象 {}"),
        "example": "{\"兼职\": \"中国数学会理事；华东师范大学兼职教授\", \"办公室\": \"数学楼512\"}",
    },
]

# ── 13 个字段名（顺序 = FIELD_SPEC）──
DETAIL_FIELDS = [s["key"] for s in FIELD_SPEC]

# ── 导航噪声判定（东北农大教训: 垃圾教师名对应的页面是导航/栏目页,
#   提取出的"教育经历=学位管理 下载中心 学生工作…"全是导航词）──
# 整词命中表（导航栏高频词，整 token 相等才算命中——避免误杀
# 真实字段值："教师" in "中学教师"、"教育" in "教育技术" 等）
_NAV_NOISE_WORDS = frozenset({
    "首页", "新闻", "通知", "公告", "动态", "简介", "概况", "更多", "详情",
    "返回", "登录", "注册", "搜索", "导航", "链接", "栏目", "网站",
    "学位管理", "下载中心", "学生工作", "办公室", "团委", "学生会",
    "研究生会", "社团联合会", "志愿者", "服务", "党建", "工作动态",
    "学科建设", "师资队伍", "人才引进", "本科教学", "研究生教育",
    "科学研究", "合作交流", "校友工作", "教务管理", "招生就业",
    "教职工", "考生", "荣誉教授", "客座教授", "教师名录", "人才培养",
    "基金会", "校史馆", "荣誉体系", "党政机构", "杰出人才",
    "教师", "本科生", "本科教育", "研究生招生", "本科招生", "科研处",
    "艺术实践", "院系设置", "机构设置", "招生信息", "就业信息",
    "学校简介", "学院简介", "教师招聘", "现任领导", "历任领导",
    "留学生教育", "继续教育", "学院概况", "校园新闻", "通知公告",
    "工作", "课程设置", "组织机构", "院系概况", "学院新闻", "师资概况",
    # ccS 平台导航词（陕师大发现21: 主办期刊/学位点介绍等整 token 出现,
    # 真实字段值几乎不会整词使用, 加入词表判导航噪声）
    "主办期刊", "学位点介绍", "学位授权点", "博士后流动站", "荣休教师",
    "教学团队", "课程建设", "学术动态", "研究机构", "科研成果",
    "文献资料", "学术论坛", "学生工作", "团学概况", "团学动态",
    "学生风采", "特色品牌", "校友之窗", "校友名录", "校友风采",
    "校友捐赠", "人才培养概况", "本科生教育", "最美语文教师评选",
    "硕士点", "博士点", "重点学科", "一级学科", "二级学科", "学科平台",
    "招生简章", "录取分数", "调剂信息", "学科介绍", "专业介绍",
    # 吉大/上财/清华导航词（发现25/26 离线复核: 修复前老数据残留 +
    # 招聘栏目词, 整 token 不会出现在真实字段值）
    "学术方向", "学术报告", "学术会议", "国际交流", "国际合作平台",
    "国际会议", "奖助学金", "工作流程与下载", "国外硕士项目", "社会服务",
    "校友发展", "活动通知", "回馈母校", "联系我们", "加入我们", "招聘信息",
    "职工招聘", "招聘方式", "学位合作",
    # 吉大植物科学学院（发现27 复核）: "表格下载" 被 LLM 归入课堂项目
    # 150+ 人——导航菜单项, 非真实课程名; 整 token 补表, 短值整词命中
    # 仍可判（子串命中在 ≤2 token 时已不判, 见 _is_nav_noise_value）
    "表格下载",
})
# 子串命中表（强组织/栏目词——仅保留几乎不会出现在真实字段值里的词。
# 刻意排除 研究/科学/技术/医学/经济/教育/管理/课程/平台/系统/资源/
# 学习/交流/合作/国际/教师/教授/中心/工作/建设/组织/机构/部门 等
# 会出现在研究方向/教育经历/工作经历里的词, 防误杀）
_NAV_NOISE_SUB = (
    "院系", "学系", "教职工", "考生", "研究生", "团委", "党建", "校友",
    "招生", "就业", "下载", "简介", "概况", "动态", "公告", "导航",
    "搜索", "社团", "名录", "师资", "后勤",
)

# 字段值判定排除集（发现27, 2026-08-29）: 上表是姓名/页面级通用表,
# 但字段值上下文里部分词是真实内容——"研究生"（硕士研究生/研究生导师/
# 研究生奖学金）、"团委"（职务: 团委书记）。整词同理: "教师"（职务:
# 教师）在字段值里合法。姓名/页面级判定（_is_junk_name/_is_nav_noise_page）
# 保持用全表, 字段值判定排除, 防误杀真实内容。
_NAV_NOISE_WORDS_VALUE_EXCLUDE = frozenset({"教师"})
_NAV_NOISE_SUB_VALUE_EXCLUDE = frozenset({"研究生", "团委"})


# 名单垃圾词子串表 (P0-1): 非人名词汇——仅保留几乎不可能出现在真实
# 中文姓名里的词（软件/电话/系统等办公与产品词）。与 _NAV_NOISE_SUB
# 分开维护: 导航词表为"字段值"设计, 人名校验可再收紧一档。
_NAME_JUNK_SUB = (
    "软件", "电话", "邮箱", "系统", "平台", "设备", "报名", "缴费",
    "微信", "网站", "登录", "注册",
    "党校", "讲堂",  # 东北农大"二级党校"、机构栏目词（R1 上游词表同步）
    # 发现39 (2026-08-29): 导航菜单项整词作姓名（上财"专题学习/教工之家/
    # 组织机构/本科生"、云大"教学建设/教研风采"、安大"研究生院"、厦大
    # "组织机构"）。复合栏目词不会出现在真实人名里, 子串安全。
    "专题学习", "教工之家", "组织机构", "教学建设", "教研风采",
    "研究生院", "本科生", "硕士生", "博士生",
    # 发现59 (2026-09-11): 与 dept_rule_generator._SAMPLE_CAT_WORDS 对齐
    # 的栏目词缺口——"教育教学"等 6 词漏表致 _is_person_name 误放行。
    # 均为复合栏目词, 不可能出现在真实人名里, 子串安全。
    "教育教学", "教学科研", "导师队伍", "教授风采", "研究成果",
    "学术活动", "研究团队", "学术交流", "师资力量", "学子风采",
    "校友风采", "校园新闻", "党建动态", "学生工作", "工会活动",
    # 发现59 (2026-09-11): 名单导航链接名（"七院微博"→weibo.com 实测）——
    # 社交媒体/公众号形态, 真实人名不可能含
    "微博", "公众号", "二维码",
    # 发现41 (2026-08-29): 尾漏搬移 dry-run 暴露的名单导航词漏网——
    # 上财会计学院菜单项（会议论坛/学者观点/学术期刊/培养管理/教学成果/
    # 学子风采/师者故事/专任教师/教辅人员/发展历程/党的建设/班团风采/
    # 学习资料/校友活动/校友风采/回馈母校/学术讲座）、华科"职能科室"、
    # 上交国务学院栏目名（名誉/访问/永远怀念）。同发现39 理由: 复合
    # 栏目词不会出现在真实人名里, 子串安全。
    "会议论坛", "学者观点", "学术期刊", "培养管理", "教学成果",
    "学子风采", "师者故事", "专任教师", "教辅人员", "发展历程",
    "党的建设", "班团风采", "学习资料", "校友活动", "校友风采",
    "回馈母校", "学术讲座", "职能科室", "名誉", "访问", "永远怀念",
    "办公厅",  # 陕师大教师干部培训学院名单栏目词（发现42）
    "非全专",  # 上财会计学院"非全日制专硕"栏目名截断（发现42）
    # 2026-10-24 医院科室词(上游反馈类别H实锤: 常州一院名单"肿瘤学/核医学/
    # 妇科"、南医大一临"护理概况/护理动态"被当教师名)。放 _NAME_JUNK_SUB
    # 而非 _NAV_NOISE_WORDS: 后者同判字段值, "肿瘤学"是合法研究方向值,
    # 人名层面则不可能含这些词, 子串安全
    "肿瘤学", "核医学", "妇科", "护理概况", "护理动态", "护理管理",
    "内科", "外科", "儿科", "骨科", "眼科", "口腔科", "皮肤科",
    "急诊科", "麻醉科", "检验科", "影像科", "放射科", "病理科",
    "药剂科", "康复科", "感染科", "妇产科", "耳鼻喉科", "重症医学科",
    "临床医学", "基础医学", "专家介绍", "医疗团队", "科室介绍",
    # 2026-10-25 批次2349零容忍实锤(北科大科研院/电子科大中山等): 栏目/页面
    # 控件词被当姓名 — 一次进表, 后续名单直接滤掉不触发整院FAIL
    "防护", "上页", "中山发布", "全面推进", "课程成长",
    "万象视频", "悦闻书香", "兼职团队", "服务设备",
    # 2026-10-25 兰大企业研究院实锤: 兰大首页导航词被当教师(97人整院FAIL)
    "认识兰大", "校情概览", "百年历程", "兰大标识", "入职兰大",
    # 2026-10-25 北大人文学部491人整院FAIL实锤: 栏目词混入名单
    "影像空间", "学术奖项", "奖助项目", "专题工作",
    # 2026-10-26 第三轮批实锤: Nature期刊导航词+职称碎片被当姓名
    "Nature Africa", "Nature India", "Nature Japan", "Nature Middle East",
    "Nature Asia", "Nature Reviews", "Nature News", "Nature Careers",
    "长聘", "专职", "特聘", "实验师",
    # 2026-10-25 瑞数批零容忍实锤: 页面栏目/简历碎片被当姓名(北大生科/电子
    # 科大长三角/重大肿瘤医院) — 进表后名单直接滤掉不触发整院FAIL
    "究所现有", "我院优秀", "生导师", "后项目", "后名单",
    "后招聘", "庆典日程", "百年瞬间",
    # 2026-10-26 小院系(<20教师)清理116条非人名实锤的剩余漏网: 医院片区/
    # 科室变体(大内科/内科片区)、导航碎片(预报名/登录指南/工会概况)、
    # 姓名+标签粘连("何珺简介")。人名不含这些词, 子串安全
    "内科片区", "外科片区", "大内科", "大外科", "肾内科",
    "神经外科", "预报名", "登录指南", "工会概况", "简介",
)


def _is_junk_name(name: str) -> bool:
    """名单姓名是否垃圾词（非人名）——上游名单混入导航词/栏目词/办公
    信息（西交"正版软件"、上财"联系电话"、东北农大"二级党校"），对应
    detail_url 指向学校主页/导航页而非教师页。命中 → process_teacher
    不发起网络请求, 立即判死归因"名单垃圾词"(上游数据问题)。
    复用导航词表(高选择性整词/子串) + 非人名词汇子串表; 刻意排除
    "建设/中心/工作/管理"等可能出现在真实姓名(张建设)里的词, 防误杀。
    """
    if not name:
        return False
    if name in _NAV_NOISE_WORDS:
        return True
    if any(k in name for k in _NAV_NOISE_SUB):
        return True
    if any(k in name for k in _NAME_JUNK_SUB):
        return True
    return False


# 发现59 (2026-09-11): 名单"是不是人名"形态判定——上游补充下载前过滤
# 非教师条目。机构后缀结尾（结尾而非子串, 与 _NAME_JUNK_SUB 的防误杀
# 口径一致: 刻意不含"中心/建设/管理", 张建设类真名不受影响）。
_PERSON_SUFFIX_RE = re.compile(
    r"(学院|学部|书院|委员会|研究院|研究所|实验室|办公室|教研室|"
    r"党支部|团支部|工会|团委|工作站|联络处)$")

# 拉丁姓名判定里的短语停用词（小写）: Unlock Account / Giving to FDU /
# Knowledge Exchanges… 这类链接文案不是人名。刻意收窄, 防误杀真名。
_LATIN_PHRASE_STOP = frozenset((
    "account", "unlock", "giving", "knowledge", "exchanges", "between",
    "login", "logout", "password", "download", "register", "forgot",
    "reset", "search", "subscribe", "news", "events", "contact", "about",
    "welcome", "signin", "signup", "page", "result", "view", "profile",
    "more", "read", "home", "index", "main", "default", "faculty",
    "staff", "teacher", "professor", "list", "university", "college"))


def _is_person_name(name: str) -> bool:
    """名单条目是否像人名。保守: 只拒绝明确不像人名的形态, 拿不准
    一律放行（交下载/提取判定）, 不误杀真教师。

    拒绝: ①垃圾词(_is_junk_name) ②含数字/句读括号等标点 ③纯汉字
    <2 或 >8 字 ④机构后缀结尾 ⑤拉丁短语链接文案（非姓名, 见
    _LATIN_PHRASE_STOP）。放行: 民族名 · 形态、拉丁外籍教师姓名
    （2026-09-14 修复: Brian Cox / LIM XI ZHI / Gianmatteo Sabatino
    曾被"含 ASCII 字母"整类误杀, 全库 2.6k 外籍教师）。
    """
    if not name or _is_junk_name(name):
        return False
    n = str(name).strip()
    if not n:
        return False
    # 民族名: "阿不来提·阿不都热西提"——· 前后均为汉字才放行
    if "·" in n:
        parts = n.split("·")
        if all(p and all("一" <= ch <= "鿿" for ch in p)
               for p in parts):
            return True
        return False
    # 拉丁姓名: 纯字母(含空格/连字符/撇号/句点), 1-5 个词, 每词首字母
    # 大写(全大写词通行), 无短语停用词。全小写/首词小写 = 短语非姓名。
    if re.fullmatch(r"[A-Za-z]+(?:[ .'\-][A-Za-z]+)*", n):
        words = n.split()
        if len(words) > 5:
            return False
        if any(w.lower() in _LATIN_PHRASE_STOP for w in words):
            return False
        if not all(w[:1].isupper() for w in words):
            return False
        return True
    if not all("一" <= ch <= "鿿" for ch in n):
        return False  # 数字/标点/混合
    if len(n) < 2 or len(n) > 8:
        return False  # 机构长名/单字
    if _PERSON_SUFFIX_RE.search(n):
        return False
    return True


def _is_nav_noise_value(v: str) -> bool:
    """字段值是否整串由导航词构成（学位管理 下载中心 学生工作…）。

    真实字段值（研究方向/教育经历）几乎不可能 ≥70% token 都是
    导航词；命中即该值来自页面导航栏而非正文，清空防止污染。
    长度 >120 的值原本豁免——但完整导航菜单词链常 >120 字（陕师大
    文学院 155 字全链漏网, 发现21），改为高阈值长链检测: ≥8 token
    且 ≥80% 命中才判, 真实长字段值（如荣誉列表）命中率远低于此。

    2026-08-29 误杀修复（发现27）: 字段值上下文用保守排除集, 防误杀:
      - 子串命中只对 ≥3 token 的值生效; 无分隔符整句算 1 token,
        含子串词命中率即 100%（"…硕士研究生毕业并留校工作。"被误清）
      - ≤2 token 仅整词命中可判（"社会服务"/"招生信息"仍是导航,
        "硕士研究生"不是）
      - "研究生"（硕士研究生/研究生导师/研究生奖学金）、"团委"
        （团委书记）、"教师"（职务: 教师）在字段值里是真实内容,
        字段值判定排除——姓名/页面级判定（_is_junk_name 等）不受影响
    """
    if not v:
        return False
    tokens = [t for t in re.split(r"[\s,，;；、/|]+", v) if t]
    if not tokens:
        return False
    whole_hits = sum(1 for t in tokens
                     if t in _NAV_NOISE_WORDS and t not in _NAV_NOISE_WORDS_VALUE_EXCLUDE)
    sub_hits = sum(1 for t in tokens
                   if any(k in t for k in _NAV_NOISE_SUB if k not in _NAV_NOISE_SUB_VALUE_EXCLUDE))
    if len(v) <= 120:
        if len(tokens) >= 3:
            return (whole_hits + sub_hits) / len(tokens) >= 0.7
        return len(tokens) >= 1 and whole_hits == len(tokens)
    return len(tokens) >= 8 and (whole_hits + sub_hits) / len(tokens) >= 0.8


# 发现38 (2026-08-29): 导航/侧栏链接文本碎片入字段——LLM 从
# `<a>教学名师</a><a>优秀青年基金获得者</a><a>获奖成果</a>` 等菜单链接
# 截取单/双字碎片填字段（厦大: 讲授课程="名师"/基金="获得者"; 北大历史:
# 荣誉="成果"; 116 行已清理）。_is_nav_noise_value 是长链比率判定, 对
# ≤6 字的单词碎片不生效, 需整串精确判定。职称 的合法单值词（院士/教授/
# 讲师…）不受影响——字段值判定带 field 上下文。
_NAV_FRAG_WORDS = frozenset({
    "名师", "获得者", "成果", "荣誉", "科研", "院士", "奖", "链接",
    "更多", "首页", "新闻", "通知", "公告", "概况", "简介", "机构",
    "师资", "教学", "学生", "校友", "党群", "加入", "登录", "注册",
    "搜索", "咨询", "下载", "联系我们",
    # 发现44 (2026-08-29): 4 字导航菜单词整串入字段（清华: 讲授课程=
    # "招生动态"×33、基金="博士项目"、学术成果="招生招聘"）——菜单词
    # 不会是字段内容, 整串命中即碎片
    "招生动态", "硕士项目", "博士项目", "学术刊物", "招生招聘",
    "学生发展", "科研平台", "下载专区", "教学科研", "学术动态",
    "通知公告", "信息公开", "校园文化", "人才招聘",
})
_ZC_LEGIT_SINGLE = frozenset({
    "院士", "教授", "副教授", "讲师", "助教", "研究员", "副研究员",
    "助理研究员", "工程师", "高级工程师", "实验师", "高级实验师", "实验员",
})


def _is_nav_frag_value(v, field: str = "") -> bool:
    """字段值是否为导航链接文本碎片（发现38）。"""
    v = (v or "").strip()
    if not v or len(v) > 6 or v not in _NAV_FRAG_WORDS:
        return False
    if field == "职称" and v in _ZC_LEGIT_SINGLE:
        return False
    return True


# 发现40 (2026-08-29): 节标签前缀未剥离——历史启发式切块起点=标签首字符,
# "工作经历： 2004 年…" 整串含标签入库（370 例, 姜长松/都兴林/李清伟等）。
# 确定性剥前缀 ^字段名[：: ]+；剥完后若直接是兄弟节标签 = 标签链倾倒
# （某教师: 教育经历="教育经历 工作经历 个人简介…"）, 宁缺毋滥清空
# 待 LLM 重提取。
_SECTION_LABELS = frozenset(DETAIL_FIELDS) | frozenset({
    "教学情况", "个人简介", "学习经历", "科研项目", "学术兼职",
    "社会兼职", "主要论著", "代表论文", "获奖情况", "奖励荣誉",
    "人才培养", "学术组织", "社会任职", "科研情况", "教学成果",
    "论文", "课题", "著作", "专利", "人生经历", "求学经历", "工作简历",
    "研究课题", "研究领域", "开设课程", "承担项目", "发表论文", "研究简介",
})


# 发现41 补: 同义标签前缀（张贺 教育经历="学习经历： 2009年…"、李冠乔
# 工作经历="工作履历： 2022-至今…"）——发现40 只剥字段名本身, 同义标签
# 前缀残留 8 行, 一并剥。
_SYNONYM_LABELS = {
    # 发现42: "教育背景及工作经历" 合并标签前缀（安大资环 7 行）——
    # 剥掉合并标签后可能还有同义标签（周立志: "教育背景及工作经历
    # 教育背景 1984…"）, 下方循环剥到底。
    "教育经历": ("教育背景及工作经历", "教育经历", "教育背景", "学习经历"),
    "工作经历": ("教育背景及工作经历", "工作经历", "工作履历", "任职经历"),
    "研究方向": ("研究方向", "研究领域", "科研方向", "研究兴趣"),
    "学术成果": ("学术成果", "科研成果", "代表性成果"),
    "讲授课程": ("讲授课程", "主讲课程", "承担课程"),
    "荣誉": ("荣誉", "获奖情况", "荣誉及奖励"),
}


def _field_quality_score(val: str) -> int:
    """字段值质量分(2026-10-27 择优保留用): 有效内容长度为主, 惩罚
    标签前缀/空值/残留。规则值与 LLM 值对比, 分数高者保留。"""
    val = (val or "").strip()
    if not val:
        return 0
    score = len(re.findall(r"[一-鿿A-Za-z0-9@.\-]", val))
    if re.match(r"^(?:暂无|无|空缺|空)(?:[\s。；;）)]|$)", val):
        return 0
    # 标签前缀残留惩罚(「电子邮箱: x@y」vs「x@y」——前者是切块碎片)
    if re.match(r"^(?:主要)?(?:研究方向|研究领域|教育经历|工作经历|职称|"
                r"电子?邮箱|联系方式|通讯地址|学术成果|荣誉)[：:]", val):
        score -= 8
    # HTML/注释残留惩罚
    if "-->" in val or "<" in val:
        score -= 12
    return max(score, 0)


def _strip_self_section_label(v, field: str) -> str:
    """剥离字段值开头的自身节标签（发现40）。

    幂等: 已剥离的值再进无变化。剥后为空或直接是兄弟节标签 → ""。
    发现41 补: 标签后带括注形态（"工作经历（Professional Experience）
    2019年…", 秦建春）与同义标签（"学习经历："）一并剥掉。
    发现42 补: 合并标签前缀（"教育背景及工作经历"）循环剥到底。
    """
    v = (v or "").strip()
    if not v:
        return ""
    labels = _SYNONYM_LABELS.get(field, (field,))
    for _ in range(3):
        stripped = False
        for lbl in labels:
            m = re.match(rf"^{re.escape(lbl)}(?:[：: \t]+|（[^（）]*）[：: \t]*)", v)
            if not m:
                continue
            rest = v[m.end():].strip()
            if not rest:
                return ""
            for sib in _SECTION_LABELS:
                if sib not in labels and rest.startswith(sib):
                    return ""  # 标签链倾倒（某教师）, 清空待 LLM 重提取
            v = rest
            stripped = True
            break
        if not stripped:
            break
    return v


# 发现41 (2026-08-29): 尾漏切分搬移——存量字段值尾部挂着兄弟节标签段
# （姜妍 工作经历 尾挂"科研项目 1.十四五…"; 发现30 终点检测是提取期
# 修复, 修不了 08-21 前的存量）。按独立性规则（发现30 同款: 前导行首/
# 空白/冒号 + 后不跟"及和与"）切段: 段头标签→目标字段搬移（目标空才搬,
# 已有值 = 重复内容丢弃）; 出现无映射标签 → 整值保守不动（交 LLM）。
# "研究方向" 句内叙述（"获硕士学位, 研究方向: 概率论"）前导是空格也会
# 切——但切后搬进 研究方向 字段是正确归位, 不是误伤（发现37 的教训在
# 评估判定, 不在此）。
_TAIL_LABEL_TARGET = {
    "教育经历": "教育经历", "教育背景": "教育经历", "学习经历": "教育经历",
    "工作经历": "工作经历", "工作履历": "工作经历", "任职经历": "工作经历",
    "研究方向": "研究方向", "研究领域": "研究方向", "科研方向": "研究方向",
    "研究兴趣": "研究方向", "主要研究方向": "研究方向",
    "研究领域与方向": "研究方向", "研究领域和方向": "研究方向",
    "研究领域或方向": "研究方向", "主要研究方向与成果": "研究方向",
    "研究方向与成果": "研究方向",
    "学术成果": "学术成果", "代表性成果": "学术成果", "科研成果": "学术成果",
    "学术论著": "学术成果", "论文发表": "学术成果", "论文与专利": "学术成果",
    "主要论文": "学术成果", "发表论文": "学术成果", "学术论文": "学术成果",
    "主要学术论文": "学术成果", "论文": "学术成果",
    "著作目录": "学术成果", "学术著作": "学术成果", "主要学术著作": "学术成果",
    "代表著作": "学术成果", "专著": "学术成果", "出版著作": "学术成果",
    "代表性论文": "学术成果",
    "科研项目": "基金", "主要科研项目": "基金", "在研项目": "基金",
    "在研科研项目": "基金", "科研基金": "基金", "基金项目": "基金",
    "主持项目": "基金", "课题": "基金", "代表性课题": "基金",
    "教改项目": "课堂项目", "教学项目": "课堂项目", "教学成果": "课堂项目",
    "教学及教学研究": "课堂项目", "教学研究": "课堂项目", "教学与科研": "课堂项目",
    "教学情况": "讲授课程",
    "获奖情况": "荣誉", "荣誉及奖励": "荣誉", "荣誉": "荣誉",
    "获奖": "荣誉", "人才称号": "荣誉",
    "讲授课程": "讲授课程", "主讲课程": "讲授课程", "承担课程": "讲授课程",
    "联系方式": "联系方式", "联系电话": "联系方式", "电子邮箱": "联系方式",
    "通讯地址": "通讯地址", "出生年月": "出生年月", "出生日期": "出生年月",
    "职称": "职称", "职务": "职务", "现任职务": "职务",
}
# 兼职类 → attr 兜底字段（键=标签词）
_TAIL_LABEL_ATTR = frozenset({
    "学术/技术组织任职", "学术与技术组织任职", "学术组织任职",
    "社会工作任职", "社会任职", "学术任职", "社会兼职", "学术兼职",
})


def _section_labels_for(key: str) -> tuple[str, ...]:
    """(2026-09-07) 节切块标签集 = FIELD_SPEC labels + 尾漏别名表同字段词,
    按长度降序（"主要科研项目"先于"科研项目"/"项目", 长词在独立性判定
    下才可能以完整标题形态命中）。"""
    spec = next(s for s in FIELD_SPEC if s["key"] == key)
    labels = set(spec["labels"])
    labels.update(a for a, f in _TAIL_LABEL_TARGET.items() if f == key)
    return tuple(sorted(labels, key=len, reverse=True))


def _unmix_tail_segments(v, field: str) -> tuple[str, list[tuple[str, str]]]:
    """(发现41) 拆出字段值尾部的兄弟节标签段 → (主值, [(目标字段, 内容)…])。

    幂等: 拆过的值再进无变化。主段 <20 字、无映射标签、或主段是导航
    词链时返回原值+空列表（保守不动）。
    """
    v = (v or "").strip()
    if not v:
        return v, []
    hits = []
    for em in _TAIL_SPLIT_RE.finditer(v):
        lbl = em.group(0)
        if lbl == field:
            continue
        target = (_TAIL_LABEL_TARGET.get(lbl)
                  if lbl in _TAIL_LABEL_TARGET
                  else ("attr:" + lbl if lbl in _TAIL_LABEL_ATTR else None))
        if target is None:
            return v, []  # 无映射标签 → 保守不动
        if target == field:
            continue  # 同字段内容（学术成果里的"专著，出版社"）, 非漏
        ebef = v[max(0, em.start() - 1):em.start()]
        eaft = v[em.end():em.end() + 1]
        # 发现42: "研究方向：1.xxx" 编号列表是独立倾倒铁证（发现37 同款
        # 判定）, 即使粘在前词后（"研究所研究方向：1."）也切——薛嗣康
        # 整段 CV 倾倒实测。其他标签仍需独立性（句内词不切）。
        num_list = (lbl in ("研究方向", "研究领域", "主要研究方向")
                    and eaft in "：:" and re.match(r"\s*\d", v[em.end() + 1:]))
        if not num_list:
            if ebef and ebef not in "\n \t：:":  # 句内词, 非独立标签
                continue
            if eaft and eaft in "及和与":
                continue
            if lbl in _TAIL_AMBIGUOUS_LABELS and eaft not in "：:":
                continue  # 歧义短标签必须后跟冒号（课题组/专著，误切防护）
        hits.append((em.start(), lbl))
    if not hits or hits[0][0] < 20:
        return v, []
    main = v[:hits[0][0]].strip()
    if _is_nav_noise_value(main):
        return v, []  # 主段是导航词链（上财栏目页倾倒）, 不切
    moves = []
    for i, (start, lbl) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(v)
        seg = v[start:end].strip()
        content = re.sub(rf"^{re.escape(lbl)}[：: \t]*", "", seg).strip()
        if not content:
            continue
        if lbl in _TAIL_LABEL_TARGET:
            moves.append((_TAIL_LABEL_TARGET[lbl], content))
        else:
            moves.append(("attr:" + lbl, content))
    return main, moves


# 发现42 (2026-08-29): 页脚块与整页倾倒——LLM/启发式把页面底部
# （地址：…电话：…邮箱：…Copyright…）或整个栏目页导航拍进字段值。
# 两种形态: 1) 真内容+页脚尾（王磊学术成果/袁梦霆基金）→ 切掉页脚;
# 2) 纯整页倾倒（刘卫刚/卢秀双 教育经历=栏目导航+页脚）→ 整值清空。
_FOOTER_MARK_RE = re.compile(r"Copyright|版权所有|ICP[备证]|All rights reserved")
_FOOTER_LABEL_RE = re.compile(r"(?<!通讯)(?:地址|电话|邮箱|传真|邮编|E-mail|Email|电子邮箱|联系电话|办公地点)[：: ]")
_NAV_TAIL_MARK_RE = re.compile(r"师资队伍\s*▶")
_NAV_TAIL_STRONG_RE = re.compile(r"返回顶部|返回列表")


def _nav_token_hits(v) -> int:
    """整词导航 token 命中数（发现42 修正: 子串计数会误伤
    "青年教师授课竞赛"里的"教师"——张磊荣誉实测假阳性）。"""
    tokens = [t for t in re.split(r"[\s,，;；、/|]+", v or "") if t]
    return sum(1 for t in tokens if t in _NAV_NOISE_WORDS)


def _cut_footer_noise(v):
    """(发现42) 切掉字段值尾部的页脚块。页脚块起点 = 版权标记前 400 字
    窗口内最靠前的地址/电话/邮箱标签; 无版权标记或窗口内无标签则不动。
    幂等: 切过的值无页脚标记, 再进无变化。"""
    v = (v or "").strip()
    m = _FOOTER_MARK_RE.search(v)
    if not m:
        return v
    window_start = max(0, m.start() - 400)
    labels = list(_FOOTER_LABEL_RE.finditer(v, window_start, m.start()))
    if not labels:
        return v
    # 页脚块通常以"地址"起头: 优先切在地址（联系方式字段自身的
    # 电话/邮箱是内容, 不能切）; 无地址时第一个标签必须 ≥20 字进值
    # 才切（否则是字段自身内容, 保守不动）
    for lm in labels:
        if v[lm.start():lm.start() + 2] == "地址":
            return v[:lm.start()].strip()
    if labels[0].start() >= 20:
        return v[:labels[0].start()].strip()
    return v


def _is_page_dump_value(v) -> bool:
    """(发现42) 整值是否为整页倾倒（栏目导航+页脚+联系方式标签）。
    形态1: 页脚版权标记 + ≥5 整词导航 token + ≥2 联系方式标签——
    真实字段值几乎不可能同时满足（王磊学术成果只有页脚无导航词）。
    形态2: 无页脚（提取时被截断, 唐荣）——纯导航长链且无日期:
    教育/工作经历合法值几乎必带年份, 无日期 + ≥6 整词导航 token =
    栏目页倾倒。attr JSON 串（含 招生信息 等词）经整词切分后 token
    带引号/括号, 不会命中, 无需特判。
    """
    v = (v or "").strip()
    if not v:
        return False
    if _FOOTER_MARK_RE.search(v):
        return (_nav_token_hits(v) >= 5
                and len(_FOOTER_LABEL_RE.findall(v)) >= 2)
    if len(v) >= 100 and not re.search(r"(19|20)\d{2}", v):
        return _nav_token_hits(v) >= 6
    return False


def _cut_nav_tail(v):
    """(发现42) 切掉字段值尾部的导航尾（"师资队伍 ▶ 教职工…" 面包屑 /
    "返回顶部"/"返回列表"）。发现41 搬移把页面导航尾一并搬进了目标字段
    （云大信息学院 6 行实测）。"返回顶部/返回列表" 是 100% 导航标记,
    10 字后可切; "师资队伍▶" 保留 30 字守卫（防切剩链接碎片, 段亮）。
    幂等。"""
    v = (v or "").strip()
    for m in _NAV_TAIL_STRONG_RE.finditer(v):
        if m.start() >= 10:
            return v[:m.start()].strip()
    for m in _NAV_TAIL_MARK_RE.finditer(v):
        if m.start() >= 30:
            return v[:m.start()].strip()
    return v


# 表单块拆分（发现42: 车翔玖/康卓/张华 "学历：博士 电话：68775509
# E-mail：a@b.com 领域：智能计算…" 表单串被拍进教育经历）
_FORM_LABELS = ("所在学科专业", "学历学位", "最高学位", "电子邮箱", "办公地点",
                "导师身份", "政治面貌", "学历", "学位", "电话", "E-mail",
                "Email", "手机", "传真", "领域", "职称", "性别", "出生",
                "籍贯", "民族", "邮编", "所属系", "详细情况", "研究方向")
_FORM_SPLIT_RE = re.compile("(" + "|".join(_FORM_LABELS) + r")[：: ]")
_FORM_KEEP = frozenset({"学历", "学历学位", "最高学位", "学位"})
_FORM_CONTACT = frozenset({"电话", "E-mail", "Email", "电子邮箱", "办公地点",
                           "手机", "传真"})
_FORM_DIRECTION = frozenset({"领域", "研究方向", "所在学科", "所在学科专业"})


def _split_form_pairs(v, field: str = ""):
    """(发现42) 表单块拆分 → (新值, [(目标, 内容)…])。
    前导 >10 字（表单前有正文）或 <3 对 → 原值不动。幂等: 拆过的新值
    只含 学历类 标签, 再进 <3 对。"""
    v = (v or "").strip()
    if not v:
        return v, []
    # 发现42: 单对学历表单错位（马珊: 讲授课程="最高学历：2020年12月
    # 在陕西师范大学获…博士学位"）——整值恰是一个学历对 → 搬进教育经历。
    # 组内还有表单标签 = 多对表单（康卓"学历学位：博士 电话：…"）,
    # 交给下方多对拆分, 不在此拦截
    m = re.fullmatch(r"(最高学历|学历|学历学位|学位)[：: ]\s*(.{2,})", v)
    if m and field != "教育经历" and not _FORM_SPLIT_RE.search(m.group(2)):
        return "", [("教育经历", m.group(2).strip())]
    v = re.sub(r"电[\s\u00a0]*话", "电话", v)  # "电 话：" 归一
    parts = _FORM_SPLIT_RE.split(v)
    lead = parts[0].strip()
    pairs = []
    for i in range(1, len(parts) - 1, 2):
        lbl, val = parts[i], parts[i + 1]
        val = re.sub(r"^[：: \s]+|[\s]+$", "", val)
        val = re.sub(r"\s+", " ", val)
        if val:
            pairs.append((lbl, val))
    if len(pairs) < 3 or len(lead) > 10:
        return v, []
    keep, moves = [], []
    for lbl, val in pairs:
        if lbl in _FORM_KEEP:
            keep.append(f"{lbl}：{val}")
        elif lbl in _FORM_CONTACT:
            moves.append(("联系方式", val))
        elif lbl in _FORM_DIRECTION:
            moves.append(("研究方向", val))
        else:
            moves.append(("attr:" + lbl, val))
    return "；".join(keep), moves


# 发现43 (2026-08-29): 短碎片清理——LLM 从导航/节标签截取 2-3 字
# 碎片入内容字段（荣誉="情况"、基金="经理/组介绍"、学术成果="论文/著作"、
# 讲授课程="中心/建设"、荣誉="多项。"）。303 行全库实测。
# 字段适用域（短值在该字段几乎必为碎片）: 荣誉/基金/学术成果/课堂项目/
# 讲授课程。职称/职务/出生年月/联系方式/通讯地址 短值是正常值域;
# 教育经历="博士/博士后/硕士" 是诚实极简值（曾良页只有"博士"）;
# 研究方向 3 字学科名大量合法（代数学/保险学/美术学/隋唐史）——都排除。
# 讲授课程 的 2-3 字真课名（排球/法律史/税法）用词典保护, 缺词宁缺毋滥。
_TINY_FRAG_FIELDS = frozenset({"荣誉", "基金", "学术成果", "课堂项目", "讲授课程"})
_TINY_COURSE_LEGIT = frozenset({
    "排球", "法律史", "税法", "高数", "马原", "毛概", "思修", "史纲",
    "英语", "数学", "物理", "化学", "体育", "法学", "会计", "统计",
    "经济", "管理", "美学", "乐理", "声乐", "钢琴", "素描", "油画",
    "国画", "书法", "舞蹈", "体操", "篮球", "足球", "网球", "武术",
    "太极", "游泳", "田径", "听力", "口语", "写作", "翻译", "语法",
    "阅读", "泛读", "精读", "语音", "词汇", "素描", "色彩",
})
# 发现44 (2026-08-29): 4 字标签词碎片（清华: 学术成果="期刊论文/主要论著"、
# 讲授课程="研究领域"）——标签词不是内容, 宁缺毋滥。
_TINY_LABEL_WORDS = frozenset({
    "期刊论文", "学术论文", "主要论著", "开设课程", "研究领域", "代表作",
    "学术成果", "指导教师", "评审专家", "主要论文", "论文著作", "学术论文",
    "科研项目", "获奖情况", "研究兴趣", "学术论文", "期刊论文",
})


def _is_label_only_value(v: str) -> bool:
    """发现55 (2026-09-01): 纯标签尾碎片——值只是标签（可能被切掉头部的
    标签尾巴如"与领域："）加冒号, 无任何实际内容。北大李开龙 研究方向=
    "与领域："（真实标签"研究方向与领域："的尾巴）。≤10 字中文/括号 + 冒号。"""
    if not v or len(v) > 12:
        return False
    return bool(re.fullmatch(r"[一-龥（）()，,·\s]{1,10}[：:]", v))


def _is_tiny_fragment(v, field: str) -> bool:
    """(发现43/44) 内容字段的短值碎片判定（清空, 待 LLM 重提取）。

    ≤3 字全判（除课程词典/"无"）; 4 字只判确定性形态: 自字段名 /
    句尾标点 / 纯年份 / 标签词（"期刊论文"）——4 字真值（能源导论
    课程）不误杀。
    """
    v = (v or "").strip()
    if not v or field not in _TINY_FRAG_FIELDS:
        return False
    if v == "无":
        return False  # 页面明说"无", 诚实空值
    if field == "讲授课程" and v in _TINY_COURSE_LEGIT:
        return False
    if len(v) <= 3:
        return True
    if len(v) == 4 and (v == field
                        or re.search(r"[。．；）】]", v)
                        or re.fullmatch(r"\d{4}", v)
                        or v in _TINY_LABEL_WORDS):
        return True
    return False


# 2026-09-07 截断残留判定: 值以标点开头 = 上游字段切块把它切了一半
# （成理外语 陈东: 讲授课程="》，2024.11，第一" 是学术成果第1条尾部,
# 课堂项目="，主持人。" 是基金第1条尾部——首字段值已完整, 这里只是
# 重复残留）。正常字段值不以 ,。；：、》 等标点开头（引号除外）。
_PUNCT_LEAD_RE = re.compile(r"^[，。；：、》〉」』】〕)）\]\},.;:>]+")


def _is_punct_lead_fragment(v: str) -> bool:
    """标点开头的短截断残留（≤25 字）→ 清空。"""
    s = (v or "").strip()
    if not s or len(s) > 25:
        return False
    if s.startswith(("“", "‘", "「", "『", "(", "（")):
        return False  # 引号/括号开头可能是合法引用
    return bool(_PUNCT_LEAD_RE.match(s))


# 按学院提取规则（2026-08-30 用户指令: 一个学院一个学院设计提取规则）。
# 通用 FIELD_SPEC 标签规则对散文叙述型页面（北大中文系: 出生日期在
# "个人简介"散文里、"…《古文讲读》等课程" 后置标签、"获奖："段、
# "著作（13种）"段）全部落空——按学院登记页面结构专属规则,
# 启发式逐字段补空, 与 LLM 提取并行不冲突（LLM 优先, 规则补空）。
# 规则值 = 按序尝试的正则列表; 命中即取 group(1)（无 group 取整体）。
_DEPT_EXTRACT_RULES: dict[tuple[str, str], dict[str, list[str]]] = {
    ("北京大学", "历史学系"): {
        # 表单式头部 + 非标准节标签（通用词表无"任职简历/研究兴趣/论著",
        # LLM 对 key-value 表单页也漏提取, 全系 21 人仅 11 人有职称）
        "职称": [r"职称[：:]\s*(教授|副教授|讲师|助理教授|研究员|副研究员)"],
        "教育经历": [r"学历\s*[：:]\s*(.{10,400})"],
        "工作经历": [r"任职简历\s*[：:]\s*(.{10,500})"],
        "研究方向": [r"研究兴趣\s*([^。]{6,250})"],
        "讲授课程": [r"讲授课程\s*([\s\S]{5,250}?)(?=论著|联系方式|Email|邮箱|版权所有|主持)"],
        "学术成果": [r"论著\s*([\s\S]{10,800}?)(?=联系方式|Email|邮箱|版权所有)"],
    },
    ("北京大学", "哲学系（宗教学系）"): {
        # 三种模板: ①"个人简历： 丁一峰，男，湖南岳阳人，助理教授，主要研究领域为逻辑学…"
        # ②"个人简介 ： 仰海峰， 1969 年 12 月生。…现为北京大学哲学系教授、博士生导师"
        # ③"职称和职务：教授（2014年8月1日起）、博士生导师 主要研究领域：…"
        "职称": [r"职称和职务[：:]\s*([一-龥]{2,6})",
                 r"[男|女][，,]\s*[一-龥]{2,10}人[，,]\s*([一-龥]{2,6})",
                 r"现为[^。，]{0,20}?(教授|副教授|助理教授|研究员|讲师)"],
        "出生年月": [r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*生",
                     r"(?:19|20)\d{2}\s*年生"],
        "研究方向": [r"主要研究领域[为：:]\s*([^。]{6,200})",
                     r"研究领域[为：:]\s*([^。]{6,200})"],
        "职务": [r"职称和职务[：:]\s*[^、]{2,20}、([一-龥]{2,20})"],
    },
    ("北京大学", "中国语言文学系"): {
        # 个人简介散文: "安平秋（AnPingqiu），男，1941年10月31日生"
        # 教训: 页面正文里任何日期（出版/新闻）都会被裸日期正则抓到,
        # 孔庆东=2015/戴锦华=1991 全是假生日——必须带"日 生"出生标记,
        # 且只搜页首 2000 字（标题+个人简介区）
        "出生年月": [r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*生",
                     r"(?:19|20)\d{2}\s*年生"],
        # 后置标签: "开设过《史记》研究》、《古文讲读》、《传统文化与社会生活》、
        # 《资治通鉴讲读》等课程" —— 取"等课程"前连续的《》书目
        "讲授课程": [r"(《[^》]{2,25}》(?:[\s、，]*(?:和|与|及)?[\s、，]*))+等课程"],
        # 职称: 页首标题双模板（通用词表扫描被文中他人职称先命中,
        # 某教师A被误提取为副教授）:
        #   模板1 "安平秋 教授 个人简介"
        #   模板2 "某教师B 教授 某教师B，1954年生，…"（无个人简介标签）
        "职称": [r"[一-龥]{2,4}\s*(教授|副教授|讲师|助教|研究员|副研究员)\s*(?:个人简介|[一-龥]{2,4}，)"],
        # 荣誉: "获奖： 《日本宫内厅…》，…获…第6届国家图书奖"
        "荣誉": [r"获奖[：:]\s*([^。]{8,300})"],
        # 成果: "著作（13种） …" 段, 至"获奖"止。必须锚定编号形态
        # （著作（13种））——散文里"含著作、论文"的裸"著作"先出现,
        # 从那里起截到获奖超 600 字窗口, 规则落空
        "学术成果": [r"著作[（(]\d+种[)）]\s*([\s\S]{20,2000}?)(?=获奖|论文答辩|\[与港台)"],
    },
}


_DEPT_RULES_JSON_CACHE: dict[tuple[str, str], dict | None] = {}


def _load_dept_rules_json(school: str, dept: str) -> dict | None:
    """(2026-08-31) 懒加载学院规则——唯一权威源为院系目录
    <学校>/<学院>/extract_rules.json（2026-09-07 用户指令统一命名，
    中央库 dept_rules/ 已废弃删除）。
    None=无文件。缓存按 (school, dept)。"""
    key = (school, dept)
    if key not in _DEPT_RULES_JSON_CACHE:
        base = Path(__file__).resolve().parent
        candidates = [
            base / school / dept / "extract_rules.json",  # 院系目录（唯一源）
        ]
        rules = None
        for f in candidates:
            if not f.exists():
                continue
            try:
                import json as _json
                data = _json.loads(f.read_text(encoding="utf-8"))
                rules = data.get("rules") or None
                if rules:
                    break
            except Exception:
                continue
        _DEPT_RULES_JSON_CACHE[key] = rules
    return _DEPT_RULES_JSON_CACHE[key]


def _apply_rule_value(rule, text: str, html_path: str = "",
                      field: str = "") -> str | None:
    """单条规则求值（2026-08-31 混合规则形态, 用户要求 CSS 补充正则）。

    rule 两种形态:
      - str: 正则, 作用于纯文本 text（PDF/散文统一口径）
      - dict: {"css": 选择器, "attr": 可选属性名, "regex": 可选内正则}
        —— CSS 定位 DOM 元素（HTML 场景精确定位, 区分导航同名文本）;
        attr 读属性值（meta[name=description] 等 SEO 壳）; 无 regex 时
        整个元素/属性文本即值。
    返回 None = 未命中。
    2026-08-31: 正则求值用 regex 库 + 5s 超时——崔建莲页实测某条学院
    规则灾难回溯 100% CPU 卡死整系提取; 超时即跳过该规则不阻塞。
    """
    import regex as _regex

    def _search(pat: str, scope: str):
        try:
            return _regex.search(pat, scope, timeout=5)
        except TimeoutError:
            return None
        except _regex.error:
            return None

    if isinstance(rule, str):
        m = _search(rule, text)
        if not m:
            return None
        val = m.group(0) if field == "讲授课程" else (
            m.group(1) if m.lastindex else m.group(0))
        # 2026-09-01: 可选分组未参与匹配时 group(1) 返回 None（水利水电/
        # 中科大附属医院等 8 系反复失败的根因）——None 视为未命中
        return (val or "").strip() or None
    css = (rule or {}).get("css") if isinstance(rule, dict) else ""
    if not css or not html_path:
        return None
    try:
        html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
        el = BeautifulSoup(html, "lxml").select_one(css)
    except Exception:
        return None
    if el is None:
        return None
    if rule.get("attr"):
        scope = el.get(rule["attr"])
        if isinstance(scope, list):
            scope = " ".join(str(x) for x in scope)
        scope = (scope or "").strip()
    else:
        scope = el.get_text("\n", strip=True)
    if not scope:
        return None
    pat = rule.get("regex")
    if not pat:
        return scope
    m = _search(pat, scope)
    if not m:
        return None
    val = m.group(0) if field == "讲授课程" else (
        m.group(1) if m.lastindex else m.group(0))
    return (val or "").strip() or None


def _dept_html_abs(result: dict, scraper) -> str:
    """CSS 规则需要的原始 HTML 绝对路径（无则空串, CSS 规则自动跳过）。"""
    rel = result.get("html_path") or ""
    if not rel:
        return ""
    root = Path(getattr(scraper, "raw_data_root", "."))
    p = root / rel
    return str(p) if p.exists() else ""


def _clean_college_contacts(rows: list[dict]) -> int:
    """发现52 (2026-08-31): 院级共享联系方式清洗——模板页脚漏网时,
    同一批里 ≥3 名教师共享完全相同的邮箱/电话 token = 学院公共信息
    （清华数学 6 人共享 math-hr@tsinghua + 010-62783140 实测）→ 从
    所有行剥离该 token, 剥空后整字段置空。通讯地址按整值共享 ≥3 判学院级。"""
    from collections import Counter
    if len(rows) < 3:
        return 0
    email_tok: Counter = Counter()
    tel_tok: Counter = Counter()
    addr_tok: Counter = Counter()
    for r in rows:
        v = _field_display(r.get("联系方式"))
        for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", v):
            email_tok[m.lower()] += 1
        for num in re.findall(r"0\d{2,3}-?\d{7,8}", v):
            tel_tok[num] += 1
        a = _field_display(r.get("通讯地址"))
        if a:
            addr_tok[a] += 1
    bad_emails = {e for e, n in email_tok.items() if n >= 3}
    bad_tels = {t for t, n in tel_tok.items() if n >= 3}
    bad_addrs = {a for a, n in addr_tok.items() if n >= 3}
    if not bad_emails and not bad_tels and not bad_addrs:
        return 0
    cleaned = 0
    for r in rows:
        v = _field_display(r.get("联系方式"))
        if v:
            for e in bad_emails:
                v = v.replace(e, "")
            for t in bad_tels:
                v = v.replace(t, "")
            # 清理残留骨架: 空分隔符/空标签（"邮箱: ; 电话："）
            v = re.sub(r"\s*[;；,、]\s*(?=[;；,、]|$)", "", v)
            v = re.sub(r"(?:邮箱|E-mail|Email)[:：]?\s*[;；,、]*\s*$", "", v)
            v = re.sub(r"(?:电话|Tel|TEL)[:：]?\s*$", "", v)
            v = v.strip(" ;；,，:：")
            if v != _field_display(r.get("联系方式")):
                r["联系方式"] = v
                cleaned += 1
        a = _field_display(r.get("通讯地址"))
        if a and a in bad_addrs:
            r["通讯地址"] = ""
            cleaned += 1
    return cleaned


def _apply_dept_rules(result: dict, school: str, dept: str, raw_text: str,
                      html_path: str = "") -> None:
    """(2026-08-30) 按学院规则提取——散文叙述型页面的专属结构规则。

    命中即覆盖（手设计的学院规则最专, 压过 LLM/通用结果——安平秋
    职称被 LLM 误提副教授、课程被通用切块切成散文, 全靠规则压回）。
    课程类规则命中后拆《》条目拼接。
    2026-08-31: 规则支持 CSS 选择器形态（dict, 用户要求 HTML 场景
    DOM 定位补充正则）——需要 html_path 传入原始 HTML 文件绝对路径。"""
    rules = _DEPT_EXTRACT_RULES.get((school, dept))
    if not rules:
        rules = _load_dept_rules_json(school, dept)  # 子代理 JSON 规则源
    if not rules:
        return
    # 2026-10-27 规则可信度分层(用户定调: LLM 直接提取通常更好):
    #   手写规则(_DEPT_EXTRACT_RULES 硬编码/改进工作流验证的) → 命中即覆盖;
    #   自动生成规则(extract_rules.json templates 含"自动生成"标记,
    #   机械模板未人工验证) → 择优保留: 规则值质量分严格高于 LLM 值才覆盖。
    _is_auto = bool(re.search(
        r"自动生成", str(rules.get("templates", "")))) if isinstance(
        rules, dict) and "templates" in rules else False
    # 页首限制: 职称/出生年月 只搜标题+个人简介区（页首 2000 字）,
    # 否则文中他人职称/出版日期先命中（陈平原副教授/孔庆东2015 实测）
    # CSS 规则自带元素定位, 不受页首窗口限制（窗口只约束正则规则）
    head = raw_text[:2000]
    for f, patterns in rules.items():
        scope = head if f in ("职称", "出生年月") else raw_text
        for pat in patterns:
            val = _apply_rule_value(pat, scope, html_path, f)
            if not val:
                continue
            # 发现58: 规则值形态校验——空 tab 形态规则会抓下一标签词
            # （"研究方向" tab 头 → 规则捕获"发表论文", 上交系统生物
            # 医学研究院 LLM 规则实测）; "研究方向与成果：" 复合标题
            # 规则切出"与成果："纯标签尾碎片（中科大实验室实测）。纯
            # 标签值/自述空值不进字段, 跳过该规则让启发式兜底
            if (_SECTION_END_RE.match(val)
                    or re.match(r"^(?:无|暂无|空缺|空)(?:[\s。；;）)]|$)", val)
                    or re.match(
                        r"^(?:[与和及或])?(?:方向|成果|领域|经历|项目|课程"
                        r"|信息|简介|荣誉|奖励)[：:]*$", val)):
                continue
            # 讲授课程 拆《》条目拼接
            if f == "讲授课程" and "《" in val:
                items = re.findall(r"《([^》]{2,25})》", val)
                if items:
                    val = "；".join(dict.fromkeys(items))  # 去重保序
            # 2026-10-27 择优保留(用户定调: LLM 直接提取通常更好, 规则是
            # 机械匹配——「命中即覆盖」会拿规则值压掉 LLM 的更好结果):
            # 仅自动生成规则走择优——规则值质量分严格更高才覆盖 LLM 值;
            # 手写规则(改进工作流人工验证过)保持命中即覆盖(安平秋副教授
            # →教授 靠手写规则压回)。
            if _is_auto:
                _llm_val = result.get(f) or ""
                if _llm_val.strip() and _field_quality_score(val) <= \
                        _field_quality_score(_llm_val):
                    break  # LLM 值不劣于规则值: 保留 LLM
            # 学院规则命中即覆盖——通用切块在散文页会切出垃圾值
            # （安平秋 讲授课程被通用规则切成学生培养散文、荣誉切成
            # 页脚）, 手设计的学院规则更专, 必须压过通用结果
            result[f] = val
            break


def _merge_tail_target(tv, content):
    """(发现41) 搬移目标字段已有值时的确定性合并。

    同页同节提取两次 → moved ⊆ target → 丢弃; 目标只是片段头部
    （贡艺/唐万恺: 目标="大洋性鲨鱼、鱿鱼" 而尾部是全文）→ 目标 ⊆
    moved → 用更全的替换; 两者互不包含（不同切片, 张贺基金）→ 追加。
    """
    ntv, ncv = re.sub(r"\s+", "", tv or ""), re.sub(r"\s+", "", content or "")
    if not ncv:
        return tv or ""
    if not ntv:
        return content
    if ncv in ntv:
        return tv
    if ntv in ncv:
        return content
    return tv + "；" + content


# 表格列头词（发现26, 2026-08-29）: 表格型教师主页（合肥工业 faculty.hfut
# 等）被 LLM 整表拍平归入字段——"学位 硕士 毕业学校 从事专业 职务 所属
# 院系 所在部门 职称 高级实验师…" 列头与值交替无冒号。真实个人简介是
# "key：value" 格式（列头后紧跟冒号）——那种是合法内容, 不判噪声。
_TABLE_HEADER_WORDS = (
    "学位", "毕业学校", "从事专业", "职务", "所属院系", "所在部门",
    "职称", "联系方式", "通讯地址", "邮编", "最高学位", "学历",
    "担任导师", "研究方向", "办公电话", "联系电话", "电子邮箱",
    "E-mail", "Email", "手机", "电子邮件",
)


def _is_table_header_chain(v: str) -> bool:
    """字段值是否为表格列头链（列头与值交替、无冒号 ≥4 词）→ 清空。

    2026-08-29 收紧（发现27）: 阈值 3→4。吉大数学学院 bio 倾倒值
    "联系方式 电子邮箱 / 研究方向 Yang-Baxter方程 / 学位管理" 恰好
    3 个表头词后跟空格被误清——那是 LLM 整页倾倒, 非表格拍平, 数据
    应保留。真实表格型教师主页列头链（合肥工业 张晶/王景运）有
    6-10 个列头, ≥4 无漏网。
    """
    if not v:
        return False
    hits = 0
    for h in _TABLE_HEADER_WORDS:
        m = re.search(re.escape(h), v)
        if not m:
            continue
        after = v[m.end():m.end() + 1]
        if after not in ("：", ":"):
            hits += 1  # 列头后是冒号 → 真实 key:value 简介, 不算;
                       # 列头后是空格/值 → 表格拍平噪声特征, 算命中
    return hits >= 4


# ── 发现29: 空节标签剥离（治本）──
# LLM 对"标签存在但内容为空"的 section 不可靠: 李伯虎（"学习经历\n工作经历\n
# 历任…"）/某教师（"教育经历\n工作经历\n个人简介…"）实测——教育经历字段
# 倾倒后续全部 section, prompt 规则（严禁整页倾倒/空节检测）两次重跑无效。
# 文本层治本: 检测"结构标签行后紧跟另一结构标签行"（=空节）, 把空节标签
# 从文本删除——LLM 看不到标签就不会倾倒。仅在标签后紧邻结构标签时才删,
# 标签后跟真实内容（值/段落）绝不删 → 不会误杀真实教育经历/工作经历。
_EMPTY_SECTION_LABELS = ("教育经历", "学习经历", "工作经历")
_EMPTY_SECTION_ANY = frozenset({
    "教育经历", "学习经历", "工作经历", "个人简介", "个人介绍", "个人资料",
    "研究方向", "研究领域", "讲授课程", "开授课程", "教学及科研成果",
    "联系方式", "学术成果", "科研成果", "科研项目", "在研科研项目",
    "社会兼职", "基本信息", "招生与培养", "荣誉", "荣誉及奖励", "奖励",
    "代表性成果", "论文下载", "获奖情况",
})
_EMPTY_SECTION_LINE_RE = re.compile(r"^(教育经历|学习经历|工作经历)[\s/|、，,;；:：]*$")
_EMPTY_SECTION_INLINE_RE = re.compile(
    r"(教育经历|学习经历|工作经历)[\s/|、，,;；:：]+"
    r"(?=(教育经历|学习经历|工作经历|个人简介|个人介绍|个人资料|研究方向|"
    r"研究领域|讲授课程|开授课程|教学及科研成果|联系方式|学术成果|科研成果|"
    r"科研项目|在研科研项目|社会兼职|基本信息|招生与培养|荣誉|荣誉及奖励|"
    r"奖励|代表性成果|论文下载|获奖情况))"
)


# 段落字段切块终点（发现30: 字段边界溢出）——启发式从起点标签切 section_len
# 字, 若页面在起点标签后还有其他结构小节（西南石油土木实测: "教育经历"后跟
# "学术/技术组织任职/社会工作任职/教学及教学研究/讲授课程/教改项目" 等小节
# 标签, 门禁 MISALIGN 检出 9 例教经含"讲授课程"）会一路吞入教经字段。
# 扫描起点后第一个"独立出现"的结构标签即截断。同系互为终点（工作经历是
# 教育经历的终点, 反之亦然）; 起点标签自身的重复出现不算终点。
_SECTION_END_LABELS = (
    # 教育/工作（互为终点）
    "教育经历", "教育背景", "学习经历", "工作经历", "工作履历", "任职经历",
    # 研究方向
    "研究方向", "研究领域", "科研方向", "研究兴趣",
    # 成果/论文
    "学术成果", "代表性成果", "科研成果", "研究成果", "学术论著",
    "论文发表", "论文与专利", "主要论文", "发表论文", "学术论文",
    "主要学术论文", "论文",
    "著作目录", "学术著作", "主要学术著作", "代表著作", "专著", "出版著作",
    # 项目/基金
    "科研项目", "主要科研项目", "在研项目", "在研科研项目", "教改项目",
    "教学项目", "科研基金", "基金项目", "主持项目", "课题",
    # 课程/教学
    "讲授课程", "主讲课程", "承担课程", "教学及教学研究",
    "教学与科研", "教学研究", "教学成果",
    # 荣誉
    "获奖情况", "荣誉及奖励", "荣誉", "获奖", "人才称号",
    # 任职/兼职（含斜杠/与 变体: "学术/技术组织任职" / "学术组织任职"）
    "学术/技术组织任职", "学术与技术组织任职", "学术组织任职",
    "社会工作任职", "社会任职", "学术任职", "社会兼职",
    "学术兼职", "现任职务", "职务",
    # 其他
    "联系方式", "通讯地址", "个人简介", "个人介绍", "个人资料",
    "基本信息", "招生信息", "招生与培养", "学生培养", "教育感言",
    "出生年月", "出生日期", "电子邮箱", "联系电话",
)
_SECTION_END_RE = re.compile(
    "|".join(re.escape(l) for l in
             sorted(_SECTION_END_LABELS, key=len, reverse=True)))

# 发现41: 尾漏切分用扩展标签集（_SECTION_END_LABELS + 教学情况等补充,
# 仅在存量搬移函数使用, 不改提取期终点检测行为）
_TAIL_SPLIT_RE = re.compile(
    "|".join(re.escape(l) for l in sorted(
        set(_SECTION_END_LABELS)
        | {"教学情况", "主要研究方向", "代表性课题", "代表性论文"},
        key=len, reverse=True)))

# 歧义短标签（发现41 dry-run: "课题"误切"课题组"→邹雪华教经被切坏、
# "专著"误切"专著，出版社"→王维红同字段自搬移）——这些词常作句内词,
# 只有后跟冒号（"课题："）才是结构标签。多字专用标签（科研项目/
# 获奖情况/教学情况…）保持空白也算独立。
_TAIL_AMBIGUOUS_LABELS = frozenset({
    "论文", "课题", "著作", "专著", "专利", "荣誉", "获奖", "职称", "职务",
})

# 职称权威标签（发现31）: "职称/技术职称/现任职称:" 标签值是当前职称的权威
# 来源。安大材料 PDF 实测: LLM 被工作经历历史职称误导（周腾飞 PDF 技术职称
# =教授 提取=副研究员; 17 例不一致）; 启发式词表全文扫描"副研究员"先于
# "教授"命中历史职务。标签值优先, 匹配标准职称词; "暂无/无"置空; 匹配
# 不到标准词（如"正高"）保留原值。
# 发现34: 词表匹配必须「最长优先」——子串匹配会让"研究员"吞掉"助理研究员"
# （"研究员" in "助理研究员"），迭代顺序也不能依赖词表书写顺序。标签正则
# 停用空格:"技术职称：助理研究员 聘任方式：校内双聘"→只捕获"助理研究员"。
_ZC_TAG_RE = re.compile(r"(?:现任职称|技术职称|职称)[：:]\s*([^，。；;\s]{1,20})")
_ZC_NONE = {"暂无", "无", "无职称", "未定", "未评定", "待定"}
_ZC_VALUES = sorted(
    (w for s in FIELD_SPEC if s["key"] == "职称" for w in (s.get("values") or [])),
    key=len, reverse=True)


def _match_zhicheng_value(tv: str) -> str:
    """词表最长优先匹配（发现34: 助理研究员/副研究员不被研究员吞掉）。"""
    for kw in _ZC_VALUES:
        if kw in tv:
            return kw
    return ""


# 发现35: 叙述型简历时间线（无教育/工作经历标签）——"个人简历："
# 下日期条目混排（龙明生/侯杰/李超等 PDF 实测）。日期起始识别兼容
# "2019/7-" 与 "2026.01-" 两种格式; 条目含学历信号 → 教育, 否则工作。
# 发现35加固: 起始日期必须带月份分隔符（[./年]）——真时间线全带
# （"2019/7""2026.01""2013年10月"），书名/著作年份区间（"1949-2011"）
# 是裸年份, 防止把著作目录/出版记录当时间线 dump 进教经工经。
_NARRATIVE_LABELS = ("个人简历", "简历")
_NARRATIVE_DATE_START = re.compile(
    r"(?=\d{4}[./年]\d{1,2}月?\s*[-~至]\s*"
    r"(?:至今|现在|今|present|Present|"
    r"\d{4}(?:[./年]\d{1,2}月?)?))")
_NARRATIVE_DEGREE = re.compile(r"学历|博士|硕士|学士|本科|研究生|博士后")


def _narrative_timeline(raw_text: str) -> tuple[str, str]:
    """叙述型简历时间线切分（发现35）: 返回 (教育经历, 工作经历)。

    仅当"个人简历/简历"标签后的叙述段含 ≥2 个日期条目才生效——
    散文型简历（"2018年博士毕业于…"）无日期时间线, 返回空（宁缺毋滥,
    交给 LLM prompt 处理）。
    """
    if not raw_text:
        return "", ""
    seg = ""
    for label in _NARRATIVE_LABELS:
        for m in re.finditer(re.escape(label), raw_text):
            before = raw_text[max(0, m.start() - 1):m.start()]
            after = raw_text[m.end():m.end() + 1]
            if before and before not in "\n \t：:":
                continue
            if after not in ("：", ":"):
                continue
            start = m.end()
            end = len(raw_text)
            for em in _SECTION_END_RE.finditer(raw_text, m.end()):
                if em.group(0) == label:
                    continue
                ebef = raw_text[max(0, em.start() - 1):em.start()]
                if ebef and ebef not in "\n \t：:":
                    continue
                end = min(em.start(), end)
                break
            seg = raw_text[start:end]
            break
        if seg:
            break
    if not seg:
        return "", ""
    splits = [mm.start() for mm in _NARRATIVE_DATE_START.finditer(seg)]
    if len(splits) < 2:
        return "", ""
    edu, work = [], []
    for i, s in enumerate(splits):
        entry = seg[s:splits[i + 1] if i + 1 < len(splits) else len(seg)].strip()
        if _NARRATIVE_DEGREE.search(entry):
            edu.append(entry)
        else:
            work.append(entry)
    return "；".join(edu), "；".join(work)


def _verify_zhicheng(result: dict, raw_text: str) -> None:
    """职称以"职称/技术职称/现任职称:"标签值为准（发现31/34）。"""
    m = _ZC_TAG_RE.search(raw_text)
    if not m:
        return
    tv = m.group(1).strip()
    if tv in _ZC_NONE:
        result["职称"] = ""
        return
    kw = _match_zhicheng_value(tv)
    if kw:
        result["职称"] = kw
    # 标签值匹配不到标准职称词（如"正高"）: 保留原值


def _verify_zhicheng_near_name(result: dict, raw_text: str) -> None:
    """发现54 (2026-09-01): 职称=导航词误判（北大李开龙实为研究员, 页首
    导航菜单"两院院士"先命中）。教师主体定位: 姓名出现位置 ±80 字窗口内
    的职称词优先——菜单词离姓名远, 窗口外。"""
    name = str(result.get("姓名") or "").strip()
    if not name or len(name) < 2 or not raw_text:
        return
    cur = result.get("职称") or ""
    for m in re.finditer(re.escape(name), raw_text):
        window = raw_text[max(0, m.start() - 80):m.end() + 80]
        kw = _match_zhicheng_value(window)
        if kw and kw != cur:
            result["职称"] = kw
            return


# PDF 简历密集排版行内标签（发现31）: pdfplumber extract_text 把同一视觉行
# 文字连排, "…物理系，本科工作经历（科研与学术工作经历，按时间倒序排序）："
# 的"工作经历"前是"科"（非行首/冒号）, 启发式独立判定（行首/空白/冒号前导）
# 失效 → 工经切不出。标签后跟括号（"工作经历（括注）："）是强标签信号,
# 在标签前插换行; "多年工作经历""我的研究"（后无括号）不触发, 避免误切正文。
_PDF_SECTION_INLINE_LABELS = (
    "教育经历", "教育背景", "学习经历", "工作经历", "工作履历", "任职经历",
    "个人简历", "研究方向", "学术成果", "科研成果", "讲授课程",
    "联系方式", "学术经历", "教育及工作经历", "学习和工作经历",
)
_PDF_SECTION_INLINE_RE = re.compile(
    r"(?<=[^\s：:])(?:" +
    "|".join(re.escape(l) for l in
             sorted(_PDF_SECTION_INLINE_LABELS, key=len, reverse=True)) +
    r")(?=[（(])")


def _insert_pdf_section_newlines(text: str) -> str:
    """PDF 提取文本行内标签切分: "…本科工作经历（…" → "…本科\n工作经历（…"。
    仅标签后跟括号时插入（括注=标签形态）; 正文"多年工作经历"不受影响。"""
    if not text:
        return text
    return _PDF_SECTION_INLINE_RE.sub(r"\n\g<0>", text)


def _strip_empty_section_tags(text: str) -> str:
    """剥离空节结构标签（发现29）: 标签后紧跟另一结构标签 → 该标签为空节,
    删除之, 防 LLM 把后续内容倾倒进空节字段。有内容的标签（后跟值）不删。"""
    if not text:
        return text
    # 行级: 标签独占一行且下一非空行也是结构标签 → 删该行
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _EMPTY_SECTION_LINE_RE.match(line):
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            nxt = lines[j].strip() if j < n else ""
            nxt_label = re.split(r"[\s/|、，,;；:：]", nxt)[0]
            if nxt_label in _EMPTY_SECTION_ANY:
                i += 1  # 空节标签 → 丢弃本行
                continue
        out.append(line)
        i += 1
    text = "\n".join(out)
    # 行内: "教育经历 / 工作经历 / …" 同行分隔 → 删空节标签
    text = _EMPTY_SECTION_INLINE_RE.sub("", text)
    return text


def _is_nav_noise_page(text: str) -> bool:
    """整页正文是否主要由导航词构成（教师详情页正文缺失/占位页）。

    上音教训: 教师名录详情页 1100 字几乎全是站内导航（SHCM 学生
    教职工 校友 … 荣誉教授 客座教授 教师名录 …），正文只有姓名。
    与字段值判定不同: 对整页 token 统计命中率（阈值 0.5, 页面级
    容忍度更高——短页导航占比天然大）。
    >5000 字不判: 超长文本大概率含真实正文（R13 放宽 2000→5000:
    北大某教师甲 1175 字导航壳/南师大 746 字壳均在原范围内, 但存在
    更长导航壳; 词表为整词/高选择性子串命中, 真实长正文导航词
    占比远低于 0.5, 放宽无误判风险）。
    """
    if not text or len(text) > 5000:
        return False
    tokens = [t for t in re.split(r"[\s,，;；、/|。．·]+", text) if t]
    if len(tokens) < 5:
        return False
    hits = sum(1 for t in tokens
               if t in _NAV_NOISE_WORDS or any(k in t for k in _NAV_NOISE_SUB))
    return hits / len(tokens) >= 0.5


# 职称合法值白名单（FIELD_SPEC 的 values）——职称字段值是合法职称时
# 不能被导航噪声清理误清（"教授"来自真实页 vs 来自导航"荣誉教授"
# 无法用值本身区分, 由页面级导航壳判定 + title_only 渲染救回/判死处理）
_TITLE_VALUE_SET = frozenset(
    w for s in FIELD_SPEC if s["key"] == "职称" for w in (s.get("values") or []))


# ── tab 式详情页标签词（武大 cs.whu.edu.cn 教训, 2026-08-27）──
# 页面按 tab 组织内容（教育背景/工作经验/教授课程/…），静态 HTML 只有
# tab 标签词串, 正文由 JS 异步加载。LLM 会把标签串当字段值
# （教育经历="教育背景 工作经验 教授课程…"、学术成果="科研课题"）。
# 字段值完全由这些词构成(≥2 词) 或等于单个强内容标签 → 清空,
# 交给渲染兜底从渲染后 DOM 救回。
_TAB_LABEL_WORDS = frozenset({
    "教育背景", "工作经验", "工作经历", "教授课程", "讲授课程", "发表论文",
    "科研课题", "研究团队", "知识产权", "学术服务", "成果展示", "个人简介",
    "个人简历", "基本信息", "研究方向", "研究成果", "招生信息", "联系方式",
    "获得荣誉", "获奖情况", "论文发表", "其他",
})
# 单个词即完整字段值的强内容标签（真实字段值几乎不可能只等于这些词）
_TAB_SINGLE_WORD_MAX = 12


def _is_tab_label_chain(v: str) -> bool:
    """字段值是否由 tab 标签词串构成（≥2 个标签词且全是标签词）。

    长度 >120 不判（可能是真实内容拼接，避免误清）。单 token 命中
    强内容标签(科研课题/发表论文等, ≤12 字)也算——作为完整字段值
    几乎必然是 tab 词而非真实内容。
    """
    if not v or len(v) > 120:
        return False
    tokens = [t for t in re.split(r"[\s,，;；、/|]+", v) if t]
    if not tokens:
        return False
    if len(tokens) >= 2:
        return all(t in _TAB_LABEL_WORDS for t in tokens)
    return len(tokens) == 1 and tokens[0] in _TAB_LABEL_WORDS and len(v) <= _TAB_SINGLE_WORD_MAX


# ── SPA 壳检测（武大 cs 教训）: Vue 详情页静态抓取只有字段标签 +
#    少量首屏字段, 正文 JS 异步加载。正文 <400 字但已有字段结构
#    (≥2 个内容标签词) → 内容不完整, 需要渲染救回。误触发代价仅是
#    一次渲染（渲染后有 _label_count 密度验证兜底, 无提升不重提取）。
_SPA_SHELL_LABELS = (
    "教育背景", "工作经验", "工作经历", "研究方向", "学术成果",
    "发表论文", "科研课题", "讲授课程", "个人简介", "主要经历",
    "学习经历", "论文成果", "招生信息",
)


def _is_spa_shell(text: str) -> bool:
    """正文是否 JS 渲染空壳（有字段标签但内容缺失）。"""
    if not text or len(text) >= 400:
        return False
    hits = sum(1 for lbl in _SPA_SHELL_LABELS if lbl in text)
    return hits >= 2


# ── 邮箱反爬提示还原（武大 cs 教训: "zhcai ▇ whu.edu.cn 请手工替换符号"
#    是站点的字符替换防爬, 还原为真实邮箱 zhcai@whu.edu.cn）──
_EMAIL_SCRAMBLE_RE = re.compile(
    r"([\w.\-]+)\s*[▇▲◆●■□★☆]\s*([\w.\-]+\.[a-zA-Z]{2,})"
    r"(?:\s*请\s*手工\s*替换\s*符号?)?", re.UNICODE)
# 2026-10-24 at/dot 文字替换防爬（北大数学田刚: "gtian at math dot pku
# dot edu dot cn" → gtian@math.pku.edu.cn）——全库常见防爬形态之一
_EMAIL_ATDOT_RE = re.compile(
    r"([\w.\-]+)\s+at\s+([\w.\-]+(?:\s+dot\s+[\w.\-]+)+)",
    re.UNICODE | re.IGNORECASE)


def _restore_scrambled_email(v: str) -> str:
    """还原被字符替换防爬打码的邮箱（"zhcai ▇ whu.edu.cn" → zhcai@whu.edu.cn）。

    发现19（2026-08-28, 武大重刷 evaluate）: WebPlus 还有两类残留——
      "yxhe@whu.edu.cn（请手工替换符号）": 邮箱本体完整, 尾部带提示文案
      "kevin.lifei @ gmail.com": @ 前后加空格防爬
    2026-10-24: "gtian at math dot pku dot edu dot cn" 文字替换形态一并还原
    一并清理, 保证联系方式字段只含可用邮箱。
    """
    if not v:
        return v
    v = _EMAIL_SCRAMBLE_RE.sub(lambda m: f"{m.group(1)}@{m.group(2)}", v)
    # at/dot 文字替换（gtian at math dot pku dot edu dot cn → 真实邮箱）
    v = _EMAIL_ATDOT_RE.sub(
        lambda m: m.group(1) + "@" +
        re.sub(r"\s+dot\s+", ".", m.group(2)), v)
    # @ 前后空格防爬（"lifei @ gmail.com" → lifei@gmail.com）
    v = re.sub(r"\s*@\s*", "@", v)
    # 尾部提示文案剥离（"（请手工替换符号）" / "请手工替换符号"）
    v = re.sub(r"[\s（(]*请\s*手工\s*替换\s*符号?[\s)）]*", "", v)
    return v.strip("；;，, ")


class HttpStatusError(ValueError):
    """HTTP 状态码失败（4xx/5xx = URL 失效/被拒, 确定性失败——重试不会
    改变结果, 必须快速失败而非空耗降级链重试）。"""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# 反爬/风控拦截状态码 —— 不是"URL 失效"语义, 不得按确定性失败快速失败:
#   - 202/412: 瑞数 WAF 特征（util/smart_fetch._is_ruishu 同款判定）
#   - 452: 常见 WAF/风控拦截码（西交大 2026-08-23 实测）
# 这类状态码代表"站点有反爬需要浏览器级", 重试/换层可能成功（兰大实测
# 仅 DrissionPage 层可过）; 404/410/5xx 才是 URL 失效/服务错误。
_RUISHU_STATUS = frozenset({202, 412, 452})


# ── 失败重跑队列判定 (P1-6) ──
# fetch 失败分两类:
#   - 确定性(重试无效): HTTP 4xx/5xx(URL 失效/被拒, HttpStatusError 快速失败)、
#     名单数据问题(名单URL无效/名单垃圾词——重试也是同样结果)
#   - 瞬时(可重试): 超时/断连/返回空/代理抖动——重跑一次常可自愈
# 采集完成后 process_batch 汇总可重试失败, 限速重试 1 次。
_RETRYABLE_FETCH_STATUS_RE = re.compile(r"HTTP [45]\d\d")


def _is_retryable_failure(r: dict) -> bool:
    """失败是否值得自动重跑一次 (P1-6)。"""
    if r.get("来源类型") != "fetch_failed":
        return False
    reason = r.get("失败原因") or ""
    if reason.startswith("名单"):
        return False
    if _RETRYABLE_FETCH_STATUS_RE.search(reason):
        return False
    return True


def _page_mentions_teacher(title: str, text: str, name: str) -> bool:
    """页面是否把教师姓名作为"独立词"提及（云大教训: 上游把栏目导航词
    当教师名, "教学建设"对应页面 title="本科教学建设-云南大学…",
    "教研风采"对应 title="师资队伍-云南大学…"——都不是教师详情页,
    却凭导航串/学院总机被提取出假字段, 成了假成功记录）。

    真实教师详情页 title 形态: "杨学功-北京大学哲学系" / "杨学功 教授
    简介" / "杨学功"。栏目页 title 要么不含姓名, 要么姓名只是栏目词
    的子串（"本科教学建设"含"教学建设"）——所以必须边界匹配, 不能
    子串匹配。

    规则（只判强证据, 宁可漏判不可误杀真实教师页）:
      - title 含姓名独立词 → 详情页特征 → 放行
      - title 含姓名但非独立词（栏目词嵌入）→ 判死（"本科教学建设"）
      - title 完全无姓名 → 正文含独立词则放行（title 模板站, 如通用
        "教师简介"）, 正文也无 → 判死
    """
    n = (name or "").strip()
    if not n or len(n) < 2:
        return True
    if not title and not text:
        return True

    def _independent(haystack: str) -> bool:
        # 边界: 开头/结尾/分隔符/括号前后。姓名后直接跟常见后缀
        # （简介/教授/副教授/讲师/博士/老师/主页）也算独立词
        pat = re.compile(
            r"(?:^|[-—_·|:：/\\,，。\s(（【\[])"
            + re.escape(n)
            + r"(?:$|[-—_·|:：/\\,，。\s)）】\]])"
            + r"|(?:^|[-—_·|:：/\\,，。\s)）】\]])"
            + re.escape(n)
            + r"(?:$|[-—_·|:：/\\,，。\s(（【\[])"
        )
        if pat.search(haystack):
            return True
        return bool(re.search(
            re.escape(n) + r"(?:的简介|简介|教授|副教授|讲师|博士|老师|主页|个人主页|专家|学者)",
            haystack))

    if _independent(title):
        return True
    if n in title:
        # title 含姓名但非独立词 → 栏目词嵌入（"本科教学建设"）
        return False
    if _independent(text):
        return True
    return False


def _page_is_latin(text: str) -> bool:
    """页面正文是否以拉丁字母为主（外教/英文页——姓名校验应放行,
    中文姓名不会出现在英文页面里, 无法用中文名做一致性判断）。"""
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    if not letters:
        return False
    return sum(1 for ch in text if ch.isascii() and ch.isalpha()) / letters >= 0.8


def _page_name_mismatch(title_text: str, raw_text: str, name: str) -> bool:
    """姓名一致性校验（发现32）: 返回 True = 页面与该教师无关
    （栏目/列表页/URL错配）应判死。

    云大教训: 栏目导航页被当教师详情页, 凭导航串提取出假字段 → 假成功
    记录。教师详情页的 title/正文必然把姓名作为独立词提及。两层:
      1. _page_mentions_teacher: title 含名独立词 → 放行; title 无名字
         时正文含名独立词 → 放行
      2. R13 列表页主体性判定: 名字只出现 1 次且 title 不含名 → 疑似
         列表页（某教师甲: 北大哲学系教研室列表页）。发现32 加位置防护:
         名字在文本前 30% = 个人简介开头的真实个人页（西南石油某教师乙:
         正文"某教师乙，教授，博士生导师…"仅 1 次名, title="吴 斌-土木"
         带空格, 被误判列表页判死 → 数据缺失）
    """
    n = (name or "").strip()
    if n in ("", "未知") or not raw_text:
        return False
    # 发现32: title 去空格变体——"吴 斌-土木工程与测绘学院"是字体渲染的
    # 真实个人页 title, 栏目页 title 无此形态
    title_norm = (title_text or "").replace(" ", "").replace("　", "")
    latin = _page_is_latin(raw_text)
    if not _page_mentions_teacher(title_norm, raw_text, n) and not latin:
        return True
    if (raw_text.count(n) <= 1
            and (not title_norm or n not in title_norm)
            and not latin):
        idx = raw_text.find(n)
        if idx < 0 or idx > len(raw_text) * 0.3:
            return True
    return False


def classify_record(row: dict) -> str:
    """按 25 列记录判断采集状态 —— 评估/回补共用的单一口径。

    返回:
      - "ok":          有内容产出（PDF 已落地 / HTML 正文或壳已保存）
      - "pending_pdf": 检测到 PDF 引用但未落地（pdf_url 有、pdf_path 空）
      - "failed":      详情页请求失败或处理异常
      - "no_url":      无详情页（纯名单，合法状态）
      - "unknown":     其余

    注意不要只用 path 字段判断成败:
      - html_path 空 ≠ HTML 失败（复用 PDF 无壳的旧数据也空）
      - pdf_path 空 ≠ 无 PDF（下载失败时 pdf_url 有值 = 待回补）
      - has_pdf 语义 = "检测到 PDF 引用"，不是"已落地"
    """
    src = (row.get("来源类型") or "").strip()
    pdf_path = (row.get("pdf_path") or "").strip()
    pdf_url = (row.get("pdf_url") or "").strip()
    html_path = (row.get("html_path") or "").strip()

    if src == "no_url":
        return "no_url"
    if src == "no_local_raw":
        return "unknown"  # 本地模式缺文件: 非 ok 非 failed, 下载阶段补齐后重提取
    # 导航噪声页（页面是栏目/导航页, 非教师详情页）按失败处理——
    # 不进入字段覆盖统计, 防止"覆盖率虚高/垃圾数据通过评估"
    if src in ("fetch_failed", "error", "html_noise", "pdf_noise"):
        return "failed"
    if pdf_path:
        return "ok"
    if pdf_url:
        return "pending_pdf"  # html_fallback / html_shell_pdf 等未落地场景
    if src == "html" and html_path:
        return "ok"
    return "unknown"


def _field_display(v) -> str:
    """字段显示值: dict(attr) 序列化为 JSON 串, 其余字符串化去空白。

    attr 是 dict 类型，所有按字符串处理字段的循环（导航噪声清理、
    nonempty 统计、判死）必须经此归一，否则 dict.strip() 崩溃。
    """
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False) if v else ""
    return (v or "").strip()


def render_field_spec_prompt() -> str:
    """把 FIELD_SPEC 渲染成提示词中的字段定义段。"""
    lines = ["## 字段定义（共 %d 个）" % len(FIELD_SPEC), ""]
    for i, s in enumerate(FIELD_SPEC, 1):
        block = [
            f"{i}. {s['key']}: {s['meaning']}",
            f"   - 页面常见标签: {'、'.join(s['labels'])}",
        ]
        if s.get("values"):
            block.append(f"   - 取值词表: {'、'.join(s['values'])}")
        block.append(f"   - 输出格式: {s['format']}")
        block.append(f"   - 示例: {s['example']}")
        lines.append("\n".join(block))
    lines += [
        "",
        "## 提取纪律（防污染, 2026-08-31 清华数学系学院联系方式污染教训）",
        "- 联系方式/通讯地址 只提取【教师本人】的信息。页面底部/侧栏的学院或"
        "系所公共信息——公共邮箱（-hr@/office@/zsb@ 等）、学院总机/办公电话、"
        "学院地址邮编——严禁提取",
        "- 教师主体信息优先从教师姓名附近的个人区块提取; 页脚/版权/相关链接"
        "区域的文本是模板噪声, 不作为字段信息来源",
        "- 同一页出现多个邮箱/电话时, 只取与教师本人条目相邻的那个; "
        "无法区分时留空而不是填公共邮箱",
    ]
    return "\n".join(lines)


class DetailScraper:
    """教师详情页采集器。"""

    def __init__(
        self,
        school_name: str,
        dept_name: str,
        raw_data_root: str,
        csv_output_root: str | None = None,
        rate_limit: float = 1.0,
        resume: bool = False,
        reextract_llm: bool = False,
        fetch_only: bool = False,
        local_only: bool = False,
    ):
        """初始化采集器。

        Args:
            fetch_only: True 时跳过 LLM 初始化——只抓取原始 HTML/PDF 落盘
                （批量预下载模式, 提取交给后续 reextract-llm 离线做,
                不占网关并发）。raw_data_root: 原始数据根目录。可传裸根（"tearchers raw data"，
                内部追加 学校/院系）或已含校/院的完整路径（防御性识别，
                避免 院系/学校/院系 嵌套，见 improvements.md 缺点 7）。
            csv_output_root: CSV 输出根目录，同款防御；None 时落在 raw 目录。
            resume: True 时 process_teacher 跳过 CSV 中已有结果的教师
                （断点续传，spider 侧由 RESUME=1 环境变量开启）。
            reextract_llm: True 且 resume 命中时，"LLM提取失败(已降级
                启发式)"标记的行不跳过 —— 复用本地 HTML/PDF 重跑 LLM
                提取（零网络），把 LLM 降级期间的低质量行补回来。
            local_only: True 时只读本地 HTML/PDF（第二步提取专用）——
                无本地文件即跳过且绝不发网络请求；补缺下载归第一步
                download_stage 管，两阶段解耦的硬边界。
        """
        self.school_name = school_name
        self.dept_name = dept_name
        self.raw_data_root = Path(raw_data_root)
        self.rate_limit = rate_limit
        # 断点续传: True 时 process_teacher 跳过 CSV 中已有结果的教师
        self.resume = resume
        # LLM 重提取: 补回 LLM 降级期间启发式提取的低质量行
        self.reextract_llm = reextract_llm
        # 本地模式（第二步提取专用）: 只读本地 HTML/PDF, 无本地文件即跳过,
        # 绝不发网络请求——下载补缺归第一步 download_stage 管
        self.local_only = local_only
        # 下载模式（第一步）: fetch_only=True 时只下载不提取（_llm=None）。
        # 2026-09-03: process_batch 缺口优先排序依赖此标记。
        self.fetch_only = fetch_only
        self._done_rows: dict[tuple[str, str], dict] | None = None
        # LLM 熔断: 连续超时 ≥3 次后 5 分钟内快速失败走启发式
        self._llm_blackout_until = 0.0
        self._llm_consecutive_timeouts = 0
        self._llm_slow_count = 0      # 慢调用熔断: 累计慢计数（并发下不清零, 见发现18）
        self._llm_slow_until = 0.0    # 慢调用熔断截止时间
        self._llm_slow_lock = threading.Lock()  # 慢熔断计数锁（教师级并发安全）
        self._batch_partial: list[dict] = []  # SIGTERM 优雅落盘用（信号处理器可访问）
        # 渲染兜底状态: 连续无效计数 + 系级熔断（JS 空壳站专用）
        self._render_empty_streak = 0
        self._render_blackout = False
        # 请求节流: 进程级全局节流门（模块级 _THROTTLE_LOCK/_NEXT_REQUEST_AT,
        # 2026-08-25 全局化——实例级时并发 N 校 = N×rate_limit 速率叠加）。
        # 只串行化"站点请求"本身，LLM 等待等其余环节照常并行。
        # （不再持有实例级锁/时间戳, 见 _throttle）
        # 日志锁: 并发线程的日志行不互相打断
        self._log_lock = threading.Lock()
        # 2026-10-16 共享 PDF 检测: 同一院系多教师共享同一 PDF URL = 系级
        # 资源(宣传册/年报/院系介绍), 非个人简历——南科大机械系 45 人全下
        # 同一 134MB 宣传册实锤。第 3 次重复出现即拉黑, 该 URL 不再下载,
        # 教师走 HTML 提取。线程安全(process_batch 多教师并发)。
        self._pdf_url_seen: dict[str, int] = {}
        self._pdf_shared_blacklist: set[str] = set()
        self._pdf_lock = threading.Lock()

        # ── 原始数据目录: raw_data/<学校>/<院系>/ (HTML/PDF/头像) ──
        # 防御: 若调用方传的路径已经是 <学校>/<院系> 目录本身（如 LLM 生成的
        # spider.py 传 RAW_ROOT 含校/院），不再重复追加，避免
        # 院系/学校/院系 的嵌套路径（2026-08-18 材料学部 484 个文件
        # 全部落进嵌套目录的教训）。
        p = Path(raw_data_root)
        if (p.name == _sanitize_path(dept_name)
                and p.parent.name == _sanitize_path(school_name)):
            self._raw_dir = p
        else:
            self._raw_dir = p / _sanitize_path(school_name) / _sanitize_path(dept_name)
        self._raw_dir.mkdir(parents=True, exist_ok=True)

        # ── CSV 输出目录: csv_output/<学校>/<院系>/ ──
        # 防御: 若调用方传的路径已经是 <学校>/<院系> 目录本身（如 LLM 生成的
        # spider.py 传本目录），不再重复追加，避免出现 院系/学校/院系 的嵌套路径。
        if csv_output_root:
            p = Path(csv_output_root)
            if (p.name == _sanitize_path(dept_name)
                    and p.parent.name == _sanitize_path(school_name)):
                self._csv_dir = p
            else:
                self._csv_dir = p / _sanitize_path(school_name) / _sanitize_path(dept_name)
        else:
            self._csv_dir = self._raw_dir  # fallback
        self._csv_dir.mkdir(parents=True, exist_ok=True)

        # ── 日志（必须在 HTTP/LLM 之前初始化，因为初始化过程会写日志）──
        self._log_lines: list[str] = []

        # ── 统一 HTTP 客户端（util/http_client.py，代理/降级策略集中管理）──
        sys.path.insert(0, str(_UTIL_ROOT))
        from http_client import get_client
        self._http = get_client()

        # ── LLM ──
        self._llm = None if fetch_only else self._init_llm()

    # ═══════════════════════════════════════════════════════════════
    #  LLM 初始化（fast 模型优先；失败降级启发式提取）
    # ═══════════════════════════════════════════════════════════════

    def _init_llm(self):
        """初始化 LLM。fast 模型（flash/haiku 级）用于字段提取，大幅降低延迟。"""
        try:
            sys.path.insert(0, str(_LLM_UTIL))
            from llm_client import SimpleLLM

            # 默认实例（当前配置，可能为 pro 模型）
            llm = SimpleLLM()
            if llm.available:
                # 尝试用 flash 级模型创建快速实例（提取任务够用，速度更快）
                fast_llm = self._make_fast_llm()
                if fast_llm is not None:
                    self._llm = fast_llm
                    self._log(f"⚡ LLM(fast): {fast_llm.model}")
                else:
                    self._llm = llm
                return self._llm
        except Exception as e:
            self._log(f"⚠️ LLM 初始化失败: {e}")
        return None

    @staticmethod
    def _make_fast_llm():
        """用 ANTHROPIC_SMALL_FAST_MODEL（DeepSeek-V4-Flash 级）创建快速 LLM 实例。

        实测: Flash 1.5s vs Pro 7.1s vs GLM-Turbo 9.8s，Flash 质量相当且输出干净 JSON。
        """
        import os as _os
        fast_model = (
            _os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
            or _os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            or "DeepSeek-V4-Flash"
        )
        old = _os.environ.get("ANTHROPIC_MODEL")
        try:
            _os.environ["ANTHROPIC_MODEL"] = fast_model
            sys.path.insert(0, str(_LLM_UTIL))
            from llm_client import SimpleLLM
            llm = SimpleLLM()
            if llm.available:
                return llm
        except Exception:
            pass
        finally:
            if old:
                _os.environ["ANTHROPIC_MODEL"] = old
            else:
                _os.environ.pop("ANTHROPIC_MODEL", None)
        return None

    @property
    def has_llm(self) -> bool:
        return self._llm is not None

    def begin_task(self) -> None:
        """任务边界: 院系级学院亲和（借鉴 department teachers 范式, 2026-08-25）。

        固定当前线程到 hash(学院名) 槽位 token —— 院系内连续 ask 命中网关
        prompt 缓存（省钱），跨批次/重启稳定；多学院并发时按学院均匀分散
        负载（并发放大基础）。begin_task 只影响当前线程（threading.local），
        多线程并行处理不同院系时各线程在任务开头调用即可, 无需加锁。
        """
        if self._llm is not None:
            self._llm.begin_task(self.school_name)

    # ═══════════════════════════════════════════════════════════════
    #  日志
    # ═══════════════════════════════════════════════════════════════

    def _log(self, msg: str):
        # 加锁: 多线程并发时日志行不互相打断
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._log_lock:
            self._log_lines.append(line)
            print(line)

    def _throttle(self) -> None:
        """请求节流门: 保证站点请求间隔 ≥ rate_limit（进程级全局，跨实例/跨线程）。

        2026-08-25 全局化: 节流锁与 _NEXT_REQUEST_AT 移到模块级——同进程所有
        scraper 实例共享一个节流门（--all 并发 N 校时同域请求速率仍 = rate_limit,
        不叠加成 N×rate_limit）。

        并发模型: 只串行化"站点请求"本身；教师流程里的 PDF 解析、
        LLM 提取等耗时环节照常并行——线程池的收益就来自这些等待期。
        """
        global _NEXT_REQUEST_AT
        with _THROTTLE_LOCK:
            wait = _NEXT_REQUEST_AT - time.time()
            if wait > 0:
                time.sleep(wait)
            _NEXT_REQUEST_AT = time.time() + self.rate_limit

    def _retry(self, fn, what: str) -> tuple:
        """失败重试: 最多尝试 RETRY_MAX 次，指数退避（RETRY_BACKOFF × 2^i）。

        Args:
            fn: 无参可调用对象，返回"成功值"（真值）或 None/False（失败），
                也可能抛出异常
            what: 失败原因前缀（"fetch" / "pdf_download"）
        Returns:
            (成功值, 额外重试次数, 失败原因文本)  —— 总失败时成功值为 None，
            失败原因形如 "fetch: 超时(重试2次后仍失败)"；成功时失败原因为 ""
        每次尝试内的多后端降级链（curl_cffi→RsSpider→requests）由
        UnifiedClient 内部处理，这里处理的是"整链失败"后的重试。
        """
        last_err = "未知错误"
        for attempt in range(1, RETRY_MAX + 1):
            try:
                val = fn()
                if val:
                    return val, attempt - 1, ""
                last_err = "返回空/失败"
            except HttpStatusError as e:
                # 4xx/5xx 是确定性失败（URL 失效/被拒, 北大哲学系教训:
                # 上游 URL 少一层目录全 404）。重试不会改变结果, 立即
                # 失败——否则每次重试都空耗浏览器级降级链 2-3 分钟,
                # 单教师 9 分钟, 整个院系被拖死。失败原因保留状态码。
                return None, attempt - 1, f"{what}: HTTP {e.status}"
            except Exception as e:
                last_err = str(e)[:80]
            if attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
        return None, RETRY_MAX - 1, f"{what}: {last_err}(重试{RETRY_MAX - 1}次后仍失败)"

    # ═══════════════════════════════════════════════════════════════
    #  网络请求 — 统一委托 util/http_client.py
    #  （代理/降级策略/错误页过滤集中管理）
    # ═══════════════════════════════════════════════════════════════

    def fetch_page(self, url: str, timeout: int = 20) -> Optional[str]:
        """获取页面 HTML（降级链见 util/http_client.py）。

        DNS 预检（东北师大教训）: 域名解析失败（NXDOMAIN）直接返回 None，
        不进浏览器降级链——浏览器对 NXDOMAIN 会生成 ~187KB Chrome 错误页
        （绕过 is_error_page 短页面检查，被误判 html_noise），或被浏览器
        环节挂起 15 分钟超时。DNS 解析不了 = 任何降级链都拿不到真实内容，
        预检只是让它快速失败并归因到"站点不可达"。

        TCP 预检（2026-09-28 死 IP 家族）: DNS 通但 IP 黑洞的站点直接
        3s 判死（每 host 一次），不白烧 90s+ 降级链。

        降级链: curl_cffi(带代理) → RsSpider(瑞数Cookie+代理) → smart_fetch 四级 → requests
        错误页/软404/瑞数拦截页统一过滤，返回 None。
        多线程调用安全（共享客户端，请求经 _throttle 限速）。
        """
        # ── DNS 预检: 解析失败(gaierror) → 站点不可达，直接判死 ──
        try:
            host = urlparse(url).hostname
            if host:
                socket.getaddrinfo(host, None)
        except socket.gaierror:
            self._log(f"  ⛔ DNS 解析失败(域名不存在): {host}")
            return None
        # ── TCP 预检（2026-09-28 死 IP 家族）: DNS 通但 IP 黑洞的站点
        # （山大数字媒体 211.87.227.47 实测: DNS 秒回、TCP 8s 超时、全链
        # 白烧 90s+）——每 host 只探一次(进程内 memo), 3s 连接超时;
        # 失败则整 host 快速判死, 不再进降级链。单轮进程有效, 下轮重探。
        try:
            if host:
                port = 443 if urlparse(url).scheme == "https" else 80
                with _TCP_PROBE_LOCK:
                    if host not in _TCP_PROBE_MEMO:
                        try:
                            socket.create_connection(
                                (host, port), timeout=3).close()
                            _TCP_PROBE_MEMO[host] = True
                        except OSError:
                            _TCP_PROBE_MEMO[host] = False
                    tcp_ok = _TCP_PROBE_MEMO[host]
                if not tcp_ok:
                    self._log(f"  ⛔ TCP 连接失败(IP 不可达): {host}")
                    return None
        except Exception:
            pass  # 预检自身异常不影响主链
        except OSError:
            pass  # 其它网络错误交给降级链重试

        self._throttle()
        html = self._http.fetch_text(url, timeout=timeout)
        if html is None:
            # 归因留痕（北大哲学系教训: 上游 URL 少一层目录全 404,
            # 失败原因必须区分 "HTTP 404 = URL 失效/被拒"(上游名单
            # 问题, 可回补修复) vs "返回空 = 网络/超时/连接失败"(站点
            # 问题)。状态码来自 http_client.last_status）
            status = getattr(self._http, "last_status", 0)
            if status and status >= 400 and status not in _RUISHU_STATUS:
                # 反爬拦截(412/202/452)不在此列: 非 URL 失效, 归因"返回空"
                # （站点有反爬, 已尝试过降级链; 第3轮教训: 兰大/西交/南师大
                # 412 曾被误判确定性失败, 整院系 score=0）
                raise HttpStatusError(status)
        return html

    def resolve_redirect(self, url: str, referer: str = "", timeout: int = 10) -> Optional[str]:
        """解析跳转目标（搜索引擎加密跳转解真实 URL；经节流门）。"""
        self._throttle()
        return self._http.resolve_redirect(url, referer=referer, timeout=timeout)

    def fetch_page_rendered(self, url: str, timeout: int = 30) -> Optional[str]:
        """浏览器渲染抓取（代价高，调用方需做熔断控制；经节流门）。"""
        self._throttle()
        return self._http.fetch_text_rendered(url, timeout=timeout)

    def render_via_9222(self, url: str, timeout: int = 60) -> Optional[str]:
        """9222 CDP 直连渲染（2026-10-26 Vue 壳站专用）。

        fetch_text_rendered 走 RsSpider→DrissionPage(9333) 优先链, 9333
        被系统 Chrome 漂移占用时整链坏(广西大学 976 人渲染全部失败实锤:
        ws 404 → Playwright 15s 超时); 本函数绕开 9333 直连 9222 独立
        Chromium(实测 17s 拿真实内容)。调用方须持 _VUE_RENDER_SEM。
        """
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(
                    "http://127.0.0.1:9222", timeout=60000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="networkidle",
                              timeout=max(timeout, 30) * 1000)
                    html = page.content()
                finally:
                    page.close()
                return html or None
        except Exception:
            return None

    def download_binary(self, url: str, save_path: Path, timeout: int = 30) -> bool:
        """下载二进制资源（PDF/图片）。HTML 错误页不保存。多线程调用安全（同经 _throttle）。"""
        self._throttle()
        return self._http.download_binary(url, save_path, timeout=timeout)

    # 兼容旧调用: _is_error_page / _is_binary_ok 转发到统一层
    @staticmethod
    def _is_error_page(html: str) -> bool:
        sys.path.insert(0, str(_UTIL_ROOT))
        from http_client import is_error_page
        return is_error_page(html)

    @staticmethod
    def _is_binary_ok(data: bytes) -> bool:
        sys.path.insert(0, str(_UTIL_ROOT))
        from http_client import is_binary_ok
        return is_binary_ok(data)

    # ═══════════════════════════════════════════════════════════════
    #  图片 / PDF 下载
    # ═══════════════════════════════════════════════════════════════

    def download_image(self, img_url: str, save_path: Path) -> bool:
        """下载头像图片到本地。自动携带瑞数 Cookie。"""
        return self.download_binary(img_url, save_path, timeout=20)

    def download_pdf(self, pdf_url: str, save_path: Path) -> bool:
        """下载 PDF 到本地。自动携带瑞数 Cookie。

        并发设计: 与 HTML 抓取共用同一个 _throttle 节流门（同域同风控
        画像，不另开 PDF 专属通道；_upload 静态路径即使不受瑞数保护，
        保守统一限速的代价也就 ~1s/个，不值得单独放开）。多线程场景下
        下载本身仍被串行放行，但下载等待与其它教师的 LLM 提取并行。
        落盘为原子写（http_client._atomic_write），进程被杀不留半截 PDF。
        """
        return self.download_binary(pdf_url, save_path, timeout=30)

    _AVATAR_DEFAULT_RE = re.compile(
        r"(default|nopic|noimg|anonymous|avatar-default|user-default|"
        r"default_avatar|blank|loading|empty|占位|无图|缺省|灰图)", re.IGNORECASE)
    _AVATAR_NOISE_RE = re.compile(
        r"(/logo|/banner|/icon|/template|/tpl/|code2d|visitcount|"
        r"qrcode|weixin|wechat|/sys/|/images/gl/|about\d*\.|ban\d*\.|"
        r"sites/.*sysucloud)",
        re.IGNORECASE)

    @staticmethod
    def _is_default_avatar(src: str) -> bool:
        """默认头像/占位图/站标模板图排除——URL 含噪声关键词或 data URI。"""
        if not src or src.startswith("data:image/svg"):
            return True
        if src.lower().endswith((".gif", ".svg")):
            return True
        if DetailScraper._AVATAR_DEFAULT_RE.search(src):
            return True
        return bool(DetailScraper._AVATAR_NOISE_RE.search(src))

    def _extract_avatar_url(self, html: str, base_url: str,
                            name: str = "") -> Optional[str]:
        """从详情页 HTML 中提取头像图片 URL。

        2026-09-07 强化（用户指令: 尽量提取头像 URL）: 策略 1-4 原有
        （class/id 关键字 → alt/class 关键字 → 正文区尺寸 → __local 竖图）,
        新增 5（懒加载属性 data-src/data-original 等, src 常是占位图）/
        6（内联 background-image 头像容器）/ 7（教师姓名相邻定位, 无任何
        标注也认）, 并统一默认头像占位图排除。"""
        soup = BeautifulSoup(html, "lxml")

        def norm(src: str) -> str:
            return src if src.startswith("http") else urljoin(base_url, src)

        # 策略 1: class/id 包含 photo/avatar/head
        for img in soup.select(
            "img.avatar, img.headimg, img.photo, img.headpic, "
            "img.person-img, img.teacher-photo, img.user-photo, "
            "img[src*='avatar'], img[src*='head'], img[src*='photo'], img[src*='pic'], "
            "img[class*='photo'], img[class*='avatar'], img[class*='head']"
        ):
            src = img.get("src", "")
            if src and not self._is_default_avatar(src):
                return norm(src)

        # 策略 2: 遍历前几个 img，找 alt 包含人名或"照片"
        for img in soup.find_all("img", src=True)[:10]:
            src = img.get("src", "")
            alt = img.get("alt", "").strip()
            cls = " ".join(img.get("class", []))
            combined = (alt + " " + cls).lower()
            if any(k in combined for k in ["photo", "avatar", "head", "头像", "照片", "证件照"]):
                if src and not self._is_default_avatar(src):
                    return norm(src)

        # 策略 3: 正文区首图 — 典型头像比例 (宽≤300 且高>宽) 或带 width/height 标注
        # 2026-09-07 容器选择器补苏迪 VSB 形态（成理外语: 正文在
        # v_news_content, 三策略全漏）; 只有 width 无 height 的
        # 大图(≥300)且为容器首个 img → 视为头像（苏迪竖照片常见写法）。
        for container in soup.select(
                ".wp_articlecontent, .article-content, .content, article, "
                ".v_news_content, .vsb_content, .cont"):
            seen_img = False
            for img in container.find_all("img", src=True):
                src = img.get("src", "")
                if not src or "logo" in src or "visitcount" in src or "banner" in src:
                    continue
                if self._is_default_avatar(src):
                    continue
                try:
                    w = int(float(str(img.get("width", "0")).replace("px", "")))
                    h = int(float(str(img.get("height", "0")).replace("px", "")))
                except (ValueError, TypeError):
                    w = h = 0
                style = img.get("style", "")
                if (h > w >= 100) or (0 < w <= 320 and "300" in style) or (0 < h <= 400 and w and h > w):
                    return norm(src)
                # 2026-09-07: 容器首个大图(≥300)无 height 标注 → 头像
                if not seen_img and w >= 300 and h == 0:
                    return norm(src)
                seen_img = True

        # 策略 4 (2026-08-31): 苏迪/VSB 站照片存在 /__local/ 无扩展名路径
        # ——全页扫描竖图尺寸(h>w≥100)且 __local 形态, 排除 logo/banner/icon
        for img in soup.find_all("img", src=True):
            src = img.get("src", "")
            if "__local" not in src:
                continue
            if self._is_default_avatar(src):
                continue
            low = src.lower()
            if any(k in low for k in ("logo", "banner", "icon", "visitcount", "code2d")):
                continue
            try:
                w = int(float(str(img.get("width", "0")).replace("px", "")))
                h = int(float(str(img.get("height", "0")).replace("px", "")))
            except (ValueError, TypeError):
                w = h = 0
            if h > w >= 100:
                return norm(src)

        # 策略 5 (2026-09-07): 懒加载——真实 URL 在 data-src/data-original/
        # data-lazy-src/data-url/data-echo, src 常是 blank/loading 占位图。
        # 顺带覆盖策略 1-2 里 src 是占位图、真实图在懒加载属性的场景。
        for img in soup.find_all("img"):
            for attr in ("data-src", "data-original", "data-lazy-src",
                         "data-url", "data-echo", "data-actualsrc"):
                src = img.get(attr, "")
                if src and not self._is_default_avatar(src):
                    return norm(src)
            # 无懒加载属性但 src 是占位图 → 跳过不算命中
            continue

        # 策略 6 (2026-09-07): 内联背景图头像容器——div/span 用
        # style="background-image:url(...)" 显示圆形头像（无 img 标签）。
        for tag in soup.find_all(["div", "span"], style=True):
            m = re.search(r"background(?:-image)?\s*:\s*url\(['\"]?([^'\"()]+)['\"]?\)",
                          tag.get("style", ""))
            if not m:
                continue
            src = m.group(1).strip()
            if not src or self._is_default_avatar(src):
                continue
            # 容器应为头像尺寸（≤320px 或带圆角圆形类名）
            st = (tag.get("style") or "").lower()
            cls = " ".join(tag.get("class", [])).lower()
            if ("radius" in st or "round" in st or "circle" in st
                    or any(k in cls for k in ("avatar", "photo", "head", "portrait"))
                    or "300px" in st or "200px" in st or "160px" in st
                    or "120px" in st or "100px" in st):
                return norm(src)

        # 策略 7 (2026-09-07): 教师姓名相邻定位——无 alt/class/尺寸标注
        # 的头像（纯 <img> 紧跟姓名 h1/h2/h3/strong/td 场景）。
        # 用序列化全文的字符窗口限距: 姓名后 800 字/前 300 字内的 img,
        # 避免 find_next 跑到页面底部 logo。向上容器查找保留 3 层精确路径。
        if name and len(name) >= 2:
            for m in soup.find_all(string=re.compile(re.escape(name))):
                el = m.parent
                for _ in range(3):  # 向上最多 3 层找同容器
                    if el is None:
                        break
                    for img in el.find_all("img", recursive=False):
                        src = img.get("src", "")
                        if src and not self._is_default_avatar(src):
                            return norm(src)
                    el = el.parent
            hay = str(soup)
            for mm in re.finditer(re.escape(name), hay):
                win_after = hay[mm.end():mm.end() + 800]
                for im in re.finditer(r'<img[^>]+(?:src|data-src)="([^"]+)"', win_after):
                    src = im.group(1)
                    if src and not self._is_default_avatar(src):
                        return norm(src)
                win_before = hay[max(0, mm.start() - 300):mm.start()]
                for im in re.finditer(r'<img[^>]+(?:src|data-src)="([^"]+)"', win_before):
                    src = im.group(1)
                    if src and not self._is_default_avatar(src):
                        return norm(src)

        return None

    # ═══════════════════════════════════════════════════════════════
    #  HTML / PDF 保存
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _make_filename(name: str, url: str, ext: str) -> str:
        """构造文件名: <姓名>_<url前8位md5>.<ext>"""
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        safe_name = _sanitize_path(name)
        return f"{safe_name}_{url_hash}.{ext}"

    def save_raw_html(self, html: str, name: str, url: str) -> Path:
        """保存原始 HTML 到本地，返回文件路径。"""
        filename = self._make_filename(name, url, "html")
        path = self._raw_dir / filename
        path.write_text(html, encoding="utf-8")
        return path

    # ═══════════════════════════════════════════════════════════════
    #  文本提取
    # ═══════════════════════════════════════════════════════════════

    # 2026-09-07 多容器教师页合并: 命中正文容器缺少的字段节标签若在
    # 兄弟容器/整页出现 → 并入缺失段落（某教师页: v_news_content 主体
    # 简历, 讲授课程节+邮箱在旁列容器, 单容器返回丢尾部节）。
    _MERGE_SECTION_LABELS = (
        "讲授课程", "主讲课程", "承担课程", "教育经历", "工作经历",
        "学习经历", "主要科研项目", "科研项目", "主要科研成果",
        "学术论文", "学术成果", "联系方式", "通讯地址", "电子邮箱",
        "出生年月", "个人简介", "基本情况", "研究方向", "荣誉奖励",
    )

    @classmethod
    def _merge_sibling_sections(cls, el, text: str, soup) -> str:
        """兄弟容器缺失段落并入——整页存在本容器缺失的节标签时, 从全文
        按「标签+冒号」形态切出缺失段落并入（某教师页 讲授课程 在
        v_news_content 之外的兄弟容器, 兄弟扫描够不到）。全文 get_text
        一次 + 文本窗口切取 = O(N); 整页 DOM 逐块 get_text 是 O(N²),
        12MB+ 大页实测数十秒卡死（发现58 回归扫描）。"""
        # "\n" 分隔: 标签后换行让标签字段正则（.{2,201} 不跨行）在节末
        # 停下, 不吞后续正文（空间分隔会把"某教师 教授 一、基本情况"
        # 整段吞进讲授课程值）。
        full = soup.get_text("\n", strip=True)
        missing = [lb for lb in cls._MERGE_SECTION_LABELS
                   if lb in full and lb not in text]
        if not missing:
            return ""
        _lbl_re = re.compile("|".join(
            re.escape(lb) for lb in
            sorted(cls._MERGE_SECTION_LABELS, key=len, reverse=True)))
        parts = []
        for lb in missing:
            pat = re.compile(re.escape(lb) + r"\s*[：:]")
            for m in pat.finditer(full):
                # 独立标签（行首/空白/编号顿号前导）才算节; 窗口从标签
                # 本身起（保留"讲授课程："锚, 启发式/LLM 才能按标签定位）
                # 切到下一个结构标签止, 上限 1500 字
                if m.start() > 0 and full[m.start() - 1] not in "\n \t：:、）)":
                    continue
                start = m.start()
                end = min(start + 1500, len(full))
                em = _lbl_re.search(full, start + 1)
                if em and em.start() < end:
                    end = em.start()
                seg = full[start:end].strip()
                if len(seg) >= 10:
                    parts.append(seg)
                    break  # 每标签取首个独立段落
        if not parts:
            return ""
        return text + "\n\n" + "\n\n".join(parts)

    @staticmethod
    def extract_text_from_html(html: str) -> str:
        """从 HTML 中提取纯文本。优先正文容器，剥离导航/页脚。

        发现20（2026-08-28）: 部分站点把真实联系信息（地址/邮编/办公
        电话）放在 <footer>（河大马克思主义学院即此结构），整体删除会
        误丢联系信息 → footer 按「含字段标签」条件保留, 正文命中后并入。
        """
        soup = BeautifulSoup(html, "lxml")

        # 去除脚本、样式、导航。页脚按字段标签判定：含真实联系信息
        # （地址/邮编/电话等 FIELD_SPEC labels）的保留，纯版权/友链
        # 页脚删除。nav 不可能是联系信息, 仍无条件删。
        for tag in soup.find_all(["script", "style", "noscript", "nav"]):
            tag.decompose()

        # 发现21 治本（2026-08-28）: 词表打补丁赶不上新站点导航词——ccS
        # 等自建站导航是 <div class="menu/nav/crumb"> 非语义容器, <nav> 标签
        # 剔除拦不住, 导航文本混入 fallback 正文 → LLM 归入字段（陕师大
        # 文学院 155 字导航链进教育经历）。提取前按类名剔除纯导航容器；
        # 保守: 只剔导航特征类（menu/nav/crumb/topbar/toolbar）, 不碰
        # header/sidebar——可能含页头电话/地址等真实信息（发现20 同理）。
        # 2026-10-08 华师城环全系漏提根修: 裸 "nav" 子串误杀 "nav_right"
        # 布局类（华师城环模板 nav_right=右侧正文区, 刘嗣明 750 字正文被
        # 整体 decompose → 提取文本 519 字丢正文 → 13 字段全空）。裸
        # "nav" 移除, 精确导航词补齐（navigation/navlist/nav_menu 等）;
        # nav_right/nav_left 等布局类不再命中。纯 class="nav" 容器靠
        # <nav> 标签剔除 + 兜底1 密度启发式兜底, 无回归风险。
        _nav_container_class = ("menu", "crumb", "breadcrumb",
                                "topbar", "toolbar", "sitenav", "pagenav",
                                "navbar", "menubar", "navigation", "navlist",
                                "nav_menu", "nav-menu", "mainnav")
        for tag in soup.find_all(True):
            # lxml 解析畸形 HTML（<></>）会产生 attrs=None 的空标签,
            # .get 调用会崩（self.attrs.get）。跳过即可——空标签无 class。
            if tag is None or not getattr(tag, "attrs", None):
                continue
            if any(any(k in c for k in _nav_container_class)
                   for c in (tag.get("class") or [])):
                tag.decompose()

        # 收集含字段标签的 footer 文本并删除全部 footer（纯版权/友链页脚
        # 不并入; 含真实联系信息的已存入 field_footer_text, 正文命中后并入。
        # 不删 footer 会让兜底2 整页 body 混入版权词, test 已覆盖该回归）。
        # 发现50（2026-08-31）: 含"版权所有"的页脚是学院级页脚（电话/地址/
        # 邮箱都是院系的, 非教师本人）——北大药学院 191778.htm 实测。
        # 发现51（2026-08-31）: <div class="footer"> 模板页脚（非 footer 标签）
        # ——清华数学科学系全系教师页共享学院地址/电话/math-hr 邮箱 + 相关
        # 链接, 6 人联系方式被完全污染。判定: 含版权所有 或 链接密集(>2)
        # → 模板页脚整体丢弃; 仅含教师联系标签的简单页脚（发现20 河大）
        # → 保留并入。footer/foot/copyright 类容器与 <footer> 标签同规则。
        _copyright_re = re.compile(r"版权所有|Copyright", re.I)
        field_footer_text: list[str] = []

        def _handle_footer_el(el) -> None:
            ftxt = el.get_text("\n", strip=True)
            if _copyright_re.search(ftxt) or len(el.find_all("a")) >= 2:
                el.decompose()  # 模板/学院级页脚（≥2 链接的联系块必是模板）
                return
            if any(lbl in ftxt for spec in FIELD_SPEC for lbl in spec["labels"]):
                field_footer_text.append(ftxt)  # 教师联系信息页脚（发现20）
            el.decompose()

        for ft in soup.find_all("footer"):
            _handle_footer_el(ft)
        for el in soup.find_all(True):
            if not getattr(el, "attrs", None):
                continue
            cls = " ".join(el.get("class") or []).lower()
            if any(k in cls for k in ("footer", "foot", "copyright", "banquan", "icp")):
                _handle_footer_el(el)

        # 优先正文容器（常见 CMS 详情页结构）
        # 第3轮教训 (R13): 博达 CMS 标准正文容器 #vsb_content/.v_news_content
        # 缺失 → 南师大朱文成等页面正文被丢弃, raw_text 只剩导航 → LLM
        # 提取假字段污染 nonempty → 假成功记录。id 选择器精确, 放最前。
        content_selectors = [
            "#vsb_content", ".v_news_content",  # 博达 CMS（南师大/西北大学等）
            ".wp_articlecontent", ".article-content", ".article_content",
            ".gp-subRight-con", ".subCon", ".subPage-con",
            ".content", ".main-content", "#content", "article",
            ".detail-content", ".wraplist12",
            ".article-body", ".entry-content", ".news-content",  # 常见 CMS 兜底
            ".text-content", ".read-content",
        ]
        for sel in content_selectors:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                # 正文通常 > 100 字符，且明显短于整页。
                # id 选择器（#vsb_content 等）是 CMS 标准正文容器, 置信度
                # 高, 放宽下限: 真实短正文（南师大朱文成 44 字: 科研助理+
                # 电话+邮箱）不能被 100 字下限误杀（R13 实测踩中）。
                min_len = 20 if sel.startswith("#") else 100
                if min_len < len(text) < 60000:
                    # 2026-09-07 多容器教师页合并: 教师信息被拆分在兄弟
                    # 容器（某教师页: v_news_content=1808字主体简历, 讲授
                    # 课程节+邮箱在旁列容器——单容器命中即返回会丢尾部节,
                    # 讲授课程/联系方式全空）。命中容器缺少的字段节标签
                    # 若出现在兄弟容器/整页 → 并入缺失段落。
                    _merge = DetailScraper._merge_sibling_sections(el, text, soup)
                    if _merge:
                        text = _merge
                    # 发现20: footer 里的真实联系信息并入正文
                    if field_footer_text:
                        text += "\n" + "\n".join(field_footer_text)
                    return re.sub(r"\n{3,}", "\n\n", text)

        # 兜底1: 标签密度启发式 — 全页扫描，找"包含最多字段标签"的容器。
        # 解决北大历史学系类页面: 选择器清单没命中，整页 body 被导航洪流
        # 占据，LLM 截断窗口内全是导航 → 字段全空。
        best_el = None
        best_score = 0
        for el in soup.find_all(["div", "article", "section"]):
            t = el.get_text(separator="\n", strip=True)
            if not (100 < len(t) < 60000):
                continue
            score = sum(
                1 for spec in FIELD_SPEC
                for lbl in spec["labels"]
                if lbl in t and lbl != "课程"  # 课程/教学等导航词噪声大
            )
            if score > best_score:
                best_el, best_score = el, score
        if best_el is not None and best_score >= 3:
            text = best_el.get_text(separator="\n", strip=True)
            if field_footer_text:  # 发现20: footer 联系信息并入
                text += "\n" + "\n".join(field_footer_text)
            return re.sub(r"\n{3,}", "\n\n", text)

        # 兜底2: 整页 body
        body = soup.find("body") if soup.find("body") else soup
        text = body.get_text(separator="\n", strip=True)
        # 发现49 (2026-08-31): 清华自动化系等站点把教师全文塞进
        # <meta name="description">（SEO 壳, body 由 JS 填充, 静态正文 <100 字,
        # 万森页 31KB 中正文全在 meta）。body 极短时回退 meta description
        # （须含字段标签防误用）, 并在节标签前插换行恢复段落结构。
        if len(text) < 100:
            md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
            if md:
                mtxt = (md.get("content") or "").strip()
                if len(mtxt) > 150 and any(
                        lbl in mtxt for spec in FIELD_SPEC for lbl in spec["labels"]):
                    labels = sorted({lbl for spec in FIELD_SPEC for lbl in spec["labels"]
                                     if len(lbl) >= 4}, key=len, reverse=True)
                    for lbl in labels:
                        mtxt = re.sub(re.escape(lbl), "\n" + lbl, mtxt)
                    text = mtxt
        if field_footer_text:  # 发现20: footer 联系信息并入
            text += "\n" + "\n".join(field_footer_text)
        return re.sub(r"\n{3,}", "\n\n", text)

    # ═══════════════════════════════════════════════════════════════
    #  PDF 文本提取（自适应: 表格简历页压平成 "标签：值" 行，其余页线性）
    #  本地 97 个 PDF 实测: 95 个表格简历 PDF 自适应提取孤儿标签 2-4→0、
    #  结构化标签行 2-6→9，论文 PDF 自动走线性无退化。
    #  回归测试: teachers/pdf_extract_test.py
    # ═══════════════════════════════════════════════════════════════

    # 标签单元格判定词表（含 主要/学术/获奖/在研/招生 等复合标签）
    PDF_LABEL_WORDS = (
        "姓名|性别|民族|职称|技术职称|职务|导师类别|聘任方式|出生年月|出生日期|出生|毕业|学历|学位|"
        "联系方式|联系电话|电话|手机|邮箱|E-mail|Email|通讯地址|地址|邮编|单位|院系|学院|部门|"
        "主要研究方向|研究方向|研究领域|专业方向|所在团队|招生专业名称|招生专业|个人简历|"
        "教育经历|教育背景|工作经历|学习经历|学术成果|获奖情况|荣誉情况|荣誉称号|荣誉奖励|"
        "讲授课程|教学成果|教学项目|课题|基金|在研项目|承担项目|科研项目|代表论文|代表性论文|"
        "论文|专利|著作|专著|成果|学术兼职|社会兼职|任职|在研课题"
    )
    PDF_LABEL_CELL_RE = re.compile(rf"^({PDF_LABEL_WORDS})[：:]*\s*$|^({PDF_LABEL_WORDS})[：:]")
    PDF_PREFIX_LABEL_RE = re.compile(rf"^({PDF_LABEL_WORDS})[：:]")
    # 占位值（无实际内容的值格），如 "获奖情况: 备注" → 整行丢弃
    PDF_VALUE_PLACEHOLDERS = {"备注", "无", "暂无", "待补充", "无内容", "略", "无数据"}

    @staticmethod
    def _pdf_cell_text(cell) -> str:
        """单元格 → 干净文本（合并内部换行，中文不加空格）。"""
        if cell is None:
            return ""
        return re.sub(r"\s+", "", str(cell)).strip()

    @classmethod
    def _pdf_is_label_cell(cls, cell_text: str) -> bool:
        return bool(cls.PDF_LABEL_CELL_RE.match(cell_text))

    @classmethod
    def _flatten_pdf_table_page(cls, page) -> str:
        """表格模式: 表格行压平成 '标签：值' 行。"""
        lines: list[str] = []
        pending_label = ""  # 只有标签无内容的行, 挂起拼下一行
        for table in page.extract_tables():
            for row in table:
                cells = [cls._pdf_cell_text(c) for c in row]
                cells = [c for c in cells if c and c != "照片"]  # 去空/照片格
                if not cells:
                    continue
                if cls._pdf_is_label_cell(cells[0]):
                    label_part = cells[0]
                    rest = " ".join(cells[1:])
                    if rest in cls.PDF_VALUE_PLACEHOLDERS:
                        continue  # 占位值: 整行丢弃（如 "获奖情况 备注"）
                    if (cls.PDF_PREFIX_LABEL_RE.match(label_part)
                            and label_part.count("：") + label_part.count(":") >= 1):
                        # 自带 "：" 的标签单元格: 空格拼接剩余单元格
                        lines.append(f"{label_part} {rest}".rstrip())
                    elif rest:
                        # 纯标签单元格（无 "："）: 补 "：" 拼剩余
                        lines.append(f"{label_part}：{rest}")
                    else:
                        pending_label = label_part
                elif pending_label:
                    val = " ".join(cells)
                    if val not in cls.PDF_VALUE_PLACEHOLDERS:
                        lines.append(f"{pending_label}：{val}")
                    pending_label = ""
                else:
                    lines.append(" ".join(cells))
        # 无值标签不发射幻影行（如 "获奖情况: (内容见下页)"），避免 LLM 误填
        return "\n".join(lines)

    @staticmethod
    def _pdf_file_ok(path: Path) -> bool:
        """校验本地 PDF 完整性: %PDF- 魔数 + 大小下限。

        历史脏数据来源: 进程被杀留下的半截文件、错误页被误存为 .pdf。
        复用前校验，损坏则删除重下——配合 download_binary 的原子落盘，
        正常流程不会产生损坏文件，此校验是"自愈"兜底而非主路径。
        """
        try:
            if path.stat().st_size < 1000:
                return False
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        except OSError:
            return False

    @staticmethod
    def _extract_pdf_pages(pdf_path: Path, max_pages: int = 30,
                           max_chars: int = 15000) -> tuple[str, list[str]]:
        """逐页自适应提取。返回 (text, 各页模式列表['table'|'linear'|'table→linear'])。"""
        try:
            import pdfplumber
        except ImportError:
            # 不能静默返回空: 苏大教训 — pdfplumber 未声明在 pyproject 被
            # uv sync 清除后，PDF 提取静默降级为空文本，评估只看到
            # "字段全空"，追不到根因。错误文本进 heuristic 无害
            # （匹配不到标签），但让诊断有迹可循。
            return "[PDF 解析不可用: pdfplumber 未安装]", ["error"]

        parts: list[str] = []
        modes: list[str] = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    if i >= max_pages:
                        break
                    tables = page.extract_tables()
                    has_label_table = any(
                        DetailScraper._pdf_is_label_cell(
                            DetailScraper._pdf_cell_text(row[0]))
                        for t in tables for row in t if row and row[0]
                    )
                    if has_label_table:
                        flat = DetailScraper._flatten_pdf_table_page(page)
                        if len(flat) >= 50:
                            parts.append(flat)
                            modes.append("table")
                            continue
                        # 表格产出太薄, 回退线性
                        modes.append("table→linear")
                    else:
                        modes.append("linear")
                    t = page.extract_text()
                    if t:
                        parts.append(t)
        except Exception as e:
            return f"[PDF 解析异常: {e}]", ["error"]

        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(截断)"
        return text, modes

    @staticmethod
    def extract_text_from_pdf(pdf_path: Path, max_pages: int = 30,
                              max_chars: int = 15000) -> str:
        """自适应提取 PDF 文本。

        逐页判定: 页内表格含标签类单元格 → 表格模式（表格行压平成
        '标签：值' 行，丢弃该页线性文本避免重复）；否则线性模式
        （page.extract_text()）。表格模式产出 <50 字符且线性文本充足
        时回退线性。兼容论文类 PDF（自动走线性）。
        """
        text = DetailScraper._extract_pdf_pages(pdf_path, max_pages, max_chars)[0]
        return _insert_pdf_section_newlines(text)

    @staticmethod
    def extract_text_from_pdf_linear(pdf_path: Path, max_pages: int = 30) -> str:
        """旧线性实现（保留作回归对照，勿在生产路径使用）。"""
        try:
            import pdfplumber
        except ImportError:
            return ""

        pages_text = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    if i >= max_pages:
                        break
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
        except Exception as e:
            return f"[PDF 解析异常: {e}]"

        return "\n\n".join(pages_text)

    # ═══════════════════════════════════════════════════════════════
    #  字段提取（LLM）
    # ═══════════════════════════════════════════════════════════════

    def _ask_llm(self, system: str, prompt: str) -> Optional[str]:
        """LLM 单次提取调用（总闸 + 超时 + degraded 退避 + 坏缓存扰动）。

        范式对齐 department teachers 侧（collect_teacher.llm_ask_with_timeout,
        2026-08-25）:
          - _LLM_SEM 全局总闸: 并发路径共享 LLM 配额（服务端限流保护,
            进程内单一实例, 防多模块信号量叠加打爆网关）
          - bad 判定（空 / "degraded" / "error" JSON）→ 3s/8s 退避重试:
            网关 token 池轮换期 401/空响应短波（实测 1-5 分钟）, 短退避即恢复
          - 坏缓存扰动: 相同 prompt 秒回空/残缺是网关 prompt 缓存命中了
            "截杀波"时期的坏响应（fix_loop 教训）—— 加时间戳扰动强制
            真实生成
        返回 None 表示最终失败（调用方降级启发式提取）。"""
        result = None
        for attempt, backoff in ((1, 3), (2, 8)):
            with _LLM_SEM:  # 全局总闸: 并发路径共享 LLM 配额
                result = self._call_llm_with_timeout(system, prompt)
            bad = (not result or '"degraded"' in result[:200]
                   or '"error"' in result[:200])
            if not bad:
                return result
            if attempt < 2:
                self._log(f"  ⏳ LLM 响应异常（空/degraded），{backoff}s 后重试...")
                time.sleep(backoff)
        # 坏缓存扰动重试（网关缓存命中坏响应时强制真实生成）
        self._log("  ⚗️ LLM 响应仍异常，加时间戳扰动重试 1 次 ...")
        with _LLM_SEM:
            result = self._call_llm_with_timeout(
                system, prompt + f"\n[调用扰动: {int(time.time() * 1000)}]")
        if result and '"degraded"' not in result[:200] and '"error"' not in result[:200]:
            return result
        return None

    def _call_llm_with_timeout(self, system: str, prompt: str) -> Optional[str]:
        """单次 LLM 调用（超时保护 + 熔断计数 + 耗时观测; 不含退避/扰动）。

        必须 shutdown(wait=False, cancel_futures=True) —— wait=True 会等卡死的
        SSL read 永远结束, 把 worker 拖成僵尸（collect_teacher 教训,
        2026-08-25 对齐: 不能用 with ThreadPoolExecutor 块退出）。

        耗时观测（2026-08-27）: 每次调用记耗时, 慢时段（≥LLM_SLOW_THRESHOLD）
        打日志——串行时曾人均吃满 60s 超时无法定位"网关慢/代理慢/页面大"。
        """
        import concurrent.futures as _cf

        _t0 = time.time()
        _pool = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            _fut = _pool.submit(self._llm.ask, system, prompt)
            result = _fut.result(timeout=LLM_TIMEOUT)
            elapsed = time.time() - _t0
            if elapsed >= LLM_SLOW_THRESHOLD:
                self._log(f"  🐢 LLM 慢调用 {elapsed:.1f}s (≥{LLM_SLOW_THRESHOLD:.0f}s)")
                # 慢调用熔断（发现17, 2026-08-28）: 累计 LLM_SLOW_TRIGGER 次
                # 慢调用 → 短熔断 LLM_SLOW_BLACKOUT 秒走启发式。不等超时——
                # .env LLM_TIMEOUT=420 使超时熔断形同虚设（慢时段永不超时）。
                # 发现18（2026-08-28）: 教师级并发 4 下"连续慢调用"计数被穿插的
                # 快调用清零（武大 58 人 38 慢/20 快分布），熔断永不触发 → 改为
                # 累计计数只增不减（熔断后归零）, 加锁防并发递增丢失。
                _t_trigger = False
                with self._llm_slow_lock:
                    self._llm_slow_count += 1
                    if (self._llm_slow_count >= LLM_SLOW_TRIGGER
                            and time.time() >= self._llm_slow_until
                            and LLM_SLOW_BLACKOUT > 0):
                        self._llm_slow_until = time.time() + LLM_SLOW_BLACKOUT
                        self._llm_slow_count = 0
                        _t_trigger = True
                if _t_trigger:
                    self._log(f"  🐢 LLM 累计 {LLM_SLOW_TRIGGER} 次慢调用，慢熔断 "
                              f"{LLM_SLOW_BLACKOUT:.0f}s（期间快速失败走启发式）")
            # 快调用不再清零——教师级并发下穿插的快调用会让"连续"计数永不触顶
            return result
        except _cf.TimeoutError:
            self._llm_consecutive_timeouts += 1
            if (self._llm_consecutive_timeouts >= 3
                    and time.time() >= self._llm_blackout_until):
                self._llm_blackout_until = time.time() + 300
                self._log("  🔥 LLM 连续超时≥3 次，熔断 5 分钟（期间快速失败走启发式）")
            return None
        except Exception as e:
            self._log(f"  ⚠️ LLM 字段提取失败: {e}")
            return None
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)

    def _extract_fields_llm(self, raw_text: str, url: str, is_pdf: bool = False) -> dict:
        """用 LLM 从页面文本中提取 13 个字段（带 60s 超时保护）。"""
        if not self.has_llm:
            return {}

        # ── 熔断: 连续超时 ≥3 次后 5 分钟内快速失败（返回 {} 走启发式），
        #    避免 API 拥堵时每个教师烧 120s 重试；5 分钟后自动恢复尝试。
        #    慢调用熔断（发现17）: 连续慢调用 ≥3 次后短熔断同样快速失败。──
        if (time.time() < getattr(self, "_llm_blackout_until", 0.0)
                or time.time() < getattr(self, "_llm_slow_until", 0.0)):
            return {}

        # 截断文本（发现29: 先剥离空节标签——标签后紧跟另一标签的空节,
        # LLM 会倾倒后续内容进该字段, 文本层删除标签治本）
        text_truncated = _strip_empty_section_tags(raw_text[:10000])

        source_type = "PDF 文件" if is_pdf else "HTML 网页"

        prompt = f"""你是大学教师信息提取专家。从以下教师个人页面文本中提取结构化字段。

## 页面 URL
{url}

## 页面来源类型
{source_type}

## 页面文本内容（前 10000 字符）
{text_truncated}

{render_field_spec_prompt()}

## 提取规则
- 提取必须基于页面文本中的真实内容，不得臆造
- 标签对应的值为占位词（如 "备注"、"无"、"暂无"、"待补充"）时，该字段留空字符串
- PDF 表格简历中 "标签：值" 行一一对应字段；无对应标签的段落归入最相近字段
- 严禁整页倾倒（发现28）: 每个字段只放该字段对应的内容。页面同时包含
  教育经历/工作经历/研究方向/联系方式等多个 section 时, 各 section 内容
  必须分别归入对应字段——教育经历只含学历学位教育（学校/专业/学位/时间）,
  不得把工作经历、研究方向、联系邮箱、讲授课程等内容并入教育经历
- 空节检测（发现29）: 某字段的 section 标签后紧跟另一字段的 section 标签
  （中间无实质内容）时, 该字段留空——不得把后续 section 的内容并入该字段。
  教育经历/学习经历标签后若无学历学位内容（学校/专业/学位/时间）, 该字段
  必须为空字符串, 不得填入任何其他内容, 也不得输出任何 section 标签名
- 字段值不带字段名标签前缀（如值以 "教育经历:" "工作经历 " 开头时去掉标签）
- 职称必须是现任/当前职称（发现31）: 页面有 "职称:"/"技术职称:"/"现任职称:"
  标签时以标签值为准; 工作经历/学术成果/荣誉描述里的历史职称（如"历任研究员"
  "曾任副教授""2010年获评教授"等非当前职称）不得作为职称字段值
- 叙述型简历（发现35）: 页面无 "教育经历"/"工作经历" 标签, 只有
  "个人简历:"/"简历:" 叙述段时——时间线型（"2019/7-至今 XX大学教授
  2012/9-2017/6 XX大学 学历:博士 专业:物理" 等日期条目混排）按条目拆分:
  含学历学位信号（学历:/博士/硕士/学士/本科/博士后）的条目归教育经历
  （保留学校/专业/学位/时间）, 其余归工作经历（保留单位/职称/时间）;
  散文型（"2018年博士毕业于XX大学, 从事博士后研究"）从叙述中提取
  "毕业院校/学位/时间"填入教育经历, 明确的工作单位任职填入工作经历,
  无明确信息的字段留空——不得把研究方向、学术成果、招生信息等内容
  并入教育/工作经历

## 输出要求
请返回 JSON（只输出纯 JSON，不加 markdown 代码块），除 attr 外每个字段一个字符串值：
{{
  "联系方式": "...",
  "通讯地址": "...",
  "职称": "...",
  "职务": "...",
  "出生年月": "...",
  "教育经历": "...",
  "工作经历": "...",
  "研究方向": "...",
  "学术成果": "...",
  "基金": "...",
  "荣誉": "...",
  "课堂项目": "...",
  "讲授课程": "...",
  "attr": {{"兼职": "...", "办公室": "..."}}
}}

注意事项：
- 页面没有某字段信息时，该字段填空字符串 ""
- attr 是兜底字段，输出 JSON 对象：把页面上不属于上述 13 个字段、但
  对了解该教师有价值的内容按类别收集，如 兼职/客座教授、办公室房间号、
  社会任职（学会理事/委员等）、招生方向等；键用简短类别名，值为原文；
  没有这类信息时输出空对象 {{}}
- 严格按照字段定义中的「输出格式」组织内容
- 教育经历/工作经历/学术成果等长字段提取尽可能完整（保留时间、单位、编号等细节）
- 职称只填职称词本身（如 教授），不要填成"教授、博导"混合
- 职务不含职称；页面没写职务就留空，不要用职称凑数"""

        # LLM 调用（全局总闸 + 超时保护 + degraded 退避 + 坏缓存扰动,
        # 范式对齐 department teachers 侧, 2026-08-25, 见 _ask_llm）
        result = self._ask_llm(
            "你是大学教师信息提取专家。请只输出 JSON。", prompt)

        if result:
            try:
                parsed = self._parse_json(result)
                if parsed and isinstance(parsed, dict):
                    # 确保所有字段都存在（含 attr 兜底字段, dict 类型）
                    out = {}
                    for f in DETAIL_FIELDS:
                        val = parsed.get(f, "")
                        if f == "attr":
                            if isinstance(val, dict):
                                out[f] = {str(k): str(v) for k, v in val.items()}
                            elif isinstance(val, str) and val.strip():
                                # LLM 可能把对象写成字符串, 容错再解析一次
                                nested = self._parse_json(val)
                                out[f] = nested if isinstance(nested, dict) else {}
                            else:
                                out[f] = {}
                            continue
                        if isinstance(val, list):
                            val = "; ".join(str(v) for v in val)
                        out[f] = str(val) if val else ""
                    self._llm_consecutive_timeouts = 0  # 成功 → 熔断计数清零
                    return out
            except Exception:
                pass

        return {}

    def _extract_fields_heuristic(self, raw_text: str) -> dict:
        """启发式提取（LLM 不可用时的降级方案）。标签来源 = FIELD_SPEC。"""
        # 发现31: PDF 密集排版行内标签切分（"…本科工作经历（…" → 换行分隔）。
        # extract_text_from_pdf 已切分, 此处幂等; 直接调启发式的调用方也受益
        raw_text = _insert_pdf_section_newlines(raw_text or "")
        result = {f: "" for f in DETAIL_FIELDS}
        result["attr"] = {}  # 兜底字段: dict 类型

        def _label_field(spec_key: str, max_len: int = 200) -> str:
            """按 FIELD_SPEC 的 labels 找 '标签: 内容' 模式。"""
            spec = next(s for s in FIELD_SPEC if s["key"] == spec_key)
            for label in sorted(spec["labels"], key=len, reverse=True):
                for m in re.finditer(
                        # 发现58: 双语标题形态——"研究领域\n/\n専門分野\n内容"
                        # （上外日本文化经济学院实测）标签与内容之间夹一行
                        # 副语言标签, 可选组吃掉它再捕获内容; "】"后缀是
                        # 全角方括号标题【研究方向】】; "●"独占一行是
                        # 列表符号与内容分行（东北农大水利 侯仁杰实测）;
                        # 括注形态"研究方向（领域）："（内大生命科学学院
                        # 实测）——括号注释夹在标签与冒号之间; 冒号前有
                        # 空格（"研究领域 ："合工大数学实测）——\s* 前置;
                        # "及主要研究内容介绍："长复合后缀（吉大通信工程
                        # 学院实测）——及/和/与/或+短词可选组
                        rf'{re.escape(label)}\s*'
                        rf'(?:[及和与或][^：:\n\s]{{1,14}})?\s*'
                        rf'[：:】]*\s*'
                        rf'(?:[（(][^)）]{{1,8}}[)）][：:】]*)?\s*'
                        rf'(?:/\s*[^\n]{{1,20}}\n\s*)?'
                        rf'(?:[●•·]\s*\n\s*)?'
                        rf'(.{{2,{max_len + 1}}})',
                        raw_text):
                    v = m.group(1).strip()[:max_len]
                    # 发现58: 空 tab 形态——标签后紧跟另一结构标签
                    # （"研究方向\n发表论文\n科研项目" tab 头序列, 上交
                    # 系统生物医学研究院实测）捕获值=下一标签词, 不是内容;
                    # 跳过本命中继续找后面的真实内容（"科研方向为肿瘤…"
                    # 叙述形态）。"无/暂无"值同理（页面自述空）
                    if (_SECTION_END_RE.match(v)
                            or re.match(r"^(?:无|暂无|空缺|空)(?:[\s。；;）)]|$)",
                                        v)):
                        continue
                    return re.sub(r"^为\s*", "", v)  # "科研方向为肿瘤…" 剥"为"
            return ""

        def _label_section(spec_key: str, section_len: int = 800, keep: int = 500,
                           labels: tuple = ()) -> str:
            """按标签定位段落，取其后一段文本。

            发现29: labels 参数可覆盖 FIELD_SPEC 默认——教育/工作经历段落
            只用强语义标签。"学历/学位" 等弱标签在表单页会命中
            "学历： 邮编： 联系电话：" 切出整块表单（某教师实测 435 字
            混入教育经历），弱标签留给 LLM prompt 作线索, 不用作启发式切块。
            合并标签（"教育背景及工作经历"）也跳过: 强标签作子串出现在合并
            标签里（"教育背景"后跟"及" / "工作经历"前是"及"）切出的是
            教育+工作混合段（蒋忠冠/张瑶华/郑刘根/黄茹婷 500 字倾倒实测）。
            """
            spec = next(s for s in FIELD_SPEC if s["key"] == spec_key)
            ls = labels or sorted(spec["labels"], key=len, reverse=True)
            candidates = []
            for label in ls:
                for m in re.finditer(re.escape(label), raw_text):
                    before = raw_text[max(0, m.start() - 1):m.start()]
                    after = raw_text[m.end():m.end() + 1]
                    # 独立标签: 前面是行首/换行/空白/冒号/编号顿号/闭括号
                    # （发现30 同款; 发现58 补"、"与"）"——"三、主要科研
                    # 项目"/"（一）教育经历" 编号标题形态, 某教师页实测）;
                    # 后面是空白/冒号/换行（后跟"及/和/与" = 合并标签的一部分,
                    # 跳过）; 后跟"（"是标签括注（教育经历（按时间倒排序））
                    if before and before not in "\n \t：:、）)":
                        continue
                    if after and after in "及和与":
                        continue
                    # 发现58: 歧义短标签须后跟冒号/换行才是节标签——
                    # "专著，出版社"/"论文答辩"/"课题组"是句内词, 前导
                    # 顿号放行后（"、论文答辩"）由本规则拦回
                    if (label in _TAIL_AMBIGUOUS_LABELS
                            and after and after not in "：:\n"):
                        continue
                    # 每个标签取最早合法出现; 跨标签比位置, 最早者作节起点
                    # （"学术论文"先于"学术著作"时整节从论文小节起, 不丢条目）
                    candidates.append((m.start(), m.end(), label))
                    break
            if not candidates:
                return ""
            candidates.sort()
            _start_pos, label_end, label = candidates[0]
            start = label_end
            # 发现40: 标签本身不进值——"工作经历： 2004 年…" 要
            # 从冒号/空白后起切, 否则值带 "工作经历：" 前缀
            # （姜长松/都兴林等 370 例, LLM 提取无此问题, 均来自
            # 启发式切块起点=标签首字符）。发现31 括注形态
            # （"教育经历（按时间倒排序）"后无冒号）会带进括注,
            # 由 [:keep] 截断容忍, 不影响主体内容。
            while start < len(raw_text) and raw_text[start] in "：: \t":
                start += 1
            # 终点检测（发现30: 字段边界溢出）: 起点后第一个独立结构
            # 标签即截断——"教育经历"后跟"学术/技术组织任职/社会工作
            # 任职/教学及教学研究/讲授课程"等小节时, 不再把后续小节
            # 吞进教经字段。独立判定同起点（行首/空白前导 + 后不跟
            # "及和与"）。发现58 补两处: ①同字段小节不是终点（"学术
            # 著作"在"学术成果"节内、"课题"在"基金"节内——保持全部条目）;
            # ②编号顿号/闭括号前导同样是独立标题。
            end = min(start + section_len, len(raw_text))
            for em in _SECTION_END_RE.finditer(raw_text, label_end):
                if _TAIL_LABEL_TARGET.get(em.group(0)) == spec_key:
                    continue
                ebef = raw_text[max(0, em.start() - 1):em.start()]
                eaft = raw_text[em.end():em.end() + 1]
                if ebef and ebef not in "\n \t：:、）)":
                    continue
                if eaft and eaft in "及和与":
                    continue
                end = min(em.start(), end)
                break
            return raw_text[start:end].replace("\n", " ")[:keep].strip()

        # 职称: 优先"职称/技术职称/现任职称:"标签值（发现31: 词表全文扫描
        # 会被工作经历历史职称误导——"副研究员"先于"教授"命中历史职务,
        # 安大材料 PDF 实测 17 例不一致）。无标签再全文扫描词表兜底。
        spec = next(s for s in FIELD_SPEC if s["key"] == "职称")
        # 发现34: 词表按长度降序扫描——"研究员"不会抢先命中"助理研究员"
        # 的子串; 无标签时全文扫描兜底。
        for kw in sorted(spec.get("values", []), key=len, reverse=True):
            if kw in raw_text:
                result["职称"] = kw
                break
        _verify_zhicheng(result, raw_text)
        _verify_zhicheng_near_name(result, raw_text)  # 发现54: 姓名邻近职称词优先

        # 联系方式: 邮箱 + 电话
        emails = re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', raw_text)
        phones = re.findall(r'(?:电话|Tel|Phone|手机|传真|Fax)[：:]*\s*[\d\-+() （）]+', raw_text)
        contact_parts = []
        if emails:
            contact_parts.append("邮箱: " + ", ".join(emails[:3]))
        if phones:
            contact_parts.append("; ".join(phones[:3]))
        result["联系方式"] = "; ".join(contact_parts)

        # 标签字段（FIELD_SPEC labels 驱动）——出生年月单独处理（发现58:
        # 弱标签"出生"会命中传记正文"1957年出生，中共党员…"抓回正文）
        for key in ["通讯地址", "研究方向", "荣誉", "讲授课程"]:
            result[key] = _label_field(key)

        # 发现58 (2026-09-07): 长列表字段改节切块——_label_field 200 字
        # 上限把编号列表截断（某教师学术成果只剩 1 条、基金只留头部）;
        # "课题"弱标签还会误命中基本情况头衔（"重大课题首席专家"）。
        # _label_section 的独立性判定天然免疫句内词, 终点=下一个结构标签,
        # 完整保留条目; 页脚尾巴由清理链截掉。比标签值长才替换（宁可
        # 保留已有的完整内联值）。
        for key, (slen, kp) in (("学术成果", (2000, 1500)),
                                ("基金", (1500, 1000)),
                                ("课堂项目", (1000, 600))):
            sec = _label_section(key, section_len=slen, keep=kp,
                                 labels=_section_labels_for(key))
            if sec and len(sec) > len(result.get(key, "")):
                result[key] = sec

        # 发现58 (2026-09-07): 出生年月日期形态正则优先——"1957年出生"
        # 无"年月生"标记, 标签正则不认（某教师页）。三种叙述形态在页首
        # 2000 字窗口内搜索（同职称/规则 scope, 防文中他人出生日期或
        # 出版日期误命中）; 日期正则落空再退标签值, 且须短（≤30 字）且
        # 含年份——长值必是传记正文倾倒。
        _b = (re.search(r"(?:19|20)\d{2}\s*年\s*(?:\d{1,2}\s*月\s*)?出生",
                        raw_text[:2000])
              or re.search(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*生",
                           raw_text[:2000])
              or re.search(r"(?:19|20)\d{2}\s*年\s*生(?=[人\s，,。；;、）)]|$)",
                           raw_text[:2000]))
        result["出生年月"] = _b.group(0) if _b else ""
        if not result["出生年月"]:
            lv = _label_field("出生年月")
            if lv and re.search(r"(?:19|20)\d{2}", lv) and len(lv) <= 30:
                result["出生年月"] = lv

        # 段落字段（取标签后整段）——只用强语义标签（发现29）: 学历/学位等
        # 弱标签是表单字段名, 启发式切块会混入整块表单, 留给 LLM prompt 作线索
        result["教育经历"] = _label_section(
            "教育经历", labels=("教育经历", "教育背景", "学习经历"))
        result["工作经历"] = _label_section(
            "工作经历", labels=("工作经历", "工作履历", "任职经历"))

        # attr 兜底: 兼职/客座教授/办公室/社会任职等 schema 外信息
        # （FIELD_SPEC labels 驱动, 键 = 命中的标签词。要求带冒号分隔符,
        #   避免「无兼职信息」「兼职教师」等噪声误扫; 值截断到句号前）
        attr = {}
        _attr_spec = next(s for s in FIELD_SPEC if s["key"] == "attr")
        for _lbl in sorted(_attr_spec["labels"], key=len, reverse=True):
            _m = re.search(rf'{re.escape(_lbl)}[：:]\s*([^。\n]{{2,120}})', raw_text)
            if _m:
                attr[_lbl] = _m.group(1).strip()[:120]
        result["attr"] = attr

        # 发现35: 叙述型简历时间线兜底——教经/工经为空时, "个人简历"下
        # 的日期条目按学历信号归位（龙明生/侯杰等 PDF 无教育/工作经历
        # 标签, LLM 主路径也会漏）。逐空字段填充; 散文型简历（无日期
        # 时间线）返回空, 宁缺毋滥交给 LLM prompt。
        if not result["教育经历"] or not result["工作经历"]:
            n_edu, n_work = _narrative_timeline(raw_text)
            if n_edu and not result["教育经历"]:
                result["教育经历"] = n_edu
            if n_work and not result["工作经历"]:
                result["工作经历"] = n_work

        # 按学院规则补空（2026-08-30: 散文叙述型页面专属结构规则, 见
        # _DEPT_EXTRACT_RULES; 只补空不覆盖, LLM 提取优先）
        # getattr 防 _dummy_scraper（__new__ 绕过 __init__ 的空实例）崩溃
        _apply_dept_rules(result, getattr(self, "school_name", ""),
                          getattr(self, "dept_name", ""), raw_text,
                          _dept_html_abs(result, self))

        return result

    # ═══════════════════════════════════════════════════════════════
    #  单个教师处理
    # ═══════════════════════════════════════════════════════════════

    def _load_done_rows(self) -> dict[tuple[str, str], dict]:
        """断点续传: 读 teachers_detail.csv 中"值得跳过"的教师（懒加载+缓存）。

        key = (姓名, 详情页URL)。判定用 classify_record 单一口径:
          - 跳过: ok（内容已到手）、no_url（无详情页，重试无意义）
          - 不跳过: failed（请求失败）、pending_pdf（PDF 未落地）、
            unknown —— RESUME=1 重跑 = 对这些行的自动重试
        """
        import csv as _csv

        rows: dict[tuple[str, str], dict] = {}
        csv_path = self._csv_dir / "teachers_detail.csv"
        if not csv_path.exists():
            return rows
        try:
            with open(csv_path, encoding="utf-8") as f:
                for r in _csv.DictReader(f):
                    if (r.get("来源类型") or "").strip() and (r.get("采集时间") or "").strip():
                        if classify_record(r) in ("ok", "no_url"):
                            rows[(r.get("姓名", ""), r.get("详情页URL", ""))] = r
        except Exception as e:
            self._log(f"  ⚠️ 断点清单读取失败: {e}")
        return rows

    def _record_skipped_url(self, name: str, url: str, reason: str):
        """回写拦截名单（skipped_urls.json，院系目录下）。

        2026-08-27 设计: 上游名单 URL 污染（span 占位/非 http）被预校验拦截后,
        教师详情页永久缺失——名单落盘后, 上游修复 URL 可按此名单重扫补全。
        幂等: 同 (姓名, URL) 只记一次。线程安全: 模块级锁防并发写 race。
        """
        try:
            p = self._csv_dir / "skipped_urls.json"
            with _SKIPPED_URLS_LOCK:
                recs: list[dict] = []
                if p.exists():
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            recs = data
                    except Exception:
                        recs = []
                key = (name, url)
                if any((r.get("姓名"), r.get("detail_url")) == key for r in recs):
                    return
                recs.append({
                    "姓名": name,
                    "detail_url": url,
                    "原因": reason,
                    "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                p.write_text(json.dumps(recs, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        except Exception:
            pass  # 名单回写失败不影响主流程


    def _cleanup_fields(self, result: dict) -> None:
        """字段清理链（发现21/26/38/40/41/42/43 全部确定性修复器）。

        主提取与渲染兜底重提取两条路径共用——渲染路径重提取的值同样
        会带导航碎片/节标签前缀/尾漏（发现44: 渲染路径曾绕过本链,
        清华信息国家研究中心 8 行短碎片"招生动态/成果奖/负责人"实测）。
        只清值不判死; 幂等, 可重复调用。
        """
        # 字段级噪声清理: 导航词链/整页倾倒/页脚尾/导航尾/表单块/
        # tab 标签链/表格列头链/短碎片/出生年月无年份。
        for f in DETAIL_FIELDS:
            if f == "attr":
                continue  # 发现42: attr 是 dict, 字符串检测器不适用
            v = _field_display(result.get(f))
            if not v:
                continue
            v = _strip_self_section_label(v, f)  # 发现40: 剥节标签前缀
            if not v:
                result[f] = ""
                continue
            # 发现58: 剥前导标点——页面"基金：、宁夏自然科学基金…"/
            # "获奖，如第七届…"形态（标签后跟句读, 值从句中段起）。
            # 真实字段值不会以句读标点开头; 短碎片判死交给
            # _is_punct_lead_fragment, 这里剥长值的噪声
            v = v.lstrip("，。、；;：:．")
            if not v:
                result[f] = ""
                continue
            if _is_page_dump_value(v):  # 发现42: 整页倾倒 → 清空
                result[f] = ""
                continue
            v = _cut_footer_noise(v)  # 发现42: 页脚切尾
            if not v:
                result[f] = ""
                continue
            v = _cut_nav_tail(v)  # 发现42: 切面包屑/返回顶部导航尾
            if not v:
                result[f] = ""
                continue
            # 发现42: 表单块拆分（教育经历="学历：博士 电话：… Email：…"）
            if f not in ("联系方式", "通讯地址"):
                nv, form_moves = _split_form_pairs(v, f)
                if form_moves:
                    v = nv if nv else ""
                    if not v:
                        result[f] = ""
                        continue
                    for target, content in form_moves:
                        if target.startswith("attr:"):
                            lbl = target[5:]
                            attr = result.setdefault("attr", {})
                            if not attr.get(lbl):
                                attr[lbl] = content
                        else:
                            result[target] = _merge_tail_target(
                                result.get(target), content)
            if (_is_nav_noise_value(v) or _is_nav_frag_value(v, f)
                    or _is_tab_label_chain(v)
                    or _is_table_header_chain(v)
                    or _is_tiny_fragment(v, f)  # 发现43: 2-3字碎片
                    or _is_punct_lead_fragment(v)  # 2026-09-07: 标点开头截断残留
                    # 发现55: 纯标签尾碎片（北大李开龙 研究方向="与领域："
                    # —— 标签"研究方向与领域："切块只抓到标签尾巴, 值=纯标签+冒号）
                    or _is_label_only_value(v)
                    or (f == "出生年月" and not re.search(r"(19|20)\d{2}", v))):
                result[f] = ""
            elif v != _field_display(result.get(f)):
                result[f] = v

        # 发现41: 尾漏搬移——值尾部挂着兄弟节标签段时, 拆出并搬回各自
        # 字段（目标空才搬; 已有值 = 重复内容丢弃; 兼职类进 attr）。
        for f in DETAIL_FIELDS:
            v = _field_display(result.get(f))
            if not v:
                continue
            main, moves = _unmix_tail_segments(v, f)
            if not moves:
                continue
            result[f] = main
            for target, content in moves:
                if target.startswith("attr:"):
                    lbl = target[5:]
                    attr = result.setdefault("attr", {})
                    if not attr.get(lbl):
                        attr[lbl] = content
                    else:
                        attr[lbl] = _merge_tail_target(attr[lbl], content)
                else:
                    result[target] = _merge_tail_target(result.get(target), content)

    def process_teacher(self, teacher: dict) -> dict:
        """处理单个教师：获取详情页 → 提取字段 → 保存原始数据。

        Args:
            teacher: dict with keys "name", "detail_url" (from teachers.json)

        Returns:
            dict with all 13 fields + metadata (source_type, file_path, etc.)
        """
        name = teacher.get("name", "未知")
        url = teacher.get("detail_url", "")

        # ── 断点续传: 已有完整结果的教师直接返回缓存行 ──
        if self.resume:
            if self._done_rows is None:
                self._done_rows = self._load_done_rows()
            cached = self._done_rows.get((name, url))
            if cached is not None:
                degraded = "LLM提取失败" in (cached.get("失败原因") or "")
                # 2026-10-08 假成功保护: 旧记录核心字段全空(如提取文本被
                # 导航误杀截断所致)但无失败标记 — 华师城环 27 人 09-05 旧
                # 假成功实锤, reextract_llm 时同样重提取(零网络, 只重跑字段
                # 提取)。仅看核心字段: 讲授课程/课堂项目等易混入导航垃圾值
                # (刘嗣明旧记录"讲授课程=示范中心"导航词), 不能作为有效判据。
                # 2026-10-15 扩展: 职称有但方向空(单字段漏提)同样重提
                _core_fields = ("职称", "研究方向", "教育经历", "工作经历",
                                "出生年月", "联系方式", "学术成果")
                empty_ok = not any(
                    str(cached.get(k) or "").strip() for k in _core_fields)
                weak_ok = bool(str(cached.get("职称") or "").strip()) \
                    and not str(cached.get("研究方向") or "").strip()
                if not (self.reextract_llm and (degraded or empty_ok
                                                or weak_ok)):
                    self._log(f"  ⏭️ 断点跳过（已有结果）: {name}")
                    return cached
                # LLM 降级行/全空假成功行重提取: 复用本地文件走正常流程
                # （零网络），不跳过 —— 把低质量行补回 LLM 质量
                self._log(f"  ♻️ LLM 重提取（曾降级启发式或全空）: {name}")

        result = {
            "姓名": name,
            "详情页URL": url,
            "来源类型": "unknown",
            "原始文件": "",
            "头像文件": "",
            "头像URL": "",  # 2026-09-07 用户指令: 头像URL提为顶层字段(原藏 attr["头像URL"])
            "has_pdf": False,
            "pdf_url": "",
            "pdf_path": "",
            "html_path": "",
            "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 失败标记（25 列契约的 2 个扩展列）:
            #   失败原因 = "" 表示无失败; 否则为 "fetch: ..."/"pdf_download: ..."
            #   重试次数 = 成功路径上额外使用的重试次数（0=一次成功）
            "失败原因": "",
            "重试次数": 0,
        }
        for f in DETAIL_FIELDS:
            result[f] = ""

        if not url:
            result["来源类型"] = "no_url"
            return result

        # ── 名单 URL 无效(非 http): 上游采集器可能把非 URL 文本当 detail_url
        #    （中南国家卓越工程师学院 410 人 URL 全为院系名, 第3轮实测）。
        #    直接判死并明确归因——不发起网络请求(进了也是全链失败), 失败原因
        #    可读, 帮助上游定位名单质量问题; 不污染成功率统计(归 failed)。
        if not url.startswith(("http://", "https://")):
            result["来源类型"] = "fetch_failed"
            result["失败原因"] = f"名单URL无效(非http): {url[:50]}"
            self._log(f"  ⛔ 名单URL无效(非http): {url[:40]}")
            self._record_skipped_url(name, url, "名单URL无效(非http)")
            return result

        # ── 名单 URL 含非法字符(西南石油教训, 2026-08-27): 上阶段提取
        #    红色名字教师时把 span 标签拼进 URL ("…/szdw/<span style='color:red;
        #    font-…"), 以 http:// 开头但含 < > " 空格 → 请求必失败, 且会触发
        #    CDP 降级链浪费 9222 连接尝试。预拦截归因, 不发起网络请求。
        #    2026-08-27 增强: 先 HTML 实体解码再校验——污染还有 &lt;span 实体
        #    形态（jsml2 目录实测）, 不解码则 < > 藏在实体里漏网, 只靠空格兜住。
        import html as _html
        _decoded_url = _html.unescape(url)
        if re.search(r"[<>\"\s]", _decoded_url):
            result["来源类型"] = "fetch_failed"
            result["失败原因"] = f"名单URL含非法字符: {url[:50]}"
            self._log(f"  ⛔ 名单URL含非法字符: {url[:40]}")
            self._record_skipped_url(name, url, "名单URL含非法字符")
            return result

        # ── 名单垃圾词(非人名)校验 (P0-1): 上游名单混入导航词/栏目词/办公
        #    信息（西交"正版软件"URL 指向学校主页、上财"联系电话"、东北农大
        #    "二级党校"），URL 指向非教师页。与 R11 平行: 名单数据问题,
        #    不发起网络请求, 立即判死并明确归因(帮助上游定位名单质量问题)。
        if name not in ("", "未知") and _is_junk_name(name):
            result["来源类型"] = "fetch_failed"
            result["失败原因"] = f"名单垃圾词(非人名): {name}"
            self._log(f"  ⛔ 名单垃圾词(非人名): {name[:40]}")
            return result

        # ── Step 0: 请求前断点判断 — 本地 HTML 已存在即视为已请求过，跳过网络请求 ──
        #    教师名字 + 详情 URL 是确定的，文件名 = <姓名>_<url前8位md5>.html 唯一确定。
        #    HTML 存在 → 不再发网络请求（PDF 页的 HTML 壳同样保存，见 Step 2）。
        html_filename = self._make_filename(name, url, "html")
        pdf_filename = self._make_filename(name, url, "pdf")
        local_html = self._raw_dir / html_filename
        local_pdf = self._raw_dir / pdf_filename
        # 2026-10-08 下载侧 URL 修复回退: 下载阶段(upstream_refresh
        # filter_roster)应用 util.url_rewrites 的 _URL_FIX_RULES/姓名映射
        # 改写 URL 后落盘, 文件 hash 基于改写后 URL — 提取侧按名单原 URL
        # 算文件名查不到时, 应用同源改写重算再查(武理工 csai→ai、北大
        # 医学人文姓名映射等改版救援数据曾 147/114 人提取全空)。
        if not local_html.exists() and not local_pdf.exists():
            try:
                sys.path.insert(0, str(_UTIL_ROOT.parent))
                from util.url_rewrites import apply_url_rewrite
                fixed_url = apply_url_rewrite(name, url)
                if fixed_url and fixed_url != url:
                    _hf2 = self._make_filename(name, fixed_url, "html")
                    _pf2 = self._make_filename(name, fixed_url, "pdf")
                    if ((self._raw_dir / _hf2).exists()
                            or (self._raw_dir / _pf2).exists()):
                        html_filename, pdf_filename = _hf2, _pf2
                        local_html = self._raw_dir / html_filename
                        local_pdf = self._raw_dir / pdf_filename
                        self._log(f"  🔁 URL改写匹配本地: {fixed_url[:60]}")
            except Exception:
                pass
        if local_pdf.exists() and not self._pdf_file_ok(local_pdf):
            # 损坏自愈: 半截/错误页 PDF 删掉重下（下载侧已原子落盘，
            # 这里的脏文件只可能来自历史版本或外部干扰）
            self._log(f"  🗑️ 本地 PDF 损坏，删除重下: {pdf_filename}")
            local_pdf.unlink(missing_ok=True)
        if local_pdf.exists():
            raw_text = self.extract_text_from_pdf(local_pdf)
            result["来源类型"] = "pdf"
            result["原始文件"] = str(local_pdf.relative_to(self.raw_data_root))
            result["has_pdf"] = True
            result["pdf_path"] = str(local_pdf.relative_to(self.raw_data_root))
            # HTML 壳也保存过的补引用；并从本地 HTML 恢复 pdf_url
            if local_html.exists():
                html = local_html.read_text(encoding="utf-8", errors="replace")
                result["html_path"] = str(local_html.relative_to(self.raw_data_root))
                pdf_url = self._detect_pdf(html, url)
                if pdf_url:
                    result["pdf_url"] = pdf_url
                    self._log(f"  🔗 恢复 pdf_url: {pdf_url[:60]}")
            if not result["pdf_url"] and getattr(self, "local_only", False):
                self._log(f"  🚫 本地模式: 无 HTML 壳, 跳过补页恢复 pdf_url")
            elif not result["pdf_url"]:
                # 本地无 HTML 壳（旧版采集只下了 PDF）→ 重新请求详情页恢复
                # pdf_url 并补存 HTML 壳（设计上 PDF 页的 HTML 也需要保存）
                html, retries, err = self._retry(lambda: self.fetch_page(url), "fetch")
                result["重试次数"] += retries
                if err:
                    result["失败原因"] = err
                if html:
                    self.save_raw_html(html, name, url)
                    result["html_path"] = str(local_html.relative_to(self.raw_data_root))
                    pdf_url = self._detect_pdf(html, url)
                    if pdf_url:
                        result["pdf_url"] = pdf_url
                        self._log(f"  🔗 补页恢复 pdf_url: {pdf_url[:60]}")
                    else:
                        self._log(f"  ⚠️ 详情页无 PDF 引用，pdf_url 无法恢复")
                else:
                    self._log(f"  ⚠️ 详情页请求失败，pdf_url 留空待下次回补")
            result["采集时间"] = datetime.fromtimestamp(
                local_pdf.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")
            self._log(f"  ♻️ 复用本地 PDF: {pdf_filename}")
        elif local_html.exists():
            html = local_html.read_text(encoding="utf-8", errors="replace")
            # 2026-10-26 AES-GCM 壳解密(中南财经政法全校 8+ 院系实锤):
            # 下载侧落盘的是 AES 壳字节(content_container+混淆JS), 提取侧
            # 同源解密——否则 0% 提取。解密产物含姓名才覆盖落盘(防假阳性
            # 覆盖原文件), 幂等: 下轮提取读到真实页直接跳过解密。
            try:
                sys.path.insert(0, str(_UTIL_ROOT.parent))
                from util.loader_challenge import crack_aes_gcm_shell
                _cracked = crack_aes_gcm_shell(html)
                if _cracked and name and name in _cracked:
                    local_html.write_text(_cracked, encoding="utf-8")
                    html = _cracked
                    self._log("  🔓 AES壳已解密落盘(真实内容)")
                elif _cracked:
                    html = _cracked  # 解密成功但无姓名: 用不解盘(防覆盖)
            except Exception:
                pass
            # 2026-10-26 Vue JS 壳渲染(广西大学 prof.gxu 976 人实锤):
            # 下载落盘的是 2299B Vue 空壳(authorize-web), 内容靠浏览器
            # JS 执行——9222 渲染拿真实 HTML, 含姓名覆盖落盘(与 AES 同
            # 幂等纪律)。渲染重操作, 模块级信号量限 2 路防打爆 9222。
            try:
                if (len(html) < 3000 and ("authorize-web" in html
                        or "doesn't work properly without JavaScript" in html)):
                    with _VUE_RENDER_SEM:
                        _rendered = self.render_via_9222(url, timeout=60)
                    if _rendered and name and name in _rendered:
                        local_html.write_text(_rendered, encoding="utf-8")
                        html = _rendered
                        self._log("  🖥️ Vue壳已渲染落盘(真实内容)")
            except Exception:
                pass
            result["html_path"] = str(local_html.relative_to(self.raw_data_root))
            # HTML 已有但可能缺 PDF —— 重新检测页面里的 PDF 引用
            pdf_url = self._detect_pdf(html, url)
            if pdf_url and not local_pdf.exists():
                result["has_pdf"] = True
                result["pdf_url"] = pdf_url
                ok = False
                err = ""
                if not getattr(self, "local_only", False) and DOWNLOAD_PDF:
                    ok, retries, err = self._retry(
                        lambda: self.download_pdf(pdf_url, local_pdf), "pdf_download")
                    result["重试次数"] += retries
                if ok:
                    raw_text = self.extract_text_from_pdf(local_pdf)
                    result["来源类型"] = "pdf"
                    result["原始文件"] = str(local_pdf.relative_to(self.raw_data_root))
                    result["pdf_path"] = str(local_pdf.relative_to(self.raw_data_root))
                    self._log(f"  📎 PDF 已下载: {pdf_filename}")
                else:
                    raw_text = self.extract_text_from_html(html)
                    result["来源类型"] = "html_shell_pdf"
                    result["原始文件"] = str(local_html.relative_to(self.raw_data_root))
                    if DOWNLOAD_PDF and not getattr(self, "local_only", False):
                        result["失败原因"] = err
                        self._log(f"  ⚠️ PDF 下载失败: {pdf_filename}")
                    else:
                        # local_only 或 DOWNLOAD_PDF=0: 只记录引用, 落地归第一步
                        self._log(f"  📋 PDF 暂不下载(记录URL): {pdf_url[:60]}")
            else:
                raw_text = self.extract_text_from_html(html)
                result["来源类型"] = "html"
                result["原始文件"] = str(local_html.relative_to(self.raw_data_root))
            result["采集时间"] = datetime.fromtimestamp(
                local_html.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")
            self._log(f"  ♻️ 复用本地 HTML: {html_filename}")
        else:
            raw_text = None

        if raw_text is None and getattr(self, "local_only", False):
            # 本地模式（第二步提取）: 无本地 HTML/PDF → 不碰网络,
            # 判为等待下载（第一步 download_stage 补齐后重跑本阶段即可）
            result["来源类型"] = "no_local_raw"
            result["失败原因"] = "本地无原始文件(等待下载阶段)"
            self._log(f"  🚫 本地模式无文件, 跳过网络: {name}")
            return result

        if raw_text is None:
            # ── Step 1: 获取详情页（带重试: RETRY_MAX 次指数退避）──
            html, retries, err = self._retry(lambda: self.fetch_page(url), "fetch")
            result["重试次数"] += retries
            if not html:
                result["来源类型"] = "fetch_failed"
                result["失败原因"] = err
                return result

            soup = BeautifulSoup(html, "lxml")

            # ── Step 2: 检测 PDF（主路径, 2026-08-31 抽成 helper 与渲染路径共用）──
            raw_text = self._maybe_download_pdf(
                html, url, name, result, local_html, local_pdf, pdf_filename)
            if raw_text is None:
                # HTML 模式
                self.save_raw_html(html, name, url)
                result["html_path"] = str(local_html.relative_to(self.raw_data_root))
                raw_text = self.extract_text_from_html(html)
                result["来源类型"] = "html"
                result["原始文件"] = str(local_html.relative_to(self.raw_data_root))

                # ── 头像 URL 记录 ──
                avatar_url = self._extract_avatar_url(html, url, name)
                if avatar_url:
                    # 发现53: 头像 URL 是数据（真人照片凭证）——记录进 attr。
                    # 2026-09-07 用户指令: 暂时不下载头像文件, "头像文件"
                    # 字段直接存 URL（顶层+attr 双写, 后续要文件可凭 URL 回补）。
                    if not isinstance(result.get("attr"), dict):
                        result["attr"] = {}
                    result["attr"]["头像URL"] = avatar_url
                    result["头像URL"] = avatar_url
                    result["头像文件"] = avatar_url
        else:
            # 复用本地文件时也补头像 URL（2026-09-07 用户指令: 不下载文件,
            # "头像文件"字段直接存 URL; 历史已下载文件的行保持原值不覆盖）。
            if not result.get("头像文件"):
                if not result.get("头像URL") and local_html.exists():
                    try:
                        _shell = local_html.read_text(encoding="utf-8", errors="ignore")
                        _au = self._extract_avatar_url(_shell, url, name)
                        if _au:
                            if not isinstance(result.get("attr"), dict):
                                result["attr"] = {}
                            result["attr"]["头像URL"] = _au
                            result["头像URL"] = _au
                            result["头像文件"] = _au
                            self._log(f"  🖼️ 头像URL已记录(本地模式): {_au[:50]}")
                    except Exception:
                        pass
                elif result.get("头像URL"):
                    result["头像文件"] = result["头像URL"]

        # ── Step 3: 提取字段（LLM 主提取 + 启发式字段级合并）──
        # 策略: LLM 提取 13 字段 → 启发式逐字段补 LLM 的空字段。
        # 不是整体二选一 —— LLM 整体失败时启发式兜底全部字段。
        is_pdf = result.get("来源类型") == "pdf"
        llm_failed = False
        if self.has_llm:
            fields = self._extract_fields_llm(raw_text, url, is_pdf=is_pdf)
            for k, v in fields.items():
                if v:
                    result[k] = v
            llm_failed = not fields
        heuristic = self._extract_fields_heuristic(
            _strip_empty_section_tags(raw_text))  # 发现29: 空节标签同步剥离,
        #   否则 LLM 正确返回空(页面无该节)时, 启发式从原始文本按标签切出
        #   倾倒内容覆盖——李伯虎/某教师实测: LLM 教经='' 被启发式
        #   "学习经历→切 500 字" 覆盖成整页倾倒
        for k, v in heuristic.items():
            if not result.get(k):
                result[k] = v

        # 学院规则覆盖（2026-08-30）: LLM 先提取的字段会被 merge 的
        # "只补空"逻辑锁死——LLM 在散文页同样把安平秋职称误提取为
        # 副教授、出生年月留空, 启发式里的规则修正进不了结果。规则在
        # merge 之后再跑一遍, 命中即覆盖（手设计的学院规则最专）。
        # getattr 防 object.__new__ 空实例（test_extract_container_fix）
        _apply_dept_rules(result, getattr(self, "school_name", ""),
                          getattr(self, "dept_name", ""), raw_text,
                          _dept_html_abs(result, self))

        # 职称后校验（发现31）: "职称/技术职称/现任职称:"标签值优先——LLM/
        # 启发式都可能被工作经历历史职称误导（安大材料 PDF 17 例不一致）
        _verify_zhicheng(result, raw_text)
        _verify_zhicheng_near_name(result, raw_text)  # 发现54: 姓名邻近职称词优先

        # LLM 降级也是"其他失败"的一种: 结果仍在（启发式），但必须留标记，
        # 否则无法区分"页面确实没信息"与"提取能力降级"
        if llm_failed and not result["失败原因"]:
            result["失败原因"] = "LLM提取失败(超时/熔断)，已降级启发式"

        # ── 邮箱反爬还原（武大 cs 教训: "zhcai ▇ whu.edu.cn 请手工替换
        #    符号" → zhcai@whu.edu.cn）: LLM 按页面原文提取会带上防爬
        #    提示, 这里统一还原为可用邮箱 ──
        contact = _field_display(result.get("联系方式"))
        if contact:
            result["联系方式"] = _restore_scrambled_email(contact)

        # ── 字段清理链（发现21-43 全部修复器, 见 _cleanup_fields）。
        #    主路径与渲染兜底路径共用——渲染重提取的值也必须走一遍
        #    清理（发现44: 渲染路径曾绕过, 清华信息国家研究中心 8 行
        #    碎片"招生动态/成果奖/负责人"实测）。──
        self._cleanup_fields(result)

        # ── 渲染兜底: 内容可能 JS 渲染（北大/上音类教师名录占位页,
        #    武大 cs 类 Vue 详情页）──
        # 触发: (页面很短 或 页面是导航壳 或 SPA 壳) 且 (字段全空 或
        #   只有职称类占位值)。title_only 是占位页强信号（上音: 静态正文
        #   只有导航, 启发式从"荣誉教授/客座教授"误提取出职称"教授"——
        #   真实教师页几乎不可能只提取出职称 1-2 个短字段, 也几乎不可能
        #   是 100% 导航页; 但词表对"校名+栏目"式菜单覆盖不全, 页面级
        #   噪声率只有 0.27, 所以 title_only 独立成触发）。
        #   武大 cs 教训 (2026-08-27): SPA 壳页面首屏已有姓名/职称/联系
        #   方式少量真实字段 (nonempty 非空且非 title_only), 但教育经历/
        #   学术成果等主体内容在 JS 里 —— 静态抓取永远缺正文。spa_shell
        #   独立成触发: 正文 <400 字且字段标签 ≥2 → 强制渲染救回。
        # 预算: 每教师最多 1 次渲染; 系级熔断: 连续 3 次渲染无效则不再试
        nonempty = [(f, _field_display(result.get(f))) for f in DETAIL_FIELDS
                    if _field_display(result.get(f))]
        nav_noise_page = _is_nav_noise_page(raw_text or "")
        spa_shell = _is_spa_shell(raw_text or "")
        title_only = bool(nonempty) and all(
            len(v) <= 8 and v in _TITLE_VALUE_SET for _, v in nonempty)
        rendered_attempted = False
        if (result.get("来源类型") in ("html", "html_fallback")
                and not self._render_blackout
                and not getattr(self, "local_only", False)
                and raw_text
                and (len(raw_text) < 800 or nav_noise_page or spa_shell)
                and (not nonempty or title_only or spa_shell)):
            rendered_attempted = True
            rendered = self.fetch_page_rendered(url)
            if rendered:
                rendered_text = self.extract_text_from_html(rendered)

                def _label_count(text: str) -> int:
                    return sum(1 for spec in FIELD_SPEC
                               for lbl in spec["labels"] if lbl in text)

                # 渲染后字段标签密度显著提升才重提取。注意不能用文本长度
                # 比较: 真实内容可能比壳的导航文本还短（北大页壳 9 字正文
                # 反而长于渲染内容的情形相反, 上外/测试壳 120 字导航 > 30 字
                # 真实内容 —— 长度是错误度量, 标签密度才是内容信号）
                if rendered_text and _label_count(rendered_text) > _label_count(raw_text or ""):
                    self.save_raw_html(rendered, name, url)  # 壳覆盖为渲染版
                    raw_text = rendered_text
                    # 2026-08-31: 渲染版重探测 PDF——JS 注入的简历 PDF
                    # 静态 HTML 抓不到, 渲染后才有
                    _pdf2 = self._detect_pdf(rendered, url)
                    if _pdf2 and not result.get("pdf_path") and DOWNLOAD_PDF:
                        txt2 = self._download_pdf_as_text(
                            _pdf2, local_pdf, pdf_filename, result, name)
                        if txt2 is not None:
                            raw_text = txt2
                            result["pdf_url"] = _pdf2
                            result["has_pdf"] = True
                    if self.has_llm:
                        fields = self._extract_fields_llm(raw_text, url, is_pdf=False)
                        for k, v in fields.items():
                            if v:
                                result[k] = v
                    heuristic = self._extract_fields_heuristic(raw_text)
                    for k, v in heuristic.items():
                        if not result.get(k):
                            result[k] = v
                    _verify_zhicheng(result, raw_text)  # 发现31
                    _verify_zhicheng_near_name(result, raw_text)  # 发现54: 姓名邻近职称词优先
                    # 2026-08-31: 渲染路径也跑学院规则——JS 渲染页正是
                    # 规则价值最高的场景（渲染文本与静态文本同为
                    # extract_text_from_html 产出, 锚点一致）
                    _apply_dept_rules(result, getattr(self, "school_name", ""),
                                      getattr(self, "dept_name", ""), raw_text,
                                      _dept_html_abs(result, self))
                    self._cleanup_fields(result)  # 发现44: 渲染路径也必须走清理链
                    if any(result.get(f) for f in DETAIL_FIELDS):
                        self._render_empty_streak = 0
                        result["失败原因"] = ""
                        self._log("  🖥️ 渲染兜底成功: 字段已提取")
                    else:
                        self._render_empty_streak += 1
                else:
                    self._render_empty_streak += 1
            else:
                self._render_empty_streak += 1
            if self._render_empty_streak >= 3:
                self._render_blackout = True
                self._log("  🧯 渲染兜底连续 3 次无效，本系熔断渲染尝试")

        # ── 最终判死: ① 核心字段全空; ② 只有职称类占位值且渲染未救回
        #    （上音类占位页）→ 页面没有可提取内容。
        #    标记 *_noise 归 failed（classify_record 口径）, 防止
        #    "空壳通过、覆盖率虚高"──
        nonempty = [(f, _field_display(result.get(f))) for f in DETAIL_FIELDS
                    if _field_display(result.get(f))]
        # 剔除职称类占位值后的"真实"字段（R13: "教授"等合法职称词无法与
        # 导航区分——若页面只剩这类 ≤8 字符职称值, 说明正文缺失, 必须
        # 判死; 真实页的职称值伴随姓名/研究方向等字段一起出现, 不会误杀）
        meaningful = [x for x in nonempty
                      if not (len(x[1]) <= 8 and x[1] in _TITLE_VALUE_SET)]
        # 占位信号基于渲染后的当前字段重新评估: 渲染可能把"只有职称
        # 占位"救回为真实字段, 此时不得按渲染前快照判死（原逻辑在
        # "占位 + 渲染成功"边界会把救回记录误判为噪声）
        title_only_now = bool(nonempty) and all(
            len(v) <= 8 and v in _TITLE_VALUE_SET for _, v in nonempty)
        dead = (not meaningful
                or (title_only_now and rendered_attempted))

        # ── 姓名一致性校验（云大教训: 栏目导航页被当教师详情页, 凭
        #    导航串/学院总机提取出假字段 → 假成功记录。教师详情页的
        #    title/正文必然把姓名作为独立词提及; 提及不了 → 页面与
        #    该教师无关（栏目页/列表页/URL错配）。PDF 简历已落地视为
        #    有效（可能无文本层）; 全英文页面（外教）无法用中文名校验）
        name_mismatch = False
        if (not dead
                and result.get("来源类型") in ("html", "html_fallback", "html_shell_pdf")
                and name not in ("", "未知") and raw_text):
            title_text = ""
            html_file = result.get("html_path") or ""
            if html_file:
                p = self.raw_data_root / html_file
                try:
                    if p.exists():
                        _soup = BeautifulSoup(
                            p.read_text(encoding="utf-8", errors="replace"), "lxml")
                        _t = _soup.find("title")
                        title_text = _t.get_text(strip=True) if _t else ""
                except Exception:
                    pass
            # 发现32: 抽取为 _page_name_mismatch（title 去空格变体 + R13
            # 位置防护, 可单测）。语义同原内联（见函数 docstring）
            name_mismatch = _page_name_mismatch(title_text, raw_text,
                                                str(name).strip())

        if dead or name_mismatch:
            cur = result.get("来源类型", "")
            if cur == "pdf":
                result["来源类型"] = "pdf_noise"
            elif cur in ("html", "html_fallback", "html_shell_pdf"):
                result["来源类型"] = "html_noise"
            if not result["失败原因"]:
                result["失败原因"] = (f"页面不含教师姓名(疑似栏目/列表页或URL错配): {str(name).strip()}"
                                      if name_mismatch
                                      else ("页面为导航壳(仅有职称占位/无正文)"
                                            if nav_noise_page
                                            else "页面内容缺失(静态+渲染均无正文)"))

        return result

    def _requeue_failed(self, teachers: list[dict], results: list[dict]) -> list[dict]:
        """失败重跑队列 (P1-6): 汇总"可重试"fetch 失败(瞬时故障: 超时/断连/
        代理抖动), 限速重试 1 次, 结果原位替换。确定性失败(HTTP 4xx/5xx、
        名单URL无效/名单垃圾词)不重试——重试不会改变结果, 只会浪费请求。
        """
        retryable = [
            (i, t) for i, t in enumerate(teachers)
            if i < len(results) and _is_retryable_failure(results[i])
        ]
        if not retryable:
            return results
        self._log(f"  🔁 失败重跑: {len(retryable)} 个瞬时失败重试 1 次...")
        for i, t in retryable:
            name = t.get("name", "?")
            try:
                r2 = self.process_teacher(t)
                results[i] = r2
                if r2.get("来源类型") != "fetch_failed":
                    self._log(f"  ♻️ 重跑成功: {name} → {r2.get('来源类型')}")
                else:
                    self._log(f"  ⏭️ 重跑仍失败: {name} ({r2.get('失败原因', '')[:40]})")
            except Exception as e:
                self._log(f"  ⚠️ 重跑异常(保留原结果): {name} — {e}")
        return results

    def process_batch(self, teachers: list[dict], workers: int = 1,
                      skip_done: bool = False) -> list[dict]:
        """批量处理教师详情页（增量落盘：每完成 5 人刷新 CSV/JSON）。

        workers > 1 时用线程池并发。并发模型（见 _throttle）:
          - 站点网络请求经节流门按 rate_limit 串行放行（同域压力 ≈ 单线程）
          - PDF 解析 / LLM 提取等等待环节并行 → 总时长主要由 LLM 决定时
            提速接近 workers 倍
          - 结果按输入顺序返回；增量保存由协调线程执行，无并发写冲突

        skip_done=True 时按 teachers_detail.json 已有成功记录去重（URL 键）,
        跳过已完成的人——断点续传不重烧 LLM（2026-08-27 教训: 被 kill 重启
        曾重提取已落盘 80 人）。失败/未知类型的 URL 不跳, 允许重试。
        跳过的记录会合并回最终结果, 防止覆盖丢失。

        线程安全说明: 共享 UnifiedClient 的同步会话（curl_cffi）多线程
        并发可用；Playwright 兜底非线程安全，但仅在瑞数 Cookie 全失效的
        罕见路径触发，可接受。
        """
        # 发现47 修正: _watch_stop 必须定义在 skip_done 提前返回之前,
        # 否则全完成院系走到提前返回处 UnboundLocalError（提取组X 北大实测）。
        _watch_stop = threading.Event()
        _watch_stop.set()
        # ── 断点续传去重: 跳过已成功落盘的人 ──
        _DONE_OK = frozenset({"html", "html_fallback", "html_noise",
                              "pdf", "pdf_noise", "html_shell_pdf"})
        skipped_recs: list[dict] = []
        if skip_done:
            json_path = self._csv_dir / "teachers_detail.json"
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = None
            except Exception:
                data = None
            done_map: dict[str, dict] = {}
            if data:
                ts = data.get("teachers", []) if isinstance(data, dict) else data
                for t in ts:
                    # 2026-08-31 pending_pdf 修复: html_shell_pdf 且 pdf_path 空
                    # = 探测到 PDF 未落地——不是"已完成", 不能跳, 必须重试
                    # （process_teacher 复用本地 HTML 重探测下载）。
                    # 与 _load_done_rows 的 classify_record 口径对齐
                    # （后者 pending 不算 ok）。
                    pending_pdf = (t.get("来源类型") == "html_shell_pdf"
                                   and not (t.get("pdf_path") or ""))
                    if (isinstance(t, dict) and t.get("来源类型") in _DONE_OK
                            and not pending_pdf
                            and str(t.get("详情页URL", "")).strip()):
                        done_map[str(t["详情页URL"]).strip()] = t
            if done_map:
                kept = []
                for t in teachers:
                    u = (t.get("detail_url") or "").strip()
                    if u and u in done_map:
                        degraded = "LLM提取失败" in (
                            done_map[u].get("失败原因") or "")
                        # 发现45: reextract_llm 时 LLM 降级行不跳过——
                        # 交给 process_teacher 的 resume 分支重跑 LLM
                        # （此前被 skip_done 提前过滤, reextract 形同虚设）
                        # 2026-10-08 假成功保护(同 process_teacher resume
                        # 分支口径): 核心字段全空的旧假成功(提取文本曾
                        # 被截断)同样放行重提, 否则永远卡在假成功里
                        # 2026-10-15 扩展: 职称有但方向空的单字段漏提
                        # (叙述型「从事…研究」旧数据 LLM 未提方向, 于润洋
                        # 实测可提) 同样放行 — reextract_llm 模式下重提
                        _core_fields = ("职称", "研究方向", "教育经历",
                                        "工作经历", "出生年月",
                                        "联系方式", "学术成果")
                        empty_ok = not any(
                            str(done_map[u].get(k) or "").strip()
                            for k in _core_fields)
                        weak_ok = bool(
                            str(done_map[u].get("职称") or "").strip()
                        ) and not str(
                            done_map[u].get("研究方向") or "").strip()
                        if self.reextract_llm and (degraded or empty_ok
                                                   or weak_ok):
                            kept.append(t)
                        else:
                            skipped_recs.append(done_map[u])
                    else:
                        kept.append(t)
                if skipped_recs:
                    self._log(f"  ⏭️ 断点续传跳过 {len(skipped_recs)} 个已完成教师")
                teachers = kept
            if not teachers:
                self._log(f"  ✅ 全部已完成，无需处理")
                _watch_stop.set()  # 发现47
                return skipped_recs

        # 2026-09-03 缺口优先排序: fetch_only 下载阶段, 无本地 HTML 的教师
        # （真缺口, 需新下载）排前, 已有文件的人（复用+PDF 补测）排后。
        # 教训: 国科大宁波材料 352 人 167 已有, 前 95 人全复用耗光 5min
        # 预算 → success=0 被误判死系, 185 缺口永不补。缺口优先后预算全
        # 花在真下载, 保证"每分钟都有 html 下载"; 复用/PDF 补测排后兜底。
        if self.fetch_only and not self.local_only and workers > 1:
            def _has_local_html(t: dict) -> bool:
                try:
                    fn = self._make_filename(
                        t.get("name", ""), t.get("detail_url", ""), "html")
                    return (self._raw_dir / fn).exists()
                except Exception:
                    return True  # 无法判定放后面（复用路径兜底）
            teachers = sorted(teachers, key=_has_local_html)  # 稳定: 无文件在前

        self._log(f"📝 开始采集 {len(teachers)} 个教师详情页 (workers={workers})...")

        # 发现46 (2026-08-30): 无进展看门狗——站点挂掉/浏览器假死时单个教师
        # fetch 可能永久挂起（北大先进制造学院 24/45 全 fetch_failed 后卡死
        # 29 分钟, 无超时路径兜不住）。并发批次里每教师完成刷新时间戳,
        # 10 分钟无任何进展则硬退出（os._exit 逃离挂起线程）, 交给外层
        # 断点续传重跑。仅 workers>1 时启用。
        _progress_ts = [time.time()]
        _progress_lock = threading.Lock()
        # 发现47 (2026-08-30): 看门狗必须随批次结束停止——发现46 初版
        # 无停止事件, 旧批次完成后僵尸看门狗继续计时, 10 分钟后
        # os._exit 误杀正在跑的后续院系（62 次误触发实测）。停止事件
        # 在批次所有返回路径前设置（_watch_stop 已在函数开头初始化）。
        if workers > 1:
            _watch_stop.clear()
            def _watchdog():
                while not _watch_stop.wait(20):
                    with _progress_lock:
                        if time.time() - _progress_ts[0] > 600:
                            self._log("  🧯 无进展看门狗: 10 分钟无任何教师完成, "
                                      "疑似 fetch 挂起, 硬退出交给断点续传")
                            os._exit(2)
            threading.Thread(target=_watchdog, daemon=True).start()
        else:
            _watch_stop.set()

        def _error_record(name: str, url: str, exc: Exception | None = None) -> dict:
            # 静默失败禁令: 异常必须留痕（东华教训: 运行时编辑 race 导致的
            # NameError 被吞成"未捕获异常", 无从诊断）。截断到 100 字符。
            detail = ""
            if exc is not None:
                detail = f": {str(exc)[:100]}"
            rec = {
                "姓名": name,
                "详情页URL": url,
                "来源类型": "error",
                "原始文件": "",
                "头像文件": "",
                "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "失败原因": f"process_teacher 未捕获异常{detail}",
                "重试次数": 0,
            }
            for f in DETAIL_FIELDS:
                rec[f] = ""
            return rec

        # ── 串行路径（workers ≤ 1 或任务太少不值得开池）──
        if workers <= 1 or len(teachers) <= 1:
            results = []
            for i, t in enumerate(teachers):
                name = t.get("name", f"未知{i}")
                url = t.get("detail_url", "")
                self._log(f"  [{i + 1}/{len(teachers)}] {name} — {url[:60] if url else '无URL'}")
                try:
                    result = self.process_teacher(t)
                except Exception as e:
                    self._log(f"  ❌ 处理异常: {e}")
                    result = _error_record(name, url, e)
                if workers > 1:
                    with _progress_lock:
                        _progress_ts[0] = time.time()
                results.append(result)

                # 增量落盘（任务随时被杀也不丢数据）
                if (i + 1) % 5 == 0 or i == len(teachers) - 1:
                    try:
                        self._batch_partial = list(results)
                        self.save_results(results)
                    except Exception:
                        pass
            results = self._requeue_failed(teachers, results)  # P1-6: 瞬时失败重跑 1 次
            if skipped_recs:
                results = skipped_recs + results
            try:
                self.save_results(results)
            except Exception:
                pass
            self._log(f"  ✅ 完成 {len(results)} 个教师")
            return results

        # ── 并发路径: 线程池 + 保序收集 + 协调线程增量落盘 ──
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[dict | None] = [None] * len(teachers)
        done = 0

        def _job(idx_t: tuple[int, dict]) -> tuple[int, dict]:
            idx, t = idx_t
            name = t.get("name", f"未知{idx}")
            url = t.get("detail_url", "")
            try:
                return idx, self.process_teacher(t)
            except Exception as e:
                self._log(f"  ❌ 处理异常: {e}")
                return idx, _error_record(name, url, e)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_job, (i, t)): i for i, t in enumerate(teachers)}
            for fut in as_completed(futures):
                idx, res = fut.result()
                results[idx] = res
                done += 1
                if workers > 1:
                    with _progress_lock:
                        _progress_ts[0] = time.time()  # 发现46 看门狗时间戳
                self._log(f"  [{done}/{len(teachers)}] {res.get('姓名')} → {res.get('来源类型')}")
                # 协调线程增量落盘（部分结果列表不含 None，保证 CSV 契约）
                if done % 5 == 0 or done == len(teachers):
                    try:
                        self._batch_partial = skipped_recs + [r for r in results if r]
                        self.save_results(self._batch_partial)
                    except Exception:
                        pass

        final = [r for r in results if r]
        final = self._requeue_failed(teachers, final)  # P1-6: 瞬时失败重跑 1 次
        _watch_stop.set()  # 发现47: 批次结束, 停看门狗（防僵尸误杀后续批次）
        if skipped_recs:
            final = skipped_recs + final
        _clean_college_contacts(final)  # 发现52: 院级共享联系方式剥离
        try:
            self.save_results(final)
        except Exception:
            pass
        self._log(f"  ✅ 完成 {len(final)} 个教师")
        return final

    # ═══════════════════════════════════════════════════════════════
    #  PDF 检测（复用 teacher_spider 逻辑）
    # ═══════════════════════════════════════════════════════════════

    _RESUME_RE = re.compile(r"简历|resume|cv|jianli", re.I)

    def _detect_pdfs(self, html: str, base_url: str) -> list[str]:
        """(2026-08-31) 收集页面中所有 PDF 候选 URL, 按优先级排序去重。

        优先级: embed/object(application/pdf) > iframe(pdf 路径/播放器) >
        简历锚文本的 <a>.pdf > <a href=.pdf> > 苏迪 div > _upload 正则 >
        JS 注入形态。简历优先: URL/锚文本含 简历|resume|cv|jianli 提到最前。
        """
        soup = BeautifulSoup(html, "lxml")
        cands: list[tuple[int, str]] = []

        def add(url: str, pri: int, resume: bool = False) -> None:
            url = (url or "").strip()
            if not url:
                return
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith(("http://", "https://")):
                url = urljoin(base_url, url)
            if not url.startswith(("http://", "https://")):
                return
            if resume:
                pri -= 100  # 简历优先提到最前
            if url not in (u for _, u in cands):
                cands.append((pri, url))

        # 1) embed/object（application/pdf 或 .pdf 源）
        for el in soup.find_all(["embed", "object"]):
            src = el.get("src") or el.get("data") or ""
            typ = (el.get("type") or "").lower()
            if src.lower().endswith(".pdf") or typ == "application/pdf":
                add(src, 1)

        # 2) iframe: 路径以 .pdf 结尾 或 显式 pdf.js/viewer 播放器
        #    （2026-08-31 修正过宽判定: 不再 "pdf" in src——见 pdf 字样即命中）
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            typ = (iframe.get("type") or "").lower()
            path = urlparse(src).path.lower()
            if typ == "application/pdf" or path.endswith(".pdf") \
                    or "pdf.js" in path or "viewer.html" in path:
                add(src, 2)

        # 3) <a href=*.pdf>: 简历锚文本优先
        #    2026-10-16 南科大机械系 45 人全下同一 134MB 宣传册扫描件实锤:
        #    页内「宣传册下载」等系级资源链接被当教师简历。锚文本含系级
        #    资源词(宣传册/年报/手册/申请表/模板/招生简章/培养方案/规章
        #    制度/下载专区)排除; 教师简历锚文本(个人简历/简历/Resume/CV)
        #    不受影响——「简介」不排除(教师页常见「个人简介」PDF)。
        _pdf_skip_re = re.compile(
            r"宣传册|年报|年度报告|手册|申请表?|模板|招生简章|培养方案|"
            r"规章制度|下载专区|申报书|任务书|brochure|handbook", re.I)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href.lower().endswith(".pdf"):
                text = a.get_text(" ", strip=True)
                if _pdf_skip_re.search(text):
                    continue
                add(href, 3, resume=bool(self._RESUME_RE.search(text + " " + href)))

        # 4) 苏迪 CMS 播放器 div: src / pdfsrc 属性携带完整 PDF 路径
        for attr in ("pdfsrc", "src"):
            for div in soup.find_all("div", attrs={attr: True}):
                val = div.get(attr, "")
                if ".pdf" in val.lower():
                    add(val, 4)

        # 5) 全文正则兜底: /_upload/ 或 http 开头完整路径（防误抓纯文件名）
        for m in re.finditer(r'(?:https?:)?//?[^\s"\'<>]*?_upload[^\s"\'<>]*?\.pdf', html, re.I):
            add(m.group(0), 5)

        # 6) JS 注入形态: file/pdfUrl/src/href/url 赋值 + .pdf 后缀,
        #    只信任已知路径前缀（/_upload/ /files/ /pdf/ 或 http）
        for m in re.finditer(r'(?:file|pdfUrl|src|href|url)["\'\s:=]+([^"\'\s]+?\.pdf)', html, re.I):
            u = m.group(1)
            if re.search(r"/_upload/|/files/|/pdf/", u) \
                    or u.startswith(("http://", "https://")):
                add(u, 6)

        cands.sort(key=lambda x: x[0])
        return [u for _, u in cands]

    def _detect_pdf(self, html: str, base_url: str) -> Optional[str]:
        """检测页面中的 PDF——返回候选列表的第一个（优先级最高）。

        单返回值兼容: 既有 TestDetectPdf 与 backfill_pdf.py 不变;
        多 PDF 页取简历/embed 优先者。
        """
        cands = self._detect_pdfs(html, base_url)
        return cands[0] if cands else None

    def _download_pdf_as_text(self, pdf_url: str, local_pdf: Path,
                              pdf_filename: str, result: dict, name: str) -> Optional[str]:
        """(2026-08-31) 下载 PDF 并返回其文本; 失败返回 None（调用方回退 HTML）。"""
        ok, retries, err = self._retry(
            lambda: self.download_pdf(pdf_url, local_pdf), "pdf_download")
        result["重试次数"] += retries
        if ok:
            self._log(f"  📎 PDF 已下载: {pdf_filename}")
            result["来源类型"] = "pdf"
            result["原始文件"] = str(local_pdf.relative_to(self.raw_data_root))
            result["pdf_path"] = str(local_pdf.relative_to(self.raw_data_root))
            return self.extract_text_from_pdf(local_pdf)
        result["失败原因"] = err
        return None

    def _maybe_download_pdf(self, html: str, url: str, name: str, result: dict,
                            local_html: Path, local_pdf: Path,
                            pdf_filename: str) -> Optional[str]:
        """(2026-08-31) 探测并处理 PDF（主路径/渲染路径共用）。

        返回 raw_text（PDF 文本/回退 HTML 文本/壳文本）; 无 PDF 时返回
        None, 调用方走 HTML 模式。
        """
        pdf_url = self._detect_pdf(html, url)
        if not pdf_url:
            return None
        # 2026-10-16 共享 PDF 检测: 同一院系多教师共享同一 PDF URL(去 query/
        # fragment 归一) = 系级资源非个人简历。计数 >=3 即拉黑——最多浪费
        # 2 份; 拉黑后本教师与后续教师均走 HTML 提取, 不再下载。
        _pdf_key = re.sub(r"[?#].*$", "", pdf_url)
        _shared = False
        with self._pdf_lock:
            if _pdf_key in self._pdf_shared_blacklist:
                _shared = True
            else:
                self._pdf_url_seen[_pdf_key] = self._pdf_url_seen.get(_pdf_key, 0) + 1
                if self._pdf_url_seen[_pdf_key] >= 3:
                    self._pdf_shared_blacklist.add(_pdf_key)
                    _shared = True
        if _shared:
            self._log(f"  🚫 PDF 共享资源(系级), 走 HTML: {_pdf_key[:70]}")
            result["来源类型"] = "html"
            result["原始文件"] = str(local_html.relative_to(self.raw_data_root))
            return self.extract_text_from_html(html)
        result["has_pdf"] = True
        result["pdf_url"] = pdf_url
        self.save_raw_html(html, name, url)
        result["html_path"] = str(local_html.relative_to(self.raw_data_root))
        if DOWNLOAD_PDF:
            txt = self._download_pdf_as_text(pdf_url, local_pdf, pdf_filename, result, name)
            if txt is not None:
                return txt
            result["来源类型"] = "html_fallback"
            result["原始文件"] = str(local_html.relative_to(self.raw_data_root))
            return self.extract_text_from_html(html)
        result["来源类型"] = "html_shell_pdf"
        result["原始文件"] = str(local_html.relative_to(self.raw_data_root))
        self._log(f"  📋 PDF 暂不下载(记录URL): {pdf_url[:70]}")
        return self.extract_text_from_html(html)

    # ═══════════════════════════════════════════════════════════════
    #  输出
    # ═══════════════════════════════════════════════════════════════

    def save_results(self, results: list[dict]):
        """保存 teachers_detail.json 和 CSV 到 CSV 输出目录。"""
        json_path = self._csv_dir / "teachers_detail.json"
        data = {
            "school_name": self.school_name,
            "dept_name": self.dept_name,
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(results),
            "teachers": results,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._log(f"💾 已保存: {json_path} ({len(results)} 条)")

        # ── 同时写 CSV ──
        csv_path = self._csv_dir / "teachers_detail.csv"
        self._save_csv(results, csv_path)
        self._log(f"📊 CSV: {csv_path}")

    def _save_csv(self, results: list[dict], csv_path: Path):
        """保存 CSV 版本。"""
        import csv

        headers = [
            "姓名", "详情页URL", "来源类型", "原始文件", "头像文件", "头像URL",
            "has_pdf", "pdf_url", "pdf_path", "html_path", "采集时间",
            "失败原因", "重试次数",
        ] + DETAIL_FIELDS
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in results:
                row = [
                    r.get("姓名", ""),
                    r.get("详情页URL", ""),
                    r.get("来源类型", ""),
                    r.get("原始文件", ""),
                    r.get("头像文件", ""),
                    r.get("头像URL", ""),
                    "是" if r.get("has_pdf") else "否",
                    r.get("pdf_url", ""),
                    r.get("pdf_path", ""),
                    r.get("html_path", ""),
                    r.get("采集时间", ""),
                    r.get("失败原因", ""),
                    r.get("重试次数", 0),
                ]
                for field in DETAIL_FIELDS:
                    v = r.get(field, "")
                    if field == "attr" and isinstance(v, dict):
                        v = json.dumps(v, ensure_ascii=False)
                    row.append(v)
                writer.writerow(row)

    # ═══════════════════════════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_json(result: str) -> Optional[dict]:
        """从 LLM 输出中提取 JSON。"""
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            pass
        m = re.search(r'\{[\s\S]*\}', result)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return None

def _sanitize_path(name: str) -> str:
    """清理文件名中的特殊字符。"""
    for bad in "/\\:*?\"<>|":
        name = name.replace(bad, "_")
    return name.strip()
