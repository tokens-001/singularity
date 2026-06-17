#!/usr/bin/env python3
"""desktop.py — 奇点调度平台 macOS 原生桌面壳

使用 pywebview (WKWebView) 包一层原生窗口。
Flask 后端在后台线程跑，窗口加载 http://127.0.0.1:5050。

用法:
  python3 desktop.py           # 启动桌面窗口
  python3 desktop.py --web     # 只启动 Web 服务（不弹窗口）
"""

import sys
import threading
from pathlib import Path

import webview

# ── 确保 app.py 可导入 ──────────────────────────────────
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

from app import app as flask_app


def start_flask(host: str = "127.0.0.1", port: int = 5050) -> None:
    """在后台线程启动 Flask。"""
    flask_app.run(host=host, port=port, debug=False, use_reloader=False)


def main() -> None:
    args = sys.argv[1:]

    # ── 后台启动 Flask ──
    flask_thread = threading.Thread(
        target=start_flask,
        kwargs={"host": "127.0.0.1", "port": 5050},
        daemon=True,
    )
    flask_thread.start()

    if "--web" in args:
        # 只跑 Web 服务
        print("奇点调度面板 → http://127.0.0.1:5050")
        flask_thread.join()
        return

    # ── macOS 原生窗口 ──
    print("⚡ 奇点调度平台启动中...")
    webview.create_window(
        title="奇点 Agent 调度平台",
        url="http://127.0.0.1:5050",
        width=1200,
        height=800,
        min_size=(800, 500),
        text_select=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
