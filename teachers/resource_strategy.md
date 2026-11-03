# Agent 框架流程与资源管理策略（2026-08-26 梳理）

本文档梳理高校教师数据采集 agent 的完整流程，以及 LLM token、浏览器、网络代理
三类资源的并发管理策略。基于代码事实整理，供后续扩展/调参/评审使用。

---

## 一、Agent 框架流程：四步采集流水线

```
Step 1: 学校信息          colleges department/  (采集 985/211 名单、基本信息)
Step 2: 院系采集          colleges department/  (每校二级学院目录 + 评估)
Step 3: 教师名录发现       department teachers/  (发现机制 v2: 师资页探测 → 教师名单)
Step 4: 教师详情提取       teachers/             (← 当前工作核心)
        └── extract_all_details.py 主入口
            ├── --school/--dept 单校模式 | --all 全校模式
            ├── --workers N    学校级并发 (ThreadPoolExecutor, 默认 2)
            ├── --rate-limit   站内请求间隔 (默认 1.0s, 进程级全局节流)
            └── --resume       断点续传 (task_state.json, 每院系 done 状态)
            └── process_dept()
                ├── DetailScraper.begin_task()  ← 学院亲和 (LLM 固定 token)
                └── DetailScraper.process_batch()  ← 教师批量处理
                    └── 单个教师 process_teacher():
                        1. fetch_detail_page  三级降级抓取 (见 §三)
                        2. detect_pdf_iframe  PDF 检测 → pdfplumber 提取
                        3. 文本清洗 (去 script/style/nav 噪声)
                        4. _extract_fields_llm    LLM 提取 14 字段
                           ↓ 失败/熔断降级
                           _extract_fields_heuristic  正则启发式兜底
                        5. 保存 raw HTML/PDF + teachers_detail.json/.csv
```

**字段模型**（FIELD_SPEC，14 字段）：
联系方式/通讯地址/职称/职务/出生年月/教育经历/工作经历/研究方向/学术成果/
基金/荣誉/课堂项目/讲授课程 + `attr` 兜底 dict（兼职、办公室房间号、社会任职
等未覆盖但有价值的信息，LLM 按类别收集，启发式扫 `类别：内容` 模式）。

**关键降级链**（字段提取）：
LLM 正常 → degraded/空响应退避重试(3s/8s) → 坏缓存时间戳扰动 → 连续超时
≥3 次熔断 5 分钟快速失败 → 启发式提取。字段以 LLM 为主、启发式为兜底，
互不阻塞整批任务。

---

## 二、LLM Token 管理（token manager/token_pool.py + util/llm_client.py）

### 2.1 池结构：双后端镜像

