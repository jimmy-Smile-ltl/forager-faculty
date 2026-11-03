#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剩余失败院校补采 v3 — 全程 CloakBrowser headless

自动扫描 depart_985211.jsonl 中 method 含以下标记的学校:
  skip / PW: / dept link not found / no candidates

对每所学校的流程:
  1. CloakBrowser headless 打开官网首页 (等待 WAF 挑战完成)
  2. 从首页导航找院系入口链接 (院系设置/教学机构/学部院系/组织架构 等)
  3. 进入院系列表页 (含已知 dept_page_url 重试), 提取所有候选院系链接
     (遍历所有 frame, 滚动触发懒加载)
  4. LLM (Claude Haiku 4.5, OneAPI backend) 分类教学/非教学组织
  5. 更新 JSONL 中该学校记录, 并重写 CSV

用法:
  python fix_remaining.py               # 自动处理所有失败学校
  python fix_remaining.py --school 兰州大学   # 只处理指定学校
  python fix_remaining.py --limit 1     # 只处理前 N 所
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # college teacher/ 根 (统一 util 层)
from util.llm import get_llm

BASE_DIR = Path(__file__).resolve().parent
JSONL_FILE = BASE_DIR / "depart_985211.jsonl"
CSV_FILE = BASE_DIR / "depart_985211.csv"

# 院系候选关键词
DEPT_KW = ["学院", "系", "研究院", "研究所", "中心", "学部", "书院", "实验室", "基地"]

# 首页导航中的院系入口关键词 (score 越高越优先)
ENTRY_KEYWORDS = [
    ("院系设置", 100), ("学部院系", 95), ("学院设置", 90), ("院所设置", 85),
    ("院系部门", 85), ("院系介绍", 85), ("院部设置", 85), ("教学科研单位", 80),
    ("教学科研机构", 80), ("教学机构", 75), ("教学单位", 70), ("院系", 60),
    ("学院部门", 55), ("组织机构", 35), ("机构设置", 35), ("组织架构", 40),
]

# 明显不是院系入口的导航项
ENTRY_NEWS_KW = ["新闻", "通知", "公告", "讲座", "报告", "会议", "动态",
                 "招聘", "招标", "图书馆", "邮箱", "OA", "校历", "地图", "联系"]

# 判定为失败记录的 method 标记
FAIL_MARKERS = ["skip", "PW:", "dept link not found", "no candidates"]


# ==== 浏览器内 JS ====
# 从当前页面收集首页导航中的院系入口链接
ENTRY_JS = """() => {
  const kws = __KWS__;
  const bad = __NEWS__;
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.textContent || '').trim().replace(/\\s+/g, ' ');
    const h = a.getAttribute('href') || '';
    if (!t || t.length < 2 || t.length > 30) continue;
    if (h.startsWith('#') || h.startsWith('javascript:')) continue;
    let best = 0;
    for (const [k, s] of kws) { if (t.includes(k) && s > best) best = s; }
    if (!best) continue;
    if (bad.some(w => t.includes(w))) continue;
    let full = h;
    if (!/^https?:/.test(h)) {
      try { full = new URL(h, location.href).href; } catch (e) { continue; }
    }
    const key = full.split('?')[0].replace(/\\/+$/, '');
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({t: t, url: full, score: best});
  }
  out.sort((a, b) => b.score - a.score);
  return out;
}"""

