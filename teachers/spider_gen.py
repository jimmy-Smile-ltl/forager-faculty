#!/usr/bin/env python3
"""
spider.py 生成器 — LLM 分析样本页面 → LLM 生成独立采集脚本 → 运行脚本产出结果。

流程（dept_agent 调用）:
  1. stratified_sample: 按分类分层抽取 5-10 个样本 URL（本地已有文件优先，零请求）
  2. analyze_pages_llm: LLM 逐个分析样本 HTML → 每样本结论 + 投票合并
  3. generate_spider_llm: LLM 基于结论生成可独立运行的 spider.py
  4. run_spider_script: 子进程运行 spider.py → 产出 teachers_detail.csv/json + raw data

spider.py 契约:
  - 输入: 同目录 teachers_input.json（{name, detail_url, category} 清单）
  - 输出: 同目录 teachers_detail.csv(25列) + teachers_detail.json
  - 原始数据: tearchers raw data/<学校>/<院系>/<姓名>_<url前8位md5>.<html|jpg|pdf>
  - 可独立复现: cd <out_dir> && uv run python spider.py

生成的可信度设计（LLM 输出不可靠，能用确定性手段就不用 LLM 自由发挥）:
  - 确定性头部: 路径注入/预导入/行缓冲/_blank_teacher 空记录助手，
    全部预先写好，LLM 只负责 main() 循环骨架
  - 输出清洗黑名单: 剥离 LLM 可能重复的 import 与 RAW_ROOT 重定义
  - 结果必须来自 spider.py 运行（validate_spider_output 校验 25 列契约）

运行保护（run_spider_script）:
  - 总超时 SPIDER_TIMEOUT（默认 6h）+ 进程组管理（start_new_session+killpg，
    超时不留孤儿进程）
  - 停滞看门狗: 日志连续 SPIDER_STALL_MINUTES 分钟（默认 15）无输出 → 杀进程组
  - 输出重定向到 out_dir/spider_run.log，诊断看这里
"""

import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_RAW_ROOT = _COLLEGE_ROOT / "tearchers raw data"

# ── 抽样数量 ──
SAMPLE_MIN = 5
SAMPLE_MAX = 10


# ═══════════════════════════════════════════════════════════════
#  Step 1: 分层抽样
# ═══════════════════════════════════════════════════════════════

def stratified_sample(teachers: list[dict], n: int = 5) -> list[dict]:
    """按 category 分层抽样: 每类轮转取，保证结构变化全覆盖。

    注意每组内先洗牌再轮转——上游名单常把导航/栏目词排在头部
    （东北农大教训: 108 人前 12 个全是垃圾，顺序抽样 5/5 全中垃圾），
    顺序取会让分析样本系统性偏置到名单头部。
    """
    import random

    n = max(SAMPLE_MIN, min(n, SAMPLE_MAX))
    groups: dict[str, list[dict]] = {}
    for t in teachers:
        groups.setdefault(t.get("category") or "未知", []).append(t)

    # 无详情页的（纯名单）不参与页面分析
    groups = {k: [t for t in v if t.get("detail_url")] for k, v in groups.items()}
    groups = {k: v for k, v in groups.items() if v}
    # 组内洗牌: 消除名单头部偏置（垃圾导航词常排最前）
    for k in groups:
        random.shuffle(groups[k])

    sample: list[dict] = []
    idx = {k: 0 for k in groups}
    while len(sample) < n and groups:
        progress = False
        for k in sorted(groups):
            i = idx[k]
            if i < len(groups[k]):
                sample.append(groups[k][i])
                idx[k] += 1
                progress = True
        if not progress:
            break
    return sample


# ═══════════════════════════════════════════════════════════════
#  工具: HTML 精简 + 本地文件优先取样本
# ═══════════════════════════════════════════════════════════════

