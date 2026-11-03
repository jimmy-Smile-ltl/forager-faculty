#!/usr/bin/env python3
"""
失败院校补采 v2 — 使用 CloakBrowser 替代 Playwright

CloakBrowser: 源码级 Chromium 指纹修补, 绕过瑞数 WAF
- headless 模式可处理: 北邮/安徽大学/华南理工/南理工
- headed 模式处理: 四川大学
- 仍被拦截: 兰州大学/南京师范大学 → 标记 skip
"""

import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # college teacher/ 根 (统一 util 层)
from util.llm import get_llm

OUTPUT_DIR = Path(__file__).resolve().parent
JSONL_FILE = OUTPUT_DIR / "depart_985211.jsonl"
CSV_FILE = OUTPUT_DIR / "depart_985211.csv"

DEPT_KW = ["学院", "系", "研究院", "研究所", "中心", "学部", "书院", "实验室", "基地"]


# ==== 失败学校修复配置 ====
# method: "cb_headless" | "cb_headed" | "known_url" | "skip"
FIX_MAP = {
    # ── cloakbrowser headless 有效 ──
    "北京邮电大学": {
        "website": "http://www.bupt.edu.cn/",
        "method": "cb_headless",
    },
    "安徽大学": {
        "website": "https://www.ahu.edu.cn/",
        "method": "cb_headless",
    },
    "华南理工大学": {
        "website": "https://www.scut.edu.cn/",
        "method": "cb_headless",
    },
    "南京理工大学": {
        "website": "http://www.njust.edu.cn/",
        "method": "cb_headless",
    },
    # ── cloakbrowser headed 有效 ──
    "四川大学": {
        "website": "https://www.scu.edu.cn/",
        "method": "cb_headed",
    },
    # ── 已知院系URL (之前已验证可直连) ──
    "中央财经大学": {
        "website": "http://www.cufe.edu.cn/",
        "dept_url": "http://www.cufe.edu.cn/zzjg/xysz.htm",
        "method": "known_url",
    },
    "南京信息工程大学": {
        "website": "https://www.nuist.edu.cn/",
        "dept_url": "https://www.nuist.edu.cn/jgsz/jxjg.htm",
        "method": "known_url",
    },
    "西藏大学": {
        "website": "https://www.xzu.edu.cn/",
        "dept_url": "https://www.xzu.edu.cn/html/yuansuoshezhi.html",
        "method": "known_url",
    },
    "上海交通大学": {
        "website": "https://www.sjtu.edu.cn/",
        "dept_url": "https://www.sjtu.edu.cn/yxsz/",
        "method": "known_url",
    },
    "浙江大学": {
        "website": "https://www.zju.edu.cn",
        "dept_url": "https://www.zju.edu.cn/xywxw/list.htm",
        "method": "known_url",
    },
    "上海外国语大学": {
        "website": "http://www.shisu.edu.cn/",
        "dept_url": "https://www.shisu.edu.cn/academics/schools-and-departments/index.html",
        "method": "known_url",
    },
    "中国政法大学": {
        "website": "http://www.cupl.edu.cn",
        "dept_url": "https://www.cupl.edu.cn/jxjg1.htm",
        "method": "known_url",
    },
    "西南大学": {
        "website": "http://www.swu.edu.cn",
        "dept_url": "https://www.swu.edu.cn/zzjg/jxky.htm",
        "method": "known_url",
    },
    "中国美术学院": {
        "website": "https://www.caa.edu.cn",
        "dept_url": "https://www.caa.edu.cn/xy/jxjg.htm",
        "method": "known_url",
    },
    "中央民族大学": {
        "website": "https://www.muc.edu.cn/",
        "dept_url": "https://www.muc.edu.cn/zzjg/jxhkydw1.htm",
        "method": "known_url",
    },
    "中国石油大学（华东）": {
        "website": "http://www.upc.edu.cn/",
        "dept_url": "https://www.upc.edu.cn/jxkydw.htm",
        "method": "known_url",
    },
    "山西大学": {
        "website": "https://www.sxu.edu.cn",
        "dept_url": "https://www.sxu.edu.cn/xxgk/jgsz/yxsz/index.html",
        "method": "known_url",
    },
    "青海大学": {
        "website": "https://www.qhu.edu.cn",
        "dept_url": "https://www.qhu.edu.cn/jgsz/yxsz/index.htm",
        "method": "known_url",
    },
    # ── cloakbrowser 处理 (有子页面) ──
    "中央戏剧学院": {
        "website": "https://www.chntheatre.edu.cn/cn/index",
        "dept_url": "https://www.chntheatre.edu.cn/cn/educationat",
        "method": "cb_headless",
    },
    "中央美术学院": {
        "website": "https://www.cafa.edu.cn/",
        "dept_url": "https://www.cafa.edu.cn/sp/nljg/?j=1",
        "method": "cb_headless",
    },
    "西北工业大学": {
        "website": "https://www.nwpu.edu.cn/",
        "dept_url": "https://www.nwpu.edu.cn/jgsz_cd_/jxkyjg/xy.htm",
        "method": "cb_headless",
        "extra_urls": [
            "https://www.nwpu.edu.cn/jgsz_cd_/jxkyjg/yjy_yjzx.htm",
            "https://www.nwpu.edu.cn/jgsz_cd_/jxkyjg/jxdw.htm",
        ],
    },
    "内蒙古大学": {
        "website": "https://www.imu.edu.cn/",
        "dept_url": "https://www.imu.edu.cn/jgsz1/jxkydw.htm",
        "method": "cb_headless",
    },
    "北京协和医学院": {
        "website": "https://www.pumc.edu.cn/",
        "dept_url": "https://www.pumc.edu.cn/zssy/index.htm",
        "method": "cb_headless",
    },
    "东华大学": {
        "website": "https://www.dhu.edu.cn/",
        "dept_url": "https://www.dhu.edu.cn/jgsz/list.htm",
        "method": "cb_headless",
    },
    "北京中医药大学": {
        "website": "https://www.bucm.edu.cn",
        "dept_url": "https://www.bucm.edu.cn/zzjg1/jxjg1/index.htm",
        "method": "cb_headless",
    },
    "武汉理工大学": {
        "website": "https://www.whut.edu.cn/",
        "dept_url": "https://www.whut.edu.cn/xuexiaogaikuang/xysz/",
        "method": "cb_headless",
    },
    "成都理工大学": {
        "website": "http://www.cdut.edu.cn",
        "dept_url": "https://www.cdut.edu.cn/jgsz1/jxkydw.htm",
        "method": "cb_headless",
    },
    "西南石油大学": {
        "website": "https://www.swpu.edu.cn",
        "dept_url": "https://www.swpu.edu.cn/jgsz/xysz.htm",
        "method": "cb_headless",
    },
    # ── cloakbrowser 全部拦截 → skip ──
    "兰州大学": {
        "website": "https://www.lzu.edu.cn",
        "method": "skip",
        "reason": "瑞数 WAF 所有模式均拦截 (headless/headed/humanize), 返回 39 字符空页",
    },
    "南京师范大学": {
        "website": "http://www.njnu.edu.cn",
        "method": "skip",
        "reason": "HTML 仅 39 字符, 可能被 IP/地域封禁或 URL 已变更",
    },
}


