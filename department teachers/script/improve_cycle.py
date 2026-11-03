#!/usr/bin/env python3
"""改进循环执行器 (2026-10-13 harness自我进化框架, 用户指令「按你的意见自我改进」)。

把「测试→归因→修复」的改进环节从 agent 自觉硬化成确定性代码, 批次完成后
自动执行:
  1. 归因: 扫最新批次报告 + 全库 fail_extraction/reopened 院系, 按形态聚类
  2. 匹配: 形态 → logs/_fix_rules.json 规则库 (reason_pattern 正则)
  3. 应用: 命中 active 规则且动作可批处理(delete_spider_regen/requeue_retest)
     → 自动执行(幂等: logs/_fix_ledger.json 记录 院系×规则, 不重复应用)
  4. 账本: 更新规则应用次数; 上轮应用院系的最新终态 → 效果统计
  5. 待分析: 未命中任何规则的形态聚类写入 logs/_pending_forms.json, 供
     agent 分析后新增规则(这是进化入口: 新形态→新规则→新动作)

用法: uv run python script/improve_cycle.py            # dry-run 预览
      uv run python script/improve_cycle.py --apply    # 实际执行
由 auto_keepalive.sh 在每批完成后调用(backfill 之后 select 之前)。
"""
import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

_RULES_F = LOGS / "_fix_rules.json"
_LEDGER_F = LOGS / "_fix_ledger.json"
_PENDING_F = LOGS / "_pending_forms.json"


