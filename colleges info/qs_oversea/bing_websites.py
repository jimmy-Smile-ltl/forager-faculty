"""Bing 搜索获取 1504 所大学官网 (v2: 内容验证防错配)

关键词策略 (命中即停):
  1. "{学校名}"
  2. "{学校名} official website"
  3. "{学校名} {国家}"
  4. "{学校名} official site"

防错配三重防线:
  1. 黑名单域名过滤 (社交/百科/广告/翻译/问答站)
  2. 官网内容验证: GET 候选 URL, <title> 与学校名 bigram/核心 token 匹配
     (adidas/britishairways 等广告站 title 不会含 "Addis Ababa University")
  3. 评分排序: 注册域(无子域)优先 + edu/ac 域名 + host 含核心 token

断点续传: raw/bing_websites.json

用法:
  uv run python bing_websites.py            # 全量
  uv run python bing_websites.py --limit 20 # 只跑前20条(测试)
  uv run python bing_websites.py --from N   # 从第 N 条续跑
"""

from __future__ import annotations

import base64
import html as htmllib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
NODES = os.path.join(HERE, "raw", "all_score_nodes.json")
PROGRESS = os.path.join(HERE, "raw", "bing_websites.json")
OUT_CSV = os.path.join(HERE, "qs_rankings_2027_with_website.csv")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# 非官网域名黑名单 (社交/百科/新闻/聚合/评测/广告/翻译/问答)
BLACKLIST = {
    "bing.com", "msn.com", "microsoft.com", "wikipedia.org", "wikidata.org",
    "wikimedia.org", "facebook.com", "fb.com", "x.com", "twitter.com",
    "linkedin.com", "youtube.com", "youtu.be", "instagram.com", "reddit.com",
    "quora.com", "topuniversities.com", "qs.com", "timeshighereducation.com",
    "usnews.com", "britannica.com", "baike.com", "iciba.com",
    "youdao.com", "fanyi.youdao.com", "adidas.com", "adidas.com.cn",
    "britishairways.com", "theguardian.com", "forbes.com", "nytimes.com",
    "bbc.com", "bbc.co.uk", "cnn.com", "reuters.com", "google.com",
    "yandex.com", "yahoo.com", "amazon.com", "alexa.com", "similarweb.com",
    "crunchbase.com", "universityranking.com", "4icu.org", "collegerank.net",
    "nces.ed.gov", "zhihu.com", "douban.com", "weibo.com", "bilibili.com",
    "admissions.com", "studyabroad.com", "hotcourses.com", "mastersportal.com",
    "studyinternational.com", "campusfrance.org", "daad.de", "ed.gov",
    "collegedunia.com", "careers360.com", "shiksha.com", "unirank.net",
    "webometrics.info", "unirank.com", "studyportal.io", "edurank.org",
    "zhipin.com", "jobtim.com", "chinahr.com", "qunar.com", "tripadvisor.com",
    "expedia.com", "booking.com", "glassdoor.com", "indeed.com",
    "dle.rae.es", "rae.es", "4399.com", "xiehouit.com", "travelwithmiya.com",
    "kimiyo.tw", "ontheworldmap.com", "pgy.oray.com", "oray.com",
    "csdn.net", "huawei.com", "wikipedia.org.cn",
    "qs-topuniversities.cn", "topuniversities.cn", "universitycompare.com",
    "universityofjordan.com", "jordanuniversity.com",
    "tencent.com", "aliyun.com", "cnblogs.com", "juejin.cn",
    "segmentfault.com", "51cto.com", "geekbang.org", "infoq.cn",
    "mtu-solutions.com", "mtuaeroengines.com",
    "britishcouncil.org", "charlesproxy.com", "britishcouncil.eg",
    "study-uk.britishcouncil.org", "lomonosov-porcelain.com",
    "newcastleunited.com", "nufc.co.uk", "newcastle-united.co.uk",
    "manutd.com", "manchesterunited.com", "liverpoolfc.com", "arsenal.com",
    "chelseafc.com", "realmadrid.com", "fcbarcelona.com", "juventus.com",
    "travelchinaguide.com", "chinatravel.com", "travelchina.org",
    "sichuantour.com", "chinadiscovery.com", "jupinzan.com",
    "standyou.com", "studyin-uk.com", "applyboard.com", "si-asia.com",
    "overseasstudy.com", "liuxue.com", "eic.org.cn", "liuxue86.com",
    "idp.com", "idpchina.com", "qiye.liuxue.com", "51liuxue.com",
    "jejuop.com", "studyinkorea.com", "yuhak.kr", "admissions.cn",
    "china-admissions.com", "admission.cn",
    "rdmfj.com", "nokiuh.com", "nokia.com",
    "amz123.com", "informatecdigital.com", "ufc.com", "danacita.co.id",
    "danacita.com", "kreditpintar.com", "akulaku.com", "adapta.id",
    "edusg.com.cn", "sgliuxue.com", "studyabroad.sg", "xuesg.com",
    "sgstudy.com", "xinjiayou.com", "sgfairy.com",
    "canva.com", "canva.dev", "designstudio.com", "wix.com", "squarespace.com",
    "ufcfightpass.com", "ufcfight.com", "unirank.org", "studyatchina.com",
    "cucas.cn", "ah.gov.cn", "jsjyt.edu.cn", "niad.ac.jp", "jpcup.niad.ac.jp",
    "hallym.or.kr", "yayasantrisakti.id", "ntnu.edu",
    "studyinromania.gov.ro", "studyinromania.ro",
}
BLACK_HOST = tuple(f".{d}" for d in BLACKLIST) + tuple(d for d in BLACKLIST)

