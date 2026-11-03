#!/usr/bin/env python3
"""采集主流程 Phase 1-5 + collect_dept + CLI — 2026-08-31 从 collect_teacher 拆出。

Phase 1 找师资页 / Phase 2 LLM分析生成 spider.py / Phase 3 运行验证 / Phase 3.5
LLM 评估 teachers.csv / Phase 4 带原因修正 / Phase 5 输出; collect_dept 为单院系
主循环(P0 组织类型感知)。通用能力在 util/(fetch_chain/llm_gate) 与
collect_*(constants/kb/csv)。
"""
import argparse, csv, json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from _paths import OUT  # 先导: sys.path 引导 + .env 加载
from collect_constants import FACULTY_KW, COLLEGE_SCHOOLS
from collect_kb import load_kb, save_kb, record_case, is_no_teacher_org, load_org_keywords
from collect_csv import sanitize_teachers_csv, _is_detail_url, _clean_name
from util.fetch_chain import fetch
from util.llm_gate import get_llm, llm_ask_sem, llm_ask_with_timeout, parse_json

BASE = OUT  # 兼容旧名: 本模块 BASE 指 output 目录(与 fix_loop.BASE=项目根 语义不同)
# 2026-09-04 南科大 letter/ 实锤: 导航页(校级名册/联系方式)绝不能当选师资页,
# 与 fix_loop.py:57 / script/probe_site.py NAV_PAGE_RE 同口径(段级匹配防误伤 /newsletter)。
NAV_PAGE_RE = re.compile(
    r"/(letter|mailbox|contact|feedback|guestbook|suggest|liuyan|xinfang|map)"
    r"(/|[._-]|$)"
    r"|/zh/facult(y|ies)/?$", re.I)


def _ok_faculty_url(u):
    return bool(u) and not NAV_PAGE_RE.search(u)


# ── Phase 1 ──
def find_faculty_page(dept_url, html=None):
    """从院系首页找师资页URL。可传入预取的HTML避免重复请求。

    2026-09-18 委托 util/spider.py 统一实现 — 全部发现链增强(词变体长词
    优先/后缀兜底/JS菜单URL探测/HTML注释导航/入口选择页跟随 等)都在那里;
    本模块旧版(仅精确词+模糊词)对 重大土木 语言选择页、温影 UA 注释导航、
    JS 菜单族等形态全部失明, 双实现分叉是历史 bug 温床。委托后单一真相源。"""
    try:
        from util.spider import find_faculty_page as _fpf
        if html is None:
            html = fetch(dept_url)
        if html:
            return _fpf(dept_url, html)
    except Exception:
        pass
    return None

