#!/usr/bin/env python3
"""LLM 规则生成器（2026-08-31）——让 LLM 分析原始 HTML/PDF, 生成院系提取规则。

闭环: generate_rules（LLM 分析样本出规则 JSON）→ validate_rules（静态校验）
→ replay_validate（本地回放 _apply_dept_rules 与参考值对比）→ iterative_refine
（失败字段带原文反馈重设计）→ 用户确认 → write_dept_rules（落盘 <校>/<院>/extract_rules.json,
采集侧懒加载即生效）。

用法:
  uv run python dept_rule_generator.py --school 北京大学 --dept 考古文博学院 \
      --sample 5 --max-iters 4 --out /tmp/draft.json   # 生成+校验+回放, 不落盘
  uv run python dept_rule_generator.py --school 北京大学 --dept 考古文博学院 \
      --sample 5 --write                                 # 通过校验后直接落盘
"""
import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_COLLEGE_ROOT = _BASE.parent
_RAW_DATA_ROOT = _COLLEGE_ROOT / "tearchers raw data"
sys.path.insert(0, str(_BASE))

from detail_scraper import (DetailScraper, DETAIL_FIELDS,  # noqa: E402
                            _apply_dept_rules, _load_dept_rules_json,
                            _DEPT_EXTRACT_RULES, _DEPT_RULES_JSON_CACHE,
                            _strip_empty_section_tags, _NAV_NOISE_WORDS,
                            _is_junk_name)

# DETAIL_FIELDS 含 attr(dict 兜底)——规则生成只面向 13 个字符串字段
_RULE_FIELDS = [f for f in DETAIL_FIELDS if f != "attr"]

# 截段类字段: 无前瞻终止符/长度上限的正则 = 整页倾倒风险
_CUT_FIELDS = frozenset({"教育经历", "工作经历", "学术成果", "基金", "荣誉",
                         "研究方向", "讲授课程"})
# 灾难回溯机械检测（2026-08-31 崔建莲页 100% CPU 实锤）: 组重复
# ){N,} 且组内含可空匹配的无界量词（* + {0,）→ 指数级回溯风险
_BACKTRACK_BOMB_RE = re.compile(
    r"\{(\d+|\d*,)\}[)}]?")  # 组重复形态 {1,8} 等


def _has_backtrack_bomb(pat: str) -> bool:
    """嵌套无界量词检测: (?:…*…){N} 或 (?:…{0,M}…){N} 形态。"""
    for m in re.finditer(r"\)\{(\d+|\d+,)\}", pat):
        # 找匹配的组起点（简化: 向前扫描到最近的未闭合 (?:
        i = m.start()
        depth = 0
        start = -1
        for j in range(i, -1, -1):
            if pat[j:j + 3] == "(?:":
                if depth == 0:
                    start = j
                    break
                depth -= 1
            elif pat[j] == ")":
                depth += 1
        if start < 0:
            continue
        inner = pat[start:i]
        if re.search(r"\*|\+|\{0,", inner):
            return True
    return False
_CUT_LOOKAHEAD_RE = re.compile(r"\(\?=|\{\d+,\d*\}|\[^\\s\"]\{")
_HEAD_FIELDS = frozenset({"职称", "出生年月"})
_BIRTH_MARK_RE = re.compile(r"生|出生")
# 年份约束: 显式世纪前缀(19/20xx) 或 \d{4}年（出生标记+年单位已足够约束,
# 2026-08-31 误杀修复: 生于…(\d{4}\s*年…) 被判无年份约束, 马院/理学院/
# 考古文博 3 系反复校验失败）
_YEAR_RE = re.compile(
    r"\(\?:19\|20\)\\d\{2}|19\\d\{2}|20\\d\{2}|\[12\]\\d\{3\}|\\d\{4\}\\s\*年|\\d\{4\}\)\\s\*年")


