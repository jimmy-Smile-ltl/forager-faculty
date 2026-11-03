#!/usr/bin/env python3
"""第一步：下载阶段（2026-08-31, 不依赖 LLM, 可高并发）。

流程: 名单 QC（垃圾名/URL 缺陷过滤）→ 逐院系差量下载（本地已有 HTML/PDF
跳过不重复下载; HTML 下载后探测 PDF, 有 PDF 即下载, 带重试）→ 写
download_done.flag 完成标志。第二步 extract_stage.py 只看标志。

用法:
  uv run python download_stage.py --school 北京大学
  uv run python download_stage.py --school 北京大学 --dept 考古文博学院
  uv run python download_stage.py --all --teacher-workers 24 --rate-limit 0.08
  uv run python download_stage.py --all --qc-only        # 只跑名单 QC 报告
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_RAW = _COLLEGE_ROOT / "tearchers raw data"
sys.path.insert(0, str(_BASE))

from extract_all_details import list_schools, list_depts, load_teachers  # noqa: E402
from detail_scraper import DetailScraper, _is_junk_name  # noqa: E402
from roster_qc import qc_dept  # noqa: E402

FLAG_NAME = "download_done.flag"
_RANK_FILE = _COLLEGE_ROOT / "department teachers" / "script" / "school_rank.json"
_RANK_CSV = _COLLEGE_ROOT / "colleges department" / "985_211_双一流高校清单_全字段.csv"


def _school_rank_order() -> list[str]:
    """学校排名顺序（排名越高越优先）。

    主源: 985/211/双一流全字段 CSV 的「软科排名」（缺排名的按序号垫底）；
    兜底: school_rank.json（CSV 未收录的学校）；两者都按排名升序。
    """
    order: list[tuple[int, str]] = []
    try:
        import csv as _csv
        with open(_RANK_CSV, encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                name = (row.get("学校名称") or "").strip()
                if not name:
                    continue
                rk = row.get("软科排名") or ""
                try:
                    key = (int(rk) if rk.strip() else 10**9, name)
                except ValueError:
                    key = (10**9, name)
                order.append(key)
        order.sort()
    except Exception:
        pass
    names = [n for _, n in order]
    try:
        data = json.loads(_RANK_FILE.read_text(encoding="utf-8"))
        legacy = data if isinstance(data, list) else list(data.keys())
        names.extend(s for s in legacy if s not in names)
    except Exception:
        pass
    return names


def local_raw_exists(school: str, dept: str, name: str, url: str) -> bool:
    """本地是否已有该教师的 HTML 或 PDF（按 姓名_md5 文件名）。"""
    raw_dir = _RAW / school / dept
    if not raw_dir.is_dir():
        return False
    prefix = DetailScraper._make_filename(name, url, "html")[:-5]  # 去 .html
    return any(raw_dir.glob(f"{prefix}.*")) if prefix else False


def dept_download_state(school: str, dept: str) -> dict:
    """逐教师下载状态: 有本地文件 / 垃圾名 / 无URL / 缺失。"""
    have = missing = junk = no_url = 0
    missing_samples = []
    # load_teachers 需要带 json_path 的完整 dept_info
    dept_info = None
    for d in list_depts(school):
        if d["dept"] == dept:
            dept_info = d
            break
    if dept_info is None:
        # 上游 Step3 批次实时改动名单: 目标构建后该院系可能被改名/删除
        # → 跳过不崩溃（2026-08-31 北大段实测 KeyError 崩溃整轮）
        return {"have": 0, "missing": 0, "junk": 0, "no_url": 0,
                "missing_samples": [], "error": "院系已不在名单"}
    for t in load_teachers(dept_info):
        name = str(t.get("name") or "")
        url = str(t.get("detail_url") or "")
        if _is_junk_name(name):
            junk += 1
            continue
        if not url:
            no_url += 1
            continue
        # 2026-09-03: 非 http URL（detail_url=教师姓名等上游 Step3 错位, 名单
        # 全库 8286 行/166 院系）归入 no_url —— 不可抓取。若计入 missing,
        # 100% 坏 URL 院系（陕师大数统 175/175）每次被 _judge_dead 误判死系,
        # 6h TTL 循环白耗。
        if not url.startswith(("http://", "https://")):
            no_url += 1
            continue
        if local_raw_exists(school, dept, name, url):
            have += 1
        else:
            missing += 1
            if len(missing_samples) < 5:
                missing_samples.append(name)
    return {"have": have, "missing": missing, "junk": junk,
            "no_url": no_url, "missing_samples": missing_samples}


def write_flag(school: str, dept: str, state: dict) -> Path:
    dept_dir = _BASE / school / dept
    dept_dir.mkdir(parents=True, exist_ok=True)
    p = dept_dir / FLAG_NAME
    p.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             **state}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


# ── 死系跳过（2026-09-03 用户指令: 大量失败直接换系, 记录后处理, 最大化代理利用）──
_DEAD_SKIP = ".dead_skip"
_DEAD_SKIP_TTL = 6 * 3600        # 标记 6h 后自动失效, 允许重试一次（防 IP/域名恢复）
_DEAD_LOG = _BASE / "logs" / "dead_dept_skip.tsv"


def _dead_mark_path(school: str, dept: str) -> Path:
    return _BASE / school / dept / _DEAD_SKIP


def _is_dead_skipped(school: str, dept: str) -> bool:
    """2026-10-26 用户指令: 取消死系标记跳过机制。

    旧机制: 大量失败 → 标记死系 → TTL 6h 内跳过——北师大教育学部实锤
    严重遗漏(上游名单更新 204 人但旧标记还在, 整系被跳过 8.5K 缺口)。
    新机制: 每轮动态比较名单 URL vs 本地文件, 缺什么下什么; 站点真死
    的院系靠 TCP 预检 3s 快速失败, 不再靠标记拦(白烧有限, 遗漏为零)。
    标记文件保留写入(诊断用), 但不再拦截下载。
    """
    return False


def _mark_dead(school: str, dept: str, attempt: int, success: int,
               missing: int, reason: str) -> Path:
    """大量失败 → 标记死系 + 记录 TSV 清单（供后处理集中解决）。"""
    dept_dir = _BASE / school / dept
    dept_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {"at": now, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "attempt": attempt, "success": success, "missing": missing,
               "reason": reason}
    p = dept_dir / _DEAD_SKIP
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    try:
        with open(_DEAD_LOG, "a", encoding="utf-8") as f:
            f.write(f"{payload['ts']}\t{school}\t{dept}\t{attempt}\t{success}"
                    f"\t{missing}\t{reason}\n")
    except Exception:
        pass
    return p


def _judge_dead(attempt: int, success: int, have_before: int = 0) -> bool:
    """大量失败判定: 整系零成功(≥3人) 或 大面积失败(≥8人成功<30%) → 死系。

    2026-09-03 修正: 该系已完成过半(have 占比≥50%)时只补零星缺漏,
    少数失败不判死（防清华国家卓越工程师学院 55/58 已有时 3 人失败误判）。
    """
    total = have_before + attempt
    if total >= 8 and have_before >= total * 0.5:
        return False
    if attempt >= 3 and success == 0:
        return True
    if attempt >= 8 and success < attempt * 0.3:
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="第一步: 名单QC+下载+完成标志")
    ap.add_argument("--school")
    ap.add_argument("--dept")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rank-order", action="store_true",
                    help="按 985/211/双一流 CSV 软科排名排序（排名越高越优先, 稳定排序保持校内院系原序）")
    ap.add_argument("--priority-schools", nargs="*", default=[],
                    help="排在排名表之前的学校（如 清华大学 北京大学）")
    ap.add_argument("--qc-only", action="store_true", help="只跑名单 QC, 不下载")
    ap.add_argument("--teacher-workers", type=int, default=24)
    ap.add_argument("--rate-limit", type=float, default=0.15,
                    help="教师间请求间隔秒（0.15≈6.7rps, 快代理 10rps 限额内留余量）")
    ap.add_argument("--dept-budget-min", type=int, default=5,
                    help="单系下载墙钟预算分钟（默认5; 卡死时更快跳出, 失败系下轮续补）")
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []
    if args.school and args.dept:
        targets = [(args.school, args.dept)]
    elif args.school:
        targets = [(args.school, d["dept"]) for d in list_depts(args.school)]
    elif args.all:
        for school in list_schools():
            targets.extend((school, d["dept"]) for d in list_depts(school))
        # 排序: --priority-schools 最前 → 排名表 → 其余照原序。
        # sort 稳定: 同校内院系保持名单原序。
        order: list[str] = list(args.priority_schools)
        if args.rank_order:
            order.extend(s for s in _school_rank_order() if s not in order)
        if order:
            idx = {s: i for i, s in enumerate(order)}
            targets.sort(key=lambda t: idx.get(t[0], len(order)))

        # ── 缺口优先排序（2026-09-03: 用户要求"保证每分钟都有下载, 大量失败换学校"）──
        # 全量模式: 预扫描各系缺口, 缺口大的未采新校排最前（真下载而非复用/秒过）;
        # 已采≥80% 且缺口≤5 的系视为"已采完, 缺口后处理"→ 本轮搁置跳过
        # （清华瑞数重灾已采90%+, 不再每轮为最后几个被拦的 URL 白耗 5 分钟预算）。
        _states: dict[tuple[str, str], dict] = {}
        if args.all and not args.school:
            for school, dept in targets:
                try:
                    s = dept_download_state(school, dept)
                    if not s.get("error"):
                        _states[(school, dept)] = s
                except Exception:
                    pass  # 名单变动等, 交给循环内兜底
            _skip: list[tuple[str, str]] = []
            _active: list[tuple[str, str]] = []
            for (school, dept), s in _states.items():
                total = s["have"] + s["missing"]
                if total > 0 and s["have"] >= total * 0.8 and s["missing"] <= 5:
                    _skip.append((school, dept))
                else:
                    _active.append((school, dept))
            _active.sort(key=lambda t: -_states[t]["missing"])  # 缺口大→未采新校优先
            targets = _active
            print(f"📌 缺口优先: 搁置已采完系 {len(_skip)} | 本轮下载 {len(_active)} 系")
            for school, dept in _skip[:8]:
                print(f"   ⏸️ 搁置: {school}/{dept}")
    else:
        ap.print_help()
        sys.exit(1)

    # ── 名单 QC（下载前的清洗过滤依据）──
    qc_tot = {"junk": 0, "bad_url": 0, "list_url": 0}
    for school, dept in targets:
        try:
            r = qc_dept(school, dept)
        except Exception as e:  # 畸形名单不可让整轮挂掉（后台循环必须自愈）
            print(f"  ⚠️ QC 异常跳过: {school}/{dept}: {e}")
            continue
        if "error" in r:
            continue
        qc_tot["junk"] += len(r.get("junk_names", []))
        qc_tot["bad_url"] += len(r.get("bad_urls", []))
        qc_tot["list_url"] += len(r.get("list_urls", []))
    print(f"📊 名单 QC: 垃圾名 {qc_tot['junk']} | URL缺陷 {qc_tot['bad_url']} "
          f"| 列表页URL {qc_tot['list_url']}（详见 logs/roster_qc_report.tsv）")
    if args.qc_only:
        sys.exit(0)

    # ── 逐院系下载（本地已有跳过, 差量为零即写标志）──
    log = _BASE / "logs" / f"download_stage_{int(time.time())}.log"
    processed = 0
    for i, (school, dept) in enumerate(targets):
        try:
            if _is_dead_skipped(school, dept):
                print(f"  ⏭️ 死系跳过: {school}/{dept}"
                      f"（TTL 内, 见 logs/dead_dept_skip.tsv）")
                continue
            state = _states.get((school, dept)) or dept_download_state(school, dept)
            if state.get("error"):
                print(f"  ⏭️ 跳过: {school}/{dept}: {state['error']}")
                continue
            if state["missing"] == 0:
                write_flag(school, dept, state)
                continue
            before_have = state["have"]
            before_missing = state["missing"]
            print(f"[{i + 1}/{len(targets)}] {school}/{dept}: 缺 {state['missing']} "
                  f"人（已有 {state['have']}, 垃圾名 {state['junk']}, "
                  f"无URL {state['no_url']}）")
            cmd = [sys.executable, str(_BASE / "extract_all_details.py"),
                   "--school", school, "--dept", dept, "--fetch-only",
                   "--teacher-workers", str(args.teacher_workers),
                   "--rate-limit", str(args.rate_limit)]
            # 预算按缺口动态放大: 大缺口系一次尽量下完(~100人/分钟), 避免每轮重复扫
            budget_min = args.dept_budget_min
            if state["missing"] > 500:
                budget_min = max(budget_min, min(30, state["missing"] // 80))
            budget_timeout = False
            try:
                subprocess.run(cmd, timeout=budget_min * 60, check=False)
            except subprocess.TimeoutExpired:
                budget_timeout = True
                print(f"  ⏰ 预算超时({budget_min}min): {school}/{dept}")
            # 下载后再评估状态: 只有全部落盘才写完成标志
            state = dept_download_state(school, dept)
            if state["missing"] == 0:
                write_flag(school, dept, state)
                print(f"  → 落盘后: 有 {state['have']}, 已写完成标志")
            else:
                success = state["have"] - before_have
                attempt = before_missing
                if budget_timeout:
                    # 预算超时: 落盘≥20=有效下载(大缺口未处理完) → 续补;
                    # 落盘<20=低产系(站点限速/慢响应/大量失败) → 判死搁置, 6h后重试
                    # （2026-09-03: 物理科学学院 10min仅6人, people.ucas.ac.cn限速, 每轮白耗10min教训）
                    if success >= 20:
                        print(f"  → 超时续补: 有 {state['have']}, 仍缺 {state['missing']}"
                              f"（下轮续补）")
                    else:
                        _mark_dead(school, dept, attempt, success,
                                   state["missing"],
                                   f"低产(超时{budget_min}min仅成功{success}, 疑限速/慢响应)")
                        print(f"  ⛔ 低产系→标记死系: {school}/{dept}"
                              f"（超时仅成功{success}, 6h后重试）")
                elif _judge_dead(attempt, success, before_have):
                    reason = f"大量失败(尝试{attempt}/成功{success})"
                    _mark_dead(school, dept, attempt, success,
                               state["missing"], reason)
                    print(f"  ⛔ 大量失败→标记死系: {school}/{dept} ({reason}), "
                          f"已记录 logs/dead_dept_skip.tsv, 后处理")
                else:
                    print(f"  → 落盘后: 有 {state['have']}, 仍缺 {state['missing']}"
                          f"（未写标志, 下轮续补）")
            processed += 1
        except Exception as e:
            # 后台循环必须自愈: 单系异常不得杀整轮
            # （2026-08-31 北大段名单实时变动致 KeyError 崩溃整轮教训）
            print(f"  ⚠️ 处理异常跳过: {school}/{dept}: {type(e).__name__}: {e}")

    print(f"\n✅ 下载阶段完成: 处理 {processed} 系\n日志: {log}")


if __name__ == "__main__":
    main()