# 从当前 (frame) 页面提取所有候选院系链接
# 包含无 href 的锚点 (React 站点如浙大), 尽力推断其目标 URL
EXTRACT_JS = """() => {
  const kw = __KWS__;
  const out = [];
  const seen = new Set();
  // 0) 页面源码中内嵌的 {title: 'xxx', link: 'url'} 数据 (浙大等)
  try {
    const html = document.documentElement.outerHTML;
    const rx = /title:\\s*['\"]([^'\"]{2,60})['\"][\\s\\S]{0,80}?link:\\s*['\"]([^'\"]{5,120})['\"]/g;
    let m;
    while ((m = rx.exec(html)) !== null) {
      const t = m[1].trim();
      const u = m[2];
      let dt = '';
      for (const k of kw) { if (t.includes(k)) { dt = k; break; } }
      if (!dt) continue;
      if (!/^https?:/.test(u)) continue;
      const key = u.split('?')[0].replace(/\\/+$/, '');
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({name: t, url: u, type: dt});
    }
  } catch (e) {}
  // 1) 常规 a[href] + 无 href 锚点
  for (const a of document.querySelectorAll('a')) {
    const t = (a.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length < 3 || t.length > 60) continue;
    if (!/[\\u4e00-\\u9fff]/.test(t)) continue;
    if (/\\d{4}[年./-]\\d/.test(t)) continue;
    let dt = '';
    for (const k of kw) { if (t.includes(k)) { dt = k; break; } }
    if (!dt) continue;
    const h = a.getAttribute('href') || '';
    let full = '';
    if (h && !h.startsWith('#') && !h.startsWith('javascript:')) {
      try { full = new URL(h, location.href).href; } catch (e) { continue; }
    } else {
      // 无 href / # / javascript: → 用最近的带 href 祖先或兄弟链接, 否则用当前页
      const anc = a.closest('[href]') || a.parentElement?.closest('[href]');
      if (anc) {
        const ah = anc.getAttribute('href') || '';
        if (ah && !ah.startsWith('#') && !ah.startsWith('javascript:')) {
          try { full = new URL(ah, location.href).href; } catch (e) {}
        }
      }
      if (!full) full = location.href;
    }
    const key = full.split('?')[0].replace(/\\/+$/, '') + '#' + t.slice(0, 20);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({name: t, url: full, type: dt});
  }
  return out;
}"""

# 探测页面是否被 WAF 拦截 (39 字符空页)
BLOCKED_JS = """() => {
  const n = document.querySelectorAll('a[href]').length;
  return {links: n, chars: document.documentElement.outerHTML.length};
}"""


def _render_js(template, kws=None):
    return (template
            .replace("__KWS__", json.dumps(kws or DEPT_KW, ensure_ascii=False))
            .replace("__NEWS__", json.dumps(ENTRY_NEWS_KW, ensure_ascii=False)))


# ==== LLM 分类 ====
def llm_classify(school_name: str, candidates: list[dict]) -> list[dict]:
    client = get_llm()
    if not client or not client.available:
        return candidates
    results = []
    for i in range(0, len(candidates), 30):
        batch = candidates[i:i + 30]
        links_json = json.dumps(
            [{"name": c["name"], "url": c["url"]} for c in batch],
            ensure_ascii=False)
        prompt = f"""你是高校院系数据分析师。以下是 [{school_name}] 网站上标签含"学院/系/所/中心/实验室/学部/书院/基地"的链接列表。

请判断每个链接对应的实体是否**承担教学或科研任务**的二级组织。

**教学科研组织 (保留):**
- 学院、系、教研室、教学部
- 研究院、研究所、研究中心 (从事学术研究的)
- 学部、书院
- (国家级/省部级/教育部)重点实验室、工程实验室、工程研究中心
- 实验教学中心、实训中心、实践基地

**非教学科研组织 (排除):**
- 行政管理部门: 人事处/财务处/学生处/教务处/后勤/保卫/招生/就业/审计/资产/国资
- 党政机构: 组织部/宣传部/统战部/纪委/机关党委/党校/工会/团委
- 服务窗口: 联系我们/学院地址/学院电话/校园卡/一卡通/邮箱/图书馆/校医院/出版社
- 系统入口: OA系统/教务系统/财务系统/VPN/邮件
- 技术管理: 技术转移中心/实验室管理/实验室风险/设备处/采购/招标
- 继续教育学院/培训中心 (非学历教育管理)
- 新闻/通知链接 (标题 ≥ 25 字且含"召开""举办"等动词的)

**链接列表 (JSON):**
{links_json}

**返回纯 JSON 数组, 只包含教学科研组织, 格式:**
[{{"name": "原名称", "url": "原URL", "type": "学院"}}]
type 只取: 学院 | 系 | 研究院 | 研究所 | 学部 | 书院 | 中心 | 实验室 | 基地

只输出 JSON 数组, 不要其他文字。"""

        try:
            text = client.ask("", prompt, temperature=0.0)
            m = re.search(r"\[[\s\S]*\]", text)
            if m:
                results.extend(json.loads(m.group()))
            else:
                print(f"      ⚠️ LLM 输出无 JSON: {text[:120]}")
        except Exception as e:
            print(f"      ⚠️ LLM error: {e}")

    seen = set()
    uniq = []
    for d in results:
        k = d.get("url", "").split("?")[0].rstrip("/")
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


