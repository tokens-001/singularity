"""orchestrator.py — 调度闭环核心 (facade)。

设计契约 (修复 #7): 只有主线程写 tracker。
  - worker 线程 (v3 ThreadPool) 里的 TaskRunner.execute() 只做纯执行 (dispatch + validate),
    返回 BatchOutput, 不调任何 tracker.transition/cas/create。
  - 主线程的 _run_queue_v3 负责所有 tracker 写入。

架构 #1.1: 任务生命周期已抽到 _task_runner.TaskRunner。
  orchestrator 只管队列调度 (dispatch → reap → drain 三步循环)。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

# ── 队列调度所需 (精简后) ──────────────────────────────────
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler._exec import _save_trace
from singularity.scheduler._worktree import _release_ref, cleanup_task_artifacts
from singularity.scheduler._planner import _maybe_complete_parents
from singularity.scheduler._task_runner import TaskRunner, _archive_task_outcome

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler import router as router_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import witness
from singularity.scheduler import pre_search as pre_mod
from singularity.scheduler.tracker import TaskStatus

try:
    from .merge import MergeQueue, MergeRequest
except ImportError:
    MergeQueue = None  # type: ignore
    MergeRequest = None  # type: ignore


# ── F1: 集成合并异步化 — 解除调度循环阻塞 ──
# 集成合并含 pytest/docker subprocess (最长 ~150s), 不能在调度循环线程同步跑,
# 否则单项目合并期间全局任务派发/SSE 停摆。用独立线程池异步执行, 完成后回写 phase。
_merge_executor = None  # 惰性重建: 进程重启/shutdown 后 submit 会报 cannot schedule new futures
_merge_inflight: set[str] = set()  # 正在跑集成合并的 project_id, 防重入


def _get_merge_executor() -> ThreadPoolExecutor:
    """惰性获取合并线程池，进程重启/shutdown 后自动重建。"""
    global _merge_executor
    if _merge_executor is None or getattr(_merge_executor, "_shutdown", False):
        _merge_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="integrate")
    return _merge_executor


def _submit_integration_merge(proj, agents: dict) -> None:
    """把集成合并丢进后台池。**"占位"和"提交"要么一起成、要么一起不成。**

    ⚠️ 原来两处调用点都写成 `_merge_inflight.add(proj.id)` 紧接 `submit(...)`：
    而 `submit` 是会抛的（executor 已 shutdown ⇒ `cannot schedule new futures`，
    注释就写在 `_merge_executor` 上面）。一抛，那条 id **永远不会被清** ——
    清理写在后台函数 `_run_integration_merge_async` 的 `finally` 里，而那个函数
    **根本没起来**。此后 `proj.id not in _merge_inflight` 恒为假 ⇒
    **这个项目再也不会被合并**，无声卡在 integrating。
    外层那个 `except` 只记一条 `auto_trigger:{e}`，**不回滚这个集合**。

    ⇒ 所以：登记和提交收进一个函数，失败就把占位**撤回**并出声（不是静默 pass ——
    "这个项目再也合不了"正是最该看见的那类事故）。
    **不往上抛**：这一处失败不该连累同一轮里其它项目的推进。
    """
    _merge_inflight.add(proj.id)
    try:
        _get_merge_executor().submit(_run_integration_merge_async, proj.id, agents)
    except Exception as e:
        _merge_inflight.discard(proj.id)
        witness.warn("orch", f"merge_submit_failed:{type(e).__name__}:{e}"[:160],
                     key="merge_submit_failed")


def run_queue(agents: dict, max_concurrent: int = 1) -> list[tuple]:
    """统一的调度循环入口。v3 支持 1..N 并发。"""
    return _run_queue_v3(agents, max_concurrent)


def schedule_policy(tasks: list) -> list:
    """拓扑自适应调度策略: 综合多信号排序就绪任务。

    信号权重:
      - starvation_score (1.0): 防饥饿, 等越久越优先
      - priority (0.5): 用户指定优先级
      - dependency_weight (0.5): 阻塞越多子任务越优先 (关键路径)
    两档后不再有 level_bonus (E/E+/D 已废弃, 统一 "any")。
    """
    def _score(t) -> float:
        dep_weight = len(t.children) if hasattr(t, 'children') else 0
        return (
            1.0 * t.starvation_score +
            0.5 * t.priority +
            0.5 * dep_weight
        )
    return sorted(tasks, key=_score, reverse=True)


def _dispatch_ready(dispatched: set, pool, agents, runner: TaskRunner,
                    running_futures: dict, mq) -> bool:
    """_run_queue_v3 步骤①②③: 选就绪→cas抢占→提交线程池。返回是否有新派发。"""
    ready = tracker.ready_tasks(exclude=dispatched)
    ready = schedule_policy(ready)
    dispatched_any = False
    for t in ready:
        if t.route_locked:
            # 两档后 RouteResult 不再带 level; route_level 仅作 trace 标签存于 task
            route = router_mod.RouteResult(
                gate_required=t.route_gate,
                task_type=t.route_type)
        else:
            route = router_mod.route(t.description)
        # 同上：重试过的任务才展开历史实际产出。
        pre = pre_mod.pre_search(t.description, route, deep=t.retry_count > 0)
        pre_mod.apply_escalation(route, pre)
        # PENDING/BLOCKED → ROUTED (若尚未路由)
        if t.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
            if not tracker.cas(t.id, t.status, TaskStatus.ROUTED,
                               route_level=t.route_level, route_gate=route.gate_required,
                               route_type=route.task_type):
                continue  # CAS 失败，下一轮重试
            t.status = TaskStatus.ROUTED
        if tracker.cas(t.id, TaskStatus.ROUTED, TaskStatus.DISPATCHED,
                       route_level=t.route_level, route_gate=route.gate_required,
                       route_type=route.task_type):
            from singularity.scheduler.project import repo_root_for
            snap = snap_mod.take(t.id, repo_root=repo_root_for(t))
            tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
            # ⚠️ **从这一行往下，任务已经是 RUNNING 了。** 后面任何一步抛，都会留下一个
            # **没人管的 RUNNING 任务**：没有 future ⇒ 循环看不见它 ⇒
            # `ready_tasks()` 也不返回它（它是 RUNNING 不是 PENDING）⇒
            # **900s 收割永远够不着**，它就那么挂着。
            # 2026-09-13 真机实测就是这么凭空少了一个任务（py-spy 栈：池子里没有工作线程、
            # 循环空转到 `time.sleep(3)`）。所以这一段必须兜住 ——
            # **抛了要把它转成 FAILED，不许留在 RUNNING**。
            # ⚠️ **死线必须在 submit 之前定**（2026-09-14 核外派「改动审阅」）。
            # `runner.execute` 原本是在 worker 线程**开头**才起表 —— 而池子满时
            # 任务会在队列里等，于是两把尺差了一个**排队时间**。并发默认 1、单个任务
            # 可跑 810s ⇒ 排队几分钟是常态，`W > 收尾余量(90s)` 时执行器算出的
            # "该收尾了"**晚于**外面那把 900s 的刀 ⇒ 自收尾照样赶不上收割
            # （就是 §67 那个病，换了个更常见的触发条件）。
            # 从 submit 起算，排队时间会被 `_dispatch_budget_s` 每次 dispatch 自动扣掉。
            deadline_at = time.time() + config.TASK_DEADLINE_S
            try:
                fut = pool.submit(runner.execute, t, agents, mq, deadline_at)
            except Exception as _e:
                # 走共用的兜底（§65）。**这里原来是自己手写一段**，跟 `_strand_guard`
                # 只差一样东西：**改之前不重读盘上的状态**。当前路径上那段是对的
                # （同线程、刚置完 RUNNING、submit 紧跟着抛 ⇒ 它确实还停在 RUNNING），
                # 但**第二个写入者存在时就不成立** —— 而我们有
                # `tests/integration/role_probe.py` 那个独立进程（见 `project.py` 的 `save()`）。
                # 2026-09-13 核外派答卷时发现：`_strand_guard` 的 docstring 早写着
                # "本文里同一形状有 5 处…这个是共用的兜底"，**而真机抓到的那一处恰恰没走它** ——
                # 声称跑在行为前面。`where="dispatch"` 让告警 key 仍是 `dispatch_failed`
                # （不劈开已有的常驻分组），`detail` 保住原来那句人话。
                _strand_guard(t, _e, "dispatch", detail="派发失败：future 没登记上")
                continue
            running_futures[fut] = (t, route, snap, pre, time.time())
            dispatched.add(t.id)
            dispatched_any = True
    return dispatched_any


def _salvage_timed_out(task, elapsed_s: float, snap=None):
    """超时被杀时把**已知事实**留下来：改了哪些文件、有没有提交、实际跑了多久。

    原来这里给 `_save_trace` 传 `None` → trace 里 `changed_files=[]` /
    `elapsed=0` / `tokens=0`，看上去"这个任务什么都没干"。而 worktree 里
    文件其实是写全的（2026-09-11 探路轮实测：3 个任务各撞 900s 被杀，
    事后**完全查不出它们做过什么**）。

    ⚠️ **2026-09-12 更正：只跑 `git status --porcelain` 是不够的 —— 那只看得到
    「未提交」的改动。而 agent 干完一轮会自己 `commit_wt`（"agent changes in
    <taskid>"），提交之后 `git status` 是干净的** → 于是"干完了并提交了"和
    "什么都没干"在 trace 里长得一模一样。
    探路2 的 T2 就是这样：373 行测试 + 完整实现都提交了（worktree 里 `163a52a`），
    trace 里只剩一个 `__pycache__`、`agent_output` 23 字。
    修法：跟**执行前快照的 ref** 比 —— 和 `validator._diff_base` 同一招，
    它的注释早就写着"裸 git diff 恒为空"（09-11 修了 validator，这里没同步）。

    用量（token）：2026-09-12 起**读得回来了** —— 执行器每 dispatch 一次就把累计值
    落一盘（`_exec._persist_partial_usage`），这里读回。**它是下界**（超时那一刻
    正在飞的那次调用没算进去），一次都没落盘才留 None。**None = 不知道，不是没花钱。**
    """
    # 用量先读 —— **必须放在下面的 try 外面**：那段要跑 git、可能抛，
    # 抛了就落进 except 分支，账又跟着丢了（这正是本次要修的东西，别再绕回去）。
    # 执行器每 dispatch 一次落一盘（`_exec._persist_partial_usage`），这里读回来。
    # ⚠️ 它是**下界**：超时那一刻正在飞的那次模型调用不在里面。
    # 一条都没落（比如第一轮就超时）才如实留 None —— None 是"不知道"，不是"没花钱"。
    from singularity.scheduler._exec import (
        read_partial_usage, read_partial_tool_events, read_partial_started_at)
    _partial_tokens, _partial_model = read_partial_usage(task.id)
    # **进没进过 dispatch** —— 它决定下面那句"用量未知"该说哪一种（§59 的边界）。
    _started_at = read_partial_started_at(task.id)
    # tool_events 也一样：它平时只在内存/SSE 里过一遍，超时路径拿不到
    # ⇒ trace 里 `tool_batches.turns` 恒 0，"一次多动作"那套度量在超时任务上没法算。
    _partial_events = read_partial_tool_events(task.id)

    try:
        import subprocess
        from singularity.scheduler.project import repo_root_for
        from singularity.scheduler._git_worktree import _worktrees_dir
        try:
            from singularity.scheduler.validator import _diff_base
            base = _diff_base(snap)
        except Exception:
            base = ""

        repo_root = repo_root_for(task)
        files: list[str] = []
        commits: list[str] = []

        def _git(wt, *args) -> str:
            try:
                return subprocess.run(["git", *args], cwd=str(wt), capture_output=True,
                                      text=True, timeout=10).stdout or ""
            except Exception:
                return ""

        for wt in _worktrees_dir(repo_root).glob(f"{task.id}_*"):
            if base:
                # 与基准比：**已提交 + 未提交一起**看得见
                files += [f.strip() for f in
                          _git(wt, "diff", "--name-only", base).split("\n") if f.strip()]
                commits += [c.strip() for c in
                            _git(wt, "log", "--oneline", f"{base}..HEAD").split("\n")
                            if c.strip()]
            else:
                # 拿不到基准（copy 型快照 / 没传 snap）→ 退回旧行为，但**如实标注**
                for ln in _git(wt, "status", "--porcelain").split("\n"):
                    if ln.strip():
                        files.append(ln[3:].strip())

        files = sorted(set(files))
        tail = (f"，另有 {len(commits)} 个提交未合并（{commits[0].split()[0]}）"
                if commits else "")
        if not base:
            tail += "；⚠️ 拿不到执行前基准，这里**只**能看到未提交的改动"

        class _TimedOutResult:
            """只填**能确定的**字段；token 未知就 None（不是 0）。"""
            changed_files = files
            new_commits = commits
            raw_output = (f"(执行超时(>{int(elapsed_s)}s) 被杀，未及输出总结。"
                          f"磁盘上改动了 {len(files)} 个文件{tail}"
                          + (f"；已知花费 {_partial_tokens} token（下界，最后那次调用未计）"
                             if _partial_tokens
                             # ⚠️ **两种"没账"要说清楚是哪一种**（§59 的边界）：
                             # 进过 dispatch = 调用了、只是没落账；没进过 = 真没发起过。
                             # 以前一律说"一次都没落盘"，等于把这两件事混起来报。
                             else ("；用量未知 —— **dispatch 已经开始了**，是没落账，不是没调用"
                                   if _started_at
                                   else "；用量未知 —— **一次 dispatch 都没进去过**")) + ")")
            token_count = _partial_tokens or None
            elapsed = float(elapsed_s)  # 这个是真的：确实跑了这么久
            success = False
            error = "timeout"
            error_kind = "timeout"
            patch_path = ""
            # 边跑边落的那些事件 —— 补上之后超时任务的 tool_batches 才有数
            tool_events = _partial_events

        class _TimedOutDisp:
            executor_result = _TimedOutResult()
            # 带上模型名：记账要按模型单价算钱，空串会进 unpriced_models。
            agent_cfg = {"model": _partial_model}

        return _TimedOutDisp()
    except Exception as e:
        try:
            from singularity.scheduler import witness
            witness.warn("orchestrator", f"salvage_timeout:{type(e).__name__}:{e}"[:120])
        except Exception:
            pass
        # git 那段砸了**不等于账也不用记** —— 用量是先读的，跟 git 没关系。
        # 原来这里直接 return None：调用方连 token 都拿不到，`_save_trace` 还会
        # 拿到 None 写出一份"这个任务什么都没干"的假 trace。两头都错。
        _tok_txt = (f"；已知花费 {_partial_tokens} token（下界）" if _partial_tokens
                    else "；用量未知（一次都没落盘）")

        class _FallbackResult:
            changed_files: list = []
            new_commits: list = []
            raw_output = (f"(执行超时(>{int(elapsed_s)}s) 被杀；改动抢救失败"
                          f"({type(e).__name__}){_tok_txt})")
            token_count = _partial_tokens or None
            elapsed = float(elapsed_s)
            success = False
            error = "timeout"
            error_kind = "timeout"
            patch_path = ""
            tool_events = _partial_events

        class _FallbackDisp:
            executor_result = _FallbackResult()
            agent_cfg = {"model": _partial_model}

        return _FallbackDisp()


def _account_salvaged(t, salvaged, elapsed_s: float) -> None:
    """异常收尾的**记账 + 记忆**：两条非正常路共用（超时收割 / worker 异常）。

    为什么必须单独有这个函数：`_archive_task_outcome` 是唯一记账入口，
    但它在 `finalize` / `_drain_pending` 那条正常路上 —— **超时和 worker 异常
    都走不到**（直接判失败、不进 pending）⇒ 花掉的钱一条都不落、经验也进不了记忆。
    2026-09-12 先给超时那条补上；2026-09-13 数出 worker 异常那条是同一个形状，
    抽出来共用，免得补一处漏一处。

    每件各自 try：一件炸不该连累另一件（跟 `_archive_task_outcome` 同规矩）。
    """
    # 记账 —— §55 要求异常路径留"烧了多少 token"。
    try:
        _tk = int(getattr(salvaged.executor_result, "token_count", 0) or 0)
        if _tk > 0:
            from singularity.scheduler._token_budget import record_tokens
            _pname = ""
            _pid = getattr(t, "project_id", "") or ""
            if _pid:
                try:
                    from singularity.scheduler import project as _proj_mod
                    _pname = getattr(_proj_mod.load(_pid), "name", "") or ""
                except Exception:
                    pass        # 拿不到名字不该影响记账
            record_tokens(
                project_id=_pid, project_name=_pname, task_id=t.id,
                model=getattr(salvaged, "agent_cfg", {}).get("model", ""),
                level=getattr(t, "route_level", "any"), tokens=_tk,
                elapsed_s=elapsed_s,
            )
    except Exception as _e:
        try:
            witness.warn("orch", f"salvage_record_tokens:{type(_e).__name__}"[:120])
        except Exception:
            pass
    # 记忆 —— 同理，`index_task` 走的是 `_exec.py` 那条正常收尾路径。
    # 后果：**干完了却异常结束的任务，经验永远进不了记忆**
    # （探路2 的 T2/T3 实测：373 行测试 + 计数核都写了，events.json 里轨迹是 0 字）。
    try:
        from singularity.scheduler import memory as _mem
        _er = getattr(salvaged, "executor_result", None)
        _mem.index_task(
            task_id=t.id,
            description=t.description,
            changed_files=list(getattr(_er, "changed_files", []) or []),
            depends_on=getattr(t, "depends_on", []) or [],
            created_at=getattr(t, "created_at", None),
            trajectory=str(getattr(_er, "raw_output", "") or ""),
            force=True,   # 异常条目要留下，别被去重吃掉
        )
    except Exception as _e:
        try:
            witness.warn("orch", f"salvage_index_task:{type(_e).__name__}"[:120])
        except Exception:
            pass
    # 释放 worktree 引用（留不下会影响下一次派发）
    try:
        from singularity.scheduler.project import repo_root_for
        _release_ref(t.id, repo_root=repo_root_for(t))
    except Exception:
        pass


def _flag_killed_without_wrapup(tid: str) -> None:
    """任务被外层 900s 砍掉时出声 —— **"被砍"不等于"自己收尾"**。

    执行器自带提前量（`TASK_DEADLINE_S − TASK_WRAPUP_MARGIN_S`，见 config），
    设计上它**该在外层这刀之前**带着"已知事实"回来（`error_kind="deadline"`，
    那条走 `deadline_wrapup` 分支、**压根到不了收割这里**）。
    ⇒ **所以这条告警就是"那条提前收尾的修复这轮没生效"的信号** ——
    没有它的话，两种情况在盘上长得一模一样（都是 FAILED + 一份没及总结的 trace），
    **只能靠猜**。2026-09-13 真机：任务 `1789303900782` 就是这么被砍的
    （零改动 / 5 轮 6 次调用 / `agent_output` 里没有"主动收尾"），当时**一条告警都没有**。

    ⚠️ 带 key ⇒ 进 `alert_summary` 聚合；**常驻就说明"每次都是被砍的"**，那才是真信号。
    """
    try:
        witness.warn("orch", f"task_killed_no_wrapup:{tid}"[:120],
                     key="task_killed_no_wrapup")
    except Exception:
        pass


def _strand_guard(t, exc: BaseException, where: str, detail: str = "") -> None:
    """兜住"future/batch **已经消费掉**、后续那步却抛了"—— 别把任务留在 RUNNING 没人管。

    §65 那条形状：**先改状态、后做事，中间断了就出孤儿**；而孤儿的表现是"看起来在跑"
    （没有 future ⇒ 循环看不见它 ⇒ `ready_tasks()` 也不返回它 ⇒ 900s 收割够不着）。
    本文里同一形状有 5 处（派发那次是 2026-09-13 真机抓到的），这个是共用的兜底。

    **只在它还停在 RUNNING 时才改** —— `finalize` 可能已经把它推到 DONE/FAILED 了，
    那种情况不能覆盖（会丢掉真实终态）。
    ⚠️ 为什么这里"自动改状态"是对的，而 `reconcile_projects` 那条规矩说自动纠正危险：
    那条说的是**猜**（"状态和磁盘对不上，谁对？"）；这里不猜 —— **没有 future 就是
    没在跑**，是确定的。留着 RUNNING 才是谎报。

    `where` **同时决定告警 key**（`{where}_failed`，粒度要跨调用点稳定，别随手改 ——
    `alert_summary` 按 key 归并，改了就等于把已有的常驻分组劈成两条）。
    `detail` 是给人看的措辞，不给就用 `{where} 失败`。

    ⚠️ **2026-09-13 的账**：这段 docstring 上面那句"同一形状有 5 处…共用的兜底"
    **曾经是假的** —— 真机抓到的那处（`_dispatch_ready`）自己手写了一段、没走这里，
    而它**恰恰缺的就是下面那个"只在还停在 RUNNING 时才改"的检查**。
    已改成真走这里。**教训：`_strand_guard` 这种"统一兜底"的说法，要能一口气数出
    调用点才算数**（当时只有 4 个）。
    """
    try:
        fresh = tracker.read_task(t.id)
        if fresh is not None and fresh.status == TaskStatus.RUNNING:
            tracker.transition(t.id, TaskStatus.FAILED,
                               error=f"{detail or where + ' 失败'}（任务没在跑）: "
                                     f"{type(exc).__name__}: {exc}"[:200])
    except Exception:
        pass
    try:
        witness.warn("orch", f"{where}:{type(exc).__name__}: {exc}"[:200],
                     key=f"{where}_failed")
    except Exception:
        pass


def _reap_futures(running_futures: dict, pending_batches: dict,
                  mq, runner: TaskRunner, results: list) -> bool:
    """_run_queue_v3 步骤④: 回收已完成 future → finalize 或入 pending。返回是否有回收。"""
    if not running_futures:
        return False
    now = time.time()
    # per-future 超时阈值 (15min, 单模型写代码需多轮 读→写→测→改)。
    # 数在 config 里 —— executor 用同一个数减余量提前收尾（2026-09-13: 以前这里
    # 硬编码 900，执行器不知道有这回事，于是永远"跑到被砍、砍完无账"）。
    deadline = config.TASK_DEADLINE_S
    reaped = False

    # 如果有 future 但全都未完成, 先等第一个完成 (最多 10s)
    if not any(f.done() for f in running_futures):
        wait(running_futures.keys(), timeout=10, return_when=FIRST_COMPLETED)

    # 收割所有已完成的
    for fut in list(running_futures.keys()):
        if not fut.done():
            continue
        t, route, snap, pre, submitted_at = running_futures.pop(fut)
        reaped = True
        try:
            batch, t_route, t_snap = fut.result()
        except Exception as e:
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"worker 异常: {e}")
            except Exception as _e2:
                # ⚠️ **安全网自己的失败也要出声**（2026-09-13 外派分类抓到）：
                # 这一句转不成，任务就**留在 RUNNING 成孤儿** —— 而它正是"别留孤儿"
                # 这条防护本身。防护失败还静默，等于防护不存在。
                witness.warn("orch", f"worker_error_transition_failed:{t.id}:"
                                     f"{type(_e2).__name__}:{_e2}"[:180],
                             key="worker_error_transition_failed")
            results.append((t.id, f"worker_error: {e}", None))
            # ⚠️ 原来这里传的是 `None, None` —— **worker 干了什么都查不出来**。
            # 而它跟超时那条是**同一个形状**：`_archive_task_outcome` 挂在正常收尾路上，
            # 这条走不到 ⇒ 账不落、经验不进记忆。盘上其实**早就有** sidecar
            # （`_persist_partial_usage` 每 dispatch 一次落一盘），只是没人读。
            # 2026-09-13 跟超时并成一条路（见 `_account_salvaged`）。
            _elapsed = time.time() - submitted_at
            # ⚠️ **要和下面超时那条一个形状**（2026-09-13 外派 E 抓到，2026-09-14 补）：
            # 抢救段原来**裸着**—— 里面任何一步抛都会**中断整个 reap 循环**，
            # 后面那些同样已经完成的 future 这一轮就不再处理（白等一轮）。
            # 超时那条早就包了 `_strand_guard`，两条路做的是**同一件事**，只包了一条。
            # （任务在上面的 `tracker.transition(FAILED)` 已经落终态，所以这里抛
            # 不会出孤儿；`_strand_guard` 的意义是**出声** + 那个"只在还停在 RUNNING
            # 时才改"的兜底。）
            try:
                _salvaged = _salvage_timed_out(t, _elapsed, snap)
                _save_trace(t, route, snap, _salvaged, None, False)
                _account_salvaged(t, _salvaged, _elapsed)
            except Exception as _e:
                _strand_guard(t, _e, "salvage_worker_error")
            try:
                from singularity.scheduler.project import repo_root_for
                cleanup_task_artifacts(t.id, repo_root_for(t))
            except Exception:
                pass
            continue
        try:  # 性能记录: execute 耗时 (validate/merge 在 finalize 内部, 不计)
            from singularity.scheduler._profiler import record_perf
            # now/submitted_at 都是 time.time() 秒 → 字段叫 _ms, 必须 ×1000
            record_perf(t.id, t.route_type or "", 0.0, (now - submitted_at) * 1000, 0.0, 0.0)
        except Exception:
            pass
        # ⚠️ future 在上面已经 pop 掉了 —— 这两条路**任何一步抛都出孤儿**（§65）。
        # 而且抛出去会**中断整个 reap 循环**，后面那些已完成的 future 这轮就不收了。
        if batch.merge_request:
            try:
                mq.submit(batch.merge_request)
            except Exception as _e:
                _strand_guard(t, _e, "enqueue_merge")
                continue
            pending_batches[t.id] = (t, t_route, t_snap, batch)
        else:
            try:
                runner.finalize(t, batch, t_route, t_snap, results)
            except Exception as _e:
                _strand_guard(t, _e, "finalize")

    # 超时检测
    for fut in list(running_futures.keys()):
        t, route, snap, pre, submitted_at = running_futures.get(fut, (None,)*5)
        if t is not None and now - submitted_at > deadline:
            running_futures.pop(fut)
            # ⚠️ **走到这儿 = 执行器没能自己收尾** —— 必须出声。
            # 执行器自带提前量（`TASK_DEADLINE_S − TASK_WRAPUP_MARGIN_S`，见 config），
            # 设计上它**该在外层这刀之前**带着"已知事实"回来（`error_kind="deadline"`，
            # 那条会走 `deadline_wrapup` 分支、压根到不了这里）。
            # ⇒ **能用这条告警**把"那条提前收尾的修复有没有真生效"和"只是这轮碰巧慢"分开 ——
            # 不然两种情况的盘上产物长得一样（都是 FAILED + 一份没及总结的 trace）。
            # 2026-09-13 真机：任务 `1789303900782` 就是这么被砍的（零改动、5 轮 6 次调用、
            # 没有"主动收尾"），**而当时没有任何告警**。
            _flag_killed_without_wrapup(t.id)
            try:
                # 协作式中断: 写取消标记, 让执行线程在下一 turn 边界自行退出
                config.ensure_dirs()
                (config.CANCEL_DIR / f"{t.id}.json").write_text("{}", encoding="utf-8")
            except Exception:
                pass
            try:
                fut.cancel()
            except Exception:
                pass
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"执行超时(>{deadline}s)")
            except Exception as _e2:
                # ⚠️ 同族：转不成 ⇒ **超时任务留在 RUNNING 成孤儿**，而且收割已经
                # 把它 pop 掉了、下一轮也够不着。**收割者自己失败必须出声。**
                witness.warn("orch", f"timeout_transition_failed:{t.id}:"
                                     f"{type(_e2).__name__}:{_e2}"[:180],
                             key="timeout_transition_failed")
            results.append((t.id, "timeout", None))
            # 抢救已知事实再落 trace —— 传 None 会让 trace 变成一份"什么都没干"的假象。
            # 包 try：任务已经转 FAILED 了（上面），这里抛**不会出孤儿**，但会**中断整个
            # reap 循环** —— 后面那些同样超时的 future 这轮就不再处理，白等一轮。
            try:
                _salvaged = _salvage_timed_out(t, now - submitted_at, snap)
                _save_trace(t, route, snap, _salvaged, None, False)
                _account_salvaged(t, _salvaged, now - submitted_at)
            except Exception as _e:
                _strand_guard(t, _e, "salvage_timeout")
            reaped = True

    return reaped


def _record_ledger(proj, extra: dict) -> None:
    """把这次交付的结局追加进流程账本（P6）。**失败也要记** —— 见调用点注释。"""
    try:
        from singularity.scheduler import _process_ledger
        _process_ledger.record(proj, extra)
    except Exception as e:
        try:
            from singularity.scheduler import witness
            witness.warn("ledger", f"record:{type(e).__name__}:{e}"[:120])
        except Exception:
            pass


def reconcile_projects() -> list[dict]:
    """周期对账：把「状态说的」和「磁盘上真有的」比一遍，漂移就报出来。

    分析里 P4 的"检出时延收尾"：**状态漂移要能早发现**，别等项目卡死了才回头查。
    比三样：

      ① `project.task_ids` 里有多少任务**磁盘上真的还在**
      ② 项目 `phase` 和它 `lineage` 里最后一条 phase 流转**对不对得上**
         （流转断了 = 有地方绕过 `set_phase` 直接改了状态）
      ③ **预算**：花了多少 vs 预算多少

    ⚠️ **只报不改。** 自动"纠正"状态比不报更危险 —— 它会把真的问题抹平成假的
    一致，而这正是本项目反复踩的那类坑（静默兜底 = 编造）。
    """
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import tracker as tracker_mod
    drifts: list[dict] = []
    try:
        projects = proj_mod.recover_all()
    except Exception as e:
        return [{"project_id": "-", "kind": "reconcile_error",
                 "detail": f"recover_all 失败: {type(e).__name__}: {e}"[:100]}]

    for proj in projects or []:
        pid = getattr(proj, "id", "") or "-"
        try:
            ids = list(getattr(proj, "task_ids", []) or [])
            missing = [tid for tid in ids if tracker_mod.read_task(tid) is None]

            lin = [e for e in (getattr(proj, "lineage", []) or [])
                   if isinstance(e, dict) and e.get("action") == "phase"]
            last_to = lin[-1].get("to") if lin else None
            phase = getattr(getattr(proj, "phase", None), "value", None)

            from singularity.scheduler._token_budget import project_budget_state
            level, spent, bmsg = project_budget_state(
                pid, getattr(proj, "token_budget_total", 0) or 0)
        except Exception as e:
            drifts.append({"project_id": pid, "kind": "reconcile_error",
                           "detail": f"{type(e).__name__}: {e}"[:100]})
            continue

        if missing:
            drifts.append({"project_id": pid, "kind": "task_ids_missing",
                           "detail": (f"{len(missing)}/{len(ids)} 个 task_id 磁盘上已不存在: "
                                      f"{[m[-8:] for m in missing[:5]]}")})
        if last_to and phase and last_to != phase:
            drifts.append({"project_id": pid, "kind": "phase_drift",
                           "detail": (f"phase={phase}，但 lineage 最后一条流转到 {last_to}"
                                      f"（有地方绕过 set_phase 改了状态）")})
        if level == "stop":
            drifts.append({"project_id": pid, "kind": "budget", "detail": bmsg})

    for d in drifts:
        try:
            from singularity.scheduler import witness
            witness.warn("reconcile", f"{d['kind']}:{d['project_id'][-8:]}:{d['detail'][:90]}"[:170])
        except Exception:
            pass
    return drifts


def _best_effort(what: str, fn, *a, **kw) -> None:
    """跑一件**收尾**的事：炸了就出声，别连累同一批里其它几件。

    这个规矩不是这里发明的 —— `_account_salvaged` 的 docstring 早就写着
    "每件各自 try：一件炸不该连累另一件（跟 `_archive_task_outcome` 同规矩）"。
    `_drain_pending` 那条**正常**收尾路原来没跟：三件（放快照引用 / 落 trace /
    归档经验与账）和 `transition` 挤在同一个 try 里 ⇒ 第一件一抛，
    后面几件**静默全跳过** —— 任务状态是 DONE，可盘上没 trace、账没记、
    经验没进记忆、路由没学习，四件事一起消失，而外面看起来一切正常。

    ⚠️ 出声用**独立 key**（`{what}_failed`），聚合视图才能把"同一件事老失败"聚起来。
    """
    try:
        fn(*a, **kw)
    except Exception as e:
        witness.warn("orch", f"{what}_failed:{type(e).__name__}:{e}"[:160],
                     key=f"{what}_failed")


def _drain_pending(pending_batches: dict, mq, results: list) -> int:
    """_run_queue_v3 步骤⑥: drain merge queue → 合成功的标 DONE。返回 drain 数。"""
    if not pending_batches:
        return 0
    from singularity.scheduler.project import repo_root_for
    drained = 0
    merge_results = mq.drain()
    for mr in merge_results:
        if mr.task_id in pending_batches:
            t, route, snap, batch = pending_batches.pop(mr.task_id)
            # ⚠️ batch 上面已经 pop 掉了 —— 这一段里任何一步抛都会出孤儿（§65）：
            # 任务不在 pending_batches 里了，循环会当成"没活干"直接退出，
            # 它就永远停在 RUNNING 没人管。包起来，抛了也留个明确的终态。
            failure_mode = ""
            try:
                if mr.status == "merged":
                    tracker.transition(t.id, TaskStatus.DONE)
                    _maybe_complete_parents(t.id)
                    results.append((t.id, f"merged: {mr.new_head[:8]}", batch.validation))
                    failure_mode = ""
                elif mr.status == "conflict":
                    err = mr.conflict_files or mr.reason or "未知冲突"
                    tracker.transition(t.id, TaskStatus.CONFLICT_HELD,
                                     error=f"conflict: {err}")
                    results.append((t.id, f"conflict: {mr.conflict_files}", batch.validation))
                    failure_mode = f"merge_conflict: {err}"
                else:
                    tracker.transition(t.id, TaskStatus.FAILED, error=f"merge {mr.status}")
                    results.append((t.id, "merge_failed", batch.validation))
                    failure_mode = f"merge_{mr.status}"
            except Exception as _e:
                # transition 抛了 ⇒ 任务还停在 RUNNING、且已从 pending 里 pop 掉 = 孤儿。
                # 下面那几件收尾**没有意义**（状态都没落），直接下一轮。
                _strand_guard(t, _e, "drain_pending")
                continue

            # ── 收尾三件：**每件各自 try**（2026-09-14 改，见 `_best_effort`）──
            # 原来它们和上面的 `transition` 挤在**同一个 try** 里 ⇒ 第一件一抛，
            # 后面几件静默全跳过，而 `_strand_guard` 报的只是第一件。
            # ⚠️ 这三件在三个分支里**参数完全一样**，所以顺势提到分支外，顺带去掉两份重复。
            _best_effort("release_ref", _release_ref, t.id, repo_root=repo_root_for(t))
            _best_effort("save_trace", _save_trace, t, route, snap,
                         batch.dispatch_result, batch.validation, False,
                         pre_search_skipped=batch.pre_search_skipped,
                         pre_search_reason=batch.pre_search_reason,
                         pre_search_top_decisions=batch.pre_search_top_decisions,
                         pre_search_memory=batch.pre_search_memory,
                         # 手里有事件、没有 disp_result 时别白攥着（见 _save_trace）
                         tool_events=batch.tool_events)
            # 经验归档 / 用量统计 / 路由学习 —— **这条路径以前完全不调**，
            # 只有 _save_trace 上面调了，于是走合并队列的任务这三件静默少做。
            # 实测（2026-09-11 真机验证）：跑完一个任务 experiences.json /
            # token_usage.json 根本没被创建，route_learner.json 一动不动。
            fresh = tracker.read_task(t.id)
            if fresh is not None:
                t.status = fresh.status      # transition 只改盘上对象，内存里还是旧状态
            _best_effort("archive_outcome", _archive_task_outcome, t, route,
                         batch.dispatch_result, failure_mode=failure_mode)
            drained += 1
    return drained


_orphans_warned: set[str] = set()


_ORPHAN_SCAN_INTERVAL_S = 60.0


def _warn_orphan_running(running_futures: dict = None, pending_batches: dict = None) -> None:
    """**只报不改**：tracker 里挂着 RUNNING、却**不在任何活任务表里**的任务 = 孤儿。

    本循环是唯一的派发方，`running_futures` ∪ `pending_batches` 就是"现在真有人管"的
    全集。落在这两个集合之外的 RUNNING 任务：没有 future ⇒ 循环看不见它 ⇒
    `ready_tasks()` 也不返回它（它是 RUNNING 不是 PENDING）⇒ **900s 收割永远够不着**，
    就那么挂着。2026-09-13 真机实测凭空少了一个任务（`1789239155520`：py-spy 栈显示
    池子里没有工作线程、循环空转到 `time.sleep(3)`）。

    🔴 **2026-09-14 改成"水位触发"**（结构性那条的第一个落点）：原来它**只在
    "队列要退出"那一刻被调一次**（`if not running_futures and not pending_batches`），
    而那只在**流水线彻底空下来**才成立 —— 任务一个接一个来的时候**永远走不到那个分支**
    ⇒ 孤儿探测等于没有。现在两个调用点：

      · **循环每轮**（`_run_queue_v3`，节流 `_ORPHAN_SCAN_INTERVAL_S`）：带上
        `running_futures` / `pending_batches` 这两个活任务表 —— 判据仍然是**精确的**
        "不在表里"，不是"多久没动静"那种超时猜法（后者会被慢模型、人审等待、
        暂停统统误伤）；
      · **退出前**：不传那两个表（此刻循环确实什么都不管，等价于原来的语义）。

    ⚠️ **只报警、不改状态** —— 自动"纠正"会把真问题抹平成假的一致
    （同 `reconcile_projects` 的规矩）。
    ⚠️ **每个任务只报一次**：这个检查每轮都会走到，不去重会刷屏。
    """
    # ⚠️ 这里**不套 try**：`running_futures` 的值形状是 `(task, route, snap, pre, ts)`，
    # 真变了就该让整个探测炸出来 —— 外层那个 `except` 会出声（`orphan_scan_failed`）。
    # 内层包一层静默 except 只会让"形状变了、孤儿从此测不出来"变成无声的
    # （静默 except 棘轮也会先报）。
    live: set[str] = {v[0].id for v in (running_futures or {}).values()}
    live |= set((pending_batches or {}).keys())
    try:
        for p in tracker.tasks_dir().glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            tid = d.get("id") or p.stem
            if d.get("status") != TaskStatus.RUNNING.value or tid in _orphans_warned:
                continue
            if tid in live:
                continue               # 有人管 —— 正常在跑的任务，不是孤儿
            _orphans_warned.add(tid)
            try:
                witness.warn("orch",
                             f"orphan_running_task:{tid}:没有 future，也没人收割它",
                             key="orphan_running_task")
            except Exception:
                pass
    except Exception as _e:
        # ⚠️ **探测器自己死了，必须说出来**（2026-09-13 外派分类抓到）。
        # 「走到没活干那一刻不该有 RUNNING 任务」这条判据，是**唯一**能发现那类孤儿的
        # 仪器（900s 收割够不着它）。它整体一抛就 pass ⇒ **仪器没了而没人知道**。
        witness.warn("orch", f"orphan_scan_failed:{type(_e).__name__}:{_e}"[:180],
                     key="orphan_scan_failed")


def _run_queue_v3(agents: dict, max_concurrent: int) -> list[tuple]:
    """v3 调度循环: dispatch→reap→drain 三步，支持 1..N 并发。

    修复 reap bug (2026-07-02): 根因不是 daemon 线程/future.done() 异步, 而是
    merge_queue 被硬编码 None → execute 走 v2 直接 merge_back → batch.merge_request
    恒 None → reap 永远走 finalize 分支。现 mq 传入 execute, v3 路径恢复。
    """
    results: list[tuple] = []
    mq = MergeQueue()
    dispatched: set[str] = set()
    running_futures: dict = {}
    pending_batches: dict = {}
    runner = TaskRunner()
    # 节流用的可变格子（用 list 是因为闭包/嵌套函数里不能重新绑定外层名字）
    _last_orphan_scan = [time.time()]

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while True:
            _dispatch_ready(dispatched, pool, agents, runner, running_futures, mq)

            if not running_futures and not pending_batches:
                # 队列无活任务也要推进阶段 (delivering/integrating/reviewing 依赖此推进, 否则永久卡死)
                _auto_trigger_test_fix(agents, results)
                remaining = tracker.ready_tasks(exclude=dispatched)
                if not remaining:
                    _warn_orphan_running()
                    break
                time.sleep(0.5)
                continue

            _reap_futures(running_futures, pending_batches, mq, runner, results)
            _drain_pending(pending_batches, mq, results)
            _auto_trigger_test_fix(agents, results)
            # **水位触发**（2026-09-14）：每轮重算一次"该有几个人在跑"，不是只在
            # "要退出了"那一刻算。带活任务表进去，判据仍是精确的"不在表里"。
            # 节流：这个检查要 glob 全部任务文件，每 3 秒一轮没必要。
            if time.time() - _last_orphan_scan[0] > _ORPHAN_SCAN_INTERVAL_S:
                _last_orphan_scan[0] = time.time()
                _warn_orphan_running(running_futures, pending_batches)

    return results


def _auto_trigger_test_fix(agents: dict, results: list[tuple]) -> None:
    """检查项目阶段推进: EXECUTING → INTEGRATING → REVIEWING (D2 集成合并)。

    F1: 集成合并异步化 — executing 任务全完成后只推进 phase→INTEGRATING,
    把 _run_integration_merge 扔进 _merge_executor 后台跑, 调度循环不阻塞。
    """
    try:
        from singularity.scheduler import project as proj_mod
        for proj in proj_mod.list_all():
            if proj.phase.value == "executing":
                # P2: 首次进入 → 拆解架构为任务
                if not proj.task_ids:
                    _decompose_and_create_tasks(proj, agents)
                    if not proj.task_ids:
                        # 拆不出任务 = 架构产物不可用。实测链路：融合失败（模型欠费）
                        # → 掉到通用合成 → 产物不是合法架构 JSON → parse_error
                        # → decompose 得 0 个任务。
                        # **必须在这里拦住**：下面的推进判据要求 `proj.task_ids` 非空，
                        # 空的话两条分支都不进 —— 项目**无声地永久卡在 executing**：
                        # 没任务可跑、推不动、没有终态、也没有任何告警（2026-09-11
                        # 真流水线实测：卡了 13 分钟，日志一行都没有）。
                        if not any(i.get("kind") == "no_decomposable_tasks" for i in proj.issues):
                            proj.issues.append({
                                "kind": "no_decomposable_tasks",
                                "message": "架构产物拆不出任务，无可执行内容（架构解析失败？）",
                                "ts": time.time(),
                            })
                            proj_mod.save(proj)
                            witness.warn("orch", f"project_no_tasks:{tracker.short_id(proj.id)}"[:80])
                        continue
                pending = [tid for tid in proj.task_ids
                          if tracker.read_task(tid) and tracker.read_task(tid).status not in (
                              tracker.TaskStatus.DONE, tracker.TaskStatus.ROLLED_BACK,
                              tracker.TaskStatus.FAILED, tracker.TaskStatus.DECOMPOSED)]
                if not pending and proj.task_ids:
                    # FAILED 也在"终态"集合里，所以这里必须再分一次：**一个都没成功
                    # 就没有可交付的东西**。以前不看这个 —— 7 个任务全失败的项目照样
                    # 一路推到 DONE 并播报"交付完成!"，用户看到的和事实完全相反
                    # （2026-09-11 探针实测：7 任务全 failed，项目 phase=done）。
                    done_ids = [tid for tid in proj.task_ids
                                if (t := tracker.read_task(tid))
                                and t.status == tracker.TaskStatus.DONE]
                    if not done_ids:
                        # 记一条 issue 并**停在这里等人处理**，不再往交付推。
                        # 已记过就不再重复（调度循环每 tick 都会走到这里，否则刷屏）。
                        if not any(i.get("kind") == "all_tasks_failed" for i in proj.issues):
                            proj.issues.append({
                                "kind": "all_tasks_failed",
                                "message": f"{len(proj.task_ids)} 个任务全部失败，无可交付内容",
                                "ts": time.time(),
                            })
                            proj_mod.save(proj)
                            witness.warn("orch", f"project_all_tasks_failed:"
                                                 f"{tracker.short_id(proj.id)}:{len(proj.task_ids)}"[:80])
                    else:
                        # D2: 推进到集成合并阶段, 异步跑 (不阻塞调度循环)
                        n_failed = len(proj.task_ids) - len(done_ids)
                        proj.set_phase(proj_mod.Phase.INTEGRATING,
                                        f"任务全部到终态(失败 {n_failed})→集成合并")
                        proj_mod.save(proj)
                        _pending_sse_events.append({
                            "kind": "system",
                            # 别再说"全部任务完成" —— 有失败时如实报数
                            "msg": (f"项目 {proj.name}: {len(done_ids)} 个任务完成"
                                    + (f"，{n_failed} 个失败" if n_failed else "")
                                    + "，进入集成合并"),
                            "ts": time.time(), "project_id": proj.id,
                        })
                        if proj.id not in _merge_inflight:
                            _submit_integration_merge(proj, agents)
            elif proj.phase.value == "delivering":
                # S1: 自动交付打包 (轻量, 同步即可)
                ok, detail = _run_delivery(proj)
                if ok:
                    proj.set_phase(proj_mod.Phase.DONE, f"交付完成: {detail[:60]}")
                    proj_mod.save(proj)
                    _record_ledger(proj, {"delivery": "ok", "detail": detail[:120]})
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付完成! {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
                else:
                    # 失败也记 —— **账本要的是结局，不是成功集**（只记成功的话，
                    # digest 里永远一片大好，下一轮照着做还是撞同一堵墙）
                    _record_ledger(proj, {"delivery": "failed", "detail": detail[:120]})
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付失败, 需人工处理 - {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
            elif proj.phase.value == "reviewing":
                # P3: 验收阶段 — 直接推GATE3等人审（FIXING 已删，状态不可达）
                from singularity.scheduler.workflow import run_test_fix_loop
                run_test_fix_loop(proj, agents)
            elif proj.phase.value == "integrating":
                # 重启恢复: 若没在跑则提交 (已在跑的跳过防重入)
                if proj.id not in _merge_inflight:
                    _submit_integration_merge(proj, agents)
    except Exception as e:
        # S6: 不再静默吞错 — 记录并通知, 避免项目卡死无反馈
        try:
            witness.warn('orch', f'auto_trigger:{e}')
        except Exception:
            pass


def _decompose_and_create_tasks(proj, agents: dict) -> None:
    """P2 兜底: 项目进了 executing 却一个任务都没有时，从架构再拆一次。

    ⚠️ **它不是"兜底"，是 GATE2 批准路径上的正常路径。** 这里原来写着"正常路径用不到它
    —— `run_phase` → `_run_execution` 在项目进入 executing 之前就把任务建好了"，
    **这个前提是假的**（2026-09-12 真机 · 项目 1789223754637 实测）：批准 GATE2 时
    `_api_projects.project_gate_confirm` **只顺手启 planning，不启 executing**
    （理由见那里的注释：executing 归调度循环推，推了就是两套驱动抢同一个 phase）。
    `_run_execution` 只被 `workflow.run_phase` 的 EXECUTING 分支调用 ⇒ 这条路它**不跑**。
    证据：lineage 里 `gate2→executing` 的 reason 是"人工批准 gate2"，
    **没有** `_run_execution` 才会写的"架构确认 → 建任务进执行"。

    ⇒ 所以这里必须**自己把 `constraints_checklist` 写上**（见下面的赋值）：
    它的唯一写点在 `_run_execution` 里，而这里才是真正建任务的那条路。
    漏写的后果不是"少个字段"：`_run_verification` 进门第一句就早退，
    **机械检查一条都跑不了**（防御模式 §60）。

    ⚠️ 它以前读 `<项目目录>/architecture.json` —— **全仓没有任何代码写这个文件**
    （唯一提及它的就是这里），所以永远卡在第一步 `if not arch_path.exists(): return`：
    一次都没救成功过。真进入"executing 且没任务"的项目，只能一直卡着（2026-09-11
    真流水线实测：卡了 13 分钟、零日志）。
    改成读 `proj.architecture` —— 跟 `_run_execution` 同源。
    """
    # ⚠️ 定义在 `try` **外面**：下面的 `except` 要用它做回滚，而异常可能发生在
    # 它被赋值之前（那样 handler 里引用它会变成 NameError、把原异常盖掉）。
    new_ids: list[str] = []
    try:
        arch_json = proj.architecture or {}
        if not isinstance(arch_json, dict):
            return

        from singularity.scheduler.execution_judge import decompose_architecture
        tasks = decompose_architecture(arch_json)
        if not tasks:
            return

        # 项目代码写进独立 git 仓库 (与奇点仓库隔离, 修复 #1)
        from singularity.scheduler.project import ensure_repo
        ensure_repo(proj.id)

        # 与 _run_execution 对齐。这条兜底原来只写 desc，丢了四样东西：
        #   ① **depends_on 完全没有** —— 架构里的 DAG 被压平，本该串行的任务同时开跑，
        #      产物互相看不见 + 争同一批文件 + 合并冲突；
        #   ② route_level 取 t["suggested_level"]，而那是"层"(impl/backend)不是档位；
        #   ③ 不绑 route_role → 角色提示词不会注入（那条链路本来就死过两个半月）；
        #   ④ 旧 task_ids 不清 —— 与重规划那条同一个病（见 _workflow_phases）。
        from singularity.scheduler.roles import get_phase_role
        from singularity.scheduler.project import Phase
        role_key = get_phase_role(Phase.EXECUTING) or "implementer"
        proj.task_ids = []
        id_map: dict[str, str] = {}
        for idx, t in enumerate(tasks):
            local_id = t.get("id", "") or f"T{idx+1}"
            arch_deps = t.get("depends_on", []) or t.get("depends_on_local_id", [])
            dep_ids = [id_map[d] for d in arch_deps if d in id_map]
            task = tracker.create(t["desc"], project_id=proj.id, depends_on=dep_ids)
            new_ids.append(task.id)      # 回滚集合：create 成功就记下（transition 抛时还没 append）
            tracker.transition(task.id, tracker.TaskStatus.PENDING,
                             route_level="any",
                             route_locked=True,
                             route_role=role_key)
            proj.task_ids.append(task.id)
            id_map[local_id] = task.id

        # 约束清单的唯一写点在 `_run_execution`，而这条路它不跑（见 docstring）——
        # 所以在这里补上，否则验收时清单恒空 ⇒ 机械检查整段跳过（§60）。
        # 与 `_run_execution` 同源同式（`architecture["constraints"]`），按构造相等。
        proj.constraints_checklist = arch_json.get("constraints", []) or []

        from singularity.scheduler.project import save
        save(proj)
        _pending_sse_events.append({
            "kind": "system", "msg": f"架构拆解完成: {len(tasks)} 个任务已入队",
            "ts": time.time(), "project_id": proj.id,
        })
    except Exception as e:
        # 建了任务却没登记进项目 = 它**永远不会被派发**（项目页数不到它、orchestrator
        # 只认 `task_ids`），可从界面看它就是一条正常的 pending ⇒ 撤销这一批。
        tracker.rollback_create(new_ids, why="_decompose_and_create_tasks 建任务后登记失败")
        try:
            witness.warn('orch', f'decompose:{e}')
        except Exception:
            pass


def _run_integration_merge_async(project_id: str, agents: dict) -> None:
    """F1: 后台线程跑集成合并 + 后续 phase 推进。

    完成后回写 phase (→REVIEWING 调 run_test_fix_loop, 或 →EXECUTING/GATE2 重试),
    释放 _merge_inflight, SSE 通知。任何异常都不抛出 (后台线程无法冒泡)。
    """
    from singularity.scheduler import project as proj_mod
    try:
        proj = proj_mod.load(project_id)
        if proj is None:
            return
        ok, detail = _run_integration_merge(proj)
        if ok:
            # D1: 检查审查失败次数是否触顶
            from singularity.scheduler._review import check_review_fail_limit
            fail_check = check_review_fail_limit(proj.id,
                getattr(proj, 'review_failures', 0))
            if fail_check["blocked"]:
                proj.set_phase(proj_mod.Phase.GATE2, fail_check["reason"])
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": fail_check["reason"],
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                proj.set_phase(proj_mod.Phase.REVIEWING, "集成合并通过")
                proj_mod.save(proj)
                from singularity.scheduler.workflow import run_test_fix_loop
                msg = run_test_fix_loop(proj, agents)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并通过 → REVIEWING {msg[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
        else:
            # 集成合并失败 → 计数
            integrate_fails = getattr(proj, 'integrate_failures', 0) + 1
            proj.integrate_failures = integrate_fails
            if integrate_fails >= proj_mod._INTEGRATE_MAX_RETRIES:
                # 触顶 → 打回架构 (GATE2)
                proj.set_phase(proj_mod.Phase.GATE2, f"集成合并{integrate_fails}次失败(上限{proj_mod._INTEGRATE_MAX_RETRIES})")
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并{integrate_fails}次失败→升GATE2: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                # 回实现层重试
                proj.set_phase(proj_mod.Phase.EXECUTING, f"集成合并失败第{integrate_fails}次→回实现层重试")
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并失败({integrate_fails}/{proj_mod._INTEGRATE_MAX_RETRIES})→回实现层: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
    except Exception as e:
        try:
            witness.warn('orch', f'integrate_async:{e}')
            _pending_sse_events.append({
                "kind": "system", "msg": f"集成合并异常: {e}",
                "ts": time.time(), "task_id": project_id,
            })
        except Exception:
            pass
    finally:
        _merge_inflight.discard(project_id)


def _run_integration_merge(proj) -> tuple[bool, str]:
    """D2 集成合并: 拓扑合并 + 集成测试 + 冒烟构建。

    返回 (ok, detail)。
    ponytail: 集成测试跑 test_cases.json 中的 integration 用例, 没有则跳过。
    """
    import subprocess
    from pathlib import Path as _Path
    from singularity.scheduler.project import repo_dir as _repo_dir
    # 修复: 原来取 config.PROJECT_ROOT = 奇点自己的仓库 —— 于是集成检查的是奇点的工作区
    # (你正在改奇点时项目会被无理由打回), 跑的是奇点的 test_cases.json。
    root = str(_repo_dir(proj.id))
    if not (_Path(root) / ".git").exists():
        return False, f"项目仓库不存在: {root}"

    # 1) 拓扑合并: 检查所有 worktree 已合并 (merge_queue drain 已处理)
    #    此处做最终一致性检查: git status 是否干净
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10, cwd=root)
        if r.returncode != 0:
            return False, f"git status 失败: {r.stderr[:100]}"
        dirty = [l for l in (r.stdout or "").split("\n") if l.strip() and not l.startswith("??")]
        if dirty:
            return False, f"工作区不干净 ({len(dirty)} 个变更)"
    except Exception as e:
        return False, f"git status 异常: {e}"

    # 2) 集成测试: 跑 test_cases.json 中的 integration 用例
    tc_path = _Path(root) / "test_cases.json"
    if tc_path.exists():
        try:
            import json as _json
            tc = _json.loads(tc_path.read_text())
            integration_cases = tc.get("integration", [])
            if integration_cases:
                # 跑 pytest (如果项目有测试)
                r = subprocess.run(
                    ["python3", "-m", "pytest", "-q", "--tb=short", "-k", "test_integration"],
                    capture_output=True, text=True, timeout=120, cwd=root)
                if r.returncode != 0:
                    return False, f"集成测试失败: {(r.stdout+r.stderr)[:200]}"
        except Exception as e:
            return False, f"集成测试异常: {e}"

    # 3) 冒烟构建检查: 是否存在可构建产物
    # ponytail: 轻量检查 — 有 Dockerfile 则验证语法, 有 pyproject.toml 则 pip install --dry-run
    if (_Path(root) / "Dockerfile").exists():
        try:
            r = subprocess.run(["docker", "build", "--check", "."], capture_output=True, text=True, timeout=30, cwd=root)
            if r.returncode != 0:
                return False, f"Docker build check 失败: {(r.stdout+r.stderr)[:200]}"
        except FileNotFoundError:
            pass  # docker 不可用, 跳过
        except Exception as e:
            return False, f"冒烟构建异常: {e}"

    return True, "集成合并通过"


def _run_delivery(proj) -> tuple[bool, str]:
    """S1 交付: 代码归档 + 产物打包 + 交付文档 + 报告归档。

    返回 (ok, detail)。
    ponytail: 不自动部署到生产 — 部署风险高且涉及用户基础设施。
    """
    import subprocess
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from singularity.scheduler.project import repo_dir as _repo_dir
    # 修复: 原来是奇点自己的仓库 —— tag 打在奇点上, 产物探测读的是奇点的 pyproject.toml
    root = str(_repo_dir(proj.id))
    if not (_Path(root) / ".git").exists():
        return False, f"项目仓库不存在: {root}"

    deliverables = {
        "code_ref": "", "artifacts": [], "docs": [], "reports": [],
        "handed_over_at": _dt.now().isoformat(),
    }

    # 1) 代码归档: 打 tag
    try:
        tag_name = f"release/{proj.id}-{_dt.now().strftime('%Y%m%d%H%M')}"
        r = subprocess.run(["git", "tag", tag_name], capture_output=True, text=True, timeout=15, cwd=root)
        if r.returncode == 0:
            deliverables["code_ref"] = tag_name
        else:
            # 无 git 或不成功 → 用 HEAD commit
            r2 = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=root)
            deliverables["code_ref"] = r2.stdout.strip()[:12] if r2.returncode == 0 else "unknown"
    except Exception:
        deliverables["code_ref"] = "unknown"

    if deliverables["code_ref"] == "unknown":
        # ⚠️ **别让它悄悄过去**（2026-09-14）：一旦落到这里，"交付物清单里的
        # `code_ref` 是个占位符"这件事**只存在于 detail 串里** —— 界面上它和正常交付
        # 长得一模一样（phase 照样推 DONE、账本照样记 `delivery: ok`）。
        # 真机上还没触发过（8/8 都是真 `release/*` tag），但触发的那一刻正是
        # "这次交付到底归档了哪个 commit"最重要的时候。
        # ⚠️ **仍然算交付成功**：tag 打不上 ≠ 代码没交付 —— 判失败会把好项目卡死在 delivering。
        witness.warn("orch", f"delivery_no_code_ref:{proj.id}"[:160],
                     key="delivery_no_code_ref")

    # 2) 交付文档: 收集 README + 部署说明
    # 应用数据留在奇点数据目录（原先 root=.qidian 恰好等于 QIDIAN_DIR，改成项目仓库后必须钉住）
    docs_dir = config.QIDIAN_DIR / "deliverables" / proj.id
    docs_dir.mkdir(parents=True, exist_ok=True)
    for fname in ["README.md", "DEPLOY.md", "README"]:
        src = _Path(root) / fname
        if src.exists():
            import shutil
            dst = docs_dir / fname
            shutil.copy2(str(src), str(dst))
            deliverables["docs"].append(fname)

    # 3) 报告归档: QA/Security/机器检查
    # ⚠️ 报告**不在项目仓库里**：`_save_phase_output()` 一律写
    # `.qidian/projects/<id>.<名字>`（见 workflow._phase_output_path）。
    # 原来是在 `root`（项目仓库）下找 `qa_report.json` / `security_report.json` /
    # `test_cases.json` / `review_report.json` —— **目录和名字双双对不上**，
    # 后两个全仓**压根没人写** ⇒ 这一栏**永远空**，而界面上它跟"正常交付"
    # 长得一模一样（phase 照样推 DONE、账本照样记 delivery: ok）。
    # ⚠️ 名字必须跟 `_save_phase_output` 的真实调用点对齐，别再照抄旧列表。
    from singularity.scheduler.workflow import _phase_output_path as _pop
    for fname in ["qa_report.json", "qa-report.md", "security-report.md",
                  "machine-checks.json", "e2e_checklist.json"]:
        if _pop(proj.id, fname).exists():
            deliverables["reports"].append(fname)

    # 4) 产物打包: 按项目类型
    if (_Path(root) / "pyproject.toml").exists():
        deliverables["artifacts"].append({"name": "python-package", "type": "package"})
    if (_Path(root) / "Dockerfile").exists():
        deliverables["artifacts"].append({"name": "docker-image", "type": "image"})
    if (_Path(root) / "package.json").exists():
        deliverables["artifacts"].append({"name": "npm-package", "type": "package"})

    # 写交付清单
    manifest_path = docs_dir / "delivery_manifest.json"
    manifest_path.write_text(_json.dumps(deliverables, ensure_ascii=False, indent=2))

    # ⚠️ 报告数一起报 —— 不然"报告一栏是空的"这件事在日志里看不出来，
    # 只有翻 manifest 才发现（原来就是这个问题）。
    return True, (f"交付完成: tag={deliverables['code_ref'][:16]}, 文档={len(deliverables['docs'])}, "
                  f"制品={len(deliverables['artifacts'])}, 报告={len(deliverables['reports'])}")

