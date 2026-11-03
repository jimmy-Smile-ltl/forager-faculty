#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""瑞数 iv8 求解器 (2026-09-07 用户指令: iv8 获取 cookie 优先级最高)

背景: iv8 (github.com/HanZzzzz000/iv8) 是 Python 原生 V8 扩展, C++ 层实现
BOM/DOM/CSSOM/加密/Canvas 等浏览器 API, 在 Python 内直接跑依赖 Web 环境的
JS, 无需浏览器。对瑞数反爬: 第一请求拿到挑战页(412/202 + <script src r='m'>),
把挑战页+VM脚本喂给 iv8 的 page.load 全流程执行, document.cookie 即得
P/O cookie — 真实 V8 保真度高于 nodenv(Node vm 补环境), 且纯 Python 扩展
无外部 Node 进程, 快(实测示例秒级)。

与 nodenv/RsSpider 的关系: 本模块是瑞数路径的**第一优先级**; iv8 失败才降级
nodenv → sdenv → 浏览器。不读 _nodenv_skip 账本(那只是 nodenv 形态不支持记录,
iv8 全形态通用)。

直连强制 (2026-09-07 用户指令): 瑞数 cookie 与出口 IP 强相关, 隧道代理换
出口即失效 — 本模块全程 proxies=None。

cookie 持久化: util/cookies/<host>.iv8.json (TTL 与 nodenv 同口径), 快路径
1-2s 复用。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

_IV8_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_RUISHU_NSD = re.compile(r"\$_ts\.nsd\s*=\s*\d+")
_RUISHU_COOKIE = re.compile(r"document\.cookie\s*=")
# 挑战页里的瑞数 VM 脚本: <script src="xxx.js" r='m'> (m=main VM)
_VM_SCRIPT_RE = re.compile(r"""<script[^>]*src=["']([^"']+\.js)["'][^>]*r=["']m["']""", re.I)
_COOKIE_TTL = float(os.environ.get("IV8_COOKIE_TTL", "7200"))  # 2h 复用

_BASE = Path(__file__).resolve().parent.parent
_COOKIE_DIR = Path(__file__).resolve().parent / "cookies"
_UA_OVERRIDE = os.environ.get("IV8_UA", "").strip()


def _store_path(host: str) -> Path:
    return _COOKIE_DIR / f"{host}.iv8.json"


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _load_cookie(host: str):
    p = _store_path(host)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(d.get("at", 0)) < _COOKIE_TTL:
            return d.get("cookies") or {}
    except Exception:
        pass
    return None