def condense_html(html: str, max_len: int = 6000) -> str:
    """HTML 精简: 去 script/style，保留标签骨架与关键属性，截断。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    # 保留正文相关容器与所有 img/iframe/div 的 class
    for el in soup.find_all(True):
        keep = {}
        for attr in ("class", "id", "src", "pdfsrc", "href", "sudyfile-attr", "sudyplayer"):
            if el.get(attr):
                keep[attr] = el.get(attr)
        el.attrs = keep
    s = str(soup)
    return s[:max_len]


def load_sample_html(teacher: dict, raw_dir: Path, scraper) -> tuple[str, str]:
    """优先读本地已保存的 HTML；没有则请求一次。返回 (html, source)。"""
    import hashlib
    from detail_scraper import _sanitize_path

    name = teacher.get("name", "未知")
    url = teacher.get("detail_url", "")
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    local = raw_dir / f"{_sanitize_path(name)}_{h}.html"
    if local.exists():
        return local.read_text(encoding="utf-8", errors="replace"), "local"
    html = scraper.fetch_page(url)
    return (html or ""), "fetched"


# ═══════════════════════════════════════════════════════════════
#  Step 2: LLM 分析样本页面
# ═══════════════════════════════════════════════════════════════

_ANALYSIS_PROMPT = """你是大学网站结构分析专家。分析一个教师详情页的 HTML 结构。

## 页面 URL
{url}

## 精简 HTML（去 script/style，保留标签骨架与关键属性）
{html}

## 需要回答
1. page_type: 页面类型
   - "html_content": 正文直接内嵌 HTML（教师简介等文字在页面里）
   - "pdf_shell": HTML 只有壳（导航+标题），真实内容在 PDF 播放器里
   - "empty": 几乎无内容（只有姓名/职称一行或错误页）
2. content_selector: 正文所在的 CSS 选择器（如 .wp_articlecontent / #vsb_content / .content）。
   从实际 HTML 中找出承载正文文本的容器，给最精确的选择器；找不到填 ""。
3. pdf_container: PDF 的载体（"iframe" / "div.pdfsrc" / "div.src" / "a[href$=.pdf]" / "无"）
4. visible_fields: 页面上直接可见的字段名列表（从: 联系方式/通讯地址/职称/职务/出生年月/教育经历/工作经历/研究方向/学术成果/基金/荣誉/课堂项目/讲授课程 中选）
5. reason: 一句话说明判断依据

