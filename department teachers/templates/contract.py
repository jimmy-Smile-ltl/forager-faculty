#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据契约（单一事实来源）— CSV 表头 / JSON 结构 / 历史列名映射。

所有生成端（collect_teacher / fix_loop / fix_generic / util/spider 的模板）与
校验/归一端（normalize_teachers_output.py / evaluate_teachers.py）统一引用本模块，
禁止各处手写表头 —— 历史教训: 39 种 CSV 表头变体导致 Step 4 无法稳定消费。

注意: 本模块零第三方依赖，生成后的独立 spider.py 不 import 本模块
（spider 必须可复制到院系目录独立运行），因此 spider 模板内的表头字符串
仍为写死，但必须与本模块常量逐字一致（fix_loop/fix_generic 生成模板时
可用本模块校验）。
"""

# ── CSV 规范（8 列，顺序即列顺序）──
CSV_HEADERS = [
    "大学", "学院", "教师姓名", "职务职称", "所属院系",
    "个人详情URL", "师资页URL", "采集时间",
]
CSV_HEADER_STR = ",".join(CSV_HEADERS)

# ── teachers.json 顶层必需字段 ──
JSON_TOP_REQUIRED = ["school", "dept", "total", "teachers"]
# teachers[] 每项硬依赖字段（Step 4 只读 name + detail_url）
JSON_TEACHER_REQUIRED = ["name", "detail_url"]
# teachers[] 可选增强字段（有则保留，不删）
JSON_TEACHER_OPTIONAL = ["title", "department", "category", "evidence"]

# ── 历史表头变体 → 规范列的映射（normalize_teachers_output.py 使用）──
# 值为 None 表示该列在规范中无对应（归一时丢弃）
CSV_COLUMN_ALIASES = {
    # 大学
    "大学": "大学", "学校": "大学", "university": "大学",
    # 学院
    "学院": "学院", "院系": "学院", "college": "学院",
    # 教师姓名
    "教师姓名": "教师姓名", "姓名": "教师姓名", "name": "教师姓名",
    # 职务职称
    "职务职称": "职务职称", "职称": "职务职称", "title": "职务职称",
    # 所属院系
    "所属院系": "所属院系", "department": "所属院系", "系所": "所属院系",
    # 个人详情URL
    "个人详情URL": "个人详情URL", "详情URL": "个人详情URL", "url": "个人详情URL",
    "detail_url": "个人详情URL", "profile_url": "个人详情URL", "个人主页": "个人详情URL",
    "网址": "个人详情URL", "链接": "个人详情URL", "主页": "个人详情URL", "homepage": "个人详情URL",
    # 师资页URL
    "师资页URL": "师资页URL", "source_url": "师资页URL", "source": "师资页URL",
    "师资页": "师资页URL", "page_url": "师资页URL",
    # 采集时间
    "采集时间": "采集时间", "时间": "采集时间", "crawl_date": "采集时间",
    "scrape_time": "采集时间", "crawl_time": "采集时间", "抓取时间": "采集时间", "at": "采集时间",
    # ── 可丢弃列（无规范对应）──
    "教师类别": None, "类别": None, "category": None, "teacher_type": None, "categ": None,
    "来源": None, "research": None, "email": None, "phone": None, "source_text": None,
    "selectors": None, "dept_url": None, "note": None, "pages": None, "faculty_url": None,
}

# ── JSON teacher 条目键名别名（历史变体 → 规范键）──
JSON_TEACHER_ALIASES = {
    "姓名": "name", "教师姓名": "name", "name": "name",
    "职称": "title", "职务职称": "title", "title": "title",
    "个人详情URL": "detail_url", "详情URL": "detail_url", "url": "detail_url",
    "detail_url": "detail_url", "profile_url": "detail_url", "个人主页": "detail_url",
    "homepage": "detail_url",
    "所属院系": "department", "department": "department", "学院": "department", "college": "department",
    "类别": "category", "教师类别": "category", "category": "category",
}


def normalize_csv_headers(raw_headers: list[str]) -> list[str | None]:
    """把历史表头列名映射为规范列名列表（None = 丢弃该列）。

    自动去除 BOM（\\ufeff）；不在别名表里的未知列也映射为 None（丢弃）。
    """
    out = []
    for h in raw_headers:
        h = h.lstrip("\ufeff").strip()
        out.append(CSV_COLUMN_ALIASES.get(h))  # 未知列 → None 丢弃
    return out


def is_standard_csv_headers(raw_headers: list[str]) -> bool:
    """表头（去BOM后）是否与规范 8 列完全一致（含顺序）。"""
    return [h.lstrip("\ufeff").strip() for h in raw_headers] == CSV_HEADERS
