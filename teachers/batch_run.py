#!/usr/bin/env python3
"""
分批采集编排器 — 每批 N 个学院，批后回顾评估一次，输出 agent 改进建议，再继续下一批。

递归自优化系统（RSO，见 quality_criteria.md 与参考论文概念）的落地:
  - G（生成器）= agent 设计（dept_agent/spider_gen/detail_scraper）
  - P（产物）  = 各院系 teachers_detail.csv/json + raw data + spider.py
  - Ω（理想准则）= teachers/quality_criteria.md（评估与回顾的共同参照）
  - O（优化算子）= 院系内 3 轮评估重跑 + 本文件的批后回顾
  - M（元生成算子）= 回顾建议 → improvements.md 待办 → 改进生成器自身

流程:
  1. 列出学校下有待采集数据的院系
  2. 每 batch-size 个一批，跑完整 agent 管道（发现 → LLM分析 → LLM生成spider.py → 运行 → 评估）
  3. 批后回顾: Ω 规格 + 已记录问题注入 prompt → 区分新旧问题 → 报告
  4. 收敛判据: 连续 2 批零新增问题 → 回顾降频（每 2 批一次），有新问题立即恢复
  5. M 半自动化: [规则级]/[提示词级] 建议自动归档 improvements.md 待办，
     [代码级] 留人工

并发（--parallel）:
  - 批内院系用 ProcessPoolExecutor 并行（每个 run_dept 独立解释器，
    独立 HTTP 客户端/LLM/spider 子进程，互不污染；线程池会共享
    curl_cffi session 不安全）
  - 同校域请求压力 ×parallel，瑞数风控敏感站建议 2-3 起步
  - 瑞数 Cookie 缓存已改原子写（handleRuishu._save_cookies），
    多进程并发刷新同校注册域缓存安全
  - 并行时各院系日志交错输出（每行带时间戳）；顺序与输入一致

用法:
    uv run python batch_run.py --school 安徽大学 --batch-size 5
    uv run python batch_run.py --school 安徽大学 --batch-size 5 --parallel 3 --resume
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_DEPT_TEACHERS = _COLLEGE_ROOT / "department teachers" / "output"
_THIS_DIR = _BASE

sys.path.insert(0, str(_THIS_DIR))

from dept_agent import run_dept  # noqa: E402


# ═══════════════════════════════════════════════════════════════
#  院系列表
# ═══════════════════════════════════════════════════════════════

def list_depts(school: str) -> list[str]:
    school_dir = _DEPT_TEACHERS / school
    if not school_dir.is_dir():
        return []
    depts = []
    for sub in sorted(school_dir.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / "teachers.json").exists() or (sub / "teachers.csv").exists():
            depts.append(sub.name)
    return depts


def is_done(school: str, dept: str) -> bool:
    """有 evaluation.md 即视为已跑过（resume 跳过）。"""
    return (_THIS_DIR / school / dept / "evaluation.md").exists()


def _load_quality_criteria() -> str:
    """加载 Ω 规格（quality_criteria.md）。缺失时用内联摘要兜底。"""
    path = _THIS_DIR / "quality_criteria.md"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # 截断控制 prompt 长度（规格长文取关键段落）
        return text[:3000]
    return (
        "成功率≥90%（no_url/pending 不计失败）；PDF URL 记录率≥90%；"
        "核心5字段平均覆盖率≥70%；25列契约；失败必须带失败原因/重试次数；"
        "通过线 score≥70 且成功率≥90%"
    )


def _recorded_issue_headers() -> str:
    """improvements.md 的标题行 = 已记录问题清单（回顾时避免重复发明）。"""
    path = _THIS_DIR / "improvements.md"
    if not path.exists():
        return "(无历史记录)"
    headers = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            headers.append(ln[3:].strip())
    if not headers:
        return "(无历史记录)"
    return "\n".join(f"- {h}" for h in headers[-30:])


def _dept_domain(school: str, dept: str) -> str:
    """院系的注册域（用于同域分组串行）。info.csv 院系URL 优先，回退教师URL。

    注册域算法与 search_rescue._registered_domain 一致（edu.cn 取三段）。
    """
    from search_rescue import _registered_domain

    dept_dir = _DEPT_TEACHERS / school / dept
    # info.csv 院系URL
    info_csv = dept_dir / "info.csv"
    if info_csv.exists():
        try:
            import csv as _csv
            for row in _csv.DictReader(open(info_csv, encoding="utf-8")):
                u = (row.get("院系URL") or "").strip()
                if u:
                    return _registered_domain(u)
        except Exception:
            pass
    # 回退: teachers.json 首个教师 URL
    jp = dept_dir / "teachers.json"
    if jp.exists():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            for t in data.get("teachers", []):
                u = (t.get("detail_url") or "").strip()
                if u:
                    return _registered_domain(u)
        except Exception:
            pass
    return ""


def _registry_summary() -> str:
    """站点原型注册表统计（按原型计数 + 失败模式抽样），供回顾做数据依据。"""
    from collections import Counter

    path = _THIS_DIR / "site_registry.json"
    if not path.exists():
        return "(注册表为空)"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "(注册表读取失败)"
    if not entries:
        return "(注册表为空)"
    by_type = Counter(e.get("archetype") or "未预检" for e in entries)
    passed = sum(1 for e in entries if e.get("passed"))
    lines = [f"共 {len(entries)} 院系, 通过 {passed}, "
             f"原型分布: {dict(by_type)}"]
    fails = [e for e in entries if not e.get("passed")][:5]
    for e in fails:
        lines.append(f"- ❌ {e.get('school')}/{e.get('dept')}: {e.get('mode', '')[:60]}")
    return "\n".join(lines)


def update_review_state(streak: int, interval: int, new_count: int) -> tuple[int, int]:
    """收敛判据（RSO 不动点探测）: 连续 2 批零新增问题 → 回顾降频。

    生成器序列 {G_n} 的收敛信号 = 回顾不再产生新问题（输出与自身
    编码的改进准则自洽）。降频后 LLM 预算让给采集；一旦有新问题
    立即恢复每批回顾。
    Returns: (新 streak, 新 interval)
    """
    if new_count > 0:
        return 0, 1
    streak += 1
    if streak >= 2:
        return streak, 2
    return streak, 1


def extract_suggestions(report: str) -> dict[str, list[str]]:
    """从回顾报告提取结构化建议（[规则级]/[提示词级]/[代码级] 标签行）。"""
    import re
    out: dict[str, list[str]] = {"规则级": [], "提示词级": [], "代码级": []}
    for ln in report.splitlines():
        m = re.match(r"^\s*[-*]\s*\[(规则级|提示词级|代码级)\]\s*(.+)$", ln)
        if m:
            out[m.group(1)].append(m.group(2).strip())
    return out


def parse_new_issue_count(report: str) -> int:
    """解析回顾报告末行的 '新增问题数: N'。解析失败返回 -1（视为有新问题，保守回顾）。"""
    import re
    m = re.search(r"新增问题数[:：]\s*(\d+)", report)
    if m:
        return int(m.group(1))
    return -1


# ═══════════════════════════════════════════════════════════════
#  批后回顾
# ═══════════════════════════════════════════════════════════════

_RETRO_PROMPT = """你是采集 agent 的设计评审专家。以下是本批 {n} 个院系的采集评估摘要：