只输出 JSON:
{{"page_type": "...", "content_selector": "...", "pdf_container": "...",
  "visible_fields": ["..."], "reason": "..."}}"""


def analyze_pages_llm(scraper, teachers: list[dict], raw_dir: Path,
                      sample_size: int = 5,
                      registry_hints: list[dict] | None = None) -> dict:
    """LLM 分析样本 → 每样本结论 + 投票合并。

    registry_hints: 同校已归档的站点原型结论（site_registry.json），
    注入提示词做 warm-start —— 同校院系常共用同一 CMS 结构。
    """
    sample = stratified_sample(teachers, sample_size)
    verdicts = []

    hint_lines = ""
    if registry_hints:
        hint_lines = "\n".join(
            f"- {h.get('dept')}: page_type={h.get('page_type')}, "
            f"pdf载体={h.get('pdf_container')}, 容器={h.get('content_selector')}, "
            f"结果={h.get('mode', '')[:50]}"
            for h in registry_hints[:5]
        )
        hint_lines = (f"\n## 同校已归档的站点原型结论（很可能与本院系一致，"
                      f"判断时优先参考）\n{hint_lines}\n")

    for t in sample:
        url = t.get("detail_url", "")
        html, source = load_sample_html(t, raw_dir, scraper)
        if not html:
            verdicts.append({
                "name": t.get("name", "?"), "url": url,
                "status": "fetch_failed",
                "page_type": "unknown", "content_selector": "",
                "pdf_container": "无", "visible_fields": [],
                "reason": "样本请求失败",
            })
            continue
        prompt = _ANALYSIS_PROMPT.format(url=url, html=condense_html(html))
        if hint_lines:
            prompt = prompt.replace("## 需要回答", hint_lines + "## 需要回答", 1)
        try:
            result = scraper._llm.ask("你是网页结构分析专家。只输出 JSON。", prompt)
            from detail_scraper import DetailScraper
            parsed = DetailScraper._parse_json(result) or {}
            verdict = {
                "name": t.get("name", "?"),
                "url": url,
                "status": "ok",
                "page_type": parsed.get("page_type", "unknown"),
                "content_selector": parsed.get("content_selector", ""),
                "pdf_container": parsed.get("pdf_container", "无"),
                "visible_fields": parsed.get("visible_fields", []),
                "reason": parsed.get("reason", ""),
                "source": source,
            }
        except Exception as e:
            verdict = {
                "name": t.get("name", "?"), "url": url,
                "status": "llm_failed", "page_type": "unknown",
                "content_selector": "", "pdf_container": "无",
                "visible_fields": [], "reason": f"LLM 失败: {str(e)[:60]}",
                "source": source,
            }
        verdicts.append(verdict)

    # ── 投票合并 ──
    ok_verdicts = [v for v in verdicts if v["status"] == "ok"]
    page_type = (Counter(v["page_type"] for v in ok_verdicts).most_common(1)[0][0]
                 if ok_verdicts else "unknown")
    selectors = Counter(v["content_selector"] for v in ok_verdicts if v["content_selector"])
    best_selector = selectors.most_common(1)[0][0] if selectors else ""
    pdf_container = (Counter(v["pdf_container"] for v in ok_verdicts).most_common(1)[0][0]
                     if ok_verdicts else "无")
    visible = Counter(
        f for v in ok_verdicts for f in v.get("visible_fields", [])
    ).most_common()

    conclusion = {
        "page_type": page_type,
        "content_selector": best_selector,
        "pdf_container": pdf_container,
        "visible_fields": [f for f, _ in visible],
        "sample_verdicts": verdicts,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return conclusion


# ═══════════════════════════════════════════════════════════════
#  Step 2.5: 预检站点原型（策略路由）
# ═══════════════════════════════════════════════════════════════

def preflight_archetype(scraper, teachers: list[dict], raw_dir: Path) -> dict:
    """预检站点原型: ≤3 个样本（本地文件优先）在花钱前分类。

    archetype:
      - "normal":              常规站点（有 URL 且内容密度正常）
      - "name_list_no_links":  名单无链接站（上外/中山: ≥50% 无 URL）
      - "empty_shell":         详情页空壳站（北大: 内容样本标签密度≈0）

    路由依据: name_list_no_links → 搜索式发现补 URL；
    empty_shell → 渲染兜底策略 + 空壳主导重试跳过。
    """
    from detail_scraper import DetailScraper, FIELD_SPEC

    total = len(teachers)
    no_url_n = sum(1 for t in teachers if not t.get("detail_url"))
    no_url_ratio = no_url_n / total if total else 0.0

    sample = [t for t in teachers if t.get("detail_url")][:5]
    probes = []
    for t in sample:
        html, source = load_sample_html(t, raw_dir, scraper)
        if not html:
            probes.append({"name": t.get("name"), "ok": False})
            continue
        text = DetailScraper.extract_text_from_html(html)
        labels = sum(1 for spec in FIELD_SPEC
                     for lbl in spec["labels"] if lbl in text)
        # PDF 壳站（苏迪/iframe 播放器）的 HTML 壳本来就空 ——
        # 有 PDF 引用的页面不参与"空壳"统计，否则苏大类 PDF 站
        # 被误判 empty_shell
        has_pdf = bool(scraper._detect_pdf(html, t.get("detail_url", "")))
        probes.append({"name": t.get("name"), "ok": True,
                       "text_len": len(text), "labels": labels,
                       "has_pdf": has_pdf, "source": source})

    ok_probes = [p for p in probes if p.get("ok")]
    non_pdf_probes = [p for p in ok_probes if not p.get("has_pdf")]
    pdf_n = sum(1 for p in ok_probes if p.get("has_pdf"))
    # 页面够长（>300 字）但字段标签 ≤1 → 内容是导航/壳，真内容 JS 渲染
    empty_n = sum(1 for p in non_pdf_probes
                  if p["text_len"] > 300 and p["labels"] <= 1)
    empty_shell_ratio = (empty_n / len(non_pdf_probes)
                         if non_pdf_probes else 0.0)

    if no_url_ratio >= 0.5:
        archetype = "name_list_no_links"
    elif ok_probes and pdf_n / len(ok_probes) >= 0.6:
        archetype = "pdf_shell"
    elif empty_shell_ratio >= 0.33:
        # 阈值 0.33: 抽样 3-5 个里出现 1 个空壳就路由渲染策略。误路由
        # 成本低（渲染兜底自带每教师1次+系级熔断），漏路由成本高
        # （苏大教训: 预检 1/3 空判 normal, 实际 5/5 全空）
        archetype = "empty_shell"
    else:
        archetype = "normal"

    return {
        "archetype": archetype,
        "no_url_ratio": round(no_url_ratio, 2),
        "empty_shell_ratio": round(empty_shell_ratio, 2),
        "probes": probes,
    }


# ═══════════════════════════════════════════════════════════════
#  Step 3: LLM 生成 spider.py
# ═══════════════════════════════════════════════════════════════

_SPIDER_GEN_PROMPT = """你是爬虫工程师。根据网页分析结论，生成教师详情采集脚本 spider.py 的 main() 与辅助函数。

