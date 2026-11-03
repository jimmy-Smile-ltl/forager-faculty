#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 3 Agent 测试-评估-改进循环 — 小批量渐进式完善 agent 设计。

思路: 测试 → 评估 → 发现缺点 → 改进 → 再测试。
每批只跑**同一学校**的 5-10 个院系（便于横向观察同一 CMS 模板下的表现），
批后 LLM 从「发现/生成/提取/评估/成本」五维度回顾 agent 设计缺点，
改进建议**自动落地**一部分（导航词→NAV_WORDS、页面模式→知识库），
其余（prompt/规则类）沉淀到 logs/_batch_reports/ 报告 + improvements.md，
下一批带着上一批的经验继续 —— 渐进式改进。

用法（cd 到 department teachers/）:
  uv run python batch_test_loop.py 苏州大学 --batch-size 5
  uv run python batch_test_loop.py 苏州大学 --dept 计算机科学与技术学院 电子信息学院
  uv run python batch_test_loop.py 苏州大学 --from-fix-queue      # 从 FIX_QUEUE.json 选该校待修院系
  uv run python batch_test_loop.py 苏州大学 --resume              # 跳过已 PASS(.fix_done)的院系
  uv run python batch_test_loop.py 苏州大学 --limit-batches 2     # 只跑前 N 批
  uv run python batch_test_loop.py 苏州大学 --pause               # 每批后等待回车(人工观察)
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"
sys.path.insert(0, str(BASE))

import fix_loop  # noqa: E402
from evaluate_teachers import evaluate_dept  # noqa: E402
from collect_teacher import get_llm, parse_json, load_kb, save_kb, llm_ask_with_timeout  # noqa: E402

REPORT_DIR = BASE / "logs" / "_batch_reports"
IMPROVE_LOG = BASE / "improvements.md"
NAV_WORDS_FILE = BASE / "templates" / "spider_template.py"


# ═══════════════════════════════════════════════════════════════
#  候选院系列表
# ═══════════════════════════════════════════════════════════════

def list_dept_dirs(school: str) -> list[str]:
    school_dir = OUT / school
    if not school_dir.is_dir():
        return []
    return sorted(
        d.name for d in school_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
        and ((d / "teachers.csv").exists() or (d / "spider.py").exists())
    )


def list_fix_queue_depts(school: str, limit: int = 0) -> list[str]:
    """从 FIX_QUEUE.json 选该校的待修复院系(有 URL 覆盖问题的优先)。"""
    qp = BASE / "logs" / "FIX_QUEUE.json"
    if not qp.exists():
        return []
    try:
        items = json.load(open(qp, encoding="utf-8"))
    except Exception:
        return []
    depts = [i["dept"] for i in items if i.get("school") == school]
    # 按 url_ok 行数升序(问题大的在前) + 总数降序(数据多的优先修)
    depts = sorted(set(depts))
    if limit:
        depts = depts[:limit]
    return depts


def load_dept_urls(school: str) -> dict[str, str]:
    dcsv = OUT / school / "departments.csv"
    if not dcsv.exists():
        return {}
    try:
        return {r["院系名称"]: r.get("院系URL", "").strip()
                for r in csv.DictReader(open(dcsv, encoding="utf-8"))}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
#  批后回顾（LLM 五维度分析 agent 设计缺点）
# ═══════════════════════════════════════════════════════════════

_RETRO_PROMPT = """你是采集 agent 设计评审专家。本批采集了 {school} 的 {n} 个院系（同一学校、同源模板，便于横向对比）。

## 本批评估摘要
{summaries}

## 分析维度
1. **发现策略**: 师资页/分类页发现是否漏或错（无 URL / 栏目当教师）
2. **生成策略**: LLM/确定性生成的 spider 提取逻辑是否脆弱（漏人/多抓/表头漂移）
3. **提取策略**: 姓名/职称/URL 清洗是否出错（粘连/截断/导航词混入）
4. **评估策略**: 评估器维度是否有盲区、误判（该 PASS 判 FAIL 或反之）
5. **成本效率**: 本轮 LLM/网络开销是否有可省项（重复调用、无缓存）

## 输出要求
返回 JSON（只输出纯 JSON）:
{{
  "issues": [{{"dimension": "发现|生成|提取|评估|成本", "problem": "一句话问题", "evidence": "来自哪个院系/什么现象"}}],
  "nav_words": ["本次发现的应过滤导航词(不在现有 NAV_WORDS 中的)"],
  "patterns": [{{"name": "模式名", "structure": "HTML/URL 结构特征", "extract": "提取规则要点"}}],
  "suggestions": [{{"target": "prompt|规则|代码|流程", "action": "具体改进动作"}}]
}}
没有的字段给空数组。"""


