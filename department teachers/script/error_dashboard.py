#!/usr/bin/env python3
"""报错看板 (2026-09-04 用户复盘指令: "没有清晰的报错" → 每日聚合失败原因)。

扫描 output/<学校>/<院系>/ 的终态标记 + test_report.json reason + 失败队列,
按原因分类聚合, 写 logs/error_dashboard.md 并打印控制台摘要(供 30 分钟汇报引用)。

原因桶(顺序即匹配优先级):
  ipblock    IP封禁/站点不可达(TCP直连+代理均不通)
  ruishu     瑞数WAF(412/Empty reply/Connection reset)
  gw         网关波(熔断/波动/人名终审未完成)
  count      数量硬规则(判漏采/数量明显偏少/严谨核实未通过)
  url        详情URL缺失(缺失率>30%/可点击教师链接漏抓)
  crash      spider/提取崩溃(exit/崩溃)
  shell      壳页无师资页(no_faculty_page/无师资页/合法空)
  extfail    3轮提取失败(fail_mark)
  namepoll   非人名混入
  other      其他

用法: uv run python script/error_dashboard.py [--json]
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

OUTFILE = LOGS / "error_dashboard.md"
QUEUE = LOGS / "_fix_loop_queue.json"

# (桶名, 展示名, 匹配正则) — 对 reason/status/marker 文本逐桶匹配, 先命中先归
_BUCKETS = [
    ("probe", "probe疑似误判待人工复核", re.compile(r"疑似误判需人工复核|反证清单|弱信号页")),
    ("ipblock", "IP封禁/站点不可达", re.compile(r"IP级封禁|TCP直连")),
    ("ruishu", "瑞数WAF掐断", re.compile(r"瑞数|412|Empty reply|Connection reset|连接被掐断")),
    ("gw", "LLM网关波", re.compile(r"熔断|波动|网关不可用|人名终审未完成")),
    ("count", "数量硬规则(漏采)", re.compile(r"数量硬规则|判漏采|数量明显偏少|严谨核实未通过|CSV/JSON不一致")),
    ("url", "详情URL缺失", re.compile(r"详情URL缺失|URL缺失率|可点击教师链接|详情URL全空")),
    ("crash", "spider/提取崩溃", re.compile(r"崩溃|exit \d+|exit\(\d+\)")),
    ("shell", "壳页/无师资页", re.compile(r"no_faculty|无师资页|合法空|LEGAL_EMPTY")),
    ("extfail", "3轮提取失败", re.compile(r"提取失败|提取改进未通过")),
    ("namepoll", "非人名污染", re.compile(r"非人名混入|垃圾词")),
    ("legalempty", "合法空/豁免", re.compile(r"纯文本|未发布教师名录|豁免|无师资板块|无独立师资页|小型组织|源数据无官网URL|deterministic_done|覆盖probe旧误判")),
    ("zombietime", "僵尸超时", re.compile(r"僵尸超时")),
    ("aborted", "批次采集中止", re.compile(r"批次采集中止")),
]
_LABELS = {k: v for k, v, _ in _BUCKETS}
_LABELS["other"] = "其他"


def _classify(text: str) -> str:
    if not text:
        return "other"
    for key, _label, pat in _BUCKETS:
        if pat.search(text):
            return key
    return "other"


def scan():
    """返回 {bucket: [(学校/院系, 摘要), ...]} + 全局计数 + 队列重试统计"""
    buckets = defaultdict(list)
    counters = {"total": 0, "done": 0, "fail": 0, "skip": 0, "zombie": 0,
                "with_report": 0, "no_teachers": 0, "conflict": 0}
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        for d in s.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            dept = f"{s.name}/{d.name}"
            counters["total"] += 1
            # 终态口径与 collection_stats.py 一致: fix_done > fail > skip > zombie
            # 多标记并存 = 矛盾终态(历史修复/复核留下的脏账), 单独计数
            marks = [m for m in (".fix_done", ".fail_mark", ".skip_mark", ".zombie_mark")
                     if (d / m).exists()]
            if len(marks) > 1:
                counters["conflict"] += 1
            reason = ""
            if (d / ".fix_done").exists():
                counters["done"] += 1
            elif (d / ".fail_mark").exists():
                counters["fail"] += 1
                reason = (d / ".fail_mark").read_text(encoding="utf-8",
                                                      errors="ignore").strip()
            elif (d / ".skip_mark").exists():
                counters["skip"] += 1
                reason = (d / ".skip_mark").read_text(encoding="utf-8",
                                                      errors="ignore").strip()
            elif (d / ".zombie_mark").exists():
                counters["zombie"] += 1
                reason = "僵尸超时"
            if reason and (d / ".fix_done").exists():
                reason = ""  # fix_done 优先口径下不再展示次要标记的 reason
            # test_report.json 的 reason 通常比 fail_mark 更具体, 优先
            tr = d / "test_report.json"
            if tr.exists():
                try:
                    rep = json.load(open(tr, encoding="utf-8"))
                    r = (rep.get("reason") or rep.get("recollect_reason")
                         or (rep.get("status") or ""))
                    if r:
                        counters["with_report"] += 1
                        reason = r
                except Exception:
                    pass
            if not (d / "teachers.csv").exists():
                counters["no_teachers"] += 1
            if reason:
                buckets[_classify(reason)].append((dept, reason))
    # 队列重试统计: attempts 分布 → RETRY churn 度量
    retries = {"attempts1": 0, "attempts2_3": 0, "attempts4plus": 0,
               "zombie_in_queue": 0}
    try:
        q = json.load(open(QUEUE, encoding="utf-8"))
        for school, depts in q.items():
            if not isinstance(depts, dict):
                continue
            for dname, info in depts.items():
                a = (info or {}).get("attempts", 0) if isinstance(info, dict) else 0
                if (info or {}).get("zombie") if isinstance(info, dict) else False:
                    retries["zombie_in_queue"] += 1
                if a <= 1:
                    retries["attempts1"] += 1
                elif a <= 3:
                    retries["attempts2_3"] += 1
                else:
                    retries["attempts4plus"] += 1
    except Exception:
        pass
    return buckets, counters, retries


def _md(buckets, counters, retries):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# 报错看板 {now}", "",
             "> 生成: script/error_dashboard.py — 按 test_report.json reason 分类聚合",
             "", "## 全局", "",
             f"- 院系总数 {counters['total']} | done {counters['done']} | fail {counters['fail']}"
             f" | skip {counters['skip']} | zombie {counters['zombie']}",
             f"- **矛盾终态(≥2 终态标记并存) {counters['conflict']}** — 待专项清理",
             f"- 有诊断文本的终态 {counters['with_report']} | 无 teachers.csv 的目录 {counters['no_teachers']}",
             "", "## 失败原因分布", "",
             "| 原因桶 | 院系数 | 占比 | 示例 |", "|---|---|---|---|"]
    total_reasoned = sum(len(v) for v in buckets.values())
    for key, _label, _pat in _BUCKETS:
        items = buckets.get(key, [])
        if not items:
            continue
        pct = 100.0 * len(items) / total_reasoned if total_reasoned else 0
        ex = items[0][1][:44].replace("|", "/")
        lines.append(f"| {_LABELS[key]} | {len(items)} | {pct:.0f}% | {ex} |")
    lines += ["", "## 各桶明细(Top 8)", ""]
    for key, _label, _pat in _BUCKETS:
        items = sorted(buckets.get(key, []), key=lambda x: -len(x[1]))[:8]
        if not items:
            continue
        lines.append(f"### {_LABELS[key]} ({len(buckets.get(key, []))})")
        for dept, r in items:
            lines.append(f"- {dept} — {r[:100]}")
        lines.append("")
    lines += ["## 失败队列重试分布", "",
              f"- attempts=1: {retries['attempts1']} | 2-3: {retries['attempts2_3']}"
              f" | ≥4(僵尸化候选): {retries['attempts4plus']}"
              f" | 队列内僵尸: {retries['zombie_in_queue']}", ""]
    return "\n".join(lines)


def main():
    buckets, counters, retries = scan()
    md = _md(buckets, counters, retries)
    OUTFILE.write_text(md, encoding="utf-8")
    total_reasoned = sum(len(v) for v in buckets.values())
    print(f"error_dashboard.md 已生成 (原因条目 {total_reasoned})")
    top = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:5]
    for key, items in top:
        if items:
            print(f"  {_LABELS[key]}: {len(items)}")
    print(f"  队列重试: attempts≥4 {retries['attempts4plus']} / 队列僵尸 {retries['zombie_in_queue']}")
    if "--json" in sys.argv:
        print(json.dumps({"buckets": {k: len(v) for k, v in buckets.items()},
                          "counters": counters, "retries": retries},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
