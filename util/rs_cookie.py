#!/usr/bin/env python3
"""瑞数 Cookie 生成 — 无浏览器内存轻量路径 (2026-08-31 起, 2026-09-02 扩 nodenv)。

两条无浏览器路径(用户指令: node 原生补环境最优先, 浏览器兜底):
  A. nodenv 零依赖手写补环境(2026-09-02 首选):
     curl_cffi(chrome110, HTTP 头 Windows Chrome/138 UA) 抓 412
     → node util/rsenv/nodenv/run_vm.js (纯 Node vm 沙箱, 零 npm 依赖)
     执行瑞数 VM 生成 P-cookie → 组合 O+P 重抓 → 200。
     (VM 内 navigator.userAgent 为 Mac/Chrome131, 对齐 sdenv 实测定案 —
      见 env.js 2026-08-31 注释; UA 一致性细节以 env.js 为准)
     覆盖 lzu/bupt/njnu 等 classic 形态(实测 15/15, 13-14s/轮);
     scu(锁定模板)/njust(meta-embedded) VM 解析不支持 → 交上层 sdenv/浏览器。
  B. sdenv 补环境(2026-08-31, util/rsenv/node_modules): 全站点覆盖,
     curl_cffi 抓 412 → node sdenv jsdom 执行 VM → O+P cookie → 200。

Cookie 持久化与更新策略(nodenv 路径):
  - 按域名存 util/cookies/<host>.nodenv.json (gitignored), TTL 默认 3600s
    (env RS_COOKIE_TTL 可调)
  - 快路径: 缓存 cookie 未过期 → 直接带 cookie 请求(1-2s); 412 即权威失效信号
    → 自动重跑 VM 刷新并落盘; 线程安全(锁) + 原子写(tmp rename)
  - O/P cookie 成对保存(服务端校验 O-P 关联), 刷新时整罐覆盖
依赖: node ≥ 18(零 npm) + curl_cffi。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

_RSENV = Path(__file__).resolve().parent / "rsenv"
_NODENV = _RSENV / "nodenv"
_GEN_JS = _RSENV / "generate_cookie.js"
_STORE = Path(__file__).resolve().parent / "cookies"  # util/cookies (gitignored)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
# nodenv 链 UA 矩阵(以 env.js 2026-08-31 注释为准): HTTP 请求头 UA = Windows
# Chrome/138, VM 内 navigator.userAgent = Mac/Chrome131(对齐 sdenv, VM 编码
# 材料以此为准, 与 HTTP UA 无关)。改 UA 前必读 env.js 注释并实测——历史实测
# 曾因指纹材料分叉 400(ruishuExample 教训)。
_UA_WIN = _UA
_STORE_LOCK = threading.Lock()
_NODENV_OK = None  # 惰性探测缓存: node 可用 + nodenv 文件齐
COOKIE_TTL = float(os.environ.get("RS_COOKIE_TTL", "3600"))
_RS_BLOCK_CODES = (202, 412, 452)

# ── 形态不支持域名跳过账本 (2026-09-02 缺点#5 改进) ──
# nodenv VM 对 scu/njust 类形态跑满 13-16s 仍无 cookie (纯浪费) — 失败后
# 记账, TTL 内该域名直跳 sdenv/浏览器; TTL 过期自动重试 nodenv(升级后自愈)。
_SKIP_STORE = _STORE / "_nodenv_skip.json"
_SKIP_TTL = float(os.environ.get("NODENV_SKIP_TTL", "86400"))


def _host_skipped(host: str) -> bool:
    """该域名是否在 nodenv 跳过账本 TTL 内 (形态不支持, 直跳 sdenv)。"""
    try:
        j = json.load(open(_SKIP_STORE, encoding="utf-8"))
        ts = float(j.get(host, 0))
    except Exception:
        return False
    return time.time() - ts <= _SKIP_TTL


def _mark_skipped(host: str):
    """VM 跑满无 cookie (形态不支持) → 记账本 (原子写, TTL 后自愈)。"""
    try:
        _STORE.mkdir(parents=True, exist_ok=True)
        with _STORE_LOCK:
            try:
                j = json.load(open(_SKIP_STORE, encoding="utf-8"))
            except Exception:
                j = {}
            j[host] = time.time()
            tmp = _SKIP_STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_SKIP_STORE)
    except Exception:
        pass


def sdenv_cookie(url: str, wait_sec: int = 8, timeout: int = 45) -> str:
    """调用 node sdenv 生成完整 cookie 字符串(含 O/P/S); 失败抛异常。"""
    if not _GEN_JS.exists() or not (_RSENV / "node_modules" / "sdenv").exists():
        raise RuntimeError("sdenv 未安装: npm install --prefix util/rsenv")
    env = {**os.environ, "NODE_PATH": str(_RSENV / "node_modules")}
    r = subprocess.run(
        ["node", str(_GEN_JS), f"--url={url}", f"--wait={wait_sec}",
         "--output", str(_RSENV / "output" / "_cookie.txt")],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(_RSENV), env=env)
    cookie = r.stdout.strip()
    if not cookie:
        raise RuntimeError(f"sdenv 未生成 cookie: {r.stderr.strip()[-200:]}")
    return cookie


def fetch_with_sdenv(url: str, wait_sec: int = 8, timeout: int = 45):
    """sdenv cookie + curl_cffi 抓页面, 成功返回文本, 失败返回 None。"""
    try:
        import curl_cffi.requests as cr
        cookie = sdenv_cookie(url, wait_sec, timeout)
        resp = cr.get(url, headers={"User-Agent": _UA}, impersonate="chrome110",
                      timeout=20)
        if resp.status_code == 200 and len(resp.text) > 500:
            return resp.text
        # cookie 无效/过期 → 重试一次(带 sdenv 生成的 cookie 头)
        resp = cr.get(url, headers={"User-Agent": _UA, "Cookie": cookie},
                      impersonate="chrome110", timeout=20)
        if resp.status_code == 200 and len(resp.text) > 500:
            return resp.text
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════
#  nodenv 零依赖手写补环境链式 (2026-09-02 首选, 用户指令)
# ══════════════════════════════════════════════════════════════

def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _nodenv_ready() -> bool:
    global _NODENV_OK
    if _NODENV_OK is None:
        try:
            r = subprocess.run(["node", "--version"], capture_output=True,
                               text=True, timeout=5)
            _NODENV_OK = (r.returncode == 0
                          and (_NODENV / "run_vm.js").exists()
                          and (_NODENV / "env.js").exists())
        except Exception:
            _NODENV_OK = False
    return _NODENV_OK


def nodenv_cookie(html: str, url: str, wait: int = 16, timeout: int = None) -> str:
    """412 挑战页 HTML → node 原生 vm 沙箱执行瑞数 VM → P-cookie 字符串。
    形态不支持(scu/njust)/VM 未产出 → 返回 ""(上层降级)。"""
    if not html or not _nodenv_ready():
        return ""
    tmp = None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         encoding="utf-8") as f:
            f.write(html)
            tmp = f.name
        res = subprocess.run(
            ["node", "run_vm.js", tmp, url, "--wait", str(wait),
             "--no-instrument"],
            cwd=str(_NODENV), capture_output=True, text=True,
            timeout=(wait + 90) if timeout is None else timeout,
            encoding="utf-8", errors="replace")
        return (res.stdout or "").strip()
    except Exception:
        return ""
    finally:
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass


def _is_rs_signal(text: str, status: int) -> bool:
    return status in _RS_BLOCK_CODES or "$_ts.nsd" in (text or "")[:8000]


def _store_path(host: str) -> Path:
    return _STORE / f"{host}.nodenv.json"


def _load_cached(host: str) -> dict:
    try:
        with _STORE_LOCK:
            j = json.load(open(_store_path(host), encoding="utf-8"))
        if time.time() - float(j.get("at", 0)) > COOKIE_TTL:
            return {}
        return j.get("cookies", {}) or {}
    except Exception:
        return {}


def _save_cached(host: str, cookies: dict):
    if not cookies:
        return
    try:
        _STORE.mkdir(parents=True, exist_ok=True)
        with _STORE_LOCK:
            tmp = _store_path(host).with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"cookies": cookies, "at": time.time()},
                ensure_ascii=False), encoding="utf-8")
            tmp.replace(_store_path(host))
    except Exception:
        pass


def _clear_cached(host: str):
    """2026-09-18 重大艺术学院实锤: 深度页(jxyky/skjd.htm)求解的 cookie
    用于首页时服务端回 404(路径失配) — 清缓存让链重新裸请求→412→重解。"""
    try:
        with _STORE_LOCK:
            _store_path(host).unlink(missing_ok=True)
    except Exception:
        pass


def _chain_get(session, url: str, timeout: int):
    """链内 GET: 仅直连 (2026-09-07 用户指令: 瑞数 cookie 与 IP 强相关,
    隧道代理换出口即失效 — 代理兜底不但无益, 还可能把代理出口的挑战态
    混入直连 cookie 链; 本地 IP 实测更稳: 兰大/南师大 代理412/直连200)。"""
    headers = {"User-Agent": _UA_WIN, "Referer": url}
    try:
        return session.get(url, headers=headers, impersonate="chrome110",
                           timeout=timeout, proxies=None)
    except Exception:
        pass
    return None


def fetch_with_nodenv(url: str, timeout: int = 20, max_rounds: int = 3):
    """node 原生补环境链式请求(2026-09-02 首选瑞数路径)。

    流程: ①缓存 cookie 快路径(1-2s) → ②直接 GET(chrome110+Win138 UA,
    同 UA 可过 TLS 指纹层, 兰大/川大 curl131 400 但本层 200/412)
    → ③412 挑战 → VM 生成 P-cookie → ④带 O+P 重抓 → 200 返回文本并落盘。
    非瑞数拦截(网络/TLS 级)或 VM 不支持形态 → None 交上层 sdenv/浏览器。
    2026-09-18 重大20院系实锤: 该站挑战需 3 轮(412→解→412→解→200),
    旧 max_rounds=2 在第二轮 412 后放弃 → 25/28 误判「形态不支持」;
    3 轮(每轮 VM ~13s CPU)全解通。"""
    if not _nodenv_ready():
        return None
    try:
        import curl_cffi.requests as cr
    except Exception:
        return None
    host = _host_of(url)
    if _host_skipped(host):  # 形态不支持账本(2026-09-02): njust 类 VM 白跑 13-16s
        return None          # → TTL 内直跳 sdenv/浏览器, 过期自动重试 nodenv
    session = cr.Session(impersonate="chrome110")
    _cache = _load_cached(host)
    for k, v in _cache.items():  # ① 快路径
        try:
            session.cookies.set(k, v)
        except Exception:
            pass
    try:
        for _ in range(max(1, max_rounds)):
            resp = _chain_get(session, url, timeout)
            if resp is None:
                return None
            text = resp.text or ""
            # 成功判定只看状态码: 真实 200 页面自带 $_ts.nsd 瑞数刷新脚本
            # (内容特征判挑战会误杀真页面 → 重跑 VM 死循环, bupt 实测教训)
            if resp.status_code == 200 and len(text) > 500:
                _save_cached(host, dict(session.cookies))  # 成功即持久化
                return text
            if resp.status_code == 404 and session.cookies:
                # 2026-09-18 重大艺术学院实锤: 缓存 cookie 路径失配 → 404
                # (深度页 cookie 用于首页) — 清缓存+清会话 cookie, 下一轮
                # 裸请求重新 412 → VM 重解, 不误判「非瑞数拦截」
                _clear_cached(host)
                try:
                    session.cookies.clear()
                except Exception:
                    pass
                continue
            if resp.status_code not in _RS_BLOCK_CODES:
                return None  # 非瑞数拦截(网络/TLS级如兰大 400 空响应) → 交浏览器层
            # 2026-09-20 周末值守修复: 快路径缓存 cookie 污染挑战态 — 带失效
            # 缓存 cookie 的 412 页 VM 解出的 P-cookie 与服务器基准对不上,
            # 3 轮全 412 耗尽返回 None(重大实锤: 无缓存链 3轮 200, 带失效
            # 缓存链 3轮 None)。清缓存+会话 cookie 后同轮立即裸请求重取
            # 纯挑战态, 不消耗轮次。
            if _cache and session.cookies:
                _cache = None
                _clear_cached(host)
                try:
                    session.cookies.clear()
                except Exception:
                    pass
                resp = _chain_get(session, url, timeout)
                if resp is None:
                    return None
                text = resp.text or ""
                if resp.status_code == 200 and len(text) > 500:
                    _save_cached(host, dict(session.cookies))
                    return text
                if resp.status_code not in _RS_BLOCK_CODES:
                    return None
            cookie = nodenv_cookie(text, url)  # ③ VM 生成 P-cookie
            if not cookie:
                _mark_skipped(host)  # 形态不支持(scu/njust) → 记账本, 上层 sdenv/浏览器
                return None
            for p in cookie.split(";"):
                p = p.strip()
                if "=" in p:
                    k, v = p.split("=", 1)
                    session.cookies.set(k.strip(), v.strip())
            _save_cached(host, dict(session.cookies))  # ④ 刷新落盘
    except Exception:
        return None
    return None
