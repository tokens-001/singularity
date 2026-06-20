"""Permission 引擎 — Agent 细粒度权限控制。

借鉴 Scream Code 的 Permission 引擎：
  - 许可 profile: 允许的工具、路径、操作
  - 审批策略: 哪些操作需要人工确认
  - 角色绑定: profile → agent

持久化: .qidian/permissions.json
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class PermissionProfile:
    name: str
    description: str = ""
    # 工具白名单 (空=全部允许)
    allowed_tools: list[str] = field(default_factory=list)
    # 路径白名单 (空=全部允许)
    allowed_paths: list[str] = field(default_factory=list)
    # 路径黑名单 (优先级高于白名单)
    blocked_paths: list[str] = field(default_factory=list)
    # 需要审批的操作 (read_file/write_file/run_command/search_code)
    require_approval: list[str] = field(default_factory=list)
    # 运行命令黑名单 (额外检查)
    blocked_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "allowed_paths": self.allowed_paths,
            "blocked_paths": self.blocked_paths,
            "require_approval": self.require_approval,
            "blocked_commands": self.blocked_commands,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PermissionProfile":
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            allowed_tools=d.get("allowed_tools", []),
            allowed_paths=d.get("allowed_paths", []),
            blocked_paths=d.get("blocked_paths", []),
            require_approval=d.get("require_approval", []),
            blocked_commands=d.get("blocked_commands", []),
        )


# ── 内置 profile ──────────────────────────────────────────

FULL_ACCESS = PermissionProfile(
    name="full-access",
    description="无限制：允许所有工具和路径",
)

READ_ONLY = PermissionProfile(
    name="read-only",
    description="只读：仅允许读文件和搜索",
    allowed_tools=["read_file", "search_code"],
    require_approval=["write_file", "run_command"],
)

SANDBOXED = PermissionProfile(
    name="sandboxed",
    description="沙箱：代码操作受限，敏感文件拦截",
    allowed_tools=["read_file", "write_file", "run_command", "search_code"],
    blocked_paths=[".env", ".env.*", "*.token", "*.key", ".qidian/*", ".git/*", "venv/*"],
    require_approval=["run_command"],
    blocked_commands=["rm -rf", "sudo", "chmod 777", "curl", "wget"],
)

BUILTIN_PROFILES = {
    "full-access": FULL_ACCESS,
    "read-only": READ_ONLY,
    "sandboxed": SANDBOXED,
}


# ── Permission Store ──────────────────────────────────────

class PermissionStore:
    def __init__(self):
        self._path = config.QIDIAN_DIR / "permissions.json"
        self._profiles: dict[str, PermissionProfile] = dict(BUILTIN_PROFILES)
        self._agent_bindings: dict[str, str] = {}  # "level/model" → profile_name
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                for d in data.get("profiles", []):
                    p = PermissionProfile.from_dict(d)
                    if p.name not in BUILTIN_PROFILES:
                        self._profiles[p.name] = p
                self._agent_bindings = data.get("bindings", {})
            except Exception as e:
                witness.heartbeat('permission', f'warn:{e}')

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "profiles": [p.to_dict() for p in self._profiles.values()
                        if p.name not in BUILTIN_PROFILES],
            "bindings": self._agent_bindings,
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_profile(self, name: str) -> PermissionProfile | None:
        return self._profiles.get(name)

    def list_profiles(self) -> list[dict]:
        return [{"name": p.name, "description": p.description,
                 "builtin": p.name in BUILTIN_PROFILES,
                 "allowed_tools_count": len(p.allowed_tools),
                 "require_approval": p.require_approval}
                for p in self._profiles.values()]

    def save_profile(self, profile: PermissionProfile) -> None:
        if profile.name in BUILTIN_PROFILES:
            raise ValueError(f"不能覆盖内置 profile: {profile.name}")
        self._profiles[profile.name] = profile
        self._save()

    def delete_profile(self, name: str) -> bool:
        if name in BUILTIN_PROFILES:
            return False
        self._profiles.pop(name, None)
        self._agent_bindings = {k: v for k, v in self._agent_bindings.items() if v != name}
        self._save()
        return True

    def bind_agent(self, level: str, model: str, profile_name: str) -> None:
        if profile_name not in self._profiles:
            raise ValueError(f"Profile 不存在: {profile_name}")
        key = f"{level}/{model}"
        self._agent_bindings[key] = profile_name
        self._save()

    def unbind_agent(self, level: str, model: str) -> None:
        key = f"{level}/{model}"
        self._agent_bindings.pop(key, None)
        self._save()

    def get_agent_profile(self, level: str, model: str) -> PermissionProfile:
        key = f"{level}/{model}"
        name = self._agent_bindings.get(key, "full-access")
        return self._profiles.get(name, FULL_ACCESS)


# ── 单例 ──────────────────────────────────────────────────

_store: PermissionStore | None = None


def get_store() -> PermissionStore:
    global _store
    if _store is None:
        _store = PermissionStore()
    return _store


# ── 执行时权限检查 ──────────────────────────────────────────

def check_tool(level: str, model: str, tool_name: str) -> tuple[bool, str]:
    """检查 agent 是否允许使用某工具。返回 (allowed, reason)。"""
    profile = get_store().get_agent_profile(level, model)
    if profile.allowed_tools and tool_name not in profile.allowed_tools:
        return False, f"工具 {tool_name} 不在允许列表 (profile={profile.name})"
    return True, ""


def check_path(level: str, model: str, path: str, operation: str = "read") -> tuple[bool, str]:
    """检查 agent 是否允许访问某路径。返回 (allowed, reason)。"""
    profile = get_store().get_agent_profile(level, model)
    normalized = path.replace("\\", "/")
    # 黑名单优先
    for pattern in profile.blocked_paths:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(normalized, f"*/{pattern}"):
            return False, f"路径被 profile {profile.name} 拦截: {pattern}"
    # 白名单 (空=全部允许)
    if profile.allowed_paths:
        for pattern in profile.allowed_paths:
            if fnmatch.fnmatch(normalized, pattern):
                return True, ""
        return False, f"路径不在允许列表 (profile={profile.name})"
    return True, ""


def needs_approval(level: str, model: str, tool_name: str) -> bool:
    """检查操作是否需要人工审批。"""
    profile = get_store().get_agent_profile(level, model)
    return tool_name in profile.require_approval


def check_command(level: str, model: str, command: str) -> tuple[bool, str]:
    """检查命令是否被 profile 拦截。"""
    profile = get_store().get_agent_profile(level, model)
    cmd_lower = command.lower().strip()
    for blocked in profile.blocked_commands:
        if blocked.lower() in cmd_lower:
            return False, f"命令被 profile {profile.name} 拦截: {blocked}"
    return True, ""