def _load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def _save_json(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _scan_fail_depts():
    """全库 fail_extraction/reopened 院系 → [(school, dept_dir, status, reason, at)]"""
    out = []
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        for d in s.iterdir():
            tr = d / "test_report.json"
            if not tr.exists():
                continue
            try:
                j = json.load(open(tr, encoding="utf-8"))
            except Exception:
                continue
            if j.get("status") not in ("fail_extraction", "reopened"):
                continue
            out.append((s.name, d, j.get("status"),
                        j.get("reason") or "", j.get("at") or ""))
    return out


def _classify(reason: str) -> str:
    """reason → 形态键(归一化聚类, 未匹配规则时用于待分析队列)"""
    if "机构页URL占比" in reason:
        return "机构页URL占比高"
    if "数量硬规则" in reason:
        return "数量硬规则"
    if "非人名混入" in reason or "人名终审" in reason:
        return "终审非人名"
    if "URL缺失" in reason or "详情URL" in reason:
        return "详情URL缺失"
    if "重复名" in reason:
        return "重复名"
    if "无师资" in reason or "无教师信号" in reason:
        return "无师资/无信号"
    if "不可达" in reason or "封禁" in reason:
        return "不可达/封禁"
    if "瑞数" in reason:
        return "瑞数墙"
    return reason[:30]


def _match_rule(reason: str, rules: list):
    for r in rules:
        if r.get("status") != "active":
            continue
        pat = r.get("reason_pattern", "")
        try:
            if pat and re.search(pat, reason):
                return r
        except re.error:
            pass
    return None


def _apply_action(rule, school, d, apply):
    """执行规则的批处理动作。返回是否执行(幂等由账本保证)。"""
    act = rule.get("动作")
    if act == "delete_spider_regen":
        sp = d / "spider.py"
        if not sp.exists():
            return False  # 已重生过(幂等)
        if apply:
            sp.unlink()
            (d / "spider_meta.json").unlink(missing_ok=True)
            tr = d / "test_report.json"
            try:
                j = json.load(open(tr, encoding="utf-8"))
                j["status"] = "reopened"
                j["reason"] = f"2026-10-13 规则[{rule['id']}]自动应用: " + \
                              (rule.get("动作说明") or "")[:80]
                j["at"] = ""
                json.dump(j, open(tr, "w"), ensure_ascii=False, indent=2)
            except Exception:
                pass
        return True
    if act == "requeue_retest":
        tr = d / "test_report.json"
        try:
            j = json.load(open(tr, encoding="utf-8"))
        except Exception:
            return False
        if j.get("status") == "reopened" and "规则[" in (j.get("reason") or ""):
            return False  # 已入队(幂等)
        if apply:
            j["status"] = "reopened"
            j["reason"] = f"2026-10-13 规则[{rule['id']}]自动应用: " + \
                          (rule.get("动作说明") or "")[:80]
            j["at"] = ""
            json.dump(j, open(tr, "w"), ensure_ascii=False, indent=2)
        return True
    return False  # none 类动作(已合入代码)


def _update_effects(rules, ledger):
    """效果账本: 上轮应用院系的最新终态 → 每规则 PASS/FAIL 计数"""
    applied = ledger.get("applied", {})
    for key, info in applied.items():
        rid, school, dept = info.get("rule"), info.get("school"), info.get("dept")
        d = OUT / school / dept
        tr = d / "test_report.json"
        if not tr.exists():
            continue
        try:
            j = json.load(open(tr, encoding="utf-8"))
        except Exception:
            continue
        st = j.get("status")
        if st in ("collected", "done", "verified_small_center",
                  "verified_pure_text", "deterministic_done"):
            info["outcome"] = "PASS"
        elif st in ("fail_extraction", "fail_ip_blocked", "no_teacher_org",
                    "skip_candidate", "no_faculty_page", "pending_human_analysis"):
            info["outcome"] = "FAIL"
        # reopened/retry 保持 pending
    for r in rules:
        rid = r["id"]
        outs = [i.get("outcome") for i in applied.values() if i.get("rule") == rid]
        if outs:
            n_pass = sum(1 for o in outs if o == "PASS")
            n_done = sum(1 for o in outs if o in ("PASS", "FAIL"))
            r.setdefault("重测PASS", 0)
            r.setdefault("重测已定", 0)
            # 只统计最新已知, 简单覆盖(账本保留全部历史, 规则字段为快照)
            r["重测PASS"] = n_pass
            r["重测已定"] = n_done
    _save_json(_LEDGER_F, ledger)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    rules_data = _load_json(_RULES_F, {"rules": []})
    rules = rules_data.get("rules", [])
    ledger = _load_json(_LEDGER_F, {"applied": {}, "total_applies": 0})
    depts = _scan_fail_depts()
    applied_map = ledger.setdefault("applied", {})

    matched = defaultdict(int)
    acted = Counter()
    pending_forms = Counter()
    for school, d, status, reason, at in depts:
        rule = _match_rule(reason, rules)
        if rule:
            # min_teachers 条件: 规则只对有数据的院系生效(0教师=可能真无师资)
            if rule.get("min_teachers"):
                n = 0
                tc = d / "teachers.csv"
                if tc.exists():
                    try:
                        with open(tc, encoding="utf-8") as f:
                            n = max(0, sum(1 for _ in f) - 1)
                    except Exception:
                        pass
                if n < rule["min_teachers"]:
                    pending_forms[_classify(reason) + "(教师不足条件)"] += 1
                    continue
            matched[rule["id"]] += 1
            key = f"{school}|{d.name}"
            prev = applied_map.get(key)
            if prev and prev.get("rule") == rule["id"]:
                if prev.get("outcome") in ("PASS", "FAIL"):
                    continue  # 已应用且已出结果, 不重复应用(防重烧)
                if prev.get("outcome") == "pending" and "规则[" in reason:
                    continue  # 应用仍在(状态未被覆盖), 等重测结果
                # pending 但状态被批次写回覆盖(reason无规则标记) → 重新应用
            if _apply_action(rule, school, d, args.apply):
                acted[rule["id"]] += 1
                applied_map[key] = {"rule": rule["id"], "school": school,
                                    "dept": d.name, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "outcome": "pending"}
        else:
            pending_forms[_classify(reason)] += 1

    # 账本回写
    ledger["total_applies"] = ledger.get("total_applies", 0) + sum(acted.values())
    for r in rules:
        if acted.get(r["id"]):
            r["应用次数"] = r.get("应用次数", 0) + acted[r["id"]]
    _update_effects(rules, ledger)
    _save_json(_RULES_F, rules_data)
    _save_json(_LEDGER_F, ledger)
    _save_json(_PENDING_F, {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "forms": dict(pending_forms)})

    verb = "已落盘" if args.apply else "dry-run 未落盘"
    print(f"改进循环 ({verb}) | 扫描 {len(depts)} 个失败/重开院系")
    print(f"规则命中: {dict(matched)}")
    if acted:
        print(f"自动应用: {dict(acted)}")
    if pending_forms:
        print(f"待分析形态: {dict(pending_forms.most_common(8))}")
    for r in rules:
        eff = f", 重测PASS {r.get('重测PASS', 0)}/{r.get('重测已定', 0)}" if r.get("重测已定") else ""
        print(f"  [规则] {r['id']}: 应用{r.get('应用次数', 0)}{eff} ({r.get('status')})")


if __name__ == "__main__":
    main()
