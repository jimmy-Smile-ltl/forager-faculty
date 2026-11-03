#!/usr/bin/env python3
"""
采集全国普通高等学校名单
数据来源: https://hudong.moe.gov.cn/qggxmd/
API: GET /school/wcmdata/getDataIndex.jsp?listid=10000101&page={n}&keyword=
共 148 页, 2952 条数据
字段: 序号, 学校名称, 学校标识码, 主管部门, 所在地, 办学层次, 备注

特性:
- 断点续爬: 通过 checkpoint.json 记录已爬取的页码
- CSV 写入: append 模式 (a+), 每爬完一页即 flush
- WAF 绕过: 使用 curl_cffi 模拟 Chrome 浏览器指纹
- 速率控制: 每页间隔 0.5-1.5 秒随机延迟
"""

import csv
import json
import os
import re
import sys
import time
import random
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

# ====== 配置 ======
BASE_URL = "https://hudong.moe.gov.cn/school/wcmdata/getDataIndex.jsp"
LIST_ID = "10000101"
TOTAL_PAGES = 148
TOTAL_RECORDS = 2952

OUTPUT_DIR = Path(__file__).resolve().parent
CSV_FILE = OUTPUT_DIR / "全国普通高等学校名单.csv"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"

HEADERS = [
    "序号", "学校名称", "学校标识码", "主管部门", "所在地", "办学层次", "备注"
]

# 模拟浏览器请求头
FAKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://hudong.moe.gov.cn/qggxmd/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def load_checkpoint() -> set[int]:
    """加载已成功爬取并写入的页码"""
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return set(data.get("completed_pages", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_checkpoint(completed_pages: set[int]):
    """保存断点"""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "completed_pages": sorted(completed_pages),
        "csv_file": str(CSV_FILE),
        "total_pages": TOTAL_PAGES,
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def init_csv():
    """初始化 CSV 文件, 如果不存在则创建并写入表头"""
    if not CSV_FILE.exists():
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(HEADERS)
        print(f"📄 创建 CSV 文件: {CSV_FILE}")


def fetch_page(page: int, retries: int = 3) -> str | None:
    """
    使用 curl_cffi 请求单页数据，模拟 Chrome 131 浏览器指纹
    返回 HTML 字符串或 None
    """
    params = {"listid": LIST_ID, "page": str(page), "keyword": ""}

    for attempt in range(retries):
        try:
            resp = requests.get(
                BASE_URL,
                params=params,
                headers=FAKE_HEADERS,
                impersonate="chrome131",  # curl_cffi 浏览器指纹模拟
                timeout=30,
            )
            resp.encoding = "utf-8"
            if resp.status_code == 200 and "<tr" in resp.text:
                return resp.text
            if resp.status_code == 502:
                print(f"  ⚠️ 第 {page} 页 502 错误 (WAF 拦截), 等待后重试...")
                time.sleep(random.uniform(3, 6))
            else:
                print(f"  ⚠️ 第 {page} 页 HTTP {resp.status_code}, 重试 {attempt+1}/{retries}")
                time.sleep(random.uniform(1, 3))
        except Exception as e:
            print(f"  ⚠️ 第 {page} 页请求异常: {e}, 重试 {attempt+1}/{retries}")
            time.sleep(random.uniform(2, 5))

    print(f"  ❌ 第 {page} 页请求失败 (已重试 {retries} 次)")
    return None


def parse_rows(html: str) -> list[list[str]]:
    """从 HTML 中解析表格行，返回二维列表"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) == 7:
            rows.append(cells)
    return rows


def scrape_page(page: int) -> int:
    """
    爬取单页数据并追加写入 CSV
    返回写入的行数
    """
    html = fetch_page(page)
    if html is None:
        return 0

    rows = parse_rows(html)
    if not rows:
        print(f"  ⚠️ 第 {page} 页未解析到数据行")
        return 0

    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)

    return len(rows)


def main():
    print("=" * 60)
    print("🎓 全国普通高等学校名单采集")
    print(f"   数据来源: {BASE_URL}")
    print(f"   总页数: {TOTAL_PAGES} | 总记录: {TOTAL_RECORDS}")
    print(f"   输出文件: {CSV_FILE}")
    print("=" * 60)

    # 初始化
    init_csv()
    completed = load_checkpoint()

    if completed:
        print(f"\n📋 断点恢复: 已完成 {len(completed)} 页")
        print(f"   已完成页码: {sorted(completed)}")

    # 待爬取页码
    all_pages = set(range(1, TOTAL_PAGES + 1))
    pending = sorted(all_pages - completed)

    if not pending:
        print("\n✅ 全部页面已爬取完毕!")
        return

    print(f"\n📋 待爬取: {len(pending)} 页")
    print(f"   页码范围: {pending[0]} - {pending[-1]}\n")

    total_rows = 0
    fail_pages = []

    for i, page in enumerate(pending):
        # 进度显示
        progress = (i + 1) / len(pending) * 100
        bar_len = 30
        filled = int(bar_len * (i + 1) / len(pending))
        bar = "█" * filled + "░" * (bar_len - filled)

        print(f"  [{bar}] {progress:5.1f}% | 第 {page:3d}/{TOTAL_PAGES} 页", end="", flush=True)

        n = scrape_page(page)

        if n > 0:
            completed.add(page)
            total_rows += n
            print(f" → ✅ {n} 条")
        else:
            fail_pages.append(page)
            print(f" → ❌ 失败")

        # 每页成功后保存断点
        if n > 0:
            save_checkpoint(completed)

        # 速率控制: 随机延迟 0.5-2 秒
        if i < len(pending) - 1:
            delay = random.uniform(0.5, 1.5)
            time.sleep(delay)

    # 最终统计
    print("\n" + "=" * 60)
    print(f"📊 采集完成!")
    print(f"   本次新增: {total_rows} 条")
    print(f"   累计完成: {len(completed)} 页")
    print(f"   输出文件: {CSV_FILE}")

    if fail_pages:
        print(f"\n⚠️ 失败页码 ({len(fail_pages)} 页): {fail_pages}")
        print("   请重新运行脚本，断点将自动跳过已完成的页面")
    else:
        print("   ✅ 全部 148 页采集成功!")

    save_checkpoint(completed)


if __name__ == "__main__":
    main()
