#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 LLM 加载器 — SimpleLLM 位于 util/llm_client.py

解析顺序:
  1. 本地副本: util/llm_client.py (本项目唯一来源)
  2. 兜底: PAPER_DAILY_ROOT 指定的外部 llm_client.py
     (本地副本缺失时; 一般无需配置)

用法:
    from util.llm import get_llm
    llm = get_llm()   # 失败返回 None
"""
import os
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent / "llm_client.py"


def _find_external():
    """返回外部 llm_client 所在目录或 None (兜底用, PAPER_DAILY_ROOT 指向
    含 core/llm_client.py 或 llm_client.py 的目录)"""
    env = os.environ.get("PAPER_DAILY_ROOT", "").strip()
    if env:
        p = Path(env)
        if (p / "core" / "llm_client.py").exists():
            return p
        if (p / "llm_client.py").exists():
            return p
    return None


def _load_llm():
    """实例化 SimpleLLM, 返回 (llm, 来源路径) 或 (None, None)"""
    # 1. 本地副本 (唯一来源)
    if _LOCAL.exists():
        sys.path.insert(0, str(_LOCAL.parent))
        try:
            from llm_client import SimpleLLM
            return SimpleLLM(), str(_LOCAL)
        except Exception:
            pass
    # 2. 外部兜底 (PAPER_DAILY_ROOT)
    ext = _find_external()
    if ext:
        sys.path.insert(0, str(ext))
        try:
            if (ext / "core" / "llm_client.py").exists():
                from core.llm_client import SimpleLLM
            else:
                from llm_client import SimpleLLM
            return SimpleLLM(), str(ext)
        except Exception:
            pass
    return None, None


_llm = None
_llm_source = None


def get_llm():
    """单例获取 LLM 客户端; 不可用/加载失败返回 None"""
    global _llm, _llm_source
    if _llm is None:
        _llm, _llm_source = _load_llm()
        if _llm is not None and getattr(_llm, "available", False):
            print(f"[llm] SimpleLLM 来源: {_llm_source}")
        elif _llm is not None:
            print(f"[llm] SimpleLLM 加载但不可用: {_llm_source} (检查环境变量 ANTHROPIC_*)")
            _llm = None
        else:
            print("[llm] SimpleLLM 加载失败: util/llm_client.py 不可用")
    return _llm
