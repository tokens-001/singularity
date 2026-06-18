"""executors — 执行器包

按 type 分叉:
  claude-cli   → ClaudeCliExecutor
  zhipu-api    → ZhipuApiExecutor (单轮)
  openai-agent → OpenAIAgentExecutor (多轮tool-use)
"""

from .base import BaseExecutor, ExecutorResult
from .claude_cli import ClaudeCliExecutor
from .zhipu_api import ZhipuApiExecutor
from .openai_agent import OpenAIAgentExecutor

__all__ = [
    "BaseExecutor", "ExecutorResult",
    "ClaudeCliExecutor", "ZhipuApiExecutor", "OpenAIAgentExecutor",
]
