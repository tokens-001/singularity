"""观察者智能体 facade — 按功能拆分为5个子模块。

子模块:
  _observer_tools.py      — 只读/写操作工具 (13 functions)
  _observer_definition.py — 定义阶段: skills/角色/上下文/意图检测 (8 functions)
  _observer_answer.py     — 工具调度+配置+_answer_question核心 (5 functions)
  _observer_client.py     — 客户端管理+定义会话+异常检测+submit_question (5 functions)
  _observer_worker.py     — worker线程+start/stop (4 functions)
"""

from singularity.scheduler._observer_tools import *       # noqa: F401,F403
from singularity.scheduler._observer_definition import *   # noqa: F401,F403
from singularity.scheduler._observer_answer import *       # noqa: F401,F403
from singularity.scheduler._observer_client import *       # noqa: F401,F403
from singularity.scheduler._observer_worker import *       # noqa: F401,F403