# ── Phase 2: LLM分析 → 生成spider.py ──
def phase2_generate_spider(dept_name, school_name, dept_url, faculty_url, html, retry=False):
    llm = get_llm()
    if not llm:
        print("  ❌ LLM不可用")
        return None
    print(f"  🧠 LLM: {llm.provider}/{llm.model}")

    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(["script","style","noscript"]): t.decompose()

    # 关键容器摘要
    containers = []
    for sel in ["div.content","div.main","#content","#main","article",
                ".wp_articlecontent","div.list","ul.list","table",
                "div.con",".con",'[class*="teacher"]','[class*="list"]']:
        for el in soup.select(sel)[:2]:
            t = el.get_text(strip=True)[:100]; na = len(el.find_all("a"))
            if t or na > 0:
                containers.append(f"  {sel}: class={' '.join(el.get('class',[]))[:40]} links={na} text={t[:50]}")

    # 链接样本
    links = []
    for a in soup.find_all("a", href=True)[:80]:
        t = a.get_text(strip=True); h = a.get("href","")
        if t and len(t) <= 25:
            links.append({"text":t[:30],"href":h[:100],
                          "parent":a.parent.name,
                          "pclass":" ".join(a.parent.get("class",[]))[:40] if a.parent else ""})

    body_text = soup.body.get_text()[:800] if soup.body else ""  # 800字: 服务端大prompt不稳定, 目标~3KB
    page_title = soup.title.get_text(strip=True) if soup.title else ""

    # 知识库案例注入
    kb = load_kb()
    kb_patterns = kb.get("page_structure_patterns", {})
    kb_failed = kb.get("failed_cases", {})
    kb_hint = ""
    if kb_patterns:
        kb_hint = "## 已知页面结构模式(历史案例)\n" + json.dumps(
            {k: v.get("description","")[:100] for k,v in list(kb_patterns.items())[:5]},
            ensure_ascii=False, indent=1)
    if kb_failed:
        kb_hint += "\n\n## 历史失败案例\n" + json.dumps(
            {k: v.get("reason","")[:80] for k,v in list(kb_failed.items())[:3]},
            ensure_ascii=False, indent=1)

    prompt = f"""你是爬虫专家。为下页面生成一个独立运行的 Python 采集脚本(spider.py)。

{kb_hint}


## 页面信息
- 院系: {dept_name}
- 学校: {school_name}
- 师资页: {faculty_url}
- 标题: {page_title}

## DOM结构
{chr(10).join(containers[:6])}

## 链接样本
{json.dumps(links[:8], ensure_ascii=False, separators=(",", ":"))}

## 页面文本(800字)
{body_text[:800]}

## 组织类型经验
- 继续教育学院: 通常无专职教师(教师来自其他学院兼职), 无师资板块则标记无数据
- 医院: 教师信息在挂靠学院, 医院官网通常无师资板块
- 研究院/实验室: 用"研究队伍/研究人员/PI"关键词, 不用"师资队伍"

## 详情URL硬性规则(违反则打回)
- 个人详情URL必须是教师详情页, 严禁把 list.htm/index.htm/栏目页/新闻页当详情URL
- 排除特征: URL含 list.htm|index.htm|default.htm|channel|menu; 路径含 /xwgg/ /tzgg/ /news/ /tz/ /xygk/ /zzjg/ /about/
- 页面导航栏链接(学院概况/组织机构/联系我们等)不是教师, 必须剔除
- 若列表页教师是纯文本无链接, 详情URL留空, 不要用页面URL或栏目URL填充
- 姓名只保留纯中文2-4字或外国名字母形式, 去掉(兼)/教授/研究员等后缀
- 【2026-08-30 加强】姓名在 title 属性的链接必须提取(清华AI学院形态: <a title="张钹" href=".../info/1121/3552.htm"><div>…</div></a>, 链接文本是多行混合文本): 优先取 title 属性值当姓名, 纯中文2-4字且非导航词即可, 详情URL取该链接href; 三层拼音目录路径(如 /rydw/qzjs/yaoqizhi.htm)是详情页特征
- 【2026-08-27 加强】过滤导航/栏目/新闻特征文本: 链接文本含 首页|概况|简介|组织机构|党建|工会|团委|学生|招聘|招生|就业|新闻|通知|公告|活动|动态|下载|联系|校友|捐赠|合作|交流|制度|政策|资料|服务|专题|登录|注册|更多|院系介绍|学院介绍 等词 → 不是教师, 必须剔除
- 【2026-08-27 加强】只从"师资/队伍/名册/教师"板块容器内提取姓名, 严禁遍历全页所有<a>链接当教师
- 【2026-08-27 加强】机构后缀词结尾的链接文本不是教师名, 必须剔除: 系|部|处|馆|室|站|组|所|园地|书院|培训|基地|工作室|教研室|实验室|委员会|办公室|专栏|专区|兼职|挂职|办公|实习 (如 体育系/基础部/思政部/宣教园地/中师培训/实验中心/社会兼职/院内办公)
- 【2026-08-27 加强】专业方向/学科词(动画/体育/思政/党建/设计/培训/招生/就业/园地/书院等)是栏目不是教师名; "X X"组合如 环境设计/音乐理论 一律剔除
- 【2026-08-27 加强】链接兜底: 若师资容器内提取人数 < 页面全部可点击教师级链接(2-4字中文)的30%, 必须改为全页扫描所有<a>链接提取(防CSS选择器过窄漏抓), 且提取时必须保留链接为详情URL, 不得因"非师资容器"放弃

## 生成spider.py的要求

必须包含以下函数:
1. fetch(url): HTTP请求, 用curl_cffi.requests做(import curl_cffi.requests as cr,然后用cr.get), 不要import curl_cffi的requests子模块; 网络请求尽可能走快代理(见下方示例 util.http_client._build_proxies 用法), 浏览器路径必须带并发预算(browser_budget)且用完即关
2. extract_teachers(html): 用BeautifulSoup + 写死的CSS选择器提取教师
3. main(): 调用fetch和extract, 写teachers.csv(UTF-8)和teachers.json

spider.py的数据字段:
- 大学, 学院, 教师姓名, 教师类别, 职务职称, 所属院系, 个人详情URL, 师资页URL, 采集时间
- 教师类别: 在职/兼职/退休/名誉/顾问/博士后/去世(永远怀念)。
  判断依据: URL特征(zzjs=在职, ltx/txjs/rxjs=退休, myjs=名誉, gwjs=顾问, bsh=博士后, yyhn=去世)
  或页面文本关键词(荣休教师/离退休/兼职/永远怀念等)

spider.py 代码示例要求(实现细节参考下述规则, 不提供完整示例):
- ⚠️ 代码尽量精简紧凑: 目标≤80行, 无长注释, 输出越短越快(服务端长输出易超时), 但必须功能完整
- fetch(url): curl_cffi→Playwright→requests 三层回退 + JS跳转跟随(window.location跟随); 网络走快代理(util.http_client._build_proxies); 浏览器路径用 browser_budget 并发预算, 用完即关
- extract_teachers(html): BeautifulSoup + 写死CSS选择器
- main(): 写 teachers.csv(UTF-8) 和 teachers.json

⚠️ 必须写入的规则:
1. fetch() 必须带 JS 跳转跟随: 若响应 < 2000字节且含 window.location.href='xxx', 必须跟随跳转重新抓取 (北大IC/艺术学院等CMS壳页)
2. 教师详情URL两种样式都认: /info/数字/数字.htm 和 个人目录 wangxx/index.htm
3. CSV 表头必须用中文: 大学,学院,教师姓名,职务职称,所属院系,个人详情URL,师资页URL,采集时间
4. 若师资页是目录页(教师分散在子页如 专职教师/博士后/光荣退休), 必须递归抓取每个子目录页
5. 【分页·2026-08-28】师资页有分页控件(下一页/下页/尾页/末页/数字页码链接, 含 "下一页>>"/"<<上一页" 等装饰文本)时, 必须遍历全部页: main() 里先用 util.teacher_extract.pager_pages(page_url, html) 跟随页面真实分页链接收集全部页, 无分页链接时用 list_pages(page_url) 兜底; 逐页提取合并; 连续 2 个空页或请求失败立即 break, 禁止只抓第一页
6. 【XHR师资接口·2026-08-28】师资页静态 HTML 无教师/极少数师, 但页面含 ①万鹏CMS标记 sudy-wp-siteId="数字" (名单走 {站点}/_wp3services/generalQuery?queryObj=teacherHome POST) 或 ②内嵌接口 URL(文本含 teacherHome/deptTeacherList/teacherList, 如 https://faculty.scut.edu.cn/_web/_platform/_teacherHome/api/deptTeacherList.rst GET JSONP) 时, 必须先拉接口拿全量名单再提取: 直接复用 util.teacher_extract.query_embedded_teacher_roster(page_url, html) 返回名单, 用 extract_teachers_from_api(records, page_url) 提取(字段: title=姓名, career/post/exField9=职称, cnUrl=详情URL); 接口名单为空才退回静态页提取。禁止在空壳站上反复抓静态页(复旦ai 289人/华工设计62人 静态页仅个位数)

返回JSON格式:
{{
  "page_type": "direct_list 或 sub_directory",
  "analysis": "1句话",
  "spider_code": "#!/usr/bin/env python3\\n...(完整Python)\\n..."
}}
只输出JSON。"""
    print(f"  📤 LLM请求: {len(prompt)}B prompt")
    # 带重试: DeepSeek后端偶发短响应(2048B截断), 重试最多3次
    result = None
    r = ""
    for attempt in range(3):
        r = llm_ask_sem("只输出JSON。", prompt, tag="generate")
        if r is None:
            print(f"  📥 LLM返回: None (第{attempt+1}次)")
        else:
            print(f"  📥 LLM返回: {len(r)}B (第{attempt+1}次)")
        result = parse_json(r)
        if result and len(result.get("spider_code", "")) >= 100:
            code = result.get("spider_code", "")
            # 🔒 分页硬门禁(2026-08-28): 页面有真实分页链接(下页/尾页/数字页码)但
            # spider 无分页遍历 → 打回重生成。实测 LLM 常因"≤80行精简"跳过分页,
            # 只抓第一页漏人(数量硬规则兜底但多烧一轮)。
            if html:
                try:
                    from util.teacher_extract import pager_pages as _pp
                    _pp_pages = _pp(faculty_url, html)
                except Exception:
                    _pp_pages = []
                if len(_pp_pages) > 1 and not re.search(
                        r"pager_pages|list_pages|下一页|for pg in|分页", code):
                    print(f"  🔒 分页硬门禁: 页面含{len(_pp_pages)-1}个后续分页链接, "
                          f"但spider无分页遍历, 打回重生成(第{attempt+1}次)")
                    prompt += ("\n\n⚠️ 上一版漏了分页遍历: 页面有下一页分页链接, main() "
                               "必须导入 util.teacher_extract 的 pager_pages 并跟随真实分页"
                               "链接遍历全部页(list_pages 兜底), 逐页提取合并, 禁止只抓第一页")
                    result = None
                    continue
            break
        print(f"  🔁 响应过短或解析失败, 重试...")
    if result:
        code_len = len(result.get('spider_code',''))
        print(f"  🤖 page={result.get('page_type','?')}, spider_code={code_len}B" + (" MISSING!" if code_len < 50 else " OK"))
    else:
        head = (r[:500] if r else "无响应(None)")
        print(f"  ❌ parse_json失败, LLM返回前500字: {head}")
    return result

