#!/usr/bin/env python3
"""教师信息采集主控脚本 — 向后兼容入口(2026-08-31 拆分重构)。

真实实现已拆分:
  util/fetch_chain.py      — fetch 四级降级请求链(含 Playwright/CDP 浏览器管理)
  util/llm_gate.py         — LLM 调用与并发控制(信号量/token池/超时重试)
  collect_constants.py     — 采集常量与组织类型关键词
  collect_kb.py            — 知识库(自我进化案例库)
  collect_csv.py           — teachers.csv 净化与姓名清洗
  collect_phases.py        — Phase 1-5 主流程 + collect_dept + CLI

本文件仅 re-export 保持 import 兼容: fix_loop/evaluate_teachers/fix_generic/
batch_test_loop + script/ 下 12 个脚本均从本模块取符号, 拆分后零改动。

用法:
  uv run python collect_teacher.py --school 安徽大学 --dept 大数据与统计学院
"""

from _paths import *  # noqa: F401,F403  先导: sys.path 引导 + .env 加载

from util.fetch_chain import (  # noqa: F401
    fetch, _fetch_raw, REDIRECT_JS_PAT, META_REFRESH_PAT,
)
from util.llm_gate import (  # noqa: F401
    get_llm, llm_ask_sem, llm_ask_with_timeout, parse_json,
    _LLM_SEM, LLM_TIMEOUT, LLM_CONCURRENCY_CAP,
)
from collect_constants import (  # noqa: F401
    FACULTY_KW, ORG_TYPE_KEYWORDS, NO_TEACHER_ORGS, COLLEGE_SCHOOLS,
)
from collect_kb import (  # noqa: F401
    KB_PATH, load_kb, save_kb, record_case, is_no_teacher_org, load_org_keywords,
)
from collect_csv import (  # noqa: F401
    LIST_URL_PAT, NEWS_PATH_PAT, ADMIN_PATH_PAT, NAME_OK_PAT,
    sanitize_teachers_csv, _is_detail_url, _clean_name,
)
from collect_phases import (  # noqa: F401
    find_faculty_page, phase2_generate_spider, _write_csv_from_stdout,
    phase3_run_and_verify, phase35_llm_evaluate, phase4_refine, phase5_output,
    _evaluate_final_output, _page_manifest_check,
    collect_dept, load_depts, main,
)

if __name__ == "__main__":
    main()
