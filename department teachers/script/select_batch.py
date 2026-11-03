#!/usr/bin/env python3
"""改进批次选批器（2026-10-10 用户质问「测试院系选择依据是什么」后固化）。

此前选批=未测失败池+目录字母序(临时heredoc), 缺陷: 无优先级(复旦靠前永远
先撞)、无改进兑现队列、无形态族分层。本脚本按三级优先级选批:
  P1 漏采比: 走查数/CSV 比降序 — 最可能采出大量遗漏(有老师没提取到族)
  P2 改进兑现: 已修形态受益者(真名在保护名单被误杀/词表清洗受害者/wp3
     平台) — 修了就优先重测拿回数据
  P3 未测优先: 其余失败池, 按学校轮换(每校限2个防同校扎堆)
输出: logs/_improve_batches.json 单批100个 [{school, dept, url}]

用法: uv run python script/select_batch.py [N]
"""
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import OUT, LOGS  # noqa: E402

# ── 2026-10-25 用户指令「优先处理高水平大学, 排名高的大学」 ──
# 学校档次表: 985(0) > 211(1) > 双一流(2) > 其他(3), 档内按软科排名升序
# (1=最好)。选批排序时置于第一键 — 高档校未完成户先消化。名字归一化
# 防全角/半角括号与空格差异; 表内找不到 → (3, 999) 兜底不影响选批。
_TIER_CSV = (Path(__file__).resolve().parent.parent.parent / "complilation"
             / "985_211_双一流高校清单_全字段.csv")


def _norm_school(n: str) -> str:
    return re.sub(r"[\s（）()]", "", str(n))


_SCHOOL_TIER = {}  # 归一化校名 -> (tier, 软科排名)
try:
    with open(_TIER_CSV, encoding="utf-8-sig") as _f:
        for _r in csv.DictReader(_f):
            _nm = _norm_school(_r.get("学校名称") or "")
            if not _nm:
                continue
            if _r.get("is_985") == "1":
                _tier = 0
            elif _r.get("is_211") == "1":
                _tier = 1
            elif _r.get("is_first_flow") == "1":
                _tier = 2
            else:
                _tier = 3
            try:
                _sr = int(str(_r.get("软科排名") or "999"))
            except Exception:
                _sr = 999
            _SCHOOL_TIER[_nm] = (_tier, _sr)
except Exception:
    pass


def _school_rank(sname: str):
    """(档次, 软科排名) — 第一排序键: 985优先, 档内排名高者优先。"""
    return _SCHOOL_TIER.get(_norm_school(sname), (3, 999))


def _dept_type_rank(name: str) -> int:
    """2026-10-25 用户指令「同校内xx学院优先」— 第二排序键:
    学院/学部(0) > 医院(1) > 研究院/研究所(2) > 实验室/中心/基地/站(3)
    > 书院/继续教育/其他(4)。"""
    if name.endswith(("学院", "学部")):
        return 0
    if name.endswith("医院"):
        return 1
    if name.endswith(("研究院", "研究所")):
        return 2
    if name.endswith(("实验室", "中心", "基地", "观测站", "研究站")):
        return 3
    return 4

TODAY = date.today().strftime("%Y-%m-%d")  # 已测排除线(动态, 不再写死)
# 2026-10-12 用户指令: fail_ip_blocked/reopened 池研究重试 — 纳入测试池。
# reopened 自 2026-09-22 后从未被选中(漏采比=0 排序垫底, 实为 440 个金矿),
# fail_ip_blocked 从未入池(已有 retry_ip_blocked.py 但只复活 .fail_mark 采集
# 流水线路径, 测试循环 test_report 路径没对接 → 封禁后从未复查)。
# 2026-10-16 用户指令「专门处理未完成的」: no_faculty_page(160) + no_url(56)
# 长期不在测试池 — 未完成 1,833 里 216 户从未被测试循环处理过(池外死水)。
# 实测 no_url 56 户中 55 户 tsv 名单有 http URL(URL 一直都在, 只是状态从未
# 被重测)。全部入池给结论: 修 URL 规则/发现链改进可救回的直接采, 救不回的
# 3 次上限转终态。
_POOL = ("fail_extraction", "reopened", "pending_recollect", "fail_ip_blocked",
         "no_faculty_page", "no_url")