# ── Phase 3: 运行spider.py并验证 ──
def _write_csv_from_stdout(out_dir, stdout):
    """兼容 LLM 输出形式漂移: spider.py 若向 stdout 输出 JSON 数组
    [{"name":..., "title":..., "detail_url":...}], 解析并落盘为标准 8 列
    teachers.csv。返回 True 表示已落盘（否则仍依赖 spider.py 自写 CSV 文件）。

    北大哲学系教训: 修正版 spider.py 被 LLM 重写时丢了 CSV 写入逻辑,
    只 print(json.dumps(...))——验证若只读 CSV 文件就会拿到上一轮的旧
    数据, 修正永远无法生效。stdout JSON 兜底让验证与输出形式解耦。"""
    if not stdout or not stdout.strip():
        return False
    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            return False
        rows = []
        for t in data:
            if not isinstance(t, dict) or not str(t.get("name") or "").strip():
                continue
            rows.append(["", "", str(t["name"]).strip(),
                         str(t.get("title") or "").strip(), "",
                         str(t.get("detail_url") or "").strip(), "", ""])
        if not rows:
            return False
        with open(out_dir / "teachers.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["大学", "学院", "教师姓名", "职务职称", "所属院系",
                        "个人详情URL", "师资页URL", "采集时间"])
            w.writerows(rows)
        return True
    except Exception:
        return False


