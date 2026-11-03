#!/usr/bin/env python3
"""
统一请求层 — 五级自动降级

  1. curl_cffi (impersonate chrome131)     — 无 WAF, >60% 学校
  2. iv8 (Python 原生 V8, C++ 浏览器环境) — 应用层瑞数最高优先级
     (2026-09-07 用户指令: iv8 获取 cookie 优先级最高; 全形态通用)
  3. nodenv (Node 原生补环境, 零 npm 依赖)  — 瑞数 classic 412 降级
     (lzu/bupt/njnu 等; cookie 按域持久化, iv8 失败后)
  3. RsSpider (Node.js sdenv 生成 Cookie)  — 应用层瑞数全站点(scu/njust 等)
  4. DrissionPage (headless)               — JS 渲染 + 应用层瑞数兜底
  5. CDP 桥接本地 Chrome (:9222)            — 网络层瑞数 + JS 全渲染, 浏览器兜底

瑞数自动检测: HTTP 412/202, $_ts.nsd 特征码, document.cookie JS 空壳

用法:
    from smart_fetch import smart_fetch
    html, method = smart_fetch("https://www.ahu.edu.cn/")
    # method: "curl_cffi" | "nodenv" | "ruishu" | "drission" | "cdp" | "blocked"
"""

import re as _re
from curl_cffi import requests as _cffi

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ── 自身目录入 sys.path (2026-09-02 修复): util 内兄弟模块顶层导入
# (drission_pool/browser_lifecycle) 在 spider 子进程等仅把上级根放入
# PYTHONPATH 的场景下无法解析 → ModuleNotFoundError, 全部 spider 崩溃。
# 此处自举: 任何调用方 import smart_fetch 时 util/ 必已在 path 上。
import os as _os
import sys as _sys
import threading as _threading
import time as _time
from pathlib import Path as _Path

_UTIL_DIR = str(_Path(__file__).resolve().parent)
if _UTIL_DIR not in _sys.path:
    _sys.path.insert(0, _UTIL_DIR)

# ── 方法偏好账本 (2026-09-18 重大瑞数族测试): 域 → 上次成功方法 ──
# 同域请求先按偏好方法试(快路径), 失败再走完整降级链 — 重大类站点
# iv8 每页白耗 ~30s 再落 nodenv, 多页 spider 放大为分钟级浪费;
# 偏好命中后直走 nodenv(3轮/页)。7 天 TTL 防站点改版后偏好失效。
import json as _json
from urllib.parse import urlparse as _urlparse
_METHOD_PREF_F = _Path(_UTIL_DIR) / "cookies" / "_method_pref.json"
_METHOD_PREF_TTL = 7 * 86400


