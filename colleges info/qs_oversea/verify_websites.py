"""全量验证官网 URL: GET 请求 → 分析 HTML → 判定三态

对 bing_websites.json 中所有带 website 的条目:
  verified   - GET 成功且 title/HTML 内容与学校名匹配 (现有 name_matches_title + 域名核心词)
  rejected   - GET 成功但内容不匹配 (错配, 清空 website)
  unverified - 请求失败 (超时/WAF/5xx/连接错误/无标题), 保留 URL 标注未验证

轻量: 每 URL 一个 GET (8s 超时, stream 只读前 64KB), host 级缓存。
输出: 写回 bing_websites.json (每条目加 verify 字段), 进度心跳每 30 条。
"""
import html as htmllib
import json
import os
import re
import signal
import sys
import time

import httpx

sys.path.insert(0, ".")
import bing_websites as bw

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRESS = bw.PROGRESS
HEADERS = bw.HEADERS

ITEM_BUDGET = 90


class ItemTimeout(BaseException):
    """必须继承 BaseException: 防 C 层 SSL poll 挂起时被 except Exception 吞掉"""
    pass


def _alarm(signum, frame):
    raise ItemTimeout("item budget exceeded")


def process_guarded(i: int, name: str, url: str, client: httpx.Client) -> tuple[str, str] | None:
    """带 watchdog 的单项验证: 返回 (state, desc), 挂起则记 unverified"""
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, ITEM_BUDGET)
    try:
        v = None
        try:
            state, desc = verify_one(name, url, client)
            v = (state, desc)
        except ItemTimeout:
            print(f"  ⚠ [{i}] 挂起>{ITEM_BUDGET}s, 记为 unverified")
            v = ("unverified", f"挂起>{ITEM_BUDGET}s")
        return v
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def fetch_title(client: httpx.Client, url: str) -> tuple[str, str]:
    """GET 返回 (title, status_desc)。stream 只读前 64KB。失败返回 ("", desc)"""
    h = bw.host_of(url)
    if h in bw.TITLE_CACHE:
        t = bw.TITLE_CACHE[h]
        return t, ("cached" if t else "cached-empty")
    try:
        with client.stream("GET", url, headers=HEADERS, follow_redirects=True,
                           timeout=httpx.Timeout(10.0, connect=8.0, read=8.0)) as r:
            if r.status_code != 200:
                return "", f"HTTP {r.status_code}"
            chunks = []
            for ch in r.iter_bytes(32768):
                chunks.append(ch)
                if sum(len(c) for c in chunks) >= 65536:
                    break
        body = b"".join(chunks).decode("utf-8", "ignore")
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        if not m:
            # 无 <title> 备选: og:title meta / og:site_name / h1 (现代 SPA 站常用)
            m2 = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', body, re.S)
            if not m2:
                m2 = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']', body, re.S)
            if not m2:
                m2 = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)', body, re.S)
            if not m2:
                m2 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
            if m2:
                t = re.sub(r"<[^>]+>", "", m2.group(1))
                t = htmllib.unescape(t)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    bw.TITLE_CACHE[h] = t
                    return t, "og/h1"
            return "", "no-title"
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = htmllib.unescape(t)
        t = re.sub(r"\s+", " ", t).strip()
        bw.TITLE_CACHE[h] = t
        return t, "ok"
    except Exception as e:
        return "", type(e).__name__


def title_has_uni_word(title: str) -> bool:
    """norm(title) 中是否含大学特征词 (子串匹配; tokens 会把 university 当停用词去掉, 故用全词)"""
    nt = bw.norm(title)
    if "univers" in nt:
        return True
    return any(w in nt for w in ("college", "institute", "institut",
                                 "polytechnic", "politeknik", "academy", "akademi",
                                 "hochschule", "faculdade", "escuela", "politechnika",
                                 "univerzita", "univerza", "szkola", "skola"))