def prefilter_text(text: str) -> str:
    """LLM 分析前的简单过滤整理（2026-08-31）。

    只作用于规则生成的样本视图——真实提取管线文本不改（提取侧过滤
    会丢真实内容）。规则最终在真实文本上回放验证, 若 LLM 因过滤视图
    设计出不兼容规则, replay 会抓出并带真实原文反馈重设计。

    1. 行尾去空白
    2. 导航菜单行剔除: 整行 ≥3 token 且 ≥70% 为导航词（复用
       _NAV_NOISE_WORDS, 同 _is_nav_noise_value 长链口径）
    3. 重复行去重（页脚/模板锅炉文本只留第一次）

    刻意不折叠空行——正则常锚定 \\n 行结构, 折叠会让视图与真实文本
    失配（清华中文系/万科首轮 11 字段零命中的教训）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            out.append("")
            continue
        tokens = [t for t in re.split(r"[\s,，;；、/|]+", ln) if t]
        if (len(tokens) >= 3 and len(ln) <= 120
                and sum(1 for t in tokens if t in _NAV_NOISE_WORDS) / len(tokens) >= 0.7):
            continue  # 整行是导航菜单链
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out).strip()


# 采样器专用栏目词过滤（不扩全局 _NAV_NOISE_WORDS——词表扩展误杀过真名）:
# raw 文件名首段是栏目/板块词 → 栏目页文件, 不是教师详情页
_SAMPLE_CAT_WORDS = frozenset({
    "教育教学", "研究成果", "学术活动", "学术成果", "科学研究", "人才培养",
    "师资队伍", "学院概况", "机构设置", "通知公告", "新闻动态", "招生信息",
    "教学科研", "研究团队", "学术交流", "师资力量", "导师队伍", "师资介绍",
    "教师名录", "教师队伍", "教授风采", "学子风采", "校友风采", "校园新闻",
    "党建动态", "学生工作", "工会活动",
})
_SAMPLE_CAT_SUFFIX_RE = re.compile(
    r".*(中心|委员会|办公室|实验室|研究院|研究所|学院|学部|书院|基地)$")


def _sample_ok(name: str) -> bool:
    """样本文件名首段是否为真实人名——排除导航页/栏目页文件
    （上游名单污染: 万科公卫「教育教学/研究成果/学术活动」等）。"""
    if not name or _is_junk_name(name):
        return False
    if name in _SAMPLE_CAT_WORDS or _SAMPLE_CAT_SUFFIX_RE.match(name):
        return False
    return True


def sample_texts(school: str, dept: str, n: int = 5, seed: int = 0) -> list[dict]:
    """从本地 raw 随机抽样本（HTML+PDF 混合）。

    返回 [{name, kind, text, raw}]:
      - text: 经 prefilter_text 整理的样本视图（给 LLM 分析, 干净聚焦）
      - raw:  真实管线文本（回放验证用, 保证规则在真实文本上兼容）
    随机: 洗牌后抽取（--seed 固定可复现）; PDF 优先纳入; 文件名首段
    是垃圾词（导航页文件）的跳过。
    """
    raw_dir = _RAW_DATA_ROOT / school / dept
    scraper = DetailScraper(school, dept, str(_RAW_DATA_ROOT), fetch_only=True)
    htmls = sorted(raw_dir.glob("*.html")) if raw_dir.is_dir() else []
    pdfs = sorted(raw_dir.glob("*.pdf")) if raw_dir.is_dir() else []
    rng = random.Random(seed)
    rng.shuffle(htmls)
    rng.shuffle(pdfs)
    out: list[dict] = []
    # PDF 优先纳入（规则要覆盖 PDF 文本形态）
    for p in pdfs[:max(1, n // 2)]:
        name = p.stem.split("_")[0]
        if not _sample_ok(name):
            continue
        try:
            txt = scraper.extract_text_from_pdf(p)
        except Exception:
            continue
        if txt:
            out.append({"name": name, "kind": "pdf",
                        "text": prefilter_text(txt[:6000]), "raw": txt[:6000]})
    for p in htmls:
        if len(out) >= n:
            break
        name = p.stem.split("_")[0]
        if not _sample_ok(name):
            continue
        try:
            html = p.read_text(encoding="utf-8", errors="ignore")
            txt = _strip_empty_section_tags(scraper.extract_text_from_html(html))
        except Exception:
            continue
        if txt and len(txt) > 100:
            out.append({"name": name, "kind": "html",
                        "text": prefilter_text(txt[:6000]), "raw": txt[:6000],
                        "html_path": str(p)})
    return out[:n]


def _render_field_spec() -> str:
    from detail_scraper import render_field_spec_prompt
    return render_field_spec_prompt()


def _fewshot() -> str:
    """取一份现有院系目录 extract_rules.json 作 few-shot 示例。"""
    files = sorted(_BASE.glob("*/*/extract_rules.json"))
    if not files:
        return "（无示例）"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    return json.dumps(data.get("rules", {}), ensure_ascii=False, indent=1)[:3000]


def _html_skeleton(html: str, max_lines: int = 40) -> str:
    """HTML 结构骨架（2026-08-31, CSS 规则设计用）——列出含字段标签或
    有实质文本的节点: 标签.class#id[属性] + 文本预览, 供 LLM 设计 CSS 选择器。
    只走 body 后代, 跳过 script/style。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body") if soup.find("body") else soup
    lines: list[str] = []
    for tag in body.find_all(True):
        if tag.name in ("script", "style", "noscript"):
            continue
        cls = ".".join(tag.get("class") or [])
        iid = f"#{tag.get('id')}" if tag.get("id") else ""
        sel = f"{tag.name}{'.' if cls else ''}{cls}{iid}"
        t = (tag.get_text(" ", strip=True) or "")[:60]
        if not t:
            continue
        hit = any(lbl in t for spec in FIELD_SPEC for lbl in spec["labels"])
        if not hit and len(t) < 12:
            continue
        lines.append(f"{sel} | {t}")
        if len(lines) >= max_lines:
            break
    # meta 属性容器（SEO 壳形态）
    for m in soup.find_all("meta")[:4]:
        nm = m.get("name") or ""
        content = (m.get("content") or "")[:60]
        if nm or content:
            lines.append(f"meta[name={nm}] content | {content}")
    return "\n".join(lines)


