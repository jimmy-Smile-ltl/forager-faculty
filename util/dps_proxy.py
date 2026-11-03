#!/usr/bin/env python3
"""私密代理(dps API)站级粘性管理 — 2026-10-24 用户购置快代理私密代理后新增。

与隧道代理的本质区别: API 提取的 IP 是固定分配的(同 IP 反复请求出口不变,
实测 3 次请求出口一致), 可做「站级粘性」— 同域名固定绑定一个 IP:
  - 瑞数站: 破解链在同一 IP 下求解 → cookie 与该 IP 绑定 → 粘性保证 cookie
    不失效(隧道漂移时代「瑞数禁代理」的历史约束被打破的前提)
  - 普通封锁站: 固定出口 IP 避免会话断裂触发风控

配置 (.env):
    PROXY_MODE=dps
    DPS_API_URL=https://dps.kdlapi.com/api/getdps/?secret_id=xxx&signature=xxx&num=1&sep=1
    DPS_USER=<your-dps-user>
    DPS_PWD=<your-dps-pwd>

用法:
    from dps_proxy import get_proxies_for_host, mark_host_failed
    px = get_proxies_for_host("www.cqu.edu.cn")   # -> {"http":..., "https":...} 或 None
"""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

DPS_API_URL = os.environ.get("DPS_API_URL", "").strip()
DPS_USER = os.environ.get("DPS_USER", "").strip()
DPS_PWD = os.environ.get("DPS_PWD", "").strip()

_BIND_FILE = Path(__file__).resolve().parent.parent / "logs" / "_dps_bindings.json"
_LOCK = threading.Lock()
_bindings: dict = {}       # host -> {"ip": "ip:port", "t": epoch}
_failed: dict = {}         # host -> 失败次数
_BAD_IPS: set = set()      # 失效 IP 黑名单(会话内)
_loaded = False

# API 提取节流: 私密代理 API 有调用频率限制, 最少间隔秒
_MIN_INTERVAL = 5
_last_extract = 0.0


def _load():
    global _bindings, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if _BIND_FILE.exists():
            _bindings = json.loads(_BIND_FILE.read_text(encoding="utf-8"))
    except Exception:
        _bindings = {}


def _save():
    try:
        _BIND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BIND_FILE.write_text(json.dumps(_bindings, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:
        pass


def _extract_ip() -> str:
    """调 dps API 提取一个代理 IP, 失败返回空串。带节流+简单重试。"""
    global _last_extract
    if not DPS_API_URL:
        return ""
    with _LOCK:
        wait = _MIN_INTERVAL - (time.time() - _last_extract)
        if wait > 0:
            time.sleep(wait)
        _last_extract = time.time()
    try:
        import requests
        for attempt in range(2):
            try:
                r = requests.get(DPS_API_URL, timeout=20)
                ip = r.text.strip()
                if ":" in ip and not ip.startswith("ERROR"):
                    return ip
                # 提取失败(ERROR/限额) → 退避重试一次
                time.sleep(3)
            except Exception:
                time.sleep(3)
    except Exception:
        pass
    return ""


def _proxy_url(ip: str) -> str:
    return f"http://{DPS_USER}:{DPS_PWD}@{ip}/"


def get_proxies_for_host(host: str) -> dict:
    """返回该 host 绑定的粘性代理字典; 无绑定则提取新 IP 绑定。
    全部失败返回 None(调用方直连兜底)。"""
    if not host or not DPS_API_URL:
        return None
    _load()
    with _LOCK:
        ent = _bindings.get(host)
        if ent and ent.get("ip") and ent["ip"] not in _BAD_IPS:
            return {"http": _proxy_url(ent["ip"]), "https": _proxy_url(ent["ip"])}
    ip = _extract_ip()
    if not ip:
        return None
    with _LOCK:
        _bindings[host] = {"ip": ip, "t": time.time()}
    _save()
    return {"http": _proxy_url(ip), "https": _proxy_url(ip)}


def mark_host_failed(host: str):
    """该 host 的绑定 IP 疑似失效(407/连续失败) → 拉黑该 IP, 下次重新提取。"""
    _load()
    with _LOCK:
        ent = _bindings.get(host)
        if ent and ent.get("ip"):
            _BAD_IPS.add(ent["ip"])
            _bindings.pop(host, None)
        _failed[host] = _failed.get(host, 0) + 1
    _save()


def current_binding(host: str) -> str:
    """当前绑定 IP(诊断用), 无则空。"""
    _load()
    ent = _bindings.get(host)
    return (ent or {}).get("ip", "")


if __name__ == "__main__":
    import sys
    h = sys.argv[1] if len(sys.argv) > 1 else "www.cqu.edu.cn"
    px = get_proxies_for_host(h)
    print(f"{h} → {px}")