# ==== Helpers ====
FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
}


def fetch_html(url, timeout=15, retries=2):
    for _ in range(retries):
        try:
            resp = cffi_requests.get(url, headers=FAKE_HEADERS, impersonate="chrome131", timeout=timeout)
            resp.encoding = "utf-8"
            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
        except Exception:
            time.sleep(2)
    return ""


def extract_dept_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    candidates = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        h = a["href"]
        if not t or len(t) < 3 or len(t) > 60 or not re.search(r'[一-鿿]', t):
            continue
        if h.startswith("#") or h.startswith("javascript"):
            continue
        if re.search(r'\d{4}[年./-]\d', t):
            continue
        if not any(kw in t for kw in DEPT_KW):
            continue
        full = urljoin(base_url, h)
        key = full.split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        parent = a.parent
        while parent and parent.name not in ("li", "td", "div", "p", "tr"):
            parent = parent.parent
        ptext = parent.get_text(strip=True)[:100] if parent else ""
        candidates.append({"name": t, "url": full, "parent_text": ptext})
    return candidates


def llm_classify(school_name, candidates):
    client = get_llm()
    if not client or not client.available:
        return candidates
    results = []
    for i in range(0, len(candidates), 30):
        batch = candidates[i:i+30]
        links_json = json.dumps(
            [{"name": c["name"], "url": c["url"], "parent_text": c["parent_text"][:60]} for c in batch],
            ensure_ascii=False,
        )
        prompt = f"""判断[{school_name}]页面链接, 只保留教学科研组织。

教学科研: 学院/系/研究院/研究所/学部/书院/重点实验室/实验教学中心
非教学: 行政处室/党团/OA/系统入口/服务窗口/新闻/继续教育/培训中心/管理机构

链接: {links_json}

返回纯JSON: [{{"name":"","url":"","type":"学院"}},...]"""

        try:
            text = client.ask("", prompt, temperature=0.0)
            m = re.search(r'\[[\s\S]*\]', text)
            if m:
                batch_r = json.loads(m.group())
                results.extend(batch_r)
        except Exception as e:
            print(f"      ⚠️ LLM: {e}")

    seen = set()
    uniq = []
    for d in results:
        k = d.get("url", "").split("?")[0].rstrip("/")
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def process_cb(name, cfg, headless=True):
    """使用 CloakBrowser 处理"""
    from cloakbrowser import launch

    website = cfg["website"]
    dept_url = cfg.get("dept_url")
    result = {"school_name": name, "school_url": website, "dept_page_url": dept_url or "",
              "method": f"CloakBrowser {'headless' if headless else 'headed'}", "departments": []}

    browser = launch(headless=headless)
    page = browser.new_page()

    try:
        page.goto(website, timeout=60000)
        page.wait_for_timeout(3000)

        content_len = len(page.content())
        link_count = page.evaluate("document.querySelectorAll('a[href]').length")

        if link_count < 10:
            result["method"] += f" (blocked: {content_len} chars, {link_count} links)"
            browser.close()
            return result

        # 找院系入口
        if not dept_url:
            links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
                .filter(a => {const t=a.textContent.trim(); return t.length>=2 && t.length<30;})
                .map(a => ({t: a.textContent.trim(), h: a.getAttribute('href')||''}))""")
            keywords = {"院系设置": 100, "学部院系": 95, "学院设置": 90, "院系部门": 85,
                        "教学机构": 50, "院系": 60, "组织机构": 25, "教学科研单位": 75,
                        "教学科研机构": 75, "院所设置": 80, "教学单位": 70}
            best = ("", 0)
            for l in links:
                for kw, s in keywords.items():
                    if kw in l["t"] and s > best[1]:
                        best = (urljoin(website, l["h"]), s)
            if best[0]:
                dept_url = best[0]
                result["dept_page_url"] = dept_url
                print(f"    🔗 CB: {dept_url}")
            else:
                result["method"] += " — dept link not found"
                browser.close()
                return result

        # 抓取院系页
        if dept_url:
            # Use page.evaluate fetch to get content from within browser context
            candidates = page.evaluate(f"""async () => {{
                try {{
                    const resp = await fetch('{dept_url}');
                    const html = await resp.text();
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');
                    const as = doc.querySelectorAll('a[href]');
                    const depts = [];
                    const kw = {json.dumps(DEPT_KW)};
                    const seen = new Set();
                    for (const a of as) {{
                        const t = a.textContent.trim();
                        const h = a.getAttribute('href') || '';
                        if (!t || t.length < 3 || t.length > 60) continue;
                        if (h.startsWith('#') || h.startsWith('javascript:')) continue;
                        if (/\\d{{4}}[年./-]\\d/.test(t)) continue;
                        let dt = '';
                        for (const k of kw) {{ if (t.includes(k)) {{ dt = k; break; }} }}
                        if (!dt) continue;
                        let full = h;
                        if (!h.startsWith('http')) {{
                            try {{ full = new URL(h, '{dept_url}').href; }} catch(e) {{ continue; }}
                        }}
                        const key = full.split('?')[0].replace(/\\/$/, '');
                        if (seen.has(key)) continue;
                        seen.add(key);
                        depts.push({{name: t, url: full, type: dt, parent_text: ''}});
                    }}
                    return depts;
                }} catch(e) {{ return []; }}
            }}""")

            # extra pages
            for extra_url in cfg.get("extra_urls", []):
                extra = page.evaluate(f"""async () => {{
                    const resp = await fetch('{extra_url}');
                    const html = await resp.text();
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');
                    const depts = [];
                    const kw = {json.dumps(DEPT_KW)};
                    for (const a of doc.querySelectorAll('a[href]')) {{
                        const t = a.textContent.trim();
                        const h = a.getAttribute('href') || '';
                        if (!t || t.length < 3 || t.length > 60) continue;
                        let dt = '';
                        for (const k of kw) {{ if (t.includes(k)) {{ dt = k; break; }} }}
                        if (!dt) continue;
                        let full = h;
                        if (!h.startsWith('http')) {{
                            try {{ full = new URL(h, '{extra_url}').href; }} catch(e) {{ continue; }}
                        }}
                        depts.push({{name: t, url: full, type: dt, parent_text: ''}});
                    }}
                    return depts;
                }}""")
                candidates.extend(extra)

            if candidates:
                # Dedup
                seen = set()
                uniq = []
                for d in candidates:
                    k = d["url"].split("?")[0].rstrip("/")
                    if k not in seen:
                        seen.add(k)
                        uniq.append(d)

                depts = llm_classify(name, uniq)
                result["departments"] = depts
                result["method"] += f" → LLM ({len(depts)}/{len(uniq)})"
                print(f"    ✅ {len(depts)} 院系")
            else:
                result["method"] += " — no candidates"
                print(f"    ❌ 无候选")

    except Exception as e:
        result["method"] += f" — error: {str(e)[:80]}"
        print(f"    ❌ {str(e)[:80]}")
    finally:
        browser.close()

    return result


def process_school(name, cfg):
    llm = get_llm()
    if llm:
        llm.begin_task()  # 任务边界: 每个学校开始前重新均衡分配 token (校内连续调用固定同一 token)
    website = cfg["website"]
    method = cfg["method"]
    dept_url = cfg.get("dept_url")
    result = {"school_name": name, "school_url": website, "dept_page_url": dept_url or "",
              "method": "", "departments": []}

    print(f"\n  📍 {name}: {website} [{method}]")

    if method == "skip":
        result["method"] = f"skip: {cfg.get('reason', '')}"
        print(f"    ⏭️ {cfg.get('reason', '')}")
        return result

    if method == "known_url" and dept_url:
        html = fetch_html(dept_url)
        if html:
            candidates = extract_dept_links(html, dept_url)
            if candidates:
                depts = llm_classify(name, candidates)
                result["departments"] = depts
                result["method"] = f"manual_url → LLM ({len(depts)}/{len(candidates)})"
                print(f"    ✅ {len(depts)} 院系")
                return result
            else:
                method = "cb_headless"
        else:
            method = "cb_headless"

    if method in ("cb_headless", "cb_headed"):
        return process_cb(name, cfg, headless=(method == "cb_headless"))

    result["method"] = "unknown method"
    return result


def main():
    existing = {}
    if JSONL_FILE.exists():
        with open(JSONL_FILE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line.strip())
                existing[r["school_name"]] = r

    fix_names = list(FIX_MAP.keys())
    print("=" * 60)
    print("🔧 失败院校补采 v2 (CloakBrowser)")
    print(f"   目标: {len(fix_names)} 所学校")
    print("=" * 60)

    for i, name in enumerate(fix_names):
        cfg = FIX_MAP[name]
        print(f"\n{'─'*50}")
        print(f"  [{i+1}/{len(fix_names)}] {name}")

        try:
            r = process_school(name, cfg)
            existing[name] = r
            with open(JSONL_FILE, "w", encoding="utf-8") as f:
                for v in existing.values():
                    f.write(json.dumps(v, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"    ❌ {e}")

        time.sleep(0.5)

    all_results = list(existing.values())
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["学校名称", "学校网址", "院系名称", "院系URL", "院系类型", "采集方式"])
        for r in all_results:
            method = r.get("method", "")
            if r.get("departments"):
                for d in r["departments"]:
                    w.writerow([r["school_name"], r["school_url"], d["name"], d["url"],
                                d.get("type", ""), method])
            else:
                w.writerow([r["school_name"], r["school_url"], "", "", "", method])

    success = sum(1 for r in all_results if r.get("departments"))
    total_depts = sum(len(r.get("departments", [])) for r in all_results)
    print(f"\n{'='*60}")
    print(f"📊 补采完成: {success}/{len(all_results)} 所, {total_depts} 院系")


if __name__ == "__main__":
    main()
