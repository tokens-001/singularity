"""log.py — 简单文件日志,替代散落的 print()。

用法:
  from .log import info, warn, error
  info("orchestrator", "loop started")
  warn("memory", "embed model not available")
  error("dispatcher", f"dispatch failed: {e}")
"""

from __future__ import annotations
import time
from pathlib import Path
from . import config


def _log_path() -> Path:
    d = config.QIDIAN_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "scheduler.log"


def _write(level: str, module: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {level:<5} [{module}] {msg}\n"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 日志失败不阻塞主流程


def info(module: str, msg: str) -> None:
    _write("INFO", module, msg)


def warn(module: str, msg: str) -> None:
    _write("WARN", module, msg)


def error(module: str, msg: str) -> None:
    _write("ERROR", module, msg)
