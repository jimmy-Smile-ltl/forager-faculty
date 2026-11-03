#!/usr/bin/env python3
"""
教师详情页采集 Agent — 单院系自主分析管道。

流程:
  0. (可选) 师资分类发现 — 从院系官网补齐 Step 3 漏抓的教师分类
  1. 加载教师清单（department teachers/output 的 Step 3 产出）
  2. LLM 分层抽样分析页面结构 → analysis.md
     （页面分析/生成/运行的实现都在 spider_gen.py，本文件只管编排）
  3. LLM 生成 spider.py + teachers_input.json 输入清单
  4. 运行 spider.py → teachers_detail.csv/json(25列契约) + 原始数据落盘
  5. 分层评估（成功率口径: no_url 不算失败；字段覆盖只评有内容样本；
     PDF 未下载 = pending 单列）→ evaluation.md
  6. 不通过 → 重新分析/生成/运行（最多 3 轮）；超时/停滞则用增量保存的部分结果评估

产物（每个院系目录）: analysis.md + teachers_input.json + spider.py
                      + teachers_detail.csv/json + evaluation.md
结果必须来自 spider.py 运行（可复现: cd <out_dir> && uv run python spider.py）。
设计改进教训见同目录 improvements.md（新批次运行前必读）。

用法:
    uv run python dept_agent.py --school 安徽大学 --dept 材料科学与工程学部
    uv run python run_single_dept.py --school 安徽大学 --dept 材料科学与工程学部
                                     [--with-discovery]   # 默认跳过发现省网络
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ── 路径 ──
_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_DEPT_TEACHERS = _COLLEGE_ROOT / "department teachers" / "output"
_RAW_DATA_ROOT = _COLLEGE_ROOT / "tearchers raw data"
_UTIL_ROOT = _COLLEGE_ROOT / "util"
_THIS_DIR = _BASE

sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_UTIL_ROOT))

from detail_scraper import (  # noqa: E402
    DetailScraper, DETAIL_FIELDS, classify_record, _sanitize_path,
)

# ── 输出 CSV 字段（25 列契约: 23 基础列 + 失败原因/重试次数）──
CSV_HEADERS = [
    "姓名", "详情页URL", "来源类型", "原始文件", "头像文件",
    "has_pdf", "pdf_url", "pdf_path", "html_path", "采集时间",
    "失败原因", "重试次数",
] + DETAIL_FIELDS


# ═══════════════════════════════════════════════════════════════
#  Step 0: 师资分类发现（解决 Step 3 只抓一类教师的遗漏）
# ═══════════════════════════════════════════════════════════════

def run_discovery(school: str, dept: str, teachers: list[dict],
                  faculty_url_hints: list[str] | None = None) -> tuple[list[dict], dict]:
    """从院系官网发现全部师资分类并合并教师清单。

    faculty_url_hints: 已知师资页候选（救场场景 = 被过滤条目的 URL，
    常是分类页直链）。逐个作为线索跑 discover 并累加合并。
    """
    from faculty_discovery import FacultyDiscovery

    dept_dir = _DEPT_TEACHERS / school / dept
    dept_url = ""
    faculty_url = ""

    # info.csv → 院系官网 URL
    info_csv = dept_dir / "info.csv"
    if info_csv.exists():
        try:
            import csv as _csv
            with open(info_csv, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    dept_url = (row.get("院系URL") or "").strip()
                    if dept_url:
                        break
        except Exception:
            pass

    # teachers.json 顶层 → 已知师资页 URL
    jp = dept_dir / "teachers.json"
    if jp.exists():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            faculty_url = data.get("faculty_url", "")
        except Exception:
            pass

    print(f"\n{'=' * 60}")
    hints = []
    if faculty_url:
        hints.append(faculty_url)
    if faculty_url_hints:
        hints.extend(faculty_url_hints)

    print(f"  🕸️ Step 0: 师资分类发现 — {school} > {dept}")
    print(f"  官网: {dept_url or '(无)'}  线索页: {len(hints)} 个")
    print(f"{'=' * 60}")

    scraper = DetailScraper(
        school_name=school, dept_name=dept, raw_data_root=str(_RAW_DATA_ROOT)
    )
    fd = FacultyDiscovery(scraper)

    # 逐个线索跑 discover 并累加（合并去重按 detail_url）
    merged = list(teachers)
    report = None
    for hint in hints or [""]:
        try:
            merged, report = fd.discover(merged, dept_url, hint)
        except Exception as e:
            print(f"  ⚠️ 发现失败(线索={hint[:60]}): {str(e)[:80]}")
    return merged, report


# ═══════════════════════════════════════════════════════════════
#  Step 1: 加载教师清单（Step 3 产出）
#  （页面结构分析在 spider_gen.analyze_pages_llm，本文件不再保留
#   旧版 analyze_sample，避免两份"样本分析"逻辑分叉）
# ═══════════════════════════════════════════════════════════════

def _sanitize_detail_url(u: str) -> str:
    """URL 结构校验(2026-10-16 用户指令「提取到的url应该在结构性检查」):
    非 http(s):// 开头的 detail_url 是字段错位/非详情链接(院系名/教师姓名/
    mailto:/javascript:/file:// — 全库实测 1,916 条) → 清空。
    空字符串保持空(A 类空 URL 是名单性质问题, 另一条线处理)。"""
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith(("http://", "https://")) else ""


def load_teachers(school: str, dept: str) -> list[dict]:
    """加载教师列表（JSON 空时回退 CSV）。名字做职称粘连清洗。"""
    dept_dir = _DEPT_TEACHERS / school / dept
    teachers = []
    jp = dept_dir / "teachers.json"
    if jp.exists():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            teachers = data.get("teachers", [])
        except Exception:
            pass
    if not teachers:
        csv_path = dept_dir / "teachers.csv"
        if csv_path.exists():
            import csv
            with open(csv_path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("教师姓名") or row.get("姓名") or "").strip()
                    url = (row.get("个人详情URL") or row.get("详情页URL") or "").strip()
                    if name:
                        teachers.append({"name": name, "detail_url": url})
    # URL 结构校验: 非 http(s) 的 detail_url 一律清空(防坏数据进下游)
    for t in teachers:
        if isinstance(t, dict) and t.get("detail_url"):
            t["detail_url"] = _sanitize_detail_url(t["detail_url"])
    # 名字清洗: '陈招斌高' → 陈招斌
    from faculty_discovery import clean_name
    for t in teachers:
        name, title = clean_name(t.get("name", ""))
        t["name"] = name
        if title and not t.get("title"):
            t["title"] = title
    # 过滤非人名
    from extract_all_details import _is_person_name
    return [t for t in teachers if _is_person_name(t.get("name", ""), t.get("detail_url", ""))]


# ═══════════════════════════════════════════════════════════════
#  Step 2: 写 analysis.md
# ═══════════════════════════════════════════════════════════════

def write_analysis(analysis: dict, out_dir: Path, discovery_report: dict | None = None,
                   mode_note: str = "") -> Path:
    """写 analysis.md：结构结论 + 样本明细表 + 字段获取策略（含发现报告）。

    mode_note: 模式标注（如 "抽样测试模式: 仅 N 名教师"），写在标题下。
    """
    lines = [
        f"# {analysis['dept']} — 教师详情页分析报告",
    ]
    if mode_note:
        lines.append(f"> ⚠️ {mode_note}")
    lines += [
        "",
        f"- 学校: {analysis['school']}",
        f"- 院系: {analysis['dept']}",
        f"- 分析时间: {analysis['analyzed_at']}",
        f"- 样本数: {analysis['sample_count']}",
        "",
    ]

    # ── 师资分类发现报告（Step 0）──
    if discovery_report:
        lines += [
            "## 师资分类发现",
            "",
            f"- 院系官网: {discovery_report.get('dept_url', '')}",
            f"- 师资页: {discovery_report.get('faculty_url', '')}",
            f"- Step 3 教师数: {discovery_report.get('step3_count', 0)}",
            f"- 发现新增: {discovery_report.get('new_vs_step3', 0)}",
            f"- **合并后总数: {discovery_report.get('merged_count', 0)}**",
            "",
            "| 分类 | URL | 教师数 | 页数 | 备注 |",
            "|------|-----|--------|------|------|",
        ]
        for c in discovery_report.get("categories", []):
            note = "前缀回退" if c.get("fallback_used") else (c.get("error") or "")
            lines.append(
                f"| {c.get('name', '?')} | {c.get('url', '')[:55]} | "
                f"{c.get('teachers', 0)} | {c.get('pages', 0)} | {note} |"
            )
        lines.append("")

    c = analysis.get("conclusion", {})
    lines += [
        "## 页面结构结论（LLM 分析）",
        "",
        f"- **预检站点原型**: {analysis.get('archetype', '未预检')}",
        f"- **页面类型**: {c.get('page_type', 'unknown')}",
        f"- **正文容器**: `{c.get('content_selector') or c.get('best_content_selector', '')}`",
        f"- **PDF 载体**: {c.get('pdf_container', '无')}",
        f"- **页面可见字段**: {', '.join(c.get('visible_fields', [])) or '(未分析)'}",
        "",
        "## 样本明细",
        "",
        "| 姓名 | 来源 | 页面类型 | 正文容器 | PDF载体 | 可见字段 | 判断理由 |",
        "|------|------|---------|---------|---------|---------|---------|",
    ]
    for s in analysis.get("samples", []):
        lines.append(
            f"| {s.get('name', '?')} | {s.get('source', s.get('status', ''))} | "
            f"{s.get('page_type', '?')} | {s.get('content_selector', '')} | "
            f"{s.get('pdf_container', '')} | "
            f"{','.join(s.get('visible_fields', []) or [])[:40]} | "
            f"{s.get('reason', '')[:40]} |"
        )
    lines.append("")
    lines.append("## 字段获取策略")
    lines.append("")
    lines.append("- **PDF 壳页**: 检测 `div.wp_pdf_player`/iframe/a 链接中的 PDF → 下载 → pdfplumber 自适应提取文本（表格简历页压平成 `标签：值` 行，论文页走线性）→ LLM 提取 13 字段")
    lines.append("- **HTML 正文页**: 优先正文容器提取纯文本 → LLM 提取 13 字段")
    lines.append("- **HTML 壳 + PDF**: 两者都保存，has_pdf/pdf_url/pdf_path 记录在 CSV")
    lines.append("")
    path = out_dir / "analysis.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"📄 分析报告: {path}")
    return path
# ═══════════════════════════════════════════════════════════════
#  Step 4: 运行
# ═══════════════════════════════════════════════════════════════

def run_spider(school: str, dept: str, teachers: list[dict]) -> tuple[list[dict], Path, Path]:
    """运行采集（直接调用 DetailScraper，与 spider.py 同逻辑）。"""
    from detail_scraper import DOWNLOAD_PDF, RATE_LIMIT

    print(f"\n{'=' * 60}")
    print(f"  🚀 Step 3: 运行采集 — {school} > {dept} ({len(teachers)} 人)")
    print(f"  📋 DOWNLOAD_PDF={'开' if DOWNLOAD_PDF else '关'} (环境变量控制)")
    print(f"{'=' * 60}")

    scraper = DetailScraper(
        school_name=school, dept_name=dept,
        raw_data_root=str(_RAW_DATA_ROOT),
        csv_output_root=str(_THIS_DIR),
        rate_limit=RATE_LIMIT,
    )
    results = scraper.process_batch(teachers)
    scraper.save_results(results)

    csv_path = _THIS_DIR / school / dept / "teachers_detail.csv"
    json_path = _THIS_DIR / school / dept / "teachers_detail.json"
    return results, csv_path, json_path


# ═══════════════════════════════════════════════════════════════
#  Step 5: 评估
# ═══════════════════════════════════════════════════════════════

def evaluate(results: list[dict], analysis: dict) -> dict:
    """分层评估采集结果（口径与页面类型对齐）。

    Ω 规格（理想准则的单一事实来源）: teachers/quality_criteria.md。
    阈值改动必须先改规格文件，再改这里的常量。

    口径（状态判定统一走 detail_scraper.classify_record）:
      - 采集完成度: 全量考核；no_url（纯名单）与 pending_pdf（有 PDF 引用
        未落地，含 html_fallback/html_shell_pdf）是合法状态，不计失败
      - 字段覆盖: 只评"有内容可提取"的样本（html / pdf）
      - pending 只考核 pdf_url 已记录，可回补重试
      - 质量校验: 职称∈词表 / 邮箱格式 / 日期格式 / 长度 sanity（只报告不扣分）
    """
    from detail_scraper import DOWNLOAD_PDF, FIELD_SPEC, classify_record

    total = len(results)
    if total == 0:
        return {"passed": False, "score": 0, "issues": ["无结果"]}

    # ── 分层统计（单一口径: detail_scraper.classify_record）──
    #   ok = PDF 已落地 / HTML 正文；pending_pdf = 有 PDF 引用未落地
    #   （html_fallback 与 html_shell_pdf 都归 pending，可回补重试）
    classes = [classify_record(r) for r in results]
    shell_pdf = [r for r, c in zip(results, classes) if c == "pending_pdf"]
    failed = [r for r, c in zip(results, classes) if c == "failed"]
    no_url = [r for r, c in zip(results, classes) if c == "no_url"]
    content_rows = [r for r, c in zip(results, classes)
                    if c == "ok" and r.get("来源类型") in ("html", "pdf")]

    # 采集成功率: no_url / pending 是合法状态，不算失败
    success_rate = sum(c in ("ok", "pending_pdf", "no_url") for c in classes) / total

    # PDF URL 记录覆盖（pdf 壳页必须记录 pdf_url）
    # 注意: CSV 中 has_pdf 是 "是"/"否" 字符串，JSON 中是布尔，两种都要认
    def _has_pdf(r) -> bool:
        v = r.get("has_pdf")
        return v in (True, "是", "true", "True", 1, "1")

    pdf_rows = [r for r in results if _has_pdf(r)]
    pdf_url_ok = sum(1 for r in pdf_rows if r.get("pdf_url", "").startswith("http"))
    pdf_rate = pdf_url_ok / len(pdf_rows) if pdf_rows else 1.0

    # ── 字段覆盖（只评内容样本）──
    field_coverage = {}
    for f in ["职称", "研究方向", "教育经历", "工作经历", "联系方式"]:
        cnt = sum(1 for r in content_rows if r.get(f))
        field_coverage[f] = cnt / len(content_rows) if content_rows else 0.0
    avg_coverage = sum(field_coverage.values()) / len(field_coverage)

    # ── 质量校验 ──
    title_values = []
    for s in FIELD_SPEC:
        if s["key"] == "职称":
            title_values = s.get("values", [])
    quality_issues = []
    bad_titles = 0
    bad_emails = 0
    for r in results:
        t = r.get("职称", "")
        if t and t not in title_values and not any(v in t for v in title_values):
            bad_titles += 1
        contact = r.get("联系方式", "")
        emails = re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+', contact)
        bad_emails += sum(1 for e in emails if "@" not in e)
    if bad_titles:
        quality_issues.append(f"{bad_titles} 条职称不在取值词表")
    if bad_emails:
        quality_issues.append(f"{bad_emails} 条邮箱格式异常")

    # ── 评分: 采集 40 + 字段 40 + PDF 20 ──
    page_is_pdf_shell = analysis["conclusion"]["page_type"] == "pdf_shell"
    score = 40 * success_rate
    if page_is_pdf_shell and not DOWNLOAD_PDF:
        # pending 模式: 字段待回补，字段分换成 PDF URL 记录分
        score += 40 * pdf_rate
        score += 20 * pdf_rate
    else:
        score += 40 * min(avg_coverage / 0.7, 1.0)
        score += 20 * pdf_rate

    # ── 问题清单 ──
    issues = list(quality_issues)
    if len(failed) > 0:
        # 区分失败子类: 名单URL无效 = 上游数据问题(非agent缺陷), 单独标注
        # (第3轮: 中南国家卓越工程师学院 410 人 URL 全为院系名)
        invalid_url_n = sum(1 for r in failed
                            if "名单URL无效" in (r.get("失败原因") or ""))
        if invalid_url_n:
            issues.append(f"{invalid_url_n} 人名单URL无效(上游名单数据问题, 非agent缺陷)")
        if invalid_url_n < len(failed):
            real_failed = [r for r in failed
                           if "名单URL无效" not in (r.get("失败原因") or "")]
            issues.append(f"{len(real_failed)} 人抓取失败: "
                          f"{', '.join(r['姓名'] for r in real_failed[:5])}")
    if page_is_pdf_shell and pdf_rate < 0.9:
        issues.append(f"PDF URL 记录不足: {pdf_rate:.0%}")
    if len(shell_pdf) > 0:
        issues.append(f"{len(shell_pdf)} 人 PDF 待回补(pending, 非失败)")
    if len(no_url) > 0:
        issues.append(f"{len(no_url)} 人无详情页(纯名单, 非失败)")
    # 空壳诊断: 内容样本几乎全空 → 可能容器选错，也可能页面本身内容
    # 缺失（北大历史学系教训: 详情页只有职称一行，其余字段 JS 动态
    # 加载/页面未展开，静态抓取拿不到——不要引导无谓的重新生成重跑）
    empty_rich = [r for r in content_rows
                  if len(r.get("原始文件", "")) > 0 and not any(r.get(f) for f in field_coverage)]
    if empty_rich:
        issues.append(f"{len(empty_rich)} 个页面有内容但字段全空(容器选错或页面内容缺失): "
                      f"{', '.join(r['姓名'] for r in empty_rich[:5])}")

    passed = score >= 70 and success_rate >= 0.9
    return {
        "passed": passed,
        "score": round(score, 1),
        "success_rate": success_rate,
        "pdf_rate": pdf_rate,
        "field_coverage": field_coverage,
        "avg_coverage": avg_coverage,
        "type_dist": {
            "html+pdf": len(content_rows),
            "shell_pdf(pending)": len(shell_pdf),
            "no_url(纯名单)": len(no_url),
            "fetch_failed": len(failed),
        },
        "issues": issues,
    }


# ═══════════════════════════════════════════════════════════════
#  Step 6: 写评估报告
# ═══════════════════════════════════════════════════════════════

def write_evaluation(eval_result: dict, analysis: dict, out_dir: Path, round_num: int,
                     mode_note: str = ""):
    """写 evaluation.md：评分/成功率/类型分布/字段覆盖率/问题清单。

    mode_note: 模式标注（抽样测试时注明，避免与全量评估混淆）。
    """
    td = eval_result.get("type_dist", {})
    lines = [
        f"# 采集评估报告（第 {round_num} 轮）",
    ]
    if mode_note:
        lines.append(f"> ⚠️ {mode_note}")
    lines += [
        "",
        f"- 评分: **{eval_result['score']}** {'✅ 通过' if eval_result['passed'] else '❌ 不通过'}",
        f"- 采集成功率: {eval_result['success_rate']:.0%}（no_url 纯名单不计失败）",
        f"- PDF URL 记录率: {eval_result['pdf_rate']:.0%}",
        "",
        "## 类型分布",
        "",
        f"- 有内容样本(html+pdf): {td.get('html+pdf', 0)}",
        f"- PDF 待回补(pending): {td.get('shell_pdf(pending)', 0)}",
        f"- 无详情页(纯名单): {td.get('no_url(纯名单)', 0)}",
        f"- 抓取失败: {td.get('fetch_failed', 0)}",
        "",
        "## 字段覆盖率（仅统计有内容样本）",
        "",
    ]
    for f, cov in eval_result["field_coverage"].items():
        lines.append(f"- {f}: {cov:.0%}")
    lines.append(f"- 平均: {eval_result.get('avg_coverage', 0):.0%}")
    if eval_result["issues"]:
        lines.append("")
        lines.append("## 问题")
        for i in eval_result["issues"]:
            lines.append(f"- {i}")
    lines.append("")
    path = out_dir / "evaluation.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"📊 评估报告: {path}")


# ═══════════════════════════════════════════════════════════════
#  站点原型注册表（G 对自身结论的记忆，RSO 不动点的积累）
# ═══════════════════════════════════════════════════════════════

def _site_registry_path() -> Path:
    return _THIS_DIR / "site_registry.json"


def load_site_registry(school: str = "") -> list[dict]:
    """读注册表；school 非空时只取该校条目（供同校院系分析 warm-start）。"""
    path = _site_registry_path()
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if school:
        entries = [e for e in entries if e.get("school") == school]
    return entries


def _update_site_registry(school: str, dept: str, analysis: dict,
                          eval_result: dict, teachers: list[dict]) -> None:
    """归档本院系结论: 站点原型（页面类型/PDF载体）+ 成败模式。

    消费方: spider_gen 分析提示词 warm-start（同校已归档结论）、
    batch_run 回顾、人工排查。写坏注册表不影响主流程。
    """
    from datetime import datetime
    from urllib.parse import urlparse

    path = _site_registry_path()
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    urls = [t.get("detail_url") for t in teachers if t.get("detail_url")]
    domain = urlparse(urls[0]).netloc if urls else ""
    c = analysis.get("conclusion") or {}
    mode = "通过" if eval_result.get("passed") else "; ".join(
        (eval_result.get("issues") or [])[:2])
    entry = {
        "school": school,
        "dept": dept,
        "domain": domain,
        "archetype": analysis.get("archetype", ""),
        "page_type": c.get("page_type", ""),
        "pdf_container": c.get("pdf_container", ""),
        "content_selector": c.get("content_selector", ""),
        "score": eval_result.get("score", 0),
        "passed": bool(eval_result.get("passed")),
        "mode": mode[:150],
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 同校同系覆盖旧条目
    entries = [e for e in entries
               if not (e.get("school") == school and e.get("dept") == dept)]
    entries.append(entry)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
#  单院系完整管道（batch_run 也调用）
# ═══════════════════════════════════════════════════════════════

def run_dept(school: str, dept: str, sample: int = 5,
             max_rounds: int = 3, out_dir: Path | None = None,
             skip_discovery: bool = False) -> dict:
    """完整 agent 管道: 发现 → LLM分析 → LLM生成spider.py → 运行 → 评估。

    Returns: {school, dept, passed, score, eval_result, analysis, discovery_report}
    """
    out_dir = out_dir or (_THIS_DIR / school / dept)
    out_dir.mkdir(parents=True, exist_ok=True)

    teachers = load_teachers(school, dept)
    # ── 过滤后空但原始清单非空 → 数据是分类导航/栏目链接（吉大植物
    #    科学学院教训: "教授/副教授/讲师" 是师资分类页）→ 自动执行
    #    师资发现救场，即使 skip_discovery=True（否则该院系必死）。
    #    被过滤条目的 URL 就是现成的发现线索（分类页直链）。──
    raw_count = 0
    rescue_hints: list[str] = []
    if not teachers:
        jp = _DEPT_TEACHERS / school / dept / "teachers.json"
        if jp.exists():
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
                raw_list = data.get("teachers", [])
                raw_count = len(raw_list)
                rescue_hints = [
                    (t.get("detail_url") or "").strip()
                    for t in raw_list
                    if (t.get("detail_url") or "").startswith("http")
                ]
            except Exception:
                pass
    if raw_count > 0 and (not teachers or len(teachers) < 5):
        # 0 人 = 全是分类导航链接; <5 人 = 名单过少，发现补全有价值
        reason = ("全部被过滤（疑为分类导航链接）" if not teachers
                  else f"过滤后仅 {len(teachers)} 人（过少）")
        print(f"  🧹 {school}/{dept}: Step 3 原始 {raw_count} 条{reason}，"
              f"自动执行师资发现救场（线索 {len(rescue_hints)} 个）")
        skip_discovery = False
    print(f"👥 {school} > {dept}: Step 3 提供 {len(teachers)} 名教师")

    # ── Step 0: 师资分类发现（多分类/多 URL 补全）──
    discovery_report = None
    if not skip_discovery:
        try:
            teachers, discovery_report = run_discovery(
                school, dept, teachers, rescue_hints or None)
            if discovery_report:
                print(f"  🕸️ 发现合并后: {len(teachers)} 名教师 "
                      f"(Step 3 {discovery_report['step3_count']} + 新增 {discovery_report['new_vs_step3']})")
        except Exception as e:
            print(f"  ⚠️ 师资发现失败（使用 Step 3 原始列表）: {str(e)[:80]}")

    # 发现（含救场）之后仍无教师 → 才判死
    if not teachers:
        print(f"❌ {school}/{dept}: 无教师数据")
        return {"school": school, "dept": dept, "passed": False, "score": 0,
                "error": "无教师数据"}

    # ── 分析器（网络+LLM 都经由 DetailScraper 的统一层）──
    scraper = DetailScraper(
        school_name=school, dept_name=dept, raw_data_root=str(_RAW_DATA_ROOT)
    )
    # 任务边界: 新系重新均衡分配 token, 系内连续调用固定同一 token (命中网关缓存省钱)
    if scraper.has_llm:
        scraper._llm.begin_task()
    raw_dir = _RAW_DATA_ROOT / school / dept

    # ── Step 0.5: 预检站点原型 + 策略路由（在花钱前分类）──
    from spider_gen import preflight_archetype
    preflight = preflight_archetype(scraper, teachers, raw_dir)
    archetype = preflight["archetype"]
    print(f"  🔎 预检原型: {archetype} "
          f"(no_url={preflight['no_url_ratio']}, 空壳={preflight['empty_shell_ratio']})")

    # 路由: 名单无链接站 → 搜索式发现补 URL（预算受限）
    if archetype == "name_list_no_links":
        no_url_teachers = [t for t in teachers if not t.get("detail_url")]
        # 校域名线索: info.csv 院系URL
        domain_hint = ""
        info_csv = _DEPT_TEACHERS / school / dept / "info.csv"
        if info_csv.exists():
            try:
                import csv as _csv
                for row in _csv.DictReader(open(info_csv, encoding="utf-8")):
                    domain_hint = (row.get("院系URL") or "").strip()
                    if domain_hint:
                        break
            except Exception:
                pass
        try:
            from search_rescue import search_rescue_names, save_names_without_url
            rescued, missing = search_rescue_names(
                scraper, school, dept,
                [{"name": t.get("name"),
                  "category": t.get("category", "未知")}
                 for t in no_url_teachers],
                school_domain_hint=domain_hint)
            if rescued:
                teachers = teachers + rescued
                print(f"  🔎 搜索补全 {len(rescued)} 个 URL, "
                      f"剩余无URL {len(missing)} 人")
            save_names_without_url(out_dir, missing)
        except Exception as e:
            print(f"  ⚠️ 搜索式发现失败（保留无URL名单）: {str(e)[:80]}")

    # ── Step 1: LLM 分层抽样分析 ──
    from spider_gen import (
        analyze_pages_llm, generate_spider_llm, generate_spider_fallback,
        run_spider_script, validate_spider_output, write_input_manifest,
        SpiderStalled,
    )

    def _run_analysis(sample_size: int) -> dict:
        """LLM 分析样本 → 包装成 analysis 结构。

        注入同校已归档站点原型（site_registry.json）做 warm-start:
        同校院系常共用同一 CMS 结构，不必每个院系从零分析。
        """
        if scraper.has_llm:
            hints = load_site_registry(school)
            conclusion = analyze_pages_llm(scraper, teachers, raw_dir, sample_size,
                                           registry_hints=hints or None)
        else:
            conclusion = {
                "page_type": "unknown", "content_selector": "",
                "pdf_container": "无", "visible_fields": [],
                "sample_verdicts": [], "analyzed_at": "",
            }
        return {
            "school": school,
            "dept": dept,
            "archetype": archetype,  # 预检站点原型（策略路由结论）
            "preflight": preflight,
            "sample_count": len(conclusion.get("sample_verdicts", [])),
            "samples": conclusion.get("sample_verdicts", []),
            "conclusion": {
                "page_type": conclusion.get("page_type", "unknown"),
                "content_selector": conclusion.get("content_selector", ""),
                "pdf_container": conclusion.get("pdf_container", "无"),
                "visible_fields": conclusion.get("visible_fields", []),
            },
            "analyzed_at": conclusion.get("analyzed_at", ""),
        }

    analysis = _run_analysis(sample)
    write_analysis(analysis, out_dir, discovery_report)

    # ── Step 2: LLM 生成 spider.py + 输入清单 ──
    write_input_manifest(teachers, out_dir)
    if scraper.has_llm:
        generate_spider_llm(scraper, school, dept, analysis["conclusion"], out_dir)
    else:
        generate_spider_fallback(school, dept, out_dir)  # 无LLM确定性兜底

    # ── Step 3-4: 运行 spider.py + 契约校验 + 评估 + 循环 ──
    eval_result = {"passed": False, "score": 0, "issues": ["未运行"]}
    for round_num in range(1, max_rounds + 1):
        # 运行生成的 spider.py（结果必须来自脚本运行）
        timed_out = False
        try:
            rc = run_spider_script(out_dir)
        except (subprocess.TimeoutExpired, SpiderStalled) as e:
            rc = -1
            timed_out = True
            print(f"  ⚠️ spider.py 运行中断（{type(e).__name__}），改用增量保存的部分结果评估")

        # 输出契约校验: 23 列 / 来源类型非空 —— 违规则重新生成代码
        # （超时不重新生成: 超时不是代码问题，重生成只会重复跑）
        contract_issues = validate_spider_output(out_dir)
        if (rc != 0 or contract_issues) and round_num < max_rounds and not timed_out:
            print(f"  ⚠️ spider.py 问题(退出码={rc}, 契约违规={contract_issues[:2]})，重新生成...")
            if scraper.has_llm:
                generate_spider_llm(scraper, school, dept, analysis["conclusion"], out_dir)
            continue

        csv_path = out_dir / "teachers_detail.csv"
        json_path = out_dir / "teachers_detail.json"
        results = []
        if csv_path.exists():
            import csv as _csv
            try:
                results = list(_csv.DictReader(open(csv_path, encoding="utf-8")))
            except Exception as e:
                print(f"  ⚠️ CSV 读取失败: {e}")

        eval_result = evaluate(results, analysis)
        write_evaluation(eval_result, analysis, out_dir, round_num)

        if eval_result["passed"]:
            print(f"\n✅ 第 {round_num} 轮评估通过 (score={eval_result['score']})")
            break

        print(f"\n❌ 第 {round_num} 轮评估不通过 (score={eval_result['score']})")
        for issue in eval_result["issues"]:
            print(f"   - {issue}")

        # ── 重试价值判断（两类数据源限制，重分析/重生成/重跑都无济于事）──
        # ① 纯名单主导（上外中文学院: 15/16 无URL → 名单页无详情链接）
        td = eval_result.get("type_dist", {})
        no_url_n = td.get("no_url(纯名单)", 0)
        total_n = sum(td.values()) if td else 0
        no_url_dominant = total_n > 0 and no_url_n / total_n >= 0.5
        # ② 内容空壳主导（北大历史学系: 19/19 字段全空 → 内容 JS 渲染，
        #    静态抓取拿不到，换容器/重生成无用）
        content_rows = [r for r in results
                        if classify_record(r) == "ok"
                        and r.get("来源类型") in ("html", "pdf")]
        empty_shells = [r for r in content_rows
                        if not any(r.get(f) for f in DETAIL_FIELDS)]
        empty_shell_dominant = (len(content_rows) >= 5 and content_rows
                                and len(empty_shells) / len(content_rows) >= 0.9)
        if not eval_result["passed"] and no_url_dominant and td.get("fetch_failed", 0) == 0:
            print(f"  ⏹️ 纯名单主导({no_url_n}/{total_n} 无URL)，数据源缺详情链接，重试无意义，保留现有结果")
            break
        if not eval_result["passed"] and empty_shell_dominant and td.get("fetch_failed", 0) == 0:
            print(f"  ⏹️ 内容空壳主导({len(empty_shells)}/{len(content_rows)} 字段全空)，"
                  f"静态抓取拿不到内容(疑JS渲染)，重试无意义，保留现有结果")
            break

        if round_num < max_rounds:
            print(f"  🔄 重新分析并重试...")
            analysis = _run_analysis(min(sample + round_num, 10))
            write_analysis(analysis, out_dir, discovery_report)
            if scraper.has_llm:
                generate_spider_llm(scraper, school, dept, analysis["conclusion"], out_dir)
        else:
            print(f"  ⚠️ 达到最大轮数，保留现有结果")

    # ── 站点原型归档（G 记忆自身结论，供同校后续院系 warm-start）──
    try:
        _update_site_registry(school, dept, analysis, eval_result, teachers)
    except Exception as e:
        print(f"  ⚠️ 站点注册表写入失败: {e}")

    return {
        "school": school,
        "dept": dept,
        "passed": eval_result.get("passed", False),
        "score": eval_result.get("score", 0),
        "eval_result": eval_result,
        "analysis": analysis,
        "discovery_report": discovery_report,
    }


def main():
    """CLI 入口（--school/--dept 必填）。批量场景请走 batch_run.py。"""
    parser = argparse.ArgumentParser(description="教师详情页采集 Agent（单院系）")
    parser.add_argument("--school", required=True)
    parser.add_argument("--dept", required=True)
    parser.add_argument("--sample", type=int, default=5, help="LLM 分析样本数(5-10)")
    parser.add_argument("--max-rounds", type=int, default=3, help="评估失败重试轮数")
    args = parser.parse_args()

    result = run_dept(args.school, args.dept, sample=args.sample,
                      max_rounds=args.max_rounds)
    if result.get("passed"):
        sys.exit(0)
    sys.exit(2)


if __name__ == "__main__":
    main()