def _method_pref_load() -> dict:
    try:
        d = _json.load(open(_METHOD_PREF_F, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _method_pref_save(host: str, method: str):
    try:
        d = _method_pref_load()
        d[host] = {"m": method, "t": _time.time()}
        _json.dump(d, open(_METHOD_PREF_F, "w", encoding="utf-8"),
                   ensure_ascii=False, indent=1)
    except Exception:
        pass

# ── 代理配置（college teacher/.env 的 PROXY_URL）──
try:
    from dotenv import load_dotenv as _load_dotenv
    _shell_proxy_mode = _os.environ.get("PROXY_MODE")  # shell 显式设置优先（download_loop.sh PROXY_MODE=off）
    _load_dotenv(_Path(__file__).resolve().parent.parent / ".env", override=True)  # .env 强制覆盖
    if _shell_proxy_mode is not None:
        _os.environ["PROXY_MODE"] = _shell_proxy_mode  # 防 .env 覆盖 shell（2026-09-03: 快代理失效期全直连）
except Exception:
    pass
from drission_pool import acquire_tab, release_tab, can_cdp  # noqa: E402

_PROXY_URL = _os.environ.get("PROXY_URL", "").strip()
_PROXY_MODE = _os.environ.get("PROXY_MODE", "off").strip()
_PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if (_PROXY_URL and _PROXY_MODE != "off") else None

# 直连可用域名缓存: 直连成功 → 记录域名, 后续同域名请求直接直连
# (本地 IP 不被瑞数拉黑, 第3轮实测 兰大/南师大 代理 412 / 直连 200)。
# 2026-08-24 直连优先: _PROXY_OK_HOSTS = 直连失败但代理成功的域名(永久走代理);
# _DIRECT_TIMEOUT = 直连尝试超时; _DIRECT_FAILS/_DIRECT_FAIL_LIMIT = 直连失败
# 多次 → 自主转代理优先。与 http_client.py 共享同一份缓存。
try:
    from http_client import (_DIRECT_OK_HOSTS, _PROXY_OK_HOSTS, _DIRECT_TIMEOUT,
                             _DIRECT_FAILS, _DIRECT_FAIL_LIMIT,
                             _RUISHU_BLOCK_CODES, _host_of_url)
except Exception:  # 极端环境退化: 无共享缓存时本地独立判定
    _DIRECT_OK_HOSTS = set()
    _PROXY_OK_HOSTS = set()
    _DIRECT_TIMEOUT = 8.0
    _DIRECT_FAILS = {}
    _DIRECT_FAIL_LIMIT = 3
    _RUISHU_BLOCK_CODES = frozenset({202, 412, 452})

    def _host_of_url(url: str) -> str:
        try:
            from urllib.parse import urlsplit
            return (urlsplit(url).hostname or "").lower()
        except Exception:
            return ""


def _fix_encoding(resp):
    """响应编码自适应 — 修复 GBK 站点被硬编码 utf-8 变乱码(浙大遗传学 ig.zju.edu.cn 教训)。

    优先级(与浏览器一致): 1) 页面 meta charset 永远优先(页面自我声明最可信, 曾因
    header 无 charset 时 curl_cffi 默认 utf-8 而误判 GBK 页); 2) header 明确 charset
    (非 latin1 误标) → 尊重; 3) 都无 → UTF-8 解码合法性校验: 合法 utf-8, 否则 GBK 兜底。
    """
    try:
        raw = resp.content[:3000]
        m = _re.search(rb'charset=["\']?([a-zA-Z0-9_\-]+)', raw, _re.I)
        if m:
            resp.encoding = m.group(1).decode("ascii", "ignore")
            return
    except Exception:
        pass
    try:
        enc = (getattr(resp, "encoding", None) or "").lower()
        if enc and enc not in ("iso-8859-1", "ascii", "windows-1252"):
            return
    except Exception:
        pass
    try:
        resp.content.decode("utf-8")
        resp.encoding = "utf-8"
    except (UnicodeDecodeError, Exception):
        resp.encoding = "gbk"


def _is_proxy_failure(e: BaseException) -> bool:
    """判定异常是否为"代理失效"(连接代理失败/407 认证失败/代理相关错误)。

    与 http_client.py 同名函数逻辑一致 (此处本地实现避免循环 import)。
    命中时调用方去掉代理直连重试一次 (本地网络兜底)。
    """
    import requests
    try:
        from curl_cffi.requests.exceptions import ProxyError as _CffiProxyError
    except Exception:
        _CffiProxyError = ()
    if isinstance(e, (requests.exceptions.ProxyError, _CffiProxyError)):
        return True
    name, msg = type(e).__name__.lower(), str(e).lower()
    if "proxy" in name or "407" in msg:
        return True
    if "proxy" in msg and ("connect" in msg or "tunnel" in msg or "refused" in msg):
        return True
    # 2026-08-24: curl_cffi 连不上代理可能抛 ConnectionError(非 ProxyError),
    # msg 不含 "proxy" — 连接级失败一律按代理失效处理 (retry 满 3 次)
    if ("connection refused" in msg or "connect failed" in msg
            or "connection reset" in msg or "couldn't connect" in msg):
        return True
    return False

# ── 瑞数特征码 ────────────────────────────────────
_RUISHU_NSD = _re.compile(r'\$_ts\.nsd\s*=\s*\d+')
_RUISHU_COOKIE = _re.compile(r"document\.cookie\s*=")




def _is_ruishu(text: str, status: int) -> bool:
    if status in (202, 412):
        return True
    if status == 200 and len(text) < 500:
        if "<script" in text and _RUISHU_COOKIE.search(text):
            return True
        if _RUISHU_NSD.search(text):
            return True
    return False


# ── WAF 挑战页/风控页检测 (2026-08-23) ──
# 政法大学等部署无特征码 WAF: curl_cffi 拿到 JS 挑战页(loader+challengeId) 或
# "访问被限制/浏览器环境不被允许" 页, 均 >500B 无 nsd 特征, 旧逻辑当成功返回
# → fix_loop 假"无师资页" → SKIP 死循环。检测到即视为未拿到, 继续降级。
_WAF_CHALLENGE = [
    _re.compile(r"challengeId\s*="),
    _re.compile(r"访问被限制"),
    _re.compile(r"浏览器环境不被允许"),
    _re.compile(r"waf|防护|安全验证", _re.I),
    # 2026-08-24 瑞数变种: content_container 壳 + 隐藏 head div + webdriver 探测
    # (中南财经政法 zhxmt.zuel.edu.cn 91KB 挑战页无 challengeId, 旧模式漏判 → 假"无师资页")
    _re.compile(r"navigator\.webdriver|_tox_"),
    _re.compile(r'id="content_container"[\s\S]*?display:none'),
    # 2026-10-14 中山医院 jp()/bq() eval 跳转壳实锤: 2214B 壳(oo数组+轮解密
    # +XOR+eval(po)), 无瑞数/challengeId特征被当成功返回 → 蜘蛛提取0。
    # 特征: setTimeout("xx(N)") 触发 + eval("qo=eval;qo(po);") 解码跳转
    _re.compile(r'setTimeout\(\s*["\'][a-z]{2}\(\d+\)'),
    _re.compile(r'eval\(\s*["\']qo=eval;qo\(po\);'),
]


def _is_challenge_page(html: str) -> bool:
    if not html or len(html) > 200_000:  # 真实页通常大; 大页面不做挑战判定
        return False
    for pat in _WAF_CHALLENGE:
        if pat.search(html):
            # 2026-08-24 误伤修复: 渲染后的真实页可能残留挑战脚本
            # (zhxmt 157KB 真实页仍含 content_container/webdriver 源码文本),
            # 挑战壳必然无真实链接 → 有 ≥3 个链接视为真实页放行
            if len(_re.findall(r"<a[^>]+href", html)) >= 3:
                return False
            return True
    # loader spinner + 无正文: 典型的 JS 挑战等待页
    if "<div class=\"loader\"" in html and "<title>" in html and len(html) < 8000:
        return True
    return False


def _is_blocked_html(html: str) -> bool:
    """检查 CloakBrowser/CDP 拿到的 HTML 是否还是拦截页"""
    if not html or len(html) < 200:
        return True
    if _RUISHU_NSD.search(html) and len(html) < 800:
        return True
    if _is_challenge_page(html):
        return True
    return False


# ── 各级请求 ──────────────────────────────────────

# 代理熔断 (2026-08-24): 快代理隧道实测 441 秒拒 / 200 龟速(10s+ 传不完
# 40KB) — 僵尸链根因。441/代理异常 → 熔断 30 分钟, 窗口期内跳过代理全直连。
_PROXY_MELTED_UNTIL = 0.0
_PROXY_MELT_TTL = float(_os.environ.get("PROXY_MELT_TTL", "1800"))


def _try_curl_cffi(url: str, timeout: int) -> tuple:
    """直连优先 → 代理兜底 (2026-08-24 用户指令降级链反转)。

    降级链: 本地直连(1次, DIRECT_TIMEOUT 快超时) → 代理(重试≤2) → ("","") 交上层浏览器;
    双向记忆:
      - 直连成功 → _DIRECT_OK_HOSTS: 同域永久直连(本地 IP 不被瑞数拉黑);
      - 直连失败但代理成功 → _PROXY_OK_HOSTS: 同域直接走代理, 不再重复直连超时;
    代理 441(快代理拒绝)/异常 → 熔断 _PROXY_MELTED_UNTIL, 窗口期跳过代理;
    瑞数/挑战页响应不算成功 → 交上层, 不浪费重试。
    """
    global _PROXY_MELTED_UNTIL
    host = _host_of_url(url)
    # 2026-09-15 AES-GCM 壳优先破解(中南财经政法全校部署实锤, 用户指令兼容):
    # 壳页特征(content_container+混淆JS) → 纯CPU AES-GCM 解密 → 直接返回真实
    # HTML, 一次 GET 零浏览器零挑战往返, 优于 iv8/nodenv/瑞数全部降级链;
    # 解密失败静默继续原链(非 AES 壳/动态混淆未覆盖时不阻断)
    try:
        _r0 = _cffi.get(url, headers={"User-Agent": _UA},
                        impersonate="chrome131",
                        timeout=min(timeout, _DIRECT_TIMEOUT), proxies=None)
        if _r0.status_code == 200 and _r0.text and _is_challenge_page(_r0.text):
            from util.loader_challenge import crack_aes_gcm_shell
            _cracked = crack_aes_gcm_shell(_r0.text)
            if _cracked:
                _DIRECT_OK_HOSTS.add(host)
                return _cracked, "crack_aes"
    except Exception:
        pass
    # 2026-09-07 用户指令: 瑞数域全直连 — util/cookies/<host>.nodenv.json
    # 存在即确认瑞数站, 代理尝试纯浪费且 cookie 失效; 从代理优先缓存摘除
    _rs_host = False
    try:
        _ck = _Path(__file__).resolve().parent / "cookies"
        # nodenv 或 iv8 cookie 落盘均=确认瑞数域 → 直连强制(跳过代理)
        _rs_host = ((_ck / f"{host}.nodenv.json").exists()
                    or (_ck / f"{host}.iv8.json").exists())
    except Exception:
        pass
    if _rs_host:
        _PROXY_OK_HOSTS.discard(host)
    direct_only = host in _DIRECT_OK_HOSTS
    if not direct_only and host not in _PROXY_OK_HOSTS:
        # 1) 本地直连优先: 快超时, 失败不重试直接切代理
        # 2026-09-04 用户指令: 单一 chrome131 指纹升级为轮换链
        # (chrome110→chrome136→firefox147→safari18_0) — 拦新不拦老(兰大/川大)
        # 与拦老不拦新的 WAF 都能覆盖; 全失败落代理路径, 链序统一在
        # util/fingerprint_chain.py 维护
        from util.fingerprint_chain import try_impersonations as _try_imps
        try:
            # 2026-10-14 GBK乱码根修(仁济swiper 0提取实锤): checker 在编码
            # 修正前访问 r.text → curl_cffi 按默认 utf-8 解码 GBK 字节并缓存
            # 乱码, 后续 _fix_encoding 改 encoding 无效(徐强→乱码)。checker
            # 内先 _fix_encoding 再判
            def _ck_imp(r):
                try:
                    _fix_encoding(r)
                except Exception:
                    pass
                return (r.status_code == 200 and len(r.text) > 500
                        and not _is_ruishu(r.text, r.status_code)
                        and not _is_challenge_page(r.text))
            resp, _prof = _try_imps(
                url, timeout=min(timeout, _DIRECT_TIMEOUT),
                headers={"User-Agent": _UA}, proxies=None,
                checker=_ck_imp)
            if resp is not None:
                _fix_encoding(resp)
                _DIRECT_OK_HOSTS.add(host)
                _DIRECT_FAILS.pop(host, None)
                return resp.text, "curl_cffi"
        except Exception:
            pass  # 直连失败 → 落入代理路径
        # 直连失败自适应 (2026-08-24 用户指令): 连续 DIRECT_FAIL_LIMIT 次 →
        # 该域名自主转代理优先, 不再每次重复白试直连(每请求省 DIRECT_TIMEOUT 秒)。
        n = _DIRECT_FAILS.get(host, 0) + 1
        if n >= _DIRECT_FAIL_LIMIT:
            _PROXY_OK_HOSTS.add(host)
            _DIRECT_FAILS.pop(host, None)
        else:
            _DIRECT_FAILS[host] = n
    # 2) 代理路径 (直连失败 / _PROXY_OK_HOSTS 域名; 熔断期/瑞数域跳过)
    if not direct_only and not _rs_host and _PROXIES and _time.time() >= _PROXY_MELTED_UNTIL:
        saw_441 = False
        for _attempt in range(2):
            try:
                resp = _cffi.get(
                    url, headers={"User-Agent": _UA},
                    impersonate="chrome131", timeout=timeout,
                    proxies=_PROXIES)
                _fix_encoding(resp)
                if resp.status_code == 200 and len(resp.text) > 500:
                    if not _is_ruishu(resp.text, resp.status_code) and not _is_challenge_page(resp.text):
                        _PROXY_OK_HOSTS.add(host)
                        return resp.text, "curl_cffi"
                # 站端 WAF/瑞数 (202/412/452/挑战页): retry 无意义 → 交上层浏览器
                if (resp.status_code in _RUISHU_BLOCK_CODES
                        or _is_ruishu(resp.text, resp.status_code)
                        or _is_challenge_page(resp.text)):
                    return "", ""
                if resp.status_code == 441:
                    saw_441 = True  # 快代理拒绝: 重试满 2 次, 全挂则熔断
            except Exception as e:
                if not _is_proxy_failure(e):
                    break  # 非代理错误 → 停止重试
                _PROXY_MELTED_UNTIL = _time.time() + _PROXY_MELT_TTL  # 代理异常 → 熔断
        if saw_441:
            _PROXY_MELTED_UNTIL = _time.time() + _PROXY_MELT_TTL
    # 3) 直连兜底 (代理全挂/熔断期/无代理配置)
    try:
        resp = _cffi.get(
            url, headers={"User-Agent": _UA},
            impersonate="chrome131", timeout=timeout,
            proxies=None)
        _fix_encoding(resp)
        if resp.status_code == 200 and len(resp.text) > 500:
            if not _is_ruishu(resp.text, resp.status_code) and not _is_challenge_page(resp.text):
                _DIRECT_OK_HOSTS.add(host)
                return resp.text, "curl_cffi"
    except Exception:
        pass
    return "", ""


def _try_system_curl(url: str, timeout: int) -> tuple:
    """系统 curl 兜底 (2026-09-03 暨大 jnumed 教训): 站点 WAF 按 TLS 指纹
    拦截 curl_cffi(502)/requests(473)/DrissionPage(连接断开), 但系统 libcurl
    原生指纹可通过(实测 302→https→200 53KB 真实页面)。curl_cffi 全失败后
    用 subprocess 调系统 curl 跟重定向, 成功口径与 curl_cffi 一致(200+
    >500B+非瑞数/挑战页)。注意: 返回的是解码后的 str, 与上层调用一致。"""
    import subprocess
    try:
        p = subprocess.run(
            ["curl", "-sL", "--max-time", str(min(timeout, 20)), "-A", _UA,
             "-o", "-", "-w", "\n%{http_code}", url],
            capture_output=True, timeout=timeout + 5)
        raw = p.stdout
        if not raw:
            return "", ""
        # 最后一行是 http_code(-w), 前面是响应体
        body, _, code = raw.decode("utf-8", "replace").rpartition("\n")
        code = code.strip()
        if code == "200" and len(body) > 500:
            if not _is_ruishu(body, 200) and not _is_challenge_page(body):
                return body, "system_curl"
    except Exception:
        pass
    return "", ""


def _try_iv8(url: str, timeout: int) -> tuple:
    """iv8 瑞数求解器 — 瑞数路径最高优先级 (2026-09-07 用户指令:
    github.com/HanZzzzz000/iv8 是 Python 原生 V8 扩展, C++ 浏览器环境,
    保真度高于 nodenv(Node vm 补环境), 覆盖 classic 与 scu/njust 类形态;
    纯 Python 无外部 Node 进程。全程直连(瑞数 cookie 与 IP 强相关)。"""
    try:
        from iv8_rs import fetch_with_iv8
        t = fetch_with_iv8(url, timeout=max(timeout, 15))
        if t and len(t) > 500:
            if not _is_ruishu(t, 200) and not _is_challenge_page(t):
                return t, "iv8"
    except Exception:
        pass
    return "", ""


def _try_nodenv(url: str, timeout: int) -> tuple:
    """nodenv 零依赖手写补环境链式 (2026-09-02 用户指令: node 原生补环境
    最优先, 浏览器兜底)。纯 Node vm 沙箱跑瑞数 VM, 零 npm 依赖, 13-14s/轮;
    cookie 按域持久化(util/cookies/<host>.nodenv.json), 快路径 1-2s。
    覆盖 lzu/bupt/njnu 等 classic 412; scu/njust 形态不支持 → 交 RsSpider(sdenv)。"""
    try:
        from rs_cookie import fetch_with_nodenv
        t = fetch_with_nodenv(url, timeout=max(timeout, 20))
        if t and len(t) > 500:
            # 与 _try_ruishu 同口径: 瑞数变种挑战壳(200 空壳/91KB
            # content_container)不得当成功 → 交下层 sdenv/浏览器
            if not _is_ruishu(t, 200) and not _is_challenge_page(t):
                return t, "nodenv"
    except Exception:
        pass
    return "", ""


def _try_ruishu(url: str, timeout: int) -> tuple:
    # 2026-09-16 RS_DISABLE 开关(用户政策: 9333 启动即死循环时临时关闭
    # sdenv 瑞数路径, 瑞数站降级 blocked 收敛重试 — 与 09-11「29 硬墙维持
    # blocked」一致)。默认 on; .env RS_DISABLE=1 关闭, 下批次 worker 生效。
    try:
        import os as _os
        if _os.environ.get("RS_DISABLE", "").strip() in ("1", "on", "true"):
            return "", "blocked"
    except Exception:
        pass
    # 2026-09-07 用户指令: 瑞数站全直连 — 瑞数 cookie 与出口 IP 强相关,
    # 快代理是隧道代理(出口不稳定/换 IP 即 cookie 失效), 瑞数路径一律
    # proxies=None, 禁止代理重试(本地 IP 实测更稳: 兰大/南师大 代理412/直连200)
    try:
        from handleRuishu import RsSpider
        spider = RsSpider(timeout=max(timeout * 2, 30), proxies=None)
        resp = spider.get(url)
        t = resp.text
        if resp.status_code == 200 and len(t) > 500:
            # 2026-08-24: 瑞数变种挑战页(91KB content_container 壳) status 200 无 nsd
            # 特征, _is_ruishu 漏判 → 补 _is_challenge_page 检查, 否则假"成功"
            if not _is_ruishu(t, resp.status_code) and not _is_challenge_page(t):
                return t, "ruishu"
    except Exception as e:
        # 代理失效 → RsSpider 直连再试一次 (本地网络兜底)
        if _PROXIES and _is_proxy_failure(e):
            try:
                from handleRuishu import RsSpider
                spider = RsSpider(timeout=max(timeout * 2, 30), proxies=None)
                resp = spider.get(url)
                t = resp.text
                if resp.status_code == 200 and len(t) > 500:
                    if not _is_ruishu(t, resp.status_code) and not _is_challenge_page(t):
                        return t, "ruishu"
            except Exception:
                pass
    return "", ""


def _try_drission(url: str, timeout: int) -> tuple:
    """DrissionPage tab 租用请求 — 窗口池 3×4=12 上限, get 在锁外并行"""
    tab = acquire_tab()
    if not tab:
        return "", ""
    try:
        tab.get(url, timeout=timeout)
        import time; time.sleep(2)
        html = tab.html
        # SPA 页面链接少也接受, 只要非拦截页且长度够
        if html and not _is_blocked_html(html) and len(html) > 1000:
            return html, "drission"
    except Exception:
        pass
    finally:
        release_tab(tab)  # 用完即关: 及时释放内存, 窗口保留复用
    return "", ""


_cloak_sem = _threading.Semaphore(1)  # cloakbrowser 免费版仅 1 并发会话


def _try_cloakbrowser(url: str, timeout: int) -> tuple:
    """CloakBrowser 隐身 Chromium (2026-09-12 用户指令, 排在 DrissionPage 之后):
    源码级指纹伪装(73 处 C++ 补丁: canvas/WebGL/GPU/TLS 指纹与真 Chrome 一致,
    webdriver 恒 false), 对瑞数严格档站有独特通过率(mkszy 实测 12s 真页)。
    免费版仅 1 并发会话 → 全局信号量串行 + 每次用完即关(不常驻)。"""
    import time as _t
    if not _cloak_sem.acquire(blocking=False):
        return "", ""  # 已有会话在跑: 交上层
    try:
        from cloakbrowser import launch
        _b = launch()
        try:
            _p = _b.new_page()
            _p.goto(url, timeout=min(max(timeout, 30), 90) * 1000,
                    wait_until="domcontentloaded")
            _t.sleep(5)  # 瑞数 VMP 自执行 + 自动跳转
            _html = _p.content()
            _w = 0
            while _w < 15 and ("正在加载" in _html or 'class="loader"' in _html):
                _t.sleep(3)
                _w += 3
                try:
                    _html = _p.content()
                except Exception:
                    break
            if _html and len(_html) > 2000 and not _is_blocked_html(_html):
                return _html, "cloakbrowser"
        finally:
            try:
                _b.close()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _cloak_sem.release()
    return "", ""


def _try_cdp(url: str, timeout: int) -> tuple:
    if not can_cdp():
        return "", ""
    page = None
    try:
        # 并发预算: CDP 新开页面也占一个浏览器槽位, 满则降级 (不排队)
        from browser_budget import BrowserBudgetExceeded, browser_budget
        with browser_budget():
            # 2026-08-24: 统一走 cdp_util (伪装 UA/webdriver 穿透瑞数变种,
            # 内联 playwright 无伪装拿不到 zhxmt.zuel.edu.cn 类挑战站真实页)
            from cdp_util import cdp_page
            with cdp_page() as (page, _ctx):
                # domcontentloaded: 部分站点有永不触发的 load 资源(广告/统计), 等 load 会卡死超时
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                import time
                # 滚动触发懒加载 (浙大等页面学院列表渐进渲染, 不滚只能拿到首屏)
                for _ in range(6):
                    page.mouse.wheel(0, 4000)
                    time.sleep(0.8)
                time.sleep(3)
                html = page.content()
                # SPA 页面链接少也接受(央美等单页应用), 只要非拦截页
                if html and not _is_blocked_html(html) and len(html) > 1000:
                    return html, "cdp"
    except BrowserBudgetExceeded:
        pass  # 并发超限: 降级, 交由上层判定
    except Exception:
        pass
    finally:
        # 2026-08-23 治理: close 移入 finally — goto 超时/异常路径原样泄漏
        # 空白 tab 到 9222 浏览器, 60 并发下几十个 tab 堆积吃内存
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    return "", ""


# ══════════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════════

def smart_fetch(url: str, timeout: int = 15) -> tuple:
    """四级自动降级请求.

    Returns: (html: str, method: str)
      method: "curl_cffi" | "ruishu" | "drission" | "cdp" | "blocked"
    """
    # 0. URL 合法性拦截: 站点模板转换错误占位链接 href 内嵌 HTML(<span>转换链接错误</span>)
    #    非法 URL 直接不请求 — 否则 curl_cffi 报错后降级到浏览器也照样发出畸形请求
    if not url or " " in url or "<" in url or ">" in url or "\n" in url or "\t" in url \
            or not _re.match(r"^https?://", url):
        return None, "blocked"
    # 1. curl_cffi
    html, method = _try_curl_cffi(url, timeout)
    if html:
        return html, method

    # 1.1 系统 curl 兜底 (2026-09-03 暨大 jnumed: WAF 按 TLS 指纹拦 curl_cffi/
    # requests/DrissionPage, 但系统 libcurl 可通过。纯指纹拦截站不走耗时的
    # nodenv(13s)/浏览器渲染, 在此直接解决)
    html, method = _try_system_curl(url, timeout)
    if html:
        return html, method

    # 1.2 方法偏好快路径 (2026-09-18 重大瑞数族测试): 同域上次成功的方法
    # 先行 — 重大类站点 iv8 每页白耗 ~30s 再落 nodenv, 多页 spider 放大为
    # 分钟级浪费; 偏好命中直走该方法, 失败继续完整链
    _host = _urlparse(url).netloc
    _pref = _method_pref_load().get(_host)
    if _pref and _time.time() - float(_pref.get("t", 0)) < _METHOD_PREF_TTL:
        _pm = _pref.get("m", "")
        if _pm == "iv8":
            html, method = _try_iv8(url, timeout)
            if html:
                return html, method
        elif _pm == "nodenv":
            html, method = _try_nodenv(url, timeout)
            if html:
                return html, method
        elif _pm == "ruishu":
            html, method = _try_ruishu(url, timeout)
            if html:
                return html, method

    # 1.4. iv8 (Python 原生 V8 瑞数破解 — 2026-09-07 用户指令: 优先级最高)
    html, method = _try_iv8(url, timeout)
    if html:
        _method_pref_save(_host, method)
        return html, method

    # 1.5. nodenv (Node 原生补环境 — iv8 失败后降级)
    html, method = _try_nodenv(url, timeout)
    if html:
        _method_pref_save(_host, method)
        return html, method

    # 2. RsSpider (Node.js sdenv + 9333 常驻浏览器, 单实例共享)
    # 2026-09-11 上午曾按「减小浏览器使用」移入 BROWSER_TIERS 开关之后 —
    # 后果: 纯 CPU 链(iv8/nodenv)覆盖不了的瑞数学校无任何兜底, 大面积
    # 「师资页不可达」空白(西南石油/西交/郑大实锤)。同日 16:40 调回:
    # sdenv 恢复在开关之前(单实例 9333 + handleRuishu 自身生命周期管理,
    # 非每 spider 一浏览器), 内存代价 ~400MB 可控; 40 并发时代崩溃主因
    # 是并发×浏览器叠加, 现 15-20 并发 + 单实例, 风险已排除。
    html, method = _try_ruishu(url, timeout)
    if html:
        return html, method

    # 2026-09-10 浏览器层总开关(用户机器 8GB 内存两次被浏览器+并发拖爆,
    # Chrome 劫持事故同日复现): BROWSER_TIERS=off 时跳过 DrissionPage/CDP
    # 两层 — 纯 CPU 链(curl_cffi/系统curl/iv8/nodenv) + sdenv 单实例直达。
    try:
        _browsers_off = (_os.environ.get("BROWSER_TIERS", "off").strip().lower()
                         in ("off", "0", "no", "false"))
    except Exception:
        _browsers_off = False
    if _browsers_off:
        return "", "blocked"

    # 3. DrissionPage (ChromiumPage, 替代 CloakBrowser)
    html, method = _try_drission(url, timeout)
    if html:
        return html, method

    # 4. CDP 桥接本地 Chrome (9222 无并发限制 + stealth.min.js 完整版注入)
    # 2026-09-23 用户纠偏: CDP 无并发限制, 应优先于 CloakBrowser(1并发串行)
    html, method = _try_cdp(url, timeout)
    if html:
        return html, method

    # 5. CloakBrowser 隐身 Chromium (最后手段: 免费版 1 并发串行)
    html, method = _try_cloakbrowser(url, timeout)
    if html:
        return html, method

    return "", "blocked"


def fetch_method_label(method: str) -> str:
    labels = {
        "curl_cffi":     "✅ curl_cffi",
        "system_curl":   "✅ 系统curl(指纹绕过)",
        "iv8":           "✅ iv8(V8原生瑞数破解)",
        "nodenv":        "✅ nodenv(原生补环境)",
        "ruishu":        "✅ RsSpider(瑞数绕过)",
        "cloakbrowser":  "✅ CloakBrowser",
        "cdp":           "✅ CDP(Chrome桥接)",
        "blocked":       "❌ 全部失败",
    }
    return labels.get(method, method)
