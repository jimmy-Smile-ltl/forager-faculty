# Step 2 · 二级院系采集 —— 官网院系名录（含官网 URL）

> 阶段定位：**四阶段流水线的第二站，也是本仓库开源数据边界所在**。
> 从 144 所 985/211/双一流高校官网提取全院系及教学科研组织（名称 + 官网 URL）。

```
   Step 1                Step 2 ★(当前)        Step 3                Step 4
   学校信息采集    →      二级院系采集    →     教师名录采集    →     教师详情采集
   colleges info/        colleges department/  department teachers/  teachers/
   (全国主名单)          (选校→院系+URL)       (姓名+详情URL)        (13字段)
                        ★★★ 开源数据边界止于此处 ★★★
```

## 输入与产出

| | 说明 |
|---|---|
| **输入** | `985_211_双一流高校清单.csv`（144 所目标校，源自 Step 1 筛选） |
| **产出** | `depart_985211.csv` / `.jsonl`（144 校 × 8,049 院系：名称 + 官网 URL + 类型）——**随仓库分发的唯一数据产物** |
| **运行时产物** | `output/<学校>/`（spider + 审计链路），运行本目录框架自动生成，已 `.gitignore`，不随仓库分发 |
| **下游消费者** | Step 3 按院系 URL 逐系采集教师名录 |

