#!/usr/bin/env python3
"""build_desktop.py — 打包Singularity桌面为 macOS .app

用法:
  python3 build_desktop.py          # 打包
  open dist/Singularity Dispatch.app          # 打开

依赖: pyinstaller
"""

import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent
ROOT = APP_DIR.parent
DIST_DIR = APP_DIR / "dist"
APP_NAME = "Singularity Dispatch"


def build() -> None:
    print(f"打包 {APP_NAME} ...")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",
        "--windowed",  # 无终端窗口
        "--add-data", f"templates:templates",
        "--hidden-import", "scheduler",
        "--hidden-import", "scheduler.memory",
        "--hidden-import", "scheduler.orchestrator",
        "--hidden-import", "scheduler.pre_search",
        "--hidden-import", "scheduler.tracker",
        "--hidden-import", "scheduler.config",
        "--hidden-import", "scheduler.router",
        "--hidden-import", "scheduler.dispatcher",
        "--hidden-import", "scheduler.validator",
        "--hidden-import", "scheduler.neijinglu",
        "--hidden-import", "scheduler.merge",
        "--hidden-import", "scheduler.witness",
        "--hidden-import", "scheduler.snapshot",
        "--hidden-import", "flask",
        "--hidden-import", "webview",
        "--distpath", str(DIST_DIR),
        "--workpath", str(APP_DIR / "build"),
        str(APP_DIR / "desktop.py"),
    ]

    subprocess.run(cmd, cwd=str(ROOT), check=True)

    app_path = DIST_DIR / f"{APP_NAME}.app"
    if app_path.exists():
        print(f"\n✅ 打包成功: {app_path}")
        print(f"   open {app_path}")
    else:
        onedir = DIST_DIR / APP_NAME
        if onedir.is_dir():
            print(f"\n✅ 打包成功: {onedir}")
            print(f"   open {onedir}")


if __name__ == "__main__":
    build()
