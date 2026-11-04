"""旁路精准权威验证:
1. 搜索引挚索引块级反查 (严格匹配 li.b_algo 内的域名必须为目标域名，提取权威标题)
2. `"{clean_name}" site:{host}` 针对性探测，穿透 WAF/网络限制
3. 国家法定教育顶级域名 (Edu/Ac TLD) + 核心词 / 缩写 / 连写三角鉴权
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse

import httpx

import bing_websites as bw
import verify_websites as vw

PROGRESS = bw.PROGRESS


def search_verified_title(client: httpx.Client, name: str, host: str) -> tuple[bool, str]:
    """通过 Bing 搜索 `"{clean_name}" site:{host}` 获取该域名在搜索引擎索引中的真实 Title"""
    clean_name = bw.clean(name)
    queries = [f'"{clean_name}" site:{host}', f'site:{host}']

    for q in queries:
        search_url = f"https://www.bing.com/search?q={urllib.parse.quote(q)}"
        try:
            r = client.get(search_url, headers=bw.HEADERS, timeout=12.0)
            if r.status_code != 200:
                continue

            # 严格遍历 b_algo 块
            blocks = re.findall(r'<li class="b_algo".*?</li>', r.text, re.S)
            for b in blocks[:5]:
                # 提取链接
                m_href = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"', b, re.S)
                if not m_href:
                    continue
                href = m_href.group(1)
                # 校验该结果是不是目标域名的页面 (排除 Bing 推荐的相关搜索/广告)
                res_host = bw.host_of(href)
                reg_target = bw.reg_domain(host)
                if res_host and (res_host == host or bw.reg_domain(res_host) == reg_target or res_host.endswith(f".{reg_target}")):
                    m_title = re.search(r'<h2[^>]*>.*?<a[^>]*>(.*?)</a>', b, re.S)
                    if m_title:
                        raw_t = m_title.group(1)
                        t = html.unescape(re.sub(r'<[^>]+>', '', raw_t)).strip()
                        if not t:
                            continue
                        # 判定标题是否匹配校名
                        if bw.name_matches_title(t, name):
                            return True, f"快照Title匹配({t[:35]})"
                        core = vw._tokens_loose(name)
                        t_tokens = vw._tokens_loose(t)
                        if (core & t_tokens) and (bw._core_hit(core, reg_target) or vw._abbr_hit(name, reg_target)):
                            return True, f"快照核心词({t[:35]})"
                        if vw._cjk_uni_title(t) or vw.title_has_uni_word(t):
                            return True, f"快照大学词({t[:35]})"
        except Exception:
            pass
    return False, ""


def is_state_accredited_edu_domain(reg: str) -> bool:
    """国家法定教育/学术顶级域名判定"""
    return (
        reg.endswith((
            ".edu", ".ac.uk", ".edu.au", ".edu.sg", ".edu.hk", ".ac.jp", ".ac.kr",
            ".edu.cn", ".edu.tw", ".ac.in", ".edu.pk", ".edu.sa", ".edu.eg",
            ".edu.my", ".edu.mx", ".edu.co", ".edu.ar", ".ac.za", ".ac.nz",
            ".edu.tr", ".edu.br", ".edu.vn", ".edu.ph", ".edu.bd", ".edu.iq",
            ".edu.kz", ".edu.az", ".edu.kg", ".edu.ge", ".edu.ua", ".edu.lb",
            ".edu.jo", ".edu.om", ".edu.qa", ".edu.kw", ".edu.bh", ".edu.gh",
            ".edu.ng", ".edu.et", ".edu.pe", ".edu.ec", ".edu.uy", ".edu.cl",
            ".edu.ve", ".edu.bo", ".edu.cr", ".edu.pr", ".edu.do", ".edu.gt",
            ".edu.pl", ".edu.rs", ".edu.ba", ".edu.mk", ".edu.al", ".edu.mo"
        ))
        or ".edu." in reg
        or ".ac." in reg
        or reg.endswith((".uni-heidelberg.de", ".uni-freiburg.de", ".uni-muenchen.de",
                         ".uni-hamburg.de", ".uni-bonn.de", ".uni-frankfurt.de",
                         ".uni-tuebingen.de", ".uni-goettingen.de", ".uni-koeln.de"))
    )


def verify_bypass_item(name: str, url: str, client: httpx.Client) -> tuple[bool, str]:
    """多维旁路综合交叉鉴权"""
    host = bw.host_of(url)
    reg = bw.reg_domain(host)
    core = vw._tokens_loose(name)

    # 1. 搜索引擎索引块校验
    ok, desc = search_verified_title(client, name, host)
    if ok:
        return True, desc

    # 2. 国家法定教育域 + 核心词 / 缩写 / 连写精准闭环匹配
    if is_state_accredited_edu_domain(reg):
        if bw._core_hit(core, reg):
            return True, f"国家法定教育域名+机构核心词({reg})"
        if vw._abbr_hit(name, reg):
            return True, f"国家法定教育域名+校名缩写({reg})"
        if vw._concat_hit(name, reg):
            return True, f"国家法定教育域名+校名连写({reg})"
        # 名字中包含的任意缩写等于注册域前缀 (如 CUHK -> cuhk.edu.hk, NTU -> ntu.edu.sg)
        for ab in re.findall(r"\b[A-Za-z]{2,8}\b", name):
            if ab.lower() == reg.split(".")[0]:
                return True, f"法定教育域+校名缩写匹配({reg})"

    # 3. 欧陆/国家顶级官方公立高校域名 (如 .de, .fr, .it, .es, .ru 等) + 严格核心词
    if reg.endswith((".de", ".fr", ".it", ".es", ".at", ".ch", ".se", ".no", ".dk", ".fi",
                     ".nl", ".be", ".pl", ".cz", ".hu", ".ro", ".bg", ".hr", ".rs", ".si",
                     ".sk", ".gr", ".ru", ".kz", ".jp", ".kr", ".it", ".cat")):
        if bw._core_hit(core, reg) and (vw._abbr_hit(name, reg) or vw._concat_hit(name, reg) or len(core) >= 2):
            return True, f"欧洲/国家官方高校域名确认({reg})"
        for ab in re.findall(r"\b[A-Za-z]{2,8}\b", name):
            if ab.lower() == reg.split(".")[0]:
                return True, f"国家高校域名+校名缩写({reg})"

    return False, "无法通过旁路验证"


def main() -> int:
    d = json.load(open(PROGRESS, "r", encoding="utf-8"))
    unver = {k: v for k, v in d.items() if (v or {}).get("verify") == "unverified"}
    print(f"当前待旁路验证条目: {len(unver)} 所")

    client = httpx.Client(headers=bw.HEADERS, follow_redirects=True, timeout=12.0)

    verified_cnt = 0
    for i, (k, v) in enumerate(unver.items(), 1):
        name = v.get("name", "")
        url = v.get("website", "")
        success, desc = verify_bypass_item(name, url, client)
        if success:
            verified_cnt += 1
            v["verify"] = "verified"
            v["verify_desc"] = desc
            print(f"[{i}/{len(unver)}] ✓ {name[:36]:<36} -> {desc}")
        else:
            print(f"[{i}/{len(unver)}] ? {name[:36]:<36} -> {desc}")

        if i % 25 == 0:
            vw.safe_save(d, PROGRESS)
        time.sleep(0.3)

    client.close()
    vw.safe_save(d, PROGRESS)

    print(f"\n==========================================")
    print(f"旁路交叉验证完成: 成功验证 {verified_cnt} / {len(unver)} 所！")
    print(f"==========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
