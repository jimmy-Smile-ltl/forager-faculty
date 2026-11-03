#!/usr/bin/env python3
"""路径引导统一入口 — 全项目 import 前先导模块。

职责(2026-08-31 拆分重构新增):
1. sys.path 引导: 把 department teachers 根(本项目)与上级根(college teacher/, util 所在)
   以及 templates/ 统一放到 sys.path 最前。此前有三套写法:
   - fix_loop/evaluate_teachers 顶部只 insert 本目录 + templates, util.* 靠
     templates.spider_template 模块级 insert 的导入副作用才能解析(隐式耦合);
   - collect_teacher 在 _fetch_raw/get_llm 函数内懒 insert 上级根;
   - 生成 spider 各自内联自举(不受影响, 保持原样)。
   现统一为: 任何模块 `from _paths import *` 即获得完整路径环境。
2. dotenv: 在模块级提前加载 college teacher/.env — LLM_TIMEOUT/LLM_SEM 等
   模块级 os.environ 读取必须发生在任何 LLM 模块 import 之前。
3. 路径常量: ROOT/PARENT/OUT/LOGS, 供各模块使用(不重复计算)。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # department teachers/
PARENT = ROOT.parent                             # college teacher/ (util/ 所在)
OUT = ROOT / "output"                            # 学校数据目录
LOGS = ROOT / "logs"                             # 运行态文件(队列/日志/批次报告)

for _p in (str(ROOT), str(PARENT), str(ROOT / "templates")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 显式加载 college teacher/.env(覆盖模式): LLM_TIMEOUT/LLM_SEM 等模块级读取依赖它
try:
    from dotenv import load_dotenv
    load_dotenv(PARENT / ".env", override=True)
except Exception:
    pass
