"""executors — 执行器包

按 type 分叉 (审计 6.1):
  claude-cli → ClaudeCliExecutor
  zhipu-api  → ZhipuApiExecutor
"""

from .base import BaseExecutor, ExecutorResult
from .claude_cli import ClaudeCliExecutor
from .zhipu_api import ZhipuApiExecutor

__all__ = [
    "BaseExecutor",
    "ExecutorResult",
    "ClaudeCliExecutor",
    "ZhipuApiExecutor",
]
