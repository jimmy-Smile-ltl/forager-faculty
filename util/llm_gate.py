#!/usr/bin/env python3
"""LLM 调用与并发控制层(全项目共享单一配额实例) — 2026-08-31 从 collect_teacher 迁出。

get_llm: 懒加载单例(委托 util.llm.get_llm 共享加载器);
_DynamicSem/_pool_active_count/_init_sem_cap/_sem_refresher: 动态容量信号量
(token 池有效数 × LLM_PER_TOKEN, LLM_SEM 手动固定, 防御上限 LLM_CONCURRENCY_CAP);
_LLM_SEM/_LLM_EX: 进程内唯一实例(fix_loop/evaluate_teachers 复用 — 此前三处各自
Semaphore(4) 叠加成 12 路并发打爆网关);
llm_ask_sem/llm_ask_with_timeout/parse_json: 对外调用入口(超时+退避重试+健壮JSON)。
依赖 util.llm/util.llm_client 均为函数内懒加载(与原实现逐字节一致)。
"""
import datetime, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

# .env 必须先于 LLM_TIMEOUT/LLM_CONCURRENCY_CAP 的模块级 os.environ 读取
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass
# ── LLM ──
_llm = None
def get_llm():
    global _llm
    if _llm is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 统一 util 层在仓库根
            from util.llm import get_llm as _shared_llm  # 共享加载器(env/相对路径/vendor兜底)
            _llm = _shared_llm()
        except Exception:
            _llm = None
    # 不得按 available 过滤: SimpleLLM.available 读线程局部状态(threading.local),
    # 并发 worker 线程首次调用时尚未初始化 → False → 返回None → LLM路径被静默短路
    # (实测批量13连败的根因)。可用性由调用链自身的降级/重试机制处理。
    return _llm


# ── LLM 并发控制(全项目共享单一配额实例) ──
# fix_loop.py / evaluate_teachers.py 均 import 本模块的 _LLM_SEM / llm_ask_with_timeout,
# 保证一个进程内只有一把信号量 — 此前三处各自 Semaphore(4) 会叠加成 12 路并发打爆网关。
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "150"))  # 单次调用超时(秒)
# 并发上限策略 (2026-08-24 按 token 并行, 双层):
#   内层: SimpleLLM 每 token 独立信号量 (LLM_PER_TOKEN, 默认 2) — 同一 token 最多 2 路,
#         15 token × 2 = 30 路总并发, 单 token 远低于 2026-08-23 风暴阈值(单 token 6路).
#   外层: 本模块全局 _DynamicSem 兜底 — LLM_SEM=0/未设置 → 自动 = 池有效数 × LLM_PER_TOKEN
#         (防御上限 LLM_CONCURRENCY_CAP); LLM_SEM=N → 手动固定(实验对照/网关异常降级)。
LLM_CONCURRENCY_CAP = int(os.environ.get("LLM_CONCURRENCY_CAP", "64"))  # 防御性上限


class _DynamicSem:
    """动态容量信号量: 容量跟随 token 池有效数, 后台线程周期性刷新。
    token 池新增/失效(no_quota/invalid)时容量自动调整, 不重启批次。"""

    def __init__(self, cap):
        self._cond = threading.Condition()
        self._cap = max(1, cap)
        self._held = 0

    def set_cap(self, cap):
        with self._cond:
            self._cap = max(1, cap)
            self._cond.notify_all()

    def acquire(self):
        with self._cond:
            while self._held >= self._cap:
                self._cond.wait()
            self._held += 1

    def release(self):
        with self._cond:
            self._held -= 1
            self._cond.notify()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()


def _pool_active_count():
    """探测 token 池有效(ok)数量 — 决定自动并发上限。
    优先读 SimpleLLM 实时池(redis 状态), 兜底读 token manager 镜像 tokens.json。"""
    try:
        llm = get_llm()
        pool = getattr(llm, "_pool", None)
        if pool is not None:
            try:
                return sum(1 for t in pool.list() if t.get("status") == "ok")
            except Exception:
                pass
    except Exception:
        pass
    mirror = Path(__file__).resolve().parent.parent / "token manager" / "tokens.json"
    try:
        d = json.loads(mirror.read_text(encoding="utf-8"))
        return sum(1 for t in d.get("tokens", []) if t.get("status") == "ok")
    except Exception:
        return 0


