from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path

from singularity.scheduler import config
from singularity.scheduler._io import atomic_write_json

# 修复 P1-4: tracker 实际被两类线程并发写——后台 loop 线程 (orchestrator) 与
# Flask 请求线程 (_api 的 hold/retry/override/cancel)。transition/cas/ready_tasks
# 都是 read→modify→write 非原子, 裸跑会 lost-update。
# 一把可重入锁串行化所有 read-modify-write 区段, 配合 _write 的 os.replace 原子落盘,
# 即可消除竞态。RLock 允许 maybe_complete_parent→transition 这类同线程重入。
_LOCK = threading.RLock()


class TaskStatus(Enum):
    PENDING = "pending"
    ROUTED = "routed"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    # v3 并行调度新增
    DECOMPOSED = "decomposed"        # 子任务已入队, 等聚合
    BLOCKED = "blocked"              # 依赖未满足, 等前置 DONE
    CONFLICT_HELD = "conflict_held"  # merge 冲突, parking 等人
    PAUSED = "paused"                # 暂停中 (逐步确认 / 手动暂停), 可恢复


# PAUSED: 不进 _INFLIGHT (不是崩了要重跑), 不进 _TERMINAL (能流转回 RUNNING)
# 🔴 **也不进 _SCHEDULABLE**（2026-09-25 挪走，Qoder 外派审出、我逐跳核过）。
#    原来它在这儿，注释写的是"resume 后调度循环能重新捡起"—— **那句话从来没兑现过**：
#    唯一能把它派下去的 CAS 是 PENDING/BLOCKED→ROUTED 和 ROUTED→DISPATCHED，两条都不认
#    PAUSED（`cas` 比对当前状态），所以它只会**永远留在 ready 表里**。
#    而 `_run_queue_v3` 的出口正是 `if not remaining: break` ⇒ **整个调度循环不再回头**
#    （停止位、心跳、对账一起停）。留在表里只有一个后果，没有半点好处。
#    "捡起暂停任务"这件事现在由 `recover()` 在启动时做（PAUSED→PENDING，见那里）。
_INFLIGHT = {TaskStatus.ROUTED, TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.VALIDATING}
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK}


def is_terminal(status: str | TaskStatus) -> bool:
    """这个状态是不是**走到头了**（不会再回调度循环）。

    读侧（时间线 / 统计 / 列表）**一律用这个**，别再手打状态名 —— 2026-09-14 实测：
    `_api_tasks.task_timeline` 自己抄了一份集合，把 `decomposed` / `conflict_held`
    和 `done/failed/rolled_back` 并列 ⇒ 对**没跑完、还会回调度**的任务
    编造出"dispatched → running → 终态"的完整历程。
    这两个状态在这份表里**从来就不是终态**：`decomposed` 等子任务聚合、
    `conflict_held` 等人解决 merge 冲突，之后都要继续走。
    """
    s = status.value if isinstance(status, TaskStatus) else str(status or "")
    return s in {t.value for t in _TERMINAL}

# ready_tasks 扫描的状态: 等待调度的入口态
_SCHEDULABLE = {TaskStatus.PENDING, TaskStatus.ROUTED, TaskStatus.BLOCKED}

