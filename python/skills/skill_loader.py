"""Skill 引擎 — 加载/解析 SKILL.md 文件，注册为 function calling 工具或 prompt。

借鉴 Scream Code 的 Markdown + YAML frontmatter 格式。

目录:
  python/skills/    ← 系统内置（Git 管理）
  .qidian/skills/   ← 用户自定义（优先级更高，同名覆盖）

Skill 类型:
  tool   → 注册为 OpenAI function calling 工具定义，模型可调用
  prompt → 拼入 system prompt，增强模型上下文
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# ── 路径（独立计算，不依赖 scheduler.config） ────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 奇点/
SYSTEM_SKILLS_DIR = Path(__file__).resolve().parent   # python/skills/
_QIDIAN_DIR = _PROJECT_ROOT / ".qidian"
USER_SKILLS_DIR = _QIDIAN_DIR / "skills"             # .qidian/skills/

# ── 常量 ──────────────────────────────────────────────────────────
SKILL_FILE = "SKILL.md"
_VALID_TYPES = frozenset({"tool", "prompt", "flow"})
_DANGEROUS_COMMANDS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "curl", "wget",
    "chmod 777", "chmod -R",
    "sudo", "su ",
    "mkfs.", "dd if=",
    "> /dev/sda",
]


@dataclass
class SkillDef:
    """一个已加载的 Skill 定义。"""
    name: str
    description: str = ""
    type: str = "tool"          # tool | prompt | flow
    arguments: str = ""         # 空格分隔的参数名，如 "file_path focus"
    source: str = "system"      # system | user
    dir_path: str = ""          # SKILL.md 所在目录路径
    body: str = ""              # Markdown 正文（已剥 frontmatter）
    raw: str = ""               # 原始 SKILL.md 全文

    # ── 运行时生成的 ──
    function_def: Optional[dict] = None   # OpenAI function calling 定义
    errors: list[str] = field(default_factory=list)

    def to_function_def(self) -> dict:
        """生成 OpenAI function calling 工具定义。"""
        if self.type != "tool":
            raise ValueError(f"Skill type={self.type} 不能生成 function calling 定义")
        param_names = [a.strip() for a in self.arguments.split() if a.strip()]
        properties = {}
        required = []
        for p in param_names:
            properties[p] = {"type": "string", "description": f"{p} 参数"}
            required.append(p)
        return {
            "type": "function",
            "function": {
                "name": self.name.replace("-", "_").replace(" ", "_"),
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def expand_body(self, **kwargs) -> str:
        """将 $ARGUMENTS / $arg_name 展开为实际参数值。"""
        body = self.body
        args_str = " ".join(str(v) for v in kwargs.values())
        body = body.replace("$ARGUMENTS", args_str)
        body = body.replace("${ARGUMENTS}", args_str)
        for k, v in kwargs.items():
            body = body.replace(f"${{{k}}}", str(v))
            body = body.replace(f"${k}", str(v))
            body = body.replace(f"${k.upper()}", str(v))
        return body


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 YAML frontmatter。返回 (元数据, 正文)。"""
    # 匹配 ---\n...\n--- 格式
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if not m:
        return {}, content
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, content
    body = content[m.end():]
    return meta, body


def _validate_skill(meta: dict, body: str, source: str, dir_path: str) -> tuple[SkillDef, list[str]]:
    """校验并构建 SkillDef。返回 (skill_def, errors)。"""
    errors = []
    name = meta.get("name", "").strip()
    if not name:
        errors.append("缺少 name 字段")
    skill_type = meta.get("type", "tool").strip()
    if skill_type not in _VALID_TYPES:
        errors.append(f"type 无效: {skill_type}，允许值: {', '.join(sorted(_VALID_TYPES))}")
    if skill_type == "tool" and not body.strip():
        errors.append("type=tool 必须有正文")

    skill = SkillDef(
        name=name,
        description=meta.get("description", "").strip(),
        type=skill_type,
        arguments=meta.get("arguments", "").strip(),
        source=source,
        dir_path=dir_path,
        body=body.strip(),
        raw="",
    )
    return skill, errors


def load_skills(include_flow: bool = False) -> dict[str, SkillDef]:
    """加载所有 Skill（系统 + 用户），用户同名覆盖系统。

    Args:
        include_flow: 是否包含 type=flow 的 skill（暂未支持）

    Returns:
        {skill_name: SkillDef} 字典
    """
    skills: dict[str, SkillDef] = {}

    # 1. 系统内置（先加载，优先级低）
    if SYSTEM_SKILLS_DIR.exists():
        for skill_dir in SYSTEM_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_") or skill_dir.name == "__pycache__":
                continue
            skill_file = skill_dir / SKILL_FILE
            if not skill_file.exists():
                continue
            skill = _load_one(skill_file, source="system")
            if skill and skill.name:
                skill.function_def = _build_function_def(skill)
                skills[skill.name] = skill

    # 2. 用户自定义（后加载，同名覆盖系统）
    if USER_SKILLS_DIR.exists():
        for skill_dir in USER_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_"):
                continue
            skill_file = skill_dir / SKILL_FILE
            if not skill_file.exists():
                continue
            skill = _load_one(skill_file, source="user")
            if skill and skill.name:
                skill.function_def = _build_function_def(skill)
                skills[skill.name] = skill

    # 3. 过滤 type=flow（暂不支持）
    if not include_flow:
        skills = {k: v for k, v in skills.items() if v.type != "flow"}

    return skills


