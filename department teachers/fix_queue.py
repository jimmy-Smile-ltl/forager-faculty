#!/usr/bin/env python3
"""失败重排队列管理(logs/_fix_loop_queue.json) — 2026-08-31 从 fix_loop 拆出。

跨批次累计尝试上限 MAX_QUEUE_ATTEMPTS: 超过即明确 FAIL 终态(zombie), 不再无限
重试(2026-08-31 用户要求"fail了也需要明确无误的结论, 然后可以结束")。
队列/知识库都是读-改-写 JSON, 并发院系线程必须串行化(_QUEUE_LOCK/_KB_LOCK)。
"""
import json
import threading as _threading
from contextlib import contextmanager
from datetime import datetime

from _paths import LOGS

try:
    import fcntl as _fcntl  # POSIX 文件锁 (macOS/Linux)
except Exception:  # 非 POSIX 环境优雅降级 (锁失效但功能可用)
    _fcntl = None
# ── 失败重排队列(logs/_fix_loop_queue.json) ──
# 结构: {"学校": {"院系": {"url": "...", "at": "...", "attempts": N, "zombie": bool}}}
# 2026-08-31 语义变更(用户指令): 评估→改进3轮未通过即终态 —
#   FAIL(有数据提取失败)→.fail_mark, SKIP(无数据)→.skip_mark, 均不再入队;
#   队列只收 RETRY(崩溃/异常等瞬态失败) + 历史遗留条目, --queue 模式逐一消化,
#   终态出队; 累计 attempts 超 MAX_QUEUE_ATTEMPTS → zombie(明确FAIL终态)。
FAIL_QUEUE_PATH = LOGS / "_fix_loop_queue.json"
# 2026-09-03 用户指令: 跨批次累计尝试上限 8→3 ("3次就够了, 不要一直卡在那里,
# 堵塞其他任务") — 第 4 次起僵尸化(zombie=true), 排名批次跳过, 只留给最终
# 僵尸消化轮一次性收尾。
MAX_QUEUE_ATTEMPTS = 3
TRANSIENT_MAX_ATTEMPTS = 5  # 2026-09-08 瞬态(空reason)重试软上限: 波期不烧
# 3次稳定封顶, 但真死站(永远超时/不可达)在 5 次后进僵尸兜底 — 批次循环
# 15-60min/次, 5 次≈2.5h 出结论(15 次≈7.5h 白磨太久, 用户嫌多下调)


def load_fail_queue() -> dict:
    try:
        return json.load(open(FAIL_QUEUE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_fail_queue(q: dict):
    """原子写 (tmp+rename): 双进程并发写不产生半截 JSON (2026-09-02 竞态修复)。"""
    try:
        tmp = FAIL_QUEUE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(FAIL_QUEUE_PATH)
    except Exception:
        pass


# 队列/知识库都是读-改-写 JSON, 并发院系线程会互相覆盖 — 必须串行化
_QUEUE_LOCK = _threading.Lock()
_KB_LOCK = _threading.Lock()


@contextmanager
def _queue_file_lock():
    """跨进程文件锁 (2026-09-02 竞态修复): 队列 JSON 被 multi_school_batch
    多进程 worker 共享读-改-写, 线程锁只串行化单进程 — 必须文件级串行。
    flock 随进程退出/异常自动释放, 无死锁残留; 非 POSIX 环境降级为空锁。"""
    f = None
    if _fcntl is not None:
        try:
            f = open(FAIL_QUEUE_PATH.with_suffix(".lock"), "a+")
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
        except Exception:
            f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
                f.close()
            except Exception:
                pass


def add_to_queue(school: str, dept: str, url: str, zombie: bool = False, reason: str = None, slow: bool = False):
    """reason: 本轮 RETRY 的稳定原因(波动类瞬态传 None/空, 不计 streak)。
    slow: 2026-09-09 用户指令慢站降权 — 耗时超阈值院系带 slow 标志,
    排名第二遍补队列时排到最后(先易后难, 快的先处理)。
    2026-09-04 无效重试封顶(用户指令): 同一稳定原因连续 3 轮无变化 → 直接
    zombie(明确 FAIL 终态), 不再烧第 4 轮全链路重试。"""
    with _QUEUE_LOCK:
        with _queue_file_lock():
            q = load_fail_queue()
            entry = q.setdefault(school, {}).get(dept, {})
            # 2026-08-31 累计尝试上限: 跨批次循环是"20轮"元凶(每sweep一次×18轮),
            # 超过 MAX_QUEUE_ATTEMPTS 次批次尝试 → 明确 FAIL 终态(zombie),
            # 不再无限重试 — 用户要求"fail了也需要明确无误的结论, 然后可以结束"
            # 2026-09-08 波期烧配额根修(#104落地): 夜批实锤网关波期每 RETRY
            # 都烧一次跨批预算, 一晚 3 次封顶耗光 → 1658 院系 zombie 卡死
            # (排队项被第一遍排除、第二遍被 zombie 排除, 永不可见)。
            # 波动类瞬态(reason 空)也累计 attempts, 但 zombie 阈值用软上限
            # TRANSIENT_MAX_ATTEMPTS(5) — 波期保护保留(2-3 轮连续波可覆盖),
            # 真死站(永远超时/不可达)最终仍进僵尸兜底, 不无限循环(2026-09-08
            # 用户问「会不会一直卡住没结果」: 原"空reason不计attempts"会让纯瞬态
            # 失败院系永不触发任何终态, 属设计漏洞)。
            attempts = int(entry.get("attempts", 0)) + 1
            streak = 1
            if reason:
                if entry.get("reason") == reason:
                    streak = int(entry.get("streak", 0)) + 1
            _zombie_cap = (TRANSIENT_MAX_ATTEMPTS if not reason
                           else MAX_QUEUE_ATTEMPTS)
            q.setdefault(school, {})[dept] = {"url": url, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                              "zombie": zombie or attempts > _zombie_cap
                                              or streak >= 3,
                                              "attempts": attempts,
                                              "reason": reason or entry.get("reason", ""),
                                              "streak": streak,
                                              "slow": bool(slow or entry.get("slow"))}
            save_fail_queue(q)


def remove_from_queue(school: str, dept: str):
    with _QUEUE_LOCK:
        with _queue_file_lock():
            q = load_fail_queue()
            if school in q and dept in q[school]:
                del q[school][dept]
                if not q[school]:
                    del q[school]
                save_fail_queue(q)


def clear_fail_queue():
    with _queue_file_lock():
        save_fail_queue({})