# 2026-10-26 用户指令「优先处理误删的老师list」: 孤儿证据 TSV(上游P0-1,
# RAW已下载文件姓名不在名单) 按孤儿数分桶, 选批排序插到学校档次之后 —
# 误删重灾户(孤儿≥100)先于同档所有普通户, 噪声带(1-4)排最后
_ORPHAN_TSV = (Path(__file__).resolve().parent.parent.parent / "teachers"
               / "logs" / "_orphan_names_for_upstream.tsv")


def _orphan_bucket(school: str, dept: str) -> int:
    """0=重灾(≥100) 1=大(20-99) 2=中(5-19) 3=小(1-4) 4=无孤儿。"""
    _orph_n = _ORPH.get(school + "|" + dept, 0)
    if _orph_n >= 100:
        return 0
    if _orph_n >= 20:
        return 1
    if _orph_n >= 5:
        return 2
    if _orph_n >= 1:
        return 3
    return 4


_ORPH = {}
try:
    with open(_ORPHAN_TSV, encoding="utf-8") as _f:
        next(_f, None)
        for _ln in _f:
            _p = _ln.rstrip("\n").split("\t")
            if len(_p) >= 3:
                try:
                    _ORPH[_p[0] + "|" + _p[1]] = int(_p[2])
                except ValueError:
                    pass
except OSError:
    pass

# IP 封禁错峰复查参数(与 script/retry_ip_blocked.py 同口径): 6h 冷却, 3 次上限
IP_BLOCK_COOLDOWN = 21600.0
IP_BLOCK_MAX_ATTEMPTS = 3


def _ip_block_eligible(d) -> bool:
    """fail_ip_blocked 院系是否可复查: 距上次封禁 >6h 且累计 <3 次,
    且无 .blocked_mark 用户搁置(2026-09-03 西交整体搁置指令)。"""
    if (d / ".blocked_mark").exists():
        return False
    mp = d / ".ip_block_mark"
    try:
        lines = mp.read_text(encoding="utf-8").strip().splitlines()
        ts = int(lines[0]) if lines else 0
        att = int(lines[1]) if len(lines) > 1 else 1
    except Exception:
        ts, att = 0, 0  # 账本缺失/损坏 → 视为可复查
    return (time.time() - ts) > IP_BLOCK_COOLDOWN and att < IP_BLOCK_MAX_ATTEMPTS


def _load_tsv():
    tsv = {}
    p = LOGS / "_unfinished_depts.tsv"
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8"), delimiter="\t"):
            tsv[r["学校"] + "|" + r["院系"]] = r
    return tsv