def _init_sem_cap():
    """返回 (初始容量, 是否自动模式)。LLM_SEM 手动指定则不自动跟随池。"""
    manual = os.environ.get("LLM_SEM")
    if manual is not None and manual.strip() not in ("", "0"):
        return int(manual), False
    n = _pool_active_count()
    try:  # 内层 per-token 并发数(util/llm_client): 自动容量 = 池数 × per-token
        from util.llm_client import LLM_PER_TOKEN as _pt
    except Exception:
        _pt = 1
    total = n * _pt if n else 4
    return max(2, min(total, LLM_CONCURRENCY_CAP)), True


_SEM_CAP, _LLM_SEM_AUTO = _init_sem_cap()
_LLM_SEM = _DynamicSem(_SEM_CAP)


def _sem_refresher():
    """自动模式下每 300s 跟随 token 池容量(池刷新 TTL 也是 300s)。"""
    while True:
        time.sleep(300)
        try:
            n = _pool_active_count()
            try:
                from util.llm_client import LLM_PER_TOKEN as _pt
            except Exception:
                _pt = 1
            total = n * _pt if n else 4
            new = max(2, min(total, LLM_CONCURRENCY_CAP))
            _LLM_SEM.set_cap(new)
            print(f"  🔄 LLM 并发容量刷新: {new} (池有效 {n} × per-token {_pt})", flush=True)
        except Exception as e:
            print(f"  ⚠️ LLM 并发容量刷新失败: {e}", flush=True)


if _LLM_SEM_AUTO:
    threading.Thread(target=_sem_refresher, daemon=True).start()


def _now_str():
    """时间串; datetime 被环境遮蔽(如 PYTHONPATH 里的同名目录)时回退 time。"""
    try:
        return datetime.now().strftime("%H:%M:%S")
    except Exception:
        return time.strftime("%H:%M:%S")


def _llm_wlog(tag, msg):
    """LLM 调用诊断日志(统一入口, 文件级不受 stdout 编码/管道影响)"""
    try:
        with open("/tmp/llm_wrapper.log", "a", encoding="utf-8") as _f:
            _f.write(f"[{_now_str()}] [{tag}] {msg}\n")
    except Exception:
        pass


# ── LLM 波动期熔断器 (2026-09-03 用户指令"按意见再更改完善", 提案②) ──
# 背景: 网关周期性波动(35-50min/轮), 波动期调用在底层挂 300-600s 才超时 —
# worker 全卡死 + 白烧院系预算/队列 attempts + 快速失败又会把 3 轮评估误判成
# 提取失败落 .fail_mark 终态。机制(进程内滑动窗口):
#   - 记录最近调用成败; 样本 ≥ _BRK_MIN_N 且失败率 ≥ _BRK_FAIL_RATE → 打开;
#   - 打开期间非 health 调用快速失败(返回 None, 不占共享池槽); health 探针
#     豁免(它是恢复检测器, 且探针进程与批次进程隔离);
#   - 冷却 cooldown = _BRK_BASE × 2^min(opens,3) (240/480/960/1920s 指数退避,
#     连续打开自动加长); 冷却到期半开: 放行调用重新积累证据(窗口已清空),
#     仍失败则重开(opens++), 成功则自然关闭;
#   - 配合 fix_loop 的 fix_one 入口等待: 波动期新院系不进场→不烧 attempts。
_BRK_MIN_N = 8        # 至少 N 次调用才评估
_BRK_WIN_N = 20       # 只保留最近 N 次
_BRK_FAIL_RATE = 0.6  # 失败率阈值
_BRK_BASE = int(os.environ.get("LLM_BREAKER_BASE", "240"))  # 冷却基数(秒)
_BRK_LOCK = threading.Lock()
_BRK_WIN = []          # 最近调用成败(True=成功)
_BRK_OPEN_SINCE = 0.0  # >0 = 熔断打开
_BRK_OPENS = 0         # 连续打开次数(指数退避)
_LAST_WAVE_FAIL = 0.0  # 最近一次波动签名时刻(快速失败/调用超时), 供 fix_loop 终态分流
_WAVE_FAIL_TS = []     # 300s 内失败时刻序列(区分孤立瞬态与连续波动)


