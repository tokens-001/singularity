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
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from singularity.scheduler import config, witness


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
                witness.warn('permission', f'{e}')

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "profiles": [p.to_dict() for p in self._profiles.values()
                        if p.name not in BUILTIN_PROFILES],
            "bindings": self._agent_bindings,
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

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


# ── 工具级审批通道（2026-09-11）─────────────────────────────
#
# 在这之前 `require_approval` 是个**只播报不拦**的半成品：`_dispatch_skills`
# 命中后只往 SSE 推一条"标记为需审批（当前不阻断，仅通知）"，然后照样放行；
# `_api_tasks.task_approval` 也只是推条消息，**没有任何执行器读得到**。
# 全仓没有"工具级审批"的落地通道，所以真拦下去就是死锁 —— 注释当时如实写了这点，
# 现在把通道补上。
#
# 通道复用本仓库既有的**文件信号 + 轮询**范式（`_exec._check_paused` 用 PAUSE_DIR
# 就是这么做的）：执行器写请求 → 轮询等决策 → 取走并清理。
#
# ⚠️ **绝不能死锁**：等待有上限，超时**按拒绝**处理（fail-closed）。
# 上限必须小于 orchestrator 的任务超时阈值（900s），否则任务先被调度循环砍掉，
# 而审批文件留在磁盘上没人清。

# 审批等待上限（秒），可用 QIDIAN_APPROVAL_TIMEOUT 覆盖。
APPROVAL_TIMEOUT_SEC = int(os.environ.get("QIDIAN_APPROVAL_TIMEOUT", "300"))

# 决策值 → 是否放行
_APPROVE = "approve"


def _hold_path(task_id: str) -> Path:
    """一个任务同一时刻只有一条待审请求（顺序执行，不需要队列）。

    读和写都走这里，所以下面那层净化两边一致 —— 不会出现"写在 a、去 b 找"。
    正常来源是 tracker 生成的纯数字 id（HTTP 侧另有 `^\\d{13,20}$` 校验），
    但这是**把外部字符串拼进文件名**的地方，加一道底线不亏。
    """
    safe = str(task_id).replace("/", "").replace("\\", "").replace("..", "")
    return config.HOLD_DIR / f"{safe}.json"


def list_pending_approvals(now: float = None) -> list[dict]:
    """所有**还没有人应答**的审批请求（供界面显示）。

    **顺手收尸**：超过等待上限还没人清的条目，等待方早就已经超时返回了 ——
    正常路径由 `request_approval` 的 finally 删掉，但**进程被 kill**（不是正常
    收回线程）时 finally 不会执行，文件就留在盘上。不处理的话界面会永远挂着一条
    幽灵审批：点它没反应、也不会自己消失。这个不是单测能发现的（单测里线程都正常
    收尾），是真机探测时踩出来的。

    宽限 30 秒是避免和"正在倒计时的那条"抢 —— 它可能刚好到点还没走到 finally。
    """
    out: list[dict] = []
    d = config.HOLD_DIR
    if not d.exists():
        return out
    now = time.time() if now is None else now
    stale_before = now - (APPROVAL_TIMEOUT_SEC + 30)
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue                      # 坏文件跳过，别让整个列表挂掉
        if data.get("decision"):
            continue                      # 已答，等待方马上取走
        req_at = data.get("requested_at") or 0
        # **只在能确凿判定过期时才收**：没有 requested_at 就无从判断，宁可多显示一条
        # 也不能误删活着的请求 —— 删了会让等待方看到"文件消失"而按拒绝处理，
        # 那比界面上挂个幽灵横幅严重得多。
        if req_at and req_at < stale_before:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        out.append(data)
    return out


def decide_approval(task_id: str, decision: str) -> bool:
    """人工决策落盘。返回**是否真的找到**一条待审请求。

    返回 False 很重要：界面据此告诉用户"这条已经不在了"，
    而不是默默显示成功（那会让人以为自己拦住了什么）。
    """
    p = _hold_path(task_id)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("decision"):
        return False                      # 已经答过了
    data["decision"] = _APPROVE if decision == _APPROVE else "reject"
    data["decided_at"] = time.time()
    try:
        from ._io import atomic_write_json
        atomic_write_json(p, data)
    except Exception as e:
        witness.warn("permission", f"decide_approval:{type(e).__name__}:{e}"[:120])
        return False
    return True


def _preview(args) -> str:
    """参数摘要，别把整个文件内容塞进待审列表。"""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s[:200]


def request_approval(task_id: str, level: str, model: str, tool_name: str,
                     args=None, on_event=None, timeout: int = None) -> tuple[bool, str]:
    """工具级审批：写请求 → 轮询等人工决策 → 超时按拒绝。返回 (allowed, reason)。

    **阻塞**（调用方是执行器 worker 线程，和 `_check_paused` 一样占着这个 worker）。
    **任何异常都按拒绝返回** —— 这是个门禁，失败方向必须是"不放行"，
    但原因要说清，否则用户只看到工具被拒却不知道为什么。
    """
    limit = APPROVAL_TIMEOUT_SEC if timeout is None else timeout
    path = _hold_path(task_id)
    try:
        config.ensure_dirs()
        try:
            from ._io import atomic_write_json
            atomic_write_json(path, {
                "task_id": task_id, "level": level, "model": model,
                "tool": tool_name, "args_preview": _preview(args),
                "requested_at": time.time(), "decision": None,
            })
        except Exception as e:
            return False, f"审批请求写盘失败({type(e).__name__}) → 按拒绝处理"
        if on_event:
            try:
                on_event(tool_name, task_id)
            except Exception:
                pass                      # 通知失败不影响审批本身
        deadline = time.time() + max(1, limit)
        while time.time() < deadline:
            time.sleep(1)
            if not path.exists():
                # 被删了（任务删除 / 人工清理）→ 明确拒绝，别傻等到超时
                return False, "审批请求已被移除（任务可能已删除）→ 按拒绝处理"
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue                  # 正在原子写，下一轮再读
            if cur.get("decision"):
                if cur["decision"] == _APPROVE:
                    return True, ""
                return False, f"人工拒绝: {tool_name}"
        return False, f"审批超时（{limit}s 无人应答）→ 按拒绝处理"
    except Exception as e:
        witness.warn("permission", f"approval_channel:{type(e).__name__}:{e}"[:120])
        return False, f"审批通道异常({type(e).__name__}) → 按拒绝处理"
    finally:
        try:
            path.unlink(missing_ok=True)   # 答完/超时都清掉，别留垃圾
        except Exception:
            pass