## 目标
学校: {school}  院系: {dept}

## 分析结论（LLM 对样本页面的判断）
- 页面类型: {page_type}
- 正文容器选择器: {content_selector}
- PDF 载体: {pdf_container}
- 页面可见字段: {visible_fields}

## 样本分析明细
{verdicts}

## 已预定义（脚本头部已注入，直接使用，禁止重复 import / 重复定义）
- SCHOOL = "{school}"    DEPT = "{dept}"
- RAW_ROOT = Path(__file__).resolve().parents[3] / "tearchers raw data"（裸根目录，DetailScraper 内部追加 SCHOOL/DEPT，避免嵌套路径）
- get_client() → 统一HTTP客户端: fetch_text(url)/download_binary(url, path)
- DetailScraper(school_name, dept_name, raw_data_root) → 单教师完整流程
  - scraper.process_teacher({{"name":..., "detail_url":...}}) → 25字段dict(含断点复用/PDF检测/字段提取/失败标记)
  - scraper.save_results(results) → 写 teachers_detail.csv(25列)+teachers_detail.json 到 scraper._csv_dir
- _blank_teacher(name, url, category) → 无详情页教师的空记录（25字段契约, 来源类型=no_url）
- FIELD_SPEC, DETAIL_FIELDS 已可用
- 已 import: sys, json, csv, re, hashlib, time, datetime, Path
- 环境变量 DOWNLOAD_PDF: 1=下载PDF, 0=只记录pdf_url
- 环境变量 TEACHER_WORKERS: 教师级并发线程数（默认 4；1=串行）

## 输入
同目录 teachers_input.json: [{{"name": "...", "detail_url": "...", "category": "..."}}]
main() 读取它（json.load(open("teachers_input.json", encoding="utf-8"))["teachers"]）。

## 输出契约（必须严格满足）
1. DetailScraper 构造: DetailScraper(SCHOOL, DEPT, str(RAW_ROOT), csv_output_root=Path(__file__).resolve().parent, resume=(os.environ.get("RESUME","") == "1"), reextract_llm=(os.environ.get("REEXTRACT_LLM","") == "1"))
   （RESUME=1 时 process_teacher 内部自动跳过已有结果；REEXTRACT_LLM=1 时对"LLM降级"行复用本地文件重提取——都无需自己实现）