def _build_prompt(school: str, dept: str, samples: list[dict],
                  feedback: dict | None = None,
                  frozen_rules: dict | None = None,
                  validate_errors: list[str] | None = None) -> tuple[str, str]:
    blocks = []
    for s in samples:
        blk = f"### 样本 {s['name']}（{s['kind']}）\n文本:\n{s['text'][:2600]}"
        if s.get("html_path"):
            try:
                html = Path(s["html_path"]).read_text(encoding="utf-8",
                                                      errors="ignore")
                blk += f"\n结构骨架（CSS 选择器设计用）:\n{_html_skeleton(html)}"
            except Exception:
                pass
        blocks.append(blk)
    sample_block = "\n\n".join(blocks)
    if validate_errors:
        ve = "\n".join(f"- {e}" for e in validate_errors[:8])
        task = ("上一轮规则静态校验失败。修复以下错误后重新输出【完整】规则集"
                f"（不是只修复出错字段——输出格式不变, 包含全部字段）:\n{ve}")
    elif feedback:
        fb = "\n".join(
            f"- 字段[{f}] 失败: 规则提取值={v.get('got', '')!r} 期望={v.get('want', '')!r}\n"
            f"  原文窗口: {v.get('ctx', '')[:300]}" for f, v in feedback.items())
        task = (f"上一轮规则在这些字段上失败, 只重新设计以下字段的规则"
                f"（其余字段规则已冻结, 保持原样）:\n{fb}")
    else:
        task = "设计该院系的全字段提取规则。"
    frozen_note = ""
    if frozen_rules:
        frozen_note = ("\n已冻结字段（不要修改）:\n"
                       + json.dumps(frozen_rules, ensure_ascii=False, indent=1))
    system = (
        "你是高校教师详情页提取规则设计专家。根据页面样本设计 Python 正则规则。\n"
        "只输出一个合法 JSON 对象（禁止 markdown 代码块、禁止注释）。\n"
        "输出格式:\n"
        '{"rules": {"职称": ["正则1", {"css": "选择器2", "regex": "内正则"}], ...},'
        ' "templates": "模板形态描述", "notes": "注意事项"}\n'
        "硬约束:\n"
        "1. rules 的键必须是以下字段之一: " + "、".join(_RULE_FIELDS) + "\n"
        "2. 正则必须是 Python re 兼容字符串（JSON 里 \\\\d \\\\s 等照常写）, "
        "禁止灾难回溯（嵌套无界量词）, 禁止可匹配空串的模式\n"
        "3. 规则两种形态: ①字符串=正则（作用于文本视图, PDF 与散文页面唯一选择）; "
        "②{\"css\": 选择器, \"regex\": 可选, \"attr\": 可选}=CSS 定位 DOM 元素——"
        "HTML 页面优先考虑, 能精确区分导航同名文本与正文; 元素文本即值时 "
        "regex 可省; 值在属性里时用 attr（如 meta[name=description] 配 "
        "attr=content）。CSS 选择器必须严格: 必须含 class/id/属性锚点"
        "（禁裸标签选择器如 div/p/td）, 严禁选择器含 nav/menu/footer/"
        "copyright 等导航/页脚词——页脚电话地址是学院级的, 不是教师本人\n"
        "4. 职称/出生年月 的字符串正则必须在页面【前 2000 字】内命中"
        "（应用层只搜页首 2000 字）; CSS 规则自带元素定位不受此限\n"
        "5. 出生年月 必须带出生标记（日…生/月生/年生/出生）, "
        "禁止裸日期（页面里出版/新闻日期会被误抓成假生日）\n"
        "6. 讲授课程 优先用整段匹配（连续《》书目链）, 命中后应用层会拆《》条目\n"
        "7. 教育经历/工作经历/学术成果/基金/荣誉 等长段字段必须带截断: "
        "前瞻终止符 (?=下一节标签|联系方式|Email|版权所有) 或长度上限 {m,n}, "
        "禁止贪婪整页倾倒\n"
        "8. 每个字段给 1-3 条规则, 按 CSS 精确定位→页首模板→散文→兜底顺序排列; "
        "模板确实不存在该字段信息时该字段给空列表\n"
        "9. attr 是内部兜底字段（JSON 兜底容器, 不是页面字段）, "
        "严禁出现在 rules 中\n"
    )
    user = (
        f"学校: {school} | 院系: {dept}\n\n"
        f"## 字段定义\n{_render_field_spec()}\n\n"
        f"## 页面样本\n{sample_block}\n\n"
        f"## 任务\n{task}{frozen_note}\n\n"
        f"## few-shot（可接受的规则形态示例）\n{_fewshot()}\n"
    )
    return system, user


