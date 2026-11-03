#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地代理转发器 — 给 DrissionPage 等不支持代理认证的客户端提供无认证代理出口。

背景(2026-08-23): 30 并发采集时瑞数站点走 DrissionPage JS 渲染路径, 但 Chromium 的
--proxy-server 不支持 URL 内嵌认证(user:pass@), 导致浏览器直连、单 IP 高频被瑞数风控拦截。
本转发器监听 127.0.0.1, 上游指向快代理隧道(PROXY_URL, 带认证), 客户端无感使用。

设计:
  - HTTP: 转发原始请求 + Proxy-Authorization 头
  - HTTPS: CONNECT 隧道, 双向字节透传
  - 每请求新建上游连接、用完即关: 快代理隧道每次新连接自动切换出口 IP
    (隧道要求勿复用 keep-alive 连接, 否则 IP 不切换)
  - 凭证从 college teacher/.env 的 PROXY_URL 读取, 不硬编码

用法: uv run python util/local_proxy.py [--port 15819]
"""
import argparse
import base64
import os
import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── 上游代理凭证 (college teacher/.env) ──
try:
    from dotenv import load_dotenv
    _shell_proxy_mode = os.environ.get("PROXY_MODE")  # shell 显式设置优先（download_loop.sh PROXY_MODE=off）
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    if _shell_proxy_mode is not None:
        os.environ["PROXY_MODE"] = _shell_proxy_mode  # 防 .env 覆盖 shell（2026-09-03: 快代理失效期全直连）
except Exception:
    pass

UPSTREAM = os.environ.get("PROXY_URL", "").strip() or None
PROXY_MODE = os.environ.get("PROXY_MODE", "static").strip() or "static"


def _parse_upstream(url: str):
    """PROXY_URL 格式: http://user:pass@host:port/ → (host, port, auth_header)"""
    rest = url.split("://", 1)[1]
    auth, _, hostport = rest.rpartition("@")
    host, _, port = hostport.strip("/").partition(":")
    port = int(port or 80)
    if auth and ":" in auth:
        u, _, p = auth.partition(":")
        cred = base64.b64encode(f"{u}:{p}".encode()).decode()
        return host, port, f"Proxy-Authorization: Basic {cred}\r\n"
    return host, port, ""  # 白名单模式: 无需认证


UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_AUTH = _parse_upstream(UPSTREAM) if UPSTREAM else ("", 0, "")


def _conn_upstream():
    """新建上游连接(每次新连接 → 隧道切换出口 IP)"""
    s = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=30)
    return s


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 不让 BaseHTTPRequestHandler 打印默认日志(日志在 concurrency 计数处打)
    def log_message(self, *a):
        pass

    def _relay(self, sock_in, sock_out, bufsize=65536):
        """双向字节透传 (CONNECT 隧道后半段 / 上游响应回传)"""
        try:
            while True:
                r, _, _ = select.select([sock_in, sock_out], [], [], 120)
                if not r:
                    continue
                for s in r:
                    data = s.recv(bufsize)
                    if not data:
                        return
                    (sock_out if s is sock_in else sock_in).sendall(data)
        except Exception:
            pass
        finally:
            try:
                sock_in.close()
            except Exception:
                pass
            try:
                sock_out.close()
            except Exception:
                pass

    def _do_connect(self):
        """HTTPS: CONNECT 到上游, 200 后双向透传"""
        host, _, port = self.path.partition(":")
        port = int(port or 443)
        if not UPSTREAM:
            self.send_error(502, "上游代理未配置")
            return
        try:
            up = _conn_upstream()
        except Exception:
            self.send_error(502, "上游连接失败")
            return
        try:
            up.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{UPSTREAM_AUTH}\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = up.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b" 200 " not in resp.split(b"\r\n")[0]:
                up.close()
                self.send_error(502, "上游 CONNECT 被拒")
                return
            self.send_response(200, "Connection Established")
            self.end_headers()
        except Exception:
            try:
                up.close()
            except Exception:
                pass
            return
        self._relay(self.connection, up)

    def _do_http(self, method):
        """HTTP: 原样转发 + 代理认证头"""
        if not UPSTREAM:
            self.send_error(502, "上游代理未配置")
            return
        body = b""
        if "Content-Length" in self.headers:
            body = self.rfile.read(int(self.headers["Content-Length"]))
        req = f"{method} {self.path} HTTP/1.1\r\n"
        for k, v in self.headers.items():
            if k.lower() in ("proxy-authorization", "proxy-connection"):
                continue
            req += f"{k}: {v}\r\n"
        req += UPSTREAM_AUTH + "Proxy-Connection: close\r\n\r\n"
        try:
            up = _conn_upstream()
            up.sendall(req.encode() + body)
            resp = b""
            while True:
                chunk = up.recv(65536)
                if not chunk:
                    break
                resp += chunk
            up.close()
            self.connection.sendall(resp)
        except Exception:
            self.send_error(502, "上游转发失败")
        finally:
            self.connection.close()

    def do_CONNECT(self):
        self._do_connect()

    def do_GET(self):
        self._do_http("GET")

    def do_POST(self):
        self._do_http("POST")

    def do_PUT(self):
        self._do_http("PUT")

    def do_DELETE(self):
        self._do_http("DELETE")

    def do_HEAD(self):
        self._do_http("HEAD")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15819)
    args = ap.parse_args()
    # 2026-09-03: 快代理失效期 PROXY_MODE=off 全直连 — 转发器启动即退,
    # 防止 watchdog 每轮重生转发器→浏览器走代理→517 反复出现
    # (watchdog 无 PROXY_MODE 环境, 需读 .env 的 PROXY_MODE)。
    if PROXY_MODE == "off":
        print("⛔ PROXY_MODE=off 全直连期, 本地代理转发器不启动 (watchdog 守护已同步跳过)")
        sys.exit(0)
    if not UPSTREAM:
        print("⛔ PROXY_URL 未配置 (college teacher/.env), 退出")
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"本地代理转发器: 127.0.0.1:{args.port} → {UPSTREAM_HOST}:{UPSTREAM_PORT} "
          f"(模式 {PROXY_MODE}, 每请求新连接切IP)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
