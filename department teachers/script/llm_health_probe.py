# -*- coding: utf-8 -*-
"""LLM 健康探针 v3 (2026-08-26 迁移项目内, 防 /tmp 清理) — 复刻真实 phase2 prompt(含规则模板), 目标 ≥5.5KB
候选页面多个(防单页 fetch 失败), 每 60s 探测, 连续 2 次成功(<295s) 判恢复"""
import sys, time, datetime, json as _json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from collect_teacher import llm_ask_sem, fetch, load_kb
from bs4 import BeautifulSoup

# 历史 fetch 成功过的师资页候选
FAC_URLS = [
    "https://www.pcs.tsinghua.edu.cn/szdw.htm",
    "http://www.ig.zju.edu.cn/redir.php?catalog_id=24379",
    "http://ghxy.hhu.edu.cn/5668/list.htm",
    "https://cs.henu.edu.cn/szdw/jsmlxin/dwry.htm",
    "https://ee.hainanu.edu.cn/szdw.htm",
]

RULES = """
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

返回JSON格式:
{
  "page_type": "direct_list 或 sub_directory",
  "analysis": "1句话",
  "spider_code": "#!/usr/bin/env python3\\n...(完整Python)\\n..."
}
"""

def probe_prompt():
    kb = load_kb()
    kb_hint = ""
    if kb.get("page_structure_patterns"):
        kb_hint = "## 已知页面结构模式\n" + _json.dumps(
            {k: v.get("description","")[:100] for k,v in list(kb["page_structure_patterns"].items())[:5]},
            ensure_ascii=False)
    for url in FAC_URLS:
        try:
            html = fetch(url) or ""
        except Exception:
            html = ""
        if len(html) < 3000:
            continue
        soup = BeautifulSoup(html, "lxml")
        for t in soup.find_all(["script","style","noscript"]): t.decompose()
        body_text = soup.body.get_text()[:800] if soup.body else ""
        links = []
        for a in soup.find_all("a", href=True)[:80]:
            t = a.get_text(strip=True); h = a.get("href","")
            if t and len(t) <= 25:
                links.append({"text":t[:30],"href":h[:100]})
        prompt = (f"你是爬虫专家。为下页面生成一个独立运行的 Python 采集脚本(spider.py)。\n{kb_hint}\n"
                  f"## 页面信息\n- 院系: 探针测试\n- 学校: 测试\n- 师资页: {url}\n"
                  f"## 链接样本\n{_json.dumps(links[:8], ensure_ascii=False, separators=(',',':'))}\n"
                  f"## 页面文本\n{body_text[:800]}\n{RULES}\n只输出JSON。")
        if len(prompt) >= 5500:
            return prompt
    # 全部候选失败: 用假 HTML 兜底(仅测服务端大 prompt 能力)
    body = "师资队伍 教授 副教授 讲师 " * 300
    links = [{"text":f"教师{i}","href":f"/js{i}/index.htm"} for i in range(12)]
    return (f"你是爬虫专家。生成采集脚本。\n## 页面文本\n{body[:1000]}\n## 链接样本\n"
            f"{_json.dumps(links, ensure_ascii=False)}\n{RULES}\n只输出JSON。")

# 判定: 有内容且 <890s(15分钟, 2026-09-03 用户指令"阈值搞大一点, 愿意多等待")
# 且长度≥1500(实质响应) 且非 degraded/error JSON = 服务端可用。
# 2026-08-26 阈值放宽: 服务端重任务(spider生成)延迟实测 196-297s 波动,
# 原 295s/LLM_TIMEOUT=300 差 2s 误判 BAD → LLM_TIMEOUT=420, 判定 <415 留余量。
# 2026-09-03 弱READY事故: 18:10:56 探测 363s/len=243(degraded JSON) 被旧判定
# bool(r) and dt<415 放行 → 新批 4 分钟吃 10 个 APITimeoutError。修复:
# ① dt<890 (用户指令 15 分钟); ② len>=1500 + 拒绝 degraded/error JSON —
#   网关超时兜底返回的 {"error":...,"degraded":true} 不再冒充 READY。
# 2026-08-27 波动期(11:45-12:01): 池标记正常但大 prompt 偶发空返回, 连续 2 次
# READY 门槛使批次数小时无法启动 → 放宽为 1 次 READY 即放行(批次内部有
# llm_healthy 健康检查 + fix_one 失败重试, 慢但可用窗口能持续推进)。
if __name__ == "__main__":
    STREAK = 0
    while True:
        try:
            p = probe_prompt()
            t0 = time.time()
            r = llm_ask_sem("只输出JSON。", p, timeout=900, tag="health")
            dt = time.time() - t0
            good = (bool(r) and dt < 890 and len(r) >= 1500
                    and '"degraded"' not in r[:300] and '"error"' not in r[:300])
            # 2026-09-08 网关语义分类(用户问「服务器有没有报错返回」实锤):
            # one-api 渠道耗尽返回 HTTP 503 {"error":"...无可用渠道...","type":"one_api_error"}
            # — SDK 侧表现为快速 APIStatusError(有 body); 渠道排队挂起则表现为
            # APITimeoutError(无 body, 服务器确实没返回)。分类进探针输出, 汇报可区分
            # 「容量耗尽」与「挂起波动」。
            _reason = ""
            if r:
                _r0 = (r or "")[:500]
                if "无可用渠道" in _r0 or "one_api_error" in _r0:
                    _reason = "无可用渠道(容量耗尽)"
                elif '"degraded"' in _r0:
                    _reason = "degraded降级响应"
                elif '"error"' in _r0:
                    _reason = "error响应"
            STREAK = STREAK + 1 if good else 0
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {'READY' if good else 'BAD '} {dt:.0f}s plen={len(p)} len={len(r) if r else 0} "
                  f"{('[' + _reason + ']') if _reason else ''} streak={STREAK} {'←' if good else ''}", flush=True)
            if good and STREAK >= 1:
                print("LLM_READY", flush=True)
                break
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] EXC {type(e).__name__}", flush=True)
            STREAK = 0
        time.sleep(60)  # 90→60: 白天窗口窄(~2-3分钟), 90s间隔使 streak≥2 几乎无法触发
