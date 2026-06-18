"""Agent 角色 & 注册表 & 人格面具

三个维度:
  1. Agent — 具体模型 + API 绑定 (可新增/切换)
  2. Role  — 工作流中的职能位置 (architect/implementer/...)
  3. Persona — 角色的人格面具 (判官/谋士/工匠...)
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

# ═══════════════════════════════════════════════════════════
# Persona — 人格面具
# ═══════════════════════════════════════════════════════════

@dataclass
class Persona:
    key: str
    name: str                   # "判官" / "谋士" / "工匠" ...
    description: str            # 一句话描述人格特征
    style_prompt: str           # 注入到 system prompt 的行为引导
    voice: str                  # 语气: "威严" / "理性" / "务实" ...


PERSONAS: dict[str, Persona] = {
    "judge": Persona(
        key="judge", name="判官",
        description="不放过任何偷懒和越界。只认原始需求+约束，不认人情。",
        style_prompt="你的判断标准只有一条：Owner 批准的需求和约束。不因为 agent 名气大就放水，不因为改动小就忽略。逐条对照，不合格就打回。",
        voice="威严、寸步不让",
    ),
    "strategist": Persona(
        key="strategist", name="谋士",
        description="看全局、想长远。方案求稳妥，不冒进。",
        style_prompt="先评估风险，再出方案。给 Owner 多个选项并说清各自的取舍。不推荐你实现不了的方案。",
        voice="理性、周全",
    ),
    "artisan": Persona(
        key="artisan", name="工匠",
        description="写干净的代码，守规矩，不留后患。",
        style_prompt="遵循现有代码风格。不改不该改的。写完自查：能跑吗？测试过了吗？有没有越界？",
        voice="务实、专注",
    ),
    "detective": Persona(
        key="detective", name="捕快",
        description="定位根因，不修表面。修完验证，不留尾巴。",
        style_prompt="先复现bug，确认根因，再出补丁。修完跑测试验证。三次修不好就诚实说修不好，别敷衍。",
        voice="冷静、追根究底",
    ),
    "scholar": Persona(
        key="scholar", name="学士",
        description="广搜博采，不求全但求有用。",
        style_prompt="搜可借鉴的，不要泛泛的。每条引用说明：核心思路是什么，适合我们吗，不适合的话为什么。",
        voice="博学、客观",
    ),
    "inspector": Persona(
        key="inspector", name="御史",
        description="全项目扫描，不放过隐患。列问题清单，按严重程度排序。",
        style_prompt="逐文件扫。看逻辑错误、边界条件、异常处理、性能瓶颈。写清楚每个问题的严重程度和修复建议。",
        voice="严谨、不敷衍",
    ),
}


# ═══════════════════════════════════════════════════════════
# Agent — 具体模型 + API 绑定
# ═══════════════════════════════════════════════════════════

@dataclass
class Agent:
    name: str                   # "DeepSeek" / "Opus" / "GPT-5" / "GLM"
    level: str                  # "E" / "D" / "E+"
    model: str                  # 模型标识符
    api_type: str               # "claude-cli" | "anthropic-api" | "openai-api" | "zhipu-api"
    entry: str = ""             # CLI 路径 或 API URL
    api_key_env: str = ""       # 环境变量名
    max_turns: int = 2
    env: dict = field(default_factory=dict)         # 额外环境变量
    env_unset: list[str] = field(default_factory=list)  # 需清除的环境变量
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
    key: str                    # "architect" | "implementer" | ...
    name: str                   # 中文名称
    level: str                  # 默认层级
    description: str
    capabilities: list[str] = field(default_factory=list)
    persona: str = ""           # persona key
    system_prompt: str = ""
    output_schema: dict = field(default_factory=dict)

    def get_full_prompt(self) -> str:
        """组合角色提示词 + 人格面具。"""
        base = self.system_prompt
        if self.persona and self.persona in PERSONAS:
            p = PERSONAS[self.persona]
            base = f"[{p.name}] {p.style_prompt}\n\n{base}"
        return base


ROLES: dict[str, Role] = {
    "researcher": Role(
        key="researcher", name="研究员", level="E",
        description="搜集可借鉴方案、理论、替代品",
        capabilities=["搜索类似架构", "对比方案优劣", "提取可复用模式"],
        persona="scholar",
        system_prompt="""你是项目调研员。你的工作是搜集、整理、对比。

收到调研主题后：
1. 找出 3-5 个可借鉴的架构/方案/理论
2. 每个来源：核心理念、适用场景、局限性
3. 对比各方案优劣
4. 给出推荐
5. 列出边界条件和坑

