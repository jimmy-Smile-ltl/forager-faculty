# ═══════════════════════════════════════════════════════════════
#  SimpleLLM — 本项目唯一的 LLM 客户端 (多供应商: Anthropic / DeepSeek)
#  配置: 环境变量驱动 (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 统一入口,
#        兼容 ANTHROPIC_* / DEEPSEEK_API_KEY 原生变量),
#        可选本地 agent_config.yaml 覆盖 provider
# ═══════════════════════════════════════════════════════════════
"""
多供应商 LLM 客户端。
支持 Anthropic Claude 和 DeepSeek，自动根据配置切换。
当 API key 不可用时进入 degraded 模式。
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Callable

import yaml

# ── 配置加载: 仓库根 .env (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 统一入口) ──
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE, override=True)  # .env 强制覆盖已有环境变量
except Exception:
    pass

log = logging.getLogger("llm_client")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%m-%d %H:%M:%S")
    _h = logging.StreamHandler()
    _h.setFormatter(_fmt)
    log.addHandler(_h)


# ── per-token 并发控制 (2026-08-24): 每个 token 独立并发槽, 突破全局信号量 ──
# LLM_PER_TOKEN=N → 同一 token 同时最多 N 路并发; 总并发 = 有效token数 × N。
# 历史教训(2026-08-23): 单 token 6 路并发引发网关重试风暴 → 单 token 并发保守,
# 用 "token 数 × N" 摊薄负载: 15 token × 2 = 30 路, 每 token 仅 2 路,
# 远低于单 token 风暴阈值(约4-5路), 且多 token 并行输出本就是这个 token 池的设计目标。
LLM_PER_TOKEN = int(os.environ.get("LLM_PER_TOKEN", "2"))
_token_sems = {}                 # sk -> Semaphore(LLM_PER_TOKEN)
_token_sems_lock = threading.Lock()

# ── 2026-09-20 用户指令: 模型顺序降级链 ──
# 网关超时多数是「output 多、一时半会儿吐不出来」, 不是连不上 — 取消 read
# timeout 强限制, 慢生成等到底; connect 保留 15s(真连不上快速失败, 报错与
# 慢生成完全不同, 不浪费切换)。模型按顺序尝试: DeepSeek 优先 → GLM → Gemini;
# 超时/网关错误换下一模型, 额度/密钥错误不换(交 token 池切换 key),
# 连接错误不换(换模型无意义)。
_LLM_READ_TIMEOUT = int(os.environ["LLM_SDK_TIMEOUT"]) if os.environ.get("LLM_SDK_TIMEOUT") else None  # None=无限
_LLM_CONNECT_TIMEOUT = 15.0
# 2026-09-20 网关官方模型名(oneapi /v1/models 实测): Gemini 网关暂无账号(503,
# 时有时无), 按用户指令放最低优先级兜底
_MODEL_FALLBACKS = [m.strip() for m in os.environ.get(
    "LLM_MODEL_FALLBACKS",
    "GLM-5.3-Flash,Gemini 3.7 Flash").split(",") if m.strip()]


def _token_sem(sk):
    """返回该 token 的并发信号量 (懒创建, 线程安全)。"""
    sem = _token_sems.get(sk)
    if sem is None:
        with _token_sems_lock:
            sem = _token_sems.get(sk)
            if sem is None:
                sem = threading.Semaphore(LLM_PER_TOKEN)
                _token_sems[sk] = sem
    return sem


def _is_connect_error(e) -> bool:
    """连接层错误(真连不上): 换模型无意义, 原样抛出。慢生成超时
    (ReadTimeout/APITimeoutError)不属于此类 — 那是网关慢, 换模型有用。
    (2026-09-20 用户: 「真正的连不上, 报错信息都是不一样的」)"""
    name = type(e).__name__
    if name in ("ConnectError", "ConnectTimeout", "APIConnectionError"):
        return True
    try:
        import httpx as _h
        if isinstance(e, _h.ConnectError):  # 含 ConnectTimeout
            return True
    except Exception:
        pass
    return False


def _model_chain(primary):
    """主模型 + 降级链(去重, 保序): DeepSeek 优先 → GLM → Gemini。
    (2026-09-20 用户指令: 按顺序尝试)"""
    return [primary] + [m for m in _MODEL_FALLBACKS if m != primary]


class SimpleLLM:
    # 2026-10-15 net_fail自愈: 全灭force重探的进程级节流时间戳(类属性)
    _last_force_reprobe = 0.0

    """多供应商 LLM 客户端。

    支持:
    - Anthropic Claude (原生 SDK, thinking mode)
    - DeepSeek (OpenAI 兼容 API, 通过 openai SDK)

    配置示例 (agent_config.yaml):
        provider: deepseek
        deepseek:
          api_key: "sk-..."
          model: "deepseek-chat"
          base_url: "https://<your-gateway>/v1"

        provider: anthropic
        anthropic:
          api_key: "sk-ant-..."
          model: "claude-sonnet-4-6"

    ────────────────────────────────────────────────────────────────
    token 池 + 会话固定 (缓存省钱):

    处理一个"任务单元"(如采集一个学院)时, 建议在任务开始处调用 begin_task(),
    任务内的连续 ask() 会保持同一个 token (命中网关 prompt 缓存, 价格更便宜);
    仅当该 token 额度耗尽/密钥无效时才自动切换 (切换后固定新 key)。

    并发使用:
    - SimpleLLM 是线程安全的单例: 客户端实例 / 会话固定 key 全部存放在
      threading.local 中, 每个线程各自隔离, 多线程共享同一实例不会串 key。
    - 每个线程各自调用 begin_task() 定义自己的任务边界, 只影响当前线程。
    - 推荐用法 (每线程每任务):
        for dept in depts:              # 任务循环 (可多线程并行)
            llm.begin_task()            # 任务边界: 重新均衡分配 token
            for page in dept.pages:     # 任务内
                llm.ask(system, page)   # 连续调用保持同一 token
    - 任务内 key 被耗尽: ask() 内部自动标记 no_quota 并切换, 调用方无感知。
    - 池不可用(Redis 挂/未配置)时自动回退环境变量 key 的 legacy 路径。
    """

    def __init__(self, config_path: str = None, api_key: str = None):
        if config_path is None:
            config_path = Path(__file__).parent.parent / "agent_config.yaml"
        self.config_path = config_path
        self._provider = None
        self._provider_config = {}   # 兼容引用: 主线程初始化后的快照 (实际用线程本地)
        self._local = threading.local()   # 线程隔离: client / cfg / avail / sk(会话固定key)
        self._pool = None       # 懒加载 token 池 (None=未初始化, False=不可用已禁用)
        self._pool_mod = None   # token_pool 模块引用 (QuotaExhaustedError/mask_sk)
        self._pool_lock = threading.Lock()  # 池懒加载双检锁 (多线程首次并发 ask 安全)

        # 加载配置
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    self.config = yaml.safe_load(f)
            else:
                self.config = {}
        except Exception:
            self.config = {}

        # 确定 provider
        self._provider = self.config.get("provider", "anthropic")

        # 初始化对应 provider
        if self._provider == "deepseek":
            self._init_deepseek(api_key)
        else:
            self._init_anthropic(api_key)

    def _set_thread_state(self, client, cfg, avail):
        """写入当前线程的客户端状态 (线程隔离: 每个线程有自己的 client)"""
        self._local.client = client
        self._local.cfg = cfg
        self._local.avail = avail

    def _init_anthropic(self, api_key: str = None):
        """初始化 Anthropic 客户端（支持官方 Claude + DeepSeek 兼容 API）。

        结果写入当前线程 (线程隔离); key 优先级: 参数(池key) > .env LLM_API_KEY
        > config > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY
        """
        try:
            import anthropic
        except ImportError:
            self._set_thread_state(None, {}, False)
            return

        api_cfg = self.config.get("anthropic", {})

        effective_key = (
            api_key
            or os.environ.get("LLM_API_KEY")
            or api_cfg.get("api_key")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )

        if not effective_key:
            self._set_thread_state(None, {}, False)
            return

        # Base URL: 环境变量优先于配置文件(12-factor) — .env 与配置文件各可指向
        # 不同网关; 旧顺序曾让配置文件地址覆盖 .env, 导致外部环境报 Connection error
        # (配置里的网关地址在当前环境不可达的实测教训)
        base_url = (os.environ.get("LLM_BASE_URL")
                    or os.environ.get("ANTHROPIC_BASE_URL")
                    or api_cfg.get("base_url"))

        # Model: 环境变量优先 > 配置 > 默认
        model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or api_cfg.get("model")
            or "claude-sonnet-4-6"
        )

        # 检测是否非 Claude 后端（DeepSeek 等），禁用 thinking
        is_claude = "api.anthropic.com" in (base_url or "") or not base_url

        try:
            # 2026-09-20 用户指令: 取消 timeout 强限制 — 网关超时多为慢生成
            # (output 多吐不完), read=None 无限等待; connect=15s 真连不上
            # 快速失败(连不上的报错是连接类异常, 与慢生成完全不同)。
            # LLM_SDK_TIMEOUT 仍可显式覆盖 read 超时(大 prompt 场景可调)。
            # trust_env=False: LLM 请求强制直连, 不读 http_proxy/https_proxy
            # (LLM 网关走代理会绕路/失败; 代理仅用于站点抓取层)。
            import httpx
            if base_url:
                client = anthropic.Anthropic(
                    api_key=effective_key, base_url=base_url,
                    http_client=httpx.Client(trust_env=False,
                        timeout=httpx.Timeout(_LLM_READ_TIMEOUT, connect=_LLM_CONNECT_TIMEOUT)))
            else:
                client = anthropic.Anthropic(
                    api_key=effective_key,
                    http_client=httpx.Client(trust_env=False,
                        timeout=httpx.Timeout(_LLM_READ_TIMEOUT, connect=_LLM_CONNECT_TIMEOUT)))

            cfg = {
                "model": model,
                "max_tokens": api_cfg.get("max_tokens", 16000),
                "thinking": api_cfg.get("thinking", True) and is_claude,
                "thinking_budget": api_cfg.get("thinking_budget", 4000),
            }
            self._set_thread_state(client, cfg, True)
        except Exception:
            self._set_thread_state(None, {}, False)

    def _init_deepseek(self, api_key: str = None):
        """初始化 DeepSeek 客户端 (OpenAI 兼容 API)。结果写入当前线程。"""
        try:
            from openai import OpenAI
        except ImportError:
            self._set_thread_state(None, {}, False)
            return

        api_cfg = self.config.get("deepseek", {})
        effective_key = (
            api_key
            or os.environ.get("LLM_API_KEY")   # .env 统一入口, 优先级最高
            or api_cfg.get("api_key")
            or os.environ.get("DEEPSEEK_API_KEY")
        )

        if not effective_key:
            self._set_thread_state(None, {}, False)
            return

        try:
            # Base URL: 环境变量优先于配置文件(12-factor, 与 _init_anthropic 同序)
            # — .env 是用户配置的权威来源, 配置文件仅作补充。
            # 不设任何硬编码默认地址: 用户必须自行配置网关, 缺失即置不可用。
            base_url = (os.environ.get("LLM_BASE_URL")
                        or api_cfg.get("base_url"))
            if not base_url:
                self._set_thread_state(None, {}, False)
                return
            # trust_env=False: LLM 请求强制直连, 不读 http_proxy/https_proxy 环境变量
            import httpx
            # 2026-09-20 用户指令: 取消 read timeout 强限制 — 网关超时多为
            # 慢生成(output 多吐不完), read=None 无限等待; connect=15s 真连不上
            # 快速失败。LLM_SDK_TIMEOUT 仍可显式覆盖。外层保护(单院系预算/
            # 15min守卫)在任务层, 不受影响。
            client = OpenAI(api_key=effective_key, base_url=base_url,
                            http_client=httpx.Client(trust_env=False,
                                timeout=httpx.Timeout(_LLM_READ_TIMEOUT, connect=_LLM_CONNECT_TIMEOUT)))
            cfg = {
                "model": api_cfg.get("model", "deepseek-chat"),
                "max_tokens": api_cfg.get("max_tokens", 8192),
                "temperature": api_cfg.get("temperature", 0.3),
            }
            self._set_thread_state(client, cfg, True)
        except Exception:
            self._set_thread_state(None, {}, False)

    @property
    def available(self) -> bool:
        return getattr(self._local, "avail", False)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return getattr(self._local, "cfg", {}).get("model", "unknown")

    def ask(self, system: str, user_message: str, temperature: float = 0.3) -> str:
        """发送单轮消息，返回文本响应。

        优先走 token 池 (自动 failover: 额度耗尽/密钥无效自动标记并切换);
        池不可用或全部耗尽时回退环境变量 key 的 legacy 路径。

        会话固定: 同一线程内连续 ask 复用同一个 token (命中缓存省钱),
        任务边界请调用 begin_task() 让池重新均衡。线程隔离, 多线程安全。

        Args:
            system: 系统提示
            user_message: 用户消息
            temperature: 温度 (0-2)

        Returns:
            LLM 响应的文本内容
        """
        pool = self._ensure_pool()
        if pool:
            try:
                return self._ask_with_pool(pool, system, user_message, temperature)
            except self._pool_mod.QuotaExhaustedError:
                # 2026-10-15 net_fail自愈(DeepSeek崩溃事故双保险): 全灭先force重探
                # 一次再放弃 — 探测进程死亡+Redis陈旧时(重启事故), net_fail躺死但
                # key实际是活的。节流: 进程内5分钟最多一次(force探测慢, 真全灭时
                # 不能每次ask都拖一轮)。
                now = time.time()
                if now - self.__class__._last_force_reprobe > 300:
                    self.__class__._last_force_reprobe = now
                    try:
                        alive = pool.available(refresh="force")
                        if alive:
                            log.warning("token 池自愈: force重探复活 %d 个token, 继续池路径", len(alive))
                            return self._ask_with_pool(pool, system, user_message, temperature)
                    except Exception as re:
                        log.warning("force重探失败: %.80s", re)
                log.warning("token 池全部额度耗尽, 回退环境变量 key")
                self._rebuild_with_key(None)  # 恢复环境变量 key 的客户端
            except Exception as e:
                log.error("LLM ask 兜底降级: type=%s detail=%.120s", type(e).__name__, e)
                return f'{{"error": "LLM call failed: {e}", "degraded": true}}'
        return self._ask_legacy(system, user_message, temperature)

    # ---------- token 池 failover ----------

    def _ensure_pool(self):
        """懒加载 token 池 (线程安全, 双检锁); redis/池不可用则返回 None (禁用池, 回退环境变量 key)"""
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:   # 双检: 多线程首次并发 ask 时只初始化一次
                return self._pool
            try:
                tk_dir = Path(__file__).resolve().parent.parent / "token manager"
                if str(tk_dir) not in sys.path:
                    sys.path.insert(0, str(tk_dir))
                import token_pool
                self._pool_mod = token_pool
                self._pool = token_pool.TokenPool()
                log.info("token 池已启用 (镜像双写), 池内 %d 个 token", len(self._pool.list()))
            except Exception as e:
                log.warning("token 池不可用, 回退环境变量 key: %s", e)
                self._pool = False
            return self._pool

    def _rebuild_with_key(self, key: Optional[str]):
        """用指定 key 重建客户端 (None=恢复环境变量 key 优先级)"""
        if self._provider == "deepseek":
            self._init_deepseek(key)
        else:
            self._init_anthropic(key)

    def _classify_llm_error(self, e) -> Optional[str]:
        """SDK 异常 -> 'no_quota' / 'invalid' / None(其他, 原样抛出不吞)"""
        status = getattr(e, "status_code", None)
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            try:
                text = json.dumps(body, ensure_ascii=False)
            except Exception:
                text = str(body)
        else:
            text = str(body or e)
        low = text.lower()
        if "insufficient_user_quota" in text or "当月额度已用完" in text or "quota" in low:
            return "no_quota"
        if status == 401 or any(k in low for k in (
            "无效的令牌", "unauthor", "invalid api key", "invalid_token", "authentication", "认证失败"
        )):
            return "invalid"
        return None

    def begin_task(self, sticky_key: str = None):
        """任务边界: 开始一个新任务(如处理一个学院)时调用。

        清除当前线程的会话固定 key, 让 token 池重新做负载均衡分配;
        任务内的连续 ask 会保持同一个 token (命中网关 prompt 缓存, 价格更便宜),
        直到该 token 额度耗尽/密钥无效才自动切换。

        sticky_key (2026-08-23): 学院亲和 — 传学院名时该线程固定到
        hash(学院名) 槽位 token (md5 稳定, 跨批次/重启不变), 同一学院的
        重复任务(队列重试/改进循环)命中上次的 prompt 缓存, 且 10+ token
        按学院均匀分散负载 (多 token 并行输出, 缓解单 token 网关卡顿)。
        None → 原最少消耗 + 轮询分配。

        并发说明:
        - 只影响**当前线程** (固定 key 存于 threading.local), 其他线程不受影响;
        - 每个线程应在自己处理的任务开始时调用一次 (多线程并行处理任务时, 各线程
          调用各自的 begin_task, 互不干扰);
        - 不调用 begin_task 的线程: 首次 ask 时自动分配并固定一个 key, 之后一直
          复用, 直到额度耗尽自动切换。
        """
        self._local.sk = None
        self._local.sticky = sticky_key

    def _ask_with_pool(self, pool, system: str, user_message: str, temperature: float) -> str:
        """循环 token 池调用: 会话固定 key 优先, 额度耗尽/密钥无效自动标记并切换。

        会话固定: 同一任务内的连续 ask 复用同一个 token (get(prefer=线程sk)),
        命中网关侧 prompt 缓存省钱; 发现 no_quota/invalid 后清空固定 key,
        下一次 get 重新做负载均衡分配 (最少消耗优先 + 轮询)。

        并发安全: 客户端实例 / 固定 key / 用量记账均按线程隔离 (threading.local),
        多个线程并发调用互不串 key; 每个线程只重建自己线程内的 client。

        日志只输出脱敏 key (mask_sk), 不含姓名与请求/响应内容。
        """
        qee = self._pool_mod.QuotaExhaustedError
        mask_sk = self._pool_mod.mask_sk
        local = self._local
        tried = set()
        for _ in range(max(1, len(pool.list()))):
            tok = pool.get(prefer=getattr(local, "sk", None),
                           sticky=getattr(local, "sticky", None))
            sk = tok["sk"]
            if sk in tried:
                continue
            tried.add(sk)
            # per-token 并发槽 (2026-08-24): 同一 token 同时最多 LLM_PER_TOKEN 路。
            # 覆盖 rebuild + 调用全程 — 并发线程不得同时重建同一 key 的 client。
            # 无死锁: 每线程至多持有一个槽; 切换 token(continue) 时 with 先释放当前槽。
            with _token_sem(sk):
                if sk != getattr(local, "sk", None):
                    self._rebuild_with_key(sk)
                    local.sk = sk
                    if not self.available:
                        # SDK 构造瞬态失败: 重试一次再判定, 避免误杀有效 key
                        time.sleep(1)
                        self._rebuild_with_key(sk)
                    if not self.available:
                        log.warning("池 key [%s] 客户端初始化失败, 标记无效并切换", mask_sk(sk))
                        pool._mark(sk, "invalid", "client init failed")
                        local.sk = None
                        continue
                try:
                    if self._provider == "deepseek":
                        text, usage = self._call_deepseek(system, user_message, temperature)
                    else:
                        text, usage = self._call_anthropic(system, user_message, temperature)
                    # 用量上报: 记账失败静默, 不影响主流程
                    if usage and (usage.get("in") or usage.get("out")):
                        try:
                            pool.record_usage(sk, usage.get("in", 0), usage.get("out", 0))
                        except Exception:
                            pass
                    return text
                except Exception as e:
                    code = self._classify_llm_error(e)
                    if code == "no_quota":
                        pool._mark(sk, "no_quota", str(e)[:120])
                        log.warning("调用中额度耗尽 [%s], 已标记无余额并切换下一可用 token", mask_sk(sk))
                        local.sk = None  # 固定 key 失效, 下次 get 重新均衡分配
                        continue
                    if code == "invalid":
                        pool._mark(sk, "invalid", str(e)[:120])
                        log.warning("密钥无效 [%s], 已标记并切换下一可用 token", mask_sk(sk))
                        local.sk = None
                        continue
                    # 非额度/密钥类错误(超时/5xx/网络): 2026-08-29 补详细日志,
                    # 原样抛出前记录类型/状态码/响应体片段, 供网关波动根因分析
                    status = getattr(e, "status_code", None)
                    log.error("LLM 调用异常 [%s] type=%s status=%s detail=%.120s",
                              mask_sk(sk), type(e).__name__, status, e)
                    raise
        raise qee()

    def _ask_legacy(self, system: str, user_message: str, temperature: float) -> str:
        """环境变量 key 路径 (原 ask 逻辑, 池不可用/耗尽时的兜底)"""
        if not self.available:
            # 2026-10-19 重启空转根修(0915 批实锤): 重启后 token 探测进程
            # 未起/Redis 陈旧 → available=False → 直接 degraded 全程空转;
            # force 重探只在池路径 QuotaExhaustedError 分支, 从未被触发。
            # 这里补: 节流(进程级 300s)force 重探一次, 复活则走正常池路径。
            import time as _t
            _now = _t.time()
            if _now - self.__class__._last_force_reprobe > 300:
                self.__class__._last_force_reprobe = _now
                try:
                    _pool2 = self._ensure_pool()
                    if _pool2:
                        _alive = _pool2.available(refresh="force")
                        if _alive:
                            log.warning("token 池自愈(available=False→force重探复活 %d 个), 走池路径",
                                        len(_alive))
                            return self._ask_with_pool(_pool2, system, user_message, temperature)
                except Exception as _re:
                    log.warning("force重探失败: %.80s", _re)
            return self._degraded_response(user_message)

        try:
            if self._provider == "deepseek":
                return self._ask_deepseek(system, user_message, temperature)
            else:
                return self._ask_anthropic(system, user_message, temperature)
        except Exception as e:
            return f'{{"error": "LLM call failed: {e}", "degraded": true}}'

    def _ask_anthropic(self, system: str, user_message: str, temperature: float) -> str:
        """通过 Anthropic API 发送消息（支持 Claude + DeepSeek 兼容后端）。"""
        text, _ = self._call_anthropic(system, user_message, temperature)
        return text

    def _call_anthropic(self, system: str, user_message: str, temperature: float):
        """调用 Anthropic API, 返回 (文本, {"in": input_tokens, "out": output_tokens})

        2026-09-20 模型顺序降级: DeepSeek → GLM → Gemini — 超时/网关错误换
        下一模型; 额度/密钥错误与连接错误原样抛出(前者交 token 池换 key,
        后者换模型无意义)。
        """
        local = self._local
        cfg = getattr(local, "cfg", {})
        client = getattr(local, "client", None)
        thinking = cfg.get("thinking", False)

        last_err = None
        for model in _model_chain(cfg["model"]):
            kwargs = {
                "model": model,
                "max_tokens": cfg.get("max_tokens", 16000),
                "temperature": temperature,
                "messages": [{"role": "user", "content": user_message}],
            }

            # system prompt: Anthropic SDK 支持 system 参数
            if system:
                kwargs["system"] = system

            # thinking: 仅 Claude 支持，DeepSeek 不兼容
            if thinking:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": cfg.get("thinking_budget", 4000),
                }

            try:
                response = client.messages.create(**kwargs)
            except Exception as e:
                code = self._classify_llm_error(e)
                if code in ("no_quota", "invalid"):
                    raise  # key 问题: 交 token 池切换 key
                if _is_connect_error(e):
                    raise  # 真连不上: 换模型无意义
                last_err = e
                log.warning("模型 [%s] 失败(%s: %.80s), 切换下一模型", model, type(e).__name__, e)
                continue

            text_parts = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)

            usage = {}
            if getattr(response, "usage", None):
                usage = {
                    "in": getattr(response.usage, "input_tokens", 0) or 0,
                    "out": getattr(response.usage, "output_tokens", 0) or 0,
                }
            return "\n".join(text_parts), usage
        raise last_err

    def _ask_deepseek(self, system: str, user_message: str, temperature: float) -> str:
        """通过 DeepSeek API (OpenAI 兼容) 发送消息。"""
        text, _ = self._call_deepseek(system, user_message, temperature)
        return text

    def _call_deepseek(self, system: str, user_message: str, temperature: float):
        """调用 DeepSeek API (OpenAI 兼容), 返回 (文本, {"in": prompt_tokens,
        "out": completion_tokens})。2026-09-20 模型顺序降级: DeepSeek →
        GLM → Gemini — 超时/网关错误换下一模型; 额度/密钥错误与连接错误原样抛。"""
        local = self._local
        cfg = getattr(local, "cfg", {})
        client = getattr(local, "client", None)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_message})

        last_err = None
        for model in _model_chain(cfg["model"]):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=cfg.get("max_tokens", 8192),
                    temperature=temperature if temperature else cfg.get("temperature", 0.3),
                )
            except Exception as e:
                code = self._classify_llm_error(e)
                if code in ("no_quota", "invalid"):
                    raise  # key 问题: 交 token 池切换 key
                if _is_connect_error(e):
                    raise  # 真连不上: 换模型无意义
                last_err = e
                log.warning("模型 [%s] 失败(%s: %.80s), 切换下一模型", model, type(e).__name__, e)
                continue

            choice = response.choices[0]
            content = choice.message.content

            usage = {}
            if getattr(response, "usage", None):
                usage = {
                    "in": getattr(response.usage, "prompt_tokens", 0) or 0,
                    "out": getattr(response.usage, "completion_tokens", 0) or 0,
                }
            return (content if content else ""), usage
        raise last_err

    def _degraded_response(self, prompt: str) -> str:
        """LLM 不可用时的降级响应。"""
        import json as _json

        if "json" in prompt.lower() or "```json" in prompt.lower():
            return _json.dumps({
                "degraded": True,
                "note": "LLM unavailable. Set LLM_API_KEY in .env "
                        "(or DEEPSEEK_API_KEY / ANTHROPIC_API_KEY).",
                "site_type": "html-index",
                "confidence": "low",
            }, ensure_ascii=False)

        return "{}"


if __name__ == "__main__":
    llm = SimpleLLM()
    print(f"Provider: {llm.provider}")
    print(f"Available: {llm.available}")
    print(f"Model: {llm.model}")

    if llm.available:
        result = llm.ask(
            system="你是爬虫专家。",
            user_message="用一句话解释什么是瑞数反爬的 412 挑战页。",
        )
        print(f"\nResponse: {result}")
    else:
        print("LLM not available — 在 .env 配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