# 终态任务的合法出口白名单 (其余改判一律拒绝, 见 transition)
#   DONE: 已完成并 merge → 空集。改判会造出"代码已合入却显示失败", 或转 PENDING
#         触发二次执行+二次合并。唯一合法出口 (GATE3 打回重置) 走 force=True。
#   FAILED / ROLLED_BACK → PENDING: 调度重排队 + 人工重试, 刻意保留。
# 注: PAUSED 不是终态, PAUSED→RUNNING (暂停恢复) 不受影响。
_TERMINAL_EXIT = {
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING}),
    TaskStatus.ROLLED_BACK: frozenset({TaskStatus.PENDING}),
}


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    depends_on: list[str] = field(default_factory=list)
    route_level: str = "any"  # 两档后统一 "any" (E/E+/D 已废弃)
    route_gate: bool = False
    # 「路由未判定」—— 分类**没判出来**（调用挂了 / 回复解析不出），
    # 于是 `route_gate=False` 是**折出来的**，不是分类器的意见（2026-09-20）。
    # ⚠️ **必须是个显式字段**：没有它的话，「没判」和「判了说不用」在盘上
    # 长得一模一样 —— 而这一档是**安全**类的（判据错位审计 C 组）。
    # 见 `router.RouteResult` 的 docstring（那儿还写着"为什么不兜底成 True"）。
    route_gate_unknown: bool = False
    route_type: str = "default"
    # _workflow_phases 建项目子任务时写、_exec 首轮读（Step 4 角色提示词注入）。
    # 这个字段曾经**不存在** —— 写入侧走 transition(**kwargs) 被 hasattr 静默丢弃，
    # 读取侧 getattr(task,'route_role',None) 永远拿到 ""，于是角色提示词从没注入过。
    route_role: str = ""
    snapshot_id: str = ""
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = 0.0
    updated_at: float = 0.0
    starvation_score: float = 0.0  # (now-created_at)/3600*(1+priority), 越大越饿
    children: list[str] = field(default_factory=list)  # 子任务 id 列表 (DAG 分解)
    depth: int = 0  # 分解深度, 防无限递归
    route_locked: bool = False  # planner 已指定层级 → 跳过 re-route (建议 #6)
    held: bool = False           # 人工扣留, 不进调度队列
    held_reason: str = ""        # 扣留原因
    attrs: dict = field(default_factory=dict)  # 扩展属性 (execution_mode/skip_gates 等)
    execution_mode: str = "auto_edit"  # auto_edit | confirm_changes
    project_id: str = ""         # 所属项目 ID (空=独立任务)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        d = dict(d)
        d["status"] = TaskStatus(d.get("status", "pending"))
        # 旧数据兼容: 新字段缺失时用默认值
        d.setdefault("starvation_score", 0.0)
        d.setdefault("children", [])
        d.setdefault("depth", 0)
        d.setdefault("route_locked", False)
        d.setdefault("route_role", "")     # 旧任务文件没这个键，不补会 cls(**d) 报错
        d.setdefault("held", False)
        d.setdefault("held_reason", "")
        d.setdefault("project_id", "")
        d.setdefault("attrs", {})
        d.setdefault("execution_mode", "auto_edit")
        # 🔴 **未知键：丢掉，而不是让 `cls(**d)` 抛**（2026-09-19）。
        #
        # `Task.from_dict` 是**全部**读路径的唯一入口（`read_task` / `ready_tasks` /
        # `recover` / `_load_all_tasks`），它抛一次 = 这个任务**在整个系统里不存在**，
        # 而文件**明明在盘上**。最毒的样子（09-18 独立复现）：一条 RUNNING 任务只要
        # 多一个未知键 ⇒ 读成 None ⇒ `recover()` 永远不碰它 ⇒ 它**永远停在 RUNNING**，
        # 同时任务列表里**根本不存在**。未知键的来源很平常：回滚到旧版本、
        # 手工改过盘、迁移写了一半。
        #
        # ⚠️ **代价如实记**：`to_dict()` 是 `asdict(self)`，丢掉的键在下次整份覆盖写时
        # 就**真的没了**。但那比现在轻得多 —— 现在丢的是**整条任务**。而且这条会说一声。
        extra = [k for k in d if k not in _TASK_FIELDS]
        for k in extra:
            d.pop(k)
        if extra:
            _warn_task_once(
                f"task_unknown_keys:{d.get('id', '')}",
                f"task_unknown_keys:{d.get('id', '?')}:"
                f"{','.join(sorted(extra))}（这些键会在下次写回时丢掉）",
                key="task_unknown_keys")
        return cls(**d)

    def compute_starvation(self) -> float:
        """刷新 starvation_score = (now-created_at)/3600 * (1+priority)。"""
        self.starvation_score = (time.time() - self.created_at) / 3600 * (1 + self.priority)
        return self.starvation_score


# `Task` 的字段名集合 —— `from_dict` 拿它判"哪些键是陌生的"。在类定义之后算一次。
_TASK_FIELDS = frozenset(f.name for f in fields(Task))


def tasks_dir() -> Path:
    d = config.QIDIAN_DIR / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(task_id: str) -> Path:
    return tasks_dir() / f"{task_id}.json"


def _write(task: Task) -> None:
    p = _path(task.id)
    atomic_write_json(p, task.to_dict())


# 「本进程已经报过」的记号 —— 见 `_warn_task_once`。
_TASK_WARNED: set = set()


def _warn_task_once(token: str, msg: str, key: str) -> None:
    """同一个 token 只报一次。

    ⚠️ **读侧去重是必须的，不是优化**：`ready_tasks` 每 2 秒把**全部**任务文件
    扫一遍（`_TASK_SCAN_CACHE`），`_load_all_tasks` 同理。照搬"每读一次报一次"
    ⇒ 一份坏文件**每 2 秒刷一条**告警。同族的账就在清单上：`drain_dep_blocked`
    一天 1850 条，把真事故淹了（09-17「留痕写成每轮一条 = 又一条糊筛子的告警」）。

    `witness.warn` 自己**从不抛**（失败走 logging 第二通道），所以这里不用包 try。
    """
    if token in _TASK_WARNED:
        return
    _TASK_WARNED.add(token)
    from singularity.scheduler import witness  # 函数体内 import：witness → tracker 是现成的环
    witness.warn("tracker", msg[:200], key=key)