def phase3_run_and_verify(out_dir, llm_result, fac_html):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    code = llm_result.get("spider_code","")
    if len(code) < 100: return {"passed":False,"score":0,"issues":["spider.py过短"],"teachers":[]}

    (out_dir/"spider.py").write_text(code, encoding="utf-8")
    print(f"  ▶️ 运行spider.py ...")
    try:
        r = subprocess.run(["python3", str(out_dir/"spider.py")], cwd=str(out_dir),
                           capture_output=True, text=True, timeout=60)
        if r.stdout:
            for l in r.stdout.strip().split('\n')[-3:]: print(f"     {l}")
    except subprocess.TimeoutExpired:
        return {"passed":False,"score":0,"issues":["超时"],"teachers":[]}
    except Exception as e:
        return {"passed":False,"score":0,"issues":[str(e)],"teachers":[]}

    # 输出形式解耦: stdout 为 JSON 数组时直接落盘, 否则依赖 spider 自写 CSV。
    # （北大哲学系教训: LLM 修正可能丢掉 CSV 写入, 只验证旧文件会让修正空转）
    _write_csv_from_stdout(out_dir, r.stdout)

    # spider 输出可能是非标准表头(2列/英文), 立即净化归一化为标准8列。
    # 学校/学院传 None → 从目录名推断(避免"学校"占位符污染数据)
    sanitize_teachers_csv(out_dir, None, None)

    # 读CSV
    csvf = out_dir/"teachers.csv"
    if not csvf.exists() or csvf.stat().st_size < 50:
        return {"passed":False,"score":0,"issues":["未生成有效CSV"],"teachers":[]}

    rows = list(csv.reader(open(csvf,encoding="utf-8")))
    teachers = []
    if len(rows) > 1:
        for r in rows[1:]:
            if len(r) >= 5 and r[2].strip():
                teachers.append({"name":r[2].strip(),"title":r[3].strip() if len(r)>3 else "",
                               "department":r[4].strip() if len(r)>4 else "",
                               "detail_url":r[5].strip() if len(r)>5 else ""})

    total = len(teachers)
    valid = sum(1 for t in teachers if re.match(r'^[一-鿿]{2,4}$',t["name"]))
    urls = sum(1 for t in teachers if t["detail_url"])

    # 🔒 URL硬门禁: 师资页HTML中教师级链接很多但CSV的URL=0 → 采集器漏抓, 直接打回
    # （云大教训: 纯文本名单会把导航页当详情页提取假字段, 禁纯文本豁免）
    page_links = 0
    if fac_html and total >= 3:
        soup = BeautifulSoup(fac_html, "lxml")
        seen_u = set()
        for a in soup.find_all("a", href=True):
            t = a.get_text(strip=True)
            h = a.get("href", "").strip()
            if not h or h.startswith(("javascript", "#", "mailto")):
                continue
            if re.match(r'^[一-鿿·]{2,4}$', t) and h not in seen_u:
                seen_u.add(h)
                page_links += 1
        if page_links >= 10 and urls == 0:
            print(f"  🔒 URL硬门禁: 页面有{page_links}个教师级链接但CSV的URL=0, 打回(禁纯文本豁免)")
            return {"passed": False, "score": 0, "teachers": teachers,
                    "issues": [f"页面有{page_links}个教师链接但URL未提取, 禁止纯文本豁免"]}

    # LLM评估
    llm = get_llm()
    eval_score = 100
    eval_issues = []
    # 覆盖率软提示: URL>0 但远少于页面教师级链接 → 疑似漏采, 进 issues
    # 供修正循环 LLM 感知(不硬打回, 避免修正空转死循环)
    if page_links >= 10 and 0 < urls < page_links * 0.3:
        cov_warn = f"⚠️ 页面含{page_links}个教师级链接, 仅提取{urls}个URL, 疑似漏采(覆盖率{urls/page_links:.0%})"
        print("  " + cov_warn)
        eval_issues.append(cov_warn)
    if llm and fac_html and teachers:
        soup = BeautifulSoup(fac_html,"lxml")
        pt = soup.body.get_text()[:3000] if soup.body else ""
        ep = f"""评估教师提取质量。院系:{llm_result.get('dept','')}
CSS:{json.dumps(llm_result.get('selectors',{}),ensure_ascii=False)}
结果: {total}人, 有效名{valid}/{total}, URL{urls}/{total}
前10样本:{json.dumps(teachers[:10],ensure_ascii=False)}
页面文本:{pt}
返回JSON:{{"score":85,"issues":["..."],"passed":true}}"""
        try:
            er = llm_ask_sem("只输出JSON。", ep, tag="p3_eval")
            ee = parse_json(er)
            if ee:
                eval_score = ee.get("score",100)
                eval_issues = (ee.get("issues",[]) or []) + eval_issues
                print(f"  🧪 LLM评估: {eval_score}/100")
                for i in eval_issues[:2]: print(f"     ⚠️ {i}")
        except: pass

    score = min(eval_score, 100 if total >= 3 and valid >= total*0.8 else (60 if total > 0 else 0))
    if total < 3: eval_issues.append(f"教师数<3 ({total})")
    passed = score >= 60 and total >= 3

    print(f"  📊 {total}人, valid={valid}/{total}, urls={urls}/{total}, score={score}/100, passed={passed}")
    for t in teachers[:5]:
        print(f"     {t['name']:6s} | {t.get('title','-')[:10]:10s} | {t['detail_url'][:50]}")
    return {"passed":passed,"teachers":teachers,"score":score,"issues":eval_issues}