# 停用词: 从名称里剔除后再做 core token / host 匹配
STOP = {"university", "universities", "of", "the", "and", "for", "at", "in",
        "college", "institute", "technology", "technological", "science",
        "sciences", "studies", "study", "school", "campus", "univ", "uni",
        "de", "la", "del", "el", "las", "los", "le", "en", "et", "des",
        "universidad", "universidade", "université", "universityof",
        "technische", "universitat", "hochschule"}

# 通用 bigram: 单 bigram 命中时判弱, 不单独接受
RAW_STOP_BIGRAMS = {
    "university of", "of the", "the university", "universidad de",
    "de la", "universidade de", "university in", "of technology",
    "institute of", "of science", "of engineering", "college of",
}


def clean(name: str) -> str:
    n = re.sub(r"\s*\([^)]*\)", "", name).strip()
    n = re.sub(r"\s+", " ", n).strip()
    return n


def norm(s: str) -> str:
    # NFKD 分解(é→e+组合重音), 先删除组合重音区 U+0300–U+036F, 否则 ó 会被替换成空格拆成两个词
    s = unicodedata.normalize("NFKD", s)
    s = re.sub("[̀-ͯ]", "", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(name: str) -> list[str]:
    return [t for t in norm(clean(name)).split() if t not in STOP]


def bigrams_raw(name: str) -> set[tuple[str, str]]:
    ws = norm(clean(name)).split()
    return set(zip(ws, ws[1:]))


def name_matches_title(title: str, name: str) -> bool:
    """原始 bigram 交集验证 (≥2 或 单 bigram 非通用), 短名(≤2词) 核心词兜底, 双向核心词兜底"""
    if not title:
        return False
    t = norm(title)
    if not t:
        return False
    tw = t.split()
    tb = set(zip(tw, tw[1:]))
    nb = bigrams_raw(name)
    common = nb & tb
    if len(common) >= 2:
        return True
    if len(common) == 1:
        bg = " ".join(next(iter(common)))
        if bg in RAW_STOP_BIGRAMS:
            return False
        if len(norm(clean(name)).split()) <= 3:
            return True
        return False
    # 无 bigram: 名字 ≤2 个原始词, 名称全词必须都在 title 中 (不去停用词)
    # 防 "Newcastle United" 足球俱乐部撞 "Newcastle University"
    if len(norm(clean(name)).split()) <= 2:
        nw = set(norm(clean(name)).split())
        return bool(nw) and nw.issubset(set(t.split()))
    # 缩写匹配: 括号/前置缩写出现在 title 中 (UNAM | Portal UNAM)
    # ≥3 字符缩写直接接受 (title 词数 ≤4 且无中文, 防技术文章/中文促销站);
    # 2 字符缩写需核心词配合 (防 UP 简历站误配)
    tu = t.upper()
    tw_count = len(tw)
    has_cjk = bool(re.search(r"[一-鿿]", title))
    m = re.search(r"\(([A-Za-z0-9]{2,10})\)", name)
    if m:
        abbr = m.group(1).upper()
        if (len(abbr) >= 3 and tw_count <= 4 and not has_cjk
                and re.search(r"(?<![a-z])" + re.escape(abbr) + r"(?![a-z])", tu)):
            return True
        if len(abbr) == 2:
            # 双字符缩写: 核心词全在 title 且 title 含大学特征词 (防 British Council 误配)
            nc = set(tokens(name))
            uni_words = ("university", "universidad", "universitat",
                         "college", "institute", "school", "academy", "technische")
            if (nc and all(c in t for c in nc)
                    and any(w in t for w in uni_words)):
                return True
    m2 = re.match(r"^([A-Z]{2,6})\b", name)
    if m2:
        abbr = m2.group(1).upper()
        if (len(abbr) >= 3 and tw_count <= 4 and not has_cjk
                and re.search(r"(?<![a-z])" + re.escape(abbr) + r"(?![a-z])", tu)):
            return True
    # 兜底: 名称核心词与 title 核心词完全一致 (语言变体, 如 universität/university)
    nc = set(tokens(name))
    tc = set(tokens(title))
    if len(nc) >= 2 and nc == tc:
        return True
    return False


def _decode_ck(href: str) -> str | None:
    u = htmllib.unescape(href)
    mu = re.search(r"[?&]u=(a1[A-Za-z0-9_\-]+)", u)
    if mu:
        enc = mu.group(1)[2:]
        try:
            return base64.b64decode(enc + "=" * (-len(enc) % 4)).decode("utf-8", "ignore")
        except Exception:
            return None
    return None


def search_bing(query: str, client: httpx.Client) -> list[str]:
    """搜索并返回前 10 条结果的真实 URL (兼容直链与 ck/a 重定向)"""
    url = ("https://www.bing.com/search"
           f"?q={urllib.parse.quote(query)}&mkt=en-US&setlang=en&cc=US&count=10")
    r = client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)
    r.raise_for_status()
    html = r.text
    out = []
    for b in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"', b, re.S)
        if not m:
            continue
        href = m.group(1)
        if href.startswith(("http://", "https://")):
            if "bing.com/ck/a" in href:
                dec = _decode_ck(href)
                if dec:
                    out.append(dec)
            else:
                out.append(href)
        else:
            dec = _decode_ck(href)
            if dec:
                out.append(dec)
    return out


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_blacklisted(host: str) -> bool:
    return host.endswith(BLACK_HOST)


