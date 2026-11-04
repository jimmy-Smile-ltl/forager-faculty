"""v3: 导航式抓取 QS WUR 2027 全部 51 页 (浏览器会话内)

endpoint 响应 JSON 在页面 <pre> 标签内。nid=4153156, page 从 0 开始。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from DrissionPage import ChromiumOptions, ChromiumPage

CHROME_PATH = os.environ.get("QS_CHROME_PATH", "")
PORT = 9335
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".chrome_profile")
PAGE_URL = (
    "https://www.topuniversities.com/world-university-rankings"
    "?tab=indicators&sort_by=rank&order_by=asc"
)
NID = "4153156"
RAW_DIR = os.path.join(HERE, "raw")
CSV_PATH = os.path.join(HERE, "qs_rankings_2027.csv")


def endpoint_url(nid: str, page: int, per: int = 30) -> str:
    return (f"https://www.topuniversities.com/rankings/endpoint?nid={nid}"
            f"&page={page}&items_per_page={per}&tab=indicators&region=&countries="
            f"&cities=&search=&star=&sort_by=rank&order_by=asc&program_type=&"
            f"scholarship=&fee=&english_score=&academic_score=&mix_student=&"
            f"loggedincache=&study_level=&subjects=")


def port_free(port: int, timeout: float = 0.3) -> bool:
    import socket
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return False
        except OSError:
            pass
    return True


def read_ws_url() -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
            return json.loads(r.read())["webSocketDebuggerUrl"]
    except Exception:
        return None


def launch_chrome() -> str:
    # 红线：必须使用项目独立 Chromium（CloakBrowser / playwright Chrome for Testing），
    # 禁止指向个人 Chrome 安装路径
    if not CHROME_PATH or not os.path.exists(CHROME_PATH):
        raise SystemExit(
            "QS_CHROME_PATH 必须指向项目独立 Chromium"
            "（CloakBrowser 或 playwright Chrome for Testing），禁止个人 Chrome")
    subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={PORT}"],
                   capture_output=True, timeout=10)
    for _ in range(30):
        if port_free(PORT):
            break
        time.sleep(0.5)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(PROFILE_DIR, lock)
        try:
            if os.path.islink(p):
                os.unlink(p)
        except OSError:
            pass
    subprocess.Popen(
        [CHROME_PATH, f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
         f"--user-data-dir={PROFILE_DIR}", "--no-first-run",
         "--no-default-browser-check", "--disable-background-networking",
         "--window-size=1440,900", "--lang=en-US", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        ws = read_ws_url()
        if ws:
            return ws
        time.sleep(0.5)
    raise RuntimeError("Chrome CDP 启动失败")


def get_page() -> ChromiumPage:
    ws = launch_chrome()
    co = ChromiumOptions()
    co.set_address(f"127.0.0.1:{PORT}")
    co.headless(False)
    page = None
    for attempt in range(3):
        try:
            if not port_free(PORT):
                page = ChromiumPage(addr_or_opts=co)
                if read_ws_url() == ws:
                    break
                page.quit()
            page = None
        except Exception:
            pass
        time.sleep(2)
    if page is None:
        raise RuntimeError("连接 Chrome 失败")
    return page


CF_MARKS = ("just a moment", "请稍候", "稍候", "检查你的浏览器")


def wait_cf_pass(page: ChromiumPage, timeout: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            title = page.title or ""
        except Exception:
            title = ""
        if title and all(m not in title.lower() for m in CF_MARKS):
            return
        time.sleep(2)
    raise RuntimeError(f"Cloudflare 挑战超时未通过, 标题: {page.title!r}")


def nav_fetch_json(page: ChromiumPage, url: str, retry: int = 3) -> dict:
    """导航到 endpoint, 从 <pre> 里提取 JSON"""
    for i in range(retry):
        page.get(url)
        for _ in range(40):
            html = page.html or ""
            m = re.search(r"<pre>(.*)</pre>", html, re.S)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    time.sleep(0.5)
                    continue
            if "just a moment" in html.lower() or "请稍候" in html:
                break  # 又遇 CF 挑战, 重试导航
            time.sleep(0.5)
        time.sleep(1.5)
    raise RuntimeError(f"导航抓取失败: {url}")


def main() -> int:
    os.makedirs(RAW_DIR, exist_ok=True)
    page = get_page()
    try:
        print("[1] 过 CF 挑战...")
        page.get(PAGE_URL)
        wait_cf_pass(page)
        print(f"[2] 页面标题: {page.title}")

        j0 = nav_fetch_json(page, endpoint_url(NID, 0))
        nodes = j0.get("score_nodes", [])
        print(f"[3] page 0: total_record={j0.get('total_record')}, "
              f"total_pages={j0.get('total_pages')}, 本页 {len(nodes)} 条")
        if nodes:
            print("    字段:", sorted(nodes[0].keys()))
            print("    样例(完整字段值, 截断):")
            for k, v in nodes[0].items():
                s = str(v)
                print(f"      {k} = {s[:90]}{'...' if len(s) > 90 else ''}")

        total_pages = j0.get("total_pages") or 51
        all_nodes = []
        for pg in range(total_pages):
            j = nav_fetch_json(page, endpoint_url(NID, pg))
            nodes = j.get("score_nodes", [])
            all_nodes.extend(nodes)
            with open(os.path.join(RAW_DIR, f"page_{pg+1}.json"), "w") as f:
                json.dump(j, f, ensure_ascii=False)
            print(f"    page {pg+1}/{total_pages}: +{len(nodes)}, 累计 {len(all_nodes)}")
            time.sleep(0.3)

        with open(os.path.join(RAW_DIR, "all_score_nodes.json"), "w") as f:
            json.dump(all_nodes, f, ensure_ascii=False)
        print(f"[4] 完成: {len(all_nodes)} 条")
        return 0
    finally:
        try:
            page.quit()
        except Exception:
            pass
        subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={PORT}"],
                       capture_output=True, timeout=10)


if __name__ == "__main__":
    sys.exit(main())