# ── Phase 3.5: LLM 评估 teachers.csv 质量 ──
def phase35_llm_evaluate(out_dir, dept_name, fac_html, verification):
    """LLM 评估 teachers.csv：人名是否是人名、URL是否合理、是否混入导航词。
    评估不通过 → 返回失败理由（供 Phase 4 带着错误重新生成）"""
    csvf = Path(out_dir) / "teachers.csv"
    if not csvf.exists() or csvf.stat().st_size < 50:
        return {"passed": False, "reason": "teachers.csv 不存在或为空", "verification": verification}

    rows = list(csv.reader(open(csvf, encoding="utf-8")))
    if len(rows) < 2:
        return {"passed": False, "reason": "teachers.csv 只有表头", "verification": verification}

    header = rows[0]
    name_idx = url_idx = 2
    for i, h in enumerate(header):
        if '姓名' in h or 'name' in h.lower(): name_idx = i
        if h in ('个人详情URL', '详情URL') or ('详情' in h and 'URL' in h): url_idx = i

    names = [r[name_idx].strip() for r in rows[1:] if len(r) > name_idx and r[name_idx].strip()]
    urls = [r[url_idx].strip() if url_idx < len(r) else "" for r in rows[1:] if len(r) > name_idx and r[name_idx].strip()]

    # 硬性检查1: URL缺失率>30% → 打回(允许少量无URL教师,如纯文本页/新入职)
    url_ok_count = sum(1 for u in urls if u.startswith('http'))
    url_rate = url_ok_count / len(names) if names else 0
    if len(names) >= 3 and url_rate < 0.7:
        return {"passed": False,
                "reason": f"详情URL缺失 {len(names)-url_ok_count}/{len(names)} ({url_rate:.0%})，超过30%阈值。教师名可能在<a>标签内(p.name/span)或需翻页",
                "verification": verification}

    # LLM 评估人名质量
    llm = get_llm()
    if llm:
        samples = names[:30]
        page_text = ""
        if fac_html:
            soup = BeautifulSoup(fac_html, "lxml")
            page_text = soup.body.get_text()[:3000] if soup.body else ""

        prompt = f"""评估教师名单质量。院系:{dept_name}
提取到的教师姓名(前30个):
{json.dumps(samples, ensure_ascii=False, indent=2)}

师资页原文(前3000字):
{page_text[:3000]}

请检查:
1. 哪些"姓名"其实不是人名(如导航词"学院概况"、"首页"、栏目名、部门名等)?
2. 是否有明显的非人名文本混入?
3. 与页面原文对比,是否有遗漏的教师?

返回JSON:
{{"passed": true/false, "score": 0-100, "bad_names": ["不是人名的词"], "missing": ["遗漏的教师名"], "reason": "不通过原因"}}

只输出JSON。"""
        try:
            r = llm_ask_sem("你是数据质量评估专家。只输出JSON。", prompt, tag="p35_eval")
            eval_result = parse_json(r)
            if eval_result:
                bad = eval_result.get("bad_names", [])
                # 严格: 坏名单占比>30% 直接不通过
                bad_rate = len(bad) / len(names) if names else 0
                if bad and bad_rate > 0.3:
                    print(f"  🔴 LLM评估: {len(bad)}/{len(names)} 非人名 ({bad_rate:.0%}), 超过30%阈值")
                    return {"passed": False,
                            "reason": f"提取结果中{len(bad)}个姓名不是人名(如{', '.join(bad[:5])})，请重新分析页面结构",
                            "llm_eval": eval_result, "verification": verification}
                if bad:
                    print(f"  🧪 LLM评估: 发现 {len(bad)} 个非人名: {bad[:5]}")
                if eval_result.get("passed") is False:
                    reason = eval_result.get("reason", "人名质量不合格")
                    return {"passed": False, "reason": reason, "llm_eval": eval_result,
                            "verification": verification}
                verification["llm_eval"] = eval_result
                print(f"  🧪 LLM评估: score={eval_result.get('score','?')}/100")
        except Exception as e:
            print(f"  ⚠️ LLM评估失败: {e}")

    return {"passed": True, "reason": "", "verification": verification}


# ── Phase 4: 修正（带 LLM 评估失败理由） ──
def phase4_refine(out_dir, llm_result, fac_html, verification, fail_reason=""):
    for rnd in range(3):
        if verification["passed"]: break
        print(f"\n  🔧 修正 {rnd+1}/3 ...")
        llm = get_llm()
        if not llm: break

        issues = chr(10).join(f"- {i}" for i in verification.get("issues",[]))
        if fail_reason:
            issues = f"LLM评估失败理由: {fail_reason}\n" + issues
        current_code = llm_result.get("spider_code","")[:4000]

        soup = BeautifulSoup(fac_html,"lxml")
        sample = ""
        row_sel = (llm_result.get("selectors",{}).get("teacher_row") or "")
        if row_sel:
            cards = soup.select(row_sel)
            if cards: sample = str(cards[0])[:1500]

        prompt = f"""修正spider.py选择器。问题:
{issues}
当前代码:
{current_code}
第一个card HTML:
{sample[:1500] if sample else '无匹配'}

输出约定(必须遵守, 否则修正无效):
1. 必须为每条教师记录提取详情URL(教师个人页, 非师资页本身)
2. 运行后必须生成 teachers.csv(标准8列: 大学,学院,教师姓名,职务职称,所属院系,个人详情URL,师资页URL,采集时间)
   或向 stdout 输出 JSON 数组 [{{"name":...,"title":...,"detail_url":...}}], 两者都会被采集
3. 不要丢弃现有代码中的 CSV 写入/输出逻辑

返回JSON:{{"spider_code":"修正后的Python脚本"}}
只输出JSON。"""
        r = llm_ask_sem("只输出JSON。", prompt, tag="refine")
        new_result = parse_json(r)
        if new_result and new_result.get("spider_code"):
            llm_result["spider_code"] = new_result["spider_code"]
            verification = phase3_run_and_verify(out_dir, llm_result, fac_html)
    return verification