def take_cancel_marker(task_id: str) -> str | None:
    """取走这个任务的"停"标记，返回是谁写的：`""`=用户 / `"timeout"`=调度器超时。

    **没有标记 → `None`**（和 `""` 分得开：`None` 是"没事发生"，`""` 是"有一次停，
    只是没写清是谁写的"）。

    ⚠️ **读不出来 → 按用户取消处理**（返回 `""`）。那是最保险的一侧：宁可把一次
    超时记成用户取消，也不要把用户取消咽掉 —— 但**必须出声**，因为"退到旧行为"
    正是这个洞的成因（同 `_exec._check_cancelled` 的取舍）。

    🔵 读法只此一份：原来只有 `_exec._check_cancelled` 会读它，而**标记可能在
    没有活着的 worker 时被写出来**（见 `recover()` 里那段）。同一件事两份读法，
    迟早有一条忘了改。
    """
    p = config.CANCEL_DIR / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        by = body.get("by", "") if isinstance(body, dict) else ""
    except (json.JSONDecodeError, OSError) as e:
        from singularity.scheduler import witness
        witness.warn("tracker", f"cancel_marker_unreadable:{task_id}:{type(e).__name__}"[:180],
                     key="cancel_marker_unreadable")
        by = ""
    try:
        p.unlink()
    except OSError as e:
        # 删不掉 = 这个标记**下一轮还会被读到**（任务会被反复判停）。出声，别静默。
        from singularity.scheduler import witness
        witness.warn("tracker", f"cancel_marker_unlink_failed:{task_id}:{type(e).__name__}"[:180],
                     key="cancel_marker_unlink_failed")
    return by


def _read_task_file(p: Path) -> Task | None:
    """读一个任务文件。**坏了要留痕** —— 四处读路径原来各自 `except: return None`
    / `continue`，**零留痕**（2026-09-19）。

    🔴 **为什么这条不能静默**：`Task.from_dict` 是**全部**读路径的唯一入口，
    它抛一次 = 这个任务**在整个系统里不存在**，而文件**明明在盘上**。
    最毒的样子（09-18 独立复现）：一条 RUNNING 任务只要多一个未知键
    ⇒ 读成 None ⇒ `recover()` **永远不碰它** ⇒ 它**永远停在 RUNNING**，
    同时在任务列表里**根本不存在** —— 探测器和界面都看不见它。
    这跟 §77 那一族同形：**量的是"声明在不在"，不是"实际跑没跑"。**

    走 `_io.load_json_or_quarantine`（09-14 就是为这个形状加的：原样 `.corrupt`
    备份 + log/witness 双通道 + **拒绝当空**）；`Task.from_dict` 自己拒绝
    （类型不对）也照样送去隔离 —— 那正是"未知键"那一种。

    ⚠️ **`is_quarantined` 这道闸门是必须的**：`load_json_or_quarantine` 每次调用
    都会**再备份一份、再报一条**，而上面说的扫频是 2 秒一次。
    （写侧没有任何人拿 `is_quarantined` 挡任务文件 —— 已 grep 核实，
    所以这里复用它只做去重，不会把任务变成"再也写不了"。）
    """
    from singularity.scheduler._io import is_quarantined, load_json_or_quarantine
    # 文件不在了（glob 与删除竞态）→ 就当没有，这不是损坏
    if not p.exists():
        return None
    if is_quarantined(p):
        return None
    d = load_json_or_quarantine(p)
    if d is None:
        return None      # 已经备份 + 双通道上报过了
    try:
        return Task.from_dict(d)
    except (TypeError, ValueError) as e:
        # 内容是真 JSON，但**不是**一个任务（未知键 / 缺必填 / 状态值不认识）。
        # 同样送去隔离：要的是"最近一次现场的原始字节"，不是"看着没事"。
        from singularity.scheduler._io import _quarantine_corrupt
        _quarantine_corrupt(p, f"Task.from_dict 拒绝: {type(e).__name__}: {e}"[:80])
        return None


def read_task(task_id: str) -> Task | None:
    return _read_task_file(_path(task_id))


_NEXT_ID_CACHE = 0

