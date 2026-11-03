#!/usr/bin/env python3
"""
回填排名数据 —— 为已有 gaokao_colleges.csv 补充 info.json 中的排名信息

背景: 原始采集只提取了 院校特色/保研星级, info.json 中的排名数据被丢弃。
本脚本重新拉取每所学校的 info.json, 提取排名并写回 CSV (不重爬列表)。

用法:
  cd gaokao_cn && uv run python backfill_rankings.py

特性:
  - 40 线程并发拉取 info.json
  - 断点续跑: rank_backfill_checkpoint.json 记录已完成 school_id
  - 每 50 所落盘一次 CSV, 中断不丢进度
"""

import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scrape_gaokao import RANK_FIELDS, fetch_school_info, build_rankings

BASE = Path(__file__).resolve().parent
CSV_FILE = BASE / "gaokao_colleges.csv"
CHECKPOINT_FILE = BASE / "rank_backfill_checkpoint.json"
MAX_WORKERS = 40
SAVE_EVERY = 50

RANK_COLS = list(RANK_FIELDS.values())


def load_checkpoint() -> set[int]:
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return set(data.get("completed_ids", []))
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return set()


def save_checkpoint(completed: set[int]):
    CHECKPOINT_FILE.write_text(json.dumps({
        "completed_ids": sorted(completed),
        "total_completed": len(completed),
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2))


def school_id_from_url(url: str) -> int | None:
    m = re.search(r"/(\d+)/?$", url or "")
    return int(m.group(1)) if m else None


def write_csv(rows: list[dict], columns: list[str]):
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not CSV_FILE.exists():
        print(f"❌ 未找到 {CSV_FILE}")
        return

    # 读现有 CSV, 保留原始列顺序
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        orig_cols = list(rows[0].keys()) if rows else []

    new_cols = orig_cols + [c for c in RANK_COLS if c not in orig_cols]
    print(f"📄 现有 {len(rows)} 行, 列 {len(orig_cols)} → {len(new_cols)}")

    completed = load_checkpoint()
    pending = []
    for i, row in enumerate(rows):
        sid = school_id_from_url(row.get("详情页URL", ""))
        if sid is None:
            print(f"  ⚠️ 第 {i} 行无 school_id, 跳过: {row.get('学校名称', '?')}")
            continue
        if sid not in completed:
            pending.append((sid, i, row))

    if not pending:
        print("\n✅ 全部已完成!")
        write_csv(rows, new_cols)
        return

    print(f"📋 断点恢复: 已完成 {len(completed)}, 待处理 {len(pending)}")
    print(f"📋 并发拉取 info.json (线程数={MAX_WORKERS})...")

    success = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(fetch_school_info, sid): (sid, i, row)
            for sid, i, row in pending
        }

        for idx, future in enumerate(as_completed(future_map), 1):
            sid, i, row = future_map[future]
            info = future.result()
            if info:
                rankings = build_rankings(info)
                for k, col in RANK_FIELDS.items():
                    row[col] = rankings[k]
                completed.add(sid)
                success += 1
            else:
                fail += 1

            if idx % SAVE_EVERY == 0 or idx == len(future_map):
                write_csv(rows, new_cols)
                save_checkpoint(completed)

            pct = idx / len(future_map) * 100
            print(f"  [{pct:5.1f}%] {idx}/{len(future_map)} | "
                  f"{row.get('学校名称', '?')} → {'✅' if info else '❌'}")

    write_csv(rows, new_cols)
    save_checkpoint(completed)
    print("\n" + "=" * 60)
    print(f"📊 回填完成! 成功 {success}, 失败 {fail}, 累计 {len(completed)}")
    print(f"   输出: {CSV_FILE}")


if __name__ == "__main__":
    main()