def _mark_wave_fail():
    """波动签名: 300s 窗内连续 ≥2 次失败才算波动 — 单次瞬态超时不构成网关
    波动。2026-10-15 2315 批实锤: Gemini 时代主要失败形态是孤立瞬态
    (1 次 verify 90s 超时 → _LAST_WAVE_FAIL 更新 → 300s 窗内 13 个院系
    「波动签名未消」被跳过 LLM 改进批量 RETRY)。真波动期(glm 型连续
    EMPTY/快失败)第 2 次失败立即标记, 保护不弱化; 熔断打开路径的快速
    失败连环发生, 首标至多延迟一次调用。"""
    global _LAST_WAVE_FAIL
    now = time.time()
    _WAVE_FAIL_TS.append(now)
    while _WAVE_FAIL_TS and now - _WAVE_FAIL_TS[0] > 300:
        _WAVE_FAIL_TS.pop(0)
    if len(_WAVE_FAIL_TS) >= 2:
        _LAST_WAVE_FAIL = now


def last_wave_fail_ts():
    """最近一次熔断快速失败或调用超时的时刻(epoch)。fix_loop 完成时若距此
    <300s 说明该院系运行期遭遇波动, 3 轮结论不可信 → FAIL 转 RETRY
    (完成时熔断可能已半开/关闭, 仅凭 breaker_open() 会漏判 — 2026-09-03
    山大耳鼻喉: 完成时半开, URL缺失100% FAIL 落终态)。"""
    return _LAST_WAVE_FAIL


def _brk_cooldown():
    # opens=1 首次打开 → base; 连续打开指数退避 base/2base/4base/8base
    return _BRK_BASE * (2 ** min(max(_BRK_OPENS - 1, 0), 3))


def _brk_record(ok, weight=1):
    """记录一次调用结果; 打开条件只在关闭态评估(冷却期内不重评)。
    weight: 失败严重度加权 — TIMEOUT 传 3 (服务端挂起整周期, 比 401/空响应
    严重得多; 2026-09-03 实测"长prompt改进全挂+短prompt评估全成"的降级模式
    里, 成功样本稀释失败率致熔断永远打不开, 加权后 4 次超时即可打开)。
    冷却到期: 半开复位(此处而非 breaker_open 里清 _BRK_OPEN_SINCE, 保证
    半开后新调用能从零积累证据 — 2026-09-03 测试实抓: 到期不清零会让
    守卫永久吞证据, 熔断器只能打开一次)。半开后首个成功 = 波动结束,
    退避计数清零。
    两个打开条件(快速崩断 + 整体失败率):
    ① 最近 _BRK_MIN_N 次全失败 = 波动期签名(调用~100%失败), 立即打开;
    ② 窗口失败率 ≥ _BRK_FAIL_RATE = 部分降级(旧成功样本稀释后仍过半失败)。"""
    global _BRK_OPEN_SINCE, _BRK_OPENS
    with _BRK_LOCK:
        if _BRK_OPEN_SINCE:
            if (time.time() - _BRK_OPEN_SINCE) < _brk_cooldown():
                return  # 冷却期内不重评
            _BRK_OPEN_SINCE = 0.0  # 冷却到期: 半开
            if ok:
                _BRK_OPENS = 0  # 半开后首个成功 = 波动结束
        _BRK_WIN.extend([ok] * max(1, weight))
        del _BRK_WIN[:-_BRK_WIN_N]
        if len(_BRK_WIN) < _BRK_MIN_N:
            return
        fails = sum(1 for x in _BRK_WIN if not x)
        last_all_fail = sum(1 for x in _BRK_WIN[-_BRK_MIN_N:] if not x) == _BRK_MIN_N
        if last_all_fail or fails / len(_BRK_WIN) >= _BRK_FAIL_RATE:
            _BRK_OPEN_SINCE = time.time()
            _BRK_OPENS += 1
            _BRK_WIN.clear()  # 半开期重新积累证据
            _llm_wlog("breaker", f"OPEN opens={_BRK_OPENS} "
                                 f"cooldown={_brk_cooldown()}s fails={fails}")


def breaker_open():
    """熔断是否处于打开(冷却)期。llm_ask_sem 快速失败 / fix_loop 入口等待依据。"""
    with _BRK_LOCK:
        if not _BRK_OPEN_SINCE:
            return False
        return (time.time() - _BRK_OPEN_SINCE) < _brk_cooldown()