TITLE_CACHE: dict[str, str] = {}


def title_of(url: str, client: httpx.Client) -> str:
    """GET 页面取 <title>, 失败返回空. host 级缓存 (同域只取一次)"""
    h = host_of(url)
    if h in TITLE_CACHE:
        return TITLE_CACHE[h]
    try:
        r = client.get(url, headers=HEADERS, follow_redirects=True, timeout=8)
        if r.status_code != 200:
            TITLE_CACHE[h] = ""
            return ""
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S)
        if m:
            t = re.sub(r"<[^>]+>", "", m.group(1))
            t = htmllib.unescape(t)
            t = re.sub(r"\s+", " ", t).strip()
        else:
            t = ""
    except Exception:
        t = ""
    TITLE_CACHE[h] = t
    return t


def reg_domain(host: str) -> str:
    """注册域: 去掉 www 与子域. .ac.uk/.edu.cn 等两段后缀特殊处理"""
    h = host
    for p in ("www.", "ww2."):
        if h.startswith(p):
            h = h[len(p):]
            break
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    if parts[-1] == "uk" and parts[-2] == "ac":
        return ".".join(parts[-3:])
    if (parts[-1] in ("cn", "jp", "kr", "au", "br", "tr", "in", "za", "tw",
                      "hk", "sg", "mx", "ar", "cl", "co", "my", "th", "vn",
                      "eg", "sa", "ae", "ir", "id", "nz", "az", "kg", "kz",
                      "ua", "uz", "am", "ge", "by", "md", "lt", "lv", "ee",
                      "rs", "mk", "ba", "hr", "si", "sk", "bg", "ro", "hu",
                      "cy", "mt", "is", "lb", "qa", "kw", "bh", "om", "jo", "pk")
            and len(parts[-2]) in (2, 3)):
        # ac.jp/ac.kr/co.jp/edu.cn/org.cn/edu.az/edu.kg 等两段后缀, 第二段 2 或 3 字母
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def reg_freq(urls: list[str]) -> dict[str, int]:
    """统计候选 URL 中各注册域出现次数"""
    freq: dict[str, int] = {}
    for u in urls:
        h = host_of(u)
        if h and not is_blacklisted(h):
            freq[reg_domain(h)] = freq.get(reg_domain(h), 0) + 1
    return freq