def retrospective(school: str, batch_results: list[dict], llm) -> dict:
    """LLM 回顾本批 → 结构化改进建议。"""
    summaries = []
    for r in batch_results:
        reasons = "; ".join(r.get("reasons", [])[:3]) or "无"
        summaries.append(
            f"- {r['dept']}: {r['status']} | 教师{r.get('total', '?')}人 | "
            f"URL{r.get('url_ok', '?')} | 问题: {reasons[:120]}"
        )
    prompt = _RETRO_PROMPT.format(school=school, n=len(batch_results),
                                  summaries="\n".join(summaries))
    try:
        r = llm_ask_with_timeout("你是采集 agent 设计评审专家。只输出 JSON。", prompt, tag="retro")
    except Exception as e:
        return {"issues": [{"dimension": "成本", "problem": f"LLM 回顾失败: {e}", "evidence": ""}],
                "nav_words": [], "patterns": [], "suggestions": []}
    parsed = parse_json(r) or {}
    return {
        "issues": parsed.get("issues", []),
        "nav_words": parsed.get("nav_words", []),
        "patterns": parsed.get("patterns", []),
        "suggestions": parsed.get("suggestions", []),
    }


def render_report(school: str, batch_idx: int, batch_results: list[dict], retro: dict) -> str:
    L = [f"# 第 {batch_idx} 批测试回顾 — {school}", "",
         f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
         f"- 院系: {'、'.join(r['dept'] for r in batch_results)}", ""]
    L += ["## 本批结果", "", "| 院系 | 状态 | 教师 | URL | 问题 |", "|---|---|---|---|---|"]
    for r in batch_results:
        L.append(f"| {r['dept']} | {r['status']} | {r.get('total', '?')} | "
                 f"{r.get('url_ok', '?')} | {'; '.join(r.get('reasons', [])[:2])} |")
    L += ["", "## Agent 设计缺点", ""]
    for i in retro.get("issues", []):
        L.append(f"- [{i.get('dimension', '?')}] {i.get('problem', '')} "
                 f"({i.get('evidence', '')})")
    if retro.get("nav_words"):
        L += ["", f"## 新增导航词(已自动并入 NAV_WORDS): {retro['nav_words']}"]
    if retro.get("patterns"):
        L += ["", "## 新页面模式(已沉淀知识库):"]
        for p in retro["patterns"]:
            L.append(f"- {p.get('name', '?')}: {p.get('structure', '')} → {p.get('extract', '')}")
    if retro.get("suggestions"):
        L += ["", "## 改进建议(待实施)"]
        for s in retro["suggestions"]:
            L.append(f"- [{s.get('target', '?')}] {s.get('action', '')}")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════
#  改进自动落地
# ═══════════════════════════════════════════════════════════════

def apply_nav_words(words: list[str]) -> int:
    """把新导航词合并进 templates/spider_template.py 的 NAV_WORDS(模块级 update)。"""
    if not words:
        return 0
    src = NAV_WORDS_FILE.read_text(encoding="utf-8")
    # 去重: 已在源码字面量中的词跳过
    new = [w for w in words if w and w not in src]
    if not new:
        return 0
    block = ("\n\n# ── 自动沉淀导航词(batch_test_loop 批后回顾, 勿手改) ──\n"
             f"NAV_WORDS.update({json.dumps(sorted(new), ensure_ascii=False)})\n")
    # 插到文件末尾(if __name__ 块之后)
    src = src.rstrip() + "\n" + block
    NAV_WORDS_FILE.write_text(src, encoding="utf-8")
    return len(new)