def _call_llm(system: str, user: str) -> str:
    sys.path.insert(0, str(_COLLEGE_ROOT / "util"))
    from llm_client import SimpleLLM
    llm = SimpleLLM()
    if not getattr(llm, "available", False):
        raise RuntimeError("LLM 不可用")
    # 网关波动期（35-50min 周期）: ask 超时/降级 → 短退避重试 3 次。
    # ask 内部把异常吞成 {"error":..., "degraded":true} JSON——识别并重试,
    # 不把降级 JSON 当有效输出烧掉整轮迭代。
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            text = llm.ask(system, user, temperature=0.2)
        except Exception as e:
            last_err = e
        else:
            if text and text.strip():
                try:
                    j = json.loads(text)
                except Exception:
                    return text  # 非 JSON 说明是真实模型输出（后面 _parse_json 兜底）
                if not j.get("degraded"):
                    return text
                last_err = RuntimeError(str(j.get("error", ""))[:80])
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM 3 次调用失败: {last_err}")


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    return json.loads(text)


def generate_rules(school: str, dept: str, samples: list[dict],
                   feedback: dict | None = None,
                   frozen_rules: dict | None = None,
                   validate_errors: list[str] | None = None) -> dict:
    system, user = _build_prompt(school, dept, samples, feedback,
                                 frozen_rules, validate_errors)
    text = _call_llm(system, user)
    return _parse_json(text)