def short_id(tid: str) -> str:
    """给人看的短 id —— **取后 8 位，别取前 8 位**。

    任务号 / 项目号都是**毫秒时间戳**（13 位）。`[:8]` 砍掉的正好是后 5 位毫秒，
    留下的前 8 位**每 100 秒才进一位** ⇒ 同一个项目里、甚至同一天里的 id
    截出来**全长一个样**。

    2026-09-15 真机现场（这条就是这么被抓出来的）：
        越界告警原文「17894825 改了本属 17894825 的文件 fizzbuzz.py」
    —— 真身是 `1789482513688` 和 `…690`，可读起来像"自己改了自己"，
    人审页上等于**没有这条信息**（而它长得像有）。任务列表、桌面通知同理。

    后 8 位随毫秒变，才是真正能区分的那一段。
    """
    s = str(tid)
    return s[-8:] if len(s) > 8 else s


def _next_id() -> str:
    """基于毫秒时间戳, 缓存兜底防碰撞。O(1) 非 O(n) 全表扫描。_LOCK 保护并发。"""
    global _NEXT_ID_CACHE
    with _LOCK:
        base = int(time.time() * 1000)
        # 缓存过期时才扫一次全表 (时间戳进位或首次调用)
        if base >= _NEXT_ID_CACHE:
            max_existing = base
            for p in tasks_dir().glob("*.json"):
                try:
                    max_existing = max(max_existing, int(p.stem))
                except ValueError:
                    continue
            _NEXT_ID_CACHE = max(max_existing, base)
        _NEXT_ID_CACHE += 1
        return str(_NEXT_ID_CACHE)


def create(
    desc: str,
    priority: int = 0,
    depends_on: list[str] = None,
    parent_id: str = "",
    depth: int = 0,
    project_id: str = "",
) -> Task:
    """建任务。设了 parent_id → child 继承 parent.depth+1。校验 depends_on 引用有效性。"""
    now = time.time()
    # 有父任务时, depth 从父继承 (parent.depth + 1), 防无限递归分解
    if parent_id:
        parent = read_task(parent_id)
        if parent is not None:
            depth = parent.depth + 1
    # depends_on 校验: 过滤不存在的 task_id
    valid_deps = []
    for dep_id in (depends_on or []):
        if read_task(dep_id):
            valid_deps.append(dep_id)
    task = Task(
        id=_next_id(),
        description=desc,
        priority=priority,
        depends_on=valid_deps,
        created_at=now,
        updated_at=now,
        depth=depth,
        project_id=project_id,
    )
    _write(task)
    _invalidate_scan_cache()
    return task


def rollback_create(task_ids: list[str], why: str = "") -> int:
    """把**刚建出来、还没挂进项目**的任务撤回去。返回真撤掉的条数。

    为什么需要（2026-09-14）：本仓有**三处**「先 `create`、后登记进 `project.task_ids`」
    的写法 —— `_api_tasks.task_submit` / `_workflow_phases._run_execution` /
    `orchestrator._decompose_and_create_tasks`。中间那步一抛，任务就落在盘上、
    而**项目不认识它**（项目页数不到、orchestrator 也只认 `task_ids`）
    ⇒ **它永远不会被派发**，可从界面上看它就是一条正常的 pending。
    是 §65 那个"状态说有、其实没人管"的同族，只是这次孤立的是任务、不是 RUNNING 标记。

    ⚠️ **撤不干净不许算了**：连删文件都可能失败（权限 / 占用）。那种情况下退化成
    `transition(FAILED)` 并把原因写进 `error` —— 一条**显式的失败**远好过一条
    "看着像待办、实际没人管"的任务（同 `_warn_orphan_running` 的理由：只报不改可以，
    但不能什么都不说）。

    ⚠️ **只给"刚建出来、还没有任何人引用"的任务用** —— 它**不做反引用清理**
    （父任务 `children` / 项目 `task_ids` 那些）。要删一条已经在用的任务，
    走 `_api_tasks` 的删除路径。
    """
    from singularity.scheduler import witness  # 函数体内 import：witness→tracker 是现成的环

    done = 0
    for tid in task_ids or []:
        try:
            _path(tid).unlink(missing_ok=True)
            _invalidate_scan_cache()
            done += 1
        except OSError as e:
            witness.warn("tracker", f"rollback_unlink_failed:{tid}:{type(e).__name__}"[:160],
                         key="rollback_unlink_failed")
            try:
                transition(tid, TaskStatus.FAILED,
                           error=f"建完任务后登记进项目失败，且撤回也没成功: {why}"[:200])
            except Exception as e2:                    # 连兜底都失败，也只能出声了
                witness.warn("tracker", f"rollback_mark_failed:{tid}:{type(e2).__name__}"[:160],
                             key="rollback_mark_failed")
    if done:
        witness.warn("tracker", f"created_task_rolled_back:{done}:{why}"[:160],
                     key="created_task_rolled_back")
    return done


