# LLM 接入与令牌池 — 设计文档与排查手册

> **English summary**: This document details how the LLM layer handles **tokens
> and API URLs** — two integration modes (Mode A: single token via `.env`
> `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`, the default; Mode B: a dynamic
> token-pool manager activated automatically when `token manager/tokens.json`
> exists). Key designs: environment variables are the authoritative source for
> key/base_url/model (12-factor); a three-way error classification decides
> "switch key" (quota/invalid) vs "switch model" (timeout/gateway error) vs
> "give up" (connect errors); the pool allocates keys via a 3-tier strategy
> (session-sticky `prefer` → department-affinity `sticky` md5 slot →
> least-used + atomic round-robin), so repeated calls within a task hit the
> gateway's prompt cache; probing self-heals stale states (force reprobe with
> 300s throttle, monthly quota refresh); concurrency is capped by a two-layer
> semaphore (per-token × global dynamic); a sliding-window circuit breaker with
> exponential backoff rides out gateway fluctuation waves. Includes a config
> reference and a symptom→fix troubleshooting manual.

> 本文档解释本仓库的 LLM 层如何处理 **token 与 API URL**：单 token 直连（默认）与
> token manager 动态令牌池（可选）两种模式、密钥/地址/模型的解析优先级、失效
> 切换与自愈、并发控制与熔断。所有机制以 `util/llm_client.py`、
> `token manager/token_pool.py`、`util/llm_gate.py` 代码为准。

**安全底线**：本仓库不含任何真实密钥或网关地址。所有 key / base_url 均由使用者
通过 `.env`（复制 `.env.example`）或 `token manager/tokens.json` 自行配置；
`tokens.json`、`.env`、`util/cookies/` 均在 `.gitignore` 排除之列，全部控制台
与日志输出都经 `mask_sk` 脱敏（前 8 位 + `...` + 后 4 位）。

## 目录

