#!/usr/bin/env python3
"""
采集掌上高考 (gaokao.cn) 全国高校信息

数据源:
  - 列表 API: https://api.eol.cn/gkcx/api/ (uri=apidata/api/gk/school/lists)
  - 详情 JSON: https://static-data.gaokao.cn/www/2.0/school/{id}/info.json
  - 校徽: https://static-data.gaokao.cn/upload/logo/{id}.png

采集内容:
  校徽URL, 学校名称, 所在地, 院校特色, 办学层次, 院校类别, 办学性质, 保研星级,
  软科排名, 校友会排名, QS排名, QS世界排名, US News排名, 武书连排名, 学科评估,
  详情页URL

特性:
  - 异步并发获取详情, 40 线程加速
  - 断点续爬: checkpoint.json 记录已完成 school_id
  - CSV append 模式, 逐条写入不丢失
  - 速率控制 + 重试机制
"""

import csv
import json
import os
import sys
import time
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi_requests

# ====== 配置 ======
OUTPUT_DIR = Path(__file__).resolve().parent
CSV_FILE = OUTPUT_DIR / "gaokao_colleges.csv"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint_gaokao.json"

LIST_API = "https://api.eol.cn/gkcx/api/"
INFO_URL_TMPL = "https://static-data.gaokao.cn/www/2.0/school/{school_id}/info.json"
LOGO_URL_TMPL = "https://static-data.gaokao.cn/upload/logo/{school_id}.png"
DETAIL_PAGE_TMPL = "https://www.gaokao.cn/school/{school_id}"

FAKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.gaokao.cn/",
}

CSV_HEADER = [
    "校徽URL", "学校名称", "所在地", "院校特色", "办学层次",
    "院校类别", "办学性质", "保研星级",
    "软科排名", "校友会排名", "QS排名", "QS世界排名",
    "US News排名", "武书连排名", "学科评估",
    "详情页URL",
]

# 排名相关 CSV 列 (build_rankings 返回的键 → CSV 列名)
RANK_FIELDS = {
    "ruanke_rank": "软科排名",
    "xyh_rank": "校友会排名",
    "qs_rank": "QS排名",
    "qs_world": "QS世界排名",
    "us_rank": "US News排名",
    "wsl_rank": "武书连排名",
    "xueke_rank": "学科评估",
}

# 并发线程数
MAX_WORKERS = 40

# ====== 编码映射 ======

# 办学层次
LEVEL_MAP = {
    "2001": "本科",
    "2002": "专科",
}

# 院校类别
TYPE_MAP = {
    "5000": "综合类", "5001": "理工类", "5002": "农林类",
    "5003": "医药类", "5004": "师范类", "5005": "语言类",
    "5006": "财经类", "5007": "政法类", "5008": "体育类",
    "5009": "艺术类", "5010": "民族类", "5011": "军事类",
    "5012": "其他",
}

# 办学性质
NATURE_MAP = {
    "36000": "公办",
    "36001": "民办",
    "36002": "中外合作办学",
    "36003": "内地与港澳台地区合作办学",
}

# 双高计划编码
DOUBLEHIGH_MAP = {
    "77004": "双高计划-高水平学校(A档)",
    "77005": "双高计划-高水平学校(B档)",
    "77006": "双高计划-高水平学校(C档)",
    "77007": "双高计划-高水平专业群(A档)",
    "77008": "双高计划-高水平专业群(B档)",
    "77009": "双高计划-高水平专业群(C档)",
    "77010": "双高计划-省级高水平学校",
    "77011": "双高计划-省级高水平专业群",
}


def load_checkpoint() -> set[int]:
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return set(data.get("completed_ids", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_checkpoint(completed_ids: set[int]):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "completed_ids": sorted(completed_ids),
        "total_completed": len(completed_ids),
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def init_csv():
    if not CSV_FILE.exists():
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)


def fetch_list_page(page: int, retries: int = 3) -> dict | None:
    """获取一页院校列表"""
    params = {
        "access_token": "",
        "keyword": "",
        "page": str(page),
        "province_id": "",
        "request_type": "1",
        "school_type": "",
        "signsafe": "",
        "size": "30",
        "sort": "view_total",
        "type": "",
        "uri": "apidata/api/gk/school/lists",
    }
    for attempt in range(retries):
        try:
            resp = cffi_requests.get(
                LIST_API, params=params, headers=FAKE_HEADERS,
                impersonate="chrome131", timeout=30,
            )
            data = resp.json()
            if data.get("code") == "0000":
                return data
            print(f"  ⚠️ API 返回异常 code={data.get('code')}, msg={data.get('message')}")
        except Exception as e:
            print(f"  ⚠️ 第 {page} 页请求异常: {e}, 重试 {attempt+1}/{retries}")
            time.sleep(random.uniform(1, 3))
    return None


def fetch_all_school_list() -> list[dict]:
    """获取全量院校基本信息列表"""
    print("\n📋 第一步: 获取院校列表...")
    first = fetch_list_page(1)
    if not first:
        print("❌ 无法获取第一页数据")
        return []

    total = first["data"]["numFound"]
    total_pages = (total + 29) // 30
    print(f"   总计 {total} 所学校, {total_pages} 页")

    all_schools = []
    fail_pages = []

    for page in range(1, total_pages + 1):
        bar_len = 30
        filled = int(bar_len * page / total_pages)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"  [{bar}] {page:3d}/{total_pages} 页", end="", flush=True)

        data = fetch_list_page(page) if page > 1 else first
        if data:
            items = data["data"]["item"]
            all_schools.extend(items)
            print(f" → ✅ {len(items)} 条")
        else:
            fail_pages.append(page)
            print(f" → ❌ 失败")

        if page < total_pages:
            time.sleep(random.uniform(0.15, 0.35))

    print(f"\n   ✅ 列表采集完成: {len(all_schools)} 所学校")
    if fail_pages:
        print(f"   ⚠️ 失败页码: {fail_pages}")
    return all_schools


