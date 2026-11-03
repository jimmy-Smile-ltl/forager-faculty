#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_sample_eval.py — LLM 抽样语义评估 departments.csv

分层抽样：空校/极小学校/大校/规则噪声校/特殊院校/普通校各取若干，
每校最多取 12 行，用 Claude Haiku 4.5 对每行判三件事:
  noise   — 是否新闻/行政/导航/汇总页等非院系条目
  type_ok — 院系类型标签是否准确
  url_ok  — URL 是否指向该院系本体（而非文章/列表/失效链接）

输出: llm_eval_results.json（含每行判定与理由）
运行: 在项目根 .venv 下执行（需 ANTHROPIC_* 环境变量，见 .env）
"""

import csv
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # college teacher/ 根 (统一 util 层)
from util.llm import get_llm

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
RESULT_FILE = BASE_DIR / "llm_eval_results.json"
MAX_ROWS_PER_SCHOOL = 12

# 分层抽样名单（依据当前快照的问题分层；空校无需LLM，直接0分）
SAMPLE_SCHOOLS = [
    # 极小学校（疑似采错页面）
    "天津大学", "中央美术学院", "天津医科大学",
    # 大校
    "复旦大学", "吉林大学", "成都中医药大学", "北京化工大学", "浙江大学",
    # 规则噪声校
    "长安大学", "中国人民公安大学", "华中师范大学", "上海科技大学", "华南农业大学",
    # 特殊院校
    "外交学院", "中国音乐学院", "上海体育大学",
    # 普通中等规模
    "中国矿业大学（北京）", "东北林业大学", "上海交通大学", "山东大学",
    # 中医药类（验证低分学校是否真实）
    "上海中医药大学", "天津中医药大学",
]


def sample_rows(school: str, max_n: int = MAX_ROWS_PER_SCHOOL) -> list[dict]:
    f = OUTPUT_DIR / school / "departments.csv"
    if not f.exists():
        return []
    with open(f, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh)]
    if len(rows) <= max_n:
        return rows
    rng = random.Random(hash(school) & 0xFFFF)
    return rng.sample(rows, max_n)


SYSTEM = (
    "你是高校院系数据质量评审员。对给定的院系记录逐条判断，只输出 JSON，"
    "格式: {\"rows\": [{\"idx\": <原序号>, \"noise\": <true/false>, "
    "\"type_ok\": <true/false>, \"url_ok\": <true/false>, "
    "\"reason\": \"<中文理由，简洁>\"}]}\n"
    "判定标准:\n"
    "- noise=true: 该条目不是教学/科研院系本体，而是新闻、通知、行政机构（处/办公室/委员会/纪委/团委/工会/后勤）、"
    "网站导航项（首页/学校概况/组织机构/信息公开/图书馆/新闻网）、或汇总页占位（如\"学院（部）\"、\"科研机构\"整块链接）。\n"
    "- type_ok=false: 院系类型标签与名称不符（如行政办公室被标为学院、导航项被标为基地等）。\n"
    "- url_ok=false: URL 明显是文章页(含 /info/数字/数字、日期路径)、网站内导航页、非法URL、或指向与名称不符的站点。\n"
    "若 URL 为空字符串无法判断则 url_ok=false。注意: 校史条目(如\"大连工学院\")、附属医院、附属研究院、孔子学院不算噪声，属正常收录。"
)


def eval_school(client, school: str, rows: list[dict]):
    if client:
        client.begin_task()  # 任务边界: 每个学校(任务单元)开始前重新均衡分配 token
    if not rows:
        return {"school": school, "n_total": 0, "rows": [], "error": "no csv rows"}
    payload = [{"idx": i, "name": r.get("院系名称", "").strip(),
                "url": r.get("院系URL", "").strip(),
                "type": r.get("院系类型", "").strip()}
               for i, r in enumerate(rows)]
    user = f"学校: {school}\n记录列表:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    for attempt in range(3):
        try:
            text = client.ask(SYSTEM, user, temperature=0.0)
            text = text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            judged = data.get("rows", [])
            return {"school": school, "n_total": len(rows), "rows": judged, "raw_ok": True}
        except Exception as e:
            time.sleep(2)
            last_err = str(e)
    return {"school": school, "n_total": len(rows), "rows": [], "error": last_err}


def main():
    client = get_llm()
    if not client or not client.available:
        print("❌ LLM 不可用 (检查 college teacher/.env 的 LLM_API_KEY / LLM_BASE_URL)")
        return 1
    results = []
    for school in SAMPLE_SCHOOLS:
        rows = sample_rows(school)
        r = eval_school(client, school, rows)
        results.append(r)
        judged = r.get("rows", [])
        n_noise = sum(1 for j in judged if j.get("noise"))
        n_type_bad = sum(1 for j in judged if not j.get("type_ok"))
        n_url_bad = sum(1 for j in judged if not j.get("url_ok"))
        print(f"{school}: 抽样 {r['n_total']} 行 | 噪声 {n_noise} | 类型错 {n_type_bad} | URL错 {n_url_bad}"
              + (f" | ERR {r['error'][:60]}" if r.get("error") else ""))
    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n结果已写入: {RESULT_FILE}")


if __name__ == "__main__":
    sys.exit(main())
