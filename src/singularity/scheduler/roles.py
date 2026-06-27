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
    level: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    persona: str = ""
    system_prompt: str = ""
    output_schema: dict = field(default_factory=dict)

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
        caps_raw = d.get("capabilities", [])
        caps = list(caps_raw) if isinstance(caps_raw, list) else []
        result[key] = Role(
            key=key, name=d.get("name", ""),
            level=d.get("level", ""),
            description=d.get("description", ""),
            capabilities=caps,
            persona=d.get("persona", ""),
            system_prompt=d.get("system_prompt", ""),
            output_schema=d.get("output_schema", {}),
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

    def switch(self, agent_name: str) -> bool:
        if agent_name in self.agents:
            self.active = agent_name
            return True
        return False

    def add_agent(self, agent_name: str):
        if agent_name not in self.agents:
            self.agents.append(agent_name)
        if not self.active:
            self.active = agent_name


_DEFAULT_AGENTS: dict[str, Agent] = {
    "DeepSeek-E": Agent(
        name="DeepSeek-E", level="E", model="deepseek-v4-pro",
        api_type="claude-cli",
        entry="/Users/jingzhe/.claude/local/claude --exclude-dynamic-system-prompt-sections -p {prompt}",
        max_turns=2, default=True,
    ),
    "Opus-D": Agent(
        name="Opus-D", level="D", model="claude-opus-4-8",
        api_type="claude-cli",
        entry="/opt/homebrew/bin/claude --model claude-opus-4-8 -p {prompt}",
        max_turns=2, default=True,
        env={"ANTHROPIC_API_KEY": "{ANTHROPIC_API_KEY_OPS}",
             "HTTPS_PROXY": "http://127.0.0.1:7892"},
        env_unset=["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"],
    ),
    "GLM-E+": Agent(
        name="GLM-E+", level="E+", model="glm-5.2",
        api_type="zhipu-api",
        entry="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        api_key_env="ZHIPU_API_KEY",
        max_turns=3, default=True,
    ),
}

_DEFAULT_ASSIGNMENTS: dict[str, RoleAssignment] = {
    # ── 定义层 (Observer) ──
    "researcher":       RoleAssignment(role_key="researcher", agents=["DeepSeek-E"], active="DeepSeek-E"),
    # ── 架构层 (D) ──
    "architect":        RoleAssignment(role_key="architect", agents=["Opus-D"], active="Opus-D"),
    "system_architect": RoleAssignment(role_key="system_architect", agents=["Opus-D"], active="Opus-D"),
    # ── 实现层 (E/E+) ──
    "implementer":      RoleAssignment(role_key="implementer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "debugger":         RoleAssignment(role_key="debugger", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "builder":          RoleAssignment(role_key="builder", agents=["GLM-E+"], active="GLM-E+"),
    "frontend_engineer": RoleAssignment(role_key="frontend_engineer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "backend_engineer":  RoleAssignment(role_key="backend_engineer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "data_engineer":     RoleAssignment(role_key="data_engineer", agents=["GLM-E+"], active="GLM-E+"),
    "devops_engineer":   RoleAssignment(role_key="devops_engineer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    # ── 验收层 (D) ──
    "qa_engineer":      RoleAssignment(role_key="qa_engineer", agents=["Opus-D"], active="Opus-D"),
    "security_auditor": RoleAssignment(role_key="security_auditor", agents=["Opus-D"], active="Opus-D"),
    # ── 监督/审查 ──
    "supervisor":       RoleAssignment(role_key="supervisor", agents=[], active=""),
    "reviewer":         RoleAssignment(role_key="reviewer", agents=["Opus-D"], active="Opus-D"),
}


class AgentRegistry:
    """全局 Agent 注册表。运行时可变，支持新增/切换。"""

    def __init__(self):
        self._agents: dict[str, Agent] = dict(_DEFAULT_AGENTS)
        self._assignments: dict[str, RoleAssignment] = {
            k: RoleAssignment(role_key=v.role_key, agents=list(v.agents), active=v.active)
            for k, v in _DEFAULT_ASSIGNMENTS.items()
        }

    def list_agents(self, level: str = "") -> list[Agent]:
        if level:
            return [a for a in self._agents.values() if a.level == level]
        return list(self._agents.values())

    def get_agent(self, name: str) -> Optional[Agent]:
        return self._agents.get(name)

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

    def get_assignment(self, role_key: str) -> Optional[RoleAssignment]:
        return self._assignments.get(role_key)

    def list_assignments(self) -> list[RoleAssignment]:
        return list(self._assignments.values())

    def assign_agent(self, role_key: str, agent_name: str) -> bool:
        if role_key not in self._assignments or agent_name not in self._agents:
            return False
        self._assignments[role_key].add_agent(agent_name)
        return True

    def switch_agent(self, role_key: str, agent_name: str) -> bool:
        if role_key not in self._assignments:
            return False
        return self._assignments[role_key].switch(agent_name)

    def get_active_agent(self, role_key: str) -> Optional[Agent]:
        ra = self._assignments.get(role_key)
        if not ra or not ra.active:
            return None
        return self._agents.get(ra.active)

    def get_agent_for_task(self, role_key: str) -> Optional[Agent]:
        return self.get_active_agent(role_key)


registry = AgentRegistry()


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def get_role(key: str) -> Optional[Role]:
    return ROLES.get(key)

def list_roles() -> list[Role]:
    return list(ROLES.values())

def list_personas() -> list[Persona]:
    return list(PERSONAS.values())

def get_persona(key: str) -> Optional[Persona]:
    return PERSONAS.get(key)


# ── 模块加载: 填充 PERSONAS 和 ROLES ──
def _init():
    global PERSONAS, ROLES
    PERSONAS = _load_personas()
    ROLES = _load_roles()

_init()