def build_tags_from_list(item: dict) -> list[str]:
    """从列表 API 返回的基础字段构建院校特色标签"""
    tags = []

    # 985
    if item.get("f985") == "1":
        tags.append("985")
    # 211
    if item.get("f211") == "1":
        tags.append("211")
    # 双一流
    if item.get("dual_class") in ("38000", "38001", "38002"):
        dn = item.get("dual_class_name", "")
        tags.append(dn if dn else "双一流")
    # 教育部直属
    if item.get("department") == "1":
        tags.append("教育部直属")
    # 中央部委 (central: 非2即有)
    if item.get("central") not in ("", "2", None):
        tags.append("中央部委")
    # 强基计划
    if item.get("admissions") == "1":
        tags.append("强基计划")
    # 双高计划
    dh = item.get("doublehigh", "0")
    if dh != "0" and dh in DOUBLEHIGH_MAP:
        tags.append("双高计划")

    return tags


def build_tags_from_info(info: dict) -> list[str]:
    """从 info.json 的 label_list 和特殊字段构建标签"""
    tags = []

    # label_list 包含: 985, 211, 双一流, 强基, C9, 101计划, 机械五虎, etc.
    label_list = info.get("label_list", [])
    for lb in label_list:
        name = lb.get("name", "")
        if name and name not in tags:
            tags.append(name)

    # 军校
    if info.get("is_military_academy") == "1":
        tags.append("军校")
    # 警校(公安)
    if info.get("is_police_public_academy") == "1":
        tags.append("警校")
    # 警校(司法)
    if info.get("is_police_judicial_academy") == "1":
        if "警校" not in tags:
            tags.append("警校")

    # attr_list 包含: 公办/民办, 类别, 教育部直属 等
    attr_list = info.get("attr_list", [])
    for attr in attr_list:
        if attr == "教育部直属" and "教育部直属" not in tags:
            tags.append(attr)

    return tags


def build_rankings(info: dict) -> dict:
    """从 info.json 提取各机构排名 (0/缺失 = 未上榜, 空串)"""
    rank = info.get("rank") or {}

    def clean(v):
        if v is None:
            return ""
        v = str(v).strip()
        return "" if v in ("0", "0.0") else v

    # 学科评估 (第四轮, 各等级学科数) 序列化为 JSON 文本
    xk = info.get("xueke_rank") or {}
    xueke = json.dumps(xk, ensure_ascii=False) if xk else ""

    return {
        "ruanke_rank": clean(info.get("ruanke_rank") or rank.get("ruanke_rank")),
        "xyh_rank": clean(info.get("xyh_rank") or rank.get("xyh_rank")),
        "qs_rank": clean(info.get("qs_rank")),
        "qs_world": clean(rank.get("qs_world")),
        "us_rank": clean(info.get("us_rank") or rank.get("us_rank")),
        "wsl_rank": clean(info.get("wsl_rank")),
        "xueke_rank": xueke,
    }


def fetch_school_info(school_id: int, retries: int = 3) -> dict | None:
    """获取单个院校的 info.json"""
    url = INFO_URL_TMPL.format(school_id=school_id)
    for attempt in range(retries):
        try:
            resp = cffi_requests.get(
                url, headers=FAKE_HEADERS,
                impersonate="chrome131", timeout=20,
            )
            data = resp.json()
            if data.get("code") == "0000":
                return data["data"]
        except Exception:
            time.sleep(random.uniform(0.5, 1.5))
    return None


