# 教师详情采集 Agent — 设计文档

> 架构/并发拓扑/站点原型/策略路由的集中说明。改动设计先读本文与
> improvements.md、quality_criteria.md（Ω 规格）。

## 1. 管道（单院系 run_dept）

```
Step 0   师资发现: 官网/线索页 → 分类页 → 名单提取（含分页/前缀回退）
         · 救场链: 过滤后空(或<5人) → 自动发现；被过滤条目 URL 作线索
         · 线索页无子分类 → 当名单页直爬
Step 0.5 预检站点原型（≤3 样本, 本地优先）→ 策略路由
Step 1   加载教师清单: clean_name(职称/标签粘连) + _is_person_name 过滤
Step 2   LLM 分层抽样分析 → analysis.md（注入同校 site_registry warm-start）
Step 3   LLM 生成 spider.py（确定性头部; 无 LLM 走确定性兜底模板）
Step 4   运行 spider.py → 25 列 CSV/JSON + raw data
Step 5   分层评估（classify_record 单一口径, Ω=quality_criteria.md）
Step 6   不通过 → 重试 ≤3 轮；数据源限制识别（纯名单主导/空壳主导）→ 跳过重试
```

## 2. 站点原型与策略路由（preflight_archetype）

| 原型 | 特征 | 策略 |
|---|---|---|
| normal | 有 URL 且内容密度正常 | 常规管道 |
| pdf_shell | 页面带 PDF 引用（苏迪播放器/iframe/链接） | PDF 检测+下载+自适应提取 |
| empty_shell | 无 PDF 且内容样本标签密度≈0（北大） | 渲染兜底（DrissionPage/Playwright, 系级熔断）+ 空壳主导重试跳过 |
| name_list_no_links | ≥50% 无 URL（上外/中山） | 搜索式发现（Bing, 预算 ≤10 查询/系）+ 名单落盘 |

原型结论归档 site_registry.json，同校后续院系分析 warm-start。

## 3. 并发拓扑

| 层级 | 机制 | 控制 | 防封 IP |
|---|---|---|---|
| 院系级 | ProcessPoolExecutor（batch_run/cross_school_test） | --parallel（默认2/6） | 跨校=不同域名互不可见 |
| 教师级 | ThreadPoolExecutor（process_batch） | TEACHER_WORKERS（默认4） | 站点请求经节流门串行放行 |
| 请求层 | _throttle 节流门（进程内全局锁+时间戳） | RATE_LIMIT=0.3s | 每域名单条限速流 |
| 渲染层 | fetch_page_rendered（同节流门） | 每教师1次+系级熔断 | 代价高，预算控制 |

## 4. 失败语义与自愈

- 25 列契约: 12 元数据（含 失败原因/重试次数）+ 13 字段
- classify_record: ok / pending_pdf / failed / no_url / unknown（单一口径）
- 重试: _retry（RETRY_MAX 3, 指数退避）包住 fetch/PDF 下载
- LLM: 60s 超时×1 重试 + 熔断（连续≥3 超时 → 5 分钟快速失败走启发式）
- resume: RESUME=1 时跳过 ok/no_url，failed/pending 自动重试
- 批后自动回补: batch_run 每批结束 RESUME=1 重跑（AUTO_BACKFILL=1）
- 运行保护: SPIDER_TIMEOUT 6h + 停滞看门狗 15min + 进程组杀（无孤儿）

## 5. RSO 闭环（递归自优化）

G（agent 代码）→ P（院系产物）→ O（评估+批后回顾）→ M（改进归档+实施）→ G'
· Ω: quality_criteria.md（评估与回顾共同参照）
· 收敛判据: 连续 2 批回顾零新增 → 降频；新问题立即恢复
· 记忆: improvements.md（教训）+ site_registry.json（站点原型结论）

## 6. 关键文件

| 文件 | 职责 |
|---|---|
| dept_agent.py | 单院系管道编排（发现/预检/分析/评估/重试价值判断） |
| spider_gen.py | 抽样/LLM 分析/确定性生成/运行保护/预检 |
| detail_scraper.py | 核心采集: 抓取/自适应 PDF/渲染兜底/LLM+启发式/并发/重试 |
| faculty_discovery.py | 师资分类发现 + clean_name |
| search_rescue.py | 名单无链接站的搜索式 URL 补全 |
| batch_run.py | 分批编排 + 回顾 + 收敛 + 自动回补 |
| cross_school_test.py | 跨校并发测试器 |
| quality_criteria.md | Ω 规格 |
| site_registry.json | 站点原型注册表（运行时生成） |
| improvements.md | 教训与待办 |
