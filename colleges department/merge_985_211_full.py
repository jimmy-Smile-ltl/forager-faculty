#!/usr/bin/env python3
"""
985/211/双一流 全字段清单 —— 三源合并

以 985_211_双一流高校清单.csv (144 所) 为主键，合并:
  - colleges info/unified_colleges.csv: MOE(标识码/主管部门/所在地/办学层次) + 排名(软科/校友会/QS/US News/学科评估)
  - colleges info/chsi/chsi_colleges.csv: orgCode + 联系方式(网址/电话/微信/微博/百家号/新媒体)
按 软科排名 升序输出 (未上榜排最后)。

用法:
  cd "colleges department" && uv run python merge_985_211_full.py
"""

import csv
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
PARENT = BASE.parent  # college teacher/

REF_FILE = BASE / "985_211_双一流高校清单.csv"
OUTPUT = BASE / "985_211_双一流高校清单_全字段.csv"
UNIFIED = PARENT / "colleges info/unified_colleges.csv"
CHSI = PARENT / "colleges info/chsi/chsi_colleges.csv"

# 输出列: 参考清单 + MOE + 排名 + CHSI
HEADER = [
    "序号", "学校名称",
    # MOE
    "学校标识码", "主管部门", "所在地", "办学层次",
    # 参考清单
    "is_985", "is_211", "is_first_flow", "院校特色",
    # 排名
    "软科排名", "校友会排名", "QS排名", "QS世界排名", "US News排名", "武书连排名", "学科评估",
    # CHSI
    "orgCode", "学校网址", "招生网址", "咨询电话",
    "微信名称", "微信号", "微博名称", "微博网址",
    "百家号名称", "百家号ID", "新媒体名称", "新媒体网址",
]

RANK_COLS = ["软科排名", "校友会排名", "QS排名", "QS世界排名", "US News排名", "武书连排名", "学科评估"]
CHSI_COLS = ["orgCode", "学校网址", "招生网址", "咨询电话",
             "微信名称", "微信号", "微博名称", "微博网址",
             "百家号名称", "百家号ID", "新媒体名称", "新媒体网址"]
MOE_COLS = ["学校标识码", "主管部门", "所在地", "办学层次"]


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def normalize(name: str) -> str:
    """统一括号 (全角→半角)、去空格"""
    n = (name or "").strip().replace("（", "(").replace("）", ")").replace("　", "")
    return n


def build_lookup(rows, key="学校名称"):
    """名称索引: 标准化后精确匹配"""
    return {normalize(r[key]): r for r in rows}


def find_chsi(chsi_lookup, chsi_rows, name):
    """chsi 匹配: 精确 → 子串 (华北电力大学 → 华北电力大学(北京), 优先主校区北京)"""
    n = normalize(name)
    if n in chsi_lookup:
        return chsi_lookup[n]
    # 子串匹配
    cands = []
    for r in chsi_rows:
        cn = normalize(r["学校名称"])
        if n in cn and len(cn) > len(n):
            cands.append(r)
    if not cands:
        return None
    # 多校区: 优先"北京"(主校区)
    cands.sort(key=lambda r: 0 if "北京" in r["学校名称"] else 1)
    return cands[0]


def sort_key(row):
    """软科排名排序键: 数值升序; '500+' 视为 501; 空值最后 (按名称)")
    """
    v = (row.get("软科排名") or "").strip()
    if not v:
        return (2, "", row["学校名称"])
    if v == "500+":
        return (1, "500+", row["学校名称"])
    try:
        return (0, f"{int(v):05d}", row["学校名称"])
    except ValueError:
        return (1, v, row["学校名称"])


def main():
    ref = load(REF_FILE)
    uni = load(UNIFIED)
    chsi = load(CHSI)

    uni_lookup = build_lookup(uni)
    chsi_lookup = build_lookup(chsi)

    print(f"参考清单: {len(ref)} 所")
    print(f"unified: {len(uni)} 行 | chsi: {len(chsi)} 行")

    rows = []
    missing_uni = []
    missing_chsi = []
    for i, r in enumerate(ref, 1):
        name = r["学校名称"].strip()
        ur = uni_lookup.get(normalize(name), {})
        cr = find_chsi(chsi_lookup, chsi, name)

        if not ur:
            missing_uni.append(name)
        if not cr:
            missing_chsi.append(name)

        row = {"序号": "", "学校名称": name}
        for c in MOE_COLS:
            row[c] = ur.get(c, "")
        for c in ["is_985", "is_211", "is_first_flow", "院校特色"]:
            row[c] = ur.get(c, r.get(c, ""))
        for c in RANK_COLS:
            row[c] = ur.get(c, "")
        for c in CHSI_COLS:
            row[c] = (cr or {}).get(c, "")
        rows.append(row)

    # 按软科排名排序
    rows.sort(key=sort_key)
    for i, row in enumerate(rows, 1):
        row["序号"] = str(i)

    with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)

    # ==== 统计 ====
    print(f"\n📊 输出: {OUTPUT}")
    print(f"   总行数: {len(rows)}")
    if missing_uni:
        print(f"   ⚠️ 未匹配 unified: {missing_uni}")
    if missing_chsi:
        print(f"   ⚠️ 未匹配 chsi: {missing_chsi}")
    for c in ["软科排名", "校友会排名", "QS世界排名", "US News排名", "学科评估", "orgCode", "咨询电话", "学校网址"]:
        n = sum(1 for r in rows if r.get(c))
        print(f"   有{c}: {n}/{len(rows)}")

    # 前 10 名预览
    print("\n   📌 软科排名前 10:")
    for r in rows[:10]:
        print(f"     {r['软科排名']:>5}  {r['学校名称']}  (985:{r['is_985']} 211:{r['is_211']} 一流:{r['is_first_flow']})")


if __name__ == "__main__":
    main()