# ==== 页面操作 ====
def extract_candidates(page) -> list[dict]:
    """从页面所有 frame 提取候选院系链接, 去重"""
    results = []
    for frame in page.frames:
        try:
            r = frame.evaluate(_render_js(EXTRACT_JS))
            if r:
                results.extend(r)
        except Exception:
            pass
    seen = set()
    uniq = []
    for d in results:
        # 无真实链接的项以 (页面URL + 名称) 去重
        k = d["url"].split("?")[0].rstrip("/")
        if d.get("url") == page.url or not d["url"].startswith("http"):
            k = k + "#" + d["name"]
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def goto_and_wait(page, url, wait_ms=3500):
    page.goto(url, timeout=60000)
    page.wait_for_timeout(wait_ms)


def is_blocked(page) -> bool:
    try:
        n = page.evaluate("document.querySelectorAll('a[href]').length")
        if n < 10:
            # 可能是 WAF 挑战尚未完成, 再等一轮
            page.wait_for_timeout(5000)
            n = page.evaluate("document.querySelectorAll('a[href]').length")
        return n < 10
    except Exception:
        return True


def try_extract(browser, url, wait_ms=3000) -> tuple[list, str]:
    """打开 URL 并提取候选; 返回 (candidates, method_suffix)"""
    page = browser.new_page()
    try:
        goto_and_wait(page, url, wait_ms=wait_ms)
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        cand = extract_candidates(page)
        return cand, page.url
    except Exception as e:
        print(f"    ⚠️ {url}: {str(e)[:80]}")
        return [], ""
    finally:
        try:
            page.close()
        except Exception:
            pass


def try_entry_probe(browser, website: str, extra_urls: list[str]) -> list[str]:
    """对疑似 WAF 拦截站点, 尝试直接打开入口候选 URL"""
    got = []
    for u in extra_urls:
        if not u:
            continue
        page = browser.new_page()
        try:
            goto_and_wait(page, u, wait_ms=4000)
            n = page.evaluate("document.querySelectorAll('a[href]').length")
            if n >= 10:
                got.append(u)
                print(f"    🔗 直连入口可用: {u} ({n} links)")
                break
        except Exception:
            pass
        finally:
            try:
                page.close()
            except Exception:
                pass
    return got


# ==== 子域名探测: 主站被 WAF 拦截时, 部分学院子站可直接访问 ====
SUB_DOMAINS = {
    "兰州大学": ["chinese", "history", "ms", "law", "math", "phy", "atmos", "geoscience",
                 "sph", "tyb", "eco"],
    "南京师范大学": [],
    "成都理工大学": [],
}


