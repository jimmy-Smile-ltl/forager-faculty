"""合并官网列 → qs_rankings_2027_with_website.csv + 人工复核清单

来源: raw/bing_websites.json (nid → website)
未命中/低置信(子域/非标准域) → raw/no_website.json 人工复核
"""
from __future__ import annotations

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw", "all_score_nodes.json")
WEBSITES = os.path.join(HERE, "raw", "bing_websites.json")
CSV_OUT = os.path.join(HERE, "qs_rankings_2027_with_website.csv")
MISS_OUT = os.path.join(HERE, "raw", "no_website.json")

# 子域/非标准域 → 人工复核标记 (主域优先: 命中子域但同注册域正确, 标记低置信)
LOW_CONF_SUFFIX = ("/about", "/study", "/admission", "/apply", "/prospective")

# 教育域后缀: 命中但域名不含这些 → 标记低置信 (旅游指南站等非大学域)
EDU_DOMAIN_MARKS = (".edu", ".ac.", ".edu.", ".uni", ".univ", ".university",
                    ".unam", ".u-", ".uh", ".um", ".up", ".ju", ".tum",
                    ".kth", ".ethz", ".tu-", ".polito", ".unist", ".tau", ".wur")


def is_edu_domain(url: str) -> bool:
    host = url.split("//")[-1].split("/")[0].lower().lstrip("www.")
    return any(m in host for m in EDU_DOMAIN_MARKS)