def _apply_attrs(task: Task, kwargs: dict, task_id: str, caller: str) -> None:
    """把 transition/cas 的 kwargs 落到 Task 上；**Task 不认的键必须留痕**。

    以前是 `if hasattr(task, k): setattr(...)` —— 拼错或字段不存在就静默丢弃，
    写入方以为设上了、读取方拿到默认值，两边都"正常"。实际踩过：
    `route_role`（字段根本不存在）被这样丢了一年，角色提示词从来没注入过。
    """
    unknown = [k for k in kwargs if not hasattr(task, k)]
    for k, v in kwargs.items():
        if k not in unknown:
            setattr(task, k, v)
    if unknown:
        from singularity.scheduler import witness
        witness.warn("tracker",
                     f"{caller}_unknown_kwargs:{','.join(unknown)}:task={short_id(task_id)}"[:200])


def transition(task_id: str, new_status: TaskStatus, force: bool = False, **kwargs) -> Task | None:
    """改状态。终态 (DONE/FAILED/ROLLED_BACK) 只允许 _TERMINAL_EXIT 白名单内的流转,
    其余改判拒绝并返回 None (force=True 可绕过, 仅 GATE3 打回这类显式重置用)。"""
    with _LOCK:
        task = read_task(task_id)
        if task is None:
            return None
        old_status = task.status
        if (old_status in _TERMINAL and new_status != old_status and not force
                and new_status not in _TERMINAL_EXIT.get(old_status, frozenset())):
            try:
                from singularity.scheduler.log import warn as _log_warn
                _log_warn("tracker", f"拒绝非法流转 {old_status.value}→{new_status.value} (task={task_id})")
            except Exception:
                pass
            return None
        task.status = new_status
        _apply_attrs(task, kwargs, task_id, "transition")
        task.updated_at = time.time()
        _write(task)
        _invalidate_scan_cache()
        # SSE 推送状态变更
        _push_task_event(task_id, new_status.value if hasattr(new_status, 'value') else str(new_status), task.description)
        return task


def _push_task_event(task_id: str, status: str, desc: str = "") -> None:
    """推送任务状态变更到 SSE 队列。"""
    try:
        import time as _time

        from singularity.scheduler._types import _pending_sse_events
        # 读取完整任务获取 project_id
        pid = ""
        try:
            t = read_task(task_id)
            if t:
                pid = getattr(t, 'project_id', '') or ''
        except Exception:
            pass
        _pending_sse_events.append({
            "kind": "task", "task_id": task_id, "status": status,
            "desc": (desc or "")[:120], "project_id": pid, "ts": _time.time(),
        })
    except Exception:
        pass


def _deps_satisfied(task: Task) -> bool:
    """depends_on 全部 DONE → True。"""
    for dep_id in task.depends_on:
        dep = read_task(dep_id)
        if dep is None or dep.status != TaskStatus.DONE:
            return False
    return True


# 「死路依赖」—— 上游**不会再产出**，下游只能降级起跑。
# 🔴 **`CONFLICT_HELD` 不在这张表里**（2026-09-26，Qoder 第二轮 #7，逐跳核过）。
# 它原来在，而 `is_terminal()` 的 docstring 明写它「**在这份表里从来就不是终态**
# …等人解决 merge 冲突，之后都要继续走」—— 两条判据直接矛盾。
# 而它确实是活的：`_cli_tasks` 的 `resolve(manual)` 把它推到 **DONE**
# （`abort` 那支推 FAILED），`merge._mark_merged` 也会推。⇒ 上游是**卡住**，不是**死了**。
#
# 放在表里的后果不只是"早跑一步"：`_any_dead_dep` 的分支会把
# 「上游依赖 X **已失败**」这句**假话落盘**，人审页上照着它读
# （09-19 专门修过"这句话必须落盘"，见 `ready_tasks` 里那段）。
# ⇒ 上游卡着等人解冲突，下游却以为它已经死了、并立刻起跑 —— 造在没合进来的产物上。
_DEAD_END = {TaskStatus.FAILED, TaskStatus.ROLLED_BACK}


def _any_dead_dep(task: Task) -> str:
    """检查是否有死路依赖 (建议 #7)。返回第一个死路 dep_id 或空串。"""
    for dep_id in task.depends_on:
        dep = read_task(dep_id)
        if dep is not None and dep.status in _DEAD_END:
            return dep_id
    return ""


def _sort_key(t: Task) -> tuple:
    """priority desc 优先; 同 priority 下 starvation_score desc (等最久的优先)。"""
    return (-t.priority, -t.starvation_score)


