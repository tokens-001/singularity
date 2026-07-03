"""MAGMA 多图记忆 — 基础设施见 _memory_core.py。"""

import json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict
from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler._memory_core import *  # noqa: F401,F403

from singularity.scheduler._memory_graph import *  # noqa: F401,F403
from singularity.scheduler._memory_lifecycle import *  # noqa: F401,F403
from singularity.scheduler._memory_experience import *  # noqa: F401,F403
from singularity.scheduler._memory_consolidator import *  # noqa: F401,F403