> **数据边界**：本阶段产物（学校属性、院系名称、院系官网 URL）随仓库分发；
> Step 3/4 的教师级数据产物不随仓库分发，见根 README [数据边界](../README.md#数据边界)。

## 采集结果（144/144 完成）

| 指标 | 数值 |
|------|:---:|
| 目标学校 | 144 所 |
| 成功采集 | **144 / 144 (100%)** ✅ |
| 院系总数 | **8,049** |
| 平均质量分 | **99.3** |
| 100分学校 | 131 所 (91.0%) |
| 通过率(≥60分) | **144 / 144 (100%)** ✅ |
| 有 URL 二级组织 | 81.2% |

质量分来自 `evaluate_departments.py`（见下文[评估器](#评估器-evaluate_departmentspy)）。

## Agent 工作流（分析 → 生成 → 执行 → 评估）

与 Step 3/4 共享「生成规则 → 运行 → 评估 → 修复」循环，但反爬与页面结构
差异较大，本阶段拆为三个专职脚本，由 `orchestrator.py` 批量编排。每校一个
完整循环：

```
985_211_双一流高校清单.csv（144 校: 名称 + 官网URL）
        │
        ▼
┌─ auto_analysis.py（分析阶段, 有LLM）────────────────────────┐
│  1. smart_fetch(首页) → 检测反爬类型                          │
│  2. find_dept_page → 找所有院系入口(教学/科研/医院多页面)      │
│  3. LLM 首页分析 → 区分入口类别, 识别多页面结构               │
│  4. 逐页 fetch + DOM 分析 → 推导严格 CSS 选择器               │
│     (禁全站 a[href] — 噪声从源头拦截)                         │
│  5. LLM 生成 filter_list.json {drop, keep} 过滤清单           │
│  ⇒ 产出 spider_config.json + analysis_report.md              │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ spider_generator.py（生成阶段, 纯本地）────────────────────┐
│  spider_config.json → 模板渲染出独立 spider.py               │
│  (软过滤 + LLM 决断留痕 + 审计链路)                           │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ spider.py（执行阶段, 纯本地无LLM）─────────────────────────┐
│  四级请求降级 → footer/nav剔除 → 名称清洗 → 特殊模式库(12种)  │
│  → filter_list drop/keep → 三级去重                          │
│  ⇒ 产出 departments.json/csv + _filter_log                   │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ evaluate_departments.py（评估阶段）────────────────────────┐
│  数量/噪声/结构/URL覆盖 四维打分 → FAIL 学校回到分析阶段修复  │
└──────────────────────────────────────────────────────────────┘
        │ FAIL（携带维度得分与修复建议）
        ▼
┌─ 修复回路 ─────────────────────────────────────────────────┐
│  调整 source_pages/选择器/filter_list → 重新生成 → 重跑 →   │
│  重评，直到通过；个别顽固站走专项路径（fix_failed.py:        │
│  按站配 method 的 CloakBrowser 补采 / known_url 直配）       │
└──────────────────────────────────────────────────────────────┘
```

**设计要点**：LLM 只出现在「分析」阶段（生成选择器与过滤清单），
执行阶段的 spider.py 是**纯本地脚本**——BeautifulSoup 严格 CSS 选择器
+ 名称正则清洗，LLM 阅读页面样本**设计规则**而非逐页提取：分析一次、
反复重跑，不烧 token，也保证同一学校重采结果可复现。

## 每校运行时产出（独立文件夹 `output/{学校名}/`）

> 该目录由框架运行时生成（LLM 分析 + spider 产物 + 审计链路），
> 属于**运行产物**而非开源数据，已在 `.gitignore` 排除。此处仅说明其结构。

```
output/安徽大学/
├── analysis_report.md        ← LLM生成的页面结构分析
├── spider_config.json        ← 机器可读配置（source_pages + 严格CSS选择器）
├── filter_list.json          ← LLM过滤清单（drop/keep, 可人工修正）
├── spider.py                 ← 独立可运行爬虫（纯本地, 无LLM）
├── _filter_log.md / .json    ← 过滤审计（候选→关键词建议→决策→理由）
├── departments.json / .csv   ← ★ 最终产出（院系名称+URL+类型）
└── result_evaluation.json    ← 质量评估结果
```

## 评估器（`evaluate_departments.py`）

四维检查，60 分及格：

| 维度 | 检测内容 |
|---|---|
| 数量 | 综合大学 ≥15 / 行业特色校(海洋/农业/矿业/地质/林业/石油/电力/化工/中医药) ≥8 / 特殊院校 ≥5，分档阈值 |
| 噪声 | 新闻 URL / 行政机构 / 导航标题 / 分类标题（泛化词才算，具体机构名不算） |
| 结构 | URL 归一后重复、名称重复、子域多样性（统一 www 的 CMS 特征） |
| URL 覆盖 | 每校 ≥20 个有 URL 的二级组织（院系总数 <20 的特殊校按 70% 豁免） |

评估器内置豁免规则防止误杀：介绍页（`/info/\d+/\d+` + 机构名）不是新闻；
同 URL 不同名（一子站挂多机构，如附属医院 7 院一块牌子）是真并列不是重复。

## 过滤规则设计（核心沉淀）

院系采集的主要敌人是**噪声**：新闻标题、行政机构、导航栏目被当成院系。
多层过滤的优先级与豁免规则如下：

### 过滤优先级

```
硬排除层(最高) → 强排除 → filter_list drop → 关键词排除 → 宁缺毋滥兜底
                                                     ↑
                                        强保留/豁免规则在此拦截误杀
```

### 关键规则

| 类别 | 规则 | 原因 |
|------|------|------|
| 硬排除 | 导航标题(院系设置/教学科研机构)、行政中心(信息与网络中心/考务中心)、医院内部页(医院概况/内科部) | 即使 filter_list keep 也不保留 |
| 顺序 | 排除词先于保留词判定 | "院系设置"含"系"会误触强保留 |
| 弱排除豁免 | 含强教学词(学院/研究所)时豁免排除词 | "国际交流学院"是真实机构 |
| 部级单位 | 体育部/外语部/思政教研部保留; 合作与发展部排除 | 按部内关键词区分教学 vs 行政 |
| 医院 | 附属XX医院/直属医院保留; 校医院/门诊部排除 | 有医生队伍 vs 行政服务 |
| 临床科室 | 部/科结尾+临床词 → 排除 | 内科部/检验科不是院系 |
| 行政后缀 | 处/室/会/馆/委/组结尾且非教学后缀 → drop | "资产与实验室管理处"不被"实验室"误救 |
| 教育导航词 | 本科生教育/招生 → drop | 但"国际教育学院/继续教育学院"有教师 → keep |
| 分类标题 | "全国重点实验室"是分类名; 具体机构名不是 | 只排除泛化词 |
| URL 形式≠性质 | 介绍页/info 页也可以是机构 | 名称才是判断核心 |
| 教学单位保护 | drop 不得删除 系/书院/学部/学院/研究院 结尾单位 | 东北农大 72 系曾整体误删 |
| 组合名拆分 | "X学院、Y学院" 多机构并列 → 保留 | 不是一条院系 |
| 注释/栏目 | "注：/备注：" 前缀、"XX简介/领导/动态" 后缀 → 硬排除 | 清华"注：*为非实体学院" |
| 精确匹配原则 | 自然科学/人文科学等只精确匹配不子串 | 子串会误杀"自然科学学部" |

### 名称清洗与三级去重

```
清洗: ■◆●▶▲▼•·✦✧➤ 列表符号 / 尾部顿号分号 / 内部空格 / 面包屑" > " /
      尾部"更多" / 重复机构名(新闻标题特征)
去重: ① URL 归一(http→https, 去尾斜杠)  ② 同名 https 优先
      ③ 模糊名称合并("法学院（纪检监察学院）" vs "法学院")
      另: 同 URL 后缀去重保留全称; 表格URL列回填机构名; 孤立括号剥离
```

## 特殊页面模式库（12 种，已固化）

高校官网的院系页形态远比想象中多样，以下模式已沉淀为 spider 内置处理：

| 模式 | 代表学校 | 处理方式 |
|------|---------|---------|
| 多页面(教学+科研+医院) | 安徽大学、北京协和 | `source_pages[]` 多 URL |
| 二级子页发现 | 北京协和 | `discover_sub_pages` 自动展开 |
| link.jsp 跳转 | 福州大学 | `bmmc=xxx` → `xxx.域名` 转换 |
| 首页直列学院 | 南京林业、上海科技 | 首页即列表检测 |
| 介绍页提取 | 天津大学 | "学院网址"字段遍历 |
| 图片地图 | 天津工业大学 | `area href` + 手工映射 |
| 纯文本学院(无href) | 浙江大学 | 无href+教学关键词保留 |
| 无URL书院 | 中国人民大学 | 文本节点提取 |
| 标题卡片 | 北京林业大学 | h2名称+同级a链接 |
| CMS折叠面板 | 中国传媒大学 | DrissionPage渲染 |
| JS滚动动画隐藏 | 兰州大学 | `li.aos-init a` + DrissionPage渲染 |
| 附属医院站内直链 | 成都中医药大学 | 硬排除内科部等栏目, 只留"附属XX医院" |

## 反爬与请求降级

复用 `util/smart_fetch.py` 统一请求栈（细节见根 README
[自适应请求栈](../README.md#2-自适应请求栈util)）：

```
curl_cffi (impersonate chrome131)     → ~60% 学校直接通过
  │ 412/202 或 $_ts.nsd 特征码
  ├── RsSpider (瑞数 Cookie 四级生成)  → 应用层瑞数
  ├── DrissionPage (ChromiumPage)     → JS渲染 + 应用层瑞数
  └── CDP (:9222 常驻浏览器)          → 网络层瑞数 + JS全渲染
```

瑞数对抗分三层，工具各不相同：

| 层级 | 特征 | 对策 |
|------|------|------|
| 应用层瑞数 | HTTP 412/202 + `$_ts.nsd` | RsSpider(缓存→sdenv→DrissionPage→Playwright) ✅ |
| 网络层瑞数 | TCP broken pipe / reset | **只能换网络出口**（换浏览器工具无效） |
| IP 黑名单 | connection refused | 换网络 / 等待解封 |

CDP 层两个实战细节：`domcontentloaded` 防挂起资源（等 `load` 会卡死超时）、
滚动触发懒加载（浙大等渐进渲染页面）。

## 文件清单

| 文件 | 说明 |
|------|------|
| `depart_985211.csv` / `.jsonl` | ★ 全局汇总（144 校 8,049 院系）—— 本阶段最终产出、唯一随仓库分发的数据 |
| `985_211_双一流高校清单.csv` | 输入：144 所目标校主表 |
| `orchestrator.py` | 批量编排：分析→生成→执行 三阶段调度 |
| `auto_analysis.py` | 分析阶段：LLM 辅助页面结构分析（入口/选择器/filter_list） |
| `spider_generator.py` | 生成阶段：spider_config → 独立 spider.py |
| `evaluate_departments.py` | 评估：四维打分 + 修复建议 |
| `evaluate_results.py` / `llm_sample_eval.py` | LLM 抽样复核评估 |
| `fix_failed.py` / `fix_remaining.py` | 失败校修复工具 |
| `scrape_departments.py` / `merge_985_211_full.py` | 早期采集与合并工具 |
| `output/<学校>/` | 运行时产物（每校一目录，见上文；已 gitignore） |

## 运行方式

在**仓库根目录**执行：

```bash
# 单校完整流程（分析 → 生成 → 运行）
uv run --project . python "colleges department/orchestrator.py" --name "安徽大学"

# 批量（全量 / 限 5 所）
uv run --project . python "colleges department/orchestrator.py" --all
uv run --project . python "colleges department/orchestrator.py" --limit 5

# 也可分步执行
uv run --project . python "colleges department/auto_analysis.py" --name "安徽大学" --url "https://www.ahu.edu.cn/"
uv run --project . python "colleges department/spider_generator.py" --name "安徽大学"
uv run --project . python "colleges department/output/安徽大学/spider.py"   # 运行时生成的独立 spider

# 全量质量评估
uv run --project . python "colleges department/evaluate_departments.py" --all
```

需要 LLM 配置（`.env`，见根 README [快速开始](../README.md#快速开始)）；
spider 执行阶段本身不消耗 LLM。