def probe_subdomains(browser, name: str) -> list[dict]:
    """探测可访问的学院子站, 返回 {name, url} 列表 (主域名内无法探测 DNS)"""
    subs = SUB_DOMAINS.get(name, [])
    if not subs:
        return []
    domain = ""
    if name == "兰州大学":
        domain = "lzu.edu.cn"
    elif name == "南京师范大学":
        domain = "njnu.edu.cn"
    elif name == "成都理工大学":
        domain = "cdut.edu.cn"
    if not domain:
        return []
    found = []
    for sub in subs:
        url = f"https://{sub}.{domain}/"
        page = browser.new_page()
        try:
            goto_and_wait(page, url, wait_ms=3000)
            n = page.evaluate("document.querySelectorAll('a[href]').length")
            if n >= 10:
                title = page.title() or ""
                print(f"    🌐 子站可达: {sub} ({title[:30]})")
                cand = extract_candidates(page)
                if cand:
                    found.extend(cand)
        except Exception:
            pass
        finally:
            try:
                page.close()
            except Exception:
                pass
    return found


# ==== 单校处理 ====
def process_school(browser, name: str, website: str, known_urls: list[str],
                   headless: bool = True) -> dict:
    llm = get_llm()
    if llm:
        llm.begin_task()  # 任务边界: 每个学校开始前重新均衡分配 token (校内连续调用固定同一 token)
    mode = "headless" if headless else "headed"
    result = {"school_name": name, "school_url": website,
              "dept_page_url": "", "method": f"CloakBrowser {mode}",
              "departments": []}
    page = browser.new_page()
    try:
        # 1. 打开官网首页
        goto_and_wait(page, website)
        blocked = is_blocked(page)
        if blocked:
            content_len = len(page.content())
            print(f"    ⚠️ 首页被拦截 ({content_len} chars)")
            # 尝试直接打开已知入口 URL (绕过首页)
            direct = try_entry_probe(browser, website, known_urls)
            if not direct:
                # 2b. 子域名探测 (部分学院子站可直连)
                sub_cand = probe_subdomains(browser, name)
                if sub_cand:
                    # 去重
                    seen = set()
                    uniq = []
                    for d in sub_cand:
                        k = d["url"].split("?")[0].rstrip("/") + "#" + d["name"][:20]
                        if k not in seen:
                            seen.add(k)
                            uniq.append(d)
                    # LLM 分类过滤
                    depts = llm_classify(name, uniq)
                    result["dept_page_url"] = website
                    result["departments"] = depts
                    result["method"] = (f"CloakBrowser {mode} subdomain-probe → LLM "
                                        f"({len(depts)}/{len(uniq)}, 主站被 WAF 拦截)")
                    print(f"    🏫 子站探测: {len(uniq)} 候选 → LLM {len(depts)} 教学科研")
                    return result
                result["method"] += f" — blocked ({content_len} chars)"
                return result
            first_dept = direct[0]
        else:
            # 2. 从首页导航找院系入口
            entries = []
            try:
                entries = page.evaluate(_render_js(ENTRY_JS, ENTRY_KEYWORDS)) or []
            except Exception as e:
                print(f"    ⚠️ entry JS: {e}")
            entry_urls = []
            for e in entries:
                if e["url"] not in entry_urls:
                    entry_urls.append(e["url"])
            if entry_urls:
                print(f"    🔗 首页院系入口: {len(entry_urls)} 个")
                for e in entries[:5]:
                    print(f"        {e['t']} ({e['score']}) → {e['url']}")
            else:
                print("    ⚠️ 首页未找到院系入口, 回退到已知 URL")
            first_dept = entry_urls[0] if entry_urls else (known_urls[0] if known_urls else "")
            if not first_dept:
                result["method"] += " — dept link not found"
                return result

        # 3. 逐个进入入口/列表页提取候选
        if blocked:
            try_urls = list(direct)
        else:
            try_urls = [u for u in known_urls if u] + entry_urls[:6]
        all_candidates = []
        first_dept_url = first_dept
        for url in try_urls:
            cand, _ = try_extract(browser, url, wait_ms=3000)
            if cand:
                all_candidates.extend(cand)
                print(f"    📄 {url} → {len(cand)} 候选")
            if len(all_candidates) > 10:
                break

        if not all_candidates:
            result["method"] += " — no candidates"
            return result

        # 全局去重 (无 href 的锚点项保留)
        seen = set()
        uniq = []
        for d in all_candidates:
            k = d["url"].split("?")[0].rstrip("/")
            if not d["url"].startswith("http") or d["url"].rstrip("/") == first_dept_url.rstrip("/"):
                k = k + "#" + d["name"]
            if k not in seen:
                seen.add(k)
                uniq.append(d)

        # 4. LLM 分类
        depts = llm_classify(name, uniq)
        result["dept_page_url"] = first_dept_url
        result["departments"] = depts
        result["method"] = f"CloakBrowser {mode} → LLM ({len(depts)}/{len(uniq)})"
        print(f"    🤖 LLM: {len(depts)} 教学科研 / {len(uniq)} 候选")
        return result

    finally:
        try:
            page.close()
        except Exception:
            pass


