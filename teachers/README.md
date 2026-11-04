# Step 4 · 教师详情采集 Agent —— 个人页下载 + 13 字段结构化提取

> 阶段定位：**四阶段流水线的最后一站**。按 Step 3 名录逐教师访问详情页，
> 下载原始页面（HTML/PDF/头像），提取 13 个结构化字段。数据产物**不随仓库
> 分发**（开源边界止于 Step 2 院系 URL 层级）。

```
   Step 1                Step 2              Step 3              Step 4 ★(当前)
   学校信息采集    →      二级院系采集    →   教师名录采集    →   教师详情采集
   colleges info/        colleges department/ department teachers/  teachers/
   (全国主名单)          (院系+URL)           (姓名+详情URL)       (13字段)
```

## 输入与产出

| | 说明 |
|---|---|
| **输入** | Step 3 产出：`department teachers/output/<学校>/<院系>/teachers.csv`（姓名 + detail_url） |
| **产出** | `teachers/<学校>/<院系>/teachers_detail.csv`（提取结果）、`tearchers raw data/<学校>/<院系>/`（原始 HTML/PDF/头像，文件名 `<姓名>_<url前8位md5>.<ext>`） |
| **运行时生成** | 以上产物目录均在 `.gitignore` 排除之列 |

## 工作流设计：两步解耦 + 单院系管道 + 改进闭环

本阶段工作流分三层：**两步解耦**（下载/提取分离，全局架构）、
**单院系 Agent 管道**（分析→生成→运行→评估→重试）、**改进闭环**
（评估驱动规则进化）。核心设计：**下载不依赖 LLM 可高并发，提取依赖 LLM
但零抓取**——两步用 `download_done.flag` 完成标志衔接，互不阻塞：

```
Step 3 teachers.csv
      │
      ▼
┌─ 第一步 下载（download_stage.py, 无LLM, 可高并发）────────────┐
│  1. 名单 QC（roster_qc.py: 垃圾名/导航链接/URL缺陷过滤）       │
│  2. 逐院系差量下载: 本地已有 HTML/PDF 跳过（断点续传）          │
│     HTML 下载后探测 PDF → 有则下载（带重试）                   │
│  3. 写 download_done.flag                                      │
│  ⇒ tearchers raw data/<学校>/<院系>/<姓名>_<md5>.html|pdf      │
└───────────────────────────────────────────────────────────────┘
      │  (只看 flag, 未下载完的院系跳过)
      ▼
┌─ 第二步 提取（extract_stage.py / extract_all_details.py, 依赖LLM）─┐
│  1. 复用本地 HTML/PDF 提取（零网络抓取）                        │
│  2. 学院规则 + LLM 提取 13 字段（无 LLM 时启发式兜底）          │
│  3. evaluate 质量评估 → 不通过进 extract_fix_loop 规则改进闭环  │
│  ⇒ teachers_detail.csv / .json                                 │
└────────────────────────────────────────────────────────────────┘
```

**为什么解耦**：下载阶段的瓶颈在网络与反爬（可以 24 并发跑通宵），提取阶段的
瓶颈在 LLM（贵且需评估循环）。解耦后两边各自最优并发，提取可反复重跑
（`--reextract-llm` / `--reapply-rules`）而不再碰目标站点——对站点一次下载、
多次提炼，礼貌性与成本控制都由此而来。

### 提取阶段的自适应：两层设计

| 层 | 自适应方式 | 何时用 LLM |
|---|---|---|
| **结构层**（找到正文在哪） | 12 个语义容器选择器优先命中 → 剥离导航/页脚噪声再喂 LLM | 选择器命中即零 LLM |
| **字段层**（抽出 13 字段） | LLM 逐页提取优先（深语义理解无稳定规则可写）；学院级规则（`scripts/gen_dept_rules.py` 生成的选择器+正则规则，格式见 [dept_rule_format.md](dept_rule_format.md)）作 LLM 不可用时的备选——自动切换并打标记，LLM 恢复后 `--reextract-llm` 升级重提 | 提取时 |

与 Step 2/3 的区别：名录采集（结构规整）由 LLM **生成规则**后零 token 复跑；
详情字段提取（叙述型简历、字段粘连形态千变万化）没有可复用的稳定规则，
故以 LLM 逐页提取为主、规则为备选。提取结果仍由**字段错位交叉校验**把关，
不过关进 `extract_fix_loop` 规则改进闭环。