def validate_rules(rules: dict) -> dict:
    """静态校验 → {ok, errors[], warnings[]}。"""
    errors, warnings = [], []
    non_empty = {f: p for f, p in (rules or {}).items() if p}
    if not non_empty:
        errors.append("空规则集（LLM 输出为空/超时降级）——必须至少一个字段有规则")
    for f, pats in rules.items():
        if f == "attr":
            warnings.append("attr 是内部兜底字段, 自动忽略（LLM 常误加）")
            continue
        if f not in _RULE_FIELDS:
            errors.append(f"非法字段 {f}")
            continue
        if not isinstance(pats, list):
            errors.append(f"字段 {f} 的规则必须是列表")
            continue
        for pat in pats:
            if isinstance(pat, dict):
                # CSS 规则形态: css 必填; regex 可选但须可编译
                if not pat.get("css"):
                    errors.append(f"{f}: CSS 规则缺 css 选择器: {pat}")
                    continue
                css = pat["css"]
                # 用户要求严格（2026-08-31）: ①禁裸标签选择器（必须含
                # class/id/属性锚点）——过宽必错位; ②禁指向导航/页脚
                # （nav/menu/footer/copyright 等词, 学院电话污染来源）
                if not re.search(r"[.#\[]", css):
                    errors.append(f"{f}: CSS 选择器过宽(裸标签, 须含 class/id/属性锚点): {css[:60]}")
                if re.search(r"(?:^|[.\s#>+~,])(?:footer|nav|menu|topbar|toolbar|sidebar|breadcrumb|crumb|copyright|copy|banquan)(?:[.\s#\[>:+~,]|$)", css, re.I):
                    errors.append(f"{f}: CSS 选择器指向导航/页脚: {css[:60]}")
                rp = pat.get("regex")
                if rp:
                    try:
                        re.compile(rp)
                    except re.error as e:
                        errors.append(f"{f}: CSS 内正则编译失败 {e}: {rp[:60]}")
                continue
            try:
                c = re.compile(pat)
            except re.error as e:
                errors.append(f"{f}: 编译失败 {e}: {pat[:60]}")
                continue
            if _has_backtrack_bomb(pat):
                errors.append(f"{f}: 疑似灾难回溯(组重复内含无界量词): {pat[:60]}")
            try:
                if c.search(""):
                    errors.append(f"{f}: 规则可匹配空串: {pat[:60]}")
            except Exception:
                pass
            if f in _CUT_FIELDS and isinstance(pat, str) and not _CUT_LOOKAHEAD_RE.search(pat):
                warnings.append(f"{f}: 无截断（前瞻/长度上限）, 有整页倾倒风险: {pat[:60]}")
        if f == "出生年月":
            for pat in pats:
                if not isinstance(pat, str):
                    continue  # CSS 规则自带元素定位
                if not _BIRTH_MARK_RE.search(pat):
                    errors.append(f"出生年月: 无出生标记（禁裸日期）: {pat[:60]}")
                if not _YEAR_RE.search(pat):
                    errors.append(f"出生年月: 无年份约束: {pat[:60]}")
        if f == "职称":
            if not any(isinstance(p, str) and re.search(r"教授|讲师|研究员|助教|工程师|院士", p) for p in pats):
                warnings.append("职称: 无职称词表锚定（CSS 规则请在内正则中体现）")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _reference_values(school: str, dept: str, name: str) -> dict:
    """参考值: 当前 teachers_detail.json 中该教师的最佳已提取字段。"""
    jp = _BASE / school / dept / "teachers_detail.json"
    if not jp.exists():
        return {}
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
        ts = data.get("teachers", []) if isinstance(data, dict) else data
        for t in ts:
            if t.get("姓名") == name:
                return {f: (t.get(f) or "")
                        for f in _RULE_FIELDS if not isinstance(t.get(f), dict)}
    except Exception:
        pass
    return {}