def _core_hit(core: set[str], reg: str) -> bool:
    """reg 中的词与名称核心词有 ≥3 字符前缀重叠 (tuhh/tuhh.de, eth/ethz.ch)。
    长度差 >2 拒绝: charlesproxy.com 撞 charles 前缀但差 6"""
    rw = set(re.findall(r"[a-z]{2,}", reg))
    for c in core:
        for r in rw:
            if (len(c) >= 3 and len(r) >= 3 and abs(len(c) - len(r)) <= 2
                    and (r.startswith(c) or c.startswith(r))):
                return True
    return False


def pick_website(urls: list[str], name: str, client: httpx.Client) -> str | None:
    """过滤黑名单 → 同域去重 → 内容验证 + 评分 → 回退 (≥2核心词 + reg前缀命中 + 强位置)"""
    cands = [u for u in urls if not is_blacklisted(host_of(u))]
    if not cands:
        return None
    freq = reg_freq(urls)
    core = set(tokens(name))

    # 同注册域保留前 3 个 URL: 首页可能 WAF 无 title, 子页可拿 (eng.ox.ac.uk vs www.ox.ac.uk)
    seen_reg: dict[str, int] = {}
    dedup = []
    for u in cands:
        r = reg_domain(host_of(u))
        if seen_reg.get(r, 0) >= 3:
            continue
        seen_reg[r] = seen_reg.get(r, 0) + 1
        dedup.append(u)

    scored = []
    for i, u in enumerate(dedup[:10]):
        host = host_of(u)
        reg = reg_domain(host)
        title = title_of(u, client)
        verified = name_matches_title(title, name)
        sc = 0
        if verified:
            sc += 5
        if freq.get(reg, 0) >= 2:
            sc += 3  # 同注册域多次出现 = 官网信号
        if reg.endswith((".edu", ".edu.")) or ".ac." in reg:
            sc += 2
        if _core_hit(core, reg):
            sc += 2
        if host == reg or host.startswith("www."):
            sc += 1  # 主域/www 优先于子域
        sc += max(0, 8 - i) // 2  # 位置加分
        scored.append((u, sc, verified, host, i))
        if verified and sc >= 7:
            break  # 高置信命中即停

    scored.sort(key=lambda x: x[1], reverse=True)
    for u, sc, verified, host, i in scored:
        if verified:
            return u
        # 回退1 (title 为空时): 名称≥2核心词 且 reg 前缀命中 且 (同域≥2 或 结果前2)
        # 注: title 非空但验证失败 = 内容不匹配, 拒绝 (lomonosov-porcelain 瓷器厂撞名)
        reg = reg_domain(host)
        if (not title_of(u, client)
                and len(core) >= 2 and _core_hit(core, reg)
                and (freq.get(reg, 0) >= 2 or i <= 1)):
            return u
        # 回退2: 单核心词 + title 空 + 结果集被同注册域主导 (ju.edu.jo 类)
        # (michigan.org 只 2 条被拦, .org/.gov/.cn/.com 州级门户域排除)
        if (len(core) == 1 and not title_of(u, client) and i == 0
                and freq.get(reg, 0) >= max(3, len(urls) * 0.4)
                and not reg.endswith((".org", ".gov", ".cn", ".com"))):
            return u
    return None


