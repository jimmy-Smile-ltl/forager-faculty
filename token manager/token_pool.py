#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态 Token 池管理器（oneapi 网关）

背景: 每人一个 SK, 每个 token 每月 1500 额度, 月初自动刷新, 无法查精确剩余额度。
只能通过发一次最小请求探测: 返回 insufficient_user_quota("当月额度已用完") 即无额度, 200 即有额度。

功能:
    - 动态增删 token (add / remove / import 从 xlsx 导入)
    - 探测额度 (check, 状态带缓存, 多进程共享)
    - 返回有额度的 token 列表 / 取出一个可用 token (list / available / get)
    - 用量统计: 每次调用成功从响应 usage 字段记账, 查看每个 token 用量 (usage)
    - complete(): 发请求, 遇额度用完自动换下一个可用 token 重试 (供采集脚本直接调用)

存储后端:
    - 默认 Redis (redis://localhost:6379/0): 多进程/多机共享; 轮询分配原子化 (INCR)
    - **月初自动刷新由 Redis TTL 天然实现**: 探测到"无余额"时给状态 key 设置
      EXPIREAT(下月1日0点), 月初 key 自动消失 -> 状态回退 unknown -> 下次访问自动重新探测,
      应用代码完全不需要感知日期
    - 传文件路径则用 JSON 文件后端 (单机 fallback): --store tokens.json

CLI 用法:
    uv run python token_pool.py import <xlsx>     # 从 xlsx 导入
    uv run python token_pool.py add <姓名> <sk>   # 添加 token
    uv run python token_pool.py remove <姓名|sk>  # 删除
    uv run python token_pool.py list              # 列表 (含缓存状态)
    uv run python token_pool.py check             # 全量探测 (强制刷新)
    uv run python token_pool.py available         # 有额度的 token (脱敏)
    uv run python token_pool.py get               # 取出一个可用 token (脱敏)
    uv run python token_pool.py usage             # 每个 token 用量 (--sort/--top/--json)
    # 所有命令加 --store <redis://... 或 文件路径> 切换后端

库用法:
    from token_pool import TokenPool
    pool = TokenPool()                            # 默认 Redis
    pool = TokenPool("tokens.json")               # JSON fallback
    pool.probe_all()                              # 先探测一遍
    tok = pool.get()                              # {'name': 'key-1', 'sk': 'sk-...'}
    content, name = pool.complete([{"role": "user", "content": "你好"}])  # 自动换 key 重试

密钥存于 Redis 或 tokens.json (所在文件夹已 gitignore); 所有控制台输出均脱敏。
"""

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import check_sk_balance as csk  # 复用探测/错误分类/脱敏逻辑
from check_sk_balance import (
    S_INVALID_KEY,
    S_NET_FAIL,
    S_NO_QUOTA,
    S_OK,
    S_OTHER_ERR,
    classify_error,
    mask_sk,
)

# 内部状态码 -> 中文显示
STATUS_LABEL = {
    "ok": "有余额",
    "no_quota": "无余额",
    "invalid": "密钥无效",
    "net_fail": "请求失败",
    "other": "其他错误",
    "unknown": "未知",
}
# check_sk_balance 探测结果 -> 内部状态码
STATUS_MAP = {
    S_OK: "ok",
    S_NO_QUOTA: "no_quota",
    S_INVALID_KEY: "invalid",
    S_NET_FAIL: "net_fail",
    S_OTHER_ERR: "other",
}

# 2026-10-15 网关崩溃事故根修: 探测模型写死单一模型名, 平台崩溃期间
# 所有探测超时 → 13个ok全刷成net_fail → 批跑器误判"token全灭"。探测只验
# key死活, 模型崩溃≠key死 — 探测模型跟随环境变量(.env联动), 主模型切换
# 探测自动跟上, 不再写死单一模型。
# 2026-10-15: 探测进程(uv run python token_pool.py check)不经过 llm_client,
# 自己加载 .env — 否则 TOKEN_PROBE_MODEL/LLM_MODEL 传不进探测链(写死模型事故复发)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

DEFAULT_MODEL = (os.environ.get("TOKEN_PROBE_MODEL")
                 or os.environ.get("LLM_MODEL")
                 or "")
DEFAULT_STORE = "redis://localhost:6379/0"
INVALID_TTL = 86400       # 密钥无效: 不频繁重探

# ── 日志: 只输出脱敏信息 (mask_sk), 不含完整 key / 姓名 / 请求与响应内容 ──
log = logging.getLogger("token_pool")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%m-%d %H:%M:%S")
    _h = logging.StreamHandler()
    _h.setFormatter(_fmt)
    log.addHandler(_h)
    try:
        _f = RotatingFileHandler(
            str(Path(__file__).resolve().parent / "token_pool.log"),
            maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _f.setFormatter(_fmt)
        log.addHandler(_f)
    except OSError:
        pass


class QuotaExhaustedError(RuntimeError):
    """池中所有 token 均不可用"""

    def __init__(self, no_quota_names=(), invalid_names=()):
        parts = []
        if no_quota_names:
            parts.append("无余额: " + ", ".join(no_quota_names))
        if invalid_names:
            parts.append("密钥无效: " + ", ".join(invalid_names))
        msg = "所有 token 均不可用"
        if parts:
            msg += " (" + "; ".join(parts) + ")"
        super().__init__(msg)


# =====================================================================
# 存储后端: JsonBackend (文件) / RedisBackend (Redis)
# 统一契约:
#   snapshot()      -> [{name, sk, status, detail, last_checked}]
#                     status 只含"新鲜"状态, 过期/缺失 -> "unknown" (由后端判定)
#   add/remove/update_name / set_status(name, code, detail)
#   apply_status({sk: (探测状态label, detail)})   写回探测结果
#   alloc_index(n)  -> 轮询分配下标 (跨进程原子)
#   claim_probe(name) -> bool  抢占探测权 (防多进程重复探测)
#   release_probe(name)
# =====================================================================


class JsonBackend:
    """JSON 文件后端 (单机 fallback)"""

    def __init__(self, path, cache_ttl=300):
        self.store_path = Path(path)
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._rr = 0
        self._tokens = []
        self._load()

    def _load(self):
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        self._tokens = [
            {"name": str(t.get("name", "")), "sk": str(t.get("sk", "")),
             "status": t.get("status", "unknown"),
             "last_checked": t.get("last_checked", ""), "detail": t.get("detail", ""),
             "calls": int(t.get("calls", 0)),
             "in_tokens": int(t.get("in_tokens", 0)),
             "out_tokens": int(t.get("out_tokens", 0))}
            for t in data.get("tokens", []) if t.get("sk")
        ]
        try:
            self._rr = int(data.get("rr", 0))
        except (TypeError, ValueError):
            self._rr = 0

    def _save_locked(self):
        data = {"tokens": self._tokens, "rr": self._rr,
                "updated_at": datetime.now().isoformat(timespec="seconds")}
        # 2026-09-02: 临时文件按 PID 隔离——多进程(批量32+下载+提取)共写同一
        # tokens.tmp 会互相覆盖/抢 replace, 报 FileNotFoundError(镜像写入失败)
        tmp = self.store_path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.replace(self.store_path)  # 原子替换, 避免写一半损坏
        finally:
            tmp.unlink(missing_ok=True)

    def _is_stale(self, t):
        """是否过期需要重新探测 (Json 后端的时间戳判定; Redis 后端由 TTL 判定)"""
        if not t.get("last_checked"):
            return True
        try:
            ts = datetime.fromisoformat(t["last_checked"])
        except ValueError:
            return True
        now = datetime.now()
        if t["status"] == "ok":
            return (now - ts) > timedelta(seconds=self.cache_ttl)
        if t["status"] == "no_quota":
            # 月初额度自动刷新: 上个月(及更早)探测出的"无余额"视为过期
            return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0) < \
                now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (now - ts) > timedelta(seconds=self.cache_ttl)

    # ---------- 契约实现 ----------

    def snapshot(self):
        with self._lock:
            out = []
            for t in self._tokens:
                d = dict(t)
                if self._is_stale(d):
                    d["status"] = "unknown"
                out.append(d)
            return out

    def add(self, name, sk):
        with self._lock:
            if any(t["sk"] == sk for t in self._tokens):
                return "该 sk 已存在"
            self._tokens.append({"name": name, "sk": sk, "status": "unknown",
                                 "last_checked": "", "detail": "",
                                 "calls": 0, "in_tokens": 0, "out_tokens": 0})
            self._save_locked()
        return None

    def update_name(self, name, sk):
        with self._lock:
            for t in self._tokens:
                if t["sk"] == sk and t["name"] != name:
                    t["name"] = name
                    self._save_locked()
                    return True
        return False

    def remove(self, name_or_sk):
        with self._lock:
            for i, t in enumerate(self._tokens):
                if t["name"] == name_or_sk or t["sk"] == name_or_sk:
                    removed = self._tokens.pop(i)
                    self._save_locked()  # 持久化删除
                    return removed
        return None

    def apply_status(self, results):
        """results: {sk: (探测状态label, detail)}"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for t in self._tokens:
                if t["sk"] in results:
                    label, detail = results[t["sk"]]
                    t["status"] = STATUS_MAP.get(label, "other")
                    t["detail"] = detail
                    t["last_checked"] = now
            self._save_locked()

    def set_status(self, name, code, detail):
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for t in self._tokens:
                if t["name"] == name:
                    t["status"] = code
                    t["detail"] = detail
                    t["last_checked"] = now
                    self._save_locked()
                    return True
        return False

    def record_usage(self, name, in_tokens, out_tokens):
        """累计该 token 的调用次数与输入/输出 tokens (负载均衡依据)"""
        with self._lock:
            for t in self._tokens:
                if t["name"] == name:
                    t["calls"] = t.get("calls", 0) + 1
                    t["in_tokens"] = t.get("in_tokens", 0) + int(in_tokens or 0)
                    t["out_tokens"] = t.get("out_tokens", 0) + int(out_tokens or 0)
                    self._save_locked()
                    return True
        return False

    def alloc_index(self, n):
        with self._lock:
            idx = self._rr % n
            self._rr += 1
            self._save_locked()  # 持久化轮询指针, 跨进程/CLI 调用也轮换
            return idx

    def claim_probe(self, name):
        return True  # 单机文件模式无跨进程竞争

    def release_probe(self, name):
        pass


class RedisBackend:
    """Redis 后端 (默认): 多进程/多机共享, TTL 自动管理状态新鲜度"""

    PREFIX = "tp:"

    def __init__(self, url, cache_ttl=300):
        try:
            import redis
        except ImportError as e:
            raise RuntimeError("未安装 redis-py, 请先 uv add redis 或 uv run --with redis") from e
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.r.ping()  # 连接失败在此抛出
        self.cache_ttl = cache_ttl

    def _k(self, kind, name=None):
        return f"{self.PREFIX}{kind}" if name is None else f"{self.PREFIX}{kind}:{name}"

    def _ttl_for(self, code):
        """状态 -> 缓存 TTL 秒数; no_quota 直接到期下月 1 日 0 点 (月初自动刷新)"""
        if code == "no_quota":
            return self._seconds_until_next_month()
        if code == "invalid":
            return INVALID_TTL
        return self.cache_ttl  # ok / net_fail / other

    def _seconds_until_next_month(self):
        now = datetime.now()
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        next_month_ts = int(datetime(y, m, 1).timestamp())  # 本地时区, 月初 0 点
        return max(next_month_ts - int(time.time()), 1)

    # ---------- 契约实现 ----------

    def snapshot(self):
        tokens = self.r.hgetall(self._k("tokens"))  # name -> sk
        if not tokens:
            return []
        pipe = self.r.pipeline()
        for name in tokens:
            pipe.get(self._k("status", name))
            pipe.ttl(self._k("status", name))
            pipe.get(self._k("detail", name))
            pipe.hgetall(self._k("used", name))  # {calls, in_tokens, out_tokens}
        vals = pipe.execute()  # 每 token 4 个值, 与 tokens 顺序一致
        out = []
        it = iter(vals)
        for name, sk in tokens.items():
            code, ttl, detail, used = next(it), next(it), next(it), next(it)
            out.append({
                "name": name, "sk": sk,
                "status": code or "unknown",          # key 不存在 = 状态过期 = 待探测
                "detail": detail or "",
                "last_checked": f"ttl {ttl}s" if ttl >= 0 else "",
                "calls": int(used.get("calls", 0)),
                "in_tokens": int(used.get("in_tokens", 0)),
                "out_tokens": int(used.get("out_tokens", 0)),
            })
        return out

    def add(self, name, sk):
        key = self._k("tokens")
        tokens = self.r.hgetall(key)
        if sk in tokens.values():
            return "该 sk 已存在"
        self.r.hset(key, name, sk)
        return None

    def update_name(self, name, sk):
        key = self._k("tokens")
        tokens = self.r.hgetall(key)
        old = next((n for n, s in tokens.items() if s == sk), None)
        if old and old != name:
            self.r.hset(key, name, sk)
            self.r.hdel(key, old)
            return True
        return False

    def remove(self, name_or_sk):
        key = self._k("tokens")
        tokens = self.r.hgetall(key)
        name = next((n for n, s in tokens.items() if n == name_or_sk or s == name_or_sk), None)
        if not name:
            return None
        pipe = self.r.pipeline()
        pipe.hdel(key, name)
        pipe.delete(self._k("status", name), self._k("detail", name), self._k("probing", name))
        pipe.execute()
        return {"name": name, "sk": tokens[name]}

    def apply_status(self, results):
        """results: {sk: (探测状态label, detail)}; 状态带 TTL 写入"""
        tokens = self.r.hgetall(self._k("tokens"))
        name_by_sk = {s: n for n, s in tokens.items()}
        pipe = self.r.pipeline()
        for sk, (label, detail) in results.items():
            name = name_by_sk.get(sk)
            if not name:
                continue
            code = STATUS_MAP.get(label, "other")
            st_key, dt_key = self._k("status", name), self._k("detail", name)
            ttl = self._ttl_for(code)
            if ttl > 0:
                pipe.set(st_key, code, ex=ttl)
                pipe.set(dt_key, detail, ex=ttl)
            else:
                pipe.delete(st_key, dt_key)
        pipe.execute()

    def set_status(self, name, code, detail):
        st_key, dt_key = self._k("status", name), self._k("detail", name)
        ttl = self._ttl_for(code)
        pipe = self.r.pipeline()
        if ttl > 0:
            pipe.set(st_key, code, ex=ttl)
            pipe.set(dt_key, detail, ex=ttl)
        else:
            pipe.delete(st_key, dt_key)
        pipe.execute()

    def record_usage(self, name, in_tokens, out_tokens):
        """累计用量 (HINCRBY 原子); key 带月初 TTL → 下月 1 日自动清零 (与额度刷新对齐)"""
        key = self._k("used", name)
        self.r.hincrby(key, "calls", 1)
        self.r.hincrby(key, "in_tokens", int(in_tokens or 0))
        self.r.hincrby(key, "out_tokens", int(out_tokens or 0))
        self.r.expireat(key, int(time.time()) + self._seconds_until_next_month())

    def alloc_index(self, n):
        # INCR 首调用返回 1, 减 1 对齐从下标 0 开始 (与 JsonBackend 的 rr 语义一致)
        return (int(self.r.incr(self._k("rr"))) - 1) % n

    def claim_probe(self, name):
        """SET NX EX 30: 同一 token 同时只允许一个进程探测"""
        return bool(self.r.set(self._k("probing", name), "1", nx=True, ex=30))

    def release_probe(self, name):
        self.r.delete(self._k("probing", name))


class MirrorBackend:
    """双写镜像: 主后端(Redis)负责读写, 次后端(本地 JSON)同步所有状态变化。
    次后端写失败仅记 WARNING, 不阻断主操作。"""

    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary

    def _both(self, fn, *args):
        result = getattr(self.primary, fn)(*args)
        try:
            getattr(self.secondary, fn)(*args)
        except Exception as e:
            log.warning("镜像写入失败 (%s): %s", fn, e)
        return result

    # 读只走主后端
    def snapshot(self):
        return self.primary.snapshot()

    def alloc_index(self, n):
        return self.primary.alloc_index(n)

    def claim_probe(self, name):
        return self.primary.claim_probe(name)

    def release_probe(self, name):
        self.primary.release_probe(name)

    # 写双份
    def add(self, name, sk):
        return self._both("add", name, sk)

    def update_name(self, name, sk):
        return self._both("update_name", name, sk)

    def remove(self, name_or_sk):
        return self._both("remove", name_or_sk)

    def apply_status(self, results):
        self._both("apply_status", results)

    def set_status(self, name, code, detail):
        self._both("set_status", name, code, detail)

    def record_usage(self, name, in_tokens, out_tokens):
        self._both("record_usage", name, in_tokens, out_tokens)


def make_backend(store, cache_ttl=300):
    """store 以 redis:// 等开头 -> RedisBackend; 其他 -> JsonBackend"""
    if isinstance(store, str) and store.startswith(("redis://", "rediss://", "unix://")):
        return RedisBackend(store, cache_ttl)
    return JsonBackend(store, cache_ttl)


# =====================================================================
# TokenPool
# =====================================================================


class TokenPool:
    def __init__(self, store=DEFAULT_STORE, model=DEFAULT_MODEL,
                 cache_ttl=300, base_url=None, workers=8, mirror=None):
        self.model = model
        self.cache_ttl = cache_ttl
        self.base_url = (base_url or os.environ.get("ONEPAI_BASE_URL")
                         or os.environ.get("LLM_BASE_URL") or csk.BASE_URL).rstrip("/")
        self.workers = workers
        self.store = store
        backend = make_backend(store, cache_ttl)
        # 默认双写镜像: store 是 redis 时, 自动镜像到 token_pool.py 所在目录的 tokens.json
        if mirror is None and isinstance(store, str) and store.startswith(("redis://", "rediss://", "unix://")):
            mirror = str(Path(__file__).resolve().parent / "tokens.json")
        if mirror and mirror != "none" and mirror != store:
            try:
                backend = MirrorBackend(backend, make_backend(mirror, cache_ttl))
                log.info("镜像模式: %s -> %s", store, mirror)
            except Exception as e:
                log.warning("镜像后端初始化失败, 仅用主后端: %s", e)
        self._backend = backend
        self._probe_lock = threading.Lock()  # 进程内: 防止重复探测同一批 stale token

    # ---------- 增删查 ----------

    def add(self, name, sk):
        """添加 token; 返回 (ok, 消息)"""
        name, sk = name.strip(), sk.strip()
        if not name or not sk:
            return False, "姓名和 sk 不能为空"
        if not sk.startswith("sk-"):
            return False, "sk 格式不正确 (应以 sk- 开头)"
        err = self._backend.add(name, sk)
        return (False, err) if err else (True, f"已添加 {name} ({mask_sk(sk)})")

    def remove(self, name_or_sk):
        """按姓名或 sk 删除; 返回 (ok, 消息)"""
        removed = self._backend.remove(name_or_sk.strip())
        if removed is None:
            return False, f"未找到: {name_or_sk}"
        return True, f"已删除 {removed['name']} ({mask_sk(removed['sk'])})"

    def import_xlsx(self, path):
        """从 xlsx 合并导入 (按 sk 去重); 返回 (added, updated, skipped)"""
        from check_sk_balance import load_tasks
        tasks, skipped = load_tasks(str(path))
        existing = {t["sk"]: t for t in self._backend.snapshot()}
        added = updated = existing_cnt = 0
        for t in tasks:
            if t["sk"] in existing:
                if existing[t["sk"]]["name"] != t["name"]:
                    self._backend.update_name(t["name"], t["sk"])
                    updated += 1
                else:
                    existing_cnt += 1
            else:
                self._backend.add(t["name"], t["sk"])
                added += 1
        return added, updated, len(skipped)

    def list(self):
        """返回 token 列表快照 (状态仅含新鲜缓存; 过期显示 unknown)"""
        return self._backend.snapshot()

    # ---------- 探测 ----------

    def _probe_many(self, tokens):
        """并发探测, 返回 {sk: (状态label, detail)}; 网络调用, 调用方不能持锁"""
        results = {}
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(csk.check_key, t["sk"], self.model): t for t in tokens}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    status, detail, _ = fut.result()
                except Exception as e:  # 探测函数自身异常兜底
                    status, detail = S_NET_FAIL, str(e)[:120]
                results[t["sk"]] = (status, detail)
        return results

    def _probe(self, tokens):
        """抢占探测权 -> 探测 -> 写回 -> 释放; 抢不到锁的说明别的进程正在探测"""
        claimed = [t for t in tokens if self._backend.claim_probe(t["name"])]
        if not claimed:
            return
        try:
            results = self._probe_many(claimed)
            self._backend.apply_status(results)
            for sk, (label, detail) in results.items():
                brief = f" ({detail[:60]})" if detail else ""
                log.info("探测 [%s]: %s%s", mask_sk(sk), label, brief)
        finally:
            for t in claimed:
                self._backend.release_probe(t["name"])

    def _refresh_stale(self):
        """探测状态过期(unknown)的 token"""
        with self._probe_lock:
            snap = self._backend.snapshot()
            stale = [t for t in snap if t["status"] == "unknown"]
            if stale:
                self._probe(stale)

    def probe_all(self, force=False):
        """全量探测; force=False 只探测过期的。返回汇总 {状态码: 数量}"""
        snap = self._backend.snapshot()
        if force:
            targets = snap  # 强制: 全部重探 (会刷新 TTL)
        else:
            targets = [t for t in snap if t["status"] == "unknown"]
        if targets:
            self._probe(targets)
        return self.summary()

    def summary(self):
        counts = {s: 0 for s in STATUS_LABEL}
        for t in self._backend.snapshot():
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

    # ---------- 分配 ----------

    def available(self, refresh=True):
        """返回有余额的 token 列表, 只含 status=ok (no_quota/invalid/unknown 一律不返回)。

        refresh: True(默认)=只补探测无缓存的 token (快, 信任 ≤5min 缓存);
                 "force"=返回前强制全量真实探测 (最严格, 慢, 每个 token 发一次探测请求);
                 False=纯读缓存不探测。
        """
        if refresh == "force":
            self.probe_all(force=True)
            snap = self._backend.snapshot()
        else:
            snap = self._backend.snapshot()
            if refresh:
                stale = [t for t in snap if t["status"] == "unknown"]
                if stale:
                    with self._probe_lock:
                        self._probe(stale)
                    snap = self._backend.snapshot()
        return [t for t in snap if t["status"] == "ok"]

    def get(self, prefer=None, sticky=None):
        """取出一个可用 token {'name', 'sk'}; 全部不可用抛 QuotaExhaustedError

        负载均衡: 最少消耗优先 (累计 in+out tokens 最少的先用),
        消耗相同(含初始全 0)时轮询打破平局, 保证公平。

        prefer: 会话固定 key (同一任务内保持同一 token, 命中网关 prompt 缓存省钱)。
                该 key 仍确认有额度(ok)则直接返回; 缓存过期(unknown)则只重探它一个,
                还有额度继续用; no_quota/invalid 或重探失败则走正常分配。

        sticky: 学院亲和 key (2026-08-23, 配合 begin_task(sticky_key) 使用)。
                稳定 hash 选槽 → 同一学院固定同一 token, 跨批次/重启稳定,
                学院重跑命中上次 prompt 缓存; md5 均匀分散实现负载均衡。
                prefer 优先 (任务内固定 > 学院固定), sticky 次之,
                两者都无 → 最少消耗 + 轮询。

        并发安全: 轮询计数为 Redis INCR 原子操作, 探测有互斥锁 (claim_probe),
        多线程/多进程同时调用不会重复探测或重复分配同一 token。prefer 只是"偏好",
        不保证一定返回该 key (它已无额度时返回其他可用 token), 调用方按返回值为准。
        """
        snap = self._backend.snapshot()
        if prefer:
            hit = next((t for t in snap if t["sk"] == prefer), None)
            if hit and hit["status"] == "ok":
                return {"name": hit["name"], "sk": hit["sk"]}
            if hit and hit["status"] == "unknown":
                # 固定 key 缓存过期: 只重探它, 尽量保持不切换 (保持缓存连续性)
                with self._probe_lock:
                    self._probe([hit])
                snap = self._backend.snapshot()
                hit = next((t for t in snap if t["sk"] == prefer), None)
                if hit and hit["status"] == "ok":
                    return {"name": hit["name"], "sk": hit["sk"]}
        stale = [t for t in snap if t["status"] == "unknown"]
        if stale:
            with self._probe_lock:
                self._probe(stale)
            snap = self._backend.snapshot()
        ok_list = [t for t in snap if t["status"] == "ok"]
        if not ok_list:
            no_quota = [t["name"] for t in snap if t["status"] == "no_quota"]
            invalid = [t["name"] for t in snap if t["status"] == "invalid"]
            log.error("所有 token 均不可用 (无余额 %d, 密钥无效 %d, 其他 %d)",
                      len(no_quota), len(invalid),
                      sum(1 for t in snap if t["status"] not in ("ok", "no_quota", "invalid")))
            raise QuotaExhaustedError(no_quota, invalid)
        if sticky:
            # 学院亲和 (2026-08-23): 稳定 hash 选槽 — 同一学院永远固定同一 token,
            # 跨批次/重启不变 → 学院重跑/改进循环命中上次 prompt 缓存 (缓存按 token
            # 账号隔离, 换 token 即缓存全 miss)。md5 均匀分散 → 10+ token 负载均衡,
            # 配合 begin_task(sticky_key=学院名) 使用。
            # 并发注意: 多个学院 hash 到同一 token 也 OK (负载仍均匀); 槽位 token
            # 恰好不可用(no_quota/invalid 已在 ok_list 过滤)时 fallback 最少消耗轮询。
            import hashlib as _hl
            _idx = int(_hl.md5(sticky.encode("utf-8")).hexdigest()[:8], 16) % len(ok_list)
            _t = ok_list[_idx]
            return {"name": _t["name"], "sk": _t["sk"]}
        used_list = [(t, int(t.get("in_tokens", 0)) + int(t.get("out_tokens", 0)))
                     for t in ok_list]
        min_u = min(u for _, u in used_list)
        cand = [t for t, u in used_list if u == min_u]
        if len(cand) < len(ok_list):
            # 消耗最少的(可能有多个并列) → 轮询打破
            t = cand[self._backend.alloc_index(len(cand)) % len(cand)]
        else:
            t = ok_list[self._backend.alloc_index(len(ok_list))]
        return {"name": t["name"], "sk": t["sk"]}

    def record_usage(self, sk, in_tokens, out_tokens):
        """调用成功后上报用量 (sk → name 记账, 双写镜像); 失败静默不影响主流程"""
        try:
            snap = self._backend.snapshot()
            name = next((t["name"] for t in snap if t["sk"] == sk), None)
            if name:
                self._backend.record_usage(name, in_tokens, out_tokens)
        except Exception:
            pass

    def _mark(self, sk, code, detail):
        """运行时发现状态变化立即写回 (如调用途中额度用完)"""
        snap = self._backend.snapshot()
        name = next((t["name"] for t in snap if t["sk"] == sk), None)
        if name:
            self._backend.set_status(name, code, detail)

    # ---------- 用量统计 ----------

    def usage(self, sort="total", top=None):
        """每个 token 的用量统计 (calls / in_tokens / out_tokens)。

        sort: name|calls|in|out|total (默认 total 降序, 最耗钱的排前面)
        top:  只返回前 N 个 (汇总仍基于全部 token)
        返回 {"tokens": [{name, status, calls, in_tokens, out_tokens, total}], "summary": {...}}
        """
        rows = []
        for t in self._backend.snapshot():
            in_t = int(t.get("in_tokens", 0) or 0)
            out_t = int(t.get("out_tokens", 0) or 0)
            rows.append({
                "name": t["name"],
                "status": t["status"],
                "calls": int(t.get("calls", 0) or 0),
                "in_tokens": in_t,
                "out_tokens": out_t,
                "total": in_t + out_t,
            })

        summary = {
            "tokens": len(rows),
            "used_tokens": sum(1 for r in rows if r["total"] > 0),
            "calls": sum(r["calls"] for r in rows),
            "in_tokens": sum(r["in_tokens"] for r in rows),
            "out_tokens": sum(r["out_tokens"] for r in rows),
            "total": sum(r["total"] for r in rows),
        }
        used = [r for r in rows if r["total"] > 0]
        if used:
            m = max(used, key=lambda r: r["total"])
            summary["max"] = {"name": m["name"], "total": m["total"]}
            summary["avg_total"] = summary["total"] / len(used)
            summary["avg_calls"] = summary["calls"] / len(used)
        else:
            summary["max"] = None
            summary["avg_total"] = 0.0
            summary["avg_calls"] = 0.0

        order = {
            "name": lambda r: r["name"],
            "calls": lambda r: -r["calls"],
            "in": lambda r: -r["in_tokens"],
            "out": lambda r: -r["out_tokens"],
            "total": lambda r: -r["total"],
        }
        rows.sort(key=order.get(sort, order["total"]))
        if top:
            rows = rows[:top]
        return {"tokens": rows, "summary": summary}

    # ---------- 调用 ----------

    def _call(self, sk, messages, model):
        """用指定 sk 发一次真实 chat 请求, 返回 (状态, 内容, 详情, usage); 网络错误重试 1 次"""
        payload = json.dumps({"model": model, "messages": messages}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {sk}"}
        url = f"{self.base_url}/chat/completions"
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                usage = body.get("usage", {}) or {}
                return S_OK, body["choices"][0]["message"]["content"], "", {
                    "in": usage.get("prompt_tokens", 0),
                    "out": usage.get("completion_tokens", 0),
                }
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                status, detail = classify_error(e.code, body)
                return status, "", detail, {}
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return S_NET_FAIL, "", str(e)[:120], {}
        return S_NET_FAIL, "", "unreachable", {}  # 不会走到

    def complete(self, messages, model=None, max_failover=None):
        """用池中可用 token 发一次 chat 请求, 遇额度用完/密钥无效自动换下一个 token 重试。
        返回 (content, token_name)。所有 token 都不行抛 QuotaExhaustedError;
        非额度类错误(其他错误)直接抛出 RuntimeError。"""
        model = model or self.model
        max_failover = max_failover or max(len(self.list()), 1)
        for _ in range(max_failover):
            tok = self.get()
            status, content, detail, usage = self._call(tok["sk"], messages, model)
            if status == S_OK:
                if usage:
                    self.record_usage(tok["sk"], usage.get("in", 0), usage.get("out", 0))
                return content, tok["name"]
            if status == S_NO_QUOTA:
                log.warning("调用中额度耗尽 [%s], 已标记无余额并切换下一可用 token", mask_sk(tok["sk"]))
                self._mark(tok["sk"], "no_quota", detail)
                continue  # 换下一个 token
            if status == S_INVALID_KEY:
                log.warning("密钥无效 [%s], 已标记并切换下一可用 token", mask_sk(tok["sk"]))
                self._mark(tok["sk"], "invalid", detail)
                continue
            raise RuntimeError(f"LLM 调用失败 [{tok['name']}]: {detail or status}")
        raise QuotaExhaustedError(
            [t["name"] for t in self.list() if t["status"] == "no_quota"],
            [t["name"] for t in self.list() if t["status"] == "invalid"],
        )


# =====================================================================
# CLI
# =====================================================================


def _print_table(pool):
    tokens = pool.list()
    print(f"{'姓名':<6} {'sk密钥(脱敏)':<28} {'状态':<10} {'用量(calls/in/out)':<20} {'上次探测/TTL':<18} 详情")
    for t in tokens:
        label = STATUS_LABEL.get(t["status"], t["status"])
        used = f"{t.get('calls', 0)}/{t.get('in_tokens', 0)}/{t.get('out_tokens', 0)}"
        print(f"{t['name']:<6} {mask_sk(t['sk']):<28} {label:<10} {used:<20} {t['last_checked']:<18} {t['detail'][:40]}")


def cmd_import(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    added, updated, skipped = pool.import_xlsx(args.xlsx)
    print(f"导入完成: 新增 {added}, 更新姓名 {updated}, 跳过 {skipped} (两列缺数据或格式不正确)")
    print(f"当前池中共 {len(pool.list())} 个 token")


def cmd_add(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    ok, msg = pool.add(args.name, args.sk)
    print(("✓ " if ok else "✗ ") + msg)


def cmd_remove(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    ok, msg = pool.remove(args.name_or_sk)
    print(("✓ " if ok else "✗ ") + msg)


def cmd_list(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    _print_table(pool)


def cmd_check(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    if not pool.list():
        print("池中没有任何 token, 请先 import 或 add")
        return
    counts = pool.probe_all(force=True)
    _print_table(pool)
    toks = pool.list()
    ok_names = [t["name"] for t in toks if t["status"] == "ok"]
    noq_names = [t["name"] for t in toks if t["status"] == "no_quota"]
    print("\n=== 汇总 ===")
    for s, label in STATUS_LABEL.items():
        print(f"{label}: {counts.get(s, 0)}")
    if ok_names:
        print(f"有余额: {', '.join(ok_names)}")
    if noq_names:
        print(f"无余额: {', '.join(noq_names)}  (额度每月初自动刷新, 下月自动恢复)")


def cmd_available(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    toks = pool.available(refresh="force" if args.fresh else True)
    if not toks:
        print("没有可用 token")
        return
    print(f"共 {len(toks)} 个可用 token (仅含最近确认有额度的):")
    for t in toks:
        print(f"  {t['name']:<6} {mask_sk(t['sk'])}")


def cmd_get(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    try:
        tok = pool.get()
    except QuotaExhaustedError as e:
        print(f"✗ {e}")
        return
    print(f"分配 token: {tok['name']}  {mask_sk(tok['sk'])}")


def cmd_usage(args):
    pool = TokenPool(args.store, mirror=args.mirror)
    data = pool.usage(sort=args.sort, top=args.top)
    rows, s = data["tokens"], data["summary"]

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    print("=== 用量统计 (本月累计, 月初自动清零) ===")
    print(f"{'姓名':<6} {'状态':<10} {'calls':>6} {'in_tokens':>11} {'out_tokens':>11} {'合计':>11} {'占比':>7}")
    for r in rows:
        label = STATUS_LABEL.get(r["status"], r["status"])
        pct = f"{r['total'] / s['total'] * 100:.1f}%" if s["total"] else "-"
        print(f"{r['name']:<6} {label:<10} {r['calls']:>6} {r['in_tokens']:>11,} {r['out_tokens']:>11,} {r['total']:>11,} {pct:>7}")
    print("-" * 62)
    print(f"总计: {s['tokens']} 个 token, 有消耗 {s['used_tokens']} 个 | "
          f"calls {s['calls']:,} | in {s['in_tokens']:,} | out {s['out_tokens']:,} | 合计 {s['total']:,}")
    if s["max"]:
        print(f"最多: {s['max']['name']} ({s['max']['total']:,} tokens, {s['avg_calls']:.1f} calls/活跃 token)")
    # 金额估算: .env 配置单价(元/千tokens)才显示, 否则不猜测
    pin, pout = os.environ.get("LLM_PRICE_IN"), os.environ.get("LLM_PRICE_OUT")
    if pin and pout:
        try:
            cost = s["in_tokens"] * float(pin) / 1000 + s["out_tokens"] * float(pout) / 1000
            budget = float(os.environ.get("LLM_BUDGET", "1500"))
            print(f"估算金额: ¥{cost:,.2f} (单价 in={pin} out={pout} 元/千tokens) | 预算 ¥{budget:,.0f} → {cost / budget * 100:.1f}%")
        except ValueError:
            pass


def build_parser():
    p = argparse.ArgumentParser(description="动态 Token 池管理器 (oneapi)")
    p.add_argument("--store", default=DEFAULT_STORE,
                   help=f"存储后端: redis:// 开头用 Redis (默认: {DEFAULT_STORE}), 否则为 JSON 文件路径")
    p.add_argument("--mirror", default=None,
                   help="本地镜像文件路径 (redis 时默认自动镜像到 tokens.json); none=关闭镜像")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("import", help="从 xlsx 导入 token (按 sk 去重合并)")
    sp.add_argument("xlsx", help="xlsx 文件路径")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("add", help="添加 token")
    sp.add_argument("name", help="姓名")
    sp.add_argument("sk", help="sk 密钥")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="删除 token (按姓名或 sk)")
    sp.add_argument("name_or_sk", help="姓名或 sk 密钥")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("list", help="列出所有 token (含缓存状态)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("check", help="全量探测额度 (强制刷新)")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("available", help="返回有额度的 token 列表 (只含 status=ok)")
    sp.add_argument("--fresh", action="store_true",
                    help="返回前强制全量真实探测 (最严格, 每个 token 发一次探测请求, 较慢)")
    sp.set_defaults(func=cmd_available)

    sp = sub.add_parser("get", help="取出一个可用 token (轮询分配)")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("usage", help="查看每个 token 的用量统计 (calls/in/out, 按合计降序)")
    sp.add_argument("--sort", choices=["name", "calls", "in", "out", "total"],
                    default="total", help="排序字段 (默认 total 合计降序)")
    sp.add_argument("--top", type=int, default=None, help="只显示前 N 个 (汇总仍基于全部)")
    sp.add_argument("--json", action="store_true", help="输出 JSON (供脚本消费)")
    sp.set_defaults(func=cmd_usage)
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"[错误] {e}")
        if "redis" in str(e).lower() or isinstance(e, (ConnectionError, OSError)):
            print("  提示: Redis 不可用时可用 --store tokens.json 回退到文件模式")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
