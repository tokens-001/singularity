"""log.py — structured JSON logging + trace_id + timing.

v2: contextvars trace_id, JSON output, log_event().
"""
from __future__ import annotations
import json as _json, logging, os, time, uuid
from contextvars import ContextVar
from functools import wraps

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        obj = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if hasattr(record, "trace_id"): obj["trace_id"] = record.trace_id
        if record.exc_info and record.exc_info[0]: obj["exc"] = self.formatException(record.exc_info)
        return _json.dumps(obj, ensure_ascii=False, default=str)

_handler = logging.StreamHandler(); _handler.setFormatter(_JsonFormatter())
_log = logging.getLogger("scheduler"); _log.handlers.clear(); _log.addHandler(_handler)
_log.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO)); _log.propagate = False

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")

def set_trace_id(tid=""): tid = tid or uuid.uuid4().hex[:12]; _trace_id.set(tid); return tid
def get_trace_id(): return _trace_id.get()

def log_event(event, module="", **kwargs):
    tid = get_trace_id()
    obj = {"event": event, "module": module, "ts": int(time.time() * 1000)}
    if tid: obj["trace_id"] = tid
    obj.update(kwargs)
    _log.info(_json.dumps(obj, ensure_ascii=False, default=str))

_file_logger = None
def _get_file_logger():
    global _file_logger
    if _file_logger is None:
        from . import config; config.ensure_dirs()
        d = config.QIDIAN_DIR / "logs"; d.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(d / "scheduler.log"))
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        _file_logger = logging.getLogger("scheduler.file"); _file_logger.addHandler(fh)
        _file_logger.setLevel(logging.DEBUG); _file_logger.propagate = False
    return _file_logger

def get_logger(name=""): return logging.getLogger(name) if name else _log

def timed(func=None, *, name=""):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter(); log_event("fn_start", module=name or f.__name__, fn=f.__name__)
            try:
                result = f(*args, **kwargs)
                log_event("fn_done", module=name or f.__name__, fn=f.__name__, elapsed_ms=int((time.perf_counter()-t0)*1000))
                return result
            except Exception:
                log_event("fn_fail", module=name or f.__name__, fn=f.__name__, elapsed_ms=int((time.perf_counter()-t0)*1000))
                raise
        return wrapper
    return decorator(func) if func else decorator

def info(module, msg):
    _get_file_logger().info("%s | %s", module, msg); logging.getLogger(module).info(msg)
def warn(module, msg):
    _get_file_logger().warning("%s | %s", module, msg); logging.getLogger(module).warning(msg)
def error(module, msg):
    _get_file_logger().error("%s | %s", module, msg); logging.getLogger(module).error(msg)