# ── Phase 5: 输出 ──
def phase5_output(out_dir, llm_result, verification, school_name, dept_name, dept_url, faculty_url):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    teachers = verification.get("teachers",[])

    # analysis.md — 含 LLM 分析 + CSS 选择器 + 评估
    sel = llm_result.get("selectors",{}) or {}
    samples = chr(10).join(f"- {t['name']} ({t.get('title','-')})" for t in teachers[:10])
    # URL 覆盖率(防除零)
    url_total = sum(1 for t in teachers if t.get('detail_url','').startswith('http'))
    url_rate = (url_total / len(teachers) * 100) if teachers else 0

    # 页面清单(来自P4.6核查)
    page_manifest_text = "- spider.py 多级递归采集(深度≤3) + 分页 + tab页检测\n"
    pm = verification.get("page_manifest")
    if pm:
        page_manifest_text = f"- 导航链接核查: {pm.get('checked', 0)} 个\n"
        missed = pm.get("potentially_missed", [])
        if missed:
            page_manifest_text += f"- ⚠️ 可能漏采: {len(missed)} 个\n"
            for m in missed[:10]:
                page_manifest_text += f"  - {m}\n"
        else:
            page_manifest_text += "- ✅ 无漏采页面\n"
        if pm.get("analysis"):
            page_manifest_text += f"- 分析: {pm['analysis']}\n"

    analysis = f"""# {dept_name} — 教师采集分析报告

- **院校**: {school_name}
- **院系URL**: {dept_url}
- **师资页**: {faculty_url}
- **采集时间**: {ts}
- **页面类型**: {llm_result.get('page_type','?')}
- **CSS选择器**: {json.dumps(sel, ensure_ascii=False, indent=2)}
- **教师总数**: {len(teachers)}
- **验证评分**: {verification.get('score',0)}/100
- **通过**: {'✅' if verification.get('passed') else '❌'}
- **URL覆盖率**: {url_total}/{len(teachers)} ({url_rate:.0f}%)

## 页面采集清单
{page_manifest_text}

## 教师样本
{samples}
"""
    (out_dir / "analysis.md").write_text(analysis, encoding="utf-8")

    # test_report.json
    report = {k:v for k,v in verification.items() if k!="teachers"}
    (out_dir / "test_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 注意: teachers.csv 和 teachers.json 必须由 spider.py 运行生成
    # Phase 3/4 已写入 spider.py 并运行
    print(f"  💾 {out_dir}/")
    print(f"     analysis.md ({len(analysis)}B)")
    print(f"     spider.py ({len(llm_result.get('spider_code',''))}B)")
    print(f"     teachers.csv = spider.py输出 ({len(teachers)}条)")
    print(f"     test_report.json")

# ── 最终输出质量评估 ──
def _evaluate_final_output(out_dir, dept_name):
    """评估spider.py生成的CSV质量：URL覆盖率、导航词混入、有效名比例"""
    import csv, re
    from templates.spider_template import NAV_WORDS as _NAV
    csvf = Path(out_dir) / "teachers.csv"
    if not csvf.exists() or csvf.stat().st_size < 50:
        return {"total": 0, "url_ok": 0, "url_rate": 0, "nav_words": [], "valid_names": 0}

    rows = list(csv.reader(open(csvf, encoding="utf-8")))
    if len(rows) < 2:
        return {"total": 0, "url_ok": 0, "url_rate": 0, "nav_words": [], "valid_names": 0}

    header = rows[0]
    name_idx = url_idx = 2
    for i, h in enumerate(header):
        if '姓名' in h or 'name' in h.lower(): name_idx = i
    # URL 列必须精确匹配详情URL——宽匹配 'URL' in h 会选中排后的
    # "师资页URL"列, 误报 url_rate=100% 掩盖真实缺陷(北大哲学系教训)
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if ('详情' in h and 'URL' in h) or hl in ('url', 'detail_url', 'page_url'):
            url_idx = i

    total = 0; url_ok = 0; valid = 0; nav = []
    with open(csvf, encoding="utf-8") as f:
        for r in list(csv.reader(f))[1:]:
            if len(r) <= max(name_idx, url_idx): continue
            name = r[name_idx].strip() if name_idx < len(r) else ""
            url = r[url_idx].strip() if url_idx < len(r) else ""
            if not name: continue
            total += 1
            if url.startswith('http'): url_ok += 1
            if re.match(r'^[一-鿿]{2,4}$', name): valid += 1
            if name in _NAV: nav.append(name)

    url_rate = url_ok / total if total > 0 else 0
    print(f"  📏 质量: {total}人, URL={url_ok}/{total}({url_rate:.0%}), valid={valid}/{total}, nav={len(nav)}")
    return {"total": total, "url_ok": url_ok, "url_rate": url_rate, "nav_words": nav, "valid_names": valid}


# ── P4.6 页面清单核查 ──
def _page_manifest_check(out_dir, dept_name, fac_url, fac_html, verification):
    """用LLM分析师资页导航, 列出所有可能含教师的子页面, 检查是否漏采"""
    llm = get_llm()
    if not llm or not fac_html:
        return

    soup = BeautifulSoup(fac_html, "lxml")
    # 提取左侧/顶部导航区域的所有链接
    nav_links = []
    for sel in [".col_menu", ".wp_listcolumn", ".sub-menu", "nav", ".nav", "#nav",
                ".menu", "#menu", ".sidebar", ".left", ".col_list"]:
        for el in soup.select(sel):
            for a in el.find_all("a", href=True):
                t = a.get_text(strip=True); h = a.get('href','').strip()
                if t and len(t) <= 20 and h and not h.startswith('javascript') and h != '#':
                    nav_links.append({"text": t, "href": urljoin(fac_url, h)})

    if not nav_links:
        return

    # 去重
    seen = set()
    unique_links = []
    for l in nav_links:
        key = (l["text"], l["href"])
        if key not in seen:
            seen.add(key)
            unique_links.append(l)

    # 当前已采集教师数
    teacher_count = len(verification.get("teachers", []))

    prompt = f"""你是网页分析专家。检查教师采集是否遗漏子页面。

院系: {dept_name}
师资页: {fac_url}
已采集教师数: {teacher_count}

左侧/顶部导航链接(可能含教师的子页面):
{json.dumps(unique_links[:40], ensure_ascii=False, indent=2)}

请分析:
1. 哪些导航链接可能指向教师名单页面(如研究方向tab/教研室/系所/教授分类)?
2. 当前 spider.py 是否可能漏采了这些页面?
3. 哪些链接确认不是教师页(新闻/通知/下载等)?

返回JSON:
{{"potentially_missed": ["可能漏采的页面URL"], "analysis": "简短分析"}}

只输出JSON。"""
    try:
        r = llm_ask_sem("只输出JSON。", prompt, tag="manifest")
        result = parse_json(r)
        if result and result.get("potentially_missed"):
            missed = result["potentially_missed"]
            print(f"  📋 页面清单核查: 发现 {len(missed)} 个可能漏采的页面")
            for m in missed[:5]:
                print(f"     ⚠️ {m[:70]}")
            # 记录到分析
            verification["page_manifest"] = {
                "checked": len(unique_links),
                "potentially_missed": missed,
                "analysis": result.get("analysis", ""),
            }
    except Exception as e:
        print(f"  ⚠️ 页面清单核查失败: {e}")

# ── 主流程 ──
def collect_dept(school_name, dept_name, dept_url, dept_type="学院"):
    # 任务边界: 新学院重新均衡分配 token, 学院内连续调用固定同一 token (命中网关缓存省钱)
    # 学院亲和(2026-08-24): 传学院名 → 该线程固定到 hash(学院名) 槽位 token, 一个 token
    # 专责一个学院 — 跨批次/重启命中 prompt 缓存, 多 token 按学院均匀分散负载(并发放大基础)。
    # 并发提示: begin_task 只影响当前线程; 若多线程并行采集不同学院, 各线程在各自任务
    # 循环里调用本函数即可, SimpleLLM 线程隔离, 不会串 token, 无需加锁。
    get_llm().begin_task(school_name)
    safe = dept_name.replace('/','／').replace(':','：')
    out_dir = BASE / school_name / safe
    print(f"\n{'#'*60}\n  🎓 {school_name} > {dept_name} [{dept_type}]\n  🌐 {dept_url}\n{'#'*60}")

    # P0: 组织类型感知 — 书院/医院等无教师组织
    if is_no_teacher_org(dept_type):
        print(f"  ℹ️ {dept_type}类型组织, 通常无独立教师名录")
        html = fetch(dept_url) or fetch(dept_url, use_playwright=True)
        if html:
            # 快速检查是否有师资关键词
            soup = BeautifulSoup(html, "lxml")
            org_kws = load_org_keywords(dept_type)
            has_faculty = any(
                any(kw in a.get_text(strip=True) for kw in org_kws)
                for a in soup.find_all("a", href=True)
            )
            if not has_faculty:
                # 生成分析报告, 标记无数据
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                analysis = f"""# {dept_name} — 网页分析报告

- **院校**: {school_name}
- **组织类型**: {dept_type}
- **URL**: {dept_url}
- **分析时间**: {ts}
- **结论**: 该{dept_type}类型组织官网无独立教师名录(教师信息在挂靠学院/其他部门)

## 说明
{dept_type}通常不设独立师资队伍板块。若官网存在{'专家/医生团队' if dept_type=='医院' else '导师'}信息, 请人工确认。

## 建议
- 人工检查官网导航是否存在教师相关板块
- 若确实无教师信息, 保持 teachers.csv 为空
"""
                (out_dir / "analysis.md").write_text(analysis, encoding="utf-8")
                (out_dir / "teachers.csv").write_text("大学,学院,教师姓名,职务职称,所属院系,个人详情URL,师资页URL,采集时间\n", encoding="utf-8")
                json.dump({"status":"no_teacher_org","dept_type":dept_type,"reason":"该类型组织无独立教师名录","at":ts},
                          open(out_dir / "test_report.json", "w"), ensure_ascii=False, indent=2)
                print(f"  ✅ 已生成网页分析报告(无教师数据)")
                record_case(f"{school_name}/{dept_name}", {
                    "reason": f"{dept_type}类型组织无独立教师名录",
                    "solution": "生成分析报告, 标记无数据",
                    "date": ts,
                })
                return {"status":"no_teacher_org"}

    # 知识库: 检查是否有相同院系的历史案例
    kb = load_kb()
    failed_case = kb.get("failed_cases", {}).get(f"{school_name}/{dept_name}")
    if failed_case:
        print(f"  📚 知识库有历史案例: {failed_case.get('reason','')[:60]}")

    # P1: Try curl_cffi first, Playwright fallback (Anhui Univ has WAF 412)
    # 跳转壳页注意: 跟随 JS 跳转后必须用 final_url 做后续 base
    # （北大哲学系教训: szdw/index.htm 壳 → 内容在 szll/, 壳做 base 会
    #  让教师详情链接少一层目录全部 404）
    html, dept_final = fetch(dept_url, return_url=True) or (None, dept_url)
    if not html or len(html) < 800:
        h2, u2 = fetch(dept_url, use_playwright=True, return_url=True) or (None, None)
        if h2 and len(h2) >= 800:
            html, dept_final = h2, u2 or dept_url
    if not html:
        print("  ❌ 网站不可达"); return {"status":"offline"}

    fac_url = find_faculty_page(dept_final, html)
    if not fac_url:
        print("  ❌ 无师资页"); return {"status":"no_faculty"}
    print(f"  ✅ 师资页: {fac_url[:80]}")

    # P2: Fetch faculty page (also needs Playwright for WAF sites)
    fac_html, fac_final = fetch(fac_url, return_url=True) or (None, fac_url)
    if not fac_html or len(fac_html) < 800:
        h2, u2 = fetch(fac_url, use_playwright=True, return_url=True) or (None, None)
        if h2 and len(h2) >= 800:
            fac_html, fac_final = h2, u2 or fac_url
    if not fac_html:
        print("  ❌ 师资页不可达"); return {"status":"fac_offline"}
    llm_result = phase2_generate_spider(dept_name, school_name, dept_url, fac_final or fac_url, fac_html)
    if not llm_result or not llm_result.get("spider_code"):
        print("  ❌ LLM生成失败"); return {"status":"llm_failed"}

    # P3
    verification = phase3_run_and_verify(out_dir, llm_result, fac_html)

    # P3.5 — LLM 评估 teachers.csv 质量（人名是否是人名、URL是否合理、导航词混入）
    fail_reason = ""
    eval_result = phase35_llm_evaluate(out_dir, dept_name, fac_html, verification)
    if not eval_result["passed"]:
        fail_reason = eval_result["reason"]
        print(f"  🔴 LLM评估不通过: {fail_reason}")

    # P4 — 带着评估失败理由修正（最多3轮）
    verification = phase4_refine(out_dir, llm_result, fac_html, verification, fail_reason)

    # P4.4 — CSV净化: 剔除导航词/假URL/重复行, 统一为标准8列格式
    total, url_ok, nav_rem, fake_rem, dup_rem = sanitize_teachers_csv(out_dir, school_name, dept_name)
    if total >= 1:
        # 用净化后的数据更新 verification, 供 P4.5/P4.6/P5 使用
        cleaned = []
        rows = list(csv.reader(open(out_dir / "teachers.csv", encoding="utf-8")))
        for r in rows[1:]:
            if len(r) >= 6 and r[2].strip():
                cleaned.append({"name": r[2].strip(), "title": r[3].strip() if len(r) > 3 else "",
                                "department": r[4].strip() if len(r) > 4 else "",
                                "detail_url": r[5].strip() if len(r) > 5 else ""})
        verification["teachers"] = cleaned
        # 净化后发现导航词/假URL混入 → 必须打回(不能覆盖失败判定)
        if nav_rem > 0 or fake_rem > 0:
            if nav_rem >= max(3, total * 0.1):
                verification["passed"] = False
                verification["score"] = min(verification.get("score", 0), 30)
                verification["issues"] = verification.get("issues", []) + [
                    f"CSV净化剔除导航词{nav_rem}个, 提取结果污染严重"]
        elif total >= 3 and (url_ok / total) >= 0.6:
            # 净化后干净且数量/URL达标 → 可放行
            if verification.get("passed") is not True:
                verification["passed"] = True

    # P4.5 — 最终质量门禁（URL覆盖率 + 导航词过滤）
    quality = _evaluate_final_output(out_dir, dept_name)
    if (quality["url_rate"] < 0.9 or quality["nav_words"]) and quality["total"] >= 3:
        print(f"  🔴 质量门禁不通过: URL={quality['url_ok']}/{quality['total']}, nav={len(quality['nav_words'])}")
        llm_result = phase2_generate_spider(dept_name, school_name, dept_url, fac_url, fac_html, retry=True)
        if llm_result and llm_result.get("spider_code"):
            verification = phase3_run_and_verify(out_dir, llm_result, fac_html)
            verification = phase4_refine(out_dir, llm_result, fac_html, verification, fail_reason)
        else:
            print(f"  ❌ 无法自动修复,需人工介入")

    # P4.6 — 页面清单核查(防止漏采tab/子页) — 用LLM分析页面导航,列出所有可能含教师的页面
    _page_manifest_check(out_dir, dept_name, fac_url, fac_html, verification)

    # P5
    phase5_output(out_dir, llm_result, verification, school_name, dept_name, dept_url, fac_url)

    print(f"\n  {'✅' if verification['passed'] else '❌'} {len(verification['teachers'])}人, score={verification['score']}/100")
    return {"status":"done","teachers":len(verification['teachers']),"passed":verification["passed"]}

# ── 批量 ──
def load_depts(name):
    p = BASE / name / "departments.csv"
    if not p.exists(): return []
    return [{"school":name,"dept":r["院系名称"].strip(),"url":r["院系URL"].strip(),
             "type":r.get("院系类型","学院").strip()}
            for r in csv.DictReader(open(p))]

def main():
    p = argparse.ArgumentParser(); p.add_argument("--school"); p.add_argument("--dept"); p.add_argument("--all",action="store_true")
    args = p.parse_args()
    if args.school and args.dept:
        depts = load_depts(args.school)
        url = next((d["url"] for d in depts if d["dept"]==args.dept),None)
        if not url: print(f"❌ {args.dept}")
        else: collect_dept(args.school, args.dept, url)
    elif args.school:
        for d in load_depts(args.school):
            collect_dept(d["school"],d["dept"],d["url"],d.get("type","学院"))
    elif args.all:
        for s in sorted(d.name for d in BASE.iterdir() if d.is_dir() and (d/"departments.csv").exists() and d.name not in COLLEGE_SCHOOLS):
            for d in load_depts(s): collect_dept(d["school"],d["dept"],d["url"])
    else: p.print_help()

if __name__ == "__main__": main()