def _save_cookie(host: str, cookies: dict):
    try:
        p = _store_path(host)
        p.write_text(json.dumps({"at": time.time(), "cookies": cookies},
                                ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _is_challenge(html: str, status: int) -> bool:
    if status in (202, 412):
        return True
    if status == 200 and len(html) < 500:
        if "<script" in html and _RUISHU_COOKIE.search(html):
            return True
        if _RUISHU_NSD.search(html):
            return True
    return False


def _fetch(url: str, timeout: float, cookies: dict = None) -> tuple:
    """直连请求 — 系统 curl (2026-09-07 实测: 北邮等 TLS 指纹站对 curl_cffi
    静默丢连接挂死, 系统 libcurl 0.3s 拿到 412 挑战页; 且系统 curl 指纹最稳、
    --max-time 可靠)。UA 与 iv8 环境一致; 全程无代理。
    返回 (body, status, headers) — headers 为真实响应头(瑞数 VM 可能读
    Set-Cookie 等, 2026-09-07 iv8 调试发现假头导致 cookie 不出)。"""
    import subprocess
    import tempfile
    try:
        hf = tempfile.NamedTemporaryFile(delete=False, suffix=".hdr")
        hf.close()
        cmd = ["curl", "-sL", "--max-time", str(min(timeout, 20)),
               "-D", hf.name, "-A", _IV8_UA, "-o", "-", "-w", "\n%{http_code}"]
        if cookies:
            cmd += ["-b", "; ".join(f"{k}={v}" for k, v in cookies.items())]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        raw = p.stdout.decode("utf-8", "replace")
        body, _, code = raw.rpartition("\n")
        code = code.strip()
        status = int(code) if code.isdigit() else 0
        headers = {}
        try:
            for line in open(hf.name, encoding="utf-8", errors="ignore"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    if k == "set-cookie":
                        headers.setdefault("set-cookie", []).append(v.strip())
                    else:
                        headers[k] = v.strip()
        except Exception:
            pass
        try:
            os.unlink(hf.name)
        except Exception:
            pass
        return body, status, headers
    except Exception:
        return "", 0, {}


def _solve_with_iv8(challenge_html: str, url: str, timeout: float,
                     challenge_headers: dict = None,
                     seed_cookies: dict = None) -> dict:
    """iv8 全流程: 挑战页 + VM 脚本 → page.load → document.cookie。"""
    import iv8  # noqa: F401  (安装失败时上层捕获 ImportError)

    m = _VM_SCRIPT_RE.search(challenge_html)
    if not m:
        return {}
    js_path = m.group(1)
    js_url = urljoin(url, js_path)
    js_text, _js_status, _ = _fetch(js_url, timeout)
    if not js_text:
        return {}
    challenge_headers = challenge_headers or {}

    _ua = _UA_OVERRIDE or _IV8_UA
    environment = {
        "location": {
            "href": url,
            "origin": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
            "protocol": f"{urlparse(url).scheme}:",
            "host": urlparse(url).netloc,
            "hostname": urlparse(url).hostname or "",
            "port": "",
            "pathname": urlparse(url).path or "/",
            "search": urlparse(url).query and f"?{urlparse(url).query}" or "",
            "hash": "",
        },
        "navigator": {
            "userAgent": _ua,
            "appVersion": ("5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "platform": "MacIntel",
            "languages": ["en-US", "en"],
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "maxTouchPoints": 0,
            "vendor": "Google Inc.",
            "vendorSub": "",
            "productSub": "20030107",
            "webdriver": False,
            "plugins": [],
            "mimeTypes": [],
            "connection": {"type": "wifi"},
        },
    }
    with iv8.JSContext(environment=environment,
                       config={"timezone": "Asia/Shanghai"}) as ctx:
        cookie_str = ""
        # 经典瑞数两轮: 第二轮 VM 读 document.cookie 里的 enable_ cookie 才
        # 生成 P cookie — 轮次 cookie 必须种入环境(2026-09-07 北邮实测:
        # 不种入则每轮只重复出 enable_, 永远到不了真页)
        if seed_cookies:
            try:
                _ck = "; ".join(f"{k}={v}" for k, v in seed_cookies.items())
                ctx.eval(f"document.cookie = {json.dumps(_ck + ';')};")
            except Exception:
                pass
        # 2026-09-07 北邮实测: page.load 整体加载时 VM 全局函数与末段调用
        # 作用域失联(document.cookie 空), 而按序 eval 成功出 cookie —
        # 链式执行: 内联脚本(除末段调用) → VM 脚本 → 末段 _$XX() 调用
        _scripts = [s for s in re.findall(r"<script[^>]*>(.*?)</script>",
                                          challenge_html, re.S) if s.strip()]
        if _scripts:
            for _s in _scripts[:-1]:
                try:
                    ctx.eval(_s)
                except Exception:
                    pass
            try:
                ctx.eval(js_text, name=js_url)
            except Exception:
                pass
            try:
                ctx.eval(_scripts[-1])
            except Exception:
                pass
            # 经典瑞数 P-cookie 由 setTimeout 链延迟生成(nodenv 侧 --wait 16s
            # 同理) — 逻辑时间下 pump 事件循环, sleep(5000) 瞬时完成
            try:
                ctx.eval("__iv8__.eventLoop.sleep(5000);")
                ctx.eval("__iv8__.eventLoop.drainTimers();")
            except Exception:
                pass
            cookie_str = ctx.eval("document.cookie") or ""
        if not cookie_str:
            # 兜底: page.load 全流程(其他形态挑战)
            try:
                snapshot = {
                    "baseURL": url,
                    "html": challenge_html,
                    "headers": [[k, v] for k, v in challenge_headers.items()],
                    "resources": {js_url: js_text},
                }
                ctx.expose(snapshot, "snapshot")
                ctx.eval("__iv8__.page.load(__iv8__.data.snapshot);")
                cookie_str = ctx.eval("document.cookie") or ""
            except Exception:
                pass
    out = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


# 2026-09-08 并发互斥(用户处置指令): 15 worker 同时跑 iv8 时 V8 isolate
# 竞争 CPU/内存, 单个求解拖过 20s 超时 → 全链降级 → 瑞数墙院系批内白磨
# (电子科大单独测 10.6s 解出、批内 150-255s 失败实锤)。求解槽上限 3,
# 槽满立即返回空串交上层降级(browser_budget 同款"拒绝不排队"语义,
# 防止 15 个线程堆在信号量上把 spider 预算全烧在等待)。
_IV8_SEM = threading.Semaphore(3)


def fetch_with_iv8(url: str, timeout: float = 20) -> str:
    """瑞数 iv8 求解入口: 返回真实页面 HTML(或空串交上层降级)。

    流程: ①缓存 cookie 快路径 → ②挑战页识别 → ③iv8 链式执行出 cookie →
    ④带 cookie 重抓 → ⑤仍挑战则下一轮(经典瑞数两轮: enable_ → P cookie) →
    ⑥非挑战/非拦截页即返回。全程直连。
    """
    if not _IV8_SEM.acquire(blocking=False):
        return ""  # 求解槽满(>3 并发), 交上层降级
    try:
        return _fetch_with_iv8_impl(url, timeout)
    finally:
        _IV8_SEM.release()


def _fetch_with_iv8_impl(url: str, timeout: float = 20) -> str:
    if timeout < 30:
        timeout = 30  # 2026-09-08 并发竞争下 20s 不够, 放宽至 30s 保求解完成
    host = _host_of(url)
    if not host:
        return ""

    cookies = dict(_load_cookie(host) or {})
    for _round in range(3):  # 经典瑞数最多 3 轮挑战(enable_ → P → 放行)
        html, status, headers = _fetch(url, timeout, cookies)
        if not html:
            return ""
        # 响应 Set-Cookie (经典瑞数的 O cookie) 必须入罐 — 浏览器自动存,
        # curl 不会; 缺 O cookie 则 VM 永远算不出 P cookie (2026-09-07 北邮实锤)
        for _sc in (headers.get("set-cookie") or []):
            _pair = _sc.split(";", 1)[0]
            if "=" in _pair:
                _k, _v = _pair.split("=", 1)
                cookies[_k.strip()] = _v.strip()
        if status == 200 and not _is_challenge(html, status) and len(html) > 500:
            if cookies:
                _save_cookie(host, cookies)
            return html
        if not _is_challenge(html, status):
            return ""  # 非挑战非200真页 → 交上层
        # iv8 链式执行生成本轮 cookie (种入已累计 cookie 供二轮 VM 使用)
        try:
            new_cookies = _solve_with_iv8(html, url, timeout, headers, cookies)
        except Exception:
            return ""  # iv8 未安装/执行失败 → 交上层 nodenv/sdenv
        if not new_cookies:
            return ""
        cookies.update(new_cookies)
        _save_cookie(host, cookies)
    return ""
