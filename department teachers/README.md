# Step 3 · 教师名录采集 Agent —— 院系师资页 → 姓名 + 详情页 URL

> 阶段定位：**四阶段流水线的第三站**。逐院系访问官网师资页，产出教师名录
> （姓名 + 个人详情页 URL）。从本阶段起进入「评估 → 修复 → 重评」完整 Agent
> 循环，数据产物**不随仓库分发**（开源边界止于 Step 2 院系 URL 层级）。

```
   Step 1                Step 2              Step 3 ★(当前)       Step 4
   学校信息采集    →      二级院系采集    →   教师名录采集    →    教师详情采集
   colleges info/        colleges department/ department teachers/  teachers/
   (全国主名单)          (院系+URL)           (姓名+详情URL)       (13字段)
                                            ★ 开源数据边界以上 ★
```

## 输入与产出

| | 说明 |
|---|---|
| **输入** | Step 2 产出：`colleges department/depart_985211.csv`（院系名称 + 官网 URL；亦兼容运行时生成的 `output/<学校>/departments.csv`） |
| **产出** | `output/<学校>/<院系>/teachers.csv`（运行时生成，`.gitignore` 排除） |
| **下游消费者** | Step 4 按 `detail_url` 逐教师下载详情页 |

## 核心不变量

> **teachers.csv 必须是运行 spider.py 的产物。**

每个院系目录满足：
- `teachers.csv` 有数据 ⟺ `spider.py` 存在且可运行
- 合法空院系（无师资/校园网限制）也有空 spider.py（运行产出表头 CSV）
- 评估器检查此不变量，违反即 FAIL

这条不变量保证任何数据都可以**复现**：spider 与产物同目录存放、带溯源 hash
（`spider_meta.json`），数据被外部回退后可检测并重跑恢复。

## Agent 设计（评估-修复-重评循环）

> 本阶段的工作流设计 = **五阶段主流水线（首次采集） + 评估-修复循环（质量收敛）+
> 测试-改进循环（同校渐进）** 三层嵌套，全流程见下。

### 全景工作流

```
depart_985211.csv（Step 2 院系清单）
        │
        ▼
┌─ 第一层: 主流水线 collect_phases.py（每院系首次采集）─────────┐
│  Phase 1  找师资页: 抓首页→按"师资队伍"词族匹配链接            │
│           (导航页排除/词变体长词优先/多候选打分)               │
│  Phase 2  LLM 阅读页面样本 → 设计提取规则 → 生成 spider.py    │
│           (BS4选择器+正则; 硬门禁不过打回; ≤80行)             │
│  Phase 3  运行 spider.py → teachers.csv（确定性执行, 零LLM）  │
│  Phase 3.5 LLM 抽查评估 CSV 质量（快速反馈）                  │
│  Phase 4  带原因修正（选择器错/漏分页/导航污染）              │
│  Phase 5  落盘 + spider_meta.json 溯源                        │
└──────────────────────────────────────────────────────────────┘
        │ FAIL 院系
        ▼
┌─ 第二层: 修复循环 fix_loop.py（≤3 轮/院系）──────────────────┐
│  evaluate_teachers.py 8维评估 → 结构化失败原因                │
│   → LLM 读原因+页面样本改进 spider → 重跑 → 重评               │
│   → 回滚保护(新版更差即恢复旧版) / LLM连败→确定性模板兜底      │
│   → 成功案例沉淀 knowledge/，失败案例供提示词注入              │
└──────────────────────────────────────────────────────────────┘
        │ 长跑仍不过（僵尸院系）
        ▼
┌─ 第三层: 改进循环 batch_test_loop / improve_zombie_loop ─────┐
│  取样→诊断→形态分类→修复→回归自我进化; 批后 LLM 回顾           │
│  把新失败形态固化成提示词规则/评估维度, 框架越跑越准           │
└──────────────────────────────────────────────────────────────┘
```

### 循环示意

```
                    ┌──────────────────────────────────┐
                    │         evaluate_teachers.py      │
   spider.py ──运行──▶ teachers.csv ──评估──▶ PASS ✅ / FAIL ❌
      ▲                                    │
      │                          FAIL 携带结构化原因列表
      │                                    ▼
      │                           fix_loop.py (≤3轮)
      │                     ├─ LLM 带原因改进 spider.py
      │                     ├─ 页面链接样本注入兜底
      │                     ├─ 回滚保护(新spider更差则恢复)
      │                     └─ 确定性模板重写(LLM连败)
      └──────────────────────────────────┘
```