def cas(
    task_id: str,
    expect_from: TaskStatus,
    to: TaskStatus,
    **kwargs,
) -> bool:
    """compare-and-swap 原子抢占: 状态==expect_from 才转 to。

    文件系统层面的"比较并交换": _LOCK 串行化 _read→判定→_write (修复 P1-4),
    os.replace 原子写保证 crash 不损坏。返回是否抢占成功。
    """
    with _LOCK:
        task = read_task(task_id)
        if task is None or task.status != expect_from:
            return False
        task.status = to
        _apply_attrs(task, kwargs, task_id, "cas")
        task.updated_at = time.time()
        _write(task)
        _invalidate_scan_cache()  # 抢占成功也失效扫描缓存 (否则 2s TTL 窗口内重复调度)
        return True


# ponytail: TTL 缓存减少 glob 全表扫描, 写操作时清空
_TASK_SCAN_CACHE: dict = {"ts": 0, "tasks": []}


def _invalidate_scan_cache():
    _TASK_SCAN_CACHE["ts"] = 0


def ready_tasks(exclude: set[str] = None) -> list[Task]:
    """DAG 就绪判定: 扫 PENDING + ROUTED + BLOCKED, 返回可调度的。

    - depends_on 全 DONE → 就绪; BLOCKED 的转 ROUTED
    - depends_on 有未完成 → 标 BLOCKED, 不返回
    - exclude 里的 task_id 不返回 (防重复调度)
    - 按 (-priority, -starvation_score) 排
    """
    exclude = exclude or set()
    now = time.time()
    # 整段进锁 (修复 P1-4): 每个任务的 读→held判定→ROUTED/BLOCKED 写 必须原子,
    # 否则 Flask 线程的 hold/cancel 会被本扫描的 ROUTED 覆盖 (lost-update)。
    with _LOCK:
        # ponytail: 2s TTL 缓存, 命中时跳过 glob+json.loads
        if now - _TASK_SCAN_CACHE["ts"] < 2 and _TASK_SCAN_CACHE["tasks"]:
            all_tasks = _TASK_SCAN_CACHE["tasks"]
        else:
            all_tasks = []
            for p in tasks_dir().glob("*.json"):
                t = _read_task_file(p)      # 读坏会留痕（见该函数），不再静默 continue
                if t is not None:
                    all_tasks.append(t)
            _TASK_SCAN_CACHE["ts"] = now
            _TASK_SCAN_CACHE["tasks"] = all_tasks
        ready = []
        for task in all_tasks:
            if task.status not in _SCHEDULABLE:
                continue
            if task.id in exclude:
                continue
            if task.held:  # 人工扣留 → 跳过调度
                continue
            dead_dep = _any_dead_dep(task)
            if dead_dep:
                # 不级联失败 → 标记降级，任务继续跑。返工循环会修复。
                # ⚠️ 这句 error **必须落盘**，只改内存等于没写：唯一的读侧
                # `workflow._flag_degraded_tasks` 走的是 `read_task()`（从盘上读），
                # 而本函数只是**建议**可调度 —— 真正派发它的 `_dispatch_ready` 用
                # `cas()`/`transition()`，那两步都是"重读盘上那份再覆盖写"
                # ⇒ 内存里设的 error 当场丢掉。原来只有 `BLOCKED→ROUTED` 那一支写盘，
                # 于是「上游已失败、这是降级起跑的」只对**先 BLOCKED 过**的任务成立；
                # PENDING/ROUTED 起步（上游早就失败 / 返工循环重排回来）的任务，
                # 人审页上永远看不出这个前提（2026-09-19 实测：走完 `ready_tasks()`
                # 再 `read_task()`，error 仍是空串）。
                _mark = f"上游依赖 {dead_dep} 已失败 (降级运行)"
                _was_blocked = task.status == TaskStatus.BLOCKED
                if _was_blocked or str(getattr(task, "error", "") or "") != _mark:
                    task.error = _mark
                    task.updated_at = time.time()
                    if _was_blocked:
                        task.status = TaskStatus.ROUTED
                    _write(task)
                task.compute_starvation()
                ready.append(task)
                continue
            if _deps_satisfied(task):
                # ponytail: 不在此预写 ROUTED — 交给 _dispatch_ready CAS 独占状态变更,
                # 避免 CAS 看到 ROUTED→跳过 PENDING→ROUTED→task 永远 pending
                task.compute_starvation()
                ready.append(task)
            else:
                # 依赖未满足 → 标 BLOCKED (仅 PENDING/ROUTED 转, 已 BLOCKED 不重复写)
                # PAUSED 排除在外: 它是人审暂停态, 改写成 BLOCKED 会让 task_resume
                # 的 "只有暂停中的任务可恢复" 判定失效, 人只能手工删 pause 文件
                if task.status not in (TaskStatus.BLOCKED, TaskStatus.PAUSED):
                    task.status = TaskStatus.BLOCKED
                    task.updated_at = time.time()
                    _write(task)
    ready.sort(key=_sort_key)
    return ready


