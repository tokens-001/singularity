"""project.py — 项目状态机 + 黑板持久化。

ProjectState 是整个工作流的单一真相源。存盘到 .qidian/projects/{id}.json。
重启恢复: load → 读 phase → 从中断点继续。
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from singularity.scheduler import config


class Phase(str, Enum):
    TEMPLATE = "template"
    RESEARCHING = "researching"
    GATE1 = "gate1"          # 用户审调研报告
    PLANNING = "planning"
    GATE2 = "gate2"          # 用户审架构+任务分配
    EXECUTING = "executing"
    INTEGRATING = "integrating"  # D2: 多路worktree合并+集成测试(非用户门)
    REVIEWING = "reviewing"      # 内部: 集成合并通过 → 跑验收(非用户门, 瞬时态)
    GATE3 = "gate3"              # 用户最终交付审核
    DELIVERING = "delivering"    # S1: 打包归档 (GATE3通过后)
    DONE = "done"


# Gate 拒绝 → 回退到哪里
_REJECT_FALLBACK: dict[Phase, Phase] = {
    Phase.GATE1: Phase.TEMPLATE,
    Phase.GATE2: Phase.RESEARCHING,        # 回调研或重写需求
    # GATE3 不在此表: 由 workflow.handle_gate3_reject 按 fix_route 分级路由
    # (impl→EXECUTING / design→PLANNING / note→不回退), 不再一刀切回 PLANNING
}

# 架构级返工: 可从这些阶段直接回 planning
_ARCHITECTURE_REDO = {Phase.EXECUTING, Phase.INTEGRATING, Phase.GATE3, Phase.REVIEWING}

# Gate 确认→下一个 phase
_GATE_NEXT: dict[Phase, Phase] = {
    Phase.GATE1: Phase.PLANNING,
    Phase.GATE2: Phase.EXECUTING,
    Phase.GATE3: Phase.DELIVERING,           # S1: 最终批准→交付打包
}

# D2: 集成合并失败上限 (自动修N轮后升GATE2)
_INTEGRATE_MAX_RETRIES = 2


@dataclass
class ProjectState:
    id: str
    name: str
    template: str = "product_dev"   # 选题模板
    phase: Phase = Phase.TEMPLATE
    auto_mode: bool = False     # 自动流转: 跳过所有 Owner Gate

    # Owner 填写的需求
    description: str = ""
    scope: str = ""
    raw_constraints: list[str] = field(default_factory=list)

    # Gate 确认状态: {gate1: "approved"|"rejected"|"pending", ...}
    owner_confirm: dict = field(default_factory=dict)

    # Artifact 区 (各阶段的产出)
    research_report: dict | None = None          # Researcher 产出
    architecture: dict | None = None             # Architect 产出 {plan, tasks, constraints}
    committee_fusion: dict | None = None         # 架构委员会中间产物 {models, outputs, fused, count}
    constraints_checklist: list[dict] = field(default_factory=list)  # Gate2 确认后的可检查约束 [{type,rule,check}]
    task_ids: list[str] = field(default_factory=list)               # 关联 tracker tasks
    issues: list[dict] = field(default_factory=list)                # Reviewer 问题清单
    supervision_log: list[dict] = field(default_factory=list)       # Supervisor 校验记录
    lineage: list[dict] = field(default_factory=list)               # 血缘日志
    handoffs: list[dict] = field(default_factory=list)              # Agent 交接记录
    token_budget_total: float = 5.0        # $ (默认 $5)
    fix_round: int = 0                      # 内循环修复轮次(上限3)
    review_failures: int = 0                # D1: 审查自动修失败计数 (上限 _REVIEW_MAX_AUTO_FIX)
    integrate_failures: int = 0             # D2: 集成合并失败计数 (上限 _INTEGRATE_MAX_RETRIES)

    # Agent 编组: {"any": ["model_a","model_b"]} — 两档后统一全池, 不设则用全局配置
    agent_lineup: dict[str, list[str]] = field(default_factory=dict)

    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        """字段表**派生于 dataclass**，不是手抄。

        原来这里手写字段清单 —— 加字段忘了补，序列化就少一个键，前端拿到 undefined
        当成"没有"，全程不报错。GATE3 的 qa_report 就是这么丢的（后端花钱生成了报告、
        打回逻辑还读它，界面上一个字都没有）。派生化之后这种漏抄结构上不可能。

        注意：`fields()` 与旧清单曾逐项核对一致（26/26），所以这个改动不改变序列化内容。
        """
        d = {}
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            d[f.name] = v.value if isinstance(v, Enum) else v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectState":
        """反序列化。**对未知键容错** —— 别让多余的一个键把整个项目弄丢。

        原来结尾是裸的 `cls(**d)`：文件里多一个当前代码不认识的键
        （旧版本留下的、或手改的），就抛 TypeError；而 `load()` 捕的就是
        `(json.JSONDecodeError, KeyError, TypeError)`，于是一句"项目不存在"、
        项目从界面上消失，只留一条 load_failed 告警。
        **删任何一个字段，所有存量文件都会立刻变成这种情况**（防御模式 #40）。
        """
        d = dict(d)
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            for k in unknown:
                d.pop(k, None)
            # 落一条告警：丢字段本身是正常的新旧兼容，但"悄悄丢"不是。
            # 存量文件被 save 一次之后这个键就没了，所以告警是暂时的。
            try:
                from singularity.scheduler import witness
                witness.warn("project", f"unknown_fields_dropped:{','.join(unknown)}"[:200])
            except Exception:
                pass
        # phase 值容错：未知值（旧版本写的、已删的枚举、手改的）**不能让整个项目炸掉**。
        # `load()` 捕的是 (JSONDecodeError, KeyError, TypeError) —— ValueError 会直接
        # 穿出去，500 / 崩调用方。删枚举值（比如 FIXING）之前必须先有这一层（防御模式 #40）。
        raw_phase = d.get("phase", "template")
        try:
            d["phase"] = Phase(raw_phase)
        except ValueError:
            try:
                from singularity.scheduler import witness
                witness.warn("project", f"unknown_phase:{raw_phase!r} → 退回 template"[:200])
            except Exception:
                pass
            d["phase"] = Phase.TEMPLATE
        d.setdefault("description", "")
        d.setdefault("scope", "")
        d.setdefault("raw_constraints", [])
        d.setdefault("owner_confirm", {})
        d.setdefault("research_report", None)
        d.setdefault("architecture", None)
        d.setdefault("committee_fusion", None)
        d.setdefault("constraints_checklist", [])
        d.setdefault("task_ids", [])
        d.setdefault("issues", [])
        d.setdefault("supervision_log", [])
        d.setdefault("lineage", [])
        d.setdefault("handoffs", [])
        d.setdefault("auto_mode", False)
        d.setdefault("token_budget_total", 5.0)
        d.setdefault("fix_round", 0)
        d.setdefault("review_failures", 0)
        d.setdefault("integrate_failures", 0)
        d.setdefault("agent_lineup", {})
        d.setdefault("created_at", 0.0)
        d.setdefault("updated_at", 0.0)
        return cls(**d)

    # ── Phase 流转 ──

    def confirm_gate(self, gate: Phase, decision: str) -> Optional[Phase]:
        """Owner 批 Gate。自动推进 phase。返回下一个 phase 或 None。"""
        self.owner_confirm[gate.value] = decision
        self.updated_at = time.time()
        if decision == "approved":
            # 架构校验没过 → **不放行**。返回 None 由调用方如实报错。
            # 拦在"人点通过"这一瞬，而不是自动打回：自动重试会撞上单向棘轮
            # （防御模式 #45），而且这个仓库的定案就是"人来兜底"。
            # 放行的话，不合格的架构会流到执行层，以"拆不出任务、项目无声卡住"爆出来
            # —— 那条路已经踩过（orchestrator 里有 13 分钟一行日志都没有的记录）。
            if gate == Phase.GATE2 and any(i.get("type") == "arch_invalid"
                                           for i in self.issues):
                return None
            next_p = _GATE_NEXT.get(gate)
            if next_p:
                self.set_phase(next_p, f"人工批准 {gate.value}")
            # 人工批 GATE2 = 人到场兜底了, 自动重试的配额必须跟着恢复。
            # review_failures / integrate_failures 是**单向棘轮**(全仓无一处清零):
            # 审查失败触顶 → 集成合并成功后又被 escalate 回 GATE2 → 你再点通过 → 又触顶,
            # 用户看到的就是"点了通过还让我审核", 且永远出不去。
            if gate == Phase.GATE2:
                self.review_failures = 0
                self.integrate_failures = 0
            return next_p
        elif decision == "rejected":
            fallback = _REJECT_FALLBACK.get(gate)
            if fallback:
                self.set_phase(fallback, f"人工打回 {gate.value}")
            return fallback
        return None

    def architecture_redo(self) -> bool:
        """架构级返工: 从 executing/reviewing 回 planning。"""
        if self.phase in _ARCHITECTURE_REDO:
            self.set_phase(Phase.PLANNING, "architecture_redo")
            self.architecture = None
            self.constraints_checklist = []
            self.updated_at = time.time()
            return True
        return False

    def add_lineage(self, entry: dict):
        """追加血缘条目。"""
        entry["ts"] = time.time()
        self.lineage.append(entry)
        # 硬上限 1000 条
        if len(self.lineage) > 1000:
            self.lineage = self.lineage[-1000:]

    def set_phase(self, phase: "Phase", reason: str = "") -> None:
        """**阶段流转的唯一入口** —— 顺带自动留痕。

        以前是 21 处 `proj.phase = X` 散在 4 个文件里，两套驱动各写各的：
        自动那套（orchestrator）会跑 INTEGRATING / DELIVERING，而人手那套
        （`run_phase`）压根不认识这两个阶段，走到就报"未知 phase"。
        出问题时**没有任何轨迹可查** —— 比如"点通过永远弹回 GATE2"那个死锁，
        用户只能看到界面在重复，翻遍项目文件也看不出是谁、第几次把它推回去的。

        不落盘：`add_lineage` 是纯内存追加（上限 1000 条），落盘由调用方在
        合适的时机 `save()`。所以加这一层**不增加任何写盘次数**。
        """
        if phase == self.phase:
            return                      # 没变就不记，免得轨迹被空转刷满
        self.add_lineage({"action": "phase", "from": self.phase.value,
                          "to": phase.value, "reason": str(reason)[:120]})
        self.phase = phase

# ═══════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════

def _projects_dir() -> Path:
    d = config.QIDIAN_DIR / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_project_dir(project_id: str) -> Path:
    """获取项目工作目录 (存定义文档等)。"""
    d = _projects_dir() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_name(name: str) -> str:
    """项目名 → 合法目录名：保留中英文/数字/_-，其余转 -，去首尾 -。"""
    s = re.sub(r"[^\w-]", "-", name.strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "project"


def constraint_text(c) -> str:
    """从约束条目提取可读文本（约束为 dict {rule/text}，兼容历史 str 脏数据）。"""
    if isinstance(c, dict):
        return (c.get("rule") or c.get("text") or "").strip()
    return str(c)


def repo_dir(project_id: str) -> Path:
    """项目代码仓库根：<项目根>/<项目名>/。与奇点仓库隔离、路径直观。"""
    proj = load(project_id)
    if proj and proj.name:
        return get_projects_root() / _sanitize_name(proj.name)
    # 兜底：项目定义已经不在了(被删 / JSON 损坏)。
    # **绝对不能 mkdir** —— 建出来的空目录让 load() 依旧返 None 而目录却"在",
    # 现场就成了"空目录在、JSON 没了", 看起来像项目文件自己消失(2026-09-11 结案的那个)。
    # 需要目录的调用方自己建 (ensure_repo 就自带 mkdir), 这里只负责别撒谎。
    from singularity.scheduler import witness
    witness.warn("project", f"repo_dir_fallback:{project_id}"[:200])
    return _projects_dir() / project_id / "repo"


def _settings_path() -> Path:
    return config.QIDIAN_DIR / "settings.json"


def get_projects_root() -> Path:
    """项目成品根目录：settings.json 用户设置 > 环境变量 > 默认 ~/qidian-projects。"""
    p = _settings_path()
    root = None
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            r = data.get("projects_root", "")
            if r:
                root = Path(r)
        except (json.JSONDecodeError, KeyError):
            pass
    root = root or config.PROJECTS_ROOT
    root.mkdir(parents=True, exist_ok=True)  # 确保根目录存在（目录选择器可浏览）
    return root


def set_projects_root(path: str) -> Path:
    """设置项目成品根目录，持久化到 settings.json。返回规范化后的绝对路径。"""
    root = Path(path).expanduser().resolve()
    p = _settings_path()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            data = {}
    data["projects_root"] = str(root)
    config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def repo_root_for(task) -> Path:
    """task 的代码仓库根：项目任务 → 项目 repo；独立任务 → 奇点仓库。"""
    pid = getattr(task, "project_id", "") or ""
    return repo_dir(pid) if pid else config.PROJECT_ROOT


def ensure_repo(project_id: str) -> Path:
    """确保项目有独立 git 仓库 (git init + main 初始提交)。幂等。"""
    import subprocess
    d = repo_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    if (d / ".git").exists():
        return d
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(d), capture_output=True, text=True)
    # 全局无 git 身份，只在奇点仓库配了本地身份 → 项目 repo 也配一份
    subprocess.run(["git", "config", "user.name", "singularity"], cwd=str(d), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "singularity@local"], cwd=str(d), capture_output=True, text=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init project repo"],
                   cwd=str(d), capture_output=True, text=True)
    return d


def _path(project_id: str) -> Path:
    return _projects_dir() / f"{project_id}.json"


_LOCK = threading.RLock()


def _next_id() -> str:
    with _LOCK:
        base = int(time.time() * 1000)
        max_existing = base
        for p in _projects_dir().glob("*.json"):
            try:
                max_existing = max(max_existing, int(p.stem))
            except ValueError:
                continue
        return str(max(max_existing, base) + 1)


def delete(project_id: str) -> bool:
    """删除项目及所有关联文件。"""
    p = _path(project_id)
    deleted = False
    if p.exists():
        p.unlink()
        deleted = True
    # 删除关联产出文件
    for f in _projects_dir().glob(f"{project_id}.*"):
        try:
            f.unlink(); deleted = True
        except Exception as e:
            # 不能静默：项目记录删了但这些产出文件还在 → 磁盘上留下查不到归属的孤儿，
            # 事后也解释不了"为什么删了项目还占着空间"。
            from singularity.scheduler import witness
            witness.warn("project", f"delete_orphan:{f.name}:{type(e).__name__}"[:200])
    # 项目工作目录也必须删。留着空目录不"无害": repo_dir() 的兜底分支
    # (load 返 None → get_project_dir() mkdir) 会把空目录当活项目接着重建,
    # 于是现场长成"空目录在、JSON 没了" —— 看起来像项目文件自己消失。
    d = _projects_dir() / project_id
    if d.exists():
        # 用 _forcibly_remove_tree 而不是裸 rmtree: agent 产出的目录常带 0555/0444,
        # 裸 rmtree 会 PermissionError 然后**把残骸留在原地** —— 正是本次要修的症状。
        from singularity.scheduler._git_worktree import _forcibly_remove_tree
        _forcibly_remove_tree(d)
        deleted = True
    return deleted


def save(project: ProjectState) -> None:
    project.updated_at = time.time()
    p = _path(project.id)
    # tmp 路径带 pid: _LOCK 是 threading 锁, 只挡得住同进程的线程 —— 而后端调度循环
    # 和 tests/integration/role_probe.py 这类**独立进程**会同时 save 同一个项目。
    # 共用一个确定性 <id>.tmp 时: A replace 成功后 tmp 就没了, B 的 replace 撞 ENOENT
    # (alerts.jsonl 里实见过), 或者两边写入交错 → 项目 JSON 多出一个 `}` → 解析失败
    # → list_all() 静默跳过 → 项目从界面上凭空消失。
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    # 锁仍需要: 同进程内多线程(A 调度循环 / B _merge_executor / C Flask 请求线程)
    # 会共用同一个 pid 的 tmp。tracker.py 同类保存已加锁, 这里当初漏了。
    with _LOCK:
        tmp.write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)  # 原子写
    # SSE 推送项目进度
    _push_project_event(project)


def _push_project_event(proj: "ProjectState") -> None:
    """推送项目状态变更到 SSE (两个通道: pending queue + 直接广播)。"""
    try:
        import json as _json
        phase = proj.phase.value if hasattr(proj.phase, 'value') else str(proj.phase)
        task_count = len(proj.task_ids) if hasattr(proj, 'task_ids') else 0
        payload = _json.dumps({
            "project_id": proj.id, "name": proj.name,
            "phase": phase, "task_count": task_count,
        })
        # Channel 1: pending queue (loop flush)
        from singularity.scheduler._types import _pending_sse_events
        _pending_sse_events.append({"kind": "project", "msg": payload, "ts": time.time()})
        # Channel 2: direct broadcast —— 事件泵只在调度循环启动时跑，
        # 循环没开时（比如刚建完项目还没跑任务）靠这条立刻送达
        from singularity.scheduler import _hooks
        _hooks.emit("project", payload)
    except Exception:
        pass


def load(project_id: str) -> Optional[ProjectState]:
    p = _path(project_id)
    if not p.exists():
        return None
    try:
        return ProjectState.from_dict(
            json.loads(p.read_text(encoding="utf-8"))
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # 原来这里静默 return None —— 调用方只会当成"项目不存在"，
        # 表现就是"项目从界面消失了"，查不出为什么。文件还在，只是读不出来。
        try:
            from singularity.scheduler import witness
            witness.warn("project", f"load_failed:{project_id}:{type(e).__name__}:{e}"[:200])
        except Exception:
            pass
        return None


def create(
    name: str, template: str = "product_dev",
    description: str = "", scope: str = "",
    constraints: list[str] = None,
    budget: float = 5.0,
    auto_mode: bool = False,
) -> ProjectState:
    now = time.time()
    # 重名校验：与已注册项目同名（sanitize 后）或目录已存在 → 拒绝
    key = _sanitize_name(name)
    if any(_sanitize_name(p.name) == key for p in list_all()) or (get_projects_root() / key).exists():
        raise ValueError(f"项目名 '{name}' 已被占用，请换一个")
    proj = ProjectState(
        id=_next_id(), name=name, template=template,
        description=description, scope=scope,
        raw_constraints=list(constraints or []),
        token_budget_total=budget,
        auto_mode=auto_mode,
        phase=Phase.TEMPLATE,
        created_at=now, updated_at=now,
    )
    save(proj)
    return proj


def list_all() -> list[ProjectState]:
    projects = []
    # ponytail: 跳过阶段产出文件 (traceability.json 等)
    _OUTPUT_SUFFIXES = {".traceability.json", ".research.md", ".architecture.md", ".test-plan.md"}
    for p in sorted(_projects_dir().glob("*.json"), reverse=True):
        if any(str(p).endswith(s) for s in _OUTPUT_SUFFIXES):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "phase" not in data:
                continue  # 非项目文件
            projects.append(ProjectState.from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # 不能静默 continue: 磁盘上有个不认的项目文件，界面上就是"凭空少一个"，
            # 用户不会知道是文件坏了还是自己删了。alerts.jsonl 里留一条，能查。
            try:
                from singularity.scheduler import witness
                witness.warn("project", f"unlistable:{p.name}:{type(e).__name__}:{e}"[:200])
            except Exception:
                pass
            continue
    return projects


def recover_all() -> list[ProjectState]:
    """启动时恢复所有非终态项目。"""
    return [p for p in list_all() if p.phase != Phase.DONE]


# ═══════════════════════════════════════════════════════════
# 选题模板
# ═══════════════════════════════════════════════════════════

TEMPLATES = {
    "product_dev": {
        "name": "产品开发",
        "fields": ["项目名称", "项目目标", "功能范围", "涉及模块", "技术约束", "验收标准"],
        "research_domains": ["架构参考", "替代方案", "类似项目"],
    },
    "bug_fix": {
        "name": "Bug修复",
        "fields": ["问题描述", "复现步骤", "影响范围", "期望行为"],
        "research_domains": ["同类问题解法", "根因分析"],
    },
    "refactor": {
        "name": "重构优化",
        "fields": ["重构目标", "现有问题", "不改的接口", "预期收益"],
        "research_domains": ["设计模式参考", "业界实践"],
    },
    "agent_dev": {
        "name": "Agent开发",
        "fields": ["Agent名称", "能力需求", "目标模型", "工具需求", "性能要求", "验收标准"],
        "research_domains": ["Agent框架参考", "工具调用优化", "同类Agent实现"],
    },
}