def _norm(v: str) -> str:
    return re.sub(r"\s+", "", v or "").rstrip("。；;：:")


def replay_validate(school: str, dept: str, samples: list[dict],
                    rules: dict) -> dict:
    """本地回放 _apply_dept_rules 对比参考值 → 逐字段报告。"""
    # 走真实函数: 写临时 JSON 供懒加载（硬编码表无该院系时）
    if (school, dept) in _DEPT_EXTRACT_RULES:
        return {"ok": False, "errors": [f"{school}/{dept} 已在硬编码表, 拒绝覆盖"]}
    report = {"ok": True, "per_field": {}, "errors": []}
    for f, pats in rules.items():
        hit = agree = false_hit = unverified = misses = 0
        fails = []
        for s in samples:
            # 回放用真实管线文本（raw）——规则在过滤视图上设计,
            # 必须在真实文本上兼容; 不兼容在此被抓出
            real_text = s.get("raw") or s["text"]
            result = {x: "" for x in _RULE_FIELDS}
            # 仅注入当前字段规则回放, 避免跨字段相互影响
            _apply_dept_rules_single(real_text, f, pats, result,
                                     s.get("html_path") or "")
            got = result.get(f) or ""
            ref = _reference_values(school, dept, s["name"])
            want = ref.get(f) or ""
            if got:
                hit += 1
                if not ref:
                    # 无提取基线（新院系引导期）: 无法对比, 只验证结构命中
                    unverified += 1
                elif _norm(got) == _norm(want) and want:
                    agree += 1
                elif not want:
                    false_hit += 1
                else:
                    fails.append({"name": s["name"], "got": got[:200],
                                  "want": want[:200],
                                  "ctx": _ctx_window(real_text, got[:30])})
            elif want:
                # 参考值存在但规则未命中 = 漏提取（此前完全不可见）
                misses += 1
                fails.append({"name": s["name"], "got": "", "want": want[:200],
                              "ctx": _ctx_window(real_text, want[:10])})
        rep = {"hit": hit, "samples": len(samples),
               "agree": agree, "false_hit": false_hit,
               "unverified": unverified, "misses": misses,
               "absent": False, "fails": fails}
        report["per_field"][f] = rep
        if pats and hit == 0:
            # 零命中规则是死规则（运行时永不命中, 不覆盖存量值, 无害）——
            # 不阻塞; 标记 zero_hit 供反馈一轮, 循环结束仍零命中则清空。
            # 参考值存在 = 字段在页面存在但规则没对上（值得反馈重试）。
            rep["zero_hit"] = any(
                (_reference_values(school, dept, s["name"]).get(f) or "")
                for s in samples)
    report["ok"] = not report["errors"]
    return report


def _apply_dept_rules_single(text: str, field: str, pats: list,
                             result: dict, html_path: str = "") -> None:
    """与 _apply_dept_rules 同语义的单字段回放（head 窗口/课程拆条/覆盖/
    CSS 规则）。"""
    from detail_scraper import _apply_rule_value
    head = text[:2000]
    for pat in pats:
        scope = head if field in _HEAD_FIELDS else text
        val = _apply_rule_value(pat, scope, html_path, field)
        if not val:
            continue
        val = val.strip()
        if field == "讲授课程" and "《" in val:
            items = re.findall(r"《([^》]{2,25})》", val)
            if items:
                val = "；".join(dict.fromkeys(items))
        if val:
            result[field] = val
            break