def apply_patterns(patterns: list[dict]) -> int:
    """页面模式沉淀进 knowledge/teacher_patterns.json 的 page_structure_patterns。"""
    if not patterns:
        return 0
    kb = load_kb()
    existing = kb.setdefault("page_structure_patterns", {})
    added = 0
    for p in patterns:
        name = (p.get("name") or "").strip()
        if not name or name in existing:
            continue
        existing[name] = {"structure": p.get("structure", ""),
                          "extract": p.get("extract", ""),
                          "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        added += 1
    if added:
        save_kb(kb)
    return added


def append_improvements_log(school: str, batch_idx: int, retro: dict, report_path: str):
    issues = "; ".join(f"{i.get('dimension')}:{i.get('problem')}" for i in retro.get("issues", [])[:3]) or "无"
    line = (f"- [{datetime.now().strftime('%m-%d %H:%M')}] 第{batch_idx}批({school}): "
            f"缺点: {issues[:120]} | 导航词+{len(retro.get('nav_words', []))} "
            f"模式+{len(retro.get('patterns', []))} | 报告: {report_path}\n")
    if not IMPROVE_LOG.exists():
        IMPROVE_LOG.write_text("# Step 3 Agent 改进日志\n\n", encoding="utf-8")
    with open(IMPROVE_LOG, "a", encoding="utf-8") as f:
        f.write(line)


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Step 3 Agent 测试-评估-改进循环(同校小批量)")
    ap.add_argument("school")
    ap.add_argument("--batch-size", type=int, default=5, help="每批院系数(5-10 为宜)")
    ap.add_argument("--dept", nargs="+", help="指定院系(代替自动选择)")
    ap.add_argument("--from-fix-queue", action="store_true", help="从 FIX_QUEUE.json 选该校待修院系")
    ap.add_argument("--resume", action="store_true", help="跳过已 PASS(有 .fix_done)的院系")
    ap.add_argument("--limit-batches", type=int, default=0, help="最多跑 N 批(0=不限制)")
    ap.add_argument("--pause", action="store_true", help="每批后等待回车")
    args = ap.parse_args()

    school = args.school
    dept_urls = load_dept_urls(school)
    if args.dept:
        cands = list(args.dept)
    elif args.from_fix_queue:
        cands = list_fix_queue_depts(school, limit=200)
    else:
        cands = list_dept_dirs(school)
    if not cands:
        print(f"❌ {school}: 无候选院系(检查 departments.csv / teachers.csv / spider.py)")
        sys.exit(1)

    if args.resume:
        before = len(cands)
        cands = [d for d in cands if not (OUT / school / d / ".fix_done").exists()]
        print(f"  --resume: 跳过 {before - len(cands)} 个已 PASS 院系")

    bs = max(1, min(args.batch_size, 10))
    batches = [cands[i:i + bs] for i in range(0, len(cands), bs)]
    if args.limit_batches:
        batches = batches[:args.limit_batches]
    print(f"🏫 {school}: 候选 {len(cands)} 院系, {len(batches)} 批(每批 {bs})")

    llm = get_llm()
    if not llm:
        print("⚠️ LLM 不可用: 批后回顾将跳过(采集循环仍可跑)")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for bi, batch in enumerate(batches, 1):
        print(f"\n{'#' * 60}\n📦 第 {bi} 批: {'、'.join(batch)}\n{'#' * 60}")
        batch_results = []
        for dept in batch:
            d = OUT / school / dept
            url = dept_urls.get(dept, "")
            try:
                status = fix_loop.fix_one(school, d, url)
            except Exception as e:
                status = f"ERROR:{str(e)[:50]}"
            # 重新评估拿详情(LLM 终审有缓存, 成本低)
            ev = {"total": "?", "url_ok": "?", "reasons": []}
            try:
                r = evaluate_dept(school, d, url)
                ev = {"total": r.get("total"), "url_ok": r.get("url_ok"),
                      "reasons": [x for x in r.get("reasons", []) if x]}
            except Exception:
                pass
            batch_results.append({"dept": dept, "status": status, **ev})
            print(f"  {'✅' if status == 'PASS' else '❌' if status == 'FAIL' else '⬜'} {dept}: {status} (教师{ev.get('total', '?')})")

        # ── 批后回顾 + 改进落地 ──
        if llm:
            retro = retrospective(school, batch_results, llm)
            report = render_report(school, bi, batch_results, retro)
        else:
            retro = {"issues": [], "nav_words": [], "patterns": [], "suggestions": []}
            report = render_report(school, bi, batch_results, retro)

        report_path = REPORT_DIR / f"batch_{bi:03d}_{school}.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\n📋 批回顾报告: {report_path}")

        n_nav = apply_nav_words(retro.get("nav_words", []))
        n_pat = apply_patterns(retro.get("patterns", []))
        if n_nav or n_pat:
            print(f"  🔧 自动落地: 导航词 +{n_nav}, 页面模式 +{n_pat}")
        append_improvements_log(school, bi, retro, str(report_path))

        if args.pause and bi < len(batches):
            input("  ⏸ 按回车继续下一批...")

    print(f"\n🎉 {school} 完成 {len(batches)} 批。改进日志: {IMPROVE_LOG}")


if __name__ == "__main__":
    main()
