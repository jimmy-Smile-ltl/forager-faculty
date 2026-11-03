# 并发 / 浏览器 / LLM 管理范式借鉴（department teachers → teachers）

> 2026-08-25 盘点。来源：`department teachers/` 各 agent（collect_teacher / fix_loop / evaluate_teachers / util）多年踩坑沉淀的范式；本文件同时记录 `teachers/` 侧差距审计与建议应用方案。相关历史见 `improvements.md`、`agent_capability_evaluation.md`。

---

## 一、department teachers 侧范式清单（Agent 沉淀）

### A. LLM 管理（最核心，教训最贵）

| # | 范式 | 位置 | 要点 |
|---|------|------|------|
| A1 | **全局单一配额信号量** | `collect_teacher._LLM_SEM`（_DynamicSem） | 全项目唯一实例，fix_loop / evaluate_teachers 均 import 复用。教训：*此前三处各自 Semaphore(4) 叠加成 12 路并发打爆网关*——同进程多模块各自建信号量会叠加，必须共享一把 |
| A2 | **双层并发控制** | 内层 `llm_client._token_sems`（per-token，模块级 dict，`LLM_PER_TOKEN=2`）+ 外层 `_LLM_SEM` | 内层按 sk 独立 Semaphore——同一 token 最多 2 路（2026-08-23 单 token 6 路风暴阈值约 4-5）；外层容量 = 池有效数 × per-token，300s 后台自动刷新，防御上限 `LLM_CONCURRENCY_CAP=64`，`LLM_SEM=N` 可手动固定（实验对照/网关异常降级） |
| A3 | **任务边界 `begin_task(sticky_key)`** | `llm_client.py:334` | 学院亲和：传学院名 → md5 hash 槽位固定 token。收益：① 学院内连续 ask 复用同一 token 命中网关 prompt 缓存（省钱）；② 跨批次/重启（队列重试/修复循环）命中上次缓存；③ 多 token 按学院均匀分散负载。`threading.local` 隔离——只影响当前线程，多线程并行各调各的，无需加锁 |
| A4 | **会话固定 failover** | `_ask_with_pool` | `pool.get(prefer=线程sk, sticky=...)` 固定 key 优先；发现 no_quota/invalid → `pool._mark` + 清空固定 key，下次 get 重新负载均衡（最少消耗 + 轮询）。调用方无感知 |
| A5 | **超时保护 + 正确 shutdown** | `llm_ask_sem` | `ThreadPoolExecutor(1)` 包 `ask` + `timeout=150`。**必须 `shutdown(wait=False, cancel_futures=True)`**——wait=True 会等卡死的 SSL read 永远结束（网关静默挂起时 SDK 默认 10min 超时拖死 worker） |
| A6 | **degraded 检测 + 退避重试** | `llm_ask_with_timeout` | 空响应 / `"degraded"` / `"error"` JSON 判定 bad → 3s/8s 两次退避重试。网关 token 池轮换期出现 1-5 分钟的 401/空响应浪潮，短退避即可恢复 |
| A7 | **坏缓存扰动重试** | `fix_loop.py:256-260` | 网关 prompt 缓存会命中"截杀波"时期的坏响应（相同 prompt 秒回空/残缺 JSON，手工略改的 prompt 正常）→ prompt 加时间戳扰动强制真实生成 |
| A8 | **降级链 + 确定性错误分类** | `SimpleLLM` | 池 → 环境变量 key（QuotaExhaustedError）→ degraded JSON 响应；`_classify_llm_error` 只认 no_quota/invalid（确定性，标记+切换），其余异常原样抛出不吞（静默失败禁令） |
| A9 | **诊断日志文件级** | `_llm_wlog` → `/tmp/llm_wrapper.log` | 不受 stdout 编码/管道影响；每调用记 tag/耗时/prompt 长度/响应头 |

### B. 浏览器管理

