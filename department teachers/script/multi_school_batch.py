#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多学校并发修复驱动 — 跨学校选 N 个院系, 单进程单线程池(LLM 限速共享)

用法:
  uv run python script/multi_school_batch.py 哈尔滨工业大学,兰州大学 --per-school 8 --workers 10
  uv run python script/multi_school_batch.py 南京师范大学 --per-school 10 --workers 8

选取策略: 每校优先失败队列, 再补无 .fix_done 的新院系(按名排序), 每校上限 --per-school。
"""
import argparse, csv, json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime

# ── 僵尸深冻冷却账本 (2026-09-15 用户指令「重试优先级最低, 全部跑一遍之后再重试」) ──
# 历史缺陷: 僵尸轮每圈把全部僵尸重试一遍, 硬门槛僵尸(attempts=71 级)每 1.5h
# 白烧一轮 → 无限重复。本账本: 僵尸连续 FREEZE_MAX_ROUNDS 轮失败即「深冻」,
# FREEZE_COOLDOWN_HOURS 内不再被僵尸轮选中; 冷却到期解冻再试一轮。
# 优先级: ranked fresh → 非僵尸队列 → 僵尸轮(未深冻) → 深冻僵尸(冷却后) 天然分层。
FREEZE_LEDGER = Path("logs/_zombie_freeze.json")
FREEZE_MAX_ROUNDS = 2
FREEZE_COOLDOWN_HOURS = 24


def _freeze_ledger_load():
    if not FREEZE_LEDGER.exists():
        return {}
    try:
        return json.load(open(FREEZE_LEDGER, encoding="utf-8"))
    except Exception:
        return {}


def _freeze_ledger_save(led):
    import fcntl
    lk = open(str(FREEZE_LEDGER) + ".lock", "w")
    fcntl.flock(lk, fcntl.LOCK_EX)
    json.dump(led, open(FREEZE_LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    fcntl.flock(lk, fcntl.LOCK_UN)


def _zombie_frozen(school, dept, led):
    rec = (led.get(school, {}) or {}).get(dept)
    if not rec:
        return False
    if rec.get("rounds", 0) < FREEZE_MAX_ROUNDS:
        return False
    # 2026-09-18 十批攻坚改进#5: 形态分级 TTL — 校站级/瑞数墙/JS渲染族 7 天
    # (校方修网络没那么快), 瞬时网络类 6h(快速复活多产出), 默认 24h
    ttl_h = FREEZE_COOLDOWN_HOURS
    _f = rec.get("form", "")
    if _f in ("subnet_dead", "ruishu_412", "js_spa", "js_menu"):
        ttl_h = 7 * 24
    elif _f == "net_recovered":
        ttl_h = 6
    try:
        last = datetime.strptime(rec.get("last", ""), "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - last).total_seconds() > ttl_h * 3600:
            return False  # 冷却到期, 解冻再试一轮
    except Exception:
        return False
    return True

# 单院系硬超时: fix_one 正常 1-6 分钟(含3轮×spider+评估+LLM)。超过即视为
# 僵尸(DrissionPage tab 挂起/网络栈卡死), 记为 FAIL 重新入队, 不让整批陪葬。
# 历史: 批次9全池挂起, as_completed 永不结束, 16 worker 全 idle。
# 2026-08-23: 30 workers + LLM_SEM=6 网关过载引发重试风暴, 单院系 >25min 被误杀 →
# 放宽到 2400s(40min) 给 LLM 排队留余量; LLM_SEM 回落到 4(网关稳定容量)。
# 2026-08-23 二轮: 失败队列难点院系(probe 600s + 3轮×[spider 120 + LLM 120 +
# 评估 90×多批])累计耗时 >40min 仍活跃(日志可见持续产出), 2400s 误杀 →
# 入队重跑再超时 → 无限循环。放宽到 5400s(90min) 匹配真实耗时上限。
PER_DEPT_TIMEOUT = 1200  # 2026-09-03 用户指令"完成一个学院时间暂定15分钟": 5400(90min)→1800(30min)→900(15min); 配合 logs/_dept_timing.log 实测各步骤耗时后再调
# 2026-09-08 用户指令「瑞数站慢, 没有办法, 无代理」: 900→1200(20min) — 慢瑞数院系
# R1 spider 480s+评估+改进在 900s 内被僵尸预算截杀(截杀≠失败, 产物白丢);
# 1200s 让慢站跑完整轮, 卡死仍由预算兜底防 worker 永久占用。
# 正常院系 3 轮评估 ~10-15 分钟, 30 分钟留 2 倍裕量; 挂死院系 30 分钟即僵尸换下一个,
# 不再占槽 90 分钟阻塞产出。网关波动期正常院系可能 20-30 分钟(波峰误杀风险已知,
# 僵尸不丢数据只入 zombie 队列等最终消化轮)。原 5400s(2026-08-27)/7200s(120min)。
# 占槽太久(25 僵尸=25 worker 全被占, 批次卡死等僵尸自然结束)。缩到 5400s 匹配
# 慢窗+LLM 排队下活跃院系真实耗时上限, 超过即判定僵尸放弃等它, 记 FAIL 入队
# (带 zombie 标记, 补位跳过), 队列项后续由 --retry-zombies 专项集中重试。
ZOMBIE_EXIT_LIMIT = 5  # 僵尸风暴阈值: 僵尸数 ≥ 5 立即终结批次, 重启换新任务

# ── 排名模式 (2026-08-28 用户指令): 按大学排名逐校累加未完成院系构成批次 ──
# 先把排名最高学校(清华)的全部未完成院系选入, 不足批次上限 --cap 则并入排名
# 更低的学校, 直到凑满一批。状态存 logs/_ranked_state.json:
#   {"sweep": 轮次, "attempted": {校名: [院系名]}, "updated": 时间戳}
# 同一轮 sweep 内每个院系只尝试一次(按名排序的稳定顺序), FAIL 回队列后留到
# 下一轮 sweep 按排名重试 — 保证"先清华后北大"的排名推进不被失败院系阻塞。


def _school_urls(sdir):
    """院系URL表: 学校级 departments.csv + info.csv 兜底 (与 main 同逻辑)。"""
    dcsv = sdir / "departments.csv"
    urls = {}
    if dcsv.exists():
        urls = {r["院系名称"]: r.get("院系URL", "").strip()
                for r in csv.DictReader(open(dcsv, encoding="utf-8"))}
    for _d in sorted(sdir.iterdir()):
        if not _d.is_dir() or _d.name.startswith((".", "_")):
            continue
        if urls.get(_d.name, "").startswith("http"):
            continue
        _ic = _d / "info.csv"
        if _ic.exists():
            for _row in csv.DictReader(open(_ic, encoding="utf-8")):
                _u = (_row.get("院系URL") or "").strip()
                if _u.startswith("http"):
                    urls[_d.name] = _u
                    break
    return urls


def load_rank_state():
    try:
        return json.load(open(RANK_STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"sweep": 1, "attempted": {}, "updated": ""}


def save_rank_state(st):
    try:
        RANK_STATE_FILE.write_text(
            json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def build_ranked_tasks(cap, exclude_attempted=True):
    """按排名累加未完成院系到 cap。2026-09-03 用户指令「先易后难, fresh 学校学院
    先采集」: 第一遍只选 fresh(从未做过的院系), 全部 fresh 选完仍不满 cap 时才
    第二遍按最老(at)全球补非僵尸队列项 — 久拖不决的队列不再抢占 fresh 名额。
    2026-08-30 用户指令"一个学校一个学校处理, 清北优先": 排名前
    TOP_SCHOOLS_NO_ATTEMPTED(2=清华北大)的学校不排除 attempted —
    难任务每批次持续重试直至完成/zombie, 其余学校维持 sweep 一次防卡死。
    返回 (tasks, sweep, covered, avail_any):
      tasks: [(school, dept_dir, url)]; covered: {school: 数量};
      avail_any: 是否存在本轮可选院系(用于判定完成/进入新 sweep)。"""
    rank = json.load(open(RANK_FILE, encoding="utf-8"))
    st = load_rank_state()
    sweep = st.get("sweep", 1)
    attempted = {k: set(v) for k, v in st.get("attempted", {}).items()}
    tasks, covered, avail_any = [], {}, False

    def _pend_allowed(school, name):
        """attempted 过滤: 清北(排名前2)不排除, 其余学校同轮每院系只试一次。"""
        return (not exclude_attempted
                or rank_i < TOP_SCHOOLS_NO_ATTEMPTED
                or name not in attempted.get(school, set()))

    # ── 第一遍: fresh 优先(队列完全不参与 — 先易后难) ──
    for rank_i, school in enumerate(rank):
        sdir = fl.OUT / school
        if not sdir.is_dir():
            continue
        urls = _school_urls(sdir)
        cands = [d for d in sorted(sdir.iterdir())
                 if d.is_dir() and not d.name.startswith((".", "_"))
                 and urls.get(d.name, "").startswith("http")]
        q_names = set(fl.load_fail_queue().get(school, {}).keys())
        fresh = [d for d in cands if not (d / ".fix_done").exists()
                 and not (d / ".skip_mark").exists() and not (d / ".rescan_done").exists()
                 and not (d / ".zombie_mark").exists()
                 and not (d / ".fail_mark").exists()
                 and not (d / ".blocked_mark").exists()  # 2026-09-03 用户指令: IP封锁/复杂反爬整体搁置, 后期集中处理
                 and not _attempted_recently(d)
                 and d.name not in q_names]
        pend = [d for d in fresh if _pend_allowed(school, d.name)]
        if pend:
            avail_any = True
        for d in pend:
            tasks.append((school, d, urls.get(d.name, "")))
            covered[school] = covered.get(school, 0) + 1
            if len(tasks) >= cap:
                return tasks, sweep, covered, avail_any

    # ── 第二/三/四遍: 动态任务队列(2026-09-20 用户指令) ──
    # 用户方案「维护完整任务队列动态管理, 深冻排队最后, 永不空转」:
    #   第二遍: 队列非僵尸、本sweep未试过 — at最新优先(先易后难)
    #   第三遍: 队列非僵尸、本sweep已试过 — 同轮二次机会补满批(防批小浪费)
    #   第四遍: 深冻(zombie/attempts超软上限) — 队尾消化, 无视冷却TTL
    #     (用户: 时间卡死不是最好方式; 全部正常任务清空后空闲时照样跑深冻,
    #      否则冷却期无活可干=空转)
    # 每层内 slow 标记(慢站降权)统一挪最后。
    qpool = []       # 第二遍: (at, school, d, url, slow)
    qpool_retry = []  # 第三遍: 同轮已试过
    zpool = []       # 第四遍: (at, school, d, url, slow) 深冻
    for rank_i, school in enumerate(rank):
        sdir = fl.OUT / school
        if not sdir.is_dir():
            continue
        urls = _school_urls(sdir)
        for n, it in fl.load_fail_queue().get(school, {}).items():
            if not isinstance(it, dict):
                continue
            _zom = bool(it.get("zombie")) or it.get("attempts", 0) > fq.TRANSIENT_MAX_ATTEMPTS
            d = sdir / n
            # 2026-09-04 南科大专项: 终态标记全查防 RETRY 磨坊复活
            if not d.is_dir() or (d / ".fix_done").exists() or (d / ".zombie_mark").exists() \
                    or (d / ".skip_mark").exists() or (d / ".fail_mark").exists():
                continue
            if (d / ".blocked_mark").exists():  # 2026-09-03 IP封锁搁置政策
                continue
            _e = (it.get("at", "9999"), school, d, urls.get(n, ""), bool(it.get("slow")))
            if _zom:
                zpool.append(_e)
            elif _pend_allowed(school, n):
                qpool.append(_e)
            else:
                qpool_retry.append(_e)
    # 排序: 第二/三遍 at 最新优先; 第四遍 at 最老优先(冻得久的先出狱);
    # slow 各层稳定挪最后
    for _pl in (qpool, qpool_retry):
        _pl.sort(key=lambda t: t[0], reverse=True)
        _pl.sort(key=lambda t: t[4])
    zpool.sort(key=lambda t: t[0])
    zpool.sort(key=lambda t: t[4])
    if qpool:
        print(f"📦 fresh 已尽 → 动态队列: 二遍{qpool.__len__()} + 三遍(同轮二次){qpool_retry.__len__()} + 深冻队尾{zpool.__len__()}, 填至 cap")
    for _pl in (qpool, qpool_retry, zpool):
        for _at, school, d, url, _slow in _pl:
            tasks.append((school, d, url))
            covered[school] = covered.get(school, 0) + 1
            avail_any = True
            if len(tasks) >= cap:
                break
        if len(tasks) >= cap:
            break
    return tasks, sweep, covered, avail_any


def mark_rank_attempted(tasks, sweep):
    """把本批选中的院系记入 attempted(同轮不重复选), 落盘。"""
    st = load_rank_state()
    st["sweep"] = sweep
    att = st.setdefault("attempted", {})
    for school, d, _u in tasks:
        att.setdefault(school, [])
        if d.name not in att[school]:
            att[school].append(d.name)
    st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_rank_state(st)


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import fix_loop as fl  # noqa: E402
import fix_queue as fq  # noqa: E402
from collect_teacher import get_llm, llm_ask_with_timeout  # noqa: E402


def _attempted_recently(d: Path) -> bool:
    """2026-10-08 用户「太慢了」提速: 批周期(~50min)会把无终态标记的
    RETRY/FAIL 院系每轮重选重跑, 硬院系每次烧 probe(600s)+3轮 — 同一批
    硬骨头一天被啃 N 遍是慢的根源。4h 内已尝试过(test_report.json mtime)
    且无 fix_done 的院系暂缓入队: 每 4h 重试一次, 让第一遍覆盖尽快扫完
    全部未完成; URL 修复等变更在 4h 窗口后自然生效。"""
    tr = d / "test_report.json"
    if not tr.exists() or (d / ".fix_done").exists():
        return False
    try:
        return (time.time() - tr.stat().st_mtime) < 4 * 3600
    except Exception:
        return False

# 排名状态文件 (依赖 BASE/fl.LOGS, 故放在导入之后; 函数在调用时才解析全局名)
RANK_FILE = BASE / "script" / "school_rank.json"
# 2026-08-30 用户指令"清北优先": 排名前2学校(清华/北大)难任务不按 sweep 跳过
TOP_SCHOOLS_NO_ATTEMPTED = 2
RANK_STATE_FILE = fl.LOGS / "_ranked_state.json"


def llm_healthy():
    """长输出健康检查(截杀波探测, 与 fix_loop 门控同标准)。
    网关长生成截杀是间歇性窗口(2026-08-23 实测: 10:43 全通, 11:05 全灭):
    一次失败不中止批次, 最多重试 3 次(间隔 10s)再判定。"""
    for attempt in range(1, 4):
        llm = get_llm()
        if llm is None:
            time.sleep(10)
            continue
        try:
            r = llm_ask_with_timeout("只输出JSON。",
                                     '写一个Python函数fib(n)求斐波那契, 加中文注释说明每行作用, '
                                     '至少40行, 返回JSON: {"code": "..."}。',
                                     tag="health")
            j = json.loads(r) if r else {}
            if len(str(j.get("code") or "")) > 500:
                return True
        except Exception:
            pass
        if attempt < 3:
            print(f"⏳ LLM长生成健康检查第{attempt}次失败, 10s后重试...")
            time.sleep(10)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("schools", nargs="?", help="逗号分隔学校列表(--all 时忽略)")
    ap.add_argument("--all", action="store_true", help="从 depart_985211.csv 读取全部学校(全量采集)")
    ap.add_argument("--per-school", type=int, default=8, help="每校选取院系数上限")
    ap.add_argument("--workers", type=int, default=24, help="跨校并发 worker 数(LLM 双层并发控制: 全局信号量兜底 + per-token 信号量, 2026-08-24 起 workers 不再被 LLM_SEM 钳制; 实际受 DrissionPage 窗口池 tab 数与内存限制)")
    ap.add_argument("--skip-queue", action="store_true",
                    help="跳过失败队列(难点院系会反复烧满 40min+ 且多轮 LLM 改进), 只选新院系。"
                    "队列院系留给专门小批次攻坚, 全量批次先扫新数据 — 2026-08-23 二轮僵尸根因")
    ap.add_argument("--retry-zombies", action="store_true",
                    help="僵尸专项: 只选带 zombie 标记的队列项(2026-08-27 僵尸集中重试), "
                    "配合 --all 或指定学校; 不设则混合模式跳过僵尸项, 僵尸留给本模式")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔学校名(全量模式跳过, 2026-08-23: 北京系瑞数慢站反复僵尸占槽,"
                    "先从全量批次排除, 留专项批次非高峰处理)")
    ap.add_argument("--ranked", action="store_true",
                    help="排名模式(2026-08-28 用户指令): 按 script/school_rank.json 排名逐校累加"
                    "未完成院系构成批次, 每批 --cap 个; 状态存 logs/_ranked_state.json, "
                    "同轮每院系只试一次, FAIL 留队列下一轮 sweep 按排名重试; 全部完成退出码 2")
    ap.add_argument("--cap", type=int, default=800,
                    help="排名模式每批院系数上限(默认800, 历史全量批约800-940)")
    ap.add_argument("--targets-file", default="",
                    help="定向清单模式(2026-09-10 用户指令): 只采集 TSV 清单中的院系"
                    "(school/dept 两列), 其他院系一概不碰; 与 --ranked/--all 互斥")
    args = ap.parse_args()

    if args.all:
        csv_path = fl.BASE.parent / "colleges department" / "depart_985211.csv"
        import io
        seen = []
        for r in csv.DictReader(io.StringIO(csv_path.read_bytes().decode("utf-8-sig", errors="replace"))):
            s = (r.get("学校名称") or "").strip()
            if s and s not in seen:
                seen.append(s)
        schools = seen
        exc = {s.strip() for s in args.exclude.split(",") if s.strip()}
        if exc:
            dropped = [s for s in schools if s in exc]
            schools = [s for s in schools if s not in exc]
            print(f"🚫 排除 {len(dropped)} 所学校: {', '.join(dropped)}")
        print(f"🗂️ 全量模式: 从 CSV 读取 {len(schools)} 所学校")
    elif args.ranked:
        schools = json.load(open(RANK_FILE, encoding="utf-8"))
        print(f"🏆 排名模式: {len(schools)} 所学校按排名逐校累加, 每批上限 {args.cap}")
    elif args.targets_file:
        schools = []  # 定向模式: 任务由下方定向分支直接构建, 此处仅占位
    else:
        schools = [s.strip() for s in args.schools.split(",") if s.strip()]
    if not llm_healthy():
        print("⛔ LLM网关长生成不可用(截杀波/降级), 中止批次。网关恢复后重跑。")
        return

    if args.targets_file:
        # ── 定向清单模式(2026-09-10 用户指令: 只解决 581 个零老师学院) ──
        # 从 TSV(school/dept 两列)构建任务, URL 从 info.csv/departments.csv 解析;
        # 清单内院系无视终态标记直接重跑(标记已由启动前清扫脚本清理)。
        _by_school = {}
        with open(args.targets_file, encoding="utf-8") as _tf:
            for _row in csv.DictReader(_tf, delimiter="\t"):
                _s = (_row.get("school") or "").strip()
                _n = (_row.get("dept") or "").strip()
                if _s and _n:
                    _by_school.setdefault(_s, []).append(_n)
        schools = sorted(_by_school)
        tasks = []
        for _s in schools:
            _sdir = fl.OUT / _s
            if not _sdir.is_dir():
                continue
            _urls = {}
            _dcsv = _sdir / "departments.csv"
            if _dcsv.exists():
                _urls = {(r.get("院系名称") or "").strip(): (r.get("院系URL") or "").strip()
                         for r in csv.DictReader(open(_dcsv, encoding="utf-8"))}
            for _n in _by_school[_s]:
                _d = _sdir / _n
                if not _d.is_dir():
                    continue
                # 本campaign内已完成/终态的跳过(定向模式循环重跑时不再重复)
                # 2026-09-11: 补 blocked_mark — 瑞数硬墙挂起(留白天专项)在定向
                # 模式同样生效, 否则每轮重新磨硬墙(09-10 起的重大/郑大 RETRY 刷屏即此)
                if ((_d / ".fix_done").exists() or (_d / ".skip_mark").exists()
                        or (_d / ".fail_mark").exists()
                        or (_d / ".blocked_mark").exists()):
                    continue
                _u = _urls.get(_n, "")
                if not _u.startswith("http"):
                    _ic = _d / "info.csv"
                    if _ic.exists():
                        for _row in csv.DictReader(open(_ic, encoding="utf-8")):
                            _uu = (_row.get("院系URL") or "").strip()
                            if _uu.startswith("http"):
                                _u = _uu
                                break
                if _u.startswith("http"):
                    tasks.append((_s, _d, _u))
        print(f"🎯 定向模式: 清单 {len(_by_school)} 校 {sum(len(v) for v in _by_school.values())} 院系, "
              f"有效任务 {len(tasks)} (workers={args.workers})")
        if not tasks:
            print("🏁 定向清单无有效任务")
            return
    elif args.ranked:
        # ── 排名模式: 逐校累加未完成院系到 cap, 失败队列下一轮 sweep 重试 ──
        tasks, sweep, covered, _ = build_ranked_tasks(args.cap)
        if not tasks:
            # 本轮无未尝试院系: 队列仍有非僵尸失败项 → 新 sweep 重试; 否则完成
            _t, _s, _c, avail_any = build_ranked_tasks(10 ** 9, exclude_attempted=False)
            if not avail_any:
                print("🏁 排名全量采集完成: 无未完成院系且队列已清 "
                      "(全部 fix_done/skip/zombie 终态)")
                sys.stdout.flush()
                os._exit(2)
            st = load_rank_state()
            st["sweep"] = st.get("sweep", 1) + 1
            st["attempted"] = {}
            save_rank_state(st)
            print(f"↻ 第{st['sweep']}轮 sweep: 重试失败队列(非僵尸), 按排名推进")
            tasks, sweep, covered, _ = build_ranked_tasks(args.cap)
            if not tasks:
                print("🏁 排名全量采集完成: 队列已清")
                sys.stdout.flush()
                os._exit(2)
        mark_rank_attempted(tasks, sweep)
        rng = [s for s in schools if covered.get(s)]
        print(f"📦 本批 {len(tasks)} 院系 | 覆盖 {len(rng)} 校 | "
              f"{rng[0]} → {rng[-1]} | 第{sweep}轮 sweep")
        for s in rng:
            print(f"   {s}: {covered[s]}")
    else:
        tasks = []  # (school, dept_dir, url)
        for school in schools:
            sdir = fl.OUT / school
            if not sdir.is_dir():
                print(f"⚠️ {school}: output 目录不存在, 跳过")
                continue
            dcsv = sdir / "departments.csv"
            urls = {}
            if dcsv.exists():
                urls = {r["院系名称"]: r.get("院系URL", "").strip()
                        for r in csv.DictReader(open(dcsv, encoding="utf-8"))}
            # 2026-08-26: info.csv 兜底 — 学校级 departments.csv 覆盖不全(1440 院系缺 URL,
            # 如交大能源研究院等空壳目录), 用院系目录 info.csv( depart_985211 生成的标准入口)补
            for _d in sorted(sdir.iterdir()):
                if not _d.is_dir() or _d.name.startswith((".", "_")):
                    continue
                if urls.get(_d.name, "").startswith("http"):
                    continue
                _ic = _d / "info.csv"
                if _ic.exists():
                    for _row in csv.DictReader(open(_ic, encoding="utf-8")):
                        _u = (_row.get("院系URL") or "").strip()
                        if _u.startswith("http"):
                            urls[_d.name] = _u
                            break
            cands = [d for d in sorted(sdir.iterdir())
                     if d.is_dir() and not d.name.startswith((".", "_"))
                     and urls.get(d.name, "").startswith("http")]
            # 2026-08-23: 排除 .skip_mark(无师资页/被WAF限制的SKIP院系) — 它们每轮
            # 重选重跑烧时间且无产出, 留给专项攻坚(清除标记后重试)。fix_done 之外
            # 的第二个"已尝试"标记, 保证新学校全量批次不再被无望院系拖慢。
            # 2026-08-23 二轮: --skip-queue 语义是"队列院系留给专门小批次", 但旧逻辑
            # 只查 .fix_done/.skip_mark, 队列成员(无标记)每轮被重选重跑重失败 —
            # 28 院系累计浪费 109 轮(上财 7 个研究基地各 FAIL 7 次, 每次 20-90min)。
            # 队列成员从 fresh 排除: skip-queue 时完全不选; 攻坚模式(qd 在前)时
            # 队列院系只走 qd 通道, 不占新增名额。add_to_queue 按 dept 名去重,
            # 计数恒定掩盖了重跑 — 排查时看日志 FAIL 次数而非队列长度。
            q_names = set(fl.load_fail_queue().get(school, {}).keys())
            fresh = [d for d in cands if not (d / ".fix_done").exists()
                     and not (d / ".skip_mark").exists() and not (d / ".rescan_done").exists()
                     and not (d / ".zombie_mark").exists()
                     and not (d / ".fail_mark").exists()
                     and not _attempted_recently(d)
                     and d.name not in q_names]
            skipped = sum(1 for d in cands if (d / ".skip_mark").exists())
            # 2026-08-27 混合模式: fresh 优先 + 队列按最老补位(每校上限5)。
            # 旧逻辑 qd 在前 → 队列≥40 的学校 fresh 一个不选(夜间 39023 批次 3h 零产出根因);
            # 新逻辑先填 fresh, 剩余名额给队列最老(at 最早)的难点, 每校 5 个上限防难点拖垮,
            # 全量批次逐步消化队列而不阻塞新数据。
            # 2026-08-27 僵尸标记: 队列项 zombie=true 的默认跳过(留给 --retry-zombies 专项),
            # 僵尸是超时挂起过的任务, 在混合批次里重试只会再次占满槽位拖死整批。
            qd = []
            if not args.skip_queue:
                q = fl.load_fail_queue().get(school, {})
                q_items = sorted(q.items(),
                                 key=lambda kv: kv[1].get("at", "9999") if isinstance(kv[1], dict) else "9999")
                _zm = []
                if args.retry_zombies:
                    _fled = _freeze_ledger_load()
                    q_items = [it for it in q_items
                               if isinstance(it[1], dict) and it[1].get("zombie")
                               and not _zombie_frozen(school, it[0], _fled)]
                    # 2026-09-02 僵尸专项修正: 只选僵尸(fresh 置空 — 原实现
                    # fresh+qd 混选, 与并行全量批次抢同一批 fresh 院系);
                    # 补选 .zombie_mark 目录(不在队列的僵尸, 与队列僵尸去重)
                    fresh = []
                    _zq_names = {n for n, _ in q_items}
                    _zm = [d for d in cands
                           if (d / ".zombie_mark").exists()
                           and d.name not in _zq_names
                           and not _zombie_frozen(school, d.name, _fled)]
                else:
                    q_items = [it for it in q_items
                               if not (isinstance(it[1], dict) and it[1].get("zombie"))]
                # 僵尸专项: 名额放开(每校 15), 混合模式维持 5 上限防难点拖垮
                qd_cap = 15 if args.retry_zombies else 5
                qd = [sdir / n for n, _ in q_items if (sdir / n).is_dir()][:qd_cap]
                qd += _zm[:max(0, qd_cap - len(qd))]
            chosen, seen = [], set()
            for d in fresh + qd:
                if d.name in seen:
                    continue
                seen.add(d.name)
                chosen.append(d)
                if len(chosen) >= args.per_school:
                    break
            for d in chosen:
                tasks.append((school, d, urls.get(d.name, "")))
            print(f"📌 {school}: 选取 {len(chosen)} 院系(队列{len(qd)} + 新增{len(chosen)-len(qd)})")

    if not tasks:
        print("无任务")
        return
    print(f"🚀 共 {len(tasks)} 院系, 并发 {args.workers}")
    # 2026-09-15 资源守卫(用户指令: 结合CPU动态管理防卡死): 批次开始前查水位,
    # red(内存压力>70%或CPU>90%)阻塞等待恢复 — 高负载开批是卡死(swap满→全退化)
    # 的历史根因; yellow 只是告警不阻断(降并发由浏览器守卫承担)
    try:
        from util.resource_guard import wait_for_green, snapshot as _rg_snap
        if not wait_for_green(timeout=600, step=15):
            print(f"⛔ 资源守卫: 600s 未恢复, 仍以 {_rg_snap()['state']} 开批(风险自担)")
        else:
            print(f"🟢 资源守卫: {_rg_snap()['state']} (内存压力{_rg_snap()['mem_pressure']:.0f}% CPU{_rg_snap()['cpu']:.0f}%)")
    except Exception:
        pass
    t0 = datetime.now()
    results = []
    _mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜", "LEGAL_EMPTY": "📋", "RETRY": "🔁", "WAIT_BROWSER": "🚦"}
    zombie_count = 0
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futs = {pool.submit(fl.fix_one, s, d, u): (s, d, u) for s, d, u in tasks}
        pending = dict(futs)
        fut_deadline = {f: time.time() + PER_DEPT_TIMEOUT for f in futs}
        fut_start = {}   # future 首次观察到 running 的时刻(开跑起算锚点, 见下)
        zombie = False
        heartbeat = time.time()
        _hb_results = 0   # 上次心跳时的结果数(无进展保护基准)
        _hb_stale = 0     # 连续无新结果的心跳数
        while pending:
            done, _ = wait(list(pending), timeout=10)
            now = time.time()
            # 僵尸收容: 只对"正在运行"的 future 计硬超时; 还在排队(PENDING)的
            # 任务顺延 deadline — 一次性 submit 数千任务时排队时间不占预算。
            # (旧 bug 修复: 统一按提交时刻计时, 排队 25 分钟后全被误判僵尸重新入队,
            #   主线程空转 4 小时产出丢失 — 2026-08-22 全量批次事故)
            # 2026-08-27 僵尸即弃(用户指令): 超时即放弃等待, 打 .zombie_mark 标记
            # (fresh/qd 双通道跳过, 避免每轮重选重挂), 记 FAIL 入队(zombie=true),
            # 留给 --retry-zombies 专项集中重试。僵尸风暴(≥ZOMBIE_EXIT_LIMIT)直接
            # 终结批次 — 僵尸线程杀不掉, 继续等只会让所有 worker 被占光。
            for f in list(pending):
                if not f.running():
                    # 还在排队: 每次检查都顺延 — 排队时间不占 900s 预算。
                    # 2026-09-03 波#10 实锤: 旧代码顺延被 "deadline 已过" 门控,
                    # 排队 8 分钟的任务在 submit-deadline 到达瞬间被斩(实际才跑
                    # 415s, trace 显示 R1.spider/eval/llm 均正常推进) → 波期
                    # 每 15-16 分钟风暴杀批一次(批#12/#13 均 ~16min 退出),
                    # 在途健康任务全灭。修=顺延移出门控, 预算从真正开跑起算
                    # (检查粒度 10s)。
                    fut_deadline[f] = now + PER_DEPT_TIMEOUT
                    continue
                if f not in fut_start:
                    fut_start[f] = now
                    fut_deadline[f] = now + PER_DEPT_TIMEOUT  # 开跑起算
                # 2026-09-04 厦大能源/眼科实锤: fix_one 入场先等熔断(≤720s),
                # 等待不占工作预算 — .work_start(入口等待结束即 touch)存在时
                # 把预算锚点顺延到真正开跑; mtime≥开跑时刻防旧批次残留标记
                # 借预算(旧标记 mtime+900 在过去, 顺延自然失效)。
                ws = pending[f][1] / ".work_start"
                try:
                    if ws.exists():
                        _m = ws.stat().st_mtime
                        if _m >= fut_start[f] - 2:
                            fut_deadline[f] = max(fut_deadline[f],
                                                   _m + PER_DEPT_TIMEOUT)
                except OSError:
                    pass
                if fut_deadline[f] < now:
                    s, d, u = pending.pop(f)
                    zombie = True
                    zombie_count += 1
                    (d / ".zombie_mark").write_text(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                    print(f"  💀 {s}/{d.name} 超过{PER_DEPT_TIMEOUT}s 未返回(僵尸), "
                          f"打标记退出等待, 记FAIL(zombie)入队 — 专项重试", flush=True)
                    fl.add_to_queue(s, d.name, u, zombie=True)
                    results.append((s, d.name, "FAIL"))
                    if zombie_count >= ZOMBIE_EXIT_LIMIT:
                        print(f"  ☠️ 僵尸风暴: 已 {zombie_count} 个僵尸 ≥ {ZOMBIE_EXIT_LIMIT}, "
                              f"终结批次释放槽位, auto_resume 重启换新任务(结果已落盘/入队)", flush=True)
                        sys.stdout.flush()
                        os._exit(0)
            if not pending:
                break
            if not done:
                if now - heartbeat >= 300:  # 进度心跳: 每 5 分钟报存活, 便于远程判活
                    heartbeat = now
                    qn = sum(len(v) for v in fl.load_fail_queue().values())
                    print(f"  ⏱️ 心跳 {datetime.now().strftime('%H:%M:%S')} 待处理 {len(pending)} "
                          f"院系 | 失败队列 {qn} | 累计结果 {len(results)}", flush=True)
                    # 无进展保护 (2026-08-29): 连续 2 次心跳(10分钟)无新结果 →
                    # 并发塌陷(蜘蛛等慢站超时/浏览器连接挂起, 全 worker 空转),
                    # 结果已落盘/入队, os._exit(0) 让 auto_resume 重启换任务,
                    # 不干等僵尸判定(5400s)。
                    # 2026-08-29 下午: 阈值 3→2 (15→10分钟) — LLM 网关周期性波动
                    # (~35-50min/轮) 时 worker 卡死在 LLM 失败重试循环, 15 分钟
                    # 空转太长(用户观察到资源闲置), 10 分钟平衡止损与防误杀。
                    # 2026-09-03 16:45: 阈值 2→3 (10→15分钟) — 尾段硬站点+网关长
                    # 波动下单院系 3 轮评估需 10-25 分钟, 10 分钟阈值导致所有 worker
                    # 都在途时被误判"塌陷"杀批 → 4 个连续 30min 周期 done+0、队列
                    # attempts 冻结、sweep 空转。15 分钟给在途院系完成窗口, 真死锁
                    # 仍由 5400s 僵尸超时兜底, 止损仅晚 5 分钟。
                    if len(results) == _hb_results:
                        _hb_stale += 1
                    else:
                        _hb_stale = 0
                    _hb_results = len(results)
                    if _hb_stale >= 3:
                        print("  🛑 15分钟无新结果(疑似并发塌陷), 强制退出重启换任务(结果已落盘)", flush=True)
                        sys.stdout.flush()
                        os._exit(0)
                continue
            for fut in done:
                s, d, u = pending.pop(fut)
                try:
                    st = fut.result()
                except Exception as e:
                    print(f"  💥 {s}/{d.name} 异常: {type(e).__name__} {str(e)[:60]}")
                    st = "RETRY"
                if st in ("PASS", "LEGAL_EMPTY"):
                    (d / ".fix_done").write_text(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                    # 队列卫生: PASS 出院系移出队列(单院系模式 fix_loop 有此逻辑,
                    # 批次模式漏了 — 残留脏数据会让队列统计失真)
                    fl.remove_from_queue(s, d.name)
                    (d / ".zombie_mark").unlink(missing_ok=True)  # 僵尸专项复生
                    (d / "_retry_reason.txt").unlink(missing_ok=True)  # 2026-09-04 streak 清理
                elif st == "SKIP":
                    # 确实没有数据(2026-08-31 用户指令): 标记skip终态
                    (d / ".skip_mark").write_text(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 无数据", encoding="utf-8")
                    fl.remove_from_queue(s, d.name)
                    (d / ".zombie_mark").unlink(missing_ok=True)
                    (d / "_retry_reason.txt").unlink(missing_ok=True)
                elif st == "FAIL":
                    # 3轮评估→改进未通过: 有数据但提取失败 — 标记fail终态,
                    # 不再自动入队无限重试(2026-08-31 用户指令: 浪费token/时间)
                    (d / ".fail_mark").write_text(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 3轮提取失败", encoding="utf-8")
                    fl.remove_from_queue(s, d.name)
                    (d / ".zombie_mark").unlink(missing_ok=True)
                    (d / "_retry_reason.txt").unlink(missing_ok=True)
                elif st == "RETRY":
                    # 瞬态失败(崩溃/异常): 入队下批重试(8次封顶)
                    # 2026-09-04 无效重试封顶: 带稳定原因入队, 同原因3轮→zombie
                    _reason = ""
                    try:
                        _rf = d / "_retry_reason.txt"
                        if _rf.exists():
                            _reason = _rf.read_text(encoding="utf-8", errors="ignore").strip()[:80]
                    except Exception:
                        pass
                    # 2026-09-09 慢站降权(用户指令): 耗时超阈值院系带 .slow_mark
                    # → 入队 slow 标志 → 第二遍补队列排最后(先易后难)
                    fl.add_to_queue(s, d.name, u, reason=_reason,
                                    slow=(d / ".slow_mark").exists())
                print(f"  {_mark.get(st, '?')} {s}/{d.name[:24]:24s} {st}")
                results.append((s, d.name, st))
                # 2026-09-15 僵尸深冻账本更新: PASS/终态 → 清账; FAIL/RETRY →
                # 累计失败轮数, 连续 FREEZE_MAX_ROUNDS 轮后深冻(冷却期内不再重试)
                if args.retry_zombies:
                    _fled = _freeze_ledger_load()
                    if st in ("PASS", "LEGAL_EMPTY", "SKIP"):
                        (_fled.get(school, {}) or {}).pop(d.name, None)
                    else:
                        rec = (_fled.get(school, {}) or {}).get(d.name, {})
                        rec["rounds"] = rec.get("rounds", 0) + 1
                        rec["last"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        _fled.setdefault(school, {})[d.name] = rec
                    _freeze_ledger_save(_fled)

    finally:
        # 2026-08-29: 不 join — shutdown(wait=False) 立即返回, 卡死的 worker
        # 线程(SSL read/浏览器连接挂起)随进程退出销毁; 结果已落盘/入队。
        # 旧 `with` 块退出是 shutdown(wait=True) 等挂死 worker, 批次永不结束
        # (上一批 222 院系 exit=143 死因: 任务全完成仍卡在 join, 只能外部 kill)。
        pool.shutdown(wait=False, cancel_futures=True)

    print(f"\n{'='*56}\n总耗时: {(datetime.now()-t0).total_seconds()/60:.1f} 分钟")
    report = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "per_school": args.per_school, "workers": args.workers,
              "duration_min": round((datetime.now()-t0).total_seconds()/60, 1),
              "zombie": zombie, "schools": {}, "totals": {"PASS": 0, "FAIL": 0, "LEGAL_EMPTY": 0, "SKIP": 0}}
    for school in schools:
        ss = [r for r in results if r[0] == school]
        if not ss:
            continue
        ok = sum(1 for r in ss if r[2] == "PASS")
        le = sum(1 for r in ss if r[2] == "LEGAL_EMPTY")
        fail = sum(1 for r in ss if r[2] == "FAIL")
        sk = sum(1 for r in ss if r[2] == "SKIP")
        # 完成度: 已有 .fix_done 的院系 / 该校正院系目录数(有效URL)
        sdir = fl.OUT / school
        total_dept = sum(1 for d in sdir.iterdir() if d.is_dir()
                         and not d.name.startswith((".", "_"))) if sdir.is_dir() else 0
        done_dept = sum(1 for d in sdir.iterdir() if (d / ".fix_done").exists()) if sdir.is_dir() else 0
        report["schools"][school] = {"total": len(ss), "PASS": ok, "FAIL": fail,
                                     "LEGAL_EMPTY": le, "SKIP": sk,
                                     "dept_total": total_dept, "dept_done": done_dept}
        for k in report["totals"]:
            report["totals"][k] += {"PASS": ok, "FAIL": fail, "LEGAL_EMPTY": le, "SKIP": sk}[k]
        print(f"{school}: {len(ss)}院系 | ✅PASS {ok} | ❌FAIL {fail} "
              f"| 📋合法空 {le} | ⬜SKIP {sk} | 完成 {done_dept}/{total_dept}")
    # 批次汇总落盘: 重启/统计时直接读 _batch_report_*.json 看每校进度
    try:
        rp = fl.LOGS / f"_batch_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📊 批次报告: {rp.name} (PASS {report['totals']['PASS']} / FAIL {report['totals']['FAIL']} "
              f"/ 合法空 {report['totals']['LEGAL_EMPTY']})")
    except Exception as e:
        print(f"⚠️ 批次报告写入失败: {e}")
    if zombie:
        # 僵尸线程仍挂着: 正常退出会 join 卡死(批次僵尸模式的根因) —
        # 结果已全部落盘/入队, 强制退出保证批次一定终结。pkill 重跑幂等安全。
        print("💀 批次存在僵尸院系, 强制退出(结果已落盘, 僵尸线程不参与join)")
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
