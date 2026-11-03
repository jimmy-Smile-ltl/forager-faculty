#!/usr/bin/env python3
"""
采集学信网 (chsi.com.cn) 全国高校招生信息

数据源: https://gaokao.chsi.com.cn/gdwz.do
API: POST /wap/gdwz/sch (Playwright 浏览器自动化绕过瑞数反爬)

采集内容:
  学校名称, 学校网址, 招生网址, 咨询电话,
  微信(名称+号), 微博(名称+网址), 百家号, 新媒体(名称+网址)

输出:
  - JSONL: 原始 JSON 每行一条
  - CSV: 结构化字段

特性:
  - 断点续爬: checkpoint_chsi.json
  - Playwright headless
  - 147 页, 每页 20 条, 约 2921 条
"""

import csv
import json
import time
import random
from pathlib import Path
import asyncio

OUTPUT_DIR = Path(__file__).resolve().parent
JSONL_FILE = OUTPUT_DIR / "chsi_gdwz_raw.jsonl"
CSV_FILE = OUTPUT_DIR / "chsi_colleges.csv"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint_chsi.json"

BASE_URL = "https://gaokao.chsi.com.cn/gdwz.do"
TOTAL_PAGES = 147
PAGE_SIZE = 20

CSV_HEADER = [
    "orgCode", "学校名称", "学校网址", "招生网址", "咨询电话",
    "微信名称", "微信号",
    "微博名称", "微博网址",
    "百家号名称", "百家号ID",
    "新媒体名称", "新媒体网址",
]


def load_checkpoint() -> set[int]:
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return set(data.get("completed_pages", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_checkpoint(completed_pages: set[int]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps({
        "completed_pages": sorted(completed_pages),
        "total_pages": TOTAL_PAGES,
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2))


def extract_row(school: dict) -> list:
    """从 JSON 提取 CSV 行"""
    return [
        school.get("orgCode", ""),
        school.get("orgName", ""),
        school.get("xxwz", ""),
        school.get("zswz", ""),
        school.get("dh", ""),
        school.get("wxmc") or "",
        school.get("wxh") or "",
        school.get("wbmc") or "",
        school.get("wbwz") or "",
        school.get("bjhmc") or "",
        school.get("bjhIdh") or "",
        school.get("zyckxtmc") or "",
        school.get("zyckxtwz") or "",
    ]


async def main():
    from playwright.async_api import async_playwright

    print("=" * 60)
    print("🎓 学信网 - 全国高校招生信息采集")
    print(f"   数据源: {BASE_URL}")
    print(f"   输出 JSONL: {JSONL_FILE}")
    print(f"   输出 CSV:   {CSV_FILE}")
    print("=" * 60)

    completed = load_checkpoint()
    if completed:
        print(f"\n📋 断点恢复: 已完成 {len(completed)} 页")

    pending = sorted(set(range(1, TOTAL_PAGES + 1)) - completed)
    if not pending:
        print("\n✅ 全部页面已采集完毕!")
        return

    print(f"\n📋 待采集: {len(pending)} 页 (第 {pending[0]} - {pending[-1]} 页)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()

        # 导航到主页, 等待瑞数验证完成
        print("\n⏳ 加载页面 (瑞数验证中)...")
        await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        print("   ✅ 验证通过")
        await page.wait_for_timeout(2000)

        fail_pages = []
        success_count = 0

        for i, cur_page in enumerate(pending):
            start = (cur_page - 1) * PAGE_SIZE

            bar_len = 30
            filled = int(bar_len * (i + 1) / len(pending))
            bar = "█" * filled + "░" * (bar_len - filled)
            pct = (i + 1) / len(pending) * 100
            print(f"  [{bar}] {pct:5.1f}% | 第 {cur_page:3d}/{TOTAL_PAGES}", end="", flush=True)

            try:
                body = (
                    f"yxmc=&xlcc=&start={start}&curPage={cur_page}"
                    f"&pageSize={PAGE_SIZE}&totalCount=2921"
                )
                result = await page.evaluate(f"""
                    async () => {{
                        const resp = await fetch('/wap/gdwz/sch', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}},
                            body: '{body}',
                            credentials: 'include',
                        }});
                        return await resp.json();
                    }}
                """)

                schools = result.get("msg", {}).get("list", [])
                if schools:
                    # 写入 JSONL
                    with open(JSONL_FILE, "a", encoding="utf-8") as f:
                        for s in schools:
                            f.write(json.dumps(s, ensure_ascii=False) + "\n")

                    # 写入 CSV
                    file_exists = CSV_FILE.exists() and CSV_FILE.stat().st_size > 0
                    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
                        writer = csv.writer(f)
                        if not file_exists:
                            writer.writerow(CSV_HEADER)
                        for s in schools:
                            writer.writerow(extract_row(s))

                    completed.add(cur_page)
                    save_checkpoint(completed)
                    success_count += len(schools)
                    print(f" → ✅ {len(schools)} 条")
                else:
                    fail_pages.append(cur_page)
                    print(f" → ❌ 无数据 (flag={result.get('flag')})")

            except Exception as e:
                fail_pages.append(cur_page)
                print(f" → ❌ {str(e)[:80]}")

            if i < len(pending) - 1:
                await page.wait_for_timeout(random.randint(200, 500))

        await browser.close()

    # 最终统计
    total = 0
    if JSONL_FILE.exists():
        with open(JSONL_FILE, encoding="utf-8") as f:
            total = sum(1 for line in f if line.strip())

    print("\n" + "=" * 60)
    print(f"📊 采集完成!")
    print(f"   累计完成: {len(completed)} 页")
    print(f"   JSONL 记录: {total} 条")
    print(f"   CSV 记录: {total} 条")

    if fail_pages:
        print(f"\n⚠️ 失败页码 ({len(fail_pages)} 页): {fail_pages}")
        print("   重新运行将自动跳过已完成页")
    else:
        print("   ✅ 全部页面采集成功!")


if __name__ == "__main__":
    asyncio.run(main())