# ==== 已知院系 URL (来自 FAILURE_ANALYSIS.md 人工验证) ====
KNOWN_DEPT_URLS = {
    "兰州大学": ["https://www.lzu.edu.cn/xxjgs/jxky.htm"],
    "南京师范大学": ["https://www.njnu.edu.cn/xysz.htm"],
    "成都理工大学": ["https://www.cdut.edu.cn/jgsz1/jxkydw.htm"],
    "南京理工大学": ["https://www.njust.edu.cn/csxybm/list.htm"],
    "四川大学": ["https://www.scu.edu.cn/zzjg1/yx.htm"],
    "浙江大学": ["https://www.zju.edu.cn/xywxw/list.htm"],
    "中国政法大学": ["https://www.cupl.edu.cn/jxjg1.htm"],
    "武汉理工大学": ["https://www.whut.edu.cn/xuexiaogaikuang/xysz/"],
    "华南理工大学": ["https://www.scut.edu.cn/new/8996/list.htm"],
}

# ==== 手动兜底: WAF 完全不可达 (瑞数 412/400, IP 封禁) ====
# 南京师范大学/成都理工大学: 主站及子站全部被 WAF 拦截, 无法实时采集。
# 以下名单来自学校官方公开信息 (阳光高考院校信息库 2026-05 更新 / 学校官网公布),
# 每所学校仅给出院系名称, URL 留空 (无法从官网验证子站地址)。
MANUAL_FALLBACK = {
    "南京师范大学": [
        "强化培养学院", "教师教育学院", "国际文化教育学院", "金陵女子学院",
        "公共管理学院", "商学院", "法学院", "马克思主义学院", "教育科学学院",
        "心理学院", "体育科学学院", "文学院", "外国语学院", "新闻与传播学院",
        "社会发展学院", "数学科学学院", "物理科学与技术学院", "化学与材料科学学院",
        "地理科学学院", "生命科学学院", "能源与机械工程学院", "电气与自动化工程学院",
        "计算机与电子信息学院/人工智能学院", "环境学院", "海洋学院", "食品与制药工程学院",
        "音乐学院", "美术学院", "南京师范大学麦考瑞大学联合学院",
    ],
    "成都理工大学": [
        "地球与行星科学学院", "能源学院", "环境与土木工程学院", "地球物理学院",
        "核技术与自动化工程学院", "材料与化学化工学院", "管理科学学院",
        "马克思主义学院", "文法学院", "外国语学院", "商学院", "传播科学与艺术学院",
        "体育学院", "计算机与网络安全学院", "地理与规划学院", "生态环境学院",
        "数学科学学院", "机电工程学院", "沉积地质研究院", "物理学院",
        "锂资源与锂电产业学院", "国际教育学院（牛津布鲁克斯学院）",
    ],
}


# ==== 文件读写 ====
def write_jsonl(existing: dict, order: list[str]):
    with open(JSONL_FILE, "w", encoding="utf-8") as f:
        for n in order:
            f.write(json.dumps(existing[n], ensure_ascii=False) + "\n")