### 主流水线 Phase 1-5（`collect_phases.py`）

单院系首次采集走五步：**Phase 1** 找师资页（导航页排除、词变体长词优先）
→ **Phase 2** LLM 分析页面结构生成 spider.py → **Phase 3** 运行验证
→ **Phase 3.5** LLM 评估 teachers.csv → **Phase 4** 带原因修正
→ **Phase 5** 输出。之后进入评估-修复循环做质量收敛。

### 自适应方法：LLM 阅读页面、设计规则，规则做提取

Phase 2 是「自适应」的落点。提示词注入四类**页面证据**让 LLM「阅读」，
而不是把整站 HTML 倾倒给它：

| 证据 | 量 | 作用 |
|---|---|---|
| DOM 容器结构 | ≤6 段 | 找姓名+链接所在的容器层级 |
| 链接样本 | ≤8 条 | 推断详情 URL 形态（`/info/数字` / 个人目录） |
| 页面正文 | 800 字 | 识别组织类型（学院/研究院/医院/继续教育） |
| 知识库提示 | 同校模式 + 历史失败案例 | warm-start，前人踩过的坑直接绕开 |

LLM 的产出**不是数据，是提取规则**——一份 ≤80 行的独立 `spider.py`：
BeautifulSoup 写死 CSS 选择器（容器→姓名→详情URL）+ 正则辅助
（URL 形态判定、职称后缀剥离、JS 跳转跟随）。规则是确定性代码：
跑一万遍结果一致、零 token、可人工审计修改。

**生成即检验（两层门禁）**——规则落地前先过检验，不过打回：

*生成时门禁（Phase 2 内，最多 3 次重生成）*：

- 页面有分页链接但 spider 无分页遍历 → 打回（LLM 常因「精简」跳过分页漏人）
- 输出非 JSON / spider_code 过短 → 打回

*运行时门禁（Phase 3，运行即验证）*：

- **URL 硬门禁**：页面有 ≥10 个教师级链接但 CSV 的 URL=0 → 打回（禁纯文本豁免；
  云大教训：纯文本名单把导航页当详情页提假字段）
- **覆盖率软提示**：URL>0 但 < 页面链接 30% → 记 issue 供修正循环感知（不硬打回防死循环）
- **表头净化**：LLM 输出形式漂移（stdout JSON / 2 列 / 英文表头）自动归一化标准 8 列
  （北大哲学系教训：修正版丢了 CSV 写入只 print JSON，只验证旧文件会让修正空转）
- **人名正则校验**：有效名（2-4 字中文）<80% 压分，<3 人直接不过

之后交给 8 维评估 + 修复循环闭环（见上文），全程只有生成与修复两处唤醒 LLM，
运行与复跑零 LLM 成本。

### 评估维度（evaluate_teachers.py，8 项）

| # | 维度 | 检测内容 |
|---|------|---------|
| 1 | 不变量 | 有CSV必有spider.py |
| 2 | 人名格式 | 2-4字中文 / 外国名字母形式 |
| 3 | 导航词 | 命中 320+ 词 NAV_WORDS |
| 4 | 专业方向词 | 合唱指挥/音乐理论/作曲理论等栏目词 |
| 5 | 名字粘连 | 车某技术=车某+"技术职称"截断 |
| 6 | URL结构 | DETAIL优先判定（详情特征白名单），机构页占比>50% 判"把栏目当教师" |
| 7 | 数量合理性 | 重抓师资页严格计数（只认详情URL特征），CSV<30% 判偏少 |
| 8 | 合法空豁免 | test_report.json 状态标记（no_teacher_org/no_faculty_page/no_url 等） |

评估模块化拆分：`eval_elements.py`（页面对照计数/纯文本严格审查）、
`eval_small.py`（小组织三查：翻页全走查/其他分类子页/单形态误判——
"怀疑漏采"不直接 FAIL，先核实）。

### 提取模式库（8 种，跨 4 校沉淀）

