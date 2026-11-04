"""解析 raw/all_score_nodes.json → CSV

列: 排名 | 学校名 | 国家 | 城市 | 大洲 | 总分 | 9 个子项得分 | 详情页URL
"""

from __future__ import annotations

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw", "all_score_nodes.json")
CSV_PATH = os.path.join(HERE, "qs_rankings_2027.csv")

# indicator_name -> 中文列名
INDICATORS = [
    ("Academic Reputation", "学术声誉"),
    ("Employer Reputation", "雇主声誉"),
    ("Faculty Student Ratio", "师生比"),
    ("Citations per Faculty", "师均引用"),
    ("International Faculty Ratio", "国际教师比例"),
    ("International Student Ratio", "国际学生比例"),
    ("International Research Network", "国际研究网络"),
    ("Employment Outcomes", "就业成果"),
    ("Sustainability Score", "可持续性"),
]

HEADERS = ["排名", "学校名", "国家", "城市", "大洲", "总分"] + \
          [cn for _, cn in INDICATORS] + ["详情页URL"]


def main() -> int:
    with open(RAW) as f:
        nodes = json.load(f)

    missing = {}
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for n in nodes:
            sc = {}
            for grp in n.get("scores", {}).values():
                for i in grp:
                    sc[i["indicator_name"]] = i.get("score", "")
            row = [
                n.get("rank_display", ""),
                n.get("title", ""),
                n.get("country", ""),
                n.get("city", ""),
                n.get("region", ""),
                n.get("overall_score", ""),
            ]
            for en, _ in INDICATORS:
                v = sc.get(en, "")
                if not v:
                    missing.setdefault(en, 0)
                    missing[en] += 1
                row.append(v)
            path = n.get("path", "")
            row.append("https://www.topuniversities.com" + path if path else "")
            w.writerow(row)

    print(f"已写入 {CSV_PATH}, {len(nodes)} 行")
    if missing:
        print("缺失子项统计:", missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
