"""executors — 执行器包

按 type 分叉:
  claude-cli     → ClaudeCliExecutor
  zhipu-api      → ZhipuApiExecutor (单轮)
  openai-agent   → OpenAIAgentExecutor (多轮tool-use)
  anthropic-api  → AnthropicApiExecutor (直连 Anthropic Messages API)
"""

from singularity.scheduler.executors.base import BaseExecutor, ExecutorResult
from singularity.scheduler.executors.claude_cli import ClaudeCliExecutor
from singularity.scheduler.executors.zhipu_api import ZhipuApiExecutor
from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
from singularity.scheduler.executors.anthropic_api import AnthropicApiExecutor

__all__ = [
    "BaseExecutor", "ExecutorResult",
    "ClaudeCliExecutor", "ZhipuApiExecutor", "OpenAIAgentExecutor",
    "AnthropicApiExecutor",
]
