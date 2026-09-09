"""Agent 角色 & 注册表 & 角色定义

三个维度:
  1. Agent — 具体模型 + API 绑定 (可新增/切换)
  2. Role  — 工作流中的职能位置 (architect/implementer/...)
  3. Persona — 角色的工作风格与行为边界

静态数据 (PERSONAS, ROLES) 从 TOML 配置文件加载:
  - personas.toml: 人格面具定义
  - roles.toml: 角色定义 + 系统提示词
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

from singularity.scheduler import config as sched_config

# ═══════════════════════════════════════════════════════════
# Persona — 人格面具
# ═══════════════════════════════════════════════════════════

@dataclass
class Persona:
    key: str
    name: str
    description: str
    style_prompt: str
    philosophy: str = ""
    limitations: str = ""
    voice: str = ""


def _load_personas() -> dict[str, Persona]:
    """从 personas.toml 加载人格面具定义。"""
    from ._io import load_toml
    path = sched_config.SCHEDULER_DIR / "personas.toml"
    if not path.exists():
        return {}
    data = load_toml(path)
    result = {}
    for key, d in data.items():
        result[key] = Persona(
            key=key, name=d.get("name", ""),
            description=d.get("description", ""),
            style_prompt=d.get("style_prompt", ""),
            philosophy=d.get("philosophy", ""),
            limitations=d.get("limitations", ""),
            voice=d.get("voice", ""),
        )
    return result


PERSONAS: dict[str, Persona] = {}  # 模块加载时填充


# ═══════════════════════════════════════════════════════════
# Agent — 具体模型 + API 绑定
# ═══════════════════════════════════════════════════════════

@dataclass
class Agent:
    name: str
    level: str
    model: str
    api_type: str
    entry: str = ""
    api_key_env: str = ""
    max_turns: int = 2
    env: dict = field(default_factory=dict)
    env_unset: list[str] = field(default_factory=list)
    default: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Agent":
        d = dict(d)
        d.setdefault("entry", "")
        d.setdefault("api_key_env", "")
        d.setdefault("max_turns", 2)
        d.setdefault("env", {})
        d.setdefault("env_unset", [])
        d.setdefault("default", False)
        return cls(**d)


# ═══════════════════════════════════════════════════════════
# Role — 工作流职能 + 人格绑定
# ═══════════════════════════════════════════════════════════

@dataclass
class Role:
    key: str
    name: str
    description: str
    persona: str = ""
    system_prompt: str = ""
    # 适用阶段（"适用阶段"是角色自己的属性 —— 调研员就是给调研用的）。
    # 空列表 = 不属于任何研发阶段（定义层角色：切换靠对话，不靠 phase）。
    phases: list[str] = field(default_factory=list)
    # 已删字段：level / capabilities / output_schema —— 全仓库无消费方（2026-09-10）

    def get_full_prompt(self) -> str:
        """组合角色提示词 + 人格面具。"""
        base = self.system_prompt
        if self.persona and self.persona in PERSONAS:
            p = PERSONAS[self.persona]
            base = f"[{p.name}] {p.style_prompt}\n\n{base}"
        return base


def _load_roles() -> dict[str, Role]:
    """从 roles.toml 加载角色定义。"""
    from ._io import load_toml
    path = sched_config.SCHEDULER_DIR / "roles.toml"
    if not path.exists():
        return {}
    data = load_toml(path)
    result = {}
    for key, d in data.items():
        result[key] = Role(
            key=key, name=d.get("name", ""),
            description=d.get("description", ""),
            persona=d.get("persona", ""),
            system_prompt=d.get("system_prompt", ""),
            phases=list(d.get("phases", []) or []),
        )
    return result


ROLES: dict[str, Role] = {}  # 模块加载时填充


# ═══════════════════════════════════════════════════════════
# Registry — Agent 注册表
# ═══════════════════════════════════════════════════════════

@dataclass
class RoleAssignment:
    role_key: str
    agents: list[str]
    active: str = ""

    def add_agent(self, agent_name: str):
        if agent_name not in self.agents:
            self.agents.append(agent_name)
        if not self.active:
            self.active = agent_name


_DEFAULT_AGENTS: dict[str, Agent] = {
    "DeepSeek-E": Agent(
        name="DeepSeek-E", level="", model="deepseek-v4-pro",
        api_type="claude-cli",
        entry="/Users/jingzhe/.claude/local/claude --exclude-dynamic-system-prompt-sections -p {prompt}",
        max_turns=2, default=True,
    ),
    "Opus-D": Agent(
        name="Opus-D", level="", model="claude-opus-4-8",
        api_type="claude-cli",
        entry="/opt/homebrew/bin/claude --model claude-opus-4-8 -p {prompt}",
        max_turns=2, default=True,
        env={"ANTHROPIC_API_KEY": "{ANTHROPIC_API_KEY_OPS}",
             "HTTPS_PROXY": "http://127.0.0.1:7892"},
        env_unset=["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"],
    ),
    "GLM-E+": Agent(
        name="GLM-E+", level="", model="glm-5.2",
        api_type="zhipu-api",
        entry="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        api_key_env="ZHIPU_API_KEY",
        max_turns=3, default=True,
    ),
}

_DEFAULT_ASSIGNMENTS: dict[str, RoleAssignment] = {
    "implementer":      RoleAssignment(role_key="implementer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "qa_engineer":      RoleAssignment(role_key="qa_engineer", agents=["gpt-5.5","kimi-k2.7-code","glm-5.2"], active="gpt-5.5"),
    "security_auditor": RoleAssignment(role_key="security_auditor", agents=["gpt-5.5","kimi-k2.7-code","glm-5.2"], active="gpt-5.5"),
}


class AgentRegistry:
    """全局 Agent 注册表。运行时可变，支持新增/切换。"""

    def __init__(self):
        self._agents: dict[str, Agent] = dict(_DEFAULT_AGENTS)
        self._assignments: dict[str, RoleAssignment] = {
            k: RoleAssignment(role_key=v.role_key, agents=list(v.agents), active=v.active)
            for k, v in _DEFAULT_ASSIGNMENTS.items()
        }

    def add_agent(self, agent: Agent) -> Agent:
        self._agents[agent.name] = agent
        return agent

    def remove_agent(self, name: str) -> bool:
        if name in self._agents:
            for ra in self._assignments.values():
                if name in ra.agents:
                    ra.agents.remove(name)
                if ra.active == name:
                    ra.active = ra.agents[0] if ra.agents else ""
            del self._agents[name]
            return True
        return False

registry = AgentRegistry()


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def get_role(key: str) -> Optional[Role]:
    return ROLES.get(key)


# 阶段 → 角色 key。默认 = 现状（不配置就不改变行为）。
# 一个角色可用于多个阶段（implementer 用于执行+修复），所以映射表独立存放。
_DEFAULT_PHASE_ROLES = {
    "researching": "surveyor",
    "planning": "architect",
    "executing": "implementer",
    "fixing": "implementer",
    "reviewing": "reviewer",
}


def get_phase_role(phase) -> str:
    """查某个研发阶段该用哪个角色。未配置 → 用默认。"""
    key = getattr(phase, "value", phase)
    from .config import QIDIAN_DIR
    path = QIDIAN_DIR / "phases.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and key in data:
                return str(data[key] or "")
        except Exception as e:
            logging.getLogger(__name__).warning("phase roles load failed: %s", e)
    return _DEFAULT_PHASE_ROLES.get(key, "")


# ── 模块加载: 填充 PERSONAS 和 ROLES ──
def _apply_overrides() -> None:
    """把 .qidian/roles_custom.json 应用到 ROLES。

    支持三种操作：
    - 改：含 system_prompt —— 以前只认 persona/level，改提示词是**白改**（写进文件但读的时候不看）
    - 新增：key 不在 roles.toml 里也能建
    - 删除：{"deleted": true}
    """
    from .config import QIDIAN_DIR
    path = QIDIAN_DIR / "roles_custom.json"
    if not path.exists():
        return
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.getLogger(__name__).warning("role override load failed: %s", e)
        return
    for key, vals in overrides.items():
        if not isinstance(vals, dict):
            continue
        if vals.get("deleted"):
            ROLES.pop(key, None)
            continue
        r = ROLES.get(key)
        if r is None:                       # 新增
            ROLES[key] = Role(
                key=key, name=vals.get("name") or key,
                description=vals.get("description", ""),
                persona=vals.get("persona", ""),
                system_prompt=vals.get("system_prompt", ""),
                phases=list(vals.get("phases") or []),
            )
            continue
        if vals.get("persona") and vals["persona"] in PERSONAS:
            r.persona = vals["persona"]
        for f in ("name", "description", "system_prompt"):
            if vals.get(f):
                setattr(r, f, vals[f])
        if vals.get("phases") is not None:
            r.phases = list(vals["phases"])


def _init():
    global PERSONAS, ROLES
    PERSONAS = _load_personas()
    ROLES = _load_roles()
    _apply_overrides()

_init()