| 模式 | 特征 | 案例 |
|------|------|------|
| 详情链接 | `a` 文本2-4字中文 + `/info/数字` 等 | 北大/北工大/苏大 |
| 卡片容器 | `ul.dpzw_pic` / `[class*=list] li a` | 北航交通 |
| 纯文本名+职称 | 相邻行：姓名行 + 职称行 | 苏大文学院 |
| 序号模式 | 数字序号+姓名+方向+职称 4行组 | 苏大音乐学院 |
| doctor-item卡 | `[id^=doctor-item] .kszj_pic a` | 厦大眼科 |
| pic-item卡 | `div.pic-item .pic-img a` + `.pic-title01` | 厦大翔安 |
| 子页递归 | 师资页含硕导/博导/教研室子页 | 南医大影像 |
| PDF表格 | 教授名录是PDF（序号+单位+姓名+性别+职称+专业） | 南医大第四临床 |

模式存于 `knowledge/teacher_patterns.json`（`load_kb`/`record_case` 读写），
成功/失败案例跨院系复用——同校后采的院系可以命中前人模式直接生成更准的 spider。

## 文件结构

```
department teachers/
├── README.md                  ← 本文件
│
├── ★ Agent 核心（评估-修复-重评循环）
├── collect_phases.py          ← 主流水线（Phase 1-5 找师资页→生成→运行→评估→修正）
├── evaluate_teachers.py       ← 质量评估器（8 维度, 表头契约校验/LLM终审缓存）
├── fix_loop.py                ← 修复循环（断点续传/失败队列/知识库学习闭环）
├── fix_generic.py             ← 通用确定性采集器（LLM连败兜底）
├── batch_test_loop.py         ← 测试-评估-改进循环（同校小批量, 批后LLM回顾→改进落地）
├── improve_zombie_loop.py     ← 僵尸院系自我进化引擎（取样→诊断→修复→回归）
│
├── 模块拆分（2026-08-31 重构）
├── collect_teacher.py         ← 向后兼容入口（薄壳 re-export）
├── collect_constants.py       ← 领域常量（师资关键词/组织类型/免采组织）
├── collect_csv.py             ← teachers.csv 净化与姓名清洗
├── collect_kb.py              ← 知识库读写与案例沉淀
├── eval_elements.py           ← 评估维度：页面对照计数/纯文本审查
├── eval_small.py              ← 评估维度：小组织严谨核实（三查）
├── fix_improve.py             ← 生成器+优化器（R0 确定性 spider 薄壳）
├── fix_queue.py               ← 失败重排队列（跨批次尝试上限, zombie 终态）
├── _paths.py                  ← 路径引导（sys.path + .env 加载, import 前先导）
│
├── templates/
│   ├── spider_template.py     ← spider标准模板 + NAV_WORDS(320+词)
│   └── contract.py            ← ★ 数据契约（CSV表头/JSON结构/列名映射, 单一事实来源）
├── knowledge/
│   └── teacher_patterns.json  ← 页面结构模式知识库（自我进化）
├── util/group_words.py        ← 词组归组工具
├── script/                    ← 运维脚本（22 个）
│   ├── auto_resume.sh         ← 自愈流水线（断点续跑, 配 guard 防双启）
│   ├── night_watchdog.sh      ← 看门狗（卡死批次检测/浏览器回收）
│   ├── token_probe_loop.sh    ← LLM token 常驻探测
│   ├── progress_loop.sh / progress_report.py / rank_report.py ← 进度报表
│   ├── multi_school_batch.py / select_batch.py / launch_next_batch.sh ← 批次编排
│   ├── evaluate_all.py / evaluate.py / check_wave.py ← 批量体检/波期检测
│   ├── probe_site.py          ← BFS 全站探测 → analysis.md
│   ├── improve_cycle.py       ← 改进循环编排
│   ├── fix_pipeline.py / retry_ip_blocked.py / reap_cdp_tabs.py ← 修复工具
│   ├── error_dashboard.py / collection_stats.py / export_unfinished.py ← 统计看板
│   └── llm_health_probe.py / auto_keepalive.sh ← 健康探测
├── logs/                      ← 顶层日志/队列/报告（运行时生成）
└── output/                    ← ★ 产出（运行时生成, 不入库）
    └── <学校名>/
        ├── departments.csv    ← 从 Step 2 复制的院系列表
        ├── EVALUATION_LOOP.json ← 循环评估结果
        └── <院系名>/
            ├── spider.py      ← 采集脚本（数据唯一来源）
            ├── teachers.csv   ← ★ spider.py 运行产物（规范8列）
            ├── teachers.json
            ├── spider_meta.json ← 产物溯源 hash
            ├── analysis.md    ← 网页结构分析
            └── test_report.json
```

