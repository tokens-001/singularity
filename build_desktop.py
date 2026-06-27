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
# ponytail: PyInstaller cwd must be project root so singularity package is importable
sys.path.insert(0, str(APP_DIR / "src"))
DIST_DIR = APP_DIR / "dist"
APP_NAME = "Singularity Dispatch"


def build() -> None:
    print(f"打包 {APP_NAME} ...")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",
        "--windowed",  # 无终端窗口
        "--add-data", f"{APP_DIR}/src/singularity/web/templates:templates",
        "--hidden-import", "singularity",
        "--hidden-import", "singularity.scheduler",
        "--hidden-import", "singularity.scheduler.memory",
        "--hidden-import", "singularity.scheduler.orchestrator",
        "--hidden-import", "singularity.scheduler.pre_search",
        "--hidden-import", "singularity.scheduler.tracker",
        "--hidden-import", "singularity.scheduler.config",
        "--hidden-import", "singularity.scheduler.router",
        "--hidden-import", "singularity.scheduler.dispatcher",
        "--hidden-import", "singularity.scheduler.validator",
        "--hidden-import", "singularity.scheduler.neijinglu",
        "--hidden-import", "singularity.scheduler.merge",
        "--hidden-import", "singularity.scheduler.witness",
        "--hidden-import", "singularity.scheduler.snapshot",
        "--hidden-import", "flask",
        "--hidden-import", "webview",
        "--distpath", str(DIST_DIR),
        "--workpath", str(APP_DIR / "build"),
        str(APP_DIR / "desktop.py"),
    ]

    subprocess.run(cmd, cwd=str(APP_DIR), check=True)

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