{summaries}

## 理想目标规格（Ω，权威参照: teachers/quality_criteria.md）

{criteria}

## 站点原型注册表统计（site_registry.json，本批前累计）

{registry_summary}

## 已记录的历史问题（improvements.md 中的条目）

{known_issues}

请从「提取策略 / 发现策略 / 采集策略 / 评估策略」四个维度分析:

1. **新增问题**: 本批新暴露的 agent 设计薄弱点（跨院系重复出现的失败模式）。
   只列"已记录问题"中没有的新东西，避免重复发明已修的问题
2. **改进建议**（每条必须以类型标签开头，方便自动归档）:
   - [规则级] 改规则/阈值/口径 —— 自动写入 improvements.md 待办
   - [提示词级] 改某个 prompt 模板 —— 自动写入 improvements.md 待办
   - [代码级] 需要改代码实现 —— 留给人工
3. **下一批注意事项**: 预计哪些院系会踩坑，提前怎么防

输出 markdown 报告（中文），直接给结论，不要寒暄。
报告最后一行必须是: 新增问题数: N（N 为整数，无新增写 0）"""


def retrospective(school: str, batch_results: list[dict], batch_idx: int, llm) -> tuple[str, str]:
    """LLM 回顾本批院系评估结果 → (报告路径, 报告原文)。

    Ω 规格（quality_criteria.md）与已记录问题（improvements.md 标题）
    注入 prompt，保证回顾的参照系与评估器一致、且能区分新旧问题。
    """
    summaries = []
    for r in batch_results:
        ev = r.get("eval_result", {})
        td = ev.get("type_dist", {})
        issues = "; ".join(ev.get("issues", [])[:3]) or "无"
        summaries.append(
            f"- [{r['dept']}] score={ev.get('score', '?')} "
            f"{'✅通过' if r.get('passed') else '❌不通过'} | "
            f"成功率={ev.get('success_rate', 0):.0%} | "
            f"类型(html/pdf/pending/no_url/fail)="
            f"{td.get('html+pdf', 0)}/{td.get('shell_pdf(pending)', 0)}/"
            f"{td.get('no_url(纯名单)', 0)}/{td.get('fetch_failed', 0)} | "
            f"平均字段覆盖={ev.get('avg_coverage', 0):.0%} | 问题: {issues}"
        )

    prompt = _RETRO_PROMPT.format(
        n=len(batch_results),
        summaries="\n".join(summaries),
        criteria=_load_quality_criteria(),
        known_issues=_recorded_issue_headers(),
        registry_summary=_registry_summary(),
    )
    try:
        report = llm.ask("你是采集 agent 设计评审专家。", prompt)
    except Exception as e:
        report = f"LLM 回顾失败: {e}"

    path = _THIS_DIR / f"batch_{batch_idx:03d}_retrospective.md"
    content = (
        f"# 第 {batch_idx} 批回顾报告 — {school}\n\n"
        f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 本批院系: {'、'.join(r['dept'] for r in batch_results)}\n\n"
        f"{report}\n"
    )
    path.write_text(content, encoding="utf-8")
    print(f"\n📋 批后回顾报告: {path}")
    return str(path), report


def append_improvements(note: str):
    """追加 agent 改进记录。"""
    path = _THIS_DIR / "improvements.md"
    entry = f"- [{datetime.now().strftime('%m-%d %H:%M')}] {note}\n"
    if not path.exists():
        path.write_text("# Agent 改进日志\n\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


def _append_suggestions(batch_idx: int, sug: dict[str, list[str]]) -> None:
    """M 半自动化: 结构化建议归档到 improvements.md。

    [规则级]/[提示词级] → 自动待办（改动范围小，下一批前可安全实施）；
    [代码级] → 人工待办。对应 RSO 的元生成算子 M：用优化产出的
    建议更新生成器自身。
    """
    if not any(sug.values()):
        return
    lines = [f"\n### 第 {batch_idx} 批回顾建议（自动归档 {datetime.now().strftime('%Y-%m-%d %H:%M')}）"]
    for level, label in (("规则级", "⏳ 待办(自动可实施)"),
                         ("提示词级", "⏳ 待办(自动可实施)"),
                         ("代码级", "👤 人工")):
        for s in sug[level]:
            lines.append(f"- [{label}] {s}")
    path = _THIS_DIR / "improvements.md"
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  📥 建议归档: 规则{len(sug['规则级'])} 提示词{len(sug['提示词级'])} 代码{len(sug['代码级'])}")


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def _run_one_dept(args: tuple) -> dict:
    """进程池 worker（模块级函数，picklable；macOS spawn 会重新 import 本模块）。

    异常在此兜底，保证单院系失败不拖垮整批。
    """
    school, dept, sample = args
    try:
        return run_dept(school, dept, sample=sample)
    except Exception as e:
        return {"school": school, "dept": dept, "passed": False,
                "score": 0, "eval_result": {"issues": [f"异常: {str(e)[:100]}"]}}


def main():
    parser = argparse.ArgumentParser(description="分批采集（5 学院一批 + 批后回顾）")
    parser.add_argument("--school", required=True)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--resume", action="store_true", help="跳过已有 evaluation.md 的院系")
    parser.add_argument("--sample", type=int, default=5, help="每院系 LLM 分析样本数")
    parser.add_argument("--parallel", type=int, default=2,
                        help="批内院系并发数（进程池；1=串行；瑞数敏感站建议 2-3）")
    args = parser.parse_args()

    school = args.school
    depts = list_depts(school)
    if not depts:
        print(f"❌ {school}: 无院系数据")
        sys.exit(1)

    todo = [d for d in depts if not (args.resume and is_done(school, d))]
    print(f"🏫 {school}: 共 {len(depts)} 个院系, 待处理 {len(todo)}, "
          f"每批 {args.batch_size} 个")

    # LLM（回顾用）
    from detail_scraper import DetailScraper
    scraper = DetailScraper(school_name=school, dept_name="_回顾",
                            raw_data_root=str(_COLLEGE_ROOT / "tearchers raw data"))
    llm = scraper._llm

    # RSO 收敛状态: 连续零新增批数 / 回顾间隔（批）
    review_streak = 0
    review_interval = 1

    batch_idx = 0
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i:i + args.batch_size]
        batch_idx += 1
        print(f"\n{'#' * 60}")
        print(f"  📦 第 {batch_idx} 批: {'、'.join(batch)}")
        print(f"{'#' * 60}")

        # ── 同域分组: 批内同注册域的院系串行（防同域多流并发），
        #    跨域组之间并行。单校批常为同域 → 实际效果 = 组内串行 ──
        from collections import OrderedDict
        domain_groups: "OrderedDict[str, list[str]]" = OrderedDict()
        for d in batch:
            domain_groups.setdefault(_dept_domain(school, d), []).append(d)

        batch_results = []
        for domain, group_depts in domain_groups.items():
            group_tasks = [(school, d, args.sample) for d in group_depts]
            if len(group_depts) > 1 and domain:
                print(f"  🔗 同域分组 [{domain or '未知域'}]: "
                      f"{'、'.join(group_depts)} → 组内串行（防同域并发）")
            if args.parallel > 1:
                from concurrent.futures import ProcessPoolExecutor
                # map 保序（回顾报告顺序与输入一致）; chunksize=1 保持负载均衡
                workers = min(args.parallel, len(group_depts))
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    batch_results.extend(
                        pool.map(_run_one_dept, group_tasks, chunksize=1))
            else:
                batch_results.extend([_run_one_dept(t) for t in group_tasks])
        for r in batch_results:
            print(f"  {'✅' if r.get('passed') else '❌'} {r.get('dept')}: score={r.get('score')}")

        # ── 批后回顾（收敛判据: 连续 2 批零新增 → 降频每 2 批一次）──
        report_path = ""
        report = ""
        if llm and batch_idx % review_interval == 0:
            # 任务边界: 每批回顾是独立任务, 重新均衡分配 token (批内各系由 run_dept 各自 begin_task)
            llm.begin_task()
            report_path, report = retrospective(school, batch_results, batch_idx, llm)
            new_count = parse_new_issue_count(report)
            review_streak, review_interval = update_review_state(
                review_streak, review_interval, new_count)
            print(f"  🔍 新增问题数: {new_count} | 收敛streak: {review_streak} | "
                  f"回顾间隔: {review_interval} 批")
            # M 半自动化: 结构化建议归档（规则/提示词级自动待办, 代码级人工）
            _append_suggestions(batch_idx, extract_suggestions(report))
        elif llm:
            print(f"  🔎 回顾降频中（连续零新增, 间隔 {review_interval} 批），跳过本批回顾")
        else:
            print("  ⚠️ LLM 不可用，跳过回顾")

        # 汇总改进记录
        passed = sum(1 for r in batch_results if r.get("passed"))
        note = (f"第 {batch_idx} 批({', '.join(r['dept'] for r in batch_results)}): "
                f"{passed}/{len(batch_results)} 通过, 回顾报告: {report_path or '无'}")
        append_improvements(note)
        print(f"  📌 {note}")

        # ── 批后自动回补: RESUME=1 重跑一遍（ok/no_url 秒级跳过，
        #    failed/pending 自动重试；AUTO_BACKFILL=0 关闭）──
        if os.environ.get("AUTO_BACKFILL", "1") == "1":
            from spider_gen import run_spider_script, SpiderStalled
            for dept in batch:
                d_dir = _THIS_DIR / school / dept
                if not (d_dir / "spider.py").exists():
                    continue
                os.environ["RESUME"] = "1"
                try:
                    rc = run_spider_script(d_dir)
                    print(f"  ♻️ 回补 pass {dept}: rc={rc}")
                except (subprocess.TimeoutExpired, SpiderStalled) as e:
                    print(f"  ⚠️ 回补 pass {dept} 中断: {type(e).__name__}")
                finally:
                    os.environ.pop("RESUME", None)

    print(f"\n🎉 {school} 全部 {batch_idx} 批完成")


if __name__ == "__main__":
    main()
