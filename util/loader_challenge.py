#!/usr/bin/env python3
"""JS loader 挑战纯 HTTP 破解 (2026-08-31 逆向西交/北理工系站"正在加载"壳)。

壳页 JS 完整逆向结论: 挑战是**硬编码的算术题**, 无 VM 无瑞数:
  - challengeId/a/b/operator 内嵌壳 JS(逐页提取)
  - answer = a op b; hash = simpleHash(challengeId + answer + UA前10字符)
    simpleHash = JS 经典 ((h<<5)-h+c) & 32bit 回绕, 结尾 Math.abs
  - POST {origin}/dynamic_challenge {challenge_id, answer, browser_info, hash}
    → {success, client_id} → 设 cookie client_id=... → GET 目标路径 = 真内容
纯 HTTP 即可完成, 无需浏览器/sdenv/node — 内存零成本。
"""
import json
import re
from urllib.parse import urljoin

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

_CHALLENGE_RE = re.compile(
    r"challengeId\s*=\s*'([^']+)'[\s\S]*?"
    r"var\s+a\s*=\s*(\d+)[\s\S]*?"
    r"var\s+b\s*=\s*(\d+)[\s\S]*?"
    r"var\s+operator\s*=\s*'([^']+)'")
_RAWPATH_RE = re.compile(r'rawPath\s*=\s*"([^"]+)"')
_IS_SHELL_RE = re.compile(r"网站正在加载中|class=\"loader\"|dynamic_challenge|content_container")

# ── AES-GCM 壳破解 (2026-09-15 中南财经政法 ggglxy.zuel 实锤) ──
# 新变体: 无算术挑战, 壳 JS 内嵌 AES-GCM 密文 + key, 浏览器端 crypto.subtle
# 解密后填充 content_container。逆向结论(纯 CPU 可解):
#   var k="..."(16字节key种子); txutu="..."(base64 密文)
#   fkb[16]: fkb[x%16] ^= UTF8(k)[x]  (XOR 折叠)
#   buf = atob(txutu); iv=buf[:12]; dat=buf[12:]
#   AESGCM(fkb).decrypt(iv, dat) → 真实 HTML
# 无头检测(webdriver/WebGL 虚拟机渲染器)只影响浏览器内 key 污染(_tox_),
# 我们直接读初始 k — 与真实浏览器解密结果一致。
# JS 原形: var i="...";j="...";k="..." (仅首个带 var, 后续裸赋值);
# 2026-09-15 实测每次请求 key 值随机化(变量名 i/j/k 稳定, 值随机)
_AES_KEY_RE = re.compile(r'var\s+(\w+)\s*=\s*"([A-Za-z0-9]{15,24})"\s*;\s*'
                         r'(\w+)\s*=\s*"([A-Za-z0-9]{15,24})"\s*;\s*'
                         r'(\w+)\s*=\s*"([A-Za-z0-9]{15,24})"')
# 密文变量名随机化(beUTg/txutu...): atob(变量) 反查变量赋值
_AES_ATOB_RE = re.compile(r'atob\((\w+)\)')
_AES_CT_NAME_RE = re.compile(r'\b{name}\s*=\s*"([A-Za-z0-9+/=]{{200,}})"')
_AES_MARK_RE = re.compile(r"AES-GCM[\s\S]{0,400}?atob\(|content_container")


def crack_aes_gcm_shell(html: str) -> str:
    """AES-GCM 壳纯 CPU 解密 → 真实 HTML; 非 AES 壳/解密失败返回 None。"""
    if not html or not _AES_MARK_RE.search(html[:8000]):
        return None
    m = _AES_KEY_RE.search(html)
    if not m:
        return None
    # 密文: 变量名随机化(beUTg/txutu...) — atob(变量) 反查变量赋值
    ma = _AES_ATOB_RE.search(html)
    ct = None
    if ma:
        _ct_re = re.compile(r'\b' + re.escape(ma.group(1)) +
                            r'\s*=\s*"([A-Za-z0-9+/=]{200,})"')
        mct = _ct_re.search(html)
        ct = mct.group(1) if mct else None
    if not ct:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        # key 候选: k 优先(JS 语义 dIOwA=k), 其余按顺序兜底; 逐一尝试
        keys = [(m.group(i), m.group(i + 1)) for i in (1, 3, 5)]
        keys.sort(key=lambda kv: 0 if kv[0] == "k" else 1)
        for _vn, k in keys:
            kb = k.encode("utf-8")
            fkb = bytearray(16)
            for x, b in enumerate(kb):
                fkb[x % 16] ^= b
            buf = base64.b64decode(mct.group(1))
            iv, dat = buf[:12], buf[12:]
            try:
                dec = AESGCM(bytes(fkb)).decrypt(iv, dat, None)
            except Exception:
                continue
            text = dec.decode("utf-8", errors="replace")
            if len(text) > 800 and "<" in text:
                return text
    except Exception:
        pass
    return None