def process_school(item: dict) -> dict | None:
    """处理单个学校: 从列表数据 + info.json 组装完整信息"""
    school_id = int(item["school_id"])
    name = item.get("name", "")

    # 基本信息从列表字段获取
    level_name = LEVEL_MAP.get(item.get("level", ""), item.get("level_name", ""))
    type_name = TYPE_MAP.get(item.get("type", ""), item.get("type_name", ""))
    nature_name = NATURE_MAP.get(item.get("nature", ""), item.get("nature_name", ""))
    province = item.get("province_name", "")
    city = item.get("city_name", "")
    location = f"{province}{city}" if province and city else (province or city or "")

    # 列表基础标签
    base_tags = build_tags_from_list(item)

    # 尝试获取 info.json 获取更详细的标签和保研星级
    info = fetch_school_info(school_id)

    info_tags = []
    baoyan_star = ""
    rankings = {k: "" for k in RANK_FIELDS}
    if info:
        info_tags = build_tags_from_info(info)
        # 保研星级
        master_level = info.get("recommend_master_level", 0)
        if isinstance(master_level, str):
            master_level = int(master_level) if master_level.isdigit() else 0
        if master_level and master_level > 0:
            baoyan_star = str(master_level)

        # 各机构排名
        rankings = build_rankings(info)

        # 从 info 补充 location
        if not location:
            ip = info.get("province_name", "")
            ic = info.get("city_name", "")
            location = f"{ip}{ic}" if ip and ic else (ip or ic or "")
        if not level_name:
            level_name = info.get("level_name", "")
        if not type_name:
            type_name = info.get("type_name", "")
        if not nature_name:
            nature_name = info.get("nature_name", "")

    # 合并标签 (info 标签优先, 去重)
    all_tags = list(dict.fromkeys(info_tags + base_tags))

    # 标签规范化: 去除同义重复
    normalized = []
    for tag in all_tags:
        if tag == "强基" and "强基计划" in all_tags:
            continue  # 保留"强基计划"即可
        if tag == "双高" and "双高计划" in all_tags:
            continue  # 保留"双高计划"即可
        normalized.append(tag)

    # 院校特色字段
    features = "、".join(normalized) if normalized else ""

    logo_url = LOGO_URL_TMPL.format(school_id=school_id)
    detail_url = DETAIL_PAGE_TMPL.format(school_id=school_id)

    return {
        "school_id": school_id,
        "logo_url": logo_url,
        "name": name,
        "location": location,
        "features": features,
        "level_name": level_name,
        "type_name": type_name,
        "nature_name": nature_name,
        "baoyan_star": baoyan_star,
        "rankings": rankings,
        "detail_url": detail_url,
    }


def append_to_csv(row: dict):
    """追加一行到 CSV"""
    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            row["logo_url"],
            row["name"],
            row["location"],
            row["features"],
            row["level_name"],
            row["type_name"],
            row["nature_name"],
            row["baoyan_star"],
            *[row["rankings"].get(k, "") for k in RANK_FIELDS],
            row["detail_url"],
        ])


def main():
    print("=" * 60)
    print("🎓 掌上高考 - 全国高校信息采集")
    print(f"   数据源: {LIST_API}")
    print(f"   输出文件: {CSV_FILE}")
    print("=" * 60)

    init_csv()
    completed_ids = load_checkpoint()

    # 第一步: 获取全量列表
    all_schools = fetch_all_school_list()
    if not all_schools:
        print("❌ 未获取到院校列表")
        return

    # 过滤已完成的
    pending = [s for s in all_schools if int(s["school_id"]) not in completed_ids]

    if not pending:
        print("\n✅ 全部院校已采集完毕!")
        return

    if completed_ids:
        print(f"\n📋 断点恢复: 已完成 {len(completed_ids)} 所, 待处理 {len(pending)} 所")
    else:
        print(f"\n📋 开始处理 {len(pending)} 所学校")

    # 第二步: 并发获取详情并写入
    print(f"\n📋 第二步: 并发获取院校详情 (线程数={MAX_WORKERS})...")
    print()

    total = len(pending)
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 按 school_id 提交任务
        future_map = {
            executor.submit(process_school, item): item
            for item in pending
        }

        for i, future in enumerate(as_completed(future_map)):
            item = future_map[future]
            sid = int(item["school_id"])
            name = item.get("name", "?")

            try:
                result = future.result()
                if result:
                    append_to_csv(result)
                    completed_ids.add(sid)
                    success_count += 1
                    # 每 50 条保存一次断点
                    if success_count % 50 == 0:
                        save_checkpoint(completed_ids)
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1

            # 进度
            done = i + 1
            pct = done / total * 100
            bar_len = 30
            filled = int(bar_len * done / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"  [{bar}] {pct:5.1f}% | {done}/{total} | {name} → {'✅' if result else '❌'}")

    # 最终保存断点
    save_checkpoint(completed_ids)

    # 最终统计
    print("\n" + "=" * 60)
    print(f"📊 采集完成!")
    print(f"   成功: {success_count} 所")
    print(f"   失败: {fail_count} 所")
    print(f"   累计完成: {len(completed_ids)} 所")
    print(f"   输出文件: {CSV_FILE}")
    print(f"   文件大小: {CSV_FILE.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
