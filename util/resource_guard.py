#!/usr/bin/env python3
"""系统资源感知守卫 (2026-09-15 用户指令: 结合CPU资源动态管理, 避免卡死)

背景: 浏览器管理曾多轮优化(独立Chromium/窗口上限/tab回收/孤儿driver/空闲回收),
但都是"问题驱动"——出事故后修单点。缺一层全局资源预算: 真实负载是动态的
(渲染型站集中/瑞数求解/LLM波动), 固定参数要么浪费要么峰值过载。
本模块补上"资源感知"一环 — 三档状态全局可见, 浏览器启动/批循环/保活均查询。

三档:
  green  — 内存压力≤50% 且 CPU≤75%: 全速
  yellow — 内存 50-70% 或 CPU 75-90%: 节流(拒绝新增浏览器, 批循环建议降并发)
  red    — 内存 >70% 或 CPU >90%: 硬停(浏览器一律不启动, 批循环等待恢复)

采样: macOS `memory_pressure -Q`(系统级压力, 含压缩+swap) + psutil CPU%。
缓存 8s 防每请求都采样(采样本身有开销)。swap 极端时 memory_pressure 数值
会反映为高压力(系统开始压缩/换页)。

环境变量:
  RESOURCE_GUARD=0       关闭守卫(恒 green, 回退旧行为)
  RG_MEM_YELLOW/RG_MEM_RED / RG_CPU_YELLOW/RG_CPU_RED  阈值覆盖(百分比)
"""
import os
import subprocess
import threading
import time

_ENABLED = os.environ.get("RESOURCE_GUARD", "1") not in ("0", "off", "false")
_MEM_YELLOW = float(os.environ.get("RG_MEM_YELLOW", "50"))
_MEM_RED = float(os.environ.get("RG_MEM_RED", "70"))
_CPU_YELLOW = float(os.environ.get("RG_CPU_YELLOW", "75"))
_CPU_RED = float(os.environ.get("RG_CPU_RED", "90"))
_CACHE_SECS = 8.0

_lock = threading.Lock()
_cache = {"at": 0.0, "mem_pressure": 0.0, "cpu": 0.0, "state": "green"}
_cpu_init = False


def _sample():
    """采样一次 → (mem_pressure%, cpu%)。失败时按 0 处理(不阻断主流程)。"""
    mem = 0.0
    cpu = 0.0
    try:
        r = subprocess.run(["memory_pressure", "-Q"], capture_output=True,
                           text=True, timeout=5)
        for line in r.stdout.splitlines():
            if "free percentage" in line:
                # "System-wide memory free percentage: 55%" → 压力 = 100-free
                import re as _re
                m = _re.search(r"(\d+)%", line)
                if m:
                    mem = max(0.0, 100.0 - float(m.group(1)))
                break
    except Exception:
        pass
    try:
        import psutil
        global _cpu_init
        if not _cpu_init:
            psutil.cpu_percent(None)  # 建基准, 首调用返回0
            _cpu_init = True
            cpu = 0.0
        else:
            cpu = psutil.cpu_percent(None)
    except Exception:
        pass
    return mem, cpu


def state(force=False):
    """三档状态, 8s 缓存。RESOURCE_GUARD=0 时恒 green。"""
    if not _ENABLED:
        return "green"
    global _cache
    now = time.time()
    if force or now - _cache["at"] > _CACHE_SECS:
        mem, cpu = _sample()
        st = "green"
        if mem > _MEM_RED or cpu > _CPU_RED:
            st = "red"
        elif mem > _MEM_YELLOW or cpu > _CPU_YELLOW:
            st = "yellow"
        with _lock:
            _cache.update(at=now, mem_pressure=mem, cpu=cpu, state=st)
    return _cache["state"]


def snapshot():
    """最近一次采样值 {mem_pressure, cpu, state} (诊断/汇报用)"""
    state()
    with _lock:
        return dict(_cache)


def allow_browser():
    """浏览器启动守卫: green 允许; yellow/red 拒绝新增(渲染降级纯CPU, 不卡机)"""
    st = state()
    if st != "green":
        return False
    return True


def batch_guard():
    """批循环守卫: 返回 (可继续?, 建议动作)。
    green → (True, ''); yellow → (True, '降并发'); red → (False, '等待资源恢复')"""
    st = state()
    if st == "green":
        return True, ""
    if st == "yellow":
        return True, "yellow: 建议降并发/暂停新浏览器"
    return False, "red: 等待资源恢复"


def wait_for_green(timeout=300, step=10):
    """RED 时阻塞等待恢复 (批循环用), 超时返回 False。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if state(force=True) != "red":
            return True
        time.sleep(step)
    return False


if __name__ == "__main__":
    print(snapshot())
