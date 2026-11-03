#!/usr/bin/env python3
"""teachers.csv 净化与姓名清洗 — 2026-08-31 从 collect_teacher 拆出。

导出: LIST_URL_PAT/NEWS_PATH_PAT/ADMIN_PATH_PAT/NAME_OK_PAT(详情URL校验正则),
_is_detail_url(排除法), _clean_name(去职称粘连), sanitize_teachers_csv(剔除
导航词/假URL/重复行, 重写标准 8 列)。NAV_WORDS 在 sanitize 内从
templates.spider_template 懒加载(经 _paths 保证可解析)。
"""
import csv, json, re, sys
from datetime import datetime
from pathlib import Path
# ── 教师详情URL特征校验(净化CSV时使用) ──
LIST_URL_PAT = re.compile(r'(list|index|default|channel|menu)\.(htm|html|shtml)', re.I)
NEWS_PATH_PAT = re.compile(r'/(xwgg|tzgg|tpxw|kyjz|xyxw|xwzx|xydt|news|tz|xygk|zzjg|about)/', re.I)
ADMIN_PATH_PAT = re.compile(r'/(bgfw|xxzq|djgz|xsgz|znxy|bgs|fzb|gzzd|bslc|xzbg|ghfc|djdt)/', re.I)
NAME_OK_PAT = re.compile(r'^[一-鿿·⺀-鿿]{2,4}$|^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ\'\.\-\s]{2,40}$')

def _is_detail_url(url):
    """判断URL是否像教师详情页(排除法)"""
    if not url or not url.startswith("http"):
        return False
    if LIST_URL_PAT.search(url):
        return False
    if NEWS_PATH_PAT.search(url) or ADMIN_PATH_PAT.search(url):
        return False
    if url.endswith("#"):
        return False
    if "/index.htm" in url or "/index.html" in url or "/index.shtml" in url:
        return False
    return True

def _clean_name(text):
    """清理姓名: 去空格/括号/职称后缀, 返回纯名字或空"""
    t = re.sub(r'[（(].*?[）)]', '', text)
    t = t.replace(' ', '').replace('　', '').replace('​', '').replace('‍', '')
    for suf in ["长聘副教授","长聘教授","助理研究员","助理教授","副研究员","副主任医师",
                "副教授","研究员","教授","主任医师","主治医师","讲师","博导","硕导",
                "博士生导师","硕士生导师","博士后","院士","博士","硕士","医师"]:
        t = t.replace(suf, "")
    m = re.match(r'^([一-鿿·]{2,4})', t)
    return m.group(1) if m else ""


# ── CSV 净化: 剔除导航词/假URL/重复行, 重写为标准格式 ──
def sanitize_teachers_csv(out_dir, school_name=None, dept_name=None):
    """读取 spider.py 生成的 teachers.csv, 净化后按标准 8 列重写。
    剔除: 导航词行、假详情URL(list.htm/锚点/=师资页)、重复姓名(保留首个)。
    school_name/dept_name 为 None 时从已有行推断, 推断不到才用目录名兜底。
    返回 (total, url_ok, nav_removed, fake_removed, dup_removed)。"""
    from templates.spider_template import NAV_WORDS as _NAV

    out_dir = Path(out_dir)
    csvf = out_dir / "teachers.csv"
    if not csvf.exists() or csvf.stat().st_size < 50:
        return (0, 0, 0, 0, 0)
    try:
        rows = list(csv.reader(open(csvf, encoding="utf-8")))
    except Exception:
        return (0, 0, 0, 0, 0)
    if len(rows) < 2:
        return (0, 0, 0, 0, 0)

    if school_name is None:
        school_name = str(out_dir.parent.name)
    if dept_name is None:
        dept_name = str(out_dir.name)

    hdr = rows[0]
    # 列定位(兼容中文/英文表头)
    ni = ui = ti = di = si = ci = None
    for i, h in enumerate(hdr):
        hl = h.strip().lower()
        if "姓名" in h or hl in ("name", "teacher_name"):
            ni = i
        if ("详情" in h and "URL" in h) or hl in ("url", "detail_url", "page_url"):
            ui = i
        if "职称" in h or "职务" in h or hl == "title":
            ti = i
        if "所属" in h or hl == "department":
            di = i
        if "学校" in h or hl in ("school", "university"):
            si = i
        if "学院" in h or hl == "college":
            ci = i
    if ni is None:
        return (0, 0, 0, 0, 0)
    if ui is None:
        ui = ni
    if ti is None:
        ti = ni
    if di is None:
        di = ni

    # 师资页URL(取第一行的该列值, 整表一致)
    fi = next((i for i, h in enumerate(hdr) if "师资页" in h), None)
    fac_url = ""
    if fi is not None:
        fac_url = next((r[fi].strip() for r in rows[1:] if fi < len(r) and r[fi].strip().startswith("http")), "")

    seen, kept, stats = set(), [], {"nav": 0, "fake": 0, "dup": 0}
    for r in rows[1:]:
        if len(r) <= max(ni, ui):
            continue
        name_raw = r[ni].strip() if ni < len(r) else ""
        if not name_raw:
            continue
        name = _clean_name(name_raw)
        if not name:
            continue
        if name in _NAV or name_raw in _NAV:
            stats["nav"] += 1
            continue
        # 截断导航词: 名字是某导航词的前缀(如"研究生招"⊂"研究生招生")
        if len(name) >= 2 and any(nav.startswith(name) for nav in _NAV):
            stats["nav"] += 1
            continue
        # 名字粘连栏目词(车某技术=车某+"技术职称"截断): 提取前2-3字真实名
        GLUE = re.compile(r"^([一-鿿·]{2,3})(技术|医学|科学|教学|研究|中心|学院|管理|工程|基地|平台|培养|教育|服务|建设|职称|部门|办公室|技|学|科|术)$")
        m = GLUE.match(name)
        if m:
            name = m.group(1)
            stats["nav"] += 1  # 记一次粘连修复
        url = r[ui].strip() if ui < len(r) else ""
        if not url.startswith("http") or not _is_detail_url(url):
            if url.startswith("http"):
                stats["fake"] += 1
            url = ""  # 假URL置空, 不保留
        elif url == fac_url:
            stats["fake"] += 1
            url = ""
        if name in seen:
            stats["dup"] += 1
            continue
        seen.add(name)
        title = r[ti].strip() if ti is not None and ti < len(r) else ""
        subdept = r[di].strip() if di is not None and di < len(r) else ""
        # 学校/学院名: 优先从行内取, 否则用参数
        row_school = r[si].strip() if si is not None and si < len(r) else ""
        row_dept = r[ci].strip() if ci is not None and ci < len(r) else ""
        out_school = row_school or school_name
        out_dept = row_dept or subdept or dept_name
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kept.append([out_school, out_dept, name, title, subdept or dept_name, url, fac_url, ts])

    # 重写标准格式
    with open(csvf, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["大学", "学院", "教师姓名", "职务职称", "所属院系", "个人详情URL", "师资页URL", "采集时间"])
        w.writerows(kept)
    print(f"  🧹 CSV净化: {len(kept)} 保留 | 导航词 {stats['nav']} | 假URL {stats['fake']} | 重复 {stats['dup']}")
    return (len(kept), sum(1 for r in kept if r[5].startswith("http")),
            stats["nav"], stats["fake"], stats["dup"])