def main() -> int:
    nodes = json.load(open(RAW))
    sites = json.load(open(WEBSITES))

    HEAD = ["排名", "排名序号", "学校名", "国家", "是否中国", "所属地区", "城市", "大洲", "总分",
            "学术声誉", "雇主声誉", "师生比", "师均引用", "国际教师比例",
            "国际学生比例", "国际研究网络", "就业成果", "可持续性", "详情页URL", "官网", "官网验证"]
    IND = ["Academic Reputation", "Employer Reputation", "Faculty Student Ratio",
           "Citations per Faculty", "International Faculty Ratio",
           "International Student Ratio", "International Research Network",
           "Employment Outcomes", "Sustainability Score"]

    # 遵循一个中国原则: 大陆/香港/澳门/台湾均为中国领土 (主权完整)
    CN_TERRITORIES = {"China (Mainland)", "Hong Kong SAR", "Macau SAR", "Taiwan"}
    # 两岸四地具体从属标注
    CN_REGION = {
        "China (Mainland)": "中国大陆",
        "Hong Kong SAR": "中国香港",
        "Macau SAR": "中国澳门",
        "Taiwan": "中国台湾",
    }
    # 海外国家/地区中文标注
    COUNTRY_CN = {
        "United States": "美国", "United Kingdom": "英国", "Germany": "德国",
        "India": "印度", "Spain": "西班牙", "Italy": "意大利",
        "South Korea": "韩国", "Japan": "日本", "France": "法国",
        "Australia": "澳大利亚", "Russia": "俄罗斯", "Malaysia": "马来西亚",
        "Canada": "加拿大", "Türkiye": "土耳其", "Saudi Arabia": "沙特阿拉伯",
        "Brazil": "巴西", "Indonesia": "印度尼西亚", "Poland": "波兰",
        "Mexico": "墨西哥", "Kazakhstan": "哈萨克斯坦", "Egypt": "埃及",
        "Pakistan": "巴基斯坦", "Chile": "智利", "Argentina": "阿根廷",
        "Czechia": "捷克", "Jordan": "约旦", "Netherlands": "荷兰",
        "Thailand": "泰国", "Bangladesh": "孟加拉国",
        "United Arab Emirates": "阿联酋", "South Africa": "南非",
        "Colombia": "哥伦比亚", "Switzerland": "瑞士", "Portugal": "葡萄牙",
        "Iran": "伊朗", "Belgium": "比利时", "Austria": "奥地利",
        "Greece": "希腊", "Azerbaijan": "阿塞拜疆", "Hungary": "匈牙利",
        "Sweden": "瑞典", "Finland": "芬兰", "Lebanon": "黎巴嫩",
        "Vietnam": "越南", "Ukraine": "乌克兰", "New Zealand": "新西兰",
        "Ireland": "爱尔兰", "Norway": "挪威", "Romania": "罗马尼亚",
        "Oman": "阿曼", "Iraq": "伊拉克", "Denmark": "丹麦",
        "Israel": "以色列", "Peru": "秘鲁", "Uzbekistan": "乌兹别克斯坦",
        "Slovakia": "斯洛伐克", "Ecuador": "厄瓜多尔", "Philippines": "菲律宾",
        "Lithuania": "立陶宛", "Kyrgyzstan": "吉尔吉斯斯坦", "Nigeria": "尼日利亚",
        "Singapore": "新加坡", "Estonia": "爱沙尼亚", "Uruguay": "乌拉圭",
        "Tunisia": "突尼斯", "Croatia": "克罗地亚", "Venezuela": "委内瑞拉",
        "Sri Lanka": "斯里兰卡", "Costa Rica": "哥斯达黎加", "Cyprus": "塞浦路斯",
        "Slovenia": "斯洛文尼亚", "Kuwait": "科威特", "Bahrain": "巴林",
        "Northern Cyprus": "北塞浦路斯", "Latvia": "拉脱维亚",
        "Palestinian Territories": "巴勒斯坦", "Georgia": "格鲁吉亚",
        "Qatar": "卡塔尔", "Brunei": "文莱", "Belarus": "白俄罗斯",
        "Bulgaria": "保加利亚", "Serbia": "塞尔维亚", "Cuba": "古巴",
        "Ghana": "加纳", "Morocco": "摩洛哥", "Guatemala": "危地马拉",
        "Dominican Republic": "多米尼加", "Luxembourg": "卢森堡",
        "Iceland": "冰岛", "Malta": "马耳他", "Ethiopia": "埃塞俄比亚",
        "Uganda": "乌干达", "Puerto Rico": "波多黎各", "Kenya": "肯尼亚",
        "Armenia": "亚美尼亚", "Syria": "叙利亚", "Paraguay": "巴拉圭",
        "Tanzania": "坦桑尼亚", "Sudan": "苏丹", "Honduras": "洪都拉斯",
        "Panama": "巴拿马", "Bosnia & Herzegovina": "波黑",
    }

    review = []      # 人工复核清单
    hit = low = miss = unver = 0
    with open(CSV_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        for idx, n in enumerate(nodes, 1):
            nid = n["nid"]
            sc = {}
            for grp in n.get("scores", {}).values():
                for i in grp:
                    sc[i["indicator_name"]] = i.get("score", "")
            path = n.get("path", "")
            country = n.get("country", "")
            territory = CN_REGION.get(country, COUNTRY_CN.get(country, country))
            row = [
                n.get("rank_display", ""), idx, n.get("title", ""), country,
                1 if country in CN_TERRITORIES else 0, territory,
                n.get("city", ""), n.get("region", ""),
                n.get("overall_score", ""),
                *[sc.get(k, "") for k in IND],
                ("https://www.topuniversities.com" + path) if path else "",
            ]
            ww = (sites.get(str(nid)) or {}).get("website", "") if sites.get(str(nid)) else ""
            if not ww and sites.get(str(nid)):
                ww = sites[str(nid)].get("website", "")
            ver = (sites.get(str(nid)) or {}).get("verify", "") if sites.get(str(nid)) else ""
            row.append(ww)
            row.append(ver or ("已验证" if ww else ""))

            if not ww:
                miss += 1
                review.append({"rank": n.get("rank_display"), "name": n.get("title", ""),
                               "country": n.get("country", ""), "reason": "未命中"})
            elif ver == "rejected":
                low += 1
                review.append({"rank": n.get("rank_display"), "name": n.get("title", ""),
                               "country": n.get("country", ""), "website": ww,
                               "reason": f"内容不匹配({(sites.get(str(nid)) or {}).get('verify_desc', '')[:40]})"})
            elif ver == "unverified":
                unver += 1
                review.append({"rank": n.get("rank_display"), "name": n.get("title", ""),
                               "country": n.get("country", ""), "website": ww,
                               "reason": f"未验证({(sites.get(str(nid)) or {}).get('verify_desc', '')[:40]})"})
            elif not is_edu_domain(ww) and ver != "verified":
                low += 1
                review.append({"rank": n.get("rank_display"), "name": n.get("title", ""),
                               "country": n.get("country", ""), "website": ww,
                               "reason": "低置信(非教育域)"})
            elif (n.get("rank_display", "") != "-" and
                  any(ww.rstrip("/").lower().endswith(s) for s in LOW_CONF_SUFFIX)):
                low += 1
                review.append({"rank": n.get("rank_display"), "name": n.get("title", ""),
                               "country": n.get("country", ""), "website": ww,
                               "reason": "低置信(子路径/子域)"})
            else:
                hit += 1
            w.writerow(row)

    json.dump(review, open(MISS_OUT, "w"), ensure_ascii=False, indent=1)
    print(f"命中 {hit} / 低置信 {low} / 未验证 {unver} / 未命中 {miss} / 共 {len(nodes)}")
    print(f"CSV: {CSV_OUT}")
    print(f"人工复核清单: {MISS_OUT} ({len(review)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
