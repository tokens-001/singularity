"""task_templates.py — 任务模板库

预定义常见任务类型的 prompt 模板：
- bugfix / 重构 / 新功能 / 写测试 / 代码审查
每个模板包含：推荐的 system prompt、建议的 max_turns、推荐模型优先级。
用户提交任务时可选模板，提高成功率。
"""

from dataclasses import dataclass, field
from typing import Optional

# ═══════════════════════════════════════════════
# Template definition
# ═══════════════════════════════════════════════

@dataclass
class TaskTemplate:
    """单个任务模板。"""
    id: str                        # 模板 ID（如 "bugfix"）
    name: str                      # 显示名（如 "Bug 修复"）
    description: str               # 适用的任务描述
    system_prompt: str             # 推荐的 system prompt
    success_criteria: str          # 成功标准
    suggested_max_turns: int = 5   # 建议的最大工具调用轮数
    recommended_models: list[str] = field(default_factory=list)  # 推荐模型优先级
    output_format: str = ""        # 期望的输出格式描述


# ═══════════════════════════════════════════════
# 五类模板
# ═══════════════════════════════════════════════

TEMPLATES: dict[str, TaskTemplate] = {
    "bugfix": TaskTemplate(
        id="bugfix",
        name="Bug 修复",
        description="修复代码中的 bug，包括逻辑错误、运行时异常、边界条件等。",
        system_prompt=(
            "你是 Bug 修复专家。\n"
            "1. 先用 read_file 读取相关源代码，定位问题根因\n"
            "2. 输出根因分析和修复方案\n"
            "3. 用 write_file 写入修复后的代码\n"
            "4. 确保修复不引入新问题"
        ),
        success_criteria=(
            "修复后的代码无语法错误；原有测试仍然通过；"
            "修复方案通用，不引入硬编码或 hack。"
        ),
        suggested_max_turns=5,
        recommended_models=["deepseek-v4-pro", "deepseek-chat", "glm-5-turbo"],
        output_format="diff 格式 或 完整修复后文件内容",
    ),
    "refactor": TaskTemplate(
        id="refactor",
        name="重构优化",
        description="重构代码结构，提高可读性、可维护性或性能，不改变外部行为。",
        system_prompt=(
            "你是代码重构专家。\n"
            "1. 分析现有代码结构和设计问题\n"
            "2. 提出重构方案（保持外部接口不变）\n"
            "3. 输出重构后的代码\n"
            "4. 说明重构理由和收益"
        ),
        success_criteria=(
            "重构后代码对外接口签名不变；所有原有测试通过；"
            "代码复杂度降低或性能提升。"
        ),
        suggested_max_turns=7,
        recommended_models=["deepseek-v4-pro", "kimi-k2.7-code"],
        output_format="重构后完整文件 或 关键差异 + 重构说明",
    ),
    "feature": TaskTemplate(
        id="feature",
        name="新功能开发",
        description="实现新功能，包括新增类/函数/模块、API 接口等。",
        system_prompt=(
            "你是全栈开发工程师。\n"
            "1. 理解需求，确认涉及的文件和接口\n"
            "2. 设计实现方案（不破坏现有接口）\n"
            "3. 用 write_file 实现代码\n"
            "4. 确保代码风格与项目一致"
        ),
        success_criteria=(
            "新功能满足需求描述中的所有要点；"
            "代码风格一致；无遗留调试代码。"
        ),
        suggested_max_turns=8,
        recommended_models=["deepseek-v4-pro", "glm-5.2", "kimi-k2.7-code"],
        output_format="新增/修改的完整代码 + 简要说明",
    ),
    "test": TaskTemplate(
        id="test",
        name="编写测试",
        description="为现有代码编写单元测试、集成测试，提高覆盖率。",
        system_prompt=(
            "你是测试工程师。\n"
            "1. 阅读待测代码，理解接口和边界条件\n"
            "2. 设计测试用例（正常路径 + 异常路径 + 边界值）\n"
            "3. 输出完整可运行的测试文件\n"
            "4. 用现有测试框架和风格"
        ),
        success_criteria=(
            "测试文件可直接运行；覆盖正常/异常/边界三类路径；"
            "测试风格与项目现有测试一致。"
        ),
        suggested_max_turns=5,
        recommended_models=["deepseek-chat", "glm-5-turbo", "deepseek-v4-pro"],
        output_format="完整测试文件代码",
    ),
    "review": TaskTemplate(
        id="review",
        name="代码审查",
        description="审查代码质量，发现潜在 bug、性能问题、安全漏洞、代码异味。",
        system_prompt=(
            "你是资深代码审查专家。\n"
            "1. 读取待审查代码\n"
            "2. 按以下维度审查：正确性、性能、安全性、可读性、可维护性\n"
            "3. 输出结构化审查报告\n"
            "4. 不修改代码，只给建议"
        ),
        success_criteria=(
            "审查覆盖正确性/性能/安全/可读/可维护五维度；"
            "每个问题有具体代码位置和建议修复方案。"
        ),
        suggested_max_turns=3,
        recommended_models=["deepseek-v4-pro", "kimi-k2.7-code", "deepseek-chat"],
        output_format="结构化审查报告（按维度分组，每个问题标位置/严重度/建议）",
    ),
}

# 默认回退模板
DEFAULT_TEMPLATE = TaskTemplate(
    id="default",
    name="通用任务",
    description="未指定类型的通用任务。",
    system_prompt=(
        "你是通用编程助手。\n"
        "1. 理解任务需求\n"
        "2. 制定执行计划\n"
        "3. 逐步完成\n"
        "4. 输出结果"
    ),
    success_criteria="任务需求得到满足，产出可直接使用。",
    suggested_max_turns=5,
    recommended_models=["deepseek-chat", "deepseek-v4-pro"],
)


# ═══════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════

def get(template_id: str) -> TaskTemplate:
    """获取模板，未知 ID 返回默认模板。"""
    return TEMPLATES.get(template_id, DEFAULT_TEMPLATE)


def list_all() -> dict[str, TaskTemplate]:
    """返回所有模板。"""
    return dict(TEMPLATES)


def guess_template(description: str) -> str:
    """从任务描述自动推断模板类型。"""
    desc_lower = description.lower()
    scores: dict[str, int] = {}

    patterns = {
        "bugfix": ["修", "bug", "fix", "错", "崩溃", "异常", "报错", "不对", "没有正确", "不该"],
        "refactor": ["重构", "重写", "改架构", "拆", "解耦", "优化结构", "整理", "清理代码"],
        "feature": ["新增", "添加", "增加", "实现", "开发", "加一个", "支持", "接入"],
        "test": ["测试", "test", "spec", "覆盖", "用例", "assert"],
        "review": ["审查", "review", "检查", "审计", "代码质量", "安全隐患"],
    }

    for tid, keywords in patterns.items():
        scores[tid] = sum(1 for kw in keywords if kw in desc_lower)

    if not scores or max(scores.values()) == 0:
        return "default"

    return max(scores, key=scores.get)


def build_routing_hint(template_id: str) -> dict:
    """根据模板生成路由提示：建议层级、建议模型。"""
    tmpl = get(template_id)
    return {
        "template_id": template_id,
        "suggested_max_turns": tmpl.suggested_max_turns,
        "recommended_models": tmpl.recommended_models,
        "system_prompt": tmpl.system_prompt,
    }
