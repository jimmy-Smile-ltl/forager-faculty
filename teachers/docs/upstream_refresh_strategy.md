# 上游数据更新 → HTML 补充下载策略（2026-09-11）

## 目标

上游 `department teachers/output/` 名单更新后，把教师详情页 HTML 补到本地
`tearchers raw data/`，为下一阶段信息提取提供输入。两种模式：

| 模式 | 语义 | 用途 |
|------|------|------|
| `test` | 每院系补到 **20 个** HTML（缺口=20-现有，只下缺文件教师） | 信息提取阶段测试（本地样本门控 ≥5 即够，20 留裕量） |
| `full` | **全量下载**——名单里所有有效 URL，本地已有（文件名 md5 命中）跳过 | 正式采集 |

## 核心原则

1. **增量语义**：文件名 = `姓名_md5(URL).html`。URL 变更 → md5 变 → 补下；
   本地已有 → 秒过不碰网络。天然支持重复运行/断点续跑。
2. **下载前过滤名单**（本次新增，见下节）：非教师条目（导航词/栏目词/机构名/
   非人名形态/坏 URL/列表页 URL）直接剔除，不计缺口、不发请求。
3. **并发封顶 20 路**（4 院系 × 5 教师）：2026-09-11 机器卡死教训（40 并发
   致强制重启），下载期并发上限 20。
4. **单系预算 6 分钟**：限速站（people.ucas 类）超时即搁置，落多少算多少，
   后续轮次续补；不做专项硬啃（石河子教训）。
5. **死系台账**：test 模式忽略死系 TTL（补 20 个测试样本，成本可控）；
   full 模式尊重死系 TTL，避免每轮为死系白耗预算。

## 名单过滤（是不是人名）

过滤链（依次）——**拿不准的一律放行**，宁可放过让下载/提取去判，不误杀真教师：

1. `_is_junk_name`：导航词/栏目词/非人名词子串（已有，西交"正版软件"、
   上财"联系电话"类）
2. `_is_person_name`（新增形态判定）：
   - 含数字/ASCII 字母 → 拒（"2023级研究生"）
   - 含句读括号等标点 → 拒（民族名 `·` 除外：`阿不来提·阿不都热西提` 放行）
   - 纯汉字但 <2 字或 >8 字 → 拒（机构长名）
   - 机构后缀结尾（学院/学部/书院/委员会/研究院/研究所/实验室/办公室/
     教研室/工会/团委）→ 拒；**刻意不含"中心/建设/管理"**（张建设类真名防误杀）
3. URL 判定：非 http(s) → 拒；列表页形态（/list.htm、/szdw.htm 等）→ 拒

过滤结果落 `logs/_upstream_filtered.tsv`，可回查、可反馈上游。

## 更新检测（哪些要下）

以「上游 output 目录 vs 本地状态」差集为准：

- 新院系：上游有、本地无目录 → 全量纳入
- 新教师/新 URL：本地 raw 目录按文件名前缀（姓名_md5）比对名单 → 缺文件即缺口
- 已有文件：跳过

## 执行流程

```bash
# 1) 更新检测 + 名单过滤报告（只统计，不下载）
uv run python scripts/upstream_refresh.py --mode test --dry-run
uv run python scripts/upstream_refresh.py --mode full --dry-run

# 2) 测试模式：每院系补到 20
uv run python scripts/upstream_refresh.py --mode test

# 3) 全量模式：所有有效 URL 补全（死系尊重 TTL）
uv run python scripts/upstream_refresh.py --mode full

# 指定清单/单校
uv run python scripts/upstream_refresh.py --mode full --list logs/_new_upstream_depts.tsv
uv run python scripts/upstream_refresh.py --mode full --school 北京大学
```

## 与下游衔接

- 下载后 `extract_stage.py` 以 `download_done.flag` 门控提取；
  本地样本门控（EXTRACT_MIN_SAMPLES=5）自动放过 html≥5 的院系
- 提取结果评估（职称/方向可判页 + 疑似漏提）闭环已有
- 每轮上游更新后：跑一次 `--dry-run` 看缺口 → 选模式执行 → 提取阶段消化
