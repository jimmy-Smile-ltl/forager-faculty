#!/usr/bin/env python3
"""
三源数据合并 —— 以教育部名单为主键，关联掌上高考和学信网

合并逻辑:
  1. 精确名称匹配 (去括号标准化)
  2. 子串匹配 (gaokao分校→MOE本部, 如 华北电力大学（北京）→华北电力大学)
  3. MOE独有学校: 模糊匹配找最相似, score≥0.8 且非其他MOE学校的子串

输出:
  - unified_colleges.csv (UTF-8 BOM, 34 列)
"""

import csv
import re
from difflib import SequenceMatcher
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ==== 读取三源 ====
def read_moe():
    rows = {}
    with open(BASE / "moe_gov/全国普通高等学校名单.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = r["学校名称"].strip()
            rows[name] = r
    return rows

def read_gaokao():
    rows = {}
    with open(BASE / "gaokao_cn/gaokao_colleges.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = r["学校名称"].strip()
            rows[name] = r
    return rows

def read_chsi():
    rows = {}
    with open(BASE / "chsi/chsi_colleges.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = r["学校名称"].strip()
            rows[name] = r
    return rows

# ==== 名称标准化 ====
def normalize(name: str) -> str:
    """统一括号、去空格"""
    n = name.strip()
    n = n.replace("（", "(").replace("）", ")")
    n = n.replace("　", "")
    return n

# ==== 匹配引擎 ====
def derive_flags(features: str) -> dict:
    """从院校特色标签派生 is_985 / is_211 / is_first_flow"""
    tags = (features or "").split("、")
    return {
        "is_985": "1" if "985" in tags else "0",
        "is_211": "1" if "211" in tags else "0",
        "is_first_flow": "1" if "双一流" in tags else "0",
    }


def build_matcher(moe_names: list[str], target_names: list[str]):
    """
    为每个 MOE 名称找到最佳 target 匹配。
    返回 dict: moe_name -> target_name (or None)
    """
    # Step 1: 精确匹配 (标准化后)
    norm_target = {normalize(n): n for n in target_names}
    norm_moe = [(n, normalize(n)) for n in moe_names]

    matched = {}
    unmatched_moe = []

    for moe_name, moe_norm in norm_moe:
        if moe_norm in norm_target:
            matched[moe_name] = norm_target[moe_norm]
        else:
            unmatched_moe.append(moe_name)

    # Step 2: 对于未匹配的 MOE，尝试反转——MOE名是否是某个target名的前缀
    still_unmatched = []
    for moe_name in unmatched_moe:
        moe_norm = normalize(moe_name)
        found = None
        for tgt_name in target_names:
            if tgt_name in matched.values():
                continue  # 已被其他 MOE 匹配
            tgt_norm = normalize(tgt_name)
            # gaokao 的 分校/医学院 包含 MOE 本部名
            if moe_norm in tgt_norm and len(tgt_norm) > len(moe_norm):
                # 确认不是"XX大学"匹配到了"XX大学YY学院" (独立学院不该匹配)
                if "学院" in moe_norm or tgt_norm.replace(moe_norm, "").startswith("("):
                    found = tgt_name
                    break
        if found:
            matched[moe_name] = found
        else:
            still_unmatched.append(moe_name)

    # Step 3: 模糊匹配 (仅对仍未匹配的)
    unmatched_targets = [n for n in target_names if n not in matched.values()]
    for moe_name in still_unmatched:
        best, best_score = None, 0
        for tgt_name in unmatched_targets:
            score = SequenceMatcher(None, normalize(moe_name), normalize(tgt_name)).ratio()
            if score > best_score:
                best_score, best = score, tgt_name
        if best_score >= 0.85:
            matched[moe_name] = best

    return matched


# ==== 主流程 ====
def main():
    moe = read_moe()
    gaokao = read_gaokao()
    chsi = read_chsi()

    moe_names = list(moe.keys())
    gk_names = list(gaokao.keys())
    chsi_names = list(chsi.keys())

    print(f"教育部: {len(moe)} 条")
    print(f"掌上高考: {len(gaokao)} 条")
    print(f"学信网: {len(chsi)} 条")

    # 匹配 gaokao
    print("\n🔗 匹配 掌上高考 ...")
    gk_match = build_matcher(moe_names, gk_names)
    gk_matched = sum(1 for v in gk_match.values() if v)
    print(f"   匹配成功: {gk_matched}/{len(moe)}")

    # 匹配 chsi
    print("🔗 匹配 学信网 ...")
    chsi_match = build_matcher(moe_names, chsi_names)
    chsi_matched = sum(1 for v in chsi_match.values() if v)
    print(f"   匹配成功: {chsi_matched}/{len(moe)}")

    # ==== 输出大表 ====
    OUTPUT = BASE / "unified_colleges.csv"

    HEADER = [
        # 教育部
        "序号", "学校名称", "学校标识码", "主管部门", "所在地", "办学层次", "备注",
        # 掌上高考
        "校徽URL", "院校特色", "院校类别", "办学性质", "保研星级", "详情页URL",
        # 学信网
        "学校网址", "招生网址", "咨询电话",
        "微信名称", "微信号", "微博名称", "微博网址",
        "百家号名称", "百家号ID", "新媒体名称", "新媒体网址",
        # 掌上高考(派生)
        "is_985", "is_211", "is_first_flow",
        # 掌上高考(排名, info.json)
        "软科排名", "校友会排名", "QS排名", "QS世界排名",
        "US News排名", "武书连排名", "学科评估",
    ]

    RANK_COLS = [
        "软科排名", "校友会排名", "QS排名", "QS世界排名",
        "US News排名", "武书连排名", "学科评估",
    ]

    with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)

        for moe_name in moe_names:
            mr = moe[moe_name]

            gr = gaokao.get(gk_match.get(moe_name), {})
            cr = chsi.get(chsi_match.get(moe_name), {})
            flags = derive_flags(gr.get("院校特色", ""))

            row = [
                mr.get("序号", ""),
                moe_name,
                mr.get("学校标识码", ""),
                mr.get("主管部门", ""),
                mr.get("所在地", ""),
                mr.get("办学层次", ""),
                mr.get("备注", ""),
                gr.get("校徽URL", ""),
                gr.get("院校特色", ""),
                gr.get("院校类别", ""),
                gr.get("办学性质", ""),
                gr.get("保研星级", ""),
                gr.get("详情页URL", ""),
                cr.get("学校网址", ""),
                cr.get("招生网址", ""),
                cr.get("咨询电话", ""),
                cr.get("微信名称", ""),
                cr.get("微信号", ""),
                cr.get("微博名称", ""),
                cr.get("微博网址", ""),
                cr.get("百家号名称", ""),
                cr.get("百家号ID", ""),
                cr.get("新媒体名称", ""),
                cr.get("新媒体网址", ""),
                flags["is_985"],
                flags["is_211"],
                flags["is_first_flow"],
                *[gr.get(c, "") for c in RANK_COLS],
            ]
            writer.writerow(row)

    # ==== 统计 ====
    with open(OUTPUT, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    has_features = sum(1 for r in rows if r["院校特色"])
    has_website = sum(1 for r in rows if r["学校网址"])
    has_logo = sum(1 for r in rows if r["校徽URL"])
    has_baoyan = sum(1 for r in rows if r["保研星级"])
    has_phone = sum(1 for r in rows if r["咨询电话"])

    print(f"\n📊 输出: {OUTPUT}")
    print(f"   总行数: {len(rows)}")
    print(f"   有院校特色: {has_features} ({has_features*100//len(rows)}%)")
    print(f"   有学校网址: {has_website} ({has_website*100//len(rows)}%)")
    print(f"   有校徽URL: {has_logo} ({has_logo*100//len(rows)}%)")
    print(f"   有保研星级: {has_baoyan} ({has_baoyan*100//len(rows)}%)")
    print(f"   有咨询电话: {has_phone} ({has_phone*100//len(rows)}%)")
    print(f"   有软科排名: {sum(1 for r in rows if r['软科排名'])}")
    print(f"   有QS世界排名: {sum(1 for r in rows if r['QS世界排名'])}")
    print(f"   有学科评估: {sum(1 for r in rows if r['学科评估'])}")

    # 清华大学 样本
    tsinghua = [r for r in rows if r["学校名称"] == "清华大学"][0]
    print(f"\n📌 清华大学 样本:")
    for k, v in tsinghua.items():
        if v:
            print(f"   {k}: {v}")

    # 无匹配的
    no_gk = [r for r in rows if not r["院校特色"] and not r["校徽URL"]]
    print(f"\n⚠️ 未匹配到掌上高考数据: {len(no_gk)} 所")
    for r in no_gk[:10]:
        print(f"   {r['学校名称']}")


if __name__ == "__main__":
    main()
