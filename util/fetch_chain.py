#!/usr/bin/env python3
"""fetch 统一入口: JS 跳转跟随 + 四级降级请求链 — 2026-08-31 从 collect_teacher 迁出。

Tier 0: util.smart_fetch(统一请求层) → Tier 1: curl_cffi(代理复用 http_client)
→ Tier 2: Playwright(浏览器管理: CDP 9222 常驻浏览器租 tab 用完即关, 不可达才
chromium.launch 兜底; 并发预算 browser_budget + 本地代理探测)
→ Tier 3: requests + 自定义 SSL(@SECLEVEL=1)。
各层依赖均为函数内懒加载(与原实现逐字节一致)。
"""
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin
# ── HTTP (curl_cffi → Playwright → requests) ──
REDIRECT_JS_PAT = re.compile(r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", re.I)
META_REFRESH_PAT = re.compile(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?[0-9.;]*url=([^\s"\'>]+)', re.I)

def fetch(url, use_playwright=False, follow_redirect=True, return_url=False):
    """fetch with JS-redirect following (max 3 hops)

    return_url=True 时返回 (html, final_url)——final_url 是跟随 JS 跳转后
    的真实 URL。**必须用 final_url 做后续 urljoin 的 base**（北大哲学系
    教训: szdw/index.htm 是 JS 跳转壳, 内容在 szll/index.htm; 若用壳 URL
    做 base, 相对链接 "mkszyzjxys/xxx.htm" 会解析成 /szdw/mkszyzjxys/
    （少 szll 层）→ 全部 404, 教师详情页一个都抓不到）。
    """
    final_url = url
    html = _fetch_raw(url, use_playwright)
    if not html:
        return (None, final_url) if return_url else None
    if follow_redirect:
        for _ in range(3):
            m = REDIRECT_JS_PAT.search(html[:2000]) or META_REFRESH_PAT.search(html[:3000])
            if not m:
                break
            target = m.group(1).strip().strip("'\"")
            target = urljoin(url, target)
            if target == url:
                break
            html2 = _fetch_raw(target, use_playwright)
            if not html2:
                break
            url, html = target, html2
            final_url = url
    return (html, final_url) if return_url else html

def _fetch_raw(url, use_playwright=False):
    # 0) 统一请求层: util/smart_fetch 四级降级(curl_cffi→瑞数→DrissionPage→CDP)
    # 兰大等瑞数站点 curl_cffi/requests 均失败, 只有 DrissionPage 层能过
    # (实测 xxxy.lzu.edu.cn → drission), 且生成 spider 走同一请求层, 口径一致
    try:
        import sys as _s
        from pathlib import Path as _P
        _root = str(_P(__file__).resolve().parent.parent)
        if _root not in _s.path:
            _s.path.insert(0, _root)
        from util.smart_fetch import smart_fetch
        html, _m = smart_fetch(url, timeout=20)
        if html and len(html) > 500:
            # 2026-08-31: JS loader 壳("正在加载"+dynamic_challenge)不是真内容,
            # 不得当成功返回(西交/北理工发现链拿壳漂移的根因) — 交 0.5 层破解
            try:
                from util.loader_challenge import is_shell as _is_shell0
                if not _is_shell0(html):
                    return html
            except Exception:
                return html
    except Exception:
        pass

    # 0.5) JS loader 挑战纯 HTTP 破解(2026-08-31 西交系站逆向全局经验:
    # "正在加载"壳=硬编码算术题(challengeId/a/b/operator 内嵌壳JS),
    # POST /dynamic_challenge 拿 client_id cookie 即过 — 0.3s 零浏览器,
    # 优先于 curl/Playwright 重层; 壳解析失败自动跳过)
    try:
        from util.loader_challenge import fetch as _lc_fetch
        _h = _lc_fetch(url)
        if _h and len(_h) > 1000:
            return _h
    except Exception:
        pass

    # 1) curl_cffi (带快代理; 代理熔断期直连, 见 util/http_client._proxy_fused)
    try:
        from util.http_client import _build_proxies, _proxy_fused
        import curl_cffi.requests as cr
        r = cr.get(url, timeout=15, verify=False, impersonate="chrome124",
                   headers={"User-Agent":"Mozilla/5.0 Chrome/138"},
                   proxies=None if _proxy_fused() else _build_proxies())
        if r.status_code == 200 and len(r.content) > 500:
            try:  # 裸请求层必须复用 smart_fetch 编码修复(SCUT 师资队伍锚文本
                # 乱码→分类过滤失效→szdw_38586 不走→内嵌接口漏扫, 2026-08-28 教训)
                from util.smart_fetch import _fix_encoding
                _fix_encoding(r)
            except Exception:
                pass
            return r.text
    except: pass

    # 2) Playwright (for WAF 412) — 浏览器并发预算内(util/browser_budget.py),
    # 满预算立即降级不排队; 页面用完即关(close 在 finally, goto 抛异常也不泄漏浏览器)
    # 2026-08-27 用户指令: 不频繁创建 window content — 优先 connect_over_cdp 连接
    # 9222 常驻浏览器(由 watchdog 守护, --user-data-dir=/tmp/chrome_cdp_9222 项目专用),
    # 只开 tab 用完即关, 不新建 Chrome 实例; 9222 不可达才 launch 兜底(budget 限流)。
    if use_playwright:
        try:
            from util.browser_budget import BrowserBudgetExceeded, browser_budget
            import socket as _socket
            _proxy_args = []
            # 2026-09-03: PROXY_MODE=off(快代理失效期全直连)时不探测转发器, 即使 15819
            # 在跑也强制直连, 与 http_client/local_proxy/smart_fetch 口径一致。
            _proxy_mode = os.environ.get("PROXY_MODE", "off").strip()
            if _proxy_mode != "off":
                try:  # 本地代理转发器(local_proxy.py → PROXY_URL)可用才走代理
                    with _socket.create_connection(("127.0.0.1", 15819), timeout=2):
                        _proxy_args = ["--proxy-server=http://127.0.0.1:15819"]
                except Exception:
                    pass
            with browser_budget():
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b, b_connected = None, False
                    try:  # 优先: 连接 9222 常驻浏览器(开 tab, 不新建窗口)
                        # 2026-08-29: connect_over_cdp 默认 180s 超时 — 并发连接 9222
                        # 变慢时 worker 卡满 180s 不干活(资源消耗偏少根因), 5s 连不上
                        # 立即降级到 launch/requests, 不拖住 worker。
                        with _socket.create_connection(("127.0.0.1", 9222), timeout=2):
                            b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
                            b_connected = True
                    except Exception:
                        pass
                    if b is None:  # 兜底: 9222 不可达才 launch 新实例
                        b = pw.chromium.launch(headless=True,
                            args=['--disable-blink-features=AutomationControlled','--no-sandbox'] + _proxy_args)
                    ctx = b.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
                    try:
                        page = ctx.new_page()
                        page.goto(url, timeout=60000, wait_until='load'); time.sleep(4)  # 2026-08-31 北理工smen 实测 30s 超时
                        html = page.content()
                        # 2026-08-31 JS挑战加载器壳(西交数学 math.xjtu.edu.cn:
                        # 首访返回"网站正在加载中..."+动态验证后跳转, 4s不够,
                        # 6.7KB 壳被当正文 → 发现链全失/BFS深度不足误判)。
                        # 壳页特征: title"正在加载"或 class="loader"
                        _waited = 0
                        while (_waited < 16 and html
                               and ("正在加载" in html or 'class="loader"' in html)):
                            time.sleep(3)
                            _waited += 3
                            try:
                                html = page.content()
                            except Exception:
                                break
                        if len(html) > 500: return html
                    finally:
                        try: page.close()  # 关 tab: 连接模式只关 tab, 不关常驻浏览器
                        except Exception: pass
                        try: ctx.close()
                        except Exception: pass
                        if not b_connected:  # 只有自启的才关浏览器; 9222 常驻不动
                            try: b.close()
                            except Exception: pass
        except BrowserBudgetExceeded:
            pass  # 浏览器并发超限: 降级到下一级
        except: pass

    # 3) requests (带快代理)
    try:
        from util.http_client import _build_proxies, _proxy_fused
        import requests as rq, ssl
        from requests.adapters import HTTPAdapter
        class _IA(HTTPAdapter):
            def init_poolmanager(self,*a,**kw):
                c=ssl.create_default_context();c.check_hostname=False
                c.verify_mode=ssl.CERT_NONE;c.set_ciphers("DEFAULT:@SECLEVEL=1")
                kw["ssl_context"]=c;return super().init_poolmanager(*a,**kw)
        s=rq.Session();s.mount("https://",_IA());s.verify=False
        if not _proxy_fused():
            _pr = _build_proxies()
            if _pr: s.proxies = _pr
        r=s.get(url,timeout=15,headers={"User-Agent":"Mozilla/5.0 Chrome/138"})
        if r.status_code==200 and len(r.content)>500:
            try:  # 同 curl_cffi 层: 复用 smart_fetch 编码修复
                from util.smart_fetch import _fix_encoding
                _fix_encoding(r)
            except Exception:
                pass
            return r.text
    except: pass
    return None