| # | 范式 | 位置 | 要点 |
|---|------|------|------|
| B1 | **全局浏览器预算** | `util/browser_budget.py` | `BoundedSemaphore(DRISSION_EXTRA_BROWSERS=3)` + `browser_budget()` contextmanager。满 → 立即抛 `BrowserBudgetExceeded`，调用方降级——**不排队、不无限新建**（与窗口池满拒绝语义一致） |
| B2 | **finally 关闭** | 所有 `_fetch_raw` | goto 抛异常也必须 `ctx.close(); b.close()`（修复前泄漏整个浏览器进程；测试 `TestPlaywrightClosesOnError` 守护） |
| B3 | **统一请求层** | `util/smart_fetch` | 四级降级 + 域名双向记忆（`_DIRECT_OK_HOSTS`/`_PROXY_OK_HOSTS`）+ WAF 412 识别 + 直连优先/代理兜底；模板/主控/prompt 示例三处同步（P1-5，源码哨兵测试守护） |
| B4 | **端口按 PID 偏移** | `_DRISSION_PORT_BASE` | 9340 + (pid%1000) × 窗口上限——ProcessPool/多进程窗口池端口块互不重叠（P1-4） |
| B5 | **线程安全前提显式声明** | fix_loop main 注释 | 用线程池前逐项列出锁前提：LLM 已加信号量 / 队列知识库已加锁 / llm_name_cache 自带锁 / DrissionPage 已加锁 |

### C. 并发设计

| # | 范式 | 位置 | 要点 |
|---|------|------|------|
| C1 | **院系级 ThreadPoolExecutor** | fix_loop / evaluate_teachers main | 非 ProcessPool——院系是独立文件域，线程安全前提满足即可（B5）。任务异常 → FAIL 入队/标记，主流程不崩 |
| C2 | **任务循环开头 begin_task** | collect_dept / fix_one / evaluate_dept | 每线程在各自任务开始时调用一次，互不干扰（A3） |
| C3 | **失败队列持久化** | `fix_loop.fail_queue.json` + `.fix_done` | PASS 移出 / FAIL 入队 / 跨运行恢复（--queue 模式只处理历史失败） |
| C4 | **串行默认 + 可选并发** | main | 单任务串行遍历；`--workers N` 才开池，`nw = max(1, min(workers, len(tasks)))` |
| C5 | **坏缓存/自由轮次** | fix_loop | 网关断供消耗"自由轮次"重试，恢复即获真实改进 |

---

## 二、teachers/ 侧现状审计

### 已有（自身发展出的对应物）

| 能力 | teachers 侧实现 | 评价 |
|------|----------------|------|
| 站点请求节流 | `_throttle` 节流门（detail_scraper.py:577） | ✅ 跨线程串行站点请求，LLM/PDF 等待并行——比 department 侧更细 |
| LLM 熔断 | `_llm_blackout_until` + `_llm_consecutive_timeouts`（≥3 → 5min 快速失败） | ✅ 比 department 侧更细（department 只有信号量，无超时熔断） |
| 确定性快速失败 | `_retry` + `HttpStatusError` 立即失败 | ✅ 与 collect_teacher 教训一致（北大哲学系 404 教训） |
| 渲染预算 | 每教师 1 次渲染 + `_render_blackout` 系级熔断 | ✅ 独特设计 |
| 并发模型 | 教师级 `process_batch` ThreadPool + 学校级 `extract_all_details` ThreadPool + 增量落盘 + 保序收集 | ✅ 与 C1 同型 |
| 失败重跑 | P1-6 `_requeue_failed`（瞬时失败重跑 1 次） | ⚠️ 内存级，跨运行不持久 |
| 降级链 | `http_client.UnifiedClient` 四级 + DNS 预检 + smart_fetch | ✅ 同源共享 |
| 统一日志 | `_log`（加锁 stdout） | ⚠️ 无文件级诊断通道 |

### 差距清单（按应用价值排序）