def _simple_hash(s: str) -> int:
    """JS simpleHash 语义: ((h<<5)-h+c) 循环, h&h 触发 32bit 有符号回绕, 末尾 abs。"""
    h = 0
    for ch in s:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return abs(h)


def is_shell(html: str) -> bool:
    return bool(html) and bool(_IS_SHELL_RE.search(html[:6000]))


def loader_pass(session, url: str) -> str:
    """执行 loader 挑战, 成功后返回目标页真实内容; 失败抛异常。"""
    import curl_cffi.requests as cr
    if session is None:
        session = cr.Session()

    # 1) GET 壳页(会话级收 sid/uv_id cookie)
    r = session.get(url, headers={"User-Agent": _UA}, impersonate="chrome110",
                    timeout=20)
    shell = r.text
    if not is_shell(shell):
        raise RuntimeError(f"非壳页(HTTP {r.status_code}, {len(shell)}B)")

    # 2) 提取挑战参数
    m = _CHALLENGE_RE.search(shell)
    if not m:
        raise RuntimeError("挑战参数提取失败")
    challenge_id, a, b, op = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    if op == "+":
        answer = a + b
    elif op == "-":
        answer = a - b
    else:
        answer = a * b
    hash_value = _simple_hash(challenge_id + str(answer) + _UA[:10])

    # 3) POST 验证 → client_id cookie
    payload = {
        "challenge_id": challenge_id,
        "answer": answer,
        "browser_info": {
            "userAgent": _UA,
            "language": "zh-CN",
            "platform": "MacIntel",
            "screen": {"width": 1440, "height": 900, "colorDepth": 24},
            "timezoneOffset": -480,
            "hasTouchEvents": False,
        },
        "hash": hash_value,
    }
    origin = url[:url.find("/", url.find("//") + 2)]
    r2 = session.post(origin + "/dynamic_challenge", json=payload,
                      headers={"User-Agent": _UA, "Content-Type": "application/json",
                               "Referer": url},
                      impersonate="chrome110", timeout=20)
    if r2.status_code != 200:
        raise RuntimeError(f"挑战 POST {r2.status_code}")
    data = r2.json()
    if not data.get("success") or not data.get("client_id"):
        raise RuntimeError(f"挑战未通过: {str(data)[:120]}")
    session.cookies.set("client_id", str(data["client_id"]))

    # 4) 目标路径(壳内 rawPath 解码, 缺省取原路径)
    mp = _RAWPATH_RE.search(shell)
    target = url
    if mp:
        try:
            import urllib.parse
            target = urljoin(origin + "/", urllib.parse.unquote(mp.group(1)))
        except Exception:
            pass

    r3 = session.get(target, headers={"User-Agent": _UA, "Referer": url},
                     impersonate="chrome110", timeout=25)
    if r3.status_code == 200 and len(r3.text) > 8000 and not is_shell(r3.text):
        return r3.text
    raise RuntimeError(f"目标页获取失败(HTTP {r3.status_code}, {len(r3.text)}B)")


def fetch(url: str, timeout: int = 60) -> str:
    """无浏览器抓取: AES-GCM 壳纯CPU解密 → 算术挑战 → 真内容; 全程纯 HTTP。
    2026-09-15 中南财经政法 ggglxy.zuel 实锤: 全站 AES-GCM 壳 WAF(每个页面
    都是壳), 算术挑战不适用 — 先试 AES 解密再走 loader_pass。失败返回 None。"""
    try:
        import curl_cffi.requests as cr
        session = cr.Session()
        r = session.get(url, headers={"User-Agent": _UA}, impersonate="chrome110",
                        timeout=20)
        if r.status_code == 200 and r.text:
            # ① AES-GCM 壳(优先: 零挑战往返, 一次解密)
            cracked = crack_aes_gcm_shell(r.text)
            if cracked:
                return cracked
            # ② 算术挑战型(西交/北理工系站)
            if is_shell(r.text):
                return loader_pass(session, url)
            return r.text
    except Exception:
        pass
    return None