| 后端 | 用途 | 说明 |
|------|------|------|
| JsonBackend (tokens.json) | 进程内/单机 | cache_ttl=300s 缓存快照 |
| RedisBackend (redis://localhost:6379/0) | 多进程/多机共享 | 轮询分配 INCR 原子化 |

双写镜像：任一可用，多进程各读各的缓存，状态经 Redis 收敛。

### 2.2 Token 状态机

```
ok ──耗尽──▶ no_quota ──┐
  └─超时/失败探测────▶ unknown ──重探──▶ ok / invalid / no_quota
unknown: 缓存过期, 待重探 (claim_probe 互斥锁, 不重复探测)
invalid: 401/密钥无效, 永久剔除
```

### 2.3 分配策略（token_pool.get，三级优先级）

1. **prefer 会话固定**（任务内）：同一任务固定同一 token，命中网关 prompt
   缓存省钱；缓存过期(unknown)只重探该 key 一个，尽量不切换
2. **sticky 学院亲和**（跨批次/重启稳定）：`md5(学院名)` 均匀选槽 →
   同一学院永远同一 token，学院重跑命中上次 prompt 缓存；10+ token 按
   学院均匀分散负载
3. **最少消耗 + 轮询**：累计 in+out tokens 最少的先用，并列时轮询打破
   平局（Redis INCR 原子）

### 2.4 并发闸门（三层）

| 层级 | 机制 | 容量 |
|------|------|------|
| 全局总闸 | `_LLM_SEM` _LLMDynamicSem（detail_scraper 模块级单例） | 自动: min(ok_token数×2, 64)；env `LLM_SEM` 手动覆盖 |
| per-token 槽 | `_token_sem(sk)`（llm_client 内） | 每 token 限并发 |
| 动态刷新 | `_llm_sem_refresher` 每 300s 跟随池容量 | 与池刷新 TTL 对齐 |

设计动机：department 侧曾因"三处 Semaphore(4)=12 路"叠加打爆网关 →
统一为进程内单一共享总闸，容量随池内可用 token 数自动伸缩。

### 2.5 故障处理

- **QuotaExhaustedError**（全部 no_quota/invalid）→ failover 环境变量单 key
- **自动标记**：`_classify_llm_error` 识别 insufficient_user_quota/401 →
  no_quota/invalid 写入池，下次分配自动绕过
- **LLM 熔断**：连续超时 ≥3 次 → `_llm_blackout_until` 5 分钟快速失败走
  启发式（不烧 token 打已挂的网关）
- **退避**：degraded/空响应 sleep 3s/8s 重试，RETRY_MAX=3；坏缓存秒回空时
  追加时间戳扰动强制真实生成
- **用量上报**：record_usage 成功后记账，驱动最少消耗分配

---

## 三、浏览器管理（保底范式）

> 用户原则（两次强调）：**浏览器是保底方案，不要动不动就用**。
> HTTP 请求优先，瑞数等反爬才逐级升级；无瑞数站全程 0 浏览器启动。

### 3.1 抓取三级降级链（fetch_detail_page）

```
1. curl_cffi HTTP 请求（TLS 指纹模拟）
   └─ 瑞数 412/202 → sdenv cookie 链 (handleRuishu)
2. 连接级失败 (TCP broken pipe) 或 cookie 链失败
   → CDP 桥接常驻 Chrome (9222) 渲染
3. browser_budget 有额度才启动 ChromiumPage (9333) 保底
   → 满则拒绝, 不排队 (保底就是保底)
```

### 3.2 浏览器资产清单（三处，各司其职）

| 端口 | 用途 | 管理策略 |
|------|------|----------|
| 9333 | 瑞数浏览器**常驻单例** | `_CDP_RESIDENT` 缓存 + `_CDP_START_LOCK` 双检锁（防多 worker 互相 pkill）+ `_resident_alive` 身份核对（端口在听 + /json/version ws 一致才算健康）；headless(True) |
| 9222 | **tab 租用池** (cdp_util.cdp_page) | 连接常驻 Chrome 只租 tab，用完 `page.close()` 只关 tab 不关浏览器；每 tab 注入 `_STEALTH_JS` + UA 覆盖（防瑞数变种检测 webdriver） |
| — | **窗口池** (smart_fetch) | 3 窗口 × 4 tab = 12 上限，watchdog 守护跨进程共享 |

### 3.3 预算控制

- `browser_budget`（BoundedSemaphore）：`DRISSION_EXTRA_BROWSERS` 默认 3；
  **满即抛 BrowserBudgetExceeded 立即拒绝，不排队**——排队 = 并发堆积
- `_CONNECTION_BLOCKED_HOSTS` 连接级失败域记忆：某域已知走不通裸请求 →
  直通浏览器链，跳过 curl 空耗
- 审计结果：spider/detail_scraper 常规路径 **0 浏览器启动点**；抽样矩阵
  （吉大博达 /info/、上大博达 /info/、上外 page.htm 三平台 108 人）全程
  0 浏览器启动，R13 容器泛化确认

---

## 四、网络代理管理（util/http_client.py）

| 机制 | 策略 |
|------|------|
| PROXY_MODE | `off` / `static`（env 配置，代理 URL 从 .env 读取，仓库不落凭证） |
| _build_proxies | 统一构造 http/https 代理参数 |
| **代理熔断** | `_PROXY_FAILED_AT` + `_PROXY_FAILURE_TTL=60s`：代理失效后 60s 内直连，避免每请求都撞死代理 |
| 脱敏 | `_mask_proxy_url` 代理 URL 中 user:pass 打码（日志安全） |

---

## 五、全局节流门

- **模块级 `_THROTTLE_LOCK` / `_NEXT_REQUEST_AT`**（detail_scraper）：
  进程内所有 scraper 实例共享一个节流门
- 效果：`--all` 并发 N 校时，站点请求间隔仍 = rate_limit（默认 1.0s），
  **不叠加成 N×rate_limit**（实例级锁的教训，2026-08-25 全局化）

---

## 六、关键可调参数一览（env 覆盖，均不落仓库）

| 参数 | 默认 | 含义 |
|------|------|------|
| LLM_CONCURRENCY_CAP | 64 | LLM 全局总闸上限 |
| LLM_PER_TOKEN | 2 | 每 token 折算并发槽 |
| LLM_SEM | (自动) | 手动覆盖总闸容量 |
| LLM_TIMEOUT | 60s | 单次 LLM 调用超时 |
| LLM_SLOW_THRESHOLD | 30s | LLM 慢调用告警阈值（🐢 日志） |
| RETRY_MAX | 3 | 降级重试上限 |
| DRISSION_EXTRA_BROWSERS | 3 | 额外浏览器预算（9333 保底池） |
| DRISSION_MAX_WINDOWS | 3 | 窗口池窗口数 |
| DRISSION_MAX_TABS_PER_WINDOW | 4 | 每窗口 tab 数（4:1） |
| PROXY_MODE | off | off/static 代理模式 |
| PROXY_SLOW_LIMIT | 3 | 代理慢速熔断连续次数（PROXY_SLOW_THRESHOLD 秒以上） |
| PROXY_SLOW_THRESHOLD | 15s | 代理慢速熔断阈值 |
| CDP_BASE_URL | http://127.0.0.1:9222 | CDP 常驻浏览器地址 |

---

## 七、设计原则总结

1. **分层降级，资源向后靠**：HTTP → CDP → 浏览器，浏览器永远最后
2. **全局闸门，单点收敛**：LLM 总闸、节流门均为进程级单例，防多模块
   信号量叠加
3. **满即拒绝，不排队**：浏览器预算满 → 立即失败走降级，不做饥饿队列
4. **亲和缓存，钱花刀刃**：会话固定 + 学院亲和，命中 prompt 缓存
5. **熔断优先于重试**：网关连续超时 → 5 分钟快速失败，不烧 token
6. **失败域记忆**：连接级黑名单、代理熔断，避免重复踩坑