def _load_one(skill_file: Path, source: str) -> Optional[SkillDef]:
    """加载单个 SKILL.md 文件。"""
    try:
        content = skill_file.read_text(encoding="utf-8")
    except Exception:
        return None
    meta, body = _parse_frontmatter(content)
    skill, errors = _validate_skill(meta, body, source, str(skill_file.parent))
    skill.raw = content
    skill.errors = errors
    return skill


def _build_function_def(skill: SkillDef) -> Optional[dict]:
    """为 type=tool 的 skill 生成 function calling 定义。"""
    if skill.type != "tool":
        return None
    try:
        return skill.to_function_def()
    except Exception:
        return None


def get_tool_definitions(skills: dict[str, SkillDef]) -> list[dict]:
    """提取所有 type=tool 的 skill 的 function calling 定义列表。"""
    return [s.function_def for s in skills.values()
            if s.type == "tool" and s.function_def]


def get_prompt_additions(skills: dict[str, SkillDef]) -> str:
    """提取所有 type=prompt 的 skill 正文，拼成 system prompt 追加片段。"""
    prompt_skills = [s for s in skills.values() if s.type == "prompt" and s.body]
    if not prompt_skills:
        return ""
    lines = ["\n## 可用 Skill (prompt 型)"]
    for s in prompt_skills:
        lines.append(f"\n### {s.name}\n{s.description}\n{s.body}")
    return "\n".join(lines)


def list_skills(skills: dict[str, SkillDef] = None) -> list[dict]:
    """列出所有 skill 摘要（供 API 使用）。"""
    if skills is None:
        skills = load_skills()
    result = []
    for name, s in sorted(skills.items()):
        result.append({
            "name": s.name,
            "description": s.description,
            "type": s.type,
            "arguments": s.arguments,
            "source": s.source,
            "errors": s.errors,
        })
    return result


def create_user_skill(name: str, description: str, skill_type: str,
                      arguments: str, body: str) -> SkillDef:
    """创建用户 skill，写入 .qidian/skills/<name>/SKILL.md。"""
    if skill_type not in _VALID_TYPES:
        raise ValueError(f"type 无效: {skill_type}")

    skill_dir = USER_SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # 构建 SKILL.md 内容
    frontmatter_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"type: {skill_type}",
    ]
    if arguments:
        frontmatter_lines.append(f"arguments: {arguments}")
    frontmatter_lines.append("---")
    frontmatter = "\n".join(frontmatter_lines)

    content = frontmatter + "\n\n" + body
    skill_file = skill_dir / SKILL_FILE
    skill_file.write_text(content, encoding="utf-8")

    return _load_one(skill_file, source="user")


def delete_user_skill(name: str) -> bool:
    """删除用户 skill 目录。返回是否成功。"""
    skill_dir = USER_SKILLS_DIR / name
    if not skill_dir.exists():
        return False
    import shutil
    shutil.rmtree(skill_dir)
    return True


def get_agent_skills(agent_level: str, agent_model: str) -> list[str]:
    """从 agents_custom.json 读取 agent 绑定的 skill 列表。"""
    custom_file = _QIDIAN_DIR / "agents_custom.json"
    if not custom_file.exists():
        return []
    try:
        import json
        data = json.loads(custom_file.read_text(encoding="utf-8"))
        agents_custom = data.get("_skills", {})
        level_skills = agents_custom.get(agent_level, {})
        return level_skills.get(agent_model, [])
    except Exception:
        return []


def set_agent_skills(agent_level: str, agent_model: str, skill_names: list[str]) -> None:
    """设置 agent 绑定的 skill 列表，写入 agents_custom.json。"""
    custom_file = _QIDIAN_DIR / "agents_custom.json"
    data = {}
    if custom_file.exists():
        try:
            import json
            data = json.loads(custom_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if "_skills" not in data:
        data["_skills"] = {}
    if agent_level not in data["_skills"]:
        data["_skills"][agent_level] = {}
    data["_skills"][agent_level][agent_model] = list(skill_names)
    custom_file.parent.mkdir(parents=True, exist_ok=True)
    import json
    custom_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
