"""模型熔断 — 某模型连续失败 N 次后冷却一段时间，期间不再被选中。

原 `CircuitBreaker` 埋在 model_profile.py 里（那个模块整体是死的，已删），
这里救出来接线：
  - 选模型前过滤: `dispatcher.pick_agent_fallback_chain`
  - 记成败: `_dispatch_exec` 的 fallback 循环

与 `dispatcher.agent_api_available()` 的分工：那个查**配置**（有没有 key/entry），
这里查**运行时**（刚连挂 3 次就别再试了）。

fail-open：全池都熔断时调用方不过滤 —— 否则一个坏 key 能让整个调度停摆。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from singularity.scheduler import config
from singularity.scheduler._io import atomic_write_json

MAX_FAILURES = 3
COOLDOWN_SECONDS = 300.0


@dataclass
class Breaker:
    model: str
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if self.consecutive_failures >= MAX_FAILURES:
            self.is_open = True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.is_open = False

    def available(self) -> bool:
        if not self.is_open:
            return True
        if time.time() - self.last_failure_time >= COOLDOWN_SECONDS:
            self.is_open = False   # 半开：放行一次，成了就恢复，再挂就重新熔断
            return True
        return False


_lock = threading.Lock()
_breakers: dict[str, Breaker] = {}
_loaded = False


def _path() -> Path:
    return config.QIDIAN_DIR / "model_breakers.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for model, d in raw.items():
        if isinstance(d, dict):
            _breakers[model] = Breaker(
                model=model,
                consecutive_failures=int(d.get("consecutive_failures", 0)),
                last_failure_time=float(d.get("last_failure_time", 0.0)),
                is_open=bool(d.get("is_open", False)),
            )


def _save() -> None:
    try:
        atomic_write_json(_path(), {
            m: {"consecutive_failures": b.consecutive_failures,
                "last_failure_time": b.last_failure_time,
                "is_open": b.is_open}
            for m, b in _breakers.items()
            if b.consecutive_failures or b.is_open   # 健康的模型不落盘
        })
    except OSError:
        pass


def is_available(model: str) -> bool:
    """False = 该模型正在冷却。空 model 视为可用（无法归因时不惩罚）。"""
    if not model:
        return True
    with _lock:
        _load()
        b = _breakers.get(model)
        return b.available() if b else True


def record_failure(model: str) -> None:
    if not model:
        return
    with _lock:
        _load()
        _breakers.setdefault(model, Breaker(model=model)).record_failure()
        _save()


def record_success(model: str) -> None:
    if not model:
        return
    with _lock:
        _load()
        b = _breakers.get(model)
        if b and (b.consecutive_failures or b.is_open):
            b.record_success()
            _save()


def snapshot() -> dict:
    """当前熔断状态（供调试/接口）。"""
    with _lock:
        _load()
        return {
            m: {"open": b.is_open, "failures": b.consecutive_failures,
                "cooldown_remaining": round(max(0.0, COOLDOWN_SECONDS - (time.time() - b.last_failure_time)), 1)
                if b.is_open else 0.0}
            for m, b in _breakers.items()
        }


def _self_check() -> None:
    """最小自检：连挂 3 次熔断 → 冷却期满半开放行 → 成功恢复。"""
    global _loaded, _breakers
    with _lock:
        _breakers = {}
        _loaded = True          # 挡住 _load()，用内存态测
    m = "__self_check_model__"
    assert is_available(m), "健康模型应可用"
    record_failure(m); record_failure(m)
    assert is_available(m), "挂 2 次还没到阈值"
    record_failure(m)
    assert not is_available(m), "挂 3 次应熔断"
    with _lock:
        _breakers[m].last_failure_time = time.time() - COOLDOWN_SECONDS - 1
    assert is_available(m), "冷却期满应半开放行"
    record_success(m)
    assert is_available(m) and snapshot().get(m, {}).get("failures", 0) == 0, "成功后应恢复"
    with _lock:
        _breakers = {}
    print("✅ _model_breaker self-check passed")


if __name__ == "__main__":
    _self_check()