- [双模式总览](#双模式总览)
- [模式 A：单 token 直连](#模式-a单-token-直连)
- [模式 B：token manager 动态令牌池](#模式-btoken-manager-动态令牌池)
- [密钥 / 地址 / 模型解析优先级](#密钥--地址--模型解析优先级)
- [一次 ask() 的完整链路](#一次-ask-的完整链路)
- [失效切换与错误分类](#失效切换与错误分类)
- [令牌池分配策略](#令牌池分配策略)
- [探测与自愈](#探测与自愈)
- [并发控制（双层信号量）](#并发控制双层信号量)
- [熔断器](#熔断器)
- [供应商与模型降级链](#供应商与模型降级链)
- [配置速查](#配置速查)
- [排查手册](#排查手册)

## 双模式总览

```
调用方 (fix_loop / evaluate / spider 生成 …)
   │
   ▼
util/llm_gate.py ── llm_ask_sem / llm_ask_with_timeout   ← 超时 + 退避重试 + 熔断
   │
   ▼
util/llm_client.py ── SimpleLLM.ask()
   │
   ├─ 模式 B（tokens.json 存在时自动启用）
   │    token manager/token_pool.py ── TokenPool.get(prefer, sticky)
   │    失效自动换 key · 配额探测 · 用量记账 · sticky 会话亲和
   │
   └─ 模式 A（默认 / 池不可用或全耗尽时兜底）
        .env 环境变量 key（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）
```

| | 模式 A（默认）：单 token | 模式 B（可选）：token 池 |
|---|---|---|
| 配置 | `.env` 三项：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `token manager/tokens.json` 填多个 key（复制 `tokens.example.json`） |
| 启用方式 | 填 `.env` 即用 | **tokens.json 存在即自动启用**（`SimpleLLM._ensure_pool()` 懒加载，无需改代码） |
| 兼容 | Anthropic / DeepSeek / 任意 OneAPI 风格网关 | 同左 |
| 失效处理 | 报错 → degraded JSON | key 失效自动切下一个；配额探测、失效自动摘除 |
| 分配策略 | — | 会话固定 + 学院亲和（sticky）+ 最少消耗优先 + 轮询 |
| 后端 | — | Redis（原子轮询）→ 单机退 JSON 文件；Redis 模式自动双写镜像回 tokens.json |

模式 B 下仍建议保留 `.env` 的 `LLM_API_KEY` 作为兜底——池整体不可用
（Redis 挂 / 全部 key 耗尽）时自动回退模式 A 路径。

## 模式 A：单 token 直连

`.env`：

```bash
LLM_API_KEY=sk-你的密钥
LLM_BASE_URL=https://你的网关/   # 留空 = Anthropic 官方地址
LLM_MODEL=deepseek-chat          # 按你的网关支持填写
```

- 支持 Anthropic 原生（thinking 模式自动启用，仅当 base_url 是官方地址）与
  DeepSeek / OpenAI 兼容 API（`agent_config.yaml` 里 `provider: deepseek` 切换）。
- 未配 key 时客户端进入 **degraded 模式**：`ask()` 返回
  `{"degraded": true, ...}` JSON，上层评估器按失败处理并走确定性兜底——
  不会假装成功。

## 模式 B：token manager 动态令牌池

```bash
cp "token manager/tokens.example.json" "token manager/tokens.json"
# 编辑填入多个 key（结构: name / key / status / used），然后：
uv run --project . python "token manager/token_pool.py" check      # 全量探测配额
uv run --project . python "token manager/token_pool.py" list       # 查看状态（脱敏）
uv run --project . python "token manager/token_pool.py" usage      # 用量统计
```

存储后端（`--store` 可切换）：

| 后端 | 用法 | 特点 |
|---|---|---|
| Redis（默认） | `redis://localhost:6379/0` | 轮询计数用 `INCR` 原子操作，跨进程安全；状态带 TTL（下月 1 日自动过期，月初额度刷新自愈） |
| JSON 文件 | 传文件路径（如 `tokens.json`） | 单机 fallback；临时文件按 PID 隔离 + 原子替换，多进程共写不损坏 |
| 镜像双写 | Redis 模式自动开启 | 主 Redis + 镜像 `token manager/tokens.json`（进程退出/Redis 挂后状态不丢） |

## 密钥 / 地址 / 模型解析优先级

三个 provider 路径（Anthropic / DeepSeek）解析规则一致——**环境变量是权威来源
（12-factor），配置文件仅作补充**：

| 项 | 解析顺序（高 → 低） |
|---|---|
| API key | `ask()` 参数（池分配的 key）> `LLM_API_KEY` > `agent_config.yaml` > provider 原生变量（`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY`） |
| Base URL | `LLM_BASE_URL` > provider 原生变量（`ANTHROPIC_BASE_URL`）> `agent_config.yaml` > DeepSeek 官方默认 |
| 模型 | `LLM_MODEL` > `ANTHROPIC_MODEL` > `agent_config.yaml` > 默认（`claude-sonnet-4-6` / `deepseek-chat`） |

> Base URL 环境变量优先是实测教训：旧顺序让配置文件地址覆盖 `.env`，
> 换环境部署时 `.env` 改了却不生效，直报 Connection error。

HTTP 客户端两条硬规则：

- **`trust_env=False`**：LLM 请求强制直连，不读 `http_proxy`/`https_proxy`
  ——代理只用于站点抓取层，网关走代理会绕路/失败。
- **read 超时 = None，connect = 15s**：网关超时多数是「output 多、一时半会儿
  吐不出来」（慢生成），无限等；真连不上 15s 快速失败——两类错误处理路径
  完全不同。`LLM_SDK_TIMEOUT` 可显式覆盖 read。

## 一次 ask() 的完整链路

```
SimpleLLM.ask(system, prompt)
  │ ① _ensure_pool()：tokens.json 存在 → 启用池（双检锁，多线程首调安全）
  │ ② 池路径 _ask_with_pool():
  │     pool.get(prefer=线程固定key, sticky=学院亲和key) → tok
  │     per-token 信号量（同 key 最多 LLM_PER_TOKEN 路并发）
  │     key 变化 → 该线程内重建 client（threading.local 隔离，不串 key）
  │     调用成功 → record_usage 记账（in/out tokens）→ 返回
  │     no_quota/invalid → pool._mark() 摘除 → 换下一个 key 重试
  │     全部耗尽 → QuotaExhaustedError
  │ ③ QuotaExhaustedError → force 重探一次（300s 节流）→ 仍全灭 → 回退模式 A
  │ ④ 模式 A / legacy：环境变量 key 直调
  │     不可用 → _degraded_response()（{"degraded": true, ...}）
```

## 失效切换与错误分类

`_classify_llm_error()` 把 SDK 异常分成三类，决定「换 key」还是「换模型」：

| 分类 | 判定 | 处置 |
|---|---|---|
| `no_quota` | `insufficient_user_quota` / 「当月额度已用完」 / `quota` | 标记该 key 无余额 → 池切换下一 key |
| `invalid` | HTTP 401 / 「无效的令牌」/ `unauthor` / 认证失败 | 标记密钥无效 → 池切换（TTL 86400s，不频繁重探） |
| 其他 | 超时 / 5xx / 空响应 | **原样抛出**：交给上层重试 / 模型降级链，不误杀 key |

**换 key 与换模型是正交的**：key 问题（额度/密钥）换 key，模型问题（超时/
网关错）换模型；连接层错误（真连不上）两个都不换——换谁都没意义，直接抛。

## 令牌池分配策略

`TokenPool.get(prefer, sticky)` 三级策略（前者优先）：

| 级别 | 参数 | 语义 |
|---|---|---|
| 1. 会话固定 | `prefer`（线程内自动传入） | 同一任务内连续 ask 复用同一 key——命中网关 **prompt cache**（便宜数倍）。固定 key 缓存过期只重探它一个，尽量不切换 |
| 2. 学院亲和 | `sticky`（`begin_task(学院名)` 传入） | `md5(学院名) % 池大小` 稳定选槽——同一学院跨批次/重启固定同一 key，重跑命中上次缓存；md5 均匀分散实现负载均衡 |
| 3. 均衡分配 | 都无 | **最少消耗优先**（累计 in+out tokens 最少先用）+ Redis `INCR` 轮询打破平局 |

任务边界：处理一个新学院前调 `llm.begin_task(学院名)`——清除会话固定 key、
设定亲和槽位。线程隔离（`threading.local`），多线程各自边界互不干扰。

## 探测与自愈

- **探测**：对每个 key 发一次最小 chat 请求（`check_sk_balance.check_key`，
  成本可忽略）判定 有余额 / 无余额 / 密钥无效 / 请求失败。`claim_probe` 互斥锁
  防多进程重复探测；状态缓存 TTL 300s，`no_quota` 到月初自动视为过期
  （配额月度刷新自愈）。
- **探测模型跟随主模型**（`TOKEN_PROBE_MODEL` > `LLM_MODEL`，无写死默认）：
  探测只验 key 死活，模型崩溃 ≠ key 死——写死探测模型曾把 13 个活 key 全刷成
  网络失败（2026-10-15 事故根修）。
- **全灭自愈**：`QuotaExhaustedError` 先 force 重探一次（进程级 300s 节流）
  再放弃——探测进程死亡 + 缓存陈旧时（重启事故），net_fail 躺死但 key 实际是活的。
- **`available=False` 自愈**：重启后首调即 degraded 的空转路径同样补了
  force 重探（2026-10-19 根修）。

## 并发控制（双层信号量）

| 层 | 机制 | 容量 |
|---|---|---|
| 内层（每 key） | `SimpleLLM` per-token 信号量 | `LLM_PER_TOKEN`（默认 2）——单 key 并发保守（单 key 6 路并发曾引发网关重试风暴），总并发靠多 key 摊薄 |
| 外层（全进程） | `llm_gate._DynamicSem` | 自动 = 池有效 key 数 × `LLM_PER_TOKEN`（上限 `LLM_CONCURRENCY_CAP=64`）；后台每 300s 跟随池容量刷新；`LLM_SEM=N` 手动固定（实验对照/网关降级） |

线程模型：client / 固定 key / 亲和槽全部存 `threading.local`，每个线程只重建
自己的 client——多线程并发 ask 不串 key。调用线程池 `_LLM_EX` 固定 8 worker
封顶：旧实现每次调用新建 1-worker 池、超时不等待 → 挂死在 SSL read 的线程
无上限积累（实测单批 50+）。

## 熔断器

网关周期性波动（实测 35-50min/轮）期间调用底层挂 300-600s 才超时——worker
全卡死白烧预算。`llm_gate.py` 进程内滑动窗口熔断：

- 样本 ≥ 8 且失败率 ≥ 60%，或最近 8 次全失败 → **打开**；
- 打开期间非 health 调用**快速失败**（不占池槽）；health 探针豁免（它是恢复检测器）；
- 冷却指数退避 `LLM_BREAKER_BASE × 2^n`（240/480/960/1920s）；
- 到期**半开**放行调用重新积累证据，成功即关、失败即重开（退避 +1）；
- **超时加权 ×3**：服务端挂起整周期比 401 严重得多，4 次超时即可打开
  （防「长 prompt 全挂 + 短 prompt 全成」的降级模式里成功样本稀释失败率）；
- `last_wave_fail_ts()` 供上游终态分流：完成时距最近波动签名 <300s 说明
  3 轮结论不可信 → FAIL 转 RETRY 而非落终态。

## 供应商与模型降级链

主模型失败（超时/网关错）按序换下一模型：主模型 → `LLM_MODEL_FALLBACKS`
（逗号分隔）。key 错误与连接错误不触发换模型（见上文错误分类）。

`agent_config.yaml`（可选，仓库不含）切换供应商：

```yaml
provider: deepseek      # 或 anthropic（默认）
deepseek:
  api_key: "sk-..."     # 仍建议用环境变量, 这里只是兜底
  model: "deepseek-chat"
  base_url: "https://api.deepseek.com/v1"
```

## 配置速查

`.env`（模板见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | — | 模式 A 密钥；模式 B 下作兜底 |
| `LLM_BASE_URL` | — | 网关地址；留空 = Anthropic 官方 |
| `LLM_MODEL` | — | 主模型名 |
| `LLM_MODEL_FALLBACKS` | 内置两条 | 模型降级链（逗号分隔，按你的网关支持填写） |
| `TOKEN_PROBE_MODEL` | 随 `LLM_MODEL` | 池探测用模型（填网关上最便宜的可用模型） |
| `ONEPAI_BASE_URL` | — | 池探测网关（缺省回落 `LLM_BASE_URL`） |
| `LLM_PER_TOKEN` | `2` | 每 key 并发路数 |
| `LLM_CONCURRENCY_CAP` | `64` | 全池并发防御上限 |
| `LLM_SEM` | 自动 | 手动固定全局并发（留空 = 跟随池容量） |
| `LLM_TIMEOUT` | `150` | 单次调用超时（秒，`llm_ask_sem` 层） |
| `LLM_SDK_TIMEOUT` | 无限 | SDK read 超时覆盖（慢网关场景慎设小） |
| `LLM_BREAKER_BASE` | `240` | 熔断冷却基数（秒），指数退避 240→1920 |

## 排查手册

| 症状 | 诊断 | 处置 |
|---|---|---|
| `Available: False` | `uv run python util/llm_client.py` 自检 | `.env` 缺 `LLM_API_KEY`；或 provider 依赖未装（deepseek 需 `openai` 包） |
| 全部输出 `{"degraded": true}` | LLM 不可用（模式 A 无 key / 池全灭） | 检查 `.env` 三项；池模式跑 `token_pool.py check` 看各 key 状态 |
| 池模式没生效 | `_ensure_pool` 日志「token 池不可用」 | 确认 `token manager/tokens.json` 存在且 JSON 合法；Redis 不可用时自动退 JSON 后端 |
| 单 key 频繁 429/超时 | per-token 并发过高 | 调低 `LLM_PER_TOKEN`（默认 2 已保守） |
| 网关周期性大面积超时 | 熔断日志 `OPEN opens=N`（`/tmp/llm_wrapper.log`） | 正常机制：冷却期快速失败，半开后自动恢复；持续重开查网关侧 |
| `QuotaExhaustedError` 反复 | 全 key 无余额/无效 | `token_pool.py check` 复核；月初额度刷新自动自愈；确认探测模型在网关上可用 |
| 换了 `.env` 网关地址不生效 | 配置文件/环境变量优先级 | `LLM_BASE_URL` 优先级最高，检查 `agent_config.yaml` 是否残留旧地址 |
| prompt cache 命中率低 | 任务内 key 频繁切换 | 确认任务开始处调了 `begin_task(学院名)`；避免跨任务复用线程不重置 |
