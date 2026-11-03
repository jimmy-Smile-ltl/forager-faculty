#!/usr/bin/env python3
"""批量提取: 按清单逐院系跑 extract_all_details.py --local-only。

用法: uv run python scripts/extract_batch.py logs/_extract_depts.tsv [并发数]
"""
import csv
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_done = 0
_total = 0
_fails = []


def run_one(school: str, dept: str, tw: int = 1, reapply: bool = False) -> tuple[str, str, int]:
    """跑一个院系, 返回 (school, dept, returncode)。

    2026-10-25 分档并发: 大额院系(>400 缺口) teacher-workers=4 院内并发,
    否则单院系 2500 人串行 3.5h 占死 worker(国科大资源环境学院实锤);
    中小额 1 路足够。LLM_SEM=192 容量, 峰值 16 院系×混合≈40 路远低于限。
    reapply=True 时传 --reapply-rules(重跑已标记失败的记录——AES 壳解密
    接入等修复后重提), 否则 --resume 增量。
    """
    mode = ["--reapply-rules"] if reapply else ["--resume"]
    r = subprocess.run(
        [sys.executable, str(_BASE / "extract_all_details.py"),
         "--local-only", "--school", school, "--dept", dept,
         "--workers", "1", "--teacher-workers", str(tw)] + mode +
         # 2026-10-25 节流放开: 默认 1.0=每秒1请求/进程, local-only 复用
         # 本地 HTML 不发站点请求, 节流纯属误伤(实测 38s/人, 4 路院内
         # 仍被节流门卡到 25 人/分钟)——50/秒彻底放开
         ["--rate-limit", "50"],
        cwd=str(_BASE), capture_output=True, text=True, timeout=3600 * 6)
    return school, dept, r.returncode


def _interleave(targets: list[tuple]) -> list[tuple]:
    """按缺口数分三桶后轮转交错——避免全部 worker 卡在头部巨无霸院系
    (2026-09-24 南昌大学教训同款: 同校连续排队占满 worker 停摆)。"""
    big = [(s, d, g) for s, d, g in targets if g >= 400]
    mid = [(s, d, g) for s, d, g in targets if 80 <= g < 400]
    small = [(s, d, g) for s, d, g in targets if g < 80]
    out = []
    i = j = k = 0
    while i < len(big) or j < len(mid) or k < len(small):
        if i < len(big):
            out.append(big[i]); i += 1
        if j < len(mid):
            out.append(mid[j]); j += 1
        if k < len(small):
            out.append(small[k]); k += 1
    return out


def main():
    global _done, _total
    lst, conc = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 4
    reapply = len(sys.argv) > 3 and sys.argv[3] == "reapply"
    rows = []
    with open(lst, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            s = (row.get("学校") or "").strip()
            d = (row.get("院系") or "").strip()
            g = int(row.get("缺口") or row.get("html数") or row.get("新文件数") or 0)
            if s and d:
                rows.append((s, d, g))
    _total = len(rows)
    targets = _interleave(rows)
    print(f"🎬 批量提取 {_total} 院系 (并发 {conc}, 大额院内 4 路)", flush=True)
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(run_one, s, d, 4 if g >= 200 else (2 if g >= 50 else 1), reapply): (s, d)
                for s, d, g in targets}
        for fut in as_completed(futs):
            s, d, rc = fut.result()
            with _lock:
                _done += 1
                if rc != 0:
                    _fails.append((s, d, rc))
                print(f"  [{_done}/{_total}] {s}/{d} "
                      f"{'✓' if rc == 0 else '✗'}", flush=True)
    print(f"\n✅ 批量提取完成: {_done - len(_fails)}/{_total} 成功"
          f"{f', {len(_fails)} 失败' if _fails else ''}")
    for s, d, rc in _fails[:10]:
        print(f"  ✗ {s}/{d} rc={rc}")


if __name__ == "__main__":
    main()
