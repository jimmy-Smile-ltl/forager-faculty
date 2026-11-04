#!/usr/bin/env python3
"""
统一 HTTP 请求层 — 所有网络请求的唯一入口。

集中管理: 代理 / 降级策略 / 错误页过滤 / 二进制下载校验。
业务代码（detail_scraper、faculty_discovery、backfill 等）一律调用本模块，
后续切换代理、调整请求策略只需改这里。

配置 (college teacher/.env):
    PROXY_URL=http://127.0.0.1:7890    # 静态代理地址，空则直连
    PROXY_MODE=off                     # off=直连 | static=静态代理（预留 redis=代理池）

用法:
    from http_client import get_client
    client = get_client()
    html = client.fetch_text("https://example.edu.cn/page.htm")
    ok = client.download_binary("https://example.edu.cn/x.pdf", Path("x.pdf"))
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

# ── 配置加载 ──
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
try:
    from dotenv import load_dotenv
    _shell_proxy_mode = os.environ.get("PROXY_MODE")  # shell 显式设置优先（download_loop.sh PROXY_MODE=off）
    load_dotenv(_ENV_FILE, override=True)  # .env 强制覆盖已有环境变量
    if _shell_proxy_mode is not None:
        os.environ["PROXY_MODE"] = _shell_proxy_mode  # 防 .env 覆盖 shell（2026-09-03: 快代理失效期全直连）
except Exception:
    pass

PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXY_MODE = os.environ.get("PROXY_MODE", "off").strip()


def _mask_proxy_url(url: str) -> str:
    """日志脱敏: 去掉代理 URL 中的账号密码部分。

    快代理 URL 形如 http://user:pass@host:port/ —— 若原样打印,
    凭证会进日志/报告/入库（2026-08-23 第3轮实测 5 个 spider_run.log
    泄漏代理账号密码）。只保留 scheme://host:port。
    """
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        if p.username or p.password:
            host = p.hostname or ""
            if p.port:
                host += f":{p.port}"
            return urlunsplit((p.scheme, host, p.path, p.query, p.fragment))
    except Exception:
        pass
    return url

# ── 代理失效熔断 ──
# 首次判定代理失效后 _PROXY_FAILURE_TTL 秒内直接直连(不再带死代理白等),
# 到期后重新尝试代理(隧道可能已恢复)。
_PROXY_FAILED_AT = 0.0
_PROXY_FAILURE_TTL = 60.0

# ── 代理慢速熔断 (2026-08-27) ──
# 代理"可用但慢"（数据中心出口高峰期常见）时, 每请求都白等 timeout 秒,
# 人均耗时被拉满且无任何异常可捕获。连续 _PROXY_SLOW_LIMIT 次代理耗时
# > _PROXY_SLOW_THRESHOLD → 复用失败熔断（_PROXY_FAILED_AT）直连
# _PROXY_FAILURE_TTL 秒, 到期自动恢复代理。
_PROXY_SLOW_COUNT = 0
_PROXY_SLOW_LIMIT = int(os.environ.get("PROXY_SLOW_LIMIT", "3"))
_PROXY_SLOW_THRESHOLD = float(os.environ.get("PROXY_SLOW_THRESHOLD", "15"))


def _proxy_slow_note(elapsed: float):
    """代理请求耗时观测: 慢 → 计数; 快 → 清零; 达限 → 熔断直连。"""
    global _PROXY_SLOW_COUNT, _PROXY_FAILED_AT
    if elapsed > _PROXY_SLOW_THRESHOLD:
        _PROXY_SLOW_COUNT += 1
        if _PROXY_SLOW_COUNT >= _PROXY_SLOW_LIMIT:
            _PROXY_FAILED_AT = time.time()
            _PROXY_SLOW_COUNT = 0
    else:
        _PROXY_SLOW_COUNT = 0


def _proxy_fused() -> bool:
    """代理熔断中 → 本次请求应直连"""
    if not _PROXY_FAILED_AT:
        return False
    if time.time() - _PROXY_FAILED_AT >= _PROXY_FAILURE_TTL:
        return False  # 熔断到期, 重新尝试代理
    return True

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


def _build_proxies(host: str = "") -> Optional[dict]:
    """按 PROXY_MODE 构建代理字典。
    dps(2026-10-24 私密代理): 站级粘性 — 同域名固定绑定一个提取IP,
    瑞数 cookie-IP 绑定兼容(隧道漂移不可用的替代方案)。"""
    if PROXY_MODE in ("off", "") or not PROXY_URL:
        return None
    if PROXY_MODE == "static":
        return {"http": PROXY_URL, "https": PROXY_URL}
    if PROXY_MODE == "dps":
        try:
            from dps_proxy import get_proxies_for_host
            return get_proxies_for_host(host) if host else None
        except Exception:
            return None
    # 预留: redis 动态代理池（util/proxy.py ProxyManager）
    return {"http": PROXY_URL, "https": PROXY_URL}


# ── 代理出口被 WAF 拦截 → 直连兜底 ──
# 快代理出口多为数据中心 IP, 部分瑞数/WAF 站点直接 412 拦截(JS 挑战页)。
# 此时整条降级链带同一代理会白跑一遍 (2026-08-23 第3轮实测:
# 兰大/南师大 代理 412 / 直连 200, 本地宽带 IP 不被瑞数拉黑)。
# 检测到代理响应瑞数码 → 直连重试一次, 成功则缓存域名,
# 后续同域名请求直接直连, 不再浪费一次代理请求。
_RUISHU_BLOCK_CODES = frozenset({202, 412, 452})
_DIRECT_OK_HOSTS: set = set()  # 直连可用域名 (跨请求/跨模块共享)
# 2026-08-24 直连优先: 直连失败但代理成功的域名 → 永久走代理(不重复直连超时)。
_PROXY_OK_HOSTS: set = set()
# 直连尝试超时(秒): 直连快就省代理一跳; 超时/失败即切代理, 不白等。
_DIRECT_TIMEOUT = float(os.environ.get("DIRECT_TIMEOUT", "8"))
# 2026-08-24 用户指令自适应: 直连连续失败 DIRECT_FAIL_LIMIT 次 → 域名自主转
# 代理优先(_PROXY_OK_HOSTS), 不再每次重复白试直连(本地网络不通时每请求省 8s)。
_DIRECT_FAILS: dict = {}  # host -> 直连连续失败次数
_DIRECT_FAIL_LIMIT = int(os.environ.get("DIRECT_FAIL_LIMIT", "3"))
# 2026-09-24 系统 curl 兜底: curl_cffi/requests 的 TLS 指纹被 WAF 整类拦截
# (电子科大 smee 实测: 指纹请求 20s 超时, 系统 curl 0.1s 返回) 的站点,
# 用系统 curl 子进程取回正文。成功即记住域名, 后续请求直达系统 curl。
_SYSCURL_OK_HOSTS: set = set()


def _host_of_url(url: str) -> str:
    """URL 的 hostname (小写), 解析失败返回空串。"""
    try:
        from urllib.parse import urlsplit
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _is_proxy_failure(e: BaseException) -> bool:
    """判定异常是否为"代理失效"(连接代理失败/407 认证失败/代理相关错误)。

    代理失效时整条降级链都带同一代理, 不判定的话所有级别白跑一遍仍失败;
    判定命中后调用方应去掉代理直连重试一次(本地网络兜底)。
    命中同时记录熔断时间(见 _proxy_fused), 后续请求短时直接走直连。
    目标站点自身的 4xx/5xx 不属此类, 不做直连重试。
    """
    global _PROXY_FAILED_AT
    import requests
    try:
        from curl_cffi.requests.exceptions import ProxyError as _CffiProxyError
    except Exception:
        _CffiProxyError = ()
    hit = False
    if isinstance(e, (requests.exceptions.ProxyError, _CffiProxyError)):
        hit = True
    name, msg = type(e).__name__.lower(), str(e).lower()
    if not hit and ("proxy" in name or "407" in msg):
        hit = True
    if not hit and ("proxy" in msg and ("connect" in msg or "tunnel" in msg or "refused" in msg)):
        hit = True
    # 2026-08-24: curl_cffi 连不上代理可能抛 ConnectionError(非 ProxyError),
    # msg 不含 "proxy" — 连接级失败一律按代理失效处理 (retry 满 3 次)
    # 2026-09-03: 快代理 517 (CONNECT tunnel failed, response 517, 套餐到期/
    # 限流确定性拒绝) 漏判 — msg 无 "proxy" 字样, 且 "CONNECT tunnel failed"
    # 不匹配 "connect failed" 子串 (中间夹 " tunnel ")。curl 56 (Connection
    # closed abruptly) 同为快代理到期拒绝形态, 一并识别。漏判 → 每次请求
    # 全降级链白跑 + 渲染兜底浏览器链卡死整系 8 分钟。识别并熔断直连。
    # 注: 调用方均在"代理路径"的 except 内 (直连路径不调本函数), 安全。
    if not hit and ("connection refused" in msg or "connect failed" in msg
                    or "connection reset" in msg or "couldn't connect" in msg
                    or "tunnel failed" in msg or "response 517" in msg
                    or "response 460" in msg or "response 461" in msg
                    or "connection closed abruptly" in msg
                    or "failure when receiving data" in msg
                    or "recv error" in msg):
        hit = True
    if hit:
        _PROXY_FAILED_AT = time.time()  # 记录熔断起点
    return hit


# ═══════════════════════════════════════════════════════════════
#  结果校验（错误页/拦截页统一在此过滤）
# ═══════════════════════════════════════════════════════════════

def is_error_page(html: str) -> bool:
    """识别软 404 / 错误页（HTTP 200 但内容是错误提示）。这类结果不保存。

    DNS/连接错误页（Chrome 错误页: 东北师大 js.nenu.edu.cn NXDOMAIN 教训）
    是大页面（~187KB），不能只用短页面特征判断——浏览器错误页特征词
    特异性极高（真实教师页不可能含 ERR_NAME_NOT_RESOLVED / 找不到...的
    服务器 IP 地址），可跨长度直接判定。
    """
    soup = BeautifulSoup(html, "lxml")
    title = soup.find("title")
    t = title.get_text(strip=True) if title else ""

    # 标题直接含 404 / Not Found
    if re.search(r"404|not\s*found", t, re.I):
        return True

    # 浏览器 DNS/连接错误页（Chrome/Edge 生成，任何长度都判定）
    if re.search(
        r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED|ERR_CONNECTION_TIMED_OUT"
        r"|ERR_SSL_PROTOCOL_ERROR|ERR_EMPTY_RESPONSE|ERR_INTERNET_DISCONNECTED"
        r"|找不到.{0,30}的?服务器 IP 地址|无法访问此网站|此网站无法访问|"
        r"请检查代理服务器|DNS 配置|检查您的网络连接|No such host is known|"
        r"Host is unreachable|couldn't resolve host",
        t + " " + soup.get_text(" ", strip=True)[:500],
        re.I,
    ):
        return True

    # 短页面 + 错误特征词（教师详情页正常都 > 1000 字）
    text = soup.get_text(" ", strip=True)
    if len(html) < 3000 and re.search(
        r"页面(不|未)?存在|找不到该页面|无法访问|访问出错|错误提示|错误码|链接已失效",
        text,
    ):
        return True
    if re.search(r"错误码[:：]?\s*(404|500|502|503)", text):
        return True
    return False


def is_ruishu_challenge(html: str) -> bool:
    """瑞数 JS 挑战拦截页特征。

    2026-09-03 北邮 teacher.bupt.edu.cn 教训: 瑞数保护站解开后的真实页面
    (48KB, 含 <title> 与正文) 头部同样引用 $_ts.nsd / document.cookie
    （页面自带瑞数 JS）——仅凭标记出现即滤会把整站内容当挑战页丢弃
    （北邮卓越工程师学院 177 教师全 fetch 失败、误判死系）。
    判别升级: 挑战页为纯混淆 JS（~2.5KB）无真实 <title>；解开页有真实
    <title>。有真实标题即视为已解开的正常内容页。
    """
    if len(html) < 500:
        return False
    has_marker = ("$_ts.nsd" in html[:2000]) or ("document.cookie" in html[:1500])
    if not has_marker:
        return False
    m = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", html, re.I | re.S)
    return not (m and m.group(1).strip())


def ok_html(html: Optional[str]) -> Optional[str]:
    """校验 HTML 有效性：非空、非拦截页、非错误页。"""
    if not html or len(html) <= 500:
        return None
    if is_ruishu_challenge(html):
        return None
    if is_error_page(html):
        return None
    return html


def is_binary_ok(data: bytes) -> bool:
    """校验二进制内容：拒绝 404 错误页等 HTML 伪装成 PDF/图片的响应。"""
    if not data or len(data) <= 100:
        return False
    head = data[:500].lstrip().lower()
    if head.startswith(b"<html") or head.startswith(b"<!doctype"):
        return False
    return True


# ═══════════════════════════════════════════════════════════════
#  UnifiedClient
# ═══════════════════════════════════════════════════════════════

class UnifiedClient:
    """统一请求客户端（单例）。

    fetch_text 降级链:
      1. curl_cffi 直连（快路径，代理可选）
      2. RsSpider（瑞数 Cookie 缓存 + curl_cffi，代理可选）
      3. smart_fetch 四级降级（curl_cffi → sdenv → DrissionPage → CDP Chrome）
      4. requests（最后兜底）
    """

    _instance: Optional["UnifiedClient"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.proxies = _build_proxies()
        # 最近一次请求的 HTTP 状态码（0=无响应/异常）。
        # 供调用方归因: 404/403 是"URL 失效/被拒"(北大哲学系上游
        # URL 错层全 404), 0 是"网络/超时/连接失败"——两者处理方式
        # 完全不同, 必须有留痕才能区分。
        self.last_status = 0

        # requests session（带代理）
        import requests
        from requests.adapters import HTTPAdapter
        import ssl as _ssl
        import urllib3

        urllib3.disable_warnings()

        class _InsecureAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
                kwargs["ssl_context"] = ctx
                return super().init_poolmanager(*args, **kwargs)

        self._session = requests.Session()
        self._session.mount("https://", _InsecureAdapter())
        self._session.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._session.verify = False
        self._session.trust_env = False
        if self.proxies:
            self._session.proxies = self.proxies

        # curl_cffi
        self._curl_requests = None
        try:
            import curl_cffi.requests as _cr
            self._curl_requests = _cr
        except ImportError:
            pass

        # RsSpider（懒加载）
        self._rs_spider = None

        if self.proxies:
            print(f"🌐 代理已启用: {_mask_proxy_url(PROXY_URL)} (mode={PROXY_MODE})")

    def _get_rs_spider(self, timeout: int = 30, url: str = ""):
        if self._rs_spider is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from handleRuishu import RsSpider
            # 2026-10-24 dps 粘性代理: RsSpider 按目标站点取绑定 IP(瑞数 cookie
            # 与该 IP 绑定, 粘性保证 cookie 不失效 — 隧道漂移的替代方案)
            px = self._host_proxies(_host_of_url(url)) if (PROXY_MODE == "dps" and url) else self.proxies
            self._rs_spider = RsSpider(timeout=timeout, proxies=px)
        return self._rs_spider

    def _host_proxies(self, host: str) -> Optional[dict]:
        """dps 模式按 host 取站级粘性代理; 其他模式返回全局 self.proxies。"""
        if PROXY_MODE == "dps" and host:
            return _build_proxies(host)
        return self.proxies

    # ── 文本请求 ──
    def resolve_redirect(self, url: str, referer: str = "", timeout: int = 10) -> Optional[str]:
        """解析跳转目标（不跟随 302，返回 Location）。

        用于搜索引擎加密跳转（部分引擎的 /link?url= 跳转需带 Referer 才 302 到真实页）。
        经统一代理/降级链的 curl_cffi 快路径。
        """
        headers = {"Referer": referer} if referer else {}
        try:
            if self._curl_requests is not None:
                r = self._curl_requests.get(
                    url, timeout=timeout, verify=False, allow_redirects=False,
                    headers={**dict(self._session.headers), **headers},
                    impersonate="chrome124",
                    proxies=None if _proxy_fused() else self._host_proxies(host),  # 熔断期直连
                )
                loc = r.headers.get("Location") or r.headers.get("location")
                if loc:
                    return loc
        except Exception as e:
            if self.proxies and _is_proxy_failure(e):
                try:
                    r = self._curl_requests.get(
                        url, timeout=timeout, verify=False, allow_redirects=False,
                        headers={**dict(self._session.headers), **headers},
                        impersonate="chrome124", proxies=None,  # 代理失效 → 本地直连
                    )
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if loc:
                        return loc
                except Exception:
                    pass
        try:
            r = self._session.get(url, timeout=timeout, allow_redirects=False,
                                  headers=headers)
            loc = r.headers.get("Location") or r.headers.get("location")
            if loc:
                return loc
        except Exception as e:
            if self.proxies and _is_proxy_failure(e) and self._curl_requests is not None:
                try:
                    r = self._curl_requests.get(
                        url, timeout=timeout, verify=False, allow_redirects=False,
                        headers={**dict(self._session.headers), **headers},
                        impersonate="chrome124", proxies=None,  # 代理失效 → 本地直连
                    )
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if loc:
                        return loc
                except Exception:
                    pass
        return None

    def fetch_text_rendered(self, url: str, timeout: int = 30) -> Optional[str]:
        """浏览器渲染抓取（RsSpider 的 DrissionPage/Playwright 路径）。

        用于详情页空壳站（北大类）: 静态抓取只有壳，内容 JS 渲染。
        代价高（秒级浏览器），调用方必须做预算/熔断控制。
        """
        try:
            spider = self._get_rs_spider(max(timeout, 30))
            resp = spider.get(url)
            if resp.status_code == 200:
                html = ok_html(resp.text)
                if html:
                    return html
        except Exception as e:
            # 代理失效 → RsSpider 直连再试一次 (本地网络兜底)
            if self.proxies and _is_proxy_failure(e):
                try:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from handleRuishu import RsSpider
                    spider = RsSpider(timeout=max(timeout, 30), proxies=None)
                    resp = spider.get(url)
                    if resp.status_code == 200:
                        html = ok_html(resp.text)
                        if html:
                            return html
                except Exception:
                    pass
        return None

    def _fetch_syscurl(self, url: str, timeout: int) -> Optional[str]:
        """系统 curl 兜底 (2026-09-24): curl_cffi/requests 的 TLS 指纹被
        WAF 整类拦截、但系统 curl 可达的站点（电子科大 smee 实测
        指纹 20s 超时 vs 系统 curl 0.1s）。"""
        t = max(timeout, 12)
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(t), "--compressed",
             "-A", ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
             url],
            capture_output=True, timeout=t + 5)
        if r.returncode != 0 or not r.stdout:
            return None
        return ok_html(r.stdout.decode("utf-8", errors="ignore"))

    def fetch_text(self, url: str, timeout: int = 20) -> Optional[str]:
        """GET 文本页面，返回有效 HTML 或 None。"""
        global _PROXY_FAILED_AT  # 441/代理失效熔断: 写入模块级, 供 _proxy_fused() 读取
        host = _host_of_url(url)
        # 0) 系统 curl 快捷路径: 该域名曾被指纹拦截、系统 curl 可达 → 直达
        if host in _SYSCURL_OK_HOSTS:
            html = self._fetch_syscurl(url, timeout)
            if html:
                return html
        # 1) curl_cffi 快路径 — 2026-08-24 用户指令降级链:
        #    代理(失败重试≤3) → 本地直连 → 上层 RsSpider/CDP 浏览器。
        #    瑞数判断贯穿 (ok_html 已滤瑞数挑战页); 代理 441(快代理拒绝)
        #    3 次全挂 → 熔断, 窗口期跳过代理。
        if self._curl_requests is not None:
            host = _host_of_url(url)
            # 直连优先 (2026-08-24 用户指令): 本地网络快, 代理多一跳绕路慢。
            # 双向记忆: _DIRECT_OK_HOSTS → 永久直连; _PROXY_OK_HOSTS → 直接代理。
            # 未定域名的站点: 先直连(DIRECT_TIMEOUT 快超时), 失败/被WAF拦再走代理;
            # 直连连续失败 DIRECT_FAIL_LIMIT 次 → 自主转代理优先(用户指令)。
            direct_only = host in _DIRECT_OK_HOSTS or _proxy_fused()
            if not direct_only and host not in _PROXY_OK_HOSTS:
                try:
                    r = self._curl_requests.get(
                        url, timeout=min(timeout, _DIRECT_TIMEOUT), verify=False,
                        headers=dict(self._session.headers),
                        impersonate="chrome124", proxies=None)
                    self.last_status = r.status_code
                    if r.status_code == 200:
                        html = ok_html(r.text)
                        if html:
                            _DIRECT_OK_HOSTS.add(host)
                            _DIRECT_FAILS.pop(host, None)
                            return html
                    if r.status_code in (404, 410, 400):
                        # 2026-09-28 死链早退: 400/404/410 确定性失败(地大北京
                        # scholarDetail 旧路径、中南 202.197.61.x/bk/?id= 老系统
                        # 实测), 后续代理/浏览器/syscurl 各后端对同一死 URL
                        # 结果一致——不再白烧降级链。
                        return None
                except Exception:
                    pass  # 直连失败 → 落入代理路径
                # 直连失败自适应: 连续 N 次 → 该域名代理优先
                n = _DIRECT_FAILS.get(host, 0) + 1
                if n >= _DIRECT_FAIL_LIMIT:
                    _PROXY_OK_HOSTS.add(host)
                    _DIRECT_FAILS.pop(host, None)
                else:
                    _DIRECT_FAILS[host] = n
            # 代理路径 (直连失败 / _PROXY_OK_HOSTS 域名; 熔断期跳过)
            if not direct_only and self.proxies and not _proxy_fused():
                saw_441 = False
                for _attempt in range(2):
                    try:
                        _t0 = time.time()
                        r = self._curl_requests.get(
                            url, timeout=timeout, verify=False,
                            headers=dict(self._session.headers),
                            impersonate="chrome124", proxies=self._host_proxies(host))
                        _proxy_slow_note(time.time() - _t0)  # 慢速熔断观测
                        self.last_status = r.status_code
                        if r.status_code == 200:
                            html = ok_html(r.text)
                            if html:
                                _PROXY_OK_HOSTS.add(host)
                                return html
                        if r.status_code == 441:
                            saw_441 = True  # 快代理拒绝: 重试满 2 次, 全挂熔断
                            continue
                        break  # 站端 4xx/5xx/瑞数(ok_html 已滤): retry 无意义
                    except Exception as e:
                        if not _is_proxy_failure(e):
                            break  # 非代理错误 → 停止重试
                if saw_441:
                    _PROXY_FAILED_AT = time.time()
            # 直连兜底 (代理全挂 / 熔断期 / 无代理配置)
            try:
                r = self._curl_requests.get(
                    url, timeout=timeout, verify=False,
                    headers=dict(self._session.headers),
                    impersonate="chrome124", proxies=None)
                self.last_status = r.status_code
                if r.status_code == 200:
                    html = ok_html(r.text)
                    if html:
                        _DIRECT_OK_HOSTS.add(host)
                        return html
            except Exception:
                pass

        # 2) RsSpider（瑞数 Cookie 缓存）
        try:
            spider = self._get_rs_spider(max(timeout, 30))
            resp = spider.get(url)
            self.last_status = resp.status_code
            if resp.status_code == 200:
                html = ok_html(resp.text)
                if html:
                    return html
        except Exception:
            pass

        # 3) smart_fetch 四级降级
        try:
            # 2026-09-29 修复: smart_fetch 内部有 util.xxx 绝对导入
            # (loader_challenge/fingerprint_chain), 需要 util 的父目录
            # 在 sys.path——原实现只插了 util 自身, iv8/AES 壳破解层
            # 一直因 "No module named 'util'" 静默失效!
            _here = Path(__file__).resolve().parent
            sys.path.insert(0, str(_here))
            sys.path.insert(0, str(_here.parent))
            from smart_fetch import smart_fetch
            html, _method = smart_fetch(url, timeout=timeout)
            html = ok_html(html)
            if html:
                return html
        except Exception:
            pass

        # 4) requests 兜底
        try:
            r = self._session.get(url, timeout=timeout, allow_redirects=True)
            self.last_status = r.status_code
            if r.status_code == 200:
                if not r.encoding or r.encoding == "ISO-8859-1":
                    r.encoding = r.apparent_encoding or "utf-8"
                html = ok_html(r.text)
                if html:
                    return html
        except Exception as e:
            # 代理失效 → curl_cffi 直连兜底 (requests Session 带代理, 无法单次清空)
            if self.proxies and _is_proxy_failure(e) and self._curl_requests is not None:
                try:
                    r = self._curl_requests.get(
                        url, timeout=timeout, verify=False,
                        headers=dict(self._session.headers),
                        impersonate="chrome124",
                        proxies=None,  # 去掉代理直连
                    )
                    self.last_status = r.status_code
                    if r.status_code == 200:
                        html = ok_html(r.text)
                        if html:
                            return html
                except Exception:
                    pass

        # 5) 系统 curl 兜底 (2026-09-24): TLS 指纹被拦但系统 curl 可达的站点
        try:
            html = self._fetch_syscurl(url, timeout)
            if html:
                _SYSCURL_OK_HOSTS.add(host)
                return html
        except Exception:
            pass

        return None

    # ── 二进制下载 ──
    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """原子落盘: 写唯一临时文件再 os.replace。

        并发语义: 多线程/多进程同时下载同一路径时，最后完成的"完整文件"
        生效，任何时刻该路径上的文件都是完整的（读者只会看到旧完整版或
        新完整版）。直接 write_bytes 的话，进程被杀会留下半截文件——
        而半截 PDF 会被断点复用当有效文件信任，是最难排查的脏数据。

        临时名用 mkstemp（内核保证唯一）: 曾用 <pid>.part，但同进程的
        并发线程共用 pid 会撞同一临时文件（8 线程竞态实测 FileNotFound）。
        """
        import tempfile

        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f"{path.name}.", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            Path(tmp_name).replace(path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def download_binary(self, url: str, save_path: Path, timeout: int = 30) -> bool:
        """下载二进制资源（PDF/图片）。HTML 错误页不保存。

        顺序: curl_cffi 快路径（多数站点）→ RsSpider（瑞数站点）→ requests。
        落盘为原子写（见 _atomic_write），配合 DetailScraper 复用前的
        %PDF 校验，正常流程不可能留下半截/错误页 PDF。
        """
        global _PROXY_FAILED_AT  # 441/代理失效熔断: 写入模块级, 供 _proxy_fused() 读取
        # 1) curl_cffi 快路径（非瑞数站点秒回，避免无谓的 cookie 生成）
        #    2026-08-24 用户指令降级链: 代理(重试≤3) → 本地直连 (同 fetch_text);
        #    瑞数/错误页由 is_binary_ok + 下层处理; 代理 441 → 熔断。
        if self._curl_requests is not None:
            host = _host_of_url(url)
            direct = host in _DIRECT_OK_HOSTS or _proxy_fused()
            if not direct:
                saw_441 = False
                for _attempt in range(3):
                    try:
                        r2 = self._curl_requests.get(
                            url, timeout=timeout, verify=False,
                            impersonate="chrome124",
                            proxies=self._host_proxies(host),
                        )
                        if r2.status_code == 200 and is_binary_ok(r2.content):
                            self._atomic_write(save_path, r2.content)
                            return True
                        if r2.status_code == 441:
                            saw_441 = True  # 快代理拒绝: 重试满 3 次, 全挂熔断
                            continue
                        break  # 站端错误: retry 无意义
                    except Exception as e:
                        if not _is_proxy_failure(e):
                            break
                if saw_441:
                    _PROXY_FAILED_AT = time.time()
            # 本地直连兜底 (代理 3 次失败 / 直连缓存域名 / 熔断期)
            try:
                r2 = self._curl_requests.get(
                    url, timeout=timeout, verify=False,
                    impersonate="chrome124",
                    proxies=None,
                )
                if r2.status_code == 200 and is_binary_ok(r2.content):
                    _DIRECT_OK_HOSTS.add(host)
                    self._atomic_write(save_path, r2.content)
                    return True
            except Exception:
                pass

        # 2) RsSpider（瑞数 Cookie + 代理，403/412 站点）
        try:
            spider = self._get_rs_spider(max(timeout, 30))
            resp = spider.get(url)
            if resp.status_code == 200 and is_binary_ok(resp.content):
                self._atomic_write(save_path, resp.content)
                return True
        except Exception:
            pass

        # 3) requests session
        try:
            r = self._session.get(url, timeout=timeout, stream=True)
            if r.status_code == 200 and is_binary_ok(r.content):
                self._atomic_write(save_path, r.content)
                return True
        except Exception as e:
            # 代理失效 → curl_cffi 直连兜底
            if self.proxies and _is_proxy_failure(e) and self._curl_requests is not None:
                try:
                    r2 = self._curl_requests.get(
                        url, timeout=timeout, verify=False,
                        impersonate="chrome124",
                        proxies=None,  # 去掉代理直连
                    )
                    if r2.status_code == 200 and is_binary_ok(r2.content):
                        self._atomic_write(save_path, r2.content)
                        return True
                except Exception:
                    pass

        return False


def get_client() -> UnifiedClient:
    """获取统一客户端单例。"""
    return UnifiedClient()


if __name__ == "__main__":
    client = get_client()
    html = client.fetch_text("https://example.com")
    print(f"example: {len(html) if html else 0} chars")