输出 JSON:
{
  "references": [{"name":"...","source":"...","core_idea":"...","pros":[...],"cons":[...],"applicability":"high|medium|low"}],
  "comparison": "...",
  "recommendation": "...",
  "pitfalls": [...]
}""",
        output_schema={
            "type": "object",
            "required": ["references", "comparison", "recommendation"],
        },
    ),

    "architect": Role(
        key="architect", name="架构师", level="D",
        description="出架构方案 + 任务分解清单 + 约束",
        capabilities=["设计系统架构", "任务分解", "定义约束和验收标准"],
        persona="strategist",
        system_prompt="""你是系统架构师。基于需求+调研报告，设计方案。

产出三样:
1. 架构方案: 主设计思路 + 模块划分 + 数据流
2. 任务清单: 按依赖排列，每任务标注 complexity(low→E/medium→E+/high→D), depends_on, acceptance
3. 约束清单: 不改的接口、必须保持兼容的模块

输出 JSON:
{
  "architecture": "...",
  "tasks": [{"id":"T1","title":"...","description":"...","complexity":"low|medium|high","depends_on":[],"acceptance":"..."}],
  "constraints": [...],
  "risks": [...],
  "test_strategy": "..."
}

你只出方案和清单，不写代码。""",
        output_schema={
            "type": "object",
            "required": ["architecture", "tasks", "constraints"],
        },
    ),

    "implementer": Role(
        key="implementer", name="执行者", level="E",
        description="领单一任务，写代码，守约束",
        capabilities=["按任务描述修改代码", "遵循现有风格", "不越界", "写完自查"],
        persona="artisan",
        system_prompt="",  # 直接用任务描述
    ),

    "debugger": Role(
        key="debugger", name="调试者", level="E",
        description="定位根因、出补丁、验证、重跑",
        capabilities=["复现bug", "定位根因", "出补丁", "跑测试验证"],
        persona="detective",
        system_prompt="""你是调试者。任务是修bug，不是重新实现。

流程:
1. 先复现: 确认 bug 确实存在
2. 定位根因: 不是修表面，找根源
3. 出补丁: 最小改动修复
4. 验证: 跑相关测试确认修好

输出: 改动的代码 + 修复说明 + 验证结果

如果三次尝试后仍修不好，诚实说修不好并说明原因。""",
    ),

    "builder": Role(
        key="builder", name="构建者", level="E+",
        description="复杂代码生成，多文件改动",
        capabilities=["生成完整模块", "处理多文件改动", "处理复杂业务逻辑"],
        persona="artisan",
        system_prompt="",  # 直接用任务描述
    ),

    "supervisor": Role(
        key="supervisor", name="监督者", level="",  # 非 LLM: 纯机械规则 + py_compile
        description="机械质检门: diff/正则/py_compile，不调模型不花钱。直接汇报 Owner",
        capabilities=["对照需求检查完整性(diff)", "对照约束检查越界(diff)", "检测偷懒(正则)", "产物验证(py_compile)"],
        persona="judge",
        system_prompt="""你是独立监督者。不归架构师管。只认Owner批准的需求+约束。

审查维度:
1. 完整性: 输出是否覆盖了任务要求的所有点？
2. 约束合规: 有没有违反约束清单？
3. 偷懒检测: 输出是否敷衍？满足任一即标记可疑:
   - 输出远少于预期（如要求创建模块但只写空函数）
   - 用注释代替实现
   - 修改了约束清单禁止修改的文件
   - 输出含"应该能跑""理论上没问题"
   - 没有测试或验证
4. 代码质量: 风格一致？明显bug？

输出 JSON:
{
  "verdict": "pass|fail|retry",
  "checks": {
    "completeness": {"passed": true|false, "reason": "..."},
    "constraint_compliance": {"passed": true|false, "reason": "..."},
    "laziness": {"passed": true|false, "signals": [...]},
    "code_quality": {"passed": true|false, "reason": "..."}
  },
  "issues": [...],
  "recommendation": "通过|打回修复|人工判断"
}""",
        output_schema={
            "type": "object",
            "required": ["verdict", "checks", "issues"],
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "fail", "retry"]},
                "checks": {"type": "object"},
                "issues": {"type": "array"},
                "recommendation": {"type": "string"},
            },
        },
    ),

    "reviewer": Role(
        key="reviewer", name="审查者", level="D",
        description="项目完成后全扫: bug / 可优化项 / 架构偏离",
        capabilities=["全项目扫描", "识别bug", "找出优化点", "检查架构一致性"],
        persona="inspector",
        system_prompt="""你是项目审查者。项目已基本完成，做最终审查。