def _scan(allow_today: bool = False) -> list:
    """扫全库失败池, 返回 [(priority, ratio, school, dept, url, reason, at, status)]。
    allow_today=True 时放开「当日已测排除」(池不足补位用): 今日已 FAIL/RETRY
    但计数未触顶(≤2)的户可当日重选 — 加速 3 次触顶出清, 批次满载不空转。"""
    tsv = _load_tsv()
    # 2026-10-24 冻结账本(死主机/瑞数墙 7天TTL): 改进批管线此前不查冻结,
    # 死主机户每批重烧(南师大kjc 21×240s 实锤) — 与 multi_school_batch
    # 的 _zombie_frozen 同口径: rounds≥2 且 TTL 内跳过
    try:
        _fz = json.load(open(LOGS / "_zombie_freeze.json", encoding="utf-8"))
    except Exception:
        _fz = {}
    _now = datetime.now()
    out = []
    for s in OUT.iterdir():
        if not s.is_dir() or s.name.startswith("."):
            continue
        for d in s.iterdir():
            tr = d / "test_report.json"
            if not tr.exists():
                continue
            try:
                j = json.load(open(tr, encoding="utf-8"))
            except Exception:
                continue
            st = j.get("status")
            if st not in _POOL:
                continue
            # 2026-10-24 死主机冻结跳过(subnet_dead 7天 / 默认 24h)
            _frec = (_fz.get(s.name, {}) or {}).get(d.name)
            if _frec and _frec.get("rounds", 0) >= 2:
                _ttl = (7 * 24 * 3600
                        if _frec.get("form") in ("subnet_dead", "ruishu_412", "js_spa", "js_menu")
                        else 24 * 3600)
                try:
                    _last = datetime.strptime(str(_frec.get("last", "")),
                                              "%Y-%m-%d %H:%M:%S")
                    if (_now - _last).total_seconds() < _ttl:
                        continue
                except Exception:
                    pass
            # 2026-10-12: fail_ip_blocked 错峰复查 — 冷却期内/达上限/搁置不选
            if st == "fail_ip_blocked" and not _ip_block_eligible(d):
                continue
            at = j.get("at") or ""
            # 2026-10-19 用户指令「CPU闲置严重」: 当日排除从「今日全排除」放宽为
            # 「今日已测且计数≥2 才排除」— 今日计数≤1 的户当天可再测一次
            # (一天最多2测/户, 保留1天环境变化翻盘窗口; 触顶户已转终态不在池)。
            # 吞吐翻倍: 今日已测未出清359户中237户计数≤1立即可重选。
            _rcn2 = 0
            try:
                _rcn2 = int((d / ".retry_count").read_text(encoding="utf-8").strip() or 0)
            except Exception:
                _rcn2 = 0
            if at >= TODAY and not allow_today and _rcn2 >= 2:
                continue
            # 2026-10-11 循环效率修复: RETRY 不更新 test_report.at →
            # RETRY 院系每批被重选重烧(大工体育连续5批实锤) — 今天写过
            # _retry_reason.txt 的同样排除(计数≥2 才排, 同当日口径)
            rr = d / "_retry_reason.txt"
            if rr.exists() and not allow_today and _rcn2 >= 2:
                try:
                    if datetime.fromtimestamp(rr.stat().st_mtime).strftime("%Y-%m-%d") >= TODAY:
                        continue
                except Exception:
                    pass
            key = s.name + "|" + d.name
            row = tsv.get(key)
            if not row or not row["院系URL"].startswith("http"):
                continue
            reason = j.get("reason") or ""
            # P1: 漏采比 = 走查/CSV
            m = re.search(r"全走查\d+页共(\d+)个教师姓名\(去重, CSV (\d+)人\)", reason)
            ratio = 0.0
            if m and int(m.group(2)) > 0:
                ratio = int(m.group(1)) / int(m.group(2))
            # 2026-10-12 优先级重排: 漏采比≥2 → 0教师金矿 → 其余
            # (reopened 自 9-22 排序垫底从未被测, 344 个 0 教师是最大增量)
            # 2026-10-13 用户质问「教师数没增加」: 有数据重测族只是状态翻转
            # (PASS+但教师数不变) — 真增量=0教师院系采出数据。0教师金矿
            # 全面优先(不限reopened, ip_blocked冷却已过的0教师同列P1)
            try:
                tn = int(row["教师数"])
            except Exception:
                tn = 0
            # 2026-10-16 用户指令「专门处理未完成的」: 在途计数户(.retry_count>0,
            # 已 FAIL/RETRY 过 1-2 次)最高优先 — 先把在途户推出结论(3 次触顶
            # 转终态), 未完成池才会净下降; 否则新户不断进场, 在途户永远排队。
            # 2026-10-25 渲染窗口冻结: BROWSER_TIERS=off 期渲染验证不可用,
            # 「渲染失败(浏览器不可用)」户重测无意义(每批白测一次) — 跳过
            # 等浏览器窗口打开(env 恢复即自动回池)。原因取 test_report 当前
            # 原因 + _retry_reason.txt(渲染RETRY户落此处)双源
            _reason_cur = j.get("reason") or ""
            try:
                _rrf = d / "_retry_reason.txt"
                if _rrf.exists():
                    _reason_cur += " " + _rrf.read_text(encoding="utf-8",
                                                        errors="ignore")[:300]
            except Exception:
                pass
            if ("渲染失败" in _reason_cur or "浏览器不可用" in _reason_cur) \
                    and os.environ.get("BROWSER_TIERS", "").strip().lower() in ("off", "0"):
                continue
            try:
                _rcn = int((d / ".retry_count").read_text(encoding="utf-8").strip() or 0)
            except Exception:
                _rcn = 0
            # 2026-10-14 用户指令「273个学院0教师是重点分析测试对象」:
            # 学院结尾+0教师 → P0 最高优先(学院型必有师资先验, 重测翻盘金矿)
            # 2026-10-25 批间冷却(用户指令): 在途户若 4h 内刚测过(上一批刚
            # 跑完 3 轮)降为最低优先 — 防硬骨头每批第一个被烧、attempts 快速
            # 耗尽热循环; 4h 冷却过后恢复最高优先(继续推到 3 次触顶终态)
            _cool = 0.0
            try:
                if at:
                    _cool = (datetime.now() - datetime.strptime(at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                _cool = 1e9
            if _rcn > 0:
                prio = 3 if 0 < _cool < 4 * 3600 else -1
            elif tn == 0 and d.name.endswith("学院"):
                prio = 0
            elif ratio >= 2:
                prio = 1
            elif tn == 0:
                prio = 2
            else:
                prio = 3
            out.append((prio, ratio, s.name, d.name, row["院系URL"],
                        reason, at, st, tn,
                        _orphan_bucket(s.name, d.name)))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    rows = _scan()
    # 排序: 学校档次(985>211>双一流, 档内软科排名) → 孤儿分桶(误删重灾户
    # 优先, 2026-10-26 用户指令) → 院内类型(学院>医院>研究院>实验室>其他)
    # → 优先级(0漏采比≥2 < 1 reopened0教师 < 2其余) → 漏采比降序 → 未测优先
    rows.sort(key=lambda x: (*_school_rank(x[2]), x[9], _dept_type_rank(x[3]),
                             x[0], -x[1], x[6] or "0", x[2], x[3]))
    sel, keys, per_school = [], set(), {}
    for prio, ratio, sname, dname, url, reason, at, st, tn, ob in rows:
        if len(sel) >= n:
            break
        # 同校限 2 个, 防整批扎堆一校(复旦机构族教训)
        if per_school.get(sname, 0) >= 2:
            continue
        key = sname + "|" + dname
        if key in keys:
            continue
        sel.append({"school": sname, "dept": dname, "url": url})
        keys.add(key)
        per_school[sname] = per_school.get(sname, 0) + 1
    # 不足补: 放开同校限制按原序补齐
    if len(sel) < n:
        for prio, ratio, sname, dname, url, reason, at, st, tn, ob in rows:
            if len(sel) >= n:
                break
            key = sname + "|" + dname
            if key in keys:
                continue
            sel.append({"school": sname, "dept": dname, "url": url})
            keys.add(key)
    # 2026-10-19 用户指令「未完成依然庞大, 继续推进」: 当日已测排除把
    # 池子压到 <100(今天 6 批测完剩 88) → 批次小批空转, 推进放缓。第三轮
    # 补位: 仍不足则放开当日排除(allow_today) — 今日已 FAIL/RETRY 计数≤2
    # 的户当日重选, 加速 3 次触顶出清 + 批次满载。
    if len(sel) < n:
        rows2 = _scan(allow_today=True)
        rows2.sort(key=lambda x: (*_school_rank(x[2]), x[9],
                                  _dept_type_rank(x[3]),
                                  x[0], -x[1], x[6] or "0", x[2], x[3]))
        for prio, ratio, sname, dname, url, reason, at, st, tn, ob in rows2:
            if len(sel) >= n:
                break
            key = sname + "|" + dname
            if key in keys:
                continue
            sel.append({"school": sname, "dept": dname, "url": url})
            keys.add(key)
    json.dump([sel], open(LOGS / "_improve_batches.json", "w"),
              ensure_ascii=False, indent=1)
    hi = sum(1 for r in rows if r[1] >= 2)  # 漏采比≥2 族(最强增量信号)
    # 选中清单的状态构成(2026-10-12 池扩展验证: ip_blocked/reopened 是否被选上)
    from collections import Counter
    stc = Counter()
    key2st = {(r[2], r[3]): r[7] for r in rows}
    for x in sel:
        stc[key2st.get((x["school"], x["dept"]), "?")] += 1
    print(f"批次: {len(sel)} 院系 (池 {len(rows)}, 漏采比≥2 的 {hi} 个)")
    print(f"选中状态构成: {dict(stc)}")
    for x in sel[:5]:
        print("  ", x["school"], "/", x["dept"])


if __name__ == "__main__":
    main()