def build_queries(name: str, country: str) -> list[str]:
    """先去括号主名 (引号精确), 全名失败后加 括号缩写/前置缩写 兜底 (cn.bing 对缩写索引好)"""
    cn = clean(name)
    qs = [f'"{cn}"', f'"{cn}" official website', f'"{cn}" "{country}"',
          f'"{cn}" official site']
    m = re.search(r"\(([A-Za-z][A-Za-z0-9]{1,9})\)", name)
    if m:
        abbr = m.group(1).strip()
        if abbr.upper() not in cn.upper():
            qs += [f'"{abbr}" university', f'"{abbr}" "{country}"']
    m2 = re.match(r"^([A-Z]{2,6})\s+", name)  # 前置缩写: TUHH, KTH, ETH...
    if m2:
        abbr = m2.group(1)
        if abbr not in cn:
            qs += [f'"{abbr}" university']
    return qs


def process(node: dict, client: httpx.Client) -> tuple[str | None, str]:
    name = node.get("title", "")
    country = node.get("country", "")
    for q in build_queries(name, country):
        try:
            urls = search_bing(q, client)
        except Exception as e:
            print(f"    搜索失败 {q[:60]}: {e}")
            time.sleep(5)
            continue
        if urls:
            w = pick_website(urls, name, client)
            if w:
                return w, q
        time.sleep(2.0)
    return None, ""


def main() -> int:
    limit = None
    start = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--from" in sys.argv:
        start = int(sys.argv[sys.argv.index("--from") + 1])

    nodes = json.load(open(NODES))
    if limit:
        nodes = nodes[:limit]
    print(f"目标: {len(nodes)} 所")

    done = {}
    if os.path.exists(PROGRESS):
        done = json.load(open(PROGRESS))
    print(f"已有进度: {len(done)} 所")

    client = httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0, read=15.0))
    results = dict(done)
    try:
        for i, n in enumerate(nodes[start:], start=start + 1):
            nid = n["nid"]
            if nid in results:
                continue
            w, q = process(n, client)
            results[nid] = {"name": n.get("title", ""), "country": n.get("country", ""),
                            "website": w or "", "query": q}
            mark = "✓" if w else "✗"
            print(f"[{i}/{len(nodes)}] {mark} {n.get('rank_display')} "
                  f"{n.get('title', '')[:48]} -> {w or '未命中'}")
            if i % 10 == 0:
                json.dump(results, open(PROGRESS, "w"), ensure_ascii=False)
            time.sleep(0.8)
    finally:
        json.dump(results, open(PROGRESS, "w"), ensure_ascii=False)

    hit = sum(1 for v in results.values() if v["website"])
    miss = [v for v in results.values() if not v["website"]]
    print(f"\n完成: {len(results)} 所, 命中 {hit}, 未命中 {len(miss)}")
    if miss:
        print("未命中(前20):")
        for v in miss[:20]:
            print(f"    {v['name']} | {v['country']}")
    print(f"进度已存: {PROGRESS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