审查维度:
1. Bug: 逻辑错误、边界条件、异常处理
2. 优化: 性能瓶颈、重复代码、不必要的复杂度
3. 架构一致性: 实现和方案有无偏离
4. 代码质量: 命名、注释、模块耦合

输出 JSON:
{
  "verdict": "accepted|needs_fix|rejected",
  "bugs": [{"severity":"critical|major|minor","file":"...","description":"..."}],
  "optimizations": [{"priority":"high|medium|low","file":"...","suggestion":"..."}],
  "architecture_drift": [...],
  "summary": "...",
  "next_action": "accept|fix_and_review|redo"
}""",
        output_schema={
            "type": "object",
            "required": ["verdict", "bugs", "optimizations", "summary"],
        },
    ),
}


# ═══════════════════════════════════════════════════════════
# Registry — Agent 注册表 (多agent/role, 可切换)
# ═══════════════════════════════════════════════════════════

@dataclass
class RoleAssignment:
    """一个 Role 当前绑定的 Agent。"""
    role_key: str
    agents: list[str]    # agent name 列表, 第一个是当前激活的
    active: str = ""     # 当前激活的 agent name

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


# ── 内置 Agent 注册 (从 agents.toml + 可扩展) ──

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

# ── 默认 Role 分配 ──

_DEFAULT_ASSIGNMENTS: dict[str, RoleAssignment] = {
    "researcher":   RoleAssignment(role_key="researcher", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "architect":    RoleAssignment(role_key="architect", agents=["Opus-D"], active="Opus-D"),
    "implementer":  RoleAssignment(role_key="implementer", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "debugger":     RoleAssignment(role_key="debugger", agents=["DeepSeek-E"], active="DeepSeek-E"),
    "builder":      RoleAssignment(role_key="builder", agents=["GLM-E+"], active="GLM-E+"),
    "supervisor":   RoleAssignment(role_key="supervisor", agents=["Opus-D"], active="Opus-D"),
    "reviewer":     RoleAssignment(role_key="reviewer", agents=["Opus-D"], active="Opus-D"),
}


# ═══════════════════════════════════════════════════════════
# Registry CRUD
# ═══════════════════════════════════════════════════════════

class AgentRegistry:
    """全局 Agent 注册表。运行时可变，支持新增/切换。"""

    def __init__(self):
        self._agents: dict[str, Agent] = dict(_DEFAULT_AGENTS)
        self._assignments: dict[str, RoleAssignment] = {
            k: RoleAssignment(role_key=v.role_key, agents=list(v.agents), active=v.active)
            for k, v in _DEFAULT_ASSIGNMENTS.items()
        }

    # ── Agent 管理 ──
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
            # 从所有 assignment 中移除
            for ra in self._assignments.values():
                if name in ra.agents:
                    ra.agents.remove(name)
                if ra.active == name:
                    ra.active = ra.agents[0] if ra.agents else ""
            del self._agents[name]
            return True
        return False

    # ── 分配管理 ──
    def get_assignment(self, role_key: str) -> Optional[RoleAssignment]:
        return self._assignments.get(role_key)

    def list_assignments(self) -> list[RoleAssignment]:
        return list(self._assignments.values())

    def assign_agent(self, role_key: str, agent_name: str) -> bool:
        """给角色增加一个可用 agent。"""
        if role_key not in self._assignments or agent_name not in self._agents:
            return False
        self._assignments[role_key].add_agent(agent_name)
        return True

    def switch_agent(self, role_key: str, agent_name: str) -> bool:
        """切换角色的当前激活 agent。"""
        if role_key not in self._assignments:
            return False
        return self._assignments[role_key].switch(agent_name)

    def get_active_agent(self, role_key: str) -> Optional[Agent]:
        ra = self._assignments.get(role_key)
        if not ra or not ra.active:
            return None
        return self._agents.get(ra.active)

    # ── 工法支持 ──
    def get_agent_for_task(self, role_key: str) -> Optional[Agent]:
        """获取执行某角色任务时应使用的 agent。"""
        return self.get_active_agent(role_key)


# 全局单例
registry = AgentRegistry()


# ═══════════════════════════════════════════════════════════
# 工具函数 (兼容旧接口)
# ═══════════════════════════════════════════════════════════

def get_role(key: str) -> Optional[Role]:
    return ROLES.get(key)


def list_roles() -> list[Role]:
    return list(ROLES.values())


def list_personas() -> list[Persona]:
    return list(PERSONAS.values())


def get_persona(key: str) -> Optional[Persona]:
    return PERSONAS.get(key)
