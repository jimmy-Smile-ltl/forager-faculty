#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oneapi SK 余额检查脚本

读取 SK 汇总表 xlsx（列: 名称, sk密钥），只对「两列都有数据且格式正确（sk- 开头）」的密钥
发送一次最小 chat 请求（token 成本可忽略），判断当月额度是否用完。

用法:
    uv run --with openpyxl python check_sk_balance.py [xlsx路径] [--model <你的模型>] [--workers 8]

输出:
    - 控制台: 明细表格 + 汇总统计
    - 生成 sk检查结果.csv (utf-8-sig，Excel 可直接打开，密钥已掩码)

状态说明:
    有余额   = 请求成功返回内容
    无余额   = 当月额度已用完 (insufficient_user_quota)
    密钥无效 = 401 / 认证失败
    请求失败 = 网络异常、超时（已自动重试1次）
    其他错误 = 非预期错误（含错误信息摘要）
"""

import argparse
import csv
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openpyxl

# 网关地址必须通过环境变量注入（LLM_BASE_URL 或 ONEPAI_BASE_URL），不设任何默认值。
# 探测模型同样跟随环境变量（TOKEN_PROBE_MODEL / LLM_MODEL，与 token_pool.py 联动），
# 不写死单一模型 —— 模型下线 ≠ key 失效，写死会把活 key 全误判成请求失败。
_OSR = __import__("os").environ
BASE_URL = (_OSR.get("ONEPAI_BASE_URL") or _OSR.get("LLM_BASE_URL") or "").rstrip("/")

# 独立运行 CLI 时也加载仓库根 .env（经 token_pool.py 调用时其自身已加载）
try:
    from pathlib import Path as _P
    from dotenv import load_dotenv as _ld
    _ld(_P(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

TIMEOUT = 20          # 单请求超时秒数
MAX_RETRY = 1         # 网络失败重试次数
DEFAULT_MODEL = (_OSR.get("TOKEN_PROBE_MODEL") or _OSR.get("LLM_MODEL")
                 or "deepseek-chat")

# 状态常量
S_OK = "有余额"
S_NO_QUOTA = "无余额"
S_INVALID_KEY = "密钥无效"
S_NET_FAIL = "请求失败"
S_OTHER_ERR = "其他错误"
S_SKIP_EMPTY = "跳过(缺数据)"
S_SKIP_FORMAT = "跳过(格式不正确)"


def mask_sk(sk: str) -> str:
    """密钥脱敏: 前8位 + ... + 后4位"""
    if len(sk) > 16:
        return f"{sk[:8]}...{sk[-4:]}"
    return "***"


def classify_error(status_code: int, body: str) -> tuple[str, str]:
    """把 oneapi 错误响应归类为 (状态, 详情)"""
    try:
        err = json.loads(body).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        err = {}
    msg = str(err.get("message", "") or body)[:120]
    code = str(err.get("code", ""))

    if "insufficient_user_quota" in code or "额度" in msg or "quota" in code.lower():
        return S_NO_QUOTA, msg
    if status_code == 401 or any(k in msg.lower() for k in ("无效", "认证", "invalid", "unauthor")):
        return S_INVALID_KEY, msg
    return S_OTHER_ERR, f"HTTP {status_code}: {msg}"


def check_key(sk: str, model: str) -> tuple[str, str, float]:
    """对单个密钥发送一次最小 chat 请求, 返回 (状态, 详情, 耗时ms)"""
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "你好"}]}
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {sk}",
    }
    url = f"{BASE_URL}/chat/completions"

    for attempt in range(MAX_RETRY + 1):
        start = time.time()
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read()  # 200 即视为有余额，不解析内容
            return S_OK, "", (time.time() - start) * 1000
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            status, detail = classify_error(e.code, body)
            return status, detail, (time.time() - start) * 1000
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            if attempt < MAX_RETRY:  # 网络失败重试一次
                time.sleep(2)
                continue
            return S_NET_FAIL, str(e)[:120], (time.time() - start) * 1000
    return S_NET_FAIL, "unreachable", 0.0  # 不会走到


def load_tasks(xlsx_path: str) -> list[dict]:
    """读取 xlsx，提取两列都有数据且格式正确的密钥"""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.worksheets[0]

    def cell(row, i):
        return str(row[i]).strip() if len(row) > i and row[i] else ""

    tasks, skipped = [], []
    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        if idx == 0:
            continue  # 表头
        name, sk = cell(row, 0), cell(row, 1)
        if not name or not sk:
            skipped.append((name or "(空)", mask_sk(sk) if sk else "", S_SKIP_EMPTY, ""))
        elif not sk.startswith("sk-"):
            skipped.append((name, mask_sk(sk), S_SKIP_FORMAT, ""))
        else:
            tasks.append({"name": name, "sk": sk})
    wb.close()
    return tasks, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="oneapi SK 余额检查")
    parser.add_argument("xlsx", nargs="?", default="sk-keys.xlsx",
                        help="xlsx 路径(默认: sk-keys.xlsx)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"测试用模型(默认: {DEFAULT_MODEL})")
    parser.add_argument("--workers", type=int, default=8, help="并发数(默认: 8)")
    parser.add_argument("--no-csv", action="store_true", help="不生成 CSV 报告")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"[错误] 找不到文件: {xlsx_path}")
        return 1

    tasks, skipped = load_tasks(str(xlsx_path))
    total = len(tasks) + len(skipped)
    print(f"读取 {xlsx_path.name}: 共 {total} 行数据, 有效密钥 {len(tasks)} 个, "
          f"跳过 {len(skipped)} 个 (两列缺数据或格式不正确)")
    if not tasks:
        print("没有可测试的密钥, 结束。")
        return 0

    # 并发测试
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {pool.submit(check_key, t["sk"], args.model): t for t in tasks}
        for fut in as_completed(future_map):
            t = future_map[fut]
            status, detail, cost_ms = fut.result()
            results.append({**t, "status": status, "detail": detail, "cost_ms": cost_ms})

    # 排序: 有余额 -> 无余额 -> 其他
    order = {S_OK: 0, S_NO_QUOTA: 1, S_INVALID_KEY: 2, S_OTHER_ERR: 3, S_NET_FAIL: 4}
    results.sort(key=lambda r: (order.get(r["status"], 9), r["name"]))

    # 控制台输出
    print("\n=== 检查明细 ===")
    print(f"{'姓名':<8} {'sk密钥(脱敏)':<28} {'状态':<16} {'耗时ms':>7}  详情")
    for r in results:
        detail = r["detail"][:60] if r["detail"] else ""
        print(f"{r['name']:<8} {mask_sk(r['sk']):<28} {r['status']:<16} {r['cost_ms']:>7.0f}  {detail}")
    # 纯空行折叠为一行统计，不逐行刷屏
    empty_cnt = sum(1 for name, _, status, _ in skipped if name == "(空)" and status == S_SKIP_EMPTY)
    for name, msk, status, _ in skipped:
        if name == "(空)" and status == S_SKIP_EMPTY:
            continue
        print(f"{name:<8} {msk:<28} {status:<16}")
    if empty_cnt:
        print(f"另有 {empty_cnt} 行两列均为空, 跳过")

    # 汇总
    from collections import Counter
    cnt = Counter(r["status"] for r in results)
    ok_names = ", ".join(r["name"] for r in results if r["status"] == S_OK)
    noq_names = ", ".join(r["name"] for r in results if r["status"] == S_NO_QUOTA)
    print("\n=== 汇总 ===")
    print(f"有效密钥总数: {len(results)}")
    print(f"有余额:       {cnt.get(S_OK, 0)}  ->  {ok_names or '无'}")
    print(f"无余额:       {cnt.get(S_NO_QUOTA, 0)}  ->  {noq_names or '无'}")
    print(f"密钥无效:     {cnt.get(S_INVALID_KEY, 0)}")
    print(f"请求失败:     {cnt.get(S_NET_FAIL, 0)}")
    print(f"其他错误:     {cnt.get(S_OTHER_ERR, 0)}")

    # CSV 报告
    if not args.no_csv:
        out = xlsx_path.parent / "sk检查结果.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["姓名", "sk密钥(脱敏)", "状态", "详情", "耗时ms"])
            for r in results:
                writer.writerow([r["name"], mask_sk(r["sk"]), r["status"], r["detail"], round(r["cost_ms"])])
            for name, msk, status, _ in skipped:
                if name == "(空)" and status == S_SKIP_EMPTY:
                    continue
                writer.writerow([name, msk, status, "", ""])
            if empty_cnt:
                writer.writerow([f"(另有 {empty_cnt} 行两列均为空, 已跳过)", "", "", "", ""])
        print(f"\n报告已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
