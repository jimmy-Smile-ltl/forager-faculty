#!/usr/bin/env python3
"""
教师详情页采集主入口。
遍历 department teachers/ 下各学校/院系的 teachers.json，
获取每位教师的详情页，提取 13 个字段，保存原始数据。

用法:
    uv run python extract_all_details.py --school 吉林大学 --dept 数学学院
    uv run python extract_all_details.py --school 吉林大学
    uv run python extract_all_details.py --all --workers 2
    uv run python extract_all_details.py --all --resume
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# 无缓冲输出：nohup 后台运行时日志实时可查
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ── 项目路径 ──
_COLLEGE_ROOT = Path(__file__).resolve().parent.parent
_DEPT_TEACHERS = _COLLEGE_ROOT / "department teachers" / "output"
_RAW_DATA_ROOT = _COLLEGE_ROOT / "tearchers raw data"
_THIS_DIR = _COLLEGE_ROOT / "teachers"

# SIGTERM/SIGINT 优雅落盘: process_dept 注册处理器时置当前 scraper,
# 信号到来时保存 _batch_partial 后退出（见 process_dept._on_signal）。
_g_scraper = None

# ── 这些学校没有二级学院子目录（学院类型学校）──
COLLEGE_SCHOOLS = {
    "中央美术学院", "中央戏剧学院", "中央音乐学院",
    "中国美术学院", "中国音乐学院", "上海音乐学院",
    "北京协和医学院", "外交学院",
}

# ── 非人名条目过滤：Step 3 提取出的明显组织/栏目名 ──
_NON_PERSON_KEYWORDS = (
    "处", "部", "中心", "工作", "小组", "党委", "团委", "工会",
    "办公室", "委员会", "研究所", "实验室", "教研室", "学院", "学校",
    "招生", "就业", "财务", "后勤", "图书馆", "编辑部",
    "简介", "概况", "动态", "学习", "新闻", "通知", "公告",
    "部门", "队伍", "人才", "书记", "主任", "简介", "组织", "机构",
    # 纯职称/头衔词（吉大植物科学学院教训: "教授"/"副教授"/"讲师"/
    # "知名专家" 是师资分类导航链接，不是人名）
    "教授", "讲师", "研究员", "工程师", "院士", "专家", "助教",
    "实验师", "名师", "师资", "名录", "教师",
    # 校名后缀与页面词
    "大学", "首页", "导航", "链接", "栏目", "网站",
    "交流", "培养", "本科", "硕士", "博士", "合作", "校友",
    "校历", "校史", "校歌", "校训", "校情", "日历", "黄页",
    "学堂", "地图", "慕课", "课程", "下载", "服务", "系统", "平台", "资源",
    # 组织/栏目/品牌词（东北农大教训: "二级党校"/"农学系"/"金陵讲堂"/
    # "一级学科"/"学科团队"/"研究生会"/"易班" 是导航链接不是人名;
    # "系" 作单字后缀覆盖 "X系" 院系单位, 中文人名不含"系"字）
    "党校", "纪委", "支部", "讲堂", "学科", "团队", "系",
    "研究生", "书院", "学部", "研究院", "基地", "培训", "医院",
    "易班", "教务", "学工", "教学部", "招生办",
    # 云大教训: 上游把栏目导航词当教师名, "教学建设"/"教研风采"
    # 漏网通过词表。中文人名不会含"教学"/"风采"两字连写, 零误杀;
    # 注意"建设"不能加——"李建设/王建设"是真实人名
    "教学", "风采",
    # 陕师大教训（发现21）: 栏目导航"集体备课"/"宣讲团"/"信息登记"
    # 被 Step3 当教师名提取。中文人名不会含"备课"/"宣讲"/"登记"
    "备课", "宣讲", "登记",
    # 陕师大教训（发现23）: 教师干部培训学院把教育部官网栏目
    # "办公厅"/"教材局"（moe.gov.cn）当教师名, 远程学院"名课鉴赏"
    # 是栏目页。厅/局/鉴赏/名课不会出现在中文人名, 零误杀
    "厅", "局", "鉴赏", "名课",
    # 清华教训（发现25）: 招聘/加入栏目被当教师名（职工招聘/招聘方式/
    # 加入我们）。中文人名不含"招聘"/"加入", 零误杀
    "招聘", "加入",
)


def _is_person_name(name: str, url: str = "") -> bool:
    """判断是否为真实人名（2-4 个汉字，非组织/栏目名）。

    信号强度从高到低：
    1. 名字含组织关键词 → 非人名（强信号）
    2. URL 是导航列表页（list.htm）→ 非详情页（强信号）
    3. 其余情况放行，避免误杀真实教师（URL 形态各校不同）
    """
    if not name or len(name) < 2 or len(name) > 4:
        return False
    # 含英文字母/数字的通常不是中文人名
    if any(c.isascii() and c.isalnum() for c in name):
        return False
    # 组织关键词
    if any(k in name for k in _NON_PERSON_KEYWORDS):
        return False
    # URL 是导航页 → 非详情页
    # 2026-10-24 豁免: /拼音/list.htm 每人一页详情页形态(全库 6,236 条
    # 实测, 河海/南大/东南/长安族)放行——判别同下载侧
    # util.url_rewrites.is_list_htm_person(单一事实来源); 前段=栏目词
    # (szdw/qzjs…)的真列表页仍拒。
    if url:
        if url.endswith("list.htm") or url.rstrip("/").endswith("list"):
            try:
                import sys as _sys
                _sys.path.insert(0, str(_COLLEGE_ROOT))  # util 在 teachers 上级
                from util.url_rewrites import is_list_htm_person
                if not is_list_htm_person(url):
                    return False
            except Exception:
                return False
    return True


# ═══════════════════════════════════════════════════════════════
#  断点续传
# ═══════════════════════════════════════════════════════════════

def _state_path() -> Path:
    return _THIS_DIR / "task_state.json"


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    p = _state_path()
    existing = load_state()
    key = f"{state.get('school', '')}/{state.get('dept', '')}"
    existing[key] = state
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def is_done(school: str, dept: str) -> bool:
    st = load_state()
    key = f"{school}/{dept}"
    return st.get(key, {}).get("status") == "done"


# ═══════════════════════════════════════════════════════════════
#  数据发现
# ═══════════════════════════════════════════════════════════════

def list_schools() -> list[str]:
    """列出所有有 teachers.json 的学校目录。"""
    schools = []
    for p in sorted(_DEPT_TEACHERS.iterdir()):
        if p.is_dir() and not p.name.startswith(".") and p.name not in COLLEGE_SCHOOLS:
            # 检查是否有子目录含 teachers.json
            for sub in p.iterdir():
                if sub.is_dir() and (sub / "teachers.json").exists():
                    schools.append(p.name)
                    break
    return schools


def list_depts(school: str) -> list[dict]:
    """列出学校下所有有 teachers.json 的院系。返回 [{"dept": ..., "json_path": ...}]。"""
    school_dir = _DEPT_TEACHERS / school
    if not school_dir.is_dir():
        return []
    depts = []
    for sub in sorted(school_dir.iterdir()):
        if sub.is_dir():
            jp = sub / "teachers.json"
            if jp.exists():
                depts.append({"dept": sub.name, "json_path": jp})
    return depts


def load_teachers(dept_info: dict) -> list[dict]:
    """从 teachers.json 加载教师列表（过滤非人名条目）。JSON 空时回退 CSV。

    名字做职称粘连清洗: '陈招斌高' → 陈招斌（职称'高级工程师'被 Step 3 截断粘连）。
    """
    try:
        data = json.loads(dept_info["json_path"].read_text(encoding="utf-8"))
        teachers = data.get("teachers", [])
        faculty_url = data.get("faculty_url", "")
    except Exception:
        teachers = []
        faculty_url = ""

    if not teachers:
        # 回退到 teachers.csv（部分学校 JSON 未写入）
        json_path = dept_info.get("json_path") if isinstance(dept_info, dict) else None
        if json_path is None:
            return []  # 名单实时变动: 院系已不在名单, 空返回不崩溃
        csv_path = json_path.parent / "teachers.csv"
        if csv_path.exists():
            try:
                import csv as _csv
                with open(csv_path, encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        name = (row.get("教师姓名") or row.get("姓名") or "").strip()
                        url = (row.get("个人详情URL") or row.get("详情页URL") or "").strip()
                        if name:
                            teachers.append({
                                "name": name,
                                "detail_url": url,
                                "department": row.get("所属院系", ""),
                            })
                print(f"  📄 从 CSV 回退加载: {len(teachers)} 人")
            except Exception:
                pass

    # ── 名字清洗: 剥离粘连的职称（含 Step 3 截断粘连 '陈招斌高'）──
    try:
        from faculty_discovery import clean_name
        cleaned = []
        for t in teachers:
            name, title = clean_name(t.get("name", ""))
            t["name"] = name
            if title and not t.get("title"):
                t["title"] = title
            cleaned.append(t)
        # 2026-10-24 同名单证据剥离: 名字以单字职称尾(副/高/实/助/教/工/
        # 研/讲)结尾且去尾后的名字在同一名单存在(刘小龙副↔刘小龙 两条目
        # 实锤) → 判定粘连剥离。名单内有干净版=强证据, 比 title 字段稳定
        # (李纯高类真名在名单内无去尾版, 不误伤)。
        _names_set = {t["name"] for t in cleaned if t.get("name")}
        for t in cleaned:
            n = t["name"]
            if len(n) >= 3 and n[-1] in "副高实助教工研讲":
                base = n[:-1]
                if base in _names_set:
                    t["name"] = base
        teachers = cleaned
    except Exception:
        pass

    # 过滤明显非人名的条目（组织/栏目名）; 2026-10-27 学生条目同步过滤
    # (华科服务计算实验室 1162 条 person_type=student 实锤——历届博士生
    # 被当教师采, 不下不提取, 与下载侧 filter_roster 同口径)
    filtered = [
        t for t in teachers
        if _is_person_name(t.get("name", ""), t.get("detail_url", ""))
        and str(t.get("person_type") or "") != "student"
    ]
    if len(filtered) < len(teachers):
        print(f"  🔍 过滤非人名条目: {len(teachers) - len(filtered)} 个")
    # 上游 URL 缺陷统计（发现22 改进: 加载阶段就归因, 不必等跑完才发现;
    # process_teacher 已有非 http 前置拦截, 这里只是加载时可见化提示）
    # 发现24（2026-08-28）: 合肥工业 Step3 把 detail_url 写空、URL 全在
    # evidence.anchor（智能商学院 256 + 智能软件 59 人）——agent 侧容错:
    # detail_url 无效但 evidence.anchor 是完整 http URL 时回退恢复, 不依赖
    # 上游修复即可补采。
    recovered = 0
    for t in filtered:
        url = str(t.get("detail_url") or "")
        if not url.startswith(("http://", "https://")):
            anchor = str((t.get("evidence") or {}).get("anchor") or "")
            if anchor.startswith(("http://", "https://")):
                t["detail_url"] = anchor
                recovered += 1
    bad_url = sum(1 for t in filtered
                  if not str(t.get("detail_url") or "").startswith(("http://", "https://")))
    if recovered:
        print(f"  🔧 从 evidence.anchor 恢复 {recovered} 个 URL（发现24 字段错位容错）")
    if bad_url:
        print(f"  ⚠️ 上游 URL 缺陷 {bad_url}/{len(filtered)} 人: detail_url 非 http"
              f"（Step3 字段错位/缺 URL, process_teacher 会快速判死不抓取）")
    return filtered


# ═══════════════════════════════════════════════════════════════
#  处理一个院系
# ═══════════════════════════════════════════════════════════════

def _apply_batch_select(teachers: list[dict], args) -> list[dict]:
    """小批量测试选择器（2026-08-27 循环节奏工具化）: --limit 取前 N,
    --sample 随机抽样 N（种子默认固定, --sample-seed 可换——循环轮换
    种子扩大页面形态覆盖, 避免每次都抽同一批人, 发现22 建议）。两者
    都未给 → 原样返回。"""
    if not teachers:
        return teachers
    n = len(teachers)
    if args.limit and args.sample:
        print("⚠️ --limit 与 --sample 互斥, 仅用 --limit")
        args.sample = 0
    if args.limit:
        k = min(args.limit, n)
        print(f"  🎯 小批量测试: 取前 {k}/{n} 名 (--limit {args.limit})")
        return teachers[:k]
    if args.sample:
        import random as _random
        seed = getattr(args, "sample_seed", 0) or 20260827  # 默认固定 → 可复现
        rng = _random.Random(seed)
        k = min(args.sample, n)
        picked = rng.sample(range(n), k)
        print(f"  🎯 小批量测试: 随机抽样 {k}/{n} 名 (--sample {args.sample}, seed={seed})")
        return [teachers[i] for i in sorted(picked)]
    return teachers


def process_dept(
    school: str,
    dept: str,
    teachers: list[dict],
    rate_limit: float = 1.0,
    teacher_workers: int = 1,
    skip_done: bool = False,
    reextract_llm: bool = False,
    fetch_only: bool = False,
    reapply_rules: bool = False,
    local_only: bool = False,
) -> dict:
    """处理一个院系的所有教师详情。

    teacher_workers > 1 时教师级并发（process_batch 线程池）：
    LLM 提取等等待环节并行 → 人均耗时由 LLM 决定时提速接近 workers 倍。
    2026-08-27 教训: 串行时 LLM 慢时段人均吃满 60s 超时 (73s/人),
    单 token 学院亲和不变, 并发数需 ≤ per-token 槽 × 可用 token 数。

    skip_done=True（--resume）时按已有 teachers_detail.json 成功记录
    去重跳过已完成教师, 不重烧 LLM（2026-08-27 教训: 被 kill 重启
    曾重提取已落盘 80 人）。
    """
    from detail_scraper import DetailScraper

    # ── SIGTERM/SIGINT 优雅落盘（2026-08-27 教训: 后台进程被 kill 两次,
    #    全靠增量落盘兜住; 信号处理缩小"两次增量之间"的丢失窗口）──
    import signal as _signal
    _prev_term = _signal.getsignal(_signal.SIGTERM)
    _prev_int = _signal.getsignal(_signal.SIGINT)

    def _on_signal(signum, _frame):
        sc = _g_scraper
        if sc is not None:
            try:
                sc.save_results(getattr(sc, "_batch_partial", []))
                print(f"  ⚡ 收到信号 {signum}，已保存 {len(getattr(sc, '_batch_partial', []))} 条")
            except Exception as e:
                print(f"  ⚡ 收到信号 {signum}，保存失败: {e}")
        raise SystemExit(130)

    print(f"\n{'=' * 60}")
    print(f"  🏫 {school} > {dept}")
    print(f"  👥 {len(teachers)} 名教师")
    print(f"{'=' * 60}")

    scraper = DetailScraper(
        school_name=school,
        dept_name=dept,
        raw_data_root=str(_RAW_DATA_ROOT),
        csv_output_root=str(_THIS_DIR),
        rate_limit=rate_limit,
        resume=skip_done,
        reextract_llm=reextract_llm,
        fetch_only=fetch_only,
        local_only=local_only,
    )

    # 任务边界: 学院亲和（2026-08-25 借鉴 department teachers 范式）——
    # 固定本线程到 hash(学院) 槽位 token: 院系内连续 ask 命中网关 prompt
    # 缓存省钱; 跨批次/重启稳定; 多学院并发（--all）按学院均匀分散负载。
    # begin_task 只影响当前线程, 线程池 worker 里调用安全, 无需加锁。
    scraper.begin_task()

    # 注册信号处理器（全局引用当前 scraper; 完成后恢复默认处理器）
    global _g_scraper
    _g_scraper = scraper
    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    results = scraper.process_batch(teachers, workers=teacher_workers,
                                    skip_done=skip_done)
    _g_scraper = None
    _signal.signal(_signal.SIGTERM, _prev_term)
    _signal.signal(_signal.SIGINT, _prev_int)

    scraper.save_results(results)

    # 统计
    html_count = sum(1 for r in results if r.get("来源类型") in ("html", "html_fallback"))
    pdf_count = sum(1 for r in results if r.get("来源类型") == "pdf")
    fail_count = sum(1 for r in results if r.get("来源类型") in ("fetch_failed", "no_url"))

    summary = {
        "school": school,
        "dept": dept,
        "status": "done",
        "total": len(results),
        "html": html_count,
        "pdf": pdf_count,
        "failed": fail_count,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"  📊 HTML: {html_count}, PDF: {pdf_count}, 失败: {fail_count}")

    save_state(summary)
    return summary


# ═══════════════════════════════════════════════════════════════
#  处理一个学校
# ═══════════════════════════════════════════════════════════════

def process_school(school: str, resume: bool = False, rate_limit: float = 1.0,
                     reextract_llm: bool = False, fetch_only: bool = False,
                     reapply_rules: bool = False,
                   teacher_workers: int = 1, sample: int = 0, limit: int = 0,
                   sample_seed: int = 0, local_only: bool = False):
    print(f"\n{'#' * 60}")
    print(f"  🏫 {school}")
    print(f"{'#' * 60}")

    depts = list_depts(school)
    print(f"  共 {len(depts)} 个院系有教师数据")

    total_teachers = 0
    for i, d in enumerate(depts):
        if (resume and not reextract_llm and not reapply_rules
                and is_done(school, d["dept"])):
            print(f"  ⏭️ [{i + 1}/{len(depts)}] {d['dept']} (已完成，跳过)")
            continue

        teachers = load_teachers(d)
        # 全校模式抽样（2026-08-28 测试循环: --sample/--limit 透传,
        # 每院系只抽 demo 几人, 大幅减少网络消耗——关键任务测试而非采集）
        if sample or limit:
            from argparse import Namespace as _NS
            teachers = _apply_batch_select(
                teachers, _NS(limit=limit, sample=sample, sample_seed=sample_seed))
        print(f"  [{i + 1}/{len(depts)}] {d['dept']}: {len(teachers)} 名教师(demo)")

        if not teachers:
            save_state({
                "school": school,
                "dept": d["dept"],
                "status": "done",
                "total": 0,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        process_dept(school, d["dept"], teachers, rate_limit=rate_limit,
                     teacher_workers=teacher_workers,
                     skip_done=resume and not reapply_rules,
                     reextract_llm=reextract_llm, fetch_only=fetch_only,
                     reapply_rules=reapply_rules, local_only=local_only)
        total_teachers += len(teachers)

    return total_teachers


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="高校教师详情页采集")
    parser.add_argument("--school", default=None, help="指定学校名称")
    parser.add_argument("--dept", default=None, help="指定院系名称（需配合 --school）")
    parser.add_argument("--all", action="store_true", help="采集全部学校")
    parser.add_argument("--workers", type=int, default=2, help="学校级并发数（默认 2）")
    parser.add_argument("--teacher-workers", type=int, default=1,
                        help="教师级并发数（默认 1；LLM 慢时段建议 3-4，per-token 槽以内）")
    parser.add_argument("--limit", type=int, default=0,
                        help="小批量测试: 只处理前 N 名教师（0=全部, 2026-08-27 循环节奏工具化）")
    parser.add_argument("--sample", type=int, default=0,
                        help="小批量测试: 随机抽样 N 名教师（可与 --limit 互斥, 默认种子可复现）")
    parser.add_argument("--sample-seed", type=int, default=0,
                        help="抽样种子（默认 20260827; 循环轮换种子扩大覆盖, 发现22）")
    parser.add_argument("--resume", action="store_true", help="断点续传模式")
    parser.add_argument("--reextract-llm", action="store_true",
                        help="配合 --resume: LLM 降级(已降级启发式)行复用本地文件重跑 LLM")
    parser.add_argument("--fetch-only", action="store_true",
                        help="只抓取原始 HTML/PDF 落盘, 跳过 LLM（批量预下载, 提取后续离线做）")
    parser.add_argument("--reapply-rules", action="store_true",
                        help="重新应用提取规则: 禁用 resume 跳过, 复用本地 HTML/PDF 重跑全部行")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过 LLM, 只跑启发式+学院规则+清理链（规则效果快速验证, 不烧 token）")
    parser.add_argument("--local-only", action="store_true",
                        help="只读本地 HTML/PDF, 无本地文件即跳过且不碰网络（第二步提取专用）")
    parser.add_argument("--rate-limit", type=float, default=1.0,
                        help="教师间请求间隔（秒，默认 1.0）")
    args = parser.parse_args()

    # ── 确保 detail_scraper 可 import ──
    sys.path.insert(0, str(_THIS_DIR))

    # ── 单院系模式 ──
    if args.school and args.dept:
        depts = list_depts(args.school)
        found = None
        for d in depts:
            if d["dept"] == args.dept:
                found = d
                break
        if not found:
            print(f"❌ 未找到院系: {args.dept}")
            sys.exit(1)

        teachers = load_teachers(found)
        if not teachers:
            print(f"❌ {args.school}/{args.dept}: 无教师数据")
            sys.exit(1)

        teachers = _apply_batch_select(teachers, args)

        print(f"👥 {len(teachers)} 名教师")
        summary = process_dept(
            args.school, args.dept, teachers, rate_limit=args.rate_limit,
            teacher_workers=args.teacher_workers,
            skip_done=args.resume and not args.reapply_rules,
            reextract_llm=args.reextract_llm,
            fetch_only=args.fetch_only or args.no_llm,
            reapply_rules=args.reapply_rules,
            local_only=args.local_only or args.no_llm)
        print(f"\n✅ 完成: {summary['total']} 名教师, HTML: {summary['html']}, PDF: {summary['pdf']}")
        sys.exit(0)

    # ── 单学校模式 ──
    if args.school:
        process_school(args.school, resume=args.resume, rate_limit=args.rate_limit,
                       teacher_workers=args.teacher_workers,
                       reextract_llm=args.reextract_llm,
                       fetch_only=args.fetch_only or args.no_llm,
                       reapply_rules=args.reapply_rules,
                       local_only=args.local_only or args.no_llm,
                       sample=args.sample, limit=args.limit, sample_seed=args.sample_seed)
        sys.exit(0)

    # ── 全部学校模式 ──
    if args.all:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        schools = list_schools()
        print(f"\n🎓 全部教师详情采集")
        print(f"  共 {len(schools)} 所学校有教师数据, {args.workers} 个并发")
        print(f"  resume={'yes' if args.resume else 'no'}")
        print()

        if args.workers <= 1:
            for school in schools:
                process_school(school, resume=args.resume, rate_limit=args.rate_limit,
                               teacher_workers=args.teacher_workers,
                               reextract_llm=args.reextract_llm,
                               fetch_only=args.fetch_only or args.no_llm,
                               reapply_rules=args.reapply_rules,
                               local_only=args.local_only or args.no_llm,
                               sample=args.sample, limit=args.limit,
                               sample_seed=args.sample_seed)
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {
                    pool.submit(process_school, school, resume=args.resume,
                                rate_limit=args.rate_limit,
                                reextract_llm=args.reextract_llm,
                                fetch_only=args.fetch_only or args.no_llm,
                                reapply_rules=args.reapply_rules,
                                local_only=args.local_only or args.no_llm,
                                teacher_workers=args.teacher_workers,
                                sample=args.sample, limit=args.limit,
                                sample_seed=args.sample_seed): school
                    for school in schools
                }
                for f in as_completed(futs):
                    school = futs[f]
                    try:
                        f.result()
                        print(f"  ✅ {school}")
                    except Exception as e:
                        print(f"  ❌ {school}: {e}")

        print(f"\n🎉 全部完成!")
        sys.exit(0)

    # ── 无参数 → 显示帮助 ──
    parser.print_help()
    print("\n示例:")
    print("  uv run python extract_all_details.py --school 吉林大学 --dept 数学学院")
    print("  uv run python extract_all_details.py --school 吉林大学")
    print("  uv run python extract_all_details.py --all --workers 2")
    print("  uv run python extract_all_details.py --all --resume")


if __name__ == "__main__":
    main()