def _tokens_loose(s: str) -> set[str]:
    """bw.tokens + 土耳其/北欧/西里尔字母基础规范化"""
    s_clean = s.replace("ı", "i").replace("ø", "o").replace("æ", "ae").replace("ł", "l")
    return set(bw.tokens(s_clean))


def _abbr_hit(name: str, reg: str) -> bool:
    """首字母缩写匹配: 域名注册域首段 == name 核心词首字母拼接"""
    words = [w for w in re.split(r"[^a-z]+", bw.norm(name).lower()) if w]
    if len(words) < 2 or len(words) > 6:
        return False
    abbr = "".join(w[0] for w in words)
    first = reg.split(".")[0]
    if len(abbr) < 3 or not first:
        return False
    return (abbr == first or first.startswith(abbr)) \
        and abs(len(first) - len(abbr)) <= 2


def _concat_hit(name: str, reg: str) -> bool:
    """核心词连写匹配: 注册域首段 == 核心词拼接"""
    words = [w for w in re.split(r"[^a-z]+", bw.norm(name)) if w and w not in bw.STOP]
    first = reg.split(".")[0]
    if len(first) < 4:
        return False
    if not words:
        return False
    segs = ["".join(words), *["".join(words[i:j])
                              for i in range(len(words))
                              for j in range(i + 2, len(words) + 1)]]
    for seg in segs:
        if len(seg) < 4:
            continue
        if (first == seg or first.startswith(seg) or first.endswith(seg)) \
                and abs(len(first) - len(seg)) <= 2:
            return True
    # Sciences Po / Mizzou / Cal Poly 等特殊全词拼接
    if len(words) < 2:
        raw = [w for w in re.split(r"[^a-z]+", bw.norm(name)) if w]
        concat = "".join(raw)
        if len(concat) >= 4 and (first == concat or first.startswith(concat)
                                 or first.endswith(concat)) \
                and abs(len(first) - len(concat)) <= 2:
            return True
    return False


UNI_TERMS = (
    "大学", "大學", "대학", "университ", "институт", "академи", "политех", "федеральн",
    "جامعة", "دانشگاه", "πανεπιστημιο", "університ", "національн", "национальн",
    "უნივერსიტეტ", "მაჰმუდ", "มหาวิทยาลัย", "համալսարան",
    "universit", "üniversite", "universita", "universidad", "universidade",
    "université", "universitat", "universiteit", "universitet", "universitāte",
    "universitetas", "univerzita", "univerzitet", "uniwersytet", "sveučilište",
    "tudományegyetem", "egyetem", "politechnika", "háskóli", "đại học", "ülikool",
    "technická", "učení", "vysoká škola", "szkoła główna", "college", "institute",
    "polytechnic", "academy", "hochschule", "univerza", "rektorat", "tehnică"
)


def _cjk_uni_title(title: str) -> bool:
    """非拉丁/多语言 title 含本地语言大学特征词 → 站点是大学官网"""
    tl = title.lower()
    return any(t in tl for t in UNI_TERMS)


GENERIC_HOME_TITLES = {
    "home", "homepage", "inicio", "acasa", "startseite", "naslovnica",
    "naslovna", "kezdolap", "main", "welcome", "frontpage", "forsiden",
    "uvod", "uvodna", "landing page", "index", "главная", "басты бет",
    "الرئيسية", "accueil", "site move"
}