def rewrite_csv(existing: dict, order: list[str]):
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["学校名称", "学校网址", "院系名称", "院系URL", "院系类型", "采集方式"])
        for n in order:
            r = existing[n]
            method = r.get("method", "")
            if r.get("departments"):
                for d in r["departments"]:
                    w.writerow([r["school_name"], r["school_url"],
                                d["name"], d["url"], d.get("type", ""), method])
            else:
                w.writerow([r["school_name"], r["school_url"], "", "", "", method])


# ==== 主流程 ====
def main():
    parser = argparse.ArgumentParser(description="剩余失败院校补采 (CloakBrowser)")
    parser.add_argument("--school", action="append", help="只处理指定学校 (可多次)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 所")
    args = parser.parse_args()

    llm = get_llm()
    if not llm or not llm.available:
        print("❌ LLM 不可用 (检查 college teacher/.env 的 LLM_API_KEY / LLM_BASE_URL)")
        return

    existing = {}
    order = []
    with open(JSONL_FILE, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            existing[r["school_name"]] = r
            order.append(r["school_name"])

    # 自动找出失败记录
    targets = [n for n in order
               if any(mk in existing[n].get("method", "") for mk in FAIL_MARKERS)]
    if args.school:
        targets = [s for s in args.school if s in existing]
        if not targets:
            print("❌ 未在 JSONL 中找到指定学校:", args.school)
            return
    if args.limit > 0:
        targets = targets[:args.limit]

    print("=" * 64)
    print("🔧 剩余失败院校补采 (CloakBrowser)")
    print(f"   目标: {len(targets)} 所")
    for t in targets:
        print(f"      - {t}  [{existing[t].get('method', '')[:60]}]")
    print("=" * 64)

    from cloakbrowser import launch

    for i, name in enumerate(targets):
        r = existing[name]
        known = list(KNOWN_DEPT_URLS.get(name, []))
        if r.get("dept_page_url") and r["dept_page_url"] not in known:
            known.insert(0, r["dept_page_url"])
        website = r.get("school_url", "") or ""
        if not website:
            website = f"https://{name}.edu.cn"
        print(f"\n{'─'*56}")
        print(f"  [{i+1}/{len(targets)}] {name}  ({website})")

        # 1. headless
        browser = launch(headless=True)
        try:
            new_r = process_school(browser, name, website, known, headless=True)
        finally:
            try:
                browser.close()
            except Exception:
                pass

        # 2. 若失败且尚未重试 → headed 模式 (川大等站点需 headed)
        if (not new_r["departments"]) and not new_r["method"].endswith("(retried)"):
            print(f"    ↻ 尝试 headed 模式...")
            browser = launch(headless=False)
            try:
                new_r2 = process_school(browser, name, website, known, headless=False)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
            if new_r2["departments"]:
                new_r = new_r2

        # 3. 全部模式被 WAF 拦截 → 手动兜底名单 (来自学校官方公开信息)
        if not new_r["departments"] and name in MANUAL_FALLBACK:
            depts = [{"name": n, "url": "", "type": "学院"} for n in MANUAL_FALLBACK[name]]
            new_r = {
                "school_name": name,
                "school_url": website,
                "dept_page_url": "",
                "method": f"manual fallback (WAF 拦截, 官方公开名单 {len(depts)} 院系)",
                "departments": depts,
            }
            print(f"    📋 手动兜底: {len(depts)} 院系 (官方公开名单)")

        existing[name] = new_r
        write_jsonl(existing, order)
        print(f"    ✅ 已更新: {new_r['method']} / {len(new_r['departments'])} 院系")
        time.sleep(0.5)

    rewrite_csv(existing, order)

    success = sum(1 for n in order if existing[n].get("departments"))
    total = sum(len(existing[n].get("departments", [])) for n in order)
    print(f"\n{'='*64}")
    print(f"📊 完成: {success}/{len(order)} 所有学校, 累计 {total} 院系")


if __name__ == "__main__":
    main()
