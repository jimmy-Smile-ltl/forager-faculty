#!/usr/bin/env python3
"""
搜索式发现 — 名单无链接站的 URL 补全（上外/中山模式: 师资页列姓名但无详情链接）。

策略: 姓名 + 校名 + 院系 拼查询词 → 搜索引擎（Bing HTML）→ 结果链接
过滤到目标校域名 → 选出教师详情页形态的链接（含 jszy/szdw/teacher/
faculty 等路径段或姓名拼音段）。

预算约束（防风控/防滥用）:
  - SEARCH_RESCUE_MAX 默认 10 次查询/院系（取前 N 个无 URL 姓名）
  - 查询经统一客户端节流门（RATE_LIMIT 间隔）
  - 全部失败不影响主流程（无 URL 姓名保留在 names_without_url.json）

产物:
  - names_without_url.json: 未能补全的姓名清单（供未来人工/更强后端）
  - 返回补全成功的 [{name, detail_url, category}] 合并进教师清单
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

SEARCH_RESCUE_MAX = int(os.environ.get("SEARCH_RESCUE_MAX", "10"))

# 详情页形态的路径特征（教师主页常见目录段）
_DETAIL_PATH_HINTS = ("jszy", "szdw", "teacher", "faculty", "professor",
                      "staff", "people", "list", "/info/", "/content/",
                      "zgr", "teachers")


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


# 中国二级公共后缀: 注册域是三级（jlu.edu.cn），不能只取后两段
_CN_SECOND_LEVEL = ("edu.cn", "com.cn", "net.cn", "org.cn", "gov.cn", "ac.cn")


def _registered_domain(url: str) -> str:
    """取注册域: b.c.a.edu.cn → a.edu.cn；x.y.com → y.com。

    注意 edu.cn 等中国二级后缀必须取后三段——曾只取后两段导致
    "jlu.edu.cn" 被截成 "edu.cn"，所有高校域名互相不可区分。
    """
    host = _domain_of(url)
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _CN_SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _backend_bing(scraper, q: str, max_candidates: int = 3) -> list[str]:
    """Bing 后端: 结果链接被 /ck/a 或 /url 包装，解 u 参数取真实 URL。"""
    from bs4 import BeautifulSoup

    html = scraper.fetch_page("https://www.bing.com/search?q=" + q.replace(" ", "+"),
                              timeout=15)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    real: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        unwrapped = _unwrap_redirect(href) or href
        if unwrapped not in real:
            real.append(unwrapped)
        if len(real) >= max_candidates:
            break
    return real


def _unwrap_redirect(href: str) -> str:
    """解包 Bing 结果重定向链接，返回真实 URL 或空。

    Bing 用 /ck/a?...&u=a1<真实URL> 或 /url?...&u=<URL> 包装结果，
    href 的域名是 bing.com，直接按域名过滤会全部误杀。
    """
    from urllib.parse import parse_qs, urlparse

    host = _domain_of(href)
    if "bing.com" not in host and "bing." not in host:
        return ""
    if "/ck/a" not in href and "/url" not in href:
        return ""
    try:
        qs = parse_qs(urlparse(href).query)
        u = qs.get("u", [""])[0]
        if u:
            # /ck/a 格式: u=a1<real-url>；/url 格式: u=<urlencoded>
            if u.startswith("a1"):
                return u[2:]
            return u
    except Exception:
        pass
    return ""


def _looks_like_detail(href: str, name: str) -> bool:
    """结果链接是否为教师详情页形态。

    大学教师主页几乎都带拼音路径段（jszy.whu.edu.cn/pengzhiyong/...），
    而列表页/栏目页是 jsp?wbtreeid、纯目录（kfjl.htm）或 info 新闻页。
    规则: 有拼音 slug（/[a-z]{3,20}/）且无列表页负特征。
    """
    low = href.lower()
    # 负特征: 列表/查询页、常见栏目名
    for bad in ("wbtreeid", ".jsp?", "/list", "list.htm", "kfjl",
                "kxyj", "szdw/", "/tzgg", "/xwdt", "/zhxw"):
        if bad in low:
            return False
    # 正特征: 拼音 slug 段（教师主页标准形态）
    if re.search(r"/[a-z]{3,20}/", low):
        return True
    return False


def search_rescue_names(scraper, school: str, dept: str,
                        names_without_url: list[dict],
                        school_domain_hint: str = "",
                        max_queries: int | None = None) -> tuple[list[dict], list[dict]]:
    """预算受限搜索补全。

    Args:
        names_without_url: [{name, category}]
        school_domain_hint: 校域名（如 zky.jlu.edu.cn）；空则从查询结果
            中按 edu.cn 后缀聚类判断
    Returns:
        (rescued: [{name, detail_url, category}], still_missing)
    """
    if max_queries is None:
        max_queries = SEARCH_RESCUE_MAX
    rescued: list[dict] = []
    still = list(names_without_url)

    if school_domain_hint:
        reg_domain = _registered_domain(school_domain_hint)
    else:
        reg_domain = ""

    queried = 0
    for item in names_without_url[:max_queries]:
        name = item.get("name", "")
        if not name:
            continue
        queried += 1
        q = f"{name} {school} {dept} 教师主页"

        hit = ""
        for backend in (_backend_bing,):
            try:
                candidates = backend(scraper, q)  # 经节流门
                if not candidates:
                    # 搜索引擎反爬间歇: 空结果页常见，稍候重试一次
                    import time as _t
                    _t.sleep(2)
                    candidates = backend(scraper, q)
            except Exception:
                continue
            for href in candidates:
                # 过滤到目标校（有 hint 时严格；无 hint 时要求 edu.cn）
                # 注意 _registered_domain 返回 "edu.cn"（无前导点），
                # 曾写 ".edu.cn" in ... 永远 False 杀掉全部候选
                if reg_domain and _registered_domain(href) != reg_domain:
                    continue
                if not reg_domain and not _registered_domain(href).endswith("edu.cn"):
                    continue
                if _looks_like_detail(href, name):
                    hit = href
                    break
            if hit:
                break
        if hit:
            rescued.append({"name": name, "detail_url": hit,
                            "category": item.get("category", "搜索补全"),
                            "rescue": "search"})
            still = [x for x in still if x.get("name") != name]
            print(f"  🔎 搜索补全 {name}: {hit[:70]}")
    return rescued, still


def save_names_without_url(dept_dir: Path, missing: list[dict]) -> None:
    """未补全姓名落盘（供未来人工/更强后端，避免重复爬名单页）。"""
    if not missing:
        return
    path = dept_dir / "names_without_url.json"
    old = []
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = []
    known = {(x.get("name"), x.get("category")) for x in old}
    merged = old + [x for x in missing if (x.get("name"), x.get("category")) not in known]
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  📝 未补全姓名 {len(missing)} 个 → {path.name}")