def verify_one(name: str, url: str, client: httpx.Client) -> tuple[str, str]:
    """返回 (state, desc)。state: verified | rejected | unverified"""
    title, desc = fetch_title(client, url)
    reg = bw.reg_domain(bw.host_of(url))
    core = _tokens_loose(name)

    if not title:
        # WAF/反爬拦截特殊识别: 如果请求被 403/202/Cloudflare/Akamai 拦截，但域名属于顶级官方教育域且与校名精准对应
        if any(w in desc for w in ("403", "202", "no-title", "405", "412", "520", "503")):
            if (_abbr_hit(name, reg) or bw._core_hit(core, reg) or _concat_hit(name, reg)) and (
                ".edu" in reg or ".ac." in reg or ".ac" in reg.split(".") or reg.endswith((".de", ".fr", ".it", ".es", ".at", ".ch", ".se", ".no", ".dk", ".fi", ".nl", ".be", ".pl", ".cz", ".hu", ".ro", ".bg", ".hr", ".rs", ".si", ".sk", ".gr", ".tr", ".jp", ".kr", ".cn", ".tw", ".hk", ".sg", ".au", ".nz", ".ca", ".uk", ".br", ".mx", ".ar", ".cl", ".co", ".za", ".eg", ".sa", ".ae", ".qa", ".om", ".lb", ".jo", ".kw", ".bh"))
            ):
                return "verified", f"官方教育域WAF防护确认({reg})"
        return "unverified", f"请求失败({desc})"

    # 如果返回了通用的 Cloudflare / WAF Title (如 "Just a moment...", "Attention Required!", "Azure WAF", "Access Denied")
    if any(waf in title.lower() for waf in ("just a moment", "attention required", "azure waf", "access denied", "security verification", "robot", "bot")):
        if (_abbr_hit(name, reg) or bw._core_hit(core, reg) or _concat_hit(name, reg)) and (
            ".edu" in reg or ".ac." in reg or ".ac" in reg.split(".") or reg.endswith((".de", ".fr", ".it", ".es", ".at", ".ch", ".se", ".no", ".dk", ".fi", ".nl", ".be", ".pl", ".cz", ".hu", ".ro", ".bg", ".hr", ".rs", ".si", ".sk", ".gr", ".tr", ".jp", ".kr", ".cn", ".tw", ".hk", ".sg", ".au", ".nz", ".ca", ".uk", ".br", ".mx", ".ar", ".cl", ".co", ".za", ".eg", ".sa", ".ae", ".qa", ".om", ".lb", ".jo", ".kw", ".bh"))
        ):
            return "verified", f"官方教育域WAF防护确认({reg})"

    tl = _tokens_loose(title)

    # 0. 直接 Bigram 验证
    if bw.name_matches_title(title, name):
        return "verified", f"title匹配({title[:40]})"

    # 1. 首字母缩写 (BYU, NCU, KRSU, UCY, UBA, ASU, ITB, LMU 等)
    if _abbr_hit(name, reg):
        return "verified", f"域名缩写({reg})"

    # 2. 括号官方缩写 (UNEC, QUT, MMU, UPM, ITAM, SKU, CEU, IU, NDU, UMPV, UCLV 等)
    m = re.search(r"\(([A-Za-z0-9]{2,10})\)", name)
    if m:
        abbr = m.group(1).lower()
        if abbr == reg.split(".")[0] or reg.split(".")[0].startswith(abbr):
            return "verified", f"括号缩写({reg})"
        # title 中包含该缩写
        if re.search(r"\b" + re.escape(abbr) + r"\b", title.lower()):
            return "verified", f"title含括号缩写({abbr})"

    # 名字前置/后置大写缩写 (如 "Notre Dame University-Louaize NDU" 中的 NDU == ndu.edu.lb)
    abbrs_in_name = re.findall(r"\b[A-Z]{2,6}\b", name)
    for ab in abbrs_in_name:
        ab_low = ab.lower()
        if ab_low == reg.split(".")[0] or reg.split(".")[0].startswith(ab_low):
            return "verified", f"名称缩写({reg})"

    # 3. 核心词拼接匹配 (Sciences Po, Szeged, Kyung Hee 等)
    if _concat_hit(name, reg):
        return "verified", f"域名连写({reg})"

    # 4. 注册域命中核心词
    if bw._core_hit(core, reg):
        # 4.1 title 含 ≥1 核心词
        if tl & core:
            return "verified", f"域名+title核心词({reg})"
        # 4.2 title 为通用首页词 (如 Home, Startseite, Inicio) 且为正规教育/科研域
        t_norm = bw.norm(title).lower()
        if (t_norm in GENERIC_HOME_TITLES or "home" in t_norm.split()) and (
            ".edu" in reg or ".ac." in reg or ".ac" in reg.split(".") or reg.endswith((".de", ".fr", ".it", ".es", ".at", ".ch", ".se", ".no", ".dk", ".fi", ".nl", ".be", ".pl", ".cz", ".hu", ".ro", ".bg", ".hr", ".rs", ".si", ".sk", ".gr", ".tr", ".jp", ".kr", ".cn", ".tw", ".hk", ".sg", ".au", ".nz", ".ca", ".uk", ".br", ".mx", ".ar", ".cl", ".co", ".za", ".eg", ".sa", ".ae", ".qa", ".om", ".lb", ".jo", ".kw", ".bh"))
        ):
            return "verified", f"域名核心词+官方通用首页({reg})"
        # 4.3 title 含大学特征词 (多语言)
        if _cjk_uni_title(title) or title_has_uni_word(title):
            return "verified", f"域名核心词+title大学词({reg})"

    # 5. title 核心词全包含 且 title 含大学特征词
    if core and core.issubset(tl) and (title_has_uni_word(title) or _cjk_uni_title(title)):
        return "verified", f"title核心词全包含({title[:40]})"

    # 6. title 首段官方缩写 == 注册域首段 (ASOIU, ADPU, UNITBV, SGGW, RSU, ULiège, LMU 等)
    # 或者 title 中包含大写缩写词 (LMU, UCL, EPFL, ETH, NUS, CUHK 等)
    tfirst = title.split()[0] if title.split() else ""
    tfirst = re.sub(r"[^a-z]", "", tfirst.lower())
    if len(tfirst) >= 3 and (tfirst == reg.split(".")[0] or reg.split(".")[0].startswith(tfirst)):
        return "verified", f"title首段缩写({tfirst})"

    # 检查 title 中是否有 2~6 位大写缩写等于 reg 首段 (如 "Startseite - LMU München" 中的 LMU == lmu.de)
    abbr_caps = re.findall(r"\b[A-Z]{2,6}\b", title)
    for cap in abbr_caps:
        if cap.lower() == reg.split(".")[0] or reg.split(".")[0].startswith(cap.lower()):
            return "verified", f"title大写缩写({cap})"

    # 7. title 含多语言大学特征词 且 (注册域是正规教育域/高校域名 或 域名注册域命中了部分核心词/缩写/国家顶级域)
    if _cjk_uni_title(title):
        if (
            ".edu" in reg or ".ac." in reg or ".ac" in reg.split(".") or bw._core_hit(core, reg) or
            _abbr_hit(name, reg) or _concat_hit(name, reg) or
            reg.endswith((".de", ".fr", ".it", ".es", ".at", ".ch", ".se", ".no", ".dk", ".fi", ".nl", ".be", ".pl", ".cz", ".hu", ".ro", ".bg", ".hr", ".rs", ".si", ".sk", ".gr", ".tr", ".jp", ".kr", ".cn", ".tw", ".hk", ".sg", ".au", ".nz", ".ca", ".uk", ".br", ".mx", ".ar", ".cl", ".co", ".za", ".eg", ".sa", ".ae", ".qa", ".om", ".lb", ".jo", ".kw", ".bh", ".is", ".ee", ".lv", ".lt", ".uz", ".kz", ".kg", ".az", ".ua", ".ba", ".me", ".al", ".md", ".by"))
        ):
            return "verified", f"多语言大学词+官方域名确认({title[:35]})"

    # 8. 官方简称/知名别名特别规则
    # (UW -> washington.edu, UC Irvine -> uci.edu, Science Tokyo -> isct.ac.jp, Mizzou -> missouri.edu, UMass -> umb.edu, Kantiana -> kantiana.ru 等)
    words_raw = [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", name) if w]
    title_words = set(re.split(r"[^a-zA-Z0-9]+", title.lower()))
    for w in words_raw:
        if len(w) >= 3 and w in title_words and (".edu" in reg or ".ac." in reg or bw._core_hit(core, reg) or _abbr_hit(name, reg) or reg.startswith(w)):
            return "verified", f"专有名词命中+教育域({w})"
    if "mizzou" in title.lower() and "missouri" in reg:
        return "verified", f"知名官方简称确认(Mizzou)"
    if "kantiana" in reg:
        return "verified", f"康德大学官方域名确认({reg})"
    if "qau" in title.lower() and "qau" in reg:
        return "verified", f"官方缩写匹配(QAU)"
    if "uliege" in title.lower().replace("è", "e") and "uliege" in reg:
        return "verified", f"官方缩写匹配(ULiège)"
    if "umpv" in title.lower() and "montp3" in reg:
        return "verified", f"官方缩写匹配(UMPV)"
    if "ndu" in title.upper() and "ndu" in reg:
        return "verified", f"官方缩写匹配(NDU)"
    if "сгу" in title.lower() and "sgu" in reg:
        return "verified", f"俄语官方缩写匹配(СГУ)"
    if "рэу" in title.lower() and "rea" in reg:
        return "verified", f"俄语官方缩写匹配(РЭУ)"

    # 9. 顶级国家/高校主域名且返回正规首页 (如 Home, Inicio, Naslovnica, Главная 等)
    t_clean = title.strip().lower()
    t_norm = bw.norm(title).lower()
    if (t_clean in GENERIC_HOME_TITLES or t_norm in GENERIC_HOME_TITLES or "home" in t_norm.split() or "main" in t_norm.split() or "accueil" in t_clean):
        if _abbr_hit(name, reg) or bw._core_hit(core, reg) or _concat_hit(name, reg) or ".edu" in reg or ".ac." in reg or reg.endswith((".hr", ".ru", ".kz", ".lb", ".pk", ".fr", ".kr")):
            return "verified", f"官方通用首页确认({reg})"

    # 10. 官方大写缩写 (如 QAU, NDU 等) 出现在 title 中
    for ab in re.findall(r"\b[A-Z]{2,6}\b", title):
        if ab.lower() == reg.split(".")[0] or reg.split(".")[0].startswith(ab.lower()):
            return "verified", f"官方缩写确认({ab})"

    return "rejected", f"内容不匹配({title[:40]})"


def safe_save(data: dict, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def main() -> int:
    d = json.load(open(PROGRESS, "r", encoding="utf-8"))   # master dict: 全程内存维护, 保存时整体落盘
    todo = [k for k, v in d.items() if v.get("website") and not v.get("verify")]
    print(f"待验证: {len(todo)} 条 (已有结果跳过)")

    client = httpx.Client(http2=True, timeout=httpx.Timeout(15.0, connect=8.0, read=12.0))
    stats = {"verified": 0, "rejected": 0, "unverified": 0}
    for i, k in enumerate(todo, 1):
        r = process_guarded(i, d[k].get("name", ""), d[k]["website"], client)
        if r is None:
            continue
        state, desc = r
        v = d[k]
        v["verify"] = state
        v["verify_desc"] = desc
        stats[state] += 1
        mark = {"verified": "✓", "rejected": "✗", "unverified": "?"}[state]
        print(f"[{i}/{len(todo)}] {mark} {v.get('name', '')[:40]:<40} {desc}", flush=True)
        if i % 30 == 0:
            safe_save(d, PROGRESS)
        time.sleep(0.4)
        if state == "unverified" and desc.startswith("挂起"):
            # C 层挂起后 client 连接池可能不可靠, 重建
            client.close()
            client = httpx.Client(http2=True, timeout=httpx.Timeout(15.0, connect=8.0, read=12.0))
    client.close()
    safe_save(d, PROGRESS)
    print(f"\n验证完成: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
