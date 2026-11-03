# Forager Faculty 🎓

**简体中文** | **[English](README_EN.md)**

![Python](https://img.shields.io/badge/python-3.14%2B-blue.svg)
![Node](https://img.shields.io/badge/node-24-brightgreen.svg)
![uv](https://img.shields.io/badge/uv-managed-purple.svg)
![Tests](https://img.shields.io/badge/tests-424%20passed-brightgreen.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

> **Forager**（觅食者）—— 像觅食的蜜蜂根据花蜜信号调整路径一样，这个
> LLM 驱动的自适应采集框架根据评估器的失败原因信号，迭代修正提取规则。
> 采集对象是**高校教师**：四阶段流水线（学校 → 院系 → 教师名录 → 教师详情）
> 采集中国高校官网公开发布的教师队伍信息，核心是一个
> **「评估 → 修复 → 重评」闭环**：让 Agent 自己发现采集质量问题、
> 携带结构化原因迭代修复 spider，而不是靠人肉抽查和改规则。
>
> **开源数据边界**：本仓库只分发到「院系 URL 清单」层级（学校属性 + 院系名称/官网 URL）。
> 教师名录、教师详情的**采集框架与机制完整保留**，但其数据产物不随仓库分发，见
> [数据边界](#数据边界)。全量运行（144 校）的聚合统计见
> [docs/采集结果统计报告.md](docs/采集结果统计报告.md)。

## 目录

- [特性一览](#特性一览)
- [架构总览](#架构总览)
- [核心机制](#核心机制)
  - [评估-修复-重评循环](#1-评估-修复-重评循环agent-内核)
  - [自适应提取规则](#1b-自适应提取规则llm-设计规则规则做提取)
  - [自适应请求栈](#2-自适应请求栈util)
  - [站点原型路由](#3-站点原型路由step-4-预检)
  - [LLM 接入双模式](#4-llm-接入单-token--动态-token-池)
- [快速开始](#快速开始)
- [安全与数据边界](#安全与数据边界)
- [数据边界](#数据边界)
- [目录结构](#目录结构)
- [免责声明](#免责声明)
- [许可证](#许可证)

## 特性一览

| | 特性 | 说明 |
|---|---|---|
| 🔁 | **评估驱动的质量闭环** | 8 维评估器给每份产物打分，FAIL 携带结构化原因进入修复循环（≤3 轮），回滚保护 + 确定性模板兜底 |
| 🧩 | **自适应任意站点结构** | LLM 阅读页面样本（DOM 结构/链接/文本）**设计提取规则**——产出 BeautifulSoup 选择器 + 正则的确定性 spider，而非逐页喂给 LLM 提取；规则零 token 可复跑、结果可复现，没见过的网站也能跑 |
| 📝 | **实战沉淀的提示词体系** | 每条提示词硬规则背后是一类实测翻车现场；修复循环发现新失败模式即固化为提示词规则，与知识库联动持续进化 |
| 🌐 | **五级降级请求栈** | `curl_cffi` TLS 指纹 → `iv8`/`nodenv`/`sdenv` 纯 CPU 破瑞数 JS 挑战 → 浏览器渲染 → CDP 常驻实例，逐级自动降级 |
| ✅ | **检验结果，绝不假成功** | 每份产物必须过评估器（8 维 + 硬门禁）才算数：FAIL 携带结构化原因驱动重试（≤3 轮）+ 回滚保护；144 校院系采集 100% 通过率、8,049 院系平均 99.3 分；424 项离线单测守护框架本身 |
| 🔑 | **LLM 双模式接入** | 默认单 token 直连零配置；可选 token manager 动态池：失效自动摘除、轮询分配、sticky 会话 |
| 🛡 | **工程化运行时** | LLM 熔断器、系统资源三档守卫、看门狗自愈、失败院系冻结账本 |

## 架构总览

### 四阶段流水线

```
Step 1  学校信息 ── colleges info/
  │      输入: 教育部名单 / 掌上高考 / 学信网（公开源）
  │      产出: unified_colleges.csv（2,952 校 · 34 列公开属性）
  ▼
Step 2  二级院系 ── colleges department/
  │      输入: 学校官网 → orchestrator.py（分析→生成 spider→运行→评估→修复）
  │      产出: depart_985211.csv（144 校 · 8,049 院系: 名称 + 官网 URL）
  │      ★★★ 开源数据边界止于此处 ★★★
  ▼
Step 3  教师名录 ── department teachers/
  │      输入: 院系官网师资页 → collect_phases.py（Phase 1-5）
  │      产出: output/<学校>/<院系>/teachers.csv（姓名 + 详情页 URL，运行时生成）
  ▼
Step 4  教师详情 ── teachers/
         输入: Step 3 名单 → download_stage.py（名单 QC + 差分下载）
         产出: teachers_detail.csv（13 字段，运行时生成）+ 原始 HTML/PDF 存档
```

| 阶段 | 目录 | 职责 | 入口 |
|---|---|---|---|
| Step 1 | `colleges info/` | 三源融合出全国高校主名单 | 各子目录采集脚本 |
| Step 2 | `colleges department/` | 每校官网提取二级院系 + URL | `orchestrator.py` |
| Step 3 | `department teachers/` | 院系师资页 → 教师名录 | `collect_phases.py` / `evaluate_teachers.py` / `fix_loop.py` |
| Step 4 | `teachers/` | 教师个人页 → 13 字段结构化详情 | `download_stage.py` / `extract_all_details.py` |

阶段间的衔接是**纯文件契约**：上游产物 CSV 即下游唯一输入（`templates/contract.py`
是列名的单一事实来源）。任何阶段都可以单独重跑——spider 脚本与数据产物同目录
存放并带溯源 hash，评估器据此保证「有数据 ⟺ 有可运行的 spider」。

### 每个阶段都是同一个 Agent 循环

四个阶段共享同一种工作模式，只是评估维度和 spider 形态不同：

```
生成 spider → 运行 → 评估器多维度打分
    ▲                        │
    │                   PASS ✅ 产出落盘
    │                   FAIL ❌ 携带结构化原因
    │                        ▼
    └──── LLM 修复 spider ←──┘   （≤3 轮；连败走确定性兜底模板）
```

- **Step 2**：`orchestrator.py` —— 页面结构分析 → LLM 生成院系 spider → 运行 → 评估院系清单
- **Step 3**：`collect_phases.py`（找师资页 → LLM 生成 spider → 运行验证 → LLM 评估 → 带原因修正）
  → `evaluate_teachers.py` 8 维评估 → `fix_loop.py` 修复循环
- **Step 4**：`download_stage.py` 名单 QC → `extract_all_details.py` LLM 优先提取（规则备选）
  → 13 字段交叉校验

## 核心机制

### 1. 评估-修复-重评循环（Agent 内核）

以 Step 3 为例（`department teachers/`，其余阶段同构）：

```
 spider.py ──运行──▶ teachers.csv
     ▲                    │
     │                    ▼
     │          evaluate_teachers.py 评估器（8 维度）
     │             ├─ PASS ✅ → 落盘 + 标记 .fix_done
     │             └─ FAIL ❌ → 产出结构化失败原因
     │                    │
     │                    ▼
     │          fix_loop.py 修复循环（≤ 3 轮）
     │             ├─ LLM 读取失败原因 + 页面样本 → 改进 spider
     │             ├─ 回滚保护: 新 spider 效果更差则立即恢复旧版
     │             ├─ LLM 连败 → 确定性模板兜底（fix_generic.py）
     │             └─ 成功案例沉淀进知识库，跨院系复用
     │                    │
     └──── 修复后的 spider ◀┘
              （回到运行 → 重评，直到 PASS 或合法空终态）
```

**8 维评估**（FAIL 不只是布尔值，每条都带可执行的原因）：

| # | 维度 | 检测内容 |
|---|------|---------|
| 1 | 不变量 | 有 CSV 必有可运行的 spider |
| 2 | 人名格式 | 2-4 字中文 / 外教字母名 |
| 3 | 导航词 | 命中 320+ 词 NAV_WORDS（把栏目名当人名） |
| 4 | 专业方向词 | 合唱指挥/音乐理论等栏目词混入 |
| 5 | 名字粘连 | 「车某技术」=「车某」+「技术职称」截断 |
| 6 | URL 结构 | 机构页占比过高 = 把栏目页当教师页 |
| 7 | 数量合理性 | 重抓师资页严格计数，CSV < 30% 判偏少 |
| 8 | 合法空豁免 | 行政机构/无师资板块等标记为合法空，不误杀 |

**为什么这样设计**：爬虫的失败模式千奇百怪（导航词当人名、字段粘连、翻页漏采、
JS 壳页、PDF 名册……），规则枚不完。评估器把「什么叫采对了」形式化，LLM 只负责
「读原因改代码」这一件事——职责分离让修复循环可以无人值守地整校整校地跑。

**提示词是沉淀出来的，不是写出来的**。每条 `【2026-08-30 加强】` 式的规则背后
都是一类实测翻车现场（清华 AI 学院把 `title` 属性当姓名漏采、整页倾倒进单字段、
历史职称当现任职称……）。提示词模板与失败案例知识库（`knowledge/`）联动：
修复循环每发现一类新失败模式，就把它固化成提示词硬规则，下个院系直接绕坑——
**框架因此能自适应从未见过的网站结构，而不是只对写代码时见过的站点有效**。

### 1b. 自适应提取规则：LLM 设计规则，规则做提取

「自适应阅读 HTML」的落点是**生成器而非提取器**。对每个新院系：

```
师资页 HTML
  │  ① 取样: DOM 容器结构（≤6 段）+ 链接样本（≤8 条）+ 正文文本（800 字）
  ▼
  ② LLM 阅读样本 → 设计提取规则 → 生成 spider.py
  │     · BeautifulSoup CSS 选择器（容器 → 姓名 → 详情 URL）
  │     · 正则辅助（URL 形态判定、职称剥离、分页链接识别）
  │     · ≤80 行独立脚本，确定性执行、零 token 复跑
  ▼
  ③ 硬门禁（生成即检验，不过打回重生成）
  │     · 页面有分页链接但 spider 无分页遍历 → 打回
  │     · 运行后：页面有 ≥10 教师级链接但 CSV 的 URL=0 → 打回（禁纯文本豁免）
  │     · 人名正则校验：2-4 字中文有效名 <80% 压分；表头漂移自动归一化
  ▼
  ④ 运行 → teachers.csv → 8 维评估器检验 → 不过带原因重试（≤3 轮）
```

**为什么 LLM 生成规则、而不是 LLM 逐页提取**：

| | LLM 生成规则（本框架） | LLM 逐页提取 |
|---|---|---|
| 一个 300 人院系的 token 成本 | 1 次 spider 生成 | 300 次页面提取 |
| 重跑 / 增量补采 | 零 LLM 成本，本地复现 | 每页重新付费 |
| 结果稳定性 | 确定性脚本，同输入同输出 | 每次采样有波动 |
| 可审计 | 规则是代码，人能读能改 | 提取过程黑盒 |
| 质量责任 | 评估器盯住 spider，可回滚可修复 | 逐页纠错无抓手 |

（例外：Step 4 详情页提取确实是 LLM 逐页提取——13 字段深语义理解没有稳定规则可写；
即便如此学院级规则仍是 LLM 不可用时的备选，见 `teachers/README.md`。）

提取规则跨院系沉淀为**模式库**（`knowledge/teacher_patterns.json`，8 种页面形态：
详情链接 / 卡片容器 / 纯文本名+职称 / 序号四行组 / PDF 表格册 / 子页递归……），
生成 spider 时注入同校历史案例做 warm-start——后采的院系直接命中前人模式。

### 2. 自适应请求栈（`util/`）

统一入口 `smart_fetch(url)`，五级自动降级，返回 `(html, method)` 标记命中层级：

```
 URL
  │  ① curl_cffi ——— 浏览器 TLS 指纹模拟（chrome131），无 WAF 站点一步直达
  │  ② iv8 ——————— 纯 CPU V8 补环境，破应用层瑞数 JS 挑战（优先产 cookie）
  │  ③ nodenv / sdenv — Node 补环境（原生零依赖 / jsdom 完整版），瑞数全站点
  │  ④ DrissionPage / Playwright —— headless 渲染 + 瑞数兜底
  │  ⑤ CDP 桥接常驻浏览器（:9222）—— 网络层瑞数 + JS 全渲染
  ▼
 (html, method)   —— 全部失败返回 blocked；系统 curl 作 TLS 指纹兜底
```

配套组件：

| 组件 | 位置 | 作用 |
|---|---|---|
| 瑞数挑战处理 | `handleRuishu.py` / `rs_cookie.py` / `iv8_rs.py` | 412/202、`$_ts` 特征码自动检测，cookie 按域缓存 + 失效重生成 |
| TLS 指纹轮换 | `fingerprint_chain.py` | chrome/firefox/safari 多指纹切换 |
| 浏览器池 | `drission_pool.py` / `cdp_util.py` | 窗口×标签池、CDP 常驻复用、空闲回收（`browser_lifecycle.py`） |
| 熔断与守卫 | `llm_gate.py` / `resource_guard.py` | LLM 滑动窗口熔断；系统负载绿/黄/红三档降并发 |

> 瑞数对抗的完整设计——三层对抗模型、三条纯 CPU 求解器（iv8 / nodenv / sdenv）
> 的原理对比、cookie 生命周期、并发安全与排查手册——见专门文档
> **[docs/瑞数反爬解决方案.md](docs/瑞数反爬解决方案.md)**。

### 3. 站点原型路由（Step 4 预检）

教师个人页的「坑」是有限家族。Step 4 下载前先对每个院系做 preflight 归类，
按原型路由到不同策略，避免一把梭：

| 原型 | 特征 | 策略 |
|---|---|---|
| `normal` | 有 URL 且内容密度正常 | 常规管道 |
| `pdf_shell` | 页面带 PDF 引用（播放器/iframe/链接） | PDF 检测 + 下载 + 自适应提取 |
| `empty_shell` | 内容样本标签密度 ≈ 0（JS 壳页） | 渲染兜底 + 空壳主导时跳过重试 |
| `name_list_no_links` | ≥50% 教师无详情 URL | 搜索式发现（百度/Bing，≤10 查询/系）+ 名单落盘 |

### 4. LLM 接入：单 token / 动态 token 池

| | 模式 A（默认）：单 token | 模式 B（可选）：token 池 |
|---|---|---|
| 配置 | `.env` 填 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `token manager/tokens.json` 填多个 key |
| 兼容 | Anthropic / DeepSeek / 任意 OneAPI 风格网关 | 同左 |
| 失效处理 | 报错即停 | key 失败自动切下一个；每月配额探测、失效自动摘除 |
| 分配策略 | — | 轮询 + 最少消耗优先；sticky 会话（同院系绑同 key，利于 prompt cache） |
| 后端 | — | Redis 原子轮询（`redis://localhost:6379/0`）；单机可退 JSON 文件 |

```bash
cp "token manager/tokens.example.json" "token manager/tokens.json"
# 编辑填入多个 key，然后：
uv run --project . python "token manager/token_pool.py" check      # 探测所有 key 配额
uv run --project . python "token manager/token_pool.py" list       # 查看 token 状态
```

LLM 客户端（`util/llm_client.py`）检测到 `tokens.json` 存在即**自动启用池模式**，
无需改任何代码。

> LLM 层的完整设计——密钥/地址/模型解析优先级、失效切换与错误分类、令牌池
> 三级分配策略（会话固定/学院亲和/最少消耗）、探测与自愈、双层并发信号量、
> 熔断器——见专门文档 **[docs/LLM接入与令牌池.md](docs/LLM接入与令牌池.md)**。

## 快速开始

```bash
# 0. 环境要求: Python >= 3.14（uv 管理）、Node.js 24（仅瑞数 sdenv 路径需要）
uv sync
npm ci                # 仅 util/rsenv/ 瑞数补环境需要，不用该功能可跳过

# 1. 配置（模式 A：填三项即可跑通主体）
cp .env.example .env  # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 2. Step 3 教师名录（示例：苏州大学全部院系；--dept 商学院 可采单院系）
uv run --project . python "department teachers/collect_phases.py" --school 苏州大学
uv run --project . python "department teachers/evaluate_teachers.py" 苏州大学   # 评估
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --resume   # 修复 FAIL

# 3. Step 4 教师详情（下载 + 提取）
uv run --project . python teachers/download_stage.py --all --teacher-workers 5
uv run --project . python teachers/extract_all_details.py

# 4. 单元测试（无网络 / 无 LLM / 无浏览器）
uv run --project . python -m unittest discover -s tests
```

产物落盘在 `department teachers/output/<学校>/<院系>/teachers.csv` 与
`teachers/<学校>/`（均已 `.gitignore`，不会误提交）。所有可调参数见
`.env.example`（约 50 项，按 LLM / 代理 / 浏览器 / 反爬 / 资源守卫 / 流水线分节，
含注释）。

## 安全与数据边界

**发布前安全审计**（本仓库开源前逐项执行过）：

| 检查项 | 结果 |
|---|---|
| 密钥泄漏（`sk-`、API key、token、password 全仓扫描） | ✅ 零命中；无任何真实密钥，全部由 `.env` / `tokens.json`（均 gitignore）注入 |
| 内网地址（`*.baidu-int.com` / 内网 IP 段） | ✅ 零命中；网关地址仅通过 `LLM_BASE_URL` 环境变量配置，代码无默认值 |
| 教师个人信息（测试夹具、知识库、示例数据） | ✅ 全部化名/占位处理（`某教师`、`example.edu.cn`、真实电话清零） |
| 本机路径硬编码（`/Users/...`） | ✅ 零命中；全部 `Path(__file__)` 推导 |
| 日志与控制台输出 | ✅ 密钥一律脱敏（前 8 位 + `...` + 后 4 位，`mask_sk`） |

**数据分级**：仓库分发物止步于「院系 URL 清单」；教师级数据（名录/详情）只能由
使用者自行运行框架采集，且框架产物目录全部 gitignore，不存在误提交路径。

## 数据边界

| 层级 | 是否随仓库分发 |
|---|---|
| 学校信息（名称/主管部门/985/211/排名等公开属性） | ✅ `colleges info/unified_colleges.csv` |
| 院系清单（学校 → 院系名称 + 院系官网 URL） | ✅ `colleges department/depart_985211.csv` + `depart_985211.jsonl` |
| 教师名录（姓名 + 详情页 URL） | ❌ 不分发；运行 `department teachers/` 框架自行采集 |
| 教师详情（13 字段 + 原始页面） | ❌ 不分发；运行 `teachers/` 框架自行采集 |

所有运行时输出目录（`colleges department/output/`、`department teachers/output/`、
`teachers/<学校>/`）均在 `.gitignore` 中排除——**spider 脚本、评估报告、采集数据
一律在运行时生成，不随仓库分发**；仓库只包含经过验证的采集框架本身。

## 目录结构

```
├── colleges info/             # Step 1: 学校信息（三源融合, 2,952 校）
├── colleges department/       # Step 2: 二级院系（框架 + 8,049 院系 URL 清单——开源数据边界）
├── department teachers/       # Step 3: 教师名录采集 Agent
│   ├── collect_phases.py      #   主流水线（找师资页→生成 spider→运行→评估→修正）
│   ├── evaluate_teachers.py   #   8 维评估器
│   ├── fix_loop.py            #   修复循环（回滚保护/断点续传/知识库学习）
│   ├── fix_generic.py         #   确定性采集器（LLM 连败兜底）
│   ├── templates/             #   spider 模板 + 数据契约（contract.py）
│   ├── knowledge/             #   页面结构模式知识库（自进化）
│   └── script/                #   编排/自愈/看门狗/报表脚本
├── teachers/                  # Step 4: 教师详情采集 Agent
│   ├── download_stage.py      #   下载阶段（名单 QC + 差分下载 + 原型路由）
│   ├── extract_all_details.py #   提取阶段（LLM 优先, 规则备选）
│   ├── detail_scraper.py      #   核心采集器（PDF/渲染兜底/分类）
│   └── scripts/               #   批量差分引擎 / 院系规则工具
├── token manager/             # LLM token 池（可选负载均衡, 含示例结构）
├── util/                      # 共享请求栈（smart_fetch 五级降级/瑞数/浏览器池/LLM 网关）
├── tests/                     # 424 项单元测试（离线可跑）
├── .env.example               # 配置模板（约 50 项, 分节注释）
└── pyproject.toml             # uv 项目定义
```

各阶段内部的设计细节见各自目录下的 `README.md` / `DESIGN.md`。

## 免责声明

- 本框架仅用于采集**高校官网公开发布**的信息，请遵守目标站点的
  robots.txt、服务条款与当地法律法规；采集频率与并发请保持克制
  （框架内置了请求节流与资源守卫，请不要调高到对站点造成压力的程度）
- 教师个人信息受法律保护（如《个人信息保护法》）。使用本框架采集到的
  任何个人信息，其存储、使用与分享的责任由使用者自行承担；
  **请勿将采集结果用于违反法律或侵犯他人权益的用途**
- 仓库不包含也不分发任何已采集的教师个人数据；`token manager/`
  只包含池管理代码与示例结构，不含任何真实密钥

## 许可证

[MIT](LICENSE)
