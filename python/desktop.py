#!/usr/bin/env python3
"""desktop.py — 奇点调度平台 macOS 原生桌面壳

使用 pywebview (WKWebView) 包一层原生窗口。
Flask 后端在后台线程跑，窗口加载 http://127.0.0.1:5050。

用法:
  python3 desktop.py           # 启动桌面窗口
  python3 desktop.py --web     # 只启动 Web 服务（不弹窗口）

打包 (macOS .app):
  python3 build_desktop.py
"""

import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

# ── 确保 app.py 可导入 ──────────────────────────────────
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

from app import app as flask_app

HOST = "127.0.0.1"
PORT = 5050
BASE_URL = f"http://{HOST}:{PORT}"


def start_flask() -> None:
    """在后台线程启动 Flask。"""
    flask_app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


def wait_for_flask(timeout: float = 10.0) -> bool:
    """等待 Flask 就绪。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"{BASE_URL}/api/status", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> None:
    args = sys.argv[1:]

    # ── 后台启动 Flask ──
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    if "--web" in args:
        print(f"奇点调度面板 → {BASE_URL}")
        if wait_for_flask():
            print("Flask 已就绪")
        flask_thread.join()
        return

    # ── macOS 原生窗口 ──
    print("⚡ 奇点调度平台启动中...")

    if not wait_for_flask():
        print(f"⚠ Flask 未能在 {PORT} 端口启动, 可能有旧进程占用", file=sys.stderr)
        print(f"  试试: lsof -i :{PORT} | grep LISTEN", file=sys.stderr)

    webview.create_window(
        title="奇点 Agent 调度平台",
        url=BASE_URL,
        width=1200,
        height=800,
        min_size=(800, 500),
        text_select=True,
        easy_drag=False,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