def set_children(parent_id: str, child_ids: list[str]) -> None:
    """记录 parent 的子任务 id 列表 (DAG 分解后调)。"""
    with _LOCK:
        task = read_task(parent_id)
        if task is None:
            return
        task.children = list(child_ids)
        task.updated_at = time.time()
        _write(task)


def maybe_complete_parent(parent_id: str) -> bool:
    """子任务全 DONE → parent DONE; 任一 FAILED → parent FAILED。

    返回是否触发了 parent 终态转换。
    """
    with _LOCK:
        parent = read_task(parent_id)
        if parent is None or not parent.children:
            return False
        statuses = []
        for cid in parent.children:
            c = read_task(cid)
            if c is None:
                return False  # 子任务文件丢了, 不敢判
            statuses.append(c.status)
        if all(s == TaskStatus.DONE for s in statuses):
            transition(parent_id, TaskStatus.DONE)
            return True
        if any(s == TaskStatus.FAILED for s in statuses):
            transition(parent_id, TaskStatus.FAILED)
            return True
        return False


def recover() -> int:
    count = 0
    with _LOCK:
        for p in tasks_dir().glob("*.json"):
            # ⚠️ 这里**尤其**不能静默：读不出来的若是一条 RUNNING 任务，
            # 它就要靠这个循环被捞回去。读成 None = 它永远是个 RUNNING 幽灵。
            task = _read_task_file(p)
            if task is None:
                continue
            if task.status == TaskStatus.PAUSED:
                # ── 🔴 **重启后的 PAUSED 是孤儿，不捞它就是个死状态**（2026-09-25）──
                # 这个进程刚起来 ⇒ **定义上没有任何 worker 在跑它**。而 PAUSED 唯一的出口
                # 就是"执行它的那个 worker 自己写回 RUNNING"（`_exec._check_paused` 的
                # while 循环出来那一句）—— 没有 worker，那个出口就不存在。
                # 原来这个状态**两头都不占**：`_INFLIGHT` 不收它（不当崩溃重跑），
                # `_SCHEDULABLE` 收它（`ready_tasks` 一直把它算成就绪，而 `_dispatch_ready`
                # 的两个 CAS 都不认 PAUSED）⇒ 派不下去、又不退出 ⇒ `_run_queue_v3` 的
                # `if not remaining: break` 永远到不了（已经挪出 `_SCHEDULABLE`，见那里）。
                # ⇒ **必须把它送回 PENDING**，否则它永远停在暂停里，界面上点恢复也白点
                #   （`task_resume` 只删标记、不改状态 ⇒ 状态还是 PAUSED ⇒ 还是没人能派它）。
                # ⚠️ **不许动 `retry_count`** —— 它**不是失败**，别跟下面 `_INFLIGHT` 那一支
                #   混（那一支涨的是"进程重启回收在飞任务"的次数）。
                # 🔵 **暂停标记一个字不动**：人暂停的意图原样带过去。重派之后
                #   `_check_paused` 在 turn 循环**最开头**（任何模型调用之前）就看到它、
                #   原地再暂停一次 ⇒ "人没点恢复"这件事没有变，也不会白烧 token。
                task.status = TaskStatus.PENDING
                task.updated_at = time.time()
                _write(task)
                count += 1
                continue
            if task.status in _INFLIGHT:
                # ── 🔴 **取消标记优先于"回收重派"**（2026-09-19 夜实测的洞）──
                # 标记本来只由 `_exec._check_cancelled` 在**轮与轮之间**消费，那要求
                # **有一个活着的 worker**。而后端停着的时候照样会写出标记：
                # `task_cancel` 对 RUNNING 的**只写标记**，`project_delete` 正是走它。
                # ⇒ 没人消费，任务顶着 `running` 复活，**重派之后要先花掉第一轮模型
                # 调用才停得住**（当夜现场：删除报 `cancelled: 11`，而盘上 6 个仍是
                # running，最后靠手工补终态）。
                # ⇒ 启动回收是**唯一**保证"进程不在时写下的标记会被兑现"的地方。
                _by = take_cancel_marker(task.id)
                if _by is not None:
                    task.status = TaskStatus.FAILED
                    # 两种来源分开报（同 `_exec._check_cancelled`）：把超时记成
                    # "用户取消"就是**账记在用户头上而他什么都没做**。
                    task.error = ("调度器超时中断（进程重启回收时兑现）" if _by == "timeout"
                                  else "用户手动取消（进程重启回收时兑现）")
                    task.updated_at = time.time()
                    _write(task)
                    count += 1
                    continue
                task.retry_count += 1
                if task.retry_count >= task.max_retries:
                    task.status = TaskStatus.FAILED
                    # ⚠️ **别写"重试 N 次仍崩"** —— 这里数的 `retry_count` 是**进程重启
                    #     回收在飞任务的次数**，模型可能一次都没失败过（它只是被杀掉了）。
                    #     写成"仍崩"会把"我们重启了 N 次"记成"模型崩了 N 次"，
                    #     于是排障的人去查模型，而真因是调度侧（2026-09-19 复核 A9）。
                    #     行为一个字没改：够 `max_retries` 照样转 FAILED。
                    task.error = (f"recover: 进程重启回收 {task.retry_count} 次后仍未完成"
                                  f"（非模型失败）, 转 FAILED")
                else:
                    task.status = TaskStatus.PENDING
                task.updated_at = time.time()
                _write(task)
                count += 1
    return count