| 优先级 | 差距 | 详情 | 对应范式 |
|--------|------|------|---------|
| **P0** | **无任务边界（begin_task 学院亲和）** | detail_scraper 从不调用 begin_task → 每次运行 token 分配不稳定（首次 ask 自动分配），跨批次/重启不命中 prompt 缓存；院系内连续 ask 虽固定（A4 语义自动生效），但多个 scraper 实例（--all 并发）各自实例级 `_pool` 记账，负载不按院分散 | A3 |
| **P1** | **无全局 LLM 总闸/容量感知** | 内层 per-token 槽是模块级（`_token_sems` 天然跨实例生效，✅）；但无 `_LLM_SEM` 等价物：池有效 token 数变化不感知、无手动总闸、QuotaExhaustedError 判定各实例独立（多个 scraper 各自 fallback legacy key，可能叠加） | A1/A2/A8 |
| **P1** | **无 degraded 检测与退避重试** | `_extract_fields_llm` 只有超时重试 1 次（3s），空响应/degraded JSON 直接启发式兜底——网关坏缓存秒回空时白烧调用、质量降级 | A6/A7 |
| **P1** | **`_throttle` 实例级** | 每 scraper 一把锁 + 独立 `_next_request_at` → 跨院系并发实际请求速率 = workers × rate_limit，多学校并发时无全局节流 | B1 同源精神（全局共享资源限制） |
| ~~P1~~ | ~~渲染路径无浏览器预算~~ | **已确认非差距**（2026-08-25 应用前复核）：`handleRuishu.py` 内层 DrissionPage（:307）与 Playwright（:409）路径均已挂 `browser_budget()`——预算在正确的内层位置（创建/租用浏览器时占槽）。http_client 层再挂会同一 BoundedSemaphore 双重 acquire 把并发容量人为减半，**不得补挂** | B1 ✅ |
| P2 | 失败重跑不持久 | `_requeue_failed` 内存级；跨运行靠 task_state 院系粒度 done，教师级瞬时失败不会自动进入下轮 | C3 |
| P2 | LLM 诊断无文件级日志 | `_log` 只打 stdout（管道编码可能炸）；无 /tmp 级统一诊断通道 | A9 |
| P2 | 无修复循环 | 字段缺失 → 启发式降级，无 fix_loop 式"评估→改进→重评"与自由轮次 | C5（设计范围差异，非必须） |

### 已确认的非差距（无需动）

- **内层 per-token 并发保护跨实例有效**：`_token_sems` 是模块级 dict（按 sk），detail_scraper 每实例一个 SimpleLLM 也共享同一组信号量——不会出现"多实例叠加并发"（department 侧三处 Semaphore(4) 教训在 teachers 侧内层天然免疫）。
- **超时保护已具备**：`_extract_fields_llm` 用 `ThreadPoolExecutor(1) + fut.result(timeout=LLM_TIMEOUT)`，`shutdown` 走 with 块（退出即 wait）——但**卡死 SSL read 时 with 块 shutdown(wait=True) 同样会卡**！此点与 A5 差距合并进 P1-2。
- **线程安全前提已满足**：站点请求经 `_throttle`、渲染经 smart_fetch 内部锁、CSV 落盘由协调线程独占——线程池并发安全。

---

## 三、建议应用方案

按 P0 → P1 顺序，每项配回归测试（新增 test 文件，全量 168 测试守护），遵循"测试发现 bad case → 设计级修复 → 归档 improvements.md"工作流：

1. **P0：`process_dept` 开头注入学院亲和**
   `scraper._llm.begin_task(school)`（begin_task 只影响当前线程，process_dept 内先调用；院系内所有教师 ask 固定同 token 命中缓存）。测试：mock SimpleLLM 验证 begin_task 以 school 名调用一次。

2. **P1-1：全局 LLM 总闸**（参考 `_DynamicSem`）
   在 teachers/ 侧建模块级共享信号量（容量 = 池有效数 × LLM_PER_TOKEN，防御上限），`_extract_fields_llm` 调用外包该信号量。同进程多 scraper 实例共享一把（department 侧 A1 教训）。

3. **P1-2：degraded 检测 + 退避重试 + 正确 shutdown**
   `_extract_fields_llm` 的调用循环升级为：bad 判定（空 / "degraded" / "error"）→ 3s/8s 两退避；超时路径 `shutdown(wait=False, cancel_futures=True)`；坏缓存加时间戳扰动重试（A7）。保留现有熔断（其与信号量正交）。

4. **P1-3：`_throttle` 提升为进程级全局**
   节流锁与 `_next_request_at` 移到模块级（进程内所有 scraper 实例共享）——`--all --workers 4` 时同域请求速率仍 = rate_limit，不叠加。

5. **~~P1-4：渲染路径挂 `browser_budget()`~~** —— 应用前复核已满足（handleRuishu 内层覆盖），跳过。
6. **P2（后续迭代）：失败队列持久化**（参考 fail_queue.json）+ **文件级 LLM 诊断日志**（参考 `_llm_wlog`）。

> P0-2（no_url 回补）与本清单无关，仍作为独立迭代待定。

---

## 四、测试守护要点（应用时）

- 新增 `tests/test_llm_budget_borrow.py`：学院亲和调用、全局信号量并发上限、degraded 退避、shutdown 语义。
- 新增/扩展 `tests/test_fetch_raw_budget_proxy.py`：渲染路径 browser_budget 源码哨兵。
- `_throttle` 全局化后，现有并发测试（多实例速率叠加）断言更新。
- 全量套件运行：`cd "college teacher" && uv run python -m unittest discover -s tests -v`
