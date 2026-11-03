#!/usr/bin/env python3
"""IP 封禁错峰复查 (2026-09-02 缺点#4 改进): 南师大类 IP 封禁不再永久 FAIL。

.fail_mark 院系若带 .ip_block_mark 账本(行1=最近封禁时间戳, 行2=累计封禁次数):
  距上次封禁 > IP_BLOCK_COOLDOWN(默认 6h) 且 累计 < IP_BLOCK_MAX_ATTEMPTS(默认 3)
  → 删除 .fail_mark(下轮排名批次自然重拾), 保留 .ip_block_mark 作账本;
未到冷却期 / 已达上限 → 保持 FAIL(封禁站点不乱轰, 上线前也不白跑)。

由 auto_resume.sh 每轮批次间调用; 也可手动:
  uv run python script/retry_ip_blocked.py            # dry-run 预览
  uv run python script/retry_ip_blocked.py --apply    # 实际删除标记
环境: IP_BLOCK_COOLDOWN(秒, 默认 21600=6h)  IP_BLOCK_MAX_ATTEMPTS(默认 3)
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT  # noqa: E402

COOLDOWN = float(os.environ.get("IP_BLOCK_COOLDOWN", "21600"))
MAX_ATTEMPTS = int(os.environ.get("IP_BLOCK_MAX_ATTEMPTS", "3"))


def _parse(mp: Path) -> tuple:
    """读账本: (最近封禁时间戳, 累计次数); 损坏/缺失 → (0, 1)。"""
    try:
        lines = mp.read_text(encoding="utf-8").strip().splitlines()
        ts = int(lines[0]) if lines else 0
        att = int(lines[1]) if len(lines) > 1 else 1
        return ts, att
    except Exception:
        return 0, 1


def sweep(apply: bool) -> tuple:
    """扫描全部 .ip_block_mark, 复活冷却已过且未达上限的 FAIL 院系。

    返回 (复活数, 保持数)。apply=False 为 dry-run 只打印不动文件。"""
    now, revived, kept = time.time(), 0, 0
    for mp in sorted(OUT.rglob(".ip_block_mark")):
        d = mp.parent
        ts, att = _parse(mp)
        age_h = (now - ts) / 3600
        fm = d / ".fail_mark"
        school = d.parent.name if d.parent.name else "?"
        # 2026-09-03 用户指令(西交整体搁置): .blocked_mark 院系不参与冷却复活 —
        # 集中处理前保持搁置, 否则每 6h 复活一次与搁置冲突(未来技术学院 11:15
        # 封禁 → 17:15 冷却到期复活, 恰撞上搁置操作)。
        if (d / ".blocked_mark").exists():
            kept += 1
            print(f"⛔ 保持搁置 {school}/{d.name} (.blocked_mark 用户指令, 集中处理前不复活)")
            continue
        if age_h * 3600 > COOLDOWN and att < MAX_ATTEMPTS and fm.exists():
            revived += 1
            print(f"🔄 复活 {school}/{d.name} (封禁 {age_h:.1f}h 前, 第 {att} 次, 冷却已过)")
            if apply:
                fm.unlink(missing_ok=True)
        else:
            kept += 1
            if not fm.exists():
                why = "已无.fail_mark(已复活过)"
            elif age_h * 3600 <= COOLDOWN:
                why = f"未到冷却期({age_h:.1f}h < {COOLDOWN / 3600:.1f}h)"
            else:
                why = f"已达上限({att}/{MAX_ATTEMPTS})"
            print(f"⏸  保持 FAIL {school}/{d.name} ({why}, 第 {att} 次)")
    return revived, kept


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="实际删除 .fail_mark (默认 dry-run 只预览)")
    args = ap.parse_args()
    r, k = sweep(args.apply)
    verb = "已落盘" if args.apply else "dry-run 未落盘"
    print(f"\n合计: 复活 {r} / 保持 {k} ({verb})")