# ── DAG 结构分析 (拓扑路由前置) ────────────────────────────────

def dag_metrics() -> dict:
    """计算当前任务 DAG 的结构指标。

    返回:
      {"omega": ω, "delta": δ, "gamma": γ,
       "node_count": n, "edge_count": m,
       "components": c, "topology_hint": "parallel"|"sequential"|"mixed"}
    ω = 最大反链 (并行度上限, Dilworth)
    δ = 关键路径 (最长依赖链, 最小延迟)
    γ = 耦合密度 (|E| / max_possible_edges)
    """
    tasks = _load_all_tasks()
    if len(tasks) < 2:
        return {"omega": 1, "delta": 1, "gamma": 0.0,
                "node_count": len(tasks), "edge_count": 0,
                "components": 1, "topology_hint": "sequential"}

    # 用 task_id 建图，只考虑非终态任务
    active_ids = {t.id for t in tasks
                  if t.status not in _TERMINAL}
    if not active_ids:
        active_ids = {t.id for t in tasks}

    # 邻接表: u → [v] (v depends_on u, 所以 u 要先完成)
    adj: dict[str, list[str]] = {tid: [] for tid in active_ids}
    indeg: dict[str, int] = {tid: 0 for tid in active_ids}
    for t in tasks:
        if t.id not in active_ids:
            continue
        for dep_id in t.depends_on:
            if dep_id in active_ids:
                adj.setdefault(dep_id, []).append(t.id)
                indeg[t.id] = indeg.get(t.id, 0) + 1

    edge_count = sum(len(v) for v in adj.values())
    n = len(active_ids)
    max_edges = n * (n - 1) / 2
    gamma = edge_count / max_edges if max_edges > 0 else 0.0

    # δ: 最长路径 (DP on topological order)
    # 用 Kahn 做拓扑排序同时 DP
    indeg_copy = dict(indeg)
    queue = [tid for tid in active_ids if indeg_copy.get(tid, 0) == 0]
    dist: dict[str, int] = {tid: 1 for tid in active_ids}
    topo_order: list[str] = []

    while queue:
        u = queue.pop(0)
        topo_order.append(u)
        for v in adj.get(u, []):
            dist[v] = max(dist.get(v, 1), dist.get(u, 1) + 1)
            indeg_copy[v] -= 1
            if indeg_copy[v] == 0:
                queue.append(v)

    delta = max(dist.values()) if dist else 1

    # ω: 最大反链 ≈ 最大 BFS level 宽度
    # 用距离作 level，统计每层节点数
    level_counts: dict[int, int] = {}
    for d in dist.values():
        level_counts[d] = level_counts.get(d, 0) + 1
    omega = max(level_counts.values()) if level_counts else 1

    # 连通分量数 (弱连通)
    visited: set[str] = set()
    undirected: dict[str, set[str]] = {tid: set() for tid in active_ids}
    for u in adj:
        for v in adj[u]:
            undirected.setdefault(u, set()).add(v)
            undirected.setdefault(v, set()).add(u)
    components = 0
    for tid in active_ids:
        if tid not in visited:
            components += 1
            stack = [tid]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                for nb in undirected.get(node, []):
                    if nb not in visited:
                        stack.append(nb)

    # 拓扑提示
    if omega >= 3 and gamma < 0.3:
        hint = "parallel"
    elif gamma > 0.6:
        hint = "mixed"
    else:
        hint = "sequential"

    return {
        "omega": omega,
        "delta": delta,
        "gamma": round(gamma, 4),
        "node_count": n,
        "edge_count": edge_count,
        "components": components,
        "topology_hint": hint,
    }


def _load_all_tasks() -> list[Task]:
    """加载所有任务 (供 DAG 分析)。"""
    tasks = []
    for p in tasks_dir().glob("*.json"):
        t = _read_task_file(p)      # 读坏会留痕（见该函数）
        if t is not None:
            tasks.append(t)
    return tasks