## 单院系 Agent 管道（dept_agent.py / run_single_dept.py）

批量脚本内部对每个院系跑一个完整的「分析 → 生成 → 运行 → 评估」管道
（设计与并发拓扑集中说明见 [DESIGN.md](DESIGN.md)，质量规格见
[quality_criteria.md](quality_criteria.md)）：

```
Step 0   师资发现（faculty_discovery.py）: 官网线索页 → 分类页 → 名单提取
         （含分页遍历/前缀回退; Step 3 只抓一类教师时补漏）
Step 0.5 站点原型预检（≤3 样本）→ 策略路由（见下表）
Step 1   加载清单: clean_name 职称粘连拆解 + 人名过滤
Step 2   LLM 分层抽样分析 → analysis.md（同校 warm-start）
Step 3   LLM 生成 spider.py（无 LLM 走确定性兜底模板）
Step 4   运行 spider → 25 列 CSV/JSON + raw data
Step 5   分层评估（classify_record 单一口径）
Step 6   不通过 → 重试 ≤3 轮; 数据源限制识别 → 跳过重试
```

### 站点原型路由（preflight）

教师页的"坑"是有限家族，预检归类后按原型路由策略：

| 原型 | 特征 | 策略 |
|---|---|---|
| `normal` | 有 URL 且内容密度正常 | 常规管道 |
| `pdf_shell` | 页面带 PDF 引用（播放器/iframe/链接） | PDF 检测 + 下载 + 自适应提取 |
| `empty_shell` | 无 PDF 且内容样本标签密度 ≈ 0（JS 壳页） | 渲染兜底（系级熔断）+ 空壳主导跳过重试 |
| `name_list_no_links` | ≥50% 教师无详情 URL | 搜索式发现（search_rescue.py: Bing 查询 → 校域过滤）+ 名单落盘 |

## 提取字段（13 个）

| 字段 | 说明 |
|------|------|
| 联系方式 | 电话/邮箱/传真等 |
| 通讯地址 | 邮寄/办公地址 |
| 职称 | 教授、副教授等 |
| 职务 | 系主任、副院长等行政职务 |
| 出生年月 | 出生日期 |
| 教育经历 | 本硕博就读学校、专业、时间 |
| 工作经历 | 过往任职经历 |
| 研究方向 | 科研方向 |
| 学术成果 | 论文、专著、专利 |
| 基金 | 承担科研基金/项目 |
| 荣誉 | 奖项、人才称号 |
| 课堂项目 | 教学/教改项目 |
| 讲授课程 | 主讲课程 |