def _ctx_window(text: str, needle: str, width: int = 120) -> str:
    i = text.find(needle[:10]) if needle else -1
    if i < 0:
        return text[:width]
    return text[max(0, i - width // 2):i + width]


def _clear_absent(draft: dict, rp: dict) -> None:
    """零命中且页面无此字段信息（无参考值）的规则清空——零命中规则运行时
    永不命中, 留着无益（清华中文系无联系方式/课堂项目教训）。"""
    for f, v in rp.get("per_field", {}).items():
        if v["hit"] == 0 and not v.get("zero_hit") and draft["rules"].get(f):
            draft["rules"][f] = []


def iterative_refine(school: str, dept: str, samples: list[dict],
                     max_iters: int = 4) -> dict:
    """生成 → 校验 → 回放 → 失败字段反馈重设计, 循环至达标/上限。"""
    frozen: dict = {}
    draft = None
    validate_errors: list[str] | None = None
    for it in range(1, max_iters + 1):
        feedback = None
        if draft is not None:
            rp = replay_validate(school, dept, samples, draft["rules"])
            _clear_absent(draft, rp)
            if rp["ok"]:
                # 假命中/不一致只作警告（参考值是旧提取而非真值, 规则可能
                # 是在补旧提取的漏——严格阻塞会误杀改进）, 最终由提取
                # 结果 + 人工确认把关
                print(f"  ✅ 第 {it - 1} 轮回放通过"
                      f"（假命中 {sum(v['false_hit'] for v in rp['per_field'].values())} 待人工复核）")
                return {**draft, "validation": rp, "iters": it - 1}
            # 失败字段收集反馈
            feedback = {}
            for f, v in rp["per_field"].items():
                if v["fails"] or (draft["rules"].get(f) and v["hit"] == 0):
                    fb = v["fails"][0] if v["fails"] else {
                        "name": "全样本", "got": "", "want": "",
                        # 零命中: 反馈样本真实文本头部, 让 LLM 看清单字段
                        # 实际行结构重新设计（首轮 11 字段零命中教训）
                        "ctx": (samples[0].get("raw") or samples[0]["text"])[:200]}
                    feedback[f] = fb
            # 通过字段冻结
            for f, v in rp["per_field"].items():
                if f not in feedback and draft["rules"].get(f):
                    frozen[f] = draft["rules"][f]
            print(f"  🔁 第 {it} 轮: 重设计 {list(feedback)}")
        try:
            raw = generate_rules(school, dept, samples, feedback=feedback,
                                 frozen_rules=frozen or None,
                                 validate_errors=validate_errors)
        except Exception as e:
            # 网关波动: _call_llm 3 次重试后仍失败 → 本轮放弃, 由外层
            # 重试/批量调度补（不让单系异常拖死批量循环）
            print(f"  ⚠️ 第 {it} 轮 LLM 调用失败: {type(e).__name__} {str(e)[:80]}")
            draft = None
            continue
        rules = raw.get("rules", {})
        rules.pop("attr", None)  # 内部兜底字段, LLM 常误加 → 剥除不校验
        if frozen:
            rules = {**frozen, **rules}  # 冻结字段保持
        draft = {"rules": rules, "templates": raw.get("templates", ""),
                 "notes": raw.get("notes", ""), "school": school, "dept": dept}
        v = validate_rules(draft["rules"])
        if not v["ok"] and any("空规则集" in e for e in v["errors"]):
            draft = None
            print("  ⚠️ LLM 返回空规则集（超时降级）, 重试")
            continue
        print(f"  校验: {'通过' if v['ok'] else '失败'}")
        for e in v["errors"]:
            print(f"    ❌ {e}")
        for w in v["warnings"]:
            print(f"    ⚠️ {w}")
        if not v["ok"]:
            # 静态校验失败: 保留 draft 供回放参考, 校验错误反馈 LLM 修复
            # （此前直接丢 draft 从头生成, LLM 盲试——会计系 15 分钟失败教训）
            validate_errors = v["errors"]
            draft = None
            continue
        validate_errors = None
        rp = replay_validate(school, dept, samples, draft["rules"])
        _clear_absent(draft, rp)
        # 循环结束仍零命中的字段清空（死规则不留）
        for f, x in rp["per_field"].items():
            if x["hit"] == 0 and draft["rules"].get(f):
                draft["rules"][f] = []
        ok = rp["ok"]
        print(f"  回放: {'通过' if ok else '需迭代'}")
        for f, x in rp["per_field"].items():
            print(f"    [{f}] 命中 {x['hit']}/{x['samples']} "
                  f"一致 {x['agree']} 假命中 {x['false_hit']} "
                  f"未验证 {x.get('unverified', 0)}")
        if ok:
            return {**draft, "validation": rp, "iters": it}
    rp = replay_validate(school, dept, samples, draft["rules"]) if draft else {}
    return {**(draft or {}), "validation": rp, "iters": max_iters}


def write_dept_rules(school: str, dept: str, payload: dict) -> Path:
    """写盘院系目录（2026-09-07 用户指令统一命名）: 唯一权威源为
    <校>/<院>/extract_rules.json, 返回该路径。"""
    out = _BASE / school / dept / "extract_rules.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {"school": school, "dept": dept,
            "templates": payload.get("templates", ""),
            "rules": payload.get("rules", {}),
            "offline_test": {},
            "notes": payload.get("notes", "")}
    text = json.dumps(body, ensure_ascii=False, indent=1)
    out.write_text(text, encoding="utf-8")
    # 清缓存让懒加载立即读到新 JSON
    _DEPT_RULES_JSON_CACHE.pop((school, dept), None)
    return out


def sync_rule_mirrors() -> int:
    """回填硬编码规则院系镜像（2026-09-07 统一后）: _DEPT_EXTRACT_RULES
    硬编码规则 → <校>/<院>/extract_rules.json（仅缺失时写, 不覆盖）。"""
    n = 0
    # 硬编码规则院系（北大历史/哲学/中文等）
    for (school, dept), rules in _DEPT_EXTRACT_RULES.items():
        mirror = _BASE / school / dept / "extract_rules.json"
        if mirror.exists():
            continue
        mirror.parent.mkdir(parents=True, exist_ok=True)
        body = {"school": school, "dept": dept, "templates": "硬编码主表规则",
                "rules": rules, "offline_test": {},
                "notes": "来源: detail_scraper._DEPT_EXTRACT_RULES 硬编码表"}
        mirror.write_text(json.dumps(body, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="LLM 院系提取规则生成")
    ap.add_argument("--school", required=True)
    ap.add_argument("--dept", required=True)
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0,
                    help="样本随机种子（0=每次不同）")
    ap.add_argument("--max-iters", type=int, default=4)
    ap.add_argument("--out", help="draft 输出路径（默认打印, 不落盘）")
    ap.add_argument("--write", action="store_true",
                    help="通过校验+回放后直接写 <校>/<院>/extract_rules.json")
    ap.add_argument("--sync-mirrors", action="store_true",
                    help="回填硬编码规则院系的 extract_rules.json（仅缺失时写）")
    args = ap.parse_args()

    if args.sync_mirrors:
        n = sync_rule_mirrors()
        print(f"✅ 镜像回填完成: {n} 个院系")
        sys.exit(0)

    if (args.school, args.dept) in _DEPT_EXTRACT_RULES:
        print(f"❌ {args.school}/{args.dept} 已有硬编码规则, 拒绝覆盖")
        sys.exit(1)
    if _load_dept_rules_json(args.school, args.dept):
        print(f"⚠️ {args.school}/{args.dept} 已有 extract_rules.json, 将覆盖")

    samples = sample_texts(args.school, args.dept, args.sample, args.seed)
    if not samples:
        print("❌ 无本地 raw 样本（先跑增量采集下载）")
        sys.exit(1)
    print(f"📄 样本 {len(samples)} 个: "
          + ", ".join(f"{s['name']}({s['kind']})" for s in samples))

    final = iterative_refine(args.school, args.dept, samples, args.max_iters)
    payload = {"rules": final.get("rules", {}),
               "templates": final.get("templates", ""),
               "notes": final.get("notes", "")}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"draft → {args.out}")
    else:
        print("\n===== 规则草案 =====")
        print(text)
    if args.write and final.get("validation", {}).get("ok"):
        p = write_dept_rules(args.school, args.dept, final)
        print(f"✅ 已落盘: {p}")
    else:
        print("\n⚠️ 未落盘——人工确认 draft 提取结果正确后加 --write 落盘")


if __name__ == "__main__":
    main()
