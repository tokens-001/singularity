"""
observer/executor_proxy.py

安全执行代理（Executor Proxy）。

- 维护一个白名单，仅允许执行白名单内的操作。
- 执行前必须收到用户显式确认（confirm=True）。
- 确认通过后，调用被代理的真实 executor 执行任务。
- 记录每次执行请求、确认与执行结果，便于审计。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = os.path.join(os.path.dirname(__file__), "execution_audit.log")

# 内置默认白名单，表示允许执行的操作名/意图。
# 实际使用时可覆盖。
DEFAULT_WHITELIST: Sequence[str] = (
    "read_file",
    "list_files",
    "search_code",
    "status_check",
    "health_check",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ExecutorProxyError(Exception):
    """安全执行代理通用异常。"""


class NotWhitelistedError(ExecutorProxyError):
    """操作不在白名单中时抛出。"""


class ConfirmationRequiredError(ExecutorProxyError):
    """未收到用户显式确认时抛出。"""


class ExecutorFailureError(ExecutorProxyError):
    """底层 executor 执行失败时抛出。"""


# ---------------------------------------------------------------------------
# 审计记录
# ---------------------------------------------------------------------------

@dataclass
class AuditRecord:
    """单次执行审计记录。"""

    operation: str
    params: Dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False
    success: bool = False
    result: Any = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "operation": self.operation,
            "params": self.params,
            "confirmed": self.confirmed,
            "success": self.success,
            "result": repr(self.result) if self.result is not None else None,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 安全执行代理
# ---------------------------------------------------------------------------

class ExecutorProxy:
    """
    安全执行代理。

    用法：
        executor = ExecutorProxy(real_executor=some_callable)
        result = executor.execute("read_file", {"path": "foo.txt"}, confirm=True)

    参数：
        real_executor: 真实执行器，需为可调用对象，签名 (operation, params) -> result。
        whitelist: 允许执行的操作名列表。为空列表表示禁止所有操作。
        audit_log_path: 审计日志文件路径。
        require_confirmation: 是否要求用户显式确认，默认 True。
    """

    def __init__(
        self,
        real_executor: Callable[[str, Dict[str, Any]], Any],
        whitelist: Optional[Sequence[str]] = None,
        audit_log_path: Optional[str] = None,
        require_confirmation: bool = True,
    ) -> None:
        self._executor = real_executor
        self._whitelist: List[str] = list(whitelist if whitelist is not None else DEFAULT_WHITELIST)
        self._audit_log_path = audit_log_path or DEFAULT_LOG_PATH
        self._require_confirmation = require_confirmation
        self._history: List[AuditRecord] = []

    # ------------------------------------------------------------------
    # 白名单管理
    # ------------------------------------------------------------------

    @property
    def whitelist(self) -> List[str]:
        return list(self._whitelist)

    def add_to_whitelist(self, operation: str) -> None:
        """将操作加入白名单。"""
        if operation not in self._whitelist:
            self._whitelist.append(operation)
            logger.info("Added operation '%s' to whitelist", operation)

    def remove_from_whitelist(self, operation: str) -> None:
        """将操作从白名单移除。"""
        if operation in self._whitelist:
            self._whitelist.remove(operation)
            logger.info("Removed operation '%s' from whitelist", operation)

    def is_whitelisted(self, operation: str) -> bool:
        """检查操作是否在白名单中。"""
        return operation in self._whitelist

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------

    def execute(
        self,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        confirm: bool = False,
    ) -> Any:
        """
        安全执行操作。

        流程：
            1. 检查 operation 是否在白名单中；
            2. 若 require_confirmation 为 True，则必须有 confirm=True；
            3. 调用底层 executor；
            4. 记录审计日志。
        """
        params = params or {}
        record = AuditRecord(operation=operation, params=params, confirmed=confirm)

        # 1. 白名单检查
        if not self.is_whitelisted(operation):
            record.success = False
            record.error = f"Operation '{operation}' is not in the whitelist"
            self._persist(record)
            raise NotWhitelistedError(record.error)

        # 2. 显式确认检查
        if self._require_confirmation and not confirm:
            record.success = False
            record.error = f"Operation '{operation}' requires explicit user confirmation"
            self._persist(record)
            raise ConfirmationRequiredError(record.error)

        # 3. 调用底层 executor
        try:
            result = self._executor(operation, params)
            record.success = True
            record.result = result
            logger.info("Executed operation '%s' successfully", operation)
            return result
        except Exception as exc:  # noqa: BLE001
            record.success = False
            record.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Executor failed for operation '%s'", operation)
            raise ExecutorFailureError(record.error) from exc
        finally:
            # 4. 记录审计日志
            self._persist(record)

    # ------------------------------------------------------------------
    # 审计日志
    # ------------------------------------------------------------------

    def _persist(self, record: AuditRecord) -> None:
        """将审计记录追加到日志文件与内存历史。"""
        self._history.append(record)
        try:
            line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("Failed to write audit log: %s", exc)

    def get_history(self) -> List[AuditRecord]:
        """返回内存中的执行历史。"""
        return list(self._history)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def create_default_proxy(
    real_executor: Callable[[str, Dict[str, Any]], Any],
) -> ExecutorProxy:
    """使用默认白名单创建安全执行代理。"""
    return ExecutorProxy(real_executor=real_executor, whitelist=DEFAULT_WHITELIST)