## 并发模型（院系级并行）

- **粒度 = 院系**: 每院系是独立文件域(spider/CSV/JSON/meta), 天然可并行; 页面级不并发
- **实现**: 线程池 + `--workers`(默认4, 环境变量 `AGENT_WORKERS` 覆盖),
  evaluate_teachers / fix_loop / probe_site 三处统一支持
- **安全改造**:
  - LLM 调用信号量(全局≤2并发, 防服务端限流)
  - LLM 终审缓存读写锁(防并发丢失更新)
  - smart_fetch DrissionPage 单例锁(并发串行化浏览器访问)
- **实测**: 厦门大学 125 院系全量评估 串行~40min → 并行~5min (约8倍)

## 试点验证结果（评估-修复-重评循环）

| 学校 | PASS | FAIL | SKIP | 合法空 | 教师数 | 不变量违反 |
|------|-----:|-----:|-----:|-------:|-------:|-----------:|
| 苏州大学 | 37 | 0 | 3 | 10 | 2,149 | 0 |
| 厦门大学 | 5 | 0 | 1 | 17 | 101 | 0 |
| 南京医科大学 | 22 | 0 | 5 | 28 | 1,364 | 0 |
| **合计** | **64** | **0** | **9** | **55** | **3,614** | **0** |

- **SKIP**：源数据无URL（上游 Step 2 待补）
- **合法空**：行政机构/学部/无师资板块/校园网限制/留学生培训机构，已标记留档

## 运行方式

在**仓库根目录**执行（脚本内部通过 `_paths.py` 自定位，也可进入本目录运行）：

```bash
# ── 主流水线：采集（Phase 1-5） ──
uv run --project . python "department teachers/collect_phases.py" --school 苏州大学
uv run --project . python "department teachers/collect_phases.py" --school 苏州大学 --dept 商学院

# ── 评估-修复-重评循环（Agent 核心） ──
uv run --project . python "department teachers/evaluate_teachers.py" 苏州大学            # 评估
uv run --project . python "department teachers/evaluate_teachers.py" 苏州大学 --json     # +写EVALUATION_LOOP.json
uv run --project . python "department teachers/fix_loop.py" 苏州大学                      # 修复所有FAIL(≤3轮)
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --dept 商学院       # 修复单院系
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --resume            # 跳过已PASS(.fix_done)
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --queue             # 只处理失败队列
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --clear-queue       # 清空失败队列

# ── 测试-评估-改进循环（同校小批量渐进完善） ──
uv run --project . python "department teachers/batch_test_loop.py" 苏州大学 --batch-size 5
uv run --project . python "department teachers/batch_test_loop.py" 苏州大学 --from-fix-queue --resume

# ── 确定性采集（LLM 连败兜底） ──
uv run --project . python "department teachers/fix_generic.py" --school 南京医科大学

# ── 运维（长跑时使用, 详见各脚本头部注释） ──
bash "department teachers/script/auto_resume.sh"        # 自愈流水线
bash "department teachers/script/night_watchdog.sh"     # 看门狗
```

## 技术栈

| 组件 | 方案 | 位置 |
|------|------|------|
| LLM | SimpleLLM（单 token / token 池双模式） | `util/llm_client.py` |
| HTTP | util/smart_fetch 五级降级 | 仓库根 `util/` |
| HTML 解析 | BeautifulSoup4 + lxml | - |
| PDF 解析 | pypdf（教授名录PDF表格） | spider.py 内联 |
| 导航词过滤 | NAV_WORDS 集合（320+ 词） | `templates/spider_template.py` |
| 数据契约 | CSV 表头/JSON 结构单一事实来源 | `templates/contract.py` |