提取策略（LLM 优先、规则备选）见上文[提取阶段的自适应](#提取阶段的自适应两层设计)。
评估含**字段错位交叉校验**：结构标签词（"工作经历"/"Email"）出现在错误字段 = 真
错位/整段倾倒（安大哲学 9 人误报教训：标签词才能交叉，"本科/硕士"等值词不能）。

## 正文提取

优先命中正文容器（`.wp_articlecontent`/`.gp-subRight-con`/`.subCon` 等 12 个
选择器），剥离导航/页脚垃圾文本后再给 LLM，避免"招生/招聘/院系概况"污染。

头像提取三级：① class/id 含 photo/avatar/head 的 img → ② alt 含"照片/头像"
→ ③ 正文容器内竖图（高>宽）或小图（≤320px）。

## 反爬设计（瑞数 WAF）

复用仓库根 `util/` 统一请求栈（Step 2/3 验证过的方案，细节见
[瑞数反爬解决方案](../docs/瑞数反爬解决方案.md)）：

1. **纯 curl_cffi 快路径**：无 WAF 站点秒回，不触发任何 cookie 逻辑
2. **RsSpider**（`util/handleRuishu.py`）: 域名级 Cookie 缓存 + 失效自动重生成
   （sdenv → DrissionPage → Playwright 三级）
3. **smart_fetch 五级降级**: curl_cffi → iv8/nodenv/sdenv → DrissionPage → CDP
4. 本地 Playwright 兜底

已知特性：瑞数 T token 指纹绑定（curl_cffi 被拒后自动转浏览器内请求）；
Cookie 约 30-40 分钟失效自动重生成；同校子域名共享主域 Cookie。

## 断点续传（两级）

1. **院系级**：`task_state.json` 记录 status=done，`--resume` 跳过
2. **教师级**：`<姓名>_<md5>.html/pdf` 已存在则直接复用本地文件提取（♻️ 标记），
   任务中途被杀可无痛重启；已完成院系重跑须带 `--resume` 才跳过

## 环境变量（节选，全表见根 `.env.example`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DOWNLOAD_PDF` | `0` | PDF 下载开关。`0`=不下载（仅记录 `has_pdf`/`pdf_url`）；`1`=下载 PDF 并用 pdfplumber 提取 |
| `RATE_LIMIT` | `0.3` | 教师间请求间隔（秒） |
| `LLM_TIMEOUT` | `60` | LLM 提取超时（秒），超时降级启发式 |
| `TEACHER_WORKERS` | `4` | 教师级并发数 |

## 运行方式

在**仓库根目录**执行：

```bash
# 下载（第一步; --qc-only 只跑名单 QC 报告）
uv run --project . python teachers/download_stage.py --school 北京大学
uv run --project . python teachers/download_stage.py --school 北京大学 --dept 考古文博学院
uv run --project . python teachers/download_stage.py --all --teacher-workers 24 --rate-limit 0.08

# 提取（第二步; 只处理已打 download_done.flag 的院系）
uv run --project . python teachers/extract_stage.py --school 北京大学
uv run --project . python teachers/extract_all_details.py --all --resume
uv run --project . python teachers/extract_all_details.py --school 北京大学 --reextract-llm   # LLM升级重提

# 单院系完整 Agent 管道（分析→spider→运行→评估, 最多3轮）
uv run --project . python teachers/dept_agent.py --school 安徽大学 --dept 材料科学与工程学院

# 名单 QC / 评估驱动规则改进 / 增量采集
uv run --project . python teachers/roster_qc.py --school 北京大学
uv run --project . python teachers/extract_fix_loop.py --school 北京大学
uv run --project . python teachers/incremental_collect.py            # 上游名单更新后差量处理
```

## 文件结构

```
teachers/
├── README.md / DESIGN.md / quality_criteria.md    ← 本文件 / 设计文档 / 质量规格
├── concurrency_playbook.md / resource_strategy.md / dept_rule_format.md
│
├── download_stage.py        ← 第一步: 下载（QC+差量下载, 无LLM高并发）
├── extract_stage.py         ← 第二步: 提取（复用本地文件, 依赖LLM）
├── extract_all_details.py   ← 全量提取编排（断点续传/规则重应用/LLM重提）
├── roster_qc.py             ← 名单质量检测（垃圾名/URL缺陷→QC报告）
├── detail_scraper.py        ← 核心采集器（PDF/渲染兜底/分类, 13字段提取）
├── dept_agent.py            ← 单院系完整 Agent 管道（Step 0-6）
├── run_single_dept.py / batch_run.py / incremental_collect.py ← 单系/批量/增量编排
├── faculty_discovery.py     ← 师资分类发现（Step 3 漏采补漏）
├── search_rescue.py         ← 搜索式发现（无链接名单的 URL 补全）
├── spider_gen.py / dept_rule_generator.py / gen_rules_batch.py ← spider/规则生成
├── extract_fix_loop.py / extract_after_rules.py / extract_gap.py ← 提取改进闭环
├── evaluate_teachers.py     ← 详情质量评估（字段错位交叉校验）
├── fix_improve 相关 / improve 入口见根目录
└── scripts/                 ← 院系级工具（差分引擎 upstream_refresh/_fetch_dept、
                               规则生成/评估/测试 gen·eval·test_dept_rules、
                               批量提取 extract_batch、扫描 scan_rule_friendly）
```

## 相关文档

- [DESIGN.md](DESIGN.md) —— 管道设计 / 站点原型路由 / 并发拓扑（院系级进程池 ×
  教师级线程池 × 请求级节流门 × 渲染级预算）
- [quality_criteria.md](quality_criteria.md) —— 质量规格（Ω：classify_record 单一口径）
- [concurrency_playbook.md](concurrency_playbook.md) —— 并发实战手册
- [resource_strategy.md](resource_strategy.md) —— 资源策略
- [dept_rule_format.md](dept_rule_format.md) —— 学院提取规则格式
