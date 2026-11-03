#!/usr/bin/env python3
"""名单质量检测（2026-08-31, 第一步的前置）——检测上游 teachers 名单里
错误加入的非教师内容（导航栏链接/栏目词/垃圾名/URL 缺陷），产出 QC 报告，
供清洗过滤与上游反馈。

用法:
  uv run python roster_qc.py --school 北京大学              # 单校
  uv run python roster_qc.py --all                          # 全校
  uv run python roster_qc.py --school 北京大学 --filter     # 产出过滤后名单（不覆盖原文件）
"""
import argparse
import json
import re
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_ROSTER = _COLLEGE_ROOT / "department teachers" / "output"
sys.path.insert(0, str(_BASE))

from detail_scraper import _is_junk_name  # noqa: E402

# 名单 URL 指向列表/栏目页的形态（非教师详情页）
_LIST_URL_RE = re.compile(r"/list\.htm|/list_\d+\.|/szdw\.htm|/js\.htm|"
                          r"/index\.htm|/main\.htm|/default\.htm|"
                          r"/category|/list\b", re.I)


def qc_dept(school: str, dept: str) -> dict:
    jp = _ROSTER / school / dept / "teachers.json"
    if not jp.exists():
        return {"school": school, "dept": dept, "error": "无 teachers.json"}
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        return {"school": school, "dept": dept, "error": "JSON 解析失败"}
    ts = data if isinstance(data, list) else data.get("teachers", [])

    junk_names, bad_urls, list_urls = [], [], []
    for t in ts:
        if not isinstance(t, dict):
            # 畸形条目: 整行 list（北外日语学院等 3 文件顶层即 list）,
            # load_teachers 同口径走 CSV 回退——QC 跳过, 不 crash 整轮
            continue
        name = str(t.get("name") or t.get("姓名") or "")
        url = str(t.get("detail_url") or "")
        if _is_junk_name(name):
            junk_names.append({"name": name, "url": url[:80]})
        elif url and not url.startswith(("http://", "https://")):
            bad_urls.append({"name": name, "url": url[:80]})
        elif url and _LIST_URL_RE.search(url):
            # 2026-10-24 list.htm 详情页豁免(与下载侧同源判别):
            # /拼音/list.htm 每人一页形态不列入 list_urls 缺陷
            try:
                from util.url_rewrites import is_list_htm_person
                if not is_list_htm_person(url):
                    list_urls.append({"name": name, "url": url[:80]})
            except Exception:
                list_urls.append({"name": name, "url": url[:80]})
    return {"school": school, "dept": dept, "total": len(ts),
            "junk_names": junk_names, "bad_urls": bad_urls,
            "list_urls": list_urls}


def main():
    ap = argparse.ArgumentParser(description="名单质量检测")
    ap.add_argument("--school")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--filter", action="store_true",
                    help="产出过滤后名单(坏URL置空/垃圾名移除), 写到同目录 "
                         "teachers_filtered.json, 不覆盖原文件")
    args = ap.parse_args()

    schools = []
    if args.all:
        schools = sorted(p.name for p in _ROSTER.iterdir() if p.is_dir())
    elif args.school:
        schools = [args.school]
    else:
        ap.print_help()
        sys.exit(1)

    rows, tot = [], {"junk": 0, "bad_url": 0, "list_url": 0, "total": 0}
    for school in schools:
        sroot = _ROSTER / school
        if not sroot.is_dir():
            continue
        for dp in sorted(sroot.glob("*/teachers.json")):
            r = qc_dept(school, dp.parent.name)
            if "error" in r:
                continue
            tot["total"] += r["total"]
            tot["junk"] += len(r["junk_names"])
            tot["bad_url"] += len(r["bad_urls"])
            tot["list_url"] += len(r["list_urls"])
            for kind, key in (("junk", "junk_names"), ("bad_url", "bad_urls"),
                              ("list_url", "list_urls")):
                for x in r[key]:
                    rows.append((school, dp.parent.name, kind, x["name"],
                                 x["url"]))
            # 2026-10-16 用户指令「提取到的url应该在结构性检查」:
            # --filter 修复模式 — 结构校验落地: 非 http(s) 的 detail_url
            # 置空(字段错位/非链接), 垃圾名条目移除, 产出过滤后名单
            if args.filter:
                try:
                    data = json.loads(dp.read_text(encoding="utf-8"))
                    ts = data if isinstance(data, list) else data.get("teachers", [])
                    kept = []
                    for t in ts:
                        if not isinstance(t, dict):
                            continue
                        name = str(t.get("name") or t.get("姓名") or "")
                        url = str(t.get("detail_url") or "")
                        if _is_junk_name(name):
                            continue
                        if url and not url.startswith(("http://", "https://")):
                            t["detail_url"] = ""
                        kept.append(t)
                    if isinstance(data, dict):
                        data["teachers"] = kept
                        data["total"] = len(kept)
                    else:
                        data = kept
                    out_j = dp.parent / "teachers_filtered.json"
                    out_j.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
                except Exception:
                    pass

    out = _BASE / "logs" / "roster_qc_report.tsv"
    with open(out, "w", encoding="utf-8") as f:
        f.write("学校\t院系\t类型\t姓名\tURL\n")
        for row in rows:
            f.write("\t".join(row) + "\n")

    print(f"📊 名单 QC: {len(schools)} 校, 总 {tot['total']} 人")
    print(f"   导航/栏目垃圾名: {tot['junk']}")
    print(f"   URL 非 http（字段错位/缺失）: {tot['bad_url']}")
    print(f"   URL 指向列表/栏目页: {tot['list_url']}")
    print(f"   报告: {out}")
    if tot["junk"]:
        print("\n垃圾名样本:")
        for r in [x for x in rows if x[2] == "junk"][:10]:
            print(f"   {r[0]}/{r[1]} {r[3]!r} → {r[4][:50]}")


if __name__ == "__main__":
    main()
