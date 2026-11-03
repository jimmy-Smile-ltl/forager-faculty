#!/usr/bin/env python3
"""上游更新 → HTML 补充下载（2026-09-11 用户指令）。

策略见 docs/upstream_refresh_strategy.md。两种模式:
  --mode test  每院系补到 20 个 html（信息提取阶段测试用）
  --mode full  全量: 名单所有有效 URL（本地已有跳过, 断点续跑安全）

下载前名单过滤（是不是人名）:
  _is_person_name（垃圾词/非人名形态/机构后缀）+ URL 判定（非 http/列表页）。
  被过滤条目落 logs/_upstream_filtered.tsv, 可回查反馈上游。

用法:
  uv run python scripts/upstream_refresh.py --mode test --dry-run
  uv run python scripts/upstream_refresh.py --mode full --list logs/_new_upstream_depts.tsv
  uv run python scripts/upstream_refresh.py --mode full --school 北京大学
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlsplit

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from detail_scraper import DetailScraper, _is_person_name  # noqa: E402
from download_stage import (_is_dead_skipped, dept_download_state,  # noqa: E402
                            write_flag, _mark_dead)

_RAW = _BASE.parent / "tearchers raw data"
_UP = _BASE.parent / "department teachers" / "output"
TEST_TARGET = 20
_LIST_URL_RE = re.compile(r"/list\.htm|/list_\d+\.|/szdw\.htm|/js\.htm|"
                          r"/default\.htm|/category|/list\b", re.I)
# 2026-09-28 审查: 附件 URL 不是教师主页（地大北京名单混入 .jpg 图片、
# .doc 文档）, 下载只会落垃圾文件。.pdf 例外——两阶段架构支持直链 PDF。
_ATTACH_URL_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|bmp|webp|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|"
    r"mp3|mp4|avi)$", re.I)
# 2026-09-28 裸 IP 直链: 名单里 IP 直链基本是老系统死链（山大数字媒体
# 211.87.227.47 实测 TCP 黑洞, 每教师白烧 90s+ 全链触发看门狗杀批）。
_IP_URL_RE = re.compile(r"^https?://\d{1,3}(\.\d{1,3}){3}([:/]|$)")

# ══════════════════════════════════════════════════════════════════
# 2026-10-08 URL 修复规则收敛至公共模块 util.url_rewrites（单一事实来源）
# — 下载侧/提取侧同源导入。武理工 147 人提取全空事故根因: 下载侧改写
# URL 落盘, 提取侧按名单原 URL 算文件名 → hash 不匹配 no_local_raw。
# 规则维护只改 util/url_rewrites.py; 姓名映射仍写 logs/_name_url_maps.json。
# 修复记录仍落 logs/_url_fixes.tsv 供上游同步（见 _apply_url_fixes）。
# ══════════════════════════════════════════════════════════════════
sys.path.insert(0, str(_BASE.parent))  # util.* 在 teachers 的上级目录
from util.url_rewrites import (URL_FIX_RULES as _URL_FIX_RULES,  # noqa: F401,E402
                               load_name_url_maps as _load_name_url_maps,
                               apply_url_fix,
                               is_list_htm_person as _is_list_htm_person,
                               is_list_htm_numeric as _is_list_htm_numeric,
                               is_list_article_url as _is_list_article_url)
_url_fix_lock = threading.Lock()


def _apply_url_fixes(url: str) -> str:
    """URL 修复规则改写(规则在 util.url_rewrites); 命中并改写时记录
    logs/_url_fixes.tsv 供上游同步。未命中返回原 URL。"""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return url
    fixed = apply_url_fix(url)
    if fixed and fixed != url:
        with _url_fix_lock:
            with open(_BASE / "logs" / "_url_fixes.tsv", "a",
                      encoding="utf-8") as f:
                f.write(f"{url}\t{fixed}\t{host}\n")
        return fixed
    return url
# 院系站点首页/名单栏目词（/index.htm /main.htm 只有在这类路径下才判
# 列表页; 教师个人主页如 faculty.hfut.edu.cn/caojie/zh_CN/index.htm、
# kjxy.zuel.edu.cn/cz/main.htm 不得误杀——2026-09-14 全库 3.2 万误杀修复）
_LIST_DIR_WORDS = frozenset(("szdw", "szll", "gzry", "yjry", "xyjs",
                             "szjs", "ghjs", "cyjs", "jsjj", "teacher",
                             "teachers", "team", "list", "category"))


def _is_dept_homepage(url: str) -> bool:
    """index/main/default 只有位于站点根或名单栏目目录下才判列表页。"""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return False
    fn = segs[-1].lower()
    if fn not in ("index.htm", "index.html", "main.htm",
                  "default.htm", "default.html"):
        return False
    if len(segs) <= 1:
        return True  # host/index.htm = 站点首页
    return segs[-2].lower() in _LIST_DIR_WORDS
_lock = threading.Lock()
_filter_f = None
_filter_w = None
# 2026-10-26 用户指令: 过滤结果持久化——被过滤的名字+原因落 JSON,
# 可回查/反馈上游/避免反复判断。格式: {name: {reason, ts, count}}
_NAME_FILTER_FILE = _BASE / "logs" / "_name_filter.json"
_name_filter_lock = threading.Lock()


def _record_name_filter(names: list[tuple[str, str]]) -> None:
    """(姓名, 原因) 列表 → 合并写入 _name_filter.json。"""
    if not names:
        return
    with _name_filter_lock:
        data = {}
        try:
            data = json.loads(_NAME_FILTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for n, reason in names:
            e = data.get(n) or {"reason": reason, "ts": ts, "count": 0}
            e["reason"] = reason
            e["ts"] = ts
            e["count"] = e.get("count", 0) + 1
            data[n] = e
        tmp = _NAME_FILTER_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(_NAME_FILTER_FILE)


def _reject(school: str, dept: str, rejected: list[tuple]) -> None:
    """被过滤条目落盘（懒初始化, 线程安全）。"""
    global _filter_f, _filter_w
    with _lock:
        if _filter_f is None:
            _filter_f = open(_BASE / "logs" / "_upstream_filtered.tsv",
                             "w", encoding="utf-8")
            _filter_w = csv.writer(_filter_f, delimiter="\t")
            _filter_w.writerow(["学校", "院系", "原因", "姓名", "URL"])
        _filter_w.writerows(
            [school, dept, r[2], r[0], r[1][:90]] for r in rejected)


def html_count(school: str, dept: str) -> int:
    p = _RAW / school / dept
    if not p.is_dir():
        return 0
    return sum(1 for f in os.scandir(p)
               if f.is_file() and f.name.endswith(".html"))


def _roster(school: str, dept: str) -> list[dict] | None:
    for p in (_UP / school / dept / "teachers.json",
              _BASE / school / dept / "teachers.json"):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = data.get("teachers", []) if isinstance(data, dict) else data
        if ts:
            # 2026-10-27 anchor 恢复（发现24 同款, 与 extract_all_details.
            # load_teachers 对齐）: detail_url 非 http 但 evidence.anchor
            # 是完整 URL 时回退恢复——否则 filter_roster 全判坏URL,
            # 内大经管 110/华师地科 139/暨大生科 179 全被过滤实锤。
            # 只补非 http 缺位, 不覆盖已有合法 URL。
            for t in ts:
                if isinstance(t, dict) and not str(
                        t.get("detail_url") or "").startswith(
                        ("http://", "https://")):
                    anchor = str((t.get("evidence") or {}).get("anchor") or "")
                    if anchor.startswith(("http://", "https://")):
                        t["detail_url"] = anchor
            return ts
    return None


def filter_roster(ts: list[dict]) -> tuple[list[dict], list[tuple]]:
    """(通过教师, [(姓名, URL, 原因)…])——是不是人名 + URL 形态判定。"""
    ok, rejected = [], []
    # 2026-10-26 LLM 人名终审(用户指令: 词表打底, LLM 兜底): 词表放行的
    # 名字批量问 LLM 判非人名(栏目词/科室/导航碎片漏网家族)。缓存持久化,
    # 增量名单只付新增成本; LLM 不可用保守放行不阻塞下载。
    _llm_junk: set[str] = set()
    try:
        _cand = {str(t.get("name") or t.get("姓名") or "").strip()
                 for t in ts if isinstance(t, dict)}
        _cand = {n for n in _cand if _is_person_name(n)}
        if _cand:
            _llm_junk = llm_judge_nonperson(sorted(_cand))
    except Exception:
        pass
    # 2026-10-24 名单内 URL 共享度: 纯数字段 /NNNN/list.htm 两可
    # (mphy.ustc 2841=111 人共享名单页 / lsi.zju 25413=独享详情页),
    # 共享≥2 的纯数字段 URL = 名单页 → 拒(实测 mphy 111/111、etc
    # 60/60、rsc.cpu 16/16 全员共享实锤)。
    _share: dict[str, int] = {}
    for t in ts:
        if not isinstance(t, dict):
            continue
        u = str(t.get("detail_url") or "").strip()
        if u:
            _share[u] = _share.get(u, 0) + 1
    for t in ts:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("姓名") or "").strip()
        url = str(t.get("detail_url") or t.get("详情页URL") or "").strip()
        fixed = _apply_url_fixes(url)  # 2026-09-28 死链家族 URL 修复
        if fixed != url:
            # 修复结果持久化回名单条目——下载侧读 detail_url 才拿得到
            # 修复后的 URL（2026-09-28 重测发现修复未生效的根因）
            t = dict(t)
            t["detail_url"] = fixed
            url = fixed
        # 2026-10-01 名录重建映射: 按姓名覆盖为新版详情 URL。
        # 2026-10-24 提前应用(旧: 延迟到过滤后)——E 类缺陷院系教师
        # 的旧 URL 是名单页形态(szdw.htm/index.htm), 会被「列表页URL」
        # 过滤拒掉, 映射永远不生效。现改为映射后新 URL 参与全部过滤
        # 判定; 新 URL 若为 list.htm 详情页形态, 由 is_list_htm_person
        # 豁免放行(河海当年的延迟顾虑已随豁免规则消除)。
        fixed2 = None
        try:
            _m = _load_name_url_maps().get(
                (urlsplit(url).hostname or "").lower()) or {}
            if name in _m and _m[name] != url:
                fixed2 = _m[name]
        except Exception:
            fixed2 = None
        _orig_url = url
        if fixed2:
            url = fixed2
            t = dict(t)
            t["detail_url"] = fixed2
            with _url_fix_lock:
                with open(_BASE / "logs" / "_url_fixes.tsv", "a",
                          encoding="utf-8") as f:
                    f.write(f"{_orig_url}\t{fixed2}\t"
                            f"{urlsplit(_orig_url).hostname or ''}\n")
        if not _is_person_name(name):
            rejected.append((name, _orig_url, "非人名"))
            continue
        # 2026-10-27 学生条目过滤(华科服务计算实验室 1162 条实锤: 上游把
        # 「历届研究人员」名单页当教师名单采, person_type=student 带年级
        # 信息如"2002级博士生", 不是在职教师——不下不提取)
        if str(t.get("person_type") or "") == "student":
            rejected.append((name, _orig_url, "学生条目"))
            continue
        if name in _llm_junk:
            rejected.append((name, _orig_url, "LLM判定非人名"))
            continue
        if not url.startswith(("http://", "https://")):
            rejected.append((name, _orig_url, "坏URL"))
            continue
        # 2026-10-24 list.htm 详情页豁免(全库 6,236 条, util.url_rewrites
        # 单一事实来源): /拼音/list.htm 每人一页(河海/南大/东南/长安族)
        # 放行; 前段=栏目词(szdw/qzjs…)的真列表页仍拒。
        if _is_dept_homepage(url):
            rejected.append((name, _orig_url, "列表页URL"))
            continue
        if _LIST_URL_RE.search(url) and not _is_list_htm_person(url) \
                and not _is_list_article_url(url):
            rejected.append((name, _orig_url, "列表页URL"))
            continue
        if _is_list_htm_person(url) and _is_list_htm_numeric(url) \
                and _share.get(url, 0) >= 2:
            rejected.append((name, _orig_url, "列表页URL(数字段共享)"))
            continue
        if _ATTACH_URL_RE.search(url):
            rejected.append((name, _orig_url, "附件URL"))
            continue
        if _IP_URL_RE.search(url):
            rejected.append((name, _orig_url, "IP直链URL"))
            continue
        ok.append(t)
    # 2026-10-26 过滤结果持久化(用户指令): 拒因非人名/LLM判定非人名的
    # 写入 _name_filter.json 保留过滤结果, 可回查反馈上游
    try:
        _record_name_filter(
            [(r[0], r[2]) for r in rejected if r[2] in ("非人名", "LLM判定非人名")])
    except Exception:
        pass
    return ok, rejected


def _has_local(school: str, dept: str, t: dict) -> bool:
    name = t.get("name") or t.get("姓名") or ""
    url = t.get("detail_url") or t.get("详情页URL") or ""
    try:
        prefix = DetailScraper._make_filename(name, url, "html")[:-5]
    except Exception:
        return False
    d = _RAW / school / dept
    return any(d.glob(f"{prefix}*.html")) if d.is_dir() else False


# ══════════════════════════════════════════════════════════════════
# 学校层面抓取去重 + 按院系各存一份（2026-09-14 用户指令）
# 同一教师被上游挂到多个院系（南科大 13 个实验室/艺校全校名录）时:
#   - 网络抓取在全校只发生一次（_claimed 认领）
#   - 文件复制进每个列出该教师的院系目录（本地 copy, 不联网）
# 语义: html 下载在学校层面去重, 但各院系目录里都有一份可用的 html。
# ══════════════════════════════════════════════════════════════════
import shutil  # noqa: E402

_claimed: set[str] = set()
_claim_lock = threading.Lock()
_school_index: dict[str, dict[str, Path]] = {}
_idx_lock = threading.Lock()


def _school_files(school: str) -> dict[str, Path]:
    """全校文件索引: 文件名前缀(姓名_md5) → 第一个文件路径。"""
    with _idx_lock:
        if school in _school_index:
            return _school_index[school]
    m: dict[str, Path] = {}
    d = _RAW / school
    if d.is_dir():
        for p in d.rglob("*.html"):
            m.setdefault(p.name[:-5], p)
    with _idx_lock:
        _school_index[school] = m
    return m


def _find_school_copy(school: str, prefix: str) -> Path | None:
    return _school_files(school).get(prefix)


def _copy_to_dept(school: str, dept: str, src: Path) -> bool:
    """把已下载文件复制进目标院系目录（各院系各存一份）。"""
    d = _RAW / school / dept
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    dest = d / src.name
    if dest.exists():
        return False
    try:
        shutil.copy2(src, dest)
        # 同轮新文件进索引: 传播/后续院系立即可见（2026-09-28 审查修复——
        # 原实现索引陈旧, 收尾传播查不到本轮新下载的文件, 认领跳过者
        # 要等下一轮才补上复制）
        with _idx_lock:
            _school_index.setdefault(school, {})[dest.name[:-5]] = dest
        return True
    except Exception:
        return False


def propagate_one(school: str, dept: str) -> int:
    """传播: 同校已下载的教师文件复制进每个列出该教师的院系目录。
    纯本地操作, 无网络。返回本次复制数。"""
    roster = _roster(school, dept)
    if not roster:
        return 0
    ok, _ = filter_roster(roster)
    n = 0
    for t in ok:
        if _has_local(school, dept, t):
            continue
        name = t.get("name") or t.get("姓名") or ""
        url = t.get("detail_url") or t.get("详情页URL") or ""
        try:
            prefix = DetailScraper._make_filename(name, url, "html")[:-5]
        except Exception:
            continue
        src = _find_school_copy(school, prefix)
        if src and _copy_to_dept(school, dept, src):
            n += 1
    return n


# 2026-09-24 墙站点快速探测: 对需要下载的院系抽 3 个教师 URL 系统 curl
# 探测, 全 202/412/452(瑞数) 或全 000(不可达) → 该院系标死系跳过, 不再
# 白烧 7-10 分钟降级链(重大 412/电子科大 smee 202 实测)。按院系探测——
# 电子科大这类混合校(部分子域瑞数部分正常)不能整校一票否决。
_WALL_CODES = frozenset(("202", "412", "452", "000"))
_WALL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36")


def _probe_wall(roster_ok: list[dict]) -> str:
    """返回 ''(正常) / '瑞数墙' / '不可达' / '死链404'。"""
    urls = [t.get("detail_url") or t.get("详情页URL") or ""
            for t in roster_ok
            if (t.get("detail_url") or "").startswith("http")][:3]
    if not urls:
        return ""
    codes: list[str] = []
    for u in urls:
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", "8", "-o", "/dev/null",
                 "-w", "%{http_code}", "-A", _WALL_UA, u],
                capture_output=True, text=True, timeout=12)
            codes.append((r.stdout or "").strip() or "000")
        except Exception:
            codes.append("000")
    if codes and all(c in _WALL_CODES for c in codes):
        return "不可达" if all(c == "000" for c in codes) else "瑞数墙"
    # 2026-10-16 死链 404 家族: 采样全 404/410(改版/内容删除)同样标死,
    # 河北工大继续教育 169 人全 404 每轮白耗预算实锤(CloakBrowser 渲染
    # 复核同为 404)。dead 有 TTL 冷却, 到期重试——后续 URL 修复规则
    # 落地(如武理工 csai→ai)或站恢复后自动复活, 不会永久跳过。
    if codes and all(c in ("404", "410") for c in codes):
        return "死链404"
    return ""


def refresh_one(school: str, dept: str, mode: str, ignore_dead: bool,
                budget_min: int) -> dict:
    # 死系 TTL 内默认跳过（test 模式也跳: 中南大学 IP 不可达家族实测
    # fetch 挂起 10 分钟触发无进展看门狗杀批）; --ignore-dead 强制试
    if not ignore_dead and _is_dead_skipped(school, dept):
        return {"status": "dead-skip", "school": school, "dept": dept}
    roster = _roster(school, dept)
    if not roster:
        return {"status": "no-roster", "school": school, "dept": dept}
    ok, rejected = filter_roster(roster)
    if rejected:
        _reject(school, dept, rejected)
    have = html_count(school, dept)
    to_fetch: list[dict] = []
    copied = 0
    for t in ok:
        if _has_local(school, dept, t):
            continue
        name = t.get("name") or t.get("姓名") or ""
        url = t.get("detail_url") or t.get("详情页URL") or ""
        try:
            prefix = DetailScraper._make_filename(name, url, "html")[:-5]
        except Exception:
            continue
        # 同校已有 → 复制过来; 没有 → 全校只认领一次, 避免重复抓取
        src = _find_school_copy(school, prefix)
        if src:
            if _copy_to_dept(school, dept, src):
                copied += 1
            continue
        with _claim_lock:
            if prefix in _claimed:
                continue  # 其他院系本轮会抓, 收尾传播时复制过来
            _claimed.add(prefix)
        to_fetch.append(t)
    have += copied
    if mode == "test":
        need = max(0, TEST_TARGET - have)
        selected = to_fetch[:need]
        if not selected:
            return {"status": "ok" if have >= TEST_TARGET else "none-missing",
                    "school": school, "dept": dept}
    else:
        selected = to_fetch
    # 墙站点快速跳过: 采样"实际要下载的"教师 URL（2026-09-24）。
    # 2026-09-29: 瑞数墙不再跳过——iv8/nodenv 求解层已修复(导入路径
    # "No module named 'util'" 曾致其静默失效), 重大 1.3s/电子科大
    # 20s 实测可解; 仅"不可达"(TCP 层拒绝)快速标死, 不烧降级链。
    if selected:
        wall = _probe_wall(selected)
        # 2026-10-16 死链404 与不可达同等待遇标死(TTL 冷却)
        if wall in ("不可达", "死链404"):
            try:
                _mark_dead(school, dept, 0, 0, 0, f"探针判定: {wall}")
            except Exception:
                pass
            return {"status": "wall-skip", "school": school, "dept": dept}
    with _lock:
        print(f"  {school}/{dept}: 现有 {have}, 复制 {copied}, 下载 {len(selected)} "
              f"(过滤 {len(rejected)}, 有效名单 {len(ok)})", flush=True)
    # 每院系独立子进程下载（2026-09-11: process_batch 内置 10 分钟无进展
    # 看门狗会杀整个驱动——挂起站点必须隔离到子进程, 父进程按预算杀）
    tmp = _BASE / "logs" / "_fetch_dept_tmp"
    tmp.mkdir(exist_ok=True)
    tj = tmp / f"{school}__{dept}.json"
    tj.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    try:
        # 2026-09-29 full 模式: 预算随下载量缩放。瑞数求解(nodenv ~30s/
        # 教师)比普通 fetch(~1s)慢一个量级, 按 10s/教师预留、60 分钟封顶;
        # 健康大系几分钟即完成(预算只是上限), 挂起系由子进程内置 10 分钟
        # 无进展看门狗独立兜底。
        _timeout = min(max(budget_min * 60, len(selected) * 10), 3600)
        subprocess.run(
            [sys.executable, str(_BASE / "scripts" / "_fetch_dept.py"),
             "--school", school, "--dept", dept,
             "--teachers-json", str(tj),
             "--rate-limit", "0.3"],
            check=False, timeout=_timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # 2026-09-28 审查修复: 只标本系。整校墙/不可达已由探针在下载前
        # 拦截; 探针显示正常却挂起的多半是单系问题(个别 URL 慢),
        # 整校标记会误伤混合校的正常院系(电子科大机械挂≠信通挂)。
        try:
            _mark_dead(school, dept, 0, 0, 0,
                       f"下载预算超时挂起 {budget_min}min")
        except Exception:
            pass
    finally:
        try:
            tj.unlink()
        except Exception:
            pass
    after = html_count(school, dept)
    if mode == "full":
        st = dept_download_state(school, dept)
        if st.get("missing", 1) == 0:
            try:
                write_flag(school, dept, st)
            except Exception:
                pass
    missing_after = sum(1 for t in ok if not _has_local(school, dept, t))
    return {"status": "done" if (mode == "test" and after >= TEST_TARGET)
            or (mode == "full" and missing_after == 0) else "partial",
            "school": school, "dept": dept, "have": after}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["test", "full"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", default="")
    ap.add_argument("--school")
    ap.add_argument("--dept")
    ap.add_argument("--max-depts", type=int, default=0)
    ap.add_argument("--ignore-dead", action="store_true",
                    help="full 模式忽略死系 TTL（慎用: 会为死系白耗预算）")
    ap.add_argument("--budget-min", type=int, default=15)
    ap.add_argument("--propagate-only", action="store_true",
                    help="只做传播: 同校已下载文件复制进每个列出该教师的"
                         "院系目录, 不下载（纯本地）")
    ap.add_argument("--dept-workers", type=int, default=8,
                    help="并发院系数（默认 8; fetch-only 纯网络 IO, "
                         "8×5=40 路并发不吃内存）")
    args = ap.parse_args()

    if args.school and args.dept:
        targets = [(args.school, args.dept)]
    elif args.list:
        targets = []
        with open(args.list, encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                s = (r.get("学校") or "").strip()
                d = (r.get("院系") or "").strip()
                if s and d:
                    targets.append((s, d))
    else:
        targets = []
        for root in (_UP, _BASE):
            if not root.is_dir():
                continue
            for sp in root.iterdir():
                if not sp.is_dir() or sp.name.startswith((".", "_")) \
                        or sp.name in ("logs", "scripts"):
                    continue
                for dp in sp.iterdir():
                    if dp.is_dir():
                        targets.append((sp.name, dp.name))
    targets = sorted(set(targets))
    if args.school:
        targets = [(s, d) for s, d in targets if s == args.school]
    if args.max_depts:
        targets = targets[:args.max_depts]
    # 2026-09-24 按学校轮转交错: 同校院系连续排队时, 整校挂起（南昌大学
    # DNS 死, 12 子进程全卡）会占满全部 worker 停摆; 交错后单校故障只占
    # 少量槽位, 其余学校照常下载。
    _by_school: dict[str, list] = {}
    for s, d in targets:
        _by_school.setdefault(s, []).append((s, d))
    groups = [_by_school[s] for s in sorted(_by_school)]
    max_len = max(len(g) for g in groups) if groups else 0
    targets = [g[i] for i in range(max_len)
               for g in groups if i < len(g)]

    print(f"🎯 上游刷新 [{args.mode}] {len(targets)} 院系")
    if args.dry_run:
        print("（dry-run: 只统计缺口, 不下载）")
        for s, d in targets[:20]:
            roster = _roster(s, d)
            if not roster:
                print(f"   {s}/{d}: 无名单")
                continue
            ok, _ = filter_roster(roster)
            miss = sum(1 for t in ok if not _has_local(s, d, t))
            have = html_count(s, d)
            print(f"   {s}/{d}: 现有 {have}, 有效名单 {len(ok)}, "
                  f"缺口 {miss}" + (" (死系)" if _is_dead_skipped(s, d) else ""))
        if _filter_f is not None:
            _filter_f.close()
        return

    if args.propagate_only:
        print(f"📦 仅传播 [{len(targets)} 院系] ...", flush=True)
        total = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            pfuts = [ex.submit(propagate_one, s, d) for s, d in targets]
            for fut in as_completed(pfuts):
                try:
                    total += fut.result()
                except Exception:
                    pass
        print(f"✅ 传播完成: 复制 {total} 个文件")
        return

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.dept_workers) as ex:
        futs = [ex.submit(refresh_one, s, d, args.mode, args.ignore_dead,
                          args.budget_min) for s, d in targets]
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                # 单院系异常不得拖垮整批（2026-09-24 死系旧标记 TypeError
                # 曾崩掉整个驱动）
                counts["error"] = counts.get("error", 0) + 1
                continue
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    if _filter_f is not None:
        _filter_f.close()
    # 收尾传播: 网络抓取在全校只发生一次, 这里把已下载文件复制进每个
    # 列出该教师的院系目录（含被其他院系本轮认领抓取的那些）。
    # 2026-09-28 审查修复: 先清全校文件索引——本轮子进程新下载的文件不在
    # 陈旧索引里, 不清会导致传播查不到、认领跳过者要等下一轮才补复制。
    print("\n📦 收尾传播: 全校文件复制到各院系 ...", flush=True)
    with _idx_lock:
        _school_index.clear()
    prop_total = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        pfuts = [ex.submit(propagate_one, s, d) for s, d in targets]
        for fut in as_completed(pfuts):
            try:
                prop_total += fut.result()
            except Exception:
                pass
    print(f"   传播复制 {prop_total} 个文件", flush=True)
    print(f"\n✅ 上游刷新完成: " + " | ".join(
        f"{k} {v}" for k, v in sorted(counts.items())))
    print("过滤清单: logs/_upstream_filtered.tsv")


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════
# LLM 人名终审（2026-10-26 用户指令: 词表打底, LLM 兜底判真人）
# 词表(_is_person_name)确定性命中的直接拒; 词表放行的名字交给 LLM
# 批量判断(栏目词/科室/导航碎片/截断残片漏网家族——北师大教育学部
# 14 条栏目词、吉大"上页"×2 实锤)。缓存持久化 logs/_llm_name_judge.json,
# 只问未判过的名字, 增量名单只付新增成本。
# ══════════════════════════════════════════════════════════════════
_LLM_NAME_FILE = _BASE / "logs" / "_llm_name_judge.json"
_llm_name_cache: dict[str, bool] | None = None   # name -> is_person
_llm_name_lock = threading.Lock()
_LLM_BATCH = 60


def _llm_name_load() -> dict[str, bool]:
    global _llm_name_cache
    with _llm_name_lock:
        if _llm_name_cache is None:
            try:
                _llm_name_cache = json.loads(
                    _LLM_NAME_FILE.read_text(encoding="utf-8"))
            except Exception:
                _llm_name_cache = {}
        return dict(_llm_name_cache)


def _llm_name_save(new: dict[str, bool]) -> None:
    with _llm_name_lock:
        if _llm_name_cache is None:
            _llm_name_load()
        _llm_name_cache.update(new)
        tmp = _LLM_NAME_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_llm_name_cache, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(_LLM_NAME_FILE)


def llm_judge_nonperson(names: list[str]) -> set[str]:
    """LLM 批量判断非人名集合（缓存优先, 未判名字分批问 LLM）。

    失败/超时保守放行（不误杀真教师, 词表层兜底）。"""
    names = sorted({n for n in names if n and 2 <= len(n) <= 8})
    if not names:
        return set()
    cache = _llm_name_load()
    uncached = [n for n in names if n not in cache]
    if uncached:
        try:
            sys.path.insert(0, str(_BASE.parent))
            from util.llm_gate import llm_ask_with_timeout, parse_json
            system = (
                "你是高校教师名单质检员。给出中文名字列表，判断哪些不是"
                "真实人名（是栏目名/机构名/科室名/导航碎片/页面控件词/"
                "截断残片等，如「麻醉科」「上页」「组织机构」「头条关注」）。"
                "只输出 JSON: {\"nonperson\": [\"名字\", ...]}。"
                "拿不准的一律不列入（宁缺毋滥，不误杀真名）。")
            for i in range(0, len(uncached), _LLM_BATCH):
                chunk = uncached[i:i + _LLM_BATCH]
                prompt = "名字列表: " + "、".join(chunk)
                r = llm_ask_with_timeout(system, prompt, timeout=60,
                                         tag="name_judge")
                data = parse_json(r or "")
                non = (data or {}).get("nonperson") or []
                _llm_name_save({n: (n not in non) for n in chunk})
                if isinstance(non, list):
                    for n in non:
                        if isinstance(n, str) and n in chunk:
                            _llm_name_save({n: False})
        except Exception:
            pass  # LLM 不可用: 保守放行, 词表层兜底
    cache = _llm_name_load()
    return {n for n in names if cache.get(n) is False}