# 2026-08-29: LLM 调用共享线程池(替代每调用一个 1-worker 池)。
# 旧实现: 每次 llm_ask_sem 新建 ThreadPoolExecutor(1), 超时后 shutdown(wait=False)
# 不等待 → 卡在底层 SSL read 的 worker 线程成为孤儿, 无上限积累(实测单批 50+ 个
# 挂死线程, PySSL_select 快照实锤) → 线程数膨胀+内存爬升。共享池 8 worker 封顶,
# 挂死的调用只占池内槽位(底层 httpx timeout=120 最终兜底), 不再无限泄漏。
_LLM_EX = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-call")


def llm_ask_sem(system, prompt, timeout=LLM_TIMEOUT, tag="gen"):
    """带共享信号量+超时的单次 LLM 调用(collect_teacher 内部路径, 外层已有重试循环)
    2026-09-03 熔断: 打开期间非 health 调用直接返回 None(快速失败不占池槽)。"""
    if tag != "health" and breaker_open():
        _llm_wlog(tag, f"BREAKER_OPEN fast-fail prompt_len={len(prompt)}")
        _mark_wave_fail()  # 波动签名: 完成时终态分流依据(见 last_wave_fail_ts)
        return None
    llm = get_llm()
    if not llm:
        return None
    with _LLM_SEM:  # 并发路径共享 LLM 配额(服务端限流保护)
        fut = _LLM_EX.submit(llm.ask, system, prompt)
        try:
            r = fut.result(timeout=timeout)
        except FuturesTimeout:
            _llm_wlog(tag, f"TIMEOUT({timeout}s) prompt_len={len(prompt)}")
            _mark_wave_fail()  # 挂起整周期 = 波动签名, 同样计入终态分流
            _brk_record(False, weight=3)  # 超时加权: 服务端挂起整周期, 降级模式探测
            return None
        except Exception as e:
            _llm_wlog(tag, f"EXC {type(e).__name__} prompt_len={len(prompt)}")
            _brk_record(False)
            return None
        finally:
            if not fut.done():
                fut.cancel()  # 超时未完成: 取消排队; 已运行线程由共享池+底层120s超时兜底
    if not r or not isinstance(r, str):
        _llm_wlog(tag, f"EMPTY prompt_len={len(prompt)}")
        _brk_record(False)
        return None
    # degraded/error JSON(网关兜底降级)按失败计入熔断窗口, 但仍返回给调用方
    # (llm_ask_with_timeout 等自会判定 bad 并退避重试)。
    _brk_record(not ('"degraded"' in r[:200] or '"error"' in r[:200]))
    _llm_wlog(tag, f"ok len={len(r)} head={r[:60]!r}")
    return r


def llm_ask_with_timeout(system, prompt, timeout=LLM_TIMEOUT, tag="improve"):
    """带信号量+超时+2次退避重试的 LLM 调用(fix_loop 修复循环用)
    注意: 不用 with 块 — ThreadPoolExecutor.shutdown(wait=True) 会等卡死的
    SSL read 永远结束, 必须 shutdown(wait=False) 让守护线程自行消亡
    重试: 网关 token 池轮换时会出现短暂的 401/空响应浪潮(实测1-5分钟),
    空响应或 degraded/error JSON 会按 3s/8s 退避重试(网关空响应常见但恢复快, 短退避即可)"""
    for attempt, backoff in ((1, 3), (2, 8)):
        t0 = time.time()
        r = llm_ask_sem(system, prompt, timeout=timeout, tag=tag)
        elapsed = time.time() - t0
        bad = (not r or '"degraded"' in r[:200] or '"error"' in r[:200])
        _llm_wlog(tag, f"attempt{attempt} len={len(r) if r else 0} bad={bad} "
                        f"elapsed={elapsed:.0f}s head={repr((r or '')[:60])}")
        if not bad:
            return r
        if attempt < 2:
            if elapsed >= timeout * 0.8:
                # 近似 TIMEOUT: 服务端已挂起整个超时周期, 3s 退避重试徒劳
                # (2026-09-03 波动期实测: 跳过 attempt2 把每轮 eval 从 183s 压到 ~93s,
                #  院系预算内多存活一轮, 僵尸形成窗口减半; 正常模式无影响 —
                #  短空响应/401 浪返回极快, 不会触发此分支)
                break
            time.sleep(backoff)
    return None


def parse_json(s):
    """健壮的JSON解析: 先 json.loads, 失败后找平衡括号"""
    if not s: return None
    try: return json.loads(s)
    except: pass
    # 找第一个 { 和最后一个 } 之间的平衡区段
    start = s.find('{')
    if start < 0: return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i+1])
                except:
                    break
    return None