2. 教师处理: **直接调用预定义方法，禁止自己写教师循环**:
   ```
   results = scraper.process_batch(teachers, workers=int(os.environ.get("TEACHER_WORKERS", "4")))
   ```
   process_batch 内部已实现: 断点复用 / PDF检测下载 / 字段提取 / 无URL教师(no_url) /
   线程并发（站点请求节流限速、LLM 等待并行）/ 进度打印 / 每5人增量保存 / 失败兜底。
   自己写循环会破坏这些保证（并发安全/增量落盘/契约一致性）。
3. 全部完成后 scraper.save_results(results)（幂等，可重复调用）
4. 结尾 print 统计摘要（总数/成功/失败）。失败判定必须用
   `(r.get("来源类型") or "") in ("fetch_failed", "error")`，
   禁止臆造"失败标记/状态"等不存在的键（25列契约里没有这些字段）
5. 用 main() 入口

## 要求
- 代码尽量短: 读输入 → 调 process_batch → save_results → 打印统计
- 字段名必须用中文（姓名/来源类型等25列契约），不要用 name/source_type 之类英文键
- 只输出 Python 代码（main 与辅助函数），不要 markdown 代码块标记，不要 shebang，不要 import"""


def generate_spider_llm(scraper, school: str, dept: str, conclusion: dict,
                        out_dir: Path) -> Path:
    """LLM 生成 spider.py（确定性头部 + LLM 生成的 main 逻辑）。"""
    verdict_lines = "\n".join(
        f"- {v.get('name')} ({v.get('status')}): page_type={v.get('page_type')}, "
        f"selector={v.get('content_selector')}, pdf={v.get('pdf_container')}, "
        f"可见字段={v.get('visible_fields')} — {v.get('reason')}"
        for v in conclusion.get("sample_verdicts", [])
    )
    prompt = _SPIDER_GEN_PROMPT.format(
        school=school,
        dept=dept,
        page_type=conclusion.get("page_type"),
        content_selector=conclusion.get("content_selector") or "(未确定, 用 DetailScraper 默认)",
        pdf_container=conclusion.get("pdf_container"),
        visible_fields=", ".join(conclusion.get("visible_fields", [])) or "(未知)",
        verdicts=verdict_lines[:2000],
    )
    code = scraper._llm.ask(
        "你是爬虫工程师。只输出可运行的 Python 代码（main 与辅助函数）。", prompt
    )
    # ── LLM 输出清洗 ──
    # 历史踩坑: LLM 会包 ```python、写 shebang、以及"自作主张"地重复
    # import 或重定义头部已注入的常量（与确定性头部冲突 → ImportError/
    # 路径错乱）。这里按黑名单整行剥离:
    #   - from http_client/detail_scraper import → 头部已统一注入
    #   - import sys / from pathlib import Path → 头部已有
    #   - sys.path.insert → LLM 自己拼的路径几乎必错
    #   - RAW_ROOT = → 头部已按裸根目录定义（详见 2026-08-18 嵌套路径教训）
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
    code = re.sub(r"\n?```$", "", code)
    code = re.sub(r"^#!.*\n", "", code)
    for bad in ("from http_client import", "from detail_scraper import",
                "import sys\n", "from pathlib import Path\n",
                "sys.path.insert", "RAW_ROOT ="):
        code = "\n".join(
            ln for ln in code.split("\n") if bad not in ln
        )

    # ── 确定性头部 + LLM 生成的 main 逻辑 ──
    header = build_spider_header(school, dept)
    path = out_dir / "spider.py"
    path.write_text(header + code, encoding="utf-8")
    print(f"🐍 LLM 已生成 spider.py: {path} ({len(header) + len(code)} 字符)")
    return path


def build_spider_header(school: str, dept: str) -> str:
    """确定性头部: 路径注入 + 预导入 + 行缓冲 + 空记录助手。

    LLM 路径与无 LLM 兜底路径共用（避免两份头部分叉）。
    """
    return f'''#!/usr/bin/env python3
"""自动生成 — {school} {dept} 教师详情采集脚本（生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}）
复现: cd 本目录 && uv run python spider.py
"""
import sys
import os
import json
import csv
import re
import hashlib
import time
from datetime import datetime
from pathlib import Path

# 行缓冲: 输出到日志文件时不积压在 8KB 缓冲区（否则进度不可见）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

_BASE = Path(__file__).resolve().parent.parent.parent   # teachers/
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE.parent / "util"))

from http_client import get_client                      # noqa: E402
from detail_scraper import (                           # noqa: E402
    DetailScraper, FIELD_SPEC, DETAIL_FIELDS,
)

SCHOOL = "{school}"
DEPT = "{dept}"
RAW_ROOT = Path(__file__).resolve().parents[3] / "tearchers raw data"


def _blank_teacher(name: str, url: str = "", category: str = "") -> dict:
    """无详情页教师的空记录（严格 25 字段契约: 23 基础 + 失败原因/重试次数）。"""
    rec = {{f: "" for f in DETAIL_FIELDS}}
    rec.update({{
        "姓名": name, "详情页URL": url, "来源类型": "no_url",
        "原始文件": "", "头像文件": "", "has_pdf": False,
        "pdf_url": "", "pdf_path": "", "html_path": "",
        "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "失败原因": "", "重试次数": 0,
    }})
    return rec


'''


def generate_spider_fallback(school: str, dept: str, out_dir: Path) -> Path:
    """无 LLM 兜底: 确定性头部 + 固定 main（调 process_batch，非 LLM 自由发挥）。

    与 LLM 路径共用头部与 DetailScraper 能力，仅 main 骨架是写死的——
    保证无 LLM 时仍满足 25 列契约/并发/断点续传/增量保存。
    """
    main_code = '''
def main():
    with open("teachers_input.json", encoding="utf-8") as f:
        teachers = json.load(f)["teachers"]

    scraper = DetailScraper(
        SCHOOL, DEPT, str(RAW_ROOT),
        csv_output_root=Path(__file__).resolve().parent,
        resume=(os.environ.get("RESUME", "") == "1"),
        reextract_llm=(os.environ.get("REEXTRACT_LLM", "") == "1"),
    )
    results = scraper.process_batch(
        teachers, workers=int(os.environ.get("TEACHER_WORKERS", "4")))
    scraper.save_results(results)

    ok = sum(1 for r in results
             if (r.get("来源类型") or "") not in ("fetch_failed", "error"))
    print(f"采集完成：总数={len(results)}，成功={ok}，失败={len(results) - ok}")


if __name__ == "__main__":
    main()
'''
    path = out_dir / "spider.py"
    path.write_text(build_spider_header(school, dept) + main_code, encoding="utf-8")
    print(f"🐍 兜底模板已生成 spider.py: {path}")
    return path


# ═══════════════════════════════════════════════════════════════
#  Step 4: 运行生成的 spider.py
# ═══════════════════════════════════════════════════════════════

class SpiderStalled(RuntimeError):
    """spider 输出停滞被看门狗终止（区别于代码崩溃与超时）。"""


def run_spider_script(out_dir: Path, timeout: int | None = None) -> int:
    """子进程运行 out_dir/spider.py，返回退出码。

    - 输出重定向到 out_dir/spider_run.log（进度/诊断都在这）
    - 超时默认取环境变量 SPIDER_TIMEOUT（秒），否则 6 小时
      （大院系数百教师 × LLM 提取可能超过 1 小时，3600s 已被安徽大学
      材料学部 242 人撑爆，且超时后 uv 被杀、python 子进程变孤儿继续跑）。
    - 停滞看门狗: 日志文件大小连续 SPIDER_STALL_MINUTES 分钟（默认15，
      置 0 关闭）无变化 → 判定卡死，杀进程组并抛 SpiderStalled。
      依赖 spider.py 每名教师打一行进度（行缓冲，见生成提示词）。
    - start_new_session=True: 超时/停滞杀整个进程组，不留孤儿。
    """
    import os
    import signal
    import threading

    if timeout is None:
        timeout = int(os.environ.get("SPIDER_TIMEOUT", "21600"))
    stall_minutes = int(os.environ.get("SPIDER_STALL_MINUTES", "15"))
    log_path = out_dir / "spider_run.log"
    cmd = ["uv", "run", "python", "spider.py"]
    print(f"🚀 运行: cd {out_dir} && {' '.join(cmd)} "
          f"(timeout={timeout}s, 停滞检测={stall_minutes}min, 日志→{log_path.name})")
    log_fh = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(out_dir), start_new_session=True,
                            stdout=log_fh, stderr=subprocess.STDOUT)

    stop = threading.Event()
    stall_reason: list[str] = []

    def _watchdog() -> None:
        if stall_minutes <= 0:
            return
        last_size = -1
        last_change = time.time()
        interval = min(30, max(5, stall_minutes * 60 // 4))
        while not stop.is_set():
            time.sleep(interval)
            if stop.is_set():
                break
            try:
                cur_size = log_path.stat().st_size
            except OSError:
                cur_size = 0
            if cur_size != last_size:
                last_size = cur_size
                last_change = time.time()
            elif (proc.poll() is None
                  and time.time() - last_change >= stall_minutes * 60):
                stall_reason.append(f"{stall_minutes} 分钟无输出")
                print(f"🧟 spider 停滞（{stall_minutes}min 无输出），杀进程组…")
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()

    def _tail_log(n: int = 12) -> None:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("  ── spider_run.log 末尾 ──")
            for ln in lines[-n:]:
                print(f"  | {ln[:150]}")
        except OSError:
            pass

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"⏰ spider.py 超时({timeout}s)，杀整个进程组…")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _tail_log()
        raise
    finally:
        stop.set()
        watcher.join(timeout=5)
        log_fh.close()

    if stall_reason:
        _tail_log()
        raise SpiderStalled(stall_reason[0])
    return rc


def validate_spider_output(out_dir: Path) -> list[str]:
    """校验 spider.py 输出契约（25 列 / 来源类型非空 / 有数据）。返回问题列表。"""
    import csv
    from detail_scraper import DETAIL_FIELDS

    csv_path = out_dir / "teachers_detail.csv"
    if not csv_path.exists():
        return ["teachers_detail.csv 不存在"]

    try:
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    except Exception as e:
        return [f"CSV 读取失败: {e}"]
    if not rows:
        return ["CSV 无数据行"]

    expected_cols = (
        ["姓名", "详情页URL", "来源类型", "原始文件", "头像文件",
         "has_pdf", "pdf_url", "pdf_path", "html_path", "采集时间",
         "失败原因", "重试次数"] + DETAIL_FIELDS
    )
    issues = []
    missing = [c for c in expected_cols if c not in rows[0]]
    if missing:
        issues.append(f"CSV 缺列: {missing}")
    empty_type = sum(1 for r in rows if not (r.get("来源类型") or "").strip())
    if empty_type:
        issues.append(f"{empty_type} 行来源类型为空（23字段契约被破坏）")
    return issues


def write_input_manifest(teachers: list[dict], out_dir: Path) -> Path:
    """写 teachers_input.json（spider.py 的输入清单，保证可复现）。"""
    path = out_dir / "teachers_input.json"
    data = {
        "school": teachers[0].get("school", "") if teachers else "",
        "dept": teachers[0].get("dept", "") if teachers else "",
        "total": len(teachers),
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "teachers": [
            {
                "name": t.get("name", ""),
                "detail_url": t.get("detail_url", ""),
                "title": t.get("title", ""),
                "category": t.get("category", ""),
            }
            for t in teachers
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📇 输入清单: {path} ({len(teachers)} 人)")
    return path
