#!/usr/bin/env python3
"""知识库(自我进化案例库 knowledge/teacher_patterns.json) — 2026-08-31 从 collect_teacher 拆出。

load_kb/save_kb/record_case: 案例库读写与沉淀; is_no_teacher_org: 书院/医院等
免采组织判定; load_org_keywords: 按院系类型取优先关键词(知识库可自我进化,
未命中回退内置 ORG_TYPE_KEYWORDS)。
"""
import json, sys
from pathlib import Path

from _paths import ROOT
from collect_constants import FACULTY_KW, NO_TEACHER_ORGS, ORG_TYPE_KEYWORDS

KB_PATH = ROOT / "knowledge" / "teacher_patterns.json"
def load_kb():
    """加载案例知识库"""
    try:
        import json
        return json.load(open(KB_PATH, encoding="utf-8"))
    except:
        return {"page_structure_patterns": {}, "failed_cases": {}}

def save_kb(kb):
    """保存知识库(自我进化)"""
    try:
        import json
        KB_PATH.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    except:
        pass

def record_case(case_name: str, case_data: dict):
    """记录新案例到知识库"""
    kb = load_kb()
    kb.setdefault("failed_cases", {})[case_name] = case_data
    save_kb(kb)


def record_success_case(case_name: str, case_data: dict):
    """2026-10-25 成功案例库: PASS户沉淀(同域后续户改进提示词注入参考) —
    上限300条, 超限按 at 时间戳淘汰最旧(防膨胀)。"""
    try:
        import time as _tm
        kb = load_kb()
        sc = kb.setdefault("success_cases", {})
        sc[case_name] = case_data
        if len(sc) > 300:
            def _t(v):
                try:
                    return _tm.mktime(_tm.strptime(
                        str(v.get("at") or "2000-01-01 00:00:00"),
                        "%Y-%m-%d %H:%M:%S").timetuple())
                except Exception:
                    return 0
            for k in sorted(sc, key=lambda k2: _t(sc[k2]))[:len(sc) - 300]:
                del sc[k]
        save_kb(kb)
    except Exception:
        pass


def is_no_teacher_org(dept_type: str) -> bool:
    """判断该组织类型是否无需采集教师(如书院/医院)"""
    return dept_type in NO_TEACHER_ORGS

def load_org_keywords(dept_type: str) -> list:
    """根据院系类型返回优先关键词。

    优先读 knowledge/teacher_patterns.json 的 organization_keywords(可自我进化,
    record_case 会沉淀新案例), 未命中回退内置 ORG_TYPE_KEYWORDS。
    """
    try:
        kb = load_kb()
        kw = kb.get("organization_keywords", {}).get(dept_type, {}).get("keywords")
        if kw:
            return kw
    except Exception:
        pass
    return ORG_TYPE_KEYWORDS.get(dept_type, FACULTY_KW)

