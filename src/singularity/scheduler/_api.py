"""API handler facade — 按资源拆分为子模块，此处统一 re-export。

子模块:
  _api_tasks.py   — 任务 CRUD (21 functions)
  _api_projects.py — 项目 CRUD (12 functions)
  _api_admin.py   — agents/models/skills/permissions/MCP/auth/status (37 functions)
  _api_monitor.py — token/perf/reports/templates/health (11 functions)
  _api_memory.py  — memory/conflicts (5 functions)
"""

from singularity.scheduler._api_tasks import *     # noqa: F401,F403
from singularity.scheduler._api_projects import *   # noqa: F401,F403
from singularity.scheduler._api_admin import *      # noqa: F401,F403
from singularity.scheduler._api_monitor import *    # noqa: F401,F403
from singularity.scheduler._api_memory import *     # noqa: F401,F403
