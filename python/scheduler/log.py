"""log.py — 结构化日志 + 请求耗时追踪。

用法:
  from .log import info, warn, error, timed, get_logger
  info("orchestrator", "loop started")
  warn("memory", "embed model not available")
  error("dispatcher", f"dispatch failed: {e}")

  @timed
  def dispatch(...): ...

日志级别: LOG_LEVEL 环境变量 (DEBUG/INFO/WARNING/ERROR), 默认 INFO。
"""

from __future__ import annotations
import logging
import os
import time
from functools import wraps

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"

# ── 根 logger 配置 ──────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format=_LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log = logging.getLogger("scheduler")


def get_logger(name: str = "") -> logging.Logger:
    """获取命名 logger。空字符串返回根 logger。"""
    return logging.getLogger(name) if name else _log


# ── @timed 装饰器 ───────────────────────────────────────────
def timed(func=None, *, name: str = ""):
    """装饰器：记录函数进入/退出及耗时(ms)。异常时记录完整 traceback。

    用法:
      @timed
      def dispatch(...): ...

      @timed(name="router")
      def route(...): ...
    """
    def decorator(f):
        logger = logging.getLogger(name or "scheduler")
        @wraps(f)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            logger.info("%s start", f.__name__)
            try:
                result = f(*args, **kwargs)
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("%s done | %.0fms", f.__name__, elapsed)
                return result
            except Exception:
                elapsed = (time.perf_counter() - t0) * 1000
                logger.exception("%s failed | %.0fms", f.__name__, elapsed)
                raise
        return wrapper
    if func is not None:
        return decorator(func)
    return decorator


# ── 向后兼容的便捷函数 ──────────────────────────────────────
def info(module: str, msg: str) -> None:
    logging.getLogger(module).info(msg)


def warn(module: str, msg: str) -> None:
    logging.getLogger(module).warning(msg)


def error(module: str, msg: str) -> None:
    logging.getLogger(module).error(msg)
