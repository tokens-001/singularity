"""内部模块 — 核心执行引擎。

纯执行: dispatch + validate + trace。worker 线程安全，不写 tracker。
"""

from __future__ import annotations

import json
import time

from singularity.scheduler import config, tracker, witness
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import memory as mem_mod
from singularity.scheduler import neijinglu as nj_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import validator as val_mod

# ponytail: context 函数提取到 _exec_context.py, 此文件 re-export 保持兼容
from singularity.scheduler._exec_context import (
    _PLANNER_PREAMBLE,
    _build_project_context,
    _construct_context,
    _inject_memory,
)
from singularity.scheduler._git_worktree import (
    commit_wt,
)
from singularity.scheduler._git_worktree import (
    merge_back as wt_merge_back,
)
from singularity.scheduler._types import BatchOutput, RunContext, _pending_sse_events, _SnapProxy
from singularity.scheduler._worktree import (
    _anchor_ref,
    _build_merge_request,
    _cleanup_wt,
    _lock_wt,
    _maybe_create_worktree,
)
from singularity.scheduler.log import timed

# 全局底线：所有任务的约束，首轮无条件注入（原 karpathy-rules 技能，从技能层提升到全局层）
_GLOBAL_CONSTRAINTS = """全局底线（所有任务必须遵守）：
1. 不自作主张：不确定需求先澄清，不猜测；只做需求明确要求的
2. 简洁优先：用最少代码解决问题，优先标准库，不引入不必要的抽象
3. 精准修改：只改任务直接相关的代码，不顺手重构，diff 只含必要变更
4. 目标驱动：先定位根因再动手，改完验证，任务完成就停
5. 错误处理：输入为空、异常输入、边界情况都要处理，不留裸奔路径
6. 输出规范：多文件改动时，每个文件一个代码块，开头注明文件路径（如 <!-- @files: a.py,b.py -->）
"""


def _build_effective_task(task, turn: int, feedback: str, is_planner: bool,
                          tool_events: list = None, route_role: str = "") -> str:
    """拼接最终 prompt: 记忆注入 + 角色上下文 + planner preamble + 项目上下文。

    Step 4: route_role 非空时注入角色 system_prompt。
    """
    effective_task = task.description
    # ── 全局底线：所有任务首轮无条件注入 ──
    if turn == 1:
        effective_task = _GLOBAL_CONSTRAINTS + "\n\n" + effective_task
    # ── Step 4: 角色上下文注入 (首轮) ──
    if turn == 1 and route_role:
        role_ctx = _inject_role_context(route_role)
        if role_ctx:
            effective_task = role_ctx + "\n\n---\n" + effective_task
    # ── MAGMA 记忆注入 (仅首轮、无打回反馈时) ──
    if turn == 1 and feedback == "":
        mem_ctx = _inject_memory(task.description)
        if mem_ctx:
            effective_task = mem_ctx + "\n\n" + effective_task
    # ── ConstructContext: 工具历史裁剪 (turn≥2 且有工具事件时) ──
    if tool_events:
        ctx_ctx = _construct_context(tool_events, turn)
        if ctx_ctx:
            effective_task = ctx_ctx + "\n" + effective_task
    if is_planner:
        effective_task = _PLANNER_PREAMBLE + effective_task
    # ── 项目上下文注入 ──
    proj_ctx = _build_project_context(task)
    if proj_ctx:
        effective_task = proj_ctx + "\n\n---\n" + effective_task
    return effective_task


def _inject_role_context(route_role: str) -> str:
    """Step 4: 从 roles.toml 加载角色（system_prompt + persona 人格）作为执行上下文。"""
    try:
        from singularity.scheduler.roles import get_role
        role = get_role(route_role)
        if role:
            return role.get_full_prompt()
    except Exception as e:
        # 静默返回 "" = agent 悄悄丢掉角色提示词，行为差异在外面完全看不见
        witness.warn('orch', f'role_context:{route_role}:{e}'[:80])
    return ""


# ponytail: sidecar 里最多留这么多条工具事件。正常一轮就几个，500 够几十轮了；
# 真被截断也只会让超时 trace 的 tool_batches **少数几轮**（不是好看，是如实少报）。
_PARTIAL_TOOL_EVENTS_CAP = 500


def _persist_partial_usage(task_id: str, level: str, model: str, delta: int,
                           tool_events: list | None = None,
                           elapsed: float | None = None,
                           turn: int = 0, attempt: int = 0) -> None:
    """把执行中**累计**的 token 落一盘，给超时路径用。

    为什么必须边跑边落：超时被杀的任务**走不到收尾记账**
    （`_task_runner._archive_task_outcome` 是唯一记账入口，它在 finalize 那条路上，
    而超时分支直接把任务判失败、不进 pending）。执行器自己那个 `total_tokens`
    在它线程里，超时方 `fut` 已经 pop 掉、拿不到 —— 只能落盘。

    ⚠️ **这是下界，不是精确值**：粒度是"每次 dispatch 之后"，所以
    **超时那一刻正在飞的那次模型调用**不在里面（那次可能很贵）。
    如实当下界用，别当准确数。

    累加写（读回来 + delta）：一次任务可能换 agent 重试，每次 dispatch 的
    `token_count` 是**那一次**的用量，得累加才是这个任务总共烧的。
    """
    try:
        config.ensure_dirs()
        p = config.PARTIAL_USAGE_DIR / f"{task_id}.json"
        cur, cur_model, _prev = 0, model, {}
        if p.exists():
            try:
                _prev = json.loads(p.read_text(encoding="utf-8")) or {}
                cur = int(_prev.get("tokens", 0) or 0)
                cur_model = _prev.get("model") or model
            except Exception:
                cur, cur_model, _prev = 0, model, {}
        payload = {
            "task_id": task_id, "level": level, "model": cur_model,
            "tokens": cur + max(0, int(delta or 0)), "updated_at": time.time(),
        }
        # `started_at` 是 `_mark_dispatch_started` 在 dispatch 开头落的 —— 这里必须
        # **带着它一起写回去**，否则一次正常的累加落盘就把"进过 dispatch"这个事实抹了。
        if _prev.get("started_at") is not None:
            payload["started_at"] = _prev["started_at"]
        # ── 每次 dispatch 花了多久（量尺，2026-09-13）──
        # 为什么要记：执行器的 810s 自收尾预算**每次 dispatch 都重置**
        # （`_dispatch_exec._run_executor` 每次新建 executor，`openai_agent.run()` 里
        # `start = time.time()`），而 orchestrator 的 900s 是**任务级**的、且
        # `_exec.run` 外层循环一圈表都不看。⇒ 任务只要跑过 ≥2 次 dispatch、
        # 每次都没到 810s，自收尾就永远不触发，人却早被 900s 无声收割了。
        # 超时任务走不到收尾 ⇒ 这份只能落盘（跟 token 一个理由，见函数头）。
        if elapsed is not None:
            _disp = list(_prev.get("dispatches") or [])
            # `turn` / `attempt` 是**这次 dispatch 属于哪一轮**（2026-09-19 加）：
            # 「这个任务自动返工了几轮」原来答不上 —— 内层那圈（同 wt 带反馈重派，
            # `_decide_cascade` 的 "continue"）只活在内存里，外层那圈
            # （`_run_with_retry` 整个 run() 重跑）连变量都出不了那个函数。
            # 两个数都是**当场的事实**，不是算出来的派生值（§34）：
            #   turn    = `run()` 里那个 `for turn in range(...)` 的第几圈；
            #   attempt = `ctx.retry_count`，`_run_with_retry` 每次进 run() 前写的。
            # 读法：同一 attempt 里 turn>1 的条数 = 内层返工轮数；
            #       max(attempt)+1 = 这个任务被整个重跑了几次。
            _disp.append({"at": round(time.time(), 1), "elapsed": round(float(elapsed), 1),
                          "turn": int(turn or 0), "attempt": int(attempt or 0)})
            payload["dispatches"] = _disp[-20:]
        if tool_events is not None:
            # tool_events 平时只在内存和 SSE 里过一遍、**从来不落盘**
            # ⇒ 超时任务的 trace 里 tool_batches 的 turns 恒为 0
            # （探路3 的 T1/T2 实测就是这个，见"一次多动作埋点"那一条）。
            payload["tool_events"] = list(tool_events)[-_PARTIAL_TOOL_EVENTS_CAP:]
        elif p.exists():
            try:
                payload["tool_events"] = json.loads(
                    p.read_text(encoding="utf-8")).get("tool_events") or []
            except Exception:
                pass
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        # S6：**这份侧车是超时任务唯一的账**（收尾记账走不到，见函数头）——
        # 写失败 = 整笔账没了。原注释写"记不记成账是次要的"，那是**把两个后果当成一个**：
        # 对这个任务而言确实次要（它照旧不该被带崩、不上抛），但代价落在**后面所有
        # "这一轮烧了多少"的判断**上 —— 少一笔，而没人知道少了。
        # 所以：不上抛不变，但必须出声。
        witness.warn("exec", f"partial_usage_persist_failed: {task_id} {type(e).__name__}: {e}")


def _dispatch_budget_s(ctx) -> float | None:
    """这次 dispatch **还能花多少秒** —— 从任务那把唯一的尺倒推。

    = `ctx.deadline_at`（任务死亡时刻的绝对值）− 现在 − 收尾余量。
    每次 dispatch 都重算，所以**上几次 dispatch、重试、中间等人审花掉的时间全扣掉了**。

    为什么不能像原来那样让执行器用自己 `run()` 里的 `start` 起算：
    执行器是 `_dispatch_exec._run_executor` **每次 dispatch 新建**的，
    用 `start` 等于每 dispatch 把预算清零 ⇒ 只要任务跑过 ≥2 次 dispatch
    （模型每轮几十秒时是常态）自收尾就**永远不触发**，而人早被外面那 900s 无声收割。
    2026-09-13 查明，见 `docs/防御模式.md` §67。

    `ctx.deadline_at == 0`（goal_loop / 阶段级那条路没给）⇒ `None` = 执行器退回老行为，
    **不是"立即到期"**。
    """
    if not ctx.deadline_at:
        return None
    return ctx.deadline_at - time.time() - config.TASK_WRAPUP_MARGIN_S


def _budget_exhausted(ctx) -> bool:
    """任务那把表到点了吗 —— 到点就**别再去调模型**了。

    跟 `_dispatch_budget_s` 共用同一个判据（`<= 0` 就是它，不再写第二份算法），
    因为"发起"和"收尾"必须对同一把尺：只要预算 ≤0，新执行器必然在第 1 轮开头
    `_wrapped=True`（`openai_agent.py:437`）**立刻空转收尾**。

    ⚠️ **为什么非要挡在 dispatch 之前，而不是让它收尾**（2026-09-16 真机坐实）：
    那次空收尾只活了 39 毫秒，返回一具「0 轮 / 0 文件 / 0 token」的空壳 ——
    而这具空壳会被收尾那条 `return` 当成 `BatchOutput.dispatch_result`，
    **把前面几轮真干出来的账整份盖掉**（改了哪些文件、烧了多少 token、合并请求）。
    外面看到的是「QA: [completeness] 无文件改动」→ 任务 failed → 产物进不了合并队列，
    而 worktree 里那份 75 行的实现好好躺在 `refs/qidian/pending/` 上。

    `ctx.deadline_at == 0`（goal_loop / 阶段级那条路没给）⇒ `False` = 不管，
    **不是"立即到期"**（同 `_dispatch_budget_s`）。
    """
    b = _dispatch_budget_s(ctx)
    return b is not None and b <= 0


def _mark_dispatch_started(task_id: str) -> None:
    """dispatch **开始**时落一个时间戳 —— 只为让两种"没账"分得开。

    被 900s 收割的任务，`token_count = None` 有**两种成因**：
      ① 压根没发起过模型调用；
      ② 发起了，但那一刻正在飞的调用没落盘。
    §59 说过"分不出'真没调工具'和'没落盘'"—— 落到 token 上就是这两条。
    加这个标记之后：**sidecar 在不在**就能分开它们（在 = 进过 dispatch）。

    ⚠️ **不改 token 的语义**：它照样是**下界**，别当准确数。
    ⚠️ `started_at` 只写第一次（后来的 dispatch 不覆盖它）——
    它回答的是"这个任务有没有进过 dispatch"，不是"最后一次什么时候"。
    """
    try:
        config.ensure_dirs()
        p = config.PARTIAL_USAGE_DIR / f"{task_id}.json"
        d: dict = {}
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                d = {}
        d.setdefault("started_at", time.time())
        d["task_id"] = task_id
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass    # 落盘失败不该把任务带崩


def read_partial_started_at(task_id: str) -> float | None:
    """dispatch 开始过的时刻；**从没进过 dispatch** 才返回 `None`。"""
    try:
        p = config.PARTIAL_USAGE_DIR / f"{task_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8")).get("started_at")
    except Exception:
        return None


def read_partial_usage(task_id: str) -> tuple[int, str]:
    """读回累计用量 → `(tokens, model)`。没有/读坏了一律 `(0, "")`。"""
    try:
        p = config.PARTIAL_USAGE_DIR / f"{task_id}.json"
        if not p.exists():
            return 0, ""
        d = json.loads(p.read_text(encoding="utf-8"))
        return int(d.get("tokens", 0) or 0), str(d.get("model") or "")
    except Exception:
        return 0, ""


def read_partial_tool_events(task_id: str) -> list:
    """读回执行中攒的工具事件。没有/读坏了返回 `[]`。

    超时任务的 tool_events 原来恒空（它在线程里，fut 早被 pop 掉）⇒ trace 里
    `tool_batches.turns` 是 0 ⇒ **超时任务在"一次多动作"那套度量里根本没法算**
    （探路3 T1/T2 实测）。跟 token 同一个道理：只能边跑边落。
    """
    try:
        p = config.PARTIAL_USAGE_DIR / f"{task_id}.json"
        if not p.exists():
            return []
        d = json.loads(p.read_text(encoding="utf-8"))
        ev = d.get("tool_events")
        return list(ev) if isinstance(ev, list) else []
    except Exception:
        return []


def _check_cancelled(task, all_tool_events: list) -> BatchOutput | None:
    """检查"停"标记。返回 BatchOutput 表示该停; None 表示继续。

    ⚠️ **这个标记有两个来源，必须分开报**（2026-09-19）：

    · **用户点的**（`_api_tasks.task_cancel` 写）→ `cancelled_by_user`
    · **调度器超时写的**（`orchestrator._reap_futures` 写，body 带 `by: "timeout"`）
      → `cancelled_by_timeout`

    它们**共用同一个文件和同一个消费者**：超时那条也要靠这个标记让执行线程在下一
    turn 边界提前收手（`fut.cancel()` 拦不住已经开始跑的 future，那时它还在烧 token）。
    但后果不一样 —— 把超时读成"用户取消"，就是**账记在用户头上而他什么都没做**。

    ⚠️ 两种都保留 `"cancelled"` 前缀：全仓的消费方（`_exec._run_with_retry`、
    `goal_loop`、`_task_runner`）判的是这个前缀，语义都是"**用户叫停，别重试**"。
    超时中断同样不该在这条路里重试 —— 外层已经把这轮判死了，重试只是再烧一遍。

    读不出 `by`（旧格式 / 空 body / 文件坏了）→ **按用户取消处理**，即旧行为。
    那是最保险的一侧：宁可把一次超时记成用户取消，也不要把用户取消咽掉。
    """
    # 🔵 读法**只此一份**（`tracker.take_cancel_marker`）—— 因为同一个标记的另一个
    # 消费者是 `tracker.recover()`（进程重启那一下必须也兑现它，见那里）。两份读法
    # 迟早有一条忘了改。
    by = tracker.take_cancel_marker(task.id)
    if by is None:
        return None

    if by == "timeout":
        return BatchOutput(
            ok=False, task_id=task.id,
            term_reason="cancelled_by_timeout",
            validation=val_mod.ValidationReport(
                verdict="阻断", action="abort",
                unverified=["调度器超时中断（不是用户取消）"],
            ),
            tool_events=all_tool_events, turn_count=0,
        )
    return BatchOutput(
        ok=False, task_id=task.id,
        term_reason="cancelled_by_user",
        validation=val_mod.ValidationReport(
            verdict="阻断", action="abort",
            unverified=["用户手动取消"],
        ),
        tool_events=all_tool_events, turn_count=0,
    )


def _check_paused(task) -> bool:
    """检查人工暂停标记。有暂停信号→切 PAUSED 状态→阻塞等待恢复。

    confirm_changes 模式下, 每 turn 自动写暂停信号 (用户每步确认)。
    返回 True 表示已恢复继续; False 表示任务已终止。
    """
    from singularity.scheduler import tracker as tracker_mod

    # confirm_changes: 每 turn 自动暂停 (用户点了 resume 后下个 turn 再暂停)
    mode = getattr(task, 'execution_mode', 'auto_edit') or 'auto_edit'
    pause_path = config.PAUSE_DIR / f"{task.id}.json"
    if mode == "confirm_changes" and not pause_path.exists():
        config.ensure_dirs()
        pause_path.write_text(json.dumps({"task_id": task.id, "paused_at": time.time(), "auto": True}),
                            encoding="utf-8")

    if not pause_path.exists():
        return True  # 无暂停信号, 继续执行

    # 切到 PAUSED 状态
    tracker_mod.transition(task.id, tracker_mod.TaskStatus.PAUSED)

    # 阻塞等待: 轮询检测 pause 文件被删除=恢复信号
    import time as _time
    while pause_path.exists():
        _time.sleep(1)
        # 期间如果任务被取消, 退出等待
        cancel_path = config.CANCEL_DIR / f"{task.id}.json"
        if cancel_path.exists():
            return False

    # 恢复: 切回 RUNNING
    tracker_mod.transition(task.id, tracker_mod.TaskStatus.RUNNING)
    return True


def _process_planner_or_merge(task, ctx, turn, level, is_planner, wt,
                              exec_result, disp_result, all_tool_events,
                              pending_merge_req_holder: list, repo_root=None):
    """处理 executor 成功后的 planner 分解 / v3-v2 merge 分支。

    返回信号:
      None  → 继续 validate
      BatchOutput → 直接返回此结果 (planner 分解成功 / v2 merge 冲突)

    pending_merge_req_holder 是单元素 list, 用于 v3 路径回填 merge_request (保持引用语义)。
    """
    if is_planner:
        _save_planner_patch(task.id, exec_result.raw_output)
        subtasks = decompose(exec_result.raw_output)
        if subtasks:
            return BatchOutput(
                ok=True, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"decomposed (level={level}, turn={turn})",
                validation=val_mod.ValidationReport(
                    verdict="通过", action="pass",
                    unverified=[f"planner 分解出 {len(subtasks)} 子任务"],
                ),
                planner_decomposed=True,
                planner_subtasks=subtasks,  # 传给主线程, 避免二次解析
                tool_events=all_tool_events, turn_count=turn,
            )
    elif wt:
        if ctx.merge_queue is not None:
            # v3: commit_wt 拿含改动的 commit (修复 #2), 不直接 merge
            branch_ref = commit_wt(wt)
            # ⚠️ **工作树没动过时，`commit_wt` 返回的是 HEAD —— 那是基线，不是这个任务的产物。**
            # 工作树是从 `ctx.snapshot_ref` 建的（`wt_create(base_ref=snapshot_ref)`），
            # 所以"什么都没发生"的判据就是 `branch_ref == snapshot_ref`。
            #
            # 无条件锚它 = **把别人的活记到这个任务头上**：2026-09-18 round g 的 `…835`
            # 被判「无文件改动」却"有可打捞产物"，锚的就是它哥哥 `…833` 的合并提交
            # （835 自己的工具日志里一次 `write_file` 都没有）。它能走到这里，是因为
            # 更上游把"被掐断的半截思考"当成了正文 ⇒ `success=True`。
            #
            # ⚠️ **不能改用 `_has_changes_in_wt`**（那看的是"未提交改动"）：agent 自己
            # `git commit` 过之后工作区是干净的，用它判断会把**真的产出**当成没产出、
            # 连合并都不做 —— 比原 bug 更坏。**跟基线比才是对的。**
            # `snapshot_ref` 为空（没建快照）时 `branch_ref != ""` 成立 ⇒ 退回原行为。
            if branch_ref and branch_ref != ctx.snapshot_ref:
                _anchor_ref(task.id, branch_ref, repo_root=repo_root)  # 防 gc 回收 (重要 #3)
                pending_merge_req_holder[0] = _build_merge_request(
                    task, branch_ref, ctx.snapshot_ref, repo_root=repo_root,
                )
        else:
            # v2: 直接 merge_back
            mr = wt_merge_back(wt, repo_root=repo_root)
            if not mr.ok:
                reason = mr.reason or f"冲突文件: {mr.conflicts}"
                return ("merge_conflict", level, turn, disp_result, all_tool_events, reason)
    return None


def _decide_cascade(task, level, turn, validation, disp_result, all_tool_events,
                    pending_merge_req, fallback_chain, tried_models, quality):
    """cascade routing 决策。

    返回 (action, payload):
      ("return", BatchOutput)  → 直接返回 (pass / cascade_accept / 非 retry 终态)
      ("break", None)          → 跳出 turn loop, 升级或换 agent (finally 清 wt)
      ("continue", feedback)   → 中置信 retry, 复用同一 wt
    """
    if validation.action == "pass":
        return ("return", BatchOutput(
            ok=True, task_id=task.id, dispatch_result=disp_result,
            term_reason=f"pass (level={level}, turn={turn})",
            validation=validation,
            merge_request=pending_merge_req,
            tool_events=all_tool_events, turn_count=turn,
        ))

    if validation.action == "retry":
        # 软质量硬门槛: 首轮软修复 (turn=1 retry), 触顶 (turn>=2) 仍软伤 → 不静默放行, 标失败升人工
        if quality.get("failure_kind") == "soft_quality" and turn >= 2:
            return ("return", BatchOutput(
                ok=False, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"soft_quality_gate (软质量软修一轮未达标, level={level}, turn={turn})",
                validation=validation, merge_request=pending_merge_req,
                tool_events=all_tool_events, turn_count=turn,
            ))
        conf = validation.confidence
        # 高置信 → 跳过升级，接受当前结果 (省钱)
        #
        # ⚠️ **必须同时要求 failure_kind == "ok"**（2026-09-19 外派评审核出，本机复核成立）。
        # 光看 conf 会被"形状"骗过去：`post_execution_hook` 的基线是 0.5，长输出 +0.1、
        # 含 "passed" +0.15 = **正好 0.75** —— 于是任何一条 -0.1 的软警告扣完还剩 0.75，
        # 照样在这里被接受，那轮承诺的软修（soft_quality，见 _review）永远轮不到。
        # failure_kind 是"审查层判过什么"的直接记录，比分数稳：分数能被无关的加分项抬回来。
        if conf >= 0.75 and quality.get("failure_kind", "ok") == "ok":
            return ("return", BatchOutput(
                ok=True, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"cascade_accept (level={level}, conf={conf:.2f})",
                validation=validation, merge_request=pending_merge_req,
                tool_events=all_tool_events, turn_count=turn,
            ))
        # 低置信 + 还有更高级模型 → 立即升级，不浪费重试
        if conf < 0.35 and len(fallback_chain) > 1:
            return ("break", None)
        # 中置信 → 正常重试（给同一个模型改进机会, 复用同一 wt）
        fb_parts = [json.dumps(validation.evidence, ensure_ascii=False, indent=2)]
        if quality.get("warnings"):
            fb_parts.append("质量警告:\n" + "\n".join(f"- {w}" for w in quality["warnings"]))
        if quality.get("failure_kind") and quality["failure_kind"] != "ok":
            fb_parts.append(f"失败类型: {quality['failure_kind']}, 置信度: {quality['confidence']:.2f}")
        return ("continue", "\n\n".join(fb_parts))

    # rollback / abort 等非 retry 终态
    return ("return", BatchOutput(
        ok=False, task_id=task.id, dispatch_result=disp_result,
        term_reason=f"{validation.action} (level={level}, turn={turn})",
        validation=validation,
        tool_events=all_tool_events, turn_count=turn,
    ))


def _premium_first(chain: list, force: bool, restricted: bool) -> list:
    """重试多次时按**价格从高到低**重排 —— 恢复时先试贵的。

    ``restricted`` = 用户点名了主力名单 —— 那就不动：主力不该因为"重试过两次"
    被悄悄换掉（同 `restrict_to_lineup` 的其余用法）。

    2026-09-12 改：以前是**按模型名子串**判 premium（含 `glm`/`opus` 就算）。那是个坏代理，
    而且坏得很具体 —— 价目表里**最便宜的** `glm-5.3-flash`(0.25 $/M) 名字带 "glm"
    会在重试时被提到最前，而 `deepseek-v4-pro`(1.848) 提不上来。现在读 `model_prices.json`
    的实价（那本来就是单价唯一真相源，见 [[qidian-model-pricing]]）。
    """
    if not (force and chain and not restricted):
        return chain
    try:
        from . import model_prices
        prices = model_prices.load_prices()
    except Exception:
        return chain        # 读不到价 → 保持原顺序，别瞎排

    def _key(a):
        p = prices.get(str(a.get("model", "")))
        # 没配价的排**最后**（不知道贵不贵 → 不优先）；同价保持原顺序（sorted 稳定）
        return -(p if isinstance(p, (int, float)) else float("-inf"))

    return sorted(chain, key=_key)


@timed(name="executor")
def run(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """纯执行: dispatch + validate, 返回 BatchOutput。

    修复 #7: 不调任何 tracker.transition/cas/create。调用方 (主线程) 负责状态机。
    修复 #5: 入口 _read(task.id) 重读, 不依赖传入的内存 Task 对象 (可能陈旧)。
    修复 #2: v3 路径用 commit_wt 拿到含改动的 commit, 再构造 MergeRequest。
    """
    # 修复 #5: 重读文件, 不信任传入的 task 内存对象
    fresh = tracker.read_task(task.id)
    if fresh is not None:
        task = fresh

    level = task.route_level
    route_gate = task.route_gate
    # 「路由未判定」—— 分类没判出来时 `route_gate` 是**折出来的 False**，
    # 不是"分类器说不用跑门"。带着它往下走，让 `validate` 能把这件事记进 unverified
    # （2026-09-20，见 `router.RouteResult` 的 docstring）。
    route_gate_unknown = bool(getattr(task, "route_gate_unknown", False))
    route_type = task.route_type
    # Step 4: 读取 layer→角色路由
    route_role = getattr(task, 'route_role', None) or ""

    feedback = ""
    last_validation = val_mod.ValidationReport(
        verdict="未知", action="abort",
        unverified=["dispatcher 未产出可校验结果"],
    )
    disp_result = None
    term_reason = "未执行"
    pending_merge_req = None
    all_tool_events: list[dict] = []  # 收集所有 turn 的工具调用事件
    final_turn = 0                     # 实际推理轮次
    qa_verdict = ""                    # worker 内 QA 门禁判定, 随 batch 带回给 finalize 复用
    qa_issues: list = []
    deadline_wrapup = False            # 执行器自己撞总预算收尾 → 别升级/别重试

    # method 必须透传：审查层靠它判这个 ref 能不能当 diff 基准。漏了它，
    # `_diff_base` 恒返回空串 → 审查五道检查一起短路（2026-09-11 外派评审抓到的 P0）。
    snap = _SnapProxy(ctx.snapshot_ref, method=ctx.snapshot_method)

    # ── 执行前钩子 ──
    pre_warnings = val_mod.pre_execution_hook(task.description, snap)
    if pre_warnings:
        for w in pre_warnings:
            witness.warn("exec", f"pre_hook: {w[:80]}"[:200])

    # 「阶段 → 模型」里实现阶段配的那份名单。**这里和下面 :350 的 dispatch 要各传一次** ——
    # 本处这次只决定"用哪个 agent 建 worktree / 熔断后切谁"，真正调模型的是 dispatch()，
    # 而它在 _dispatch_exec 里会**自己重新选一遍链**。只传一处 → 指定的主力只生效一半
    # （外层按 A 建 worktree，内层实际调 B）。
    from . import phase_models as pm_mod
    _proj = None
    if getattr(task, "project_id", ""):
        try:
            from . import project as proj_mod
            _proj = proj_mod.load(task.project_id)
        except Exception:
            _proj = None                     # 项目读不到就当没配，别把执行拖挂
    exec_lineup, exec_restrict = pm_mod.selection("executing", _proj)

    # 容灾: 获取 fallback 链, 当前 agent 失败自动切下一个
    # 如果任务已重试多次，强制优先用 premium 模型
    force_premium = getattr(ctx, 'retry_count', 0) >= 2
    fallback_chain = disp_mod.pick_agent_fallback_chain(
        agents, level, fallback_levels=["any"],
        project_lineup=exec_lineup, restrict_to_lineup=exec_restrict)
    fallback_chain = _premium_first(fallback_chain, force_premium, exec_restrict)
    tried_models: set[str] = set()

    # 修复 #1: 项目任务写进项目独立 repo, 独立任务写奇点仓库
    from . import project as proj_mod
    repo_root = proj_mod.repo_root_for(task)

    while True:
        if not fallback_chain:
            break
        agent_cfg = fallback_chain[0]
        level_max = config.DEFAULT_MAX_TURNS  # 打回上限；agent 的 max_turns 是模型推理轮数(openai_agent 内部用)，语义不同
        is_planner = agent_cfg.get("mode") == "planner"

        wt = _maybe_create_worktree(task.id, level, agent_cfg, ctx.snapshot_ref, repo_root=repo_root)
        cwd = str(wt.path) if wt else str(repo_root)  # ponytail: 无worktree时直接用仓库根

        # 修复 P1-1: worktree 生命周期对称。
        # try/finally 套在 while 迭代体内（非函数级）——fallback 切 agent 会重建 wt,
        # 每个 wt 必须在本迭代结束（return/break/异常）时清理；
        # 只有 retry 的 continue 复用同一 wt（不退出 try，不触发 finally）。
        try:
            if is_planner and wt:
                _lock_wt(wt)

            for turn in range(1, level_max + 1):
                # 表到点 ⇒ 这一轮**不发起**（见 `_budget_exhausted`）。放在最前面：
                # 心跳/取消/暂停都不必做，反正这一轮什么也不会发生。
                # ⚠️ 这里 break 出去时 `disp_result` 仍是**上一轮那份真结果** ——
                # 正是要它留到收尾 `return` 里，别被空壳顶掉。
                # ⚠️ 这里**只置 `deadline_wrapup`、不写 `term_reason`** —— 循环后面那段
                # `next_level is None` 会把 `term_reason` 整个覆盖成 `no_escalation_path`，
                # 写了也是死代码（2026-09-16 写测试时被自己的用例抓到）。
                # `deadline_wrapup` 才是真信号：`_run_with_retry` 靠它"别重试"。
                if _budget_exhausted(ctx):
                    deadline_wrapup = True
                    break
                final_turn = turn          # P3 修复: 失败兜底不再恒报 0 轮
                witness.heartbeat(task.id, level)

                # 检查人工取消标记
                cancelled = _check_cancelled(task, all_tool_events)
                if cancelled is not None:
                    return cancelled

                # 检查人工暂停标记 (GATE 人审)
                if not _check_paused(task):
                    # pause 期间被 cancel 了
                    return _check_cancelled(task, all_tool_events) or BatchOutput(
                        ok=False, task_id=task.id, term_reason="cancelled_during_pause",
                        validation=val_mod.ValidationReport(verdict="阻断", action="abort",
                            unverified=["暂停期间被取消"]),
                        tool_events=all_tool_events, turn_count=turn,
                    )

                effective_task = _build_effective_task(task, turn, feedback, is_planner,
                                                        tool_events=all_tool_events,
                                                        route_role=route_role)

                # dispatch **开始**就落一个时间戳 —— 被 900s 收割时，
                # "压根没发起过调用" 和 "发起了但没落账" 从此分得开（§59 那个边界）。
                _mark_dispatch_started(task.id)
                disp_result = disp_mod.dispatch(
                    effective_task, level, task.id, agents,
                    feedback=feedback, baseline_ref=ctx.snapshot_ref, cwd=cwd,
                    budget_s=_dispatch_budget_s(ctx),
                    project_lineup=exec_lineup, restrict_to_lineup=exec_restrict,
                    # 角色标：planner 拆的子任务带"执行阶段角色"，dispatch 靠它
                    # 把实现任务挡在委员会外面（描述里带架构词汇不等于要出架构）。
                    route_role=route_role,
                    # 阶段名：技能解析用它走"阶段级绑定"那条轴（换模型不丢技能）。
                    phase="executing",
                )
                exec_result = disp_result.executor_result

                # ── 收集工具调用事件 ──
                if exec_result and getattr(exec_result, 'tool_events', None):
                    all_tool_events.extend(exec_result.tool_events)

                # 累计用量落盘 —— 超时路径要靠它才知道这个任务烧了多少（§59）。
                # 放在这儿是因为 dispatch 刚回来、token_count 是现成的。
                if exec_result is not None:
                    _persist_partial_usage(
                        task.id, level, agent_cfg.get("model", "") if isinstance(agent_cfg, dict) else "",
                        getattr(exec_result, "token_count", 0) or 0,
                        tool_events=all_tool_events,
                        elapsed=getattr(exec_result, "elapsed", 0.0),
                        # 「返工了几轮」的两个坐标（见 `_persist_partial_usage` 里那段）
                        turn=turn, attempt=getattr(ctx, "retry_count", 0),
                    )

                # 收尾前**再查一次取消**。原来只在每轮开头查，于是超时（或人工取消）
                # 之后这一轮仍然会往下走完收尾，而那一堆活要碰 worktree —— 超时方
                # 已经把它撤了 ⇒ 刷一屏 `collect_changes: [Errno 2]`，从外面看
                # 像"任务还在跑"（§59 实测：超时 8 分钟后告警里还在报）。
                _cancelled_now = _check_cancelled(task, all_tool_events)
                if _cancelled_now is not None:
                    return _cancelled_now

                if not exec_result.success:
                    if getattr(exec_result, "error_kind", "") == "deadline":
                        # 执行器自己撞了总预算收尾。**不换模型** —— 换一个只会把剩下的
                        # 时间再烧一遍，烧完照样被 orchestrator 900s 无声收割，而这一份
                        # 已经拿到手的账（token/文件/轮次）也会跟着丢。直接收。
                        deadline_wrapup = True
                        last_validation = val_mod.ValidationReport(
                            verdict="未知", action="abort",
                            unverified=[f"执行器到达总预算主动收尾: {exec_result.error}"],
                            turns_used=turn,
                        )
                        break
                    # 容灾: 切下一个 agent
                    tried_models.add(agent_cfg.get("model", ""))
                    fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                    if fallback_chain:
                        witness.warn("exec", f"fallback: {agent_cfg.get('model','')}→{fallback_chain[0].get('model','')}"[:200])
                        break  # 跳出 turn loop, 用新 agent (finally 清理本 wt)
                    last_validation = val_mod.ValidationReport(
                        verdict="未知",
                        action="abort",
                        unverified=[f"executor 失败 (已试 {len(tried_models)} agent): {exec_result.error_kind}: {exec_result.error}"],
                        turns_used=turn,
                    )
                    break

                # planner 分解 / v3-v2 merge 处理
                # pending_merge_req_holder: 单元素 list, 让子函数能回填 v3 的 merge_request
                pending_merge_req_holder = [pending_merge_req]
                pm_signal = _process_planner_or_merge(
                    task, ctx, turn, level, is_planner, wt,
                    exec_result, disp_result, all_tool_events,
                    pending_merge_req_holder, repo_root=repo_root,
                )
                pending_merge_req = pending_merge_req_holder[0]
                if pm_signal is not None:
                    if isinstance(pm_signal, BatchOutput):
                        return pm_signal  # planner 分解成功
                    # v2 merge 冲突信号: ("merge_conflict", level, turn, disp_result, all_tool_events, reason)
                    if isinstance(pm_signal, tuple) and len(pm_signal) >= 6:
                        _, level, turn, disp_result, all_tool_events, reason = pm_signal
                    else:
                        last_validation = val_mod.ValidationReport(verdict="阻断", action="abort",
                            unverified=["内部错误: 意外的 _process_planner_or_merge 返回类型"])
                        break  # 跳出 for turn 循环，走 escalation 路径
                    last_validation = val_mod.ValidationReport(
                        verdict="阻断", action="abort",
                        unverified=[f"worktree merge 失败: {reason}"],
                        turns_used=turn,
                    )
                    term_reason = f"merge_conflict (level={level}, turn={turn})"
                    return BatchOutput(
                        ok=False, task_id=task.id, dispatch_result=disp_result,
                        term_reason=term_reason, validation=last_validation,
                        tool_events=all_tool_events, turn_count=turn,
                    )

                validation = val_mod.validate(
                    candidate=exec_result.raw_output,
                    gate_required=route_gate,
                    task_type=route_type,
                    changed_files=getattr(exec_result, 'changed_files', []),
                    snap=snap, turn=turn, max_turns=level_max,
                    cwd=cwd, gate_unknown=route_gate_unknown,
                    # 🔴 只读任务（`[只读]` 声明）的"零文件改动"是**预期**，不是"没产出"。
                    # 协议标记的单一出处是 `config.READONLY_TAG` —— 三个判官共用一份
                    # （2026-09-20 之前只有 supervisor 认它，验收层把这类任务判死）。
                    readonly=config.is_readonly_task(task.description),
                )
                # 补充质量信号
                try:
                    quality = val_mod.post_execution_hook(exec_result, snap)
                    validation.confidence = quality.get("confidence", 0.5)
                    validation.quality_signals = quality.get("quality_signals", {})
                except Exception as e:
                    # ⚠️ 原来这里写死 `{"warnings": [], "failure_kind": "ok", ...}` ——
                    # 把"质量钩子自己炸了"伪装成"没问题"：下游 398 行按
                    # `failure_kind != "ok"` 决定要不要给模型加失败反馈，这个假 ok
                    # 让钩子崩溃在整条链路上不留痕（D 核外派点名叫它编造的 0.5）。
                    witness.warn("exec", f"post_exec_hook_failed:{type(e).__name__}:{e}"[:200])
                    quality = {"warnings": [f"质量钩子异常（不得当通过处理）: {e}"],
                               "failure_kind": "hook_error",
                               "quality_signals": {}, "confidence": 0.5}

                # ── v2: independent tests + multi-model review ──
                changed = getattr(exec_result, 'changed_files', []) or []
                if changed:
                    from . import _review as rev_mod
                    rev_mod.run_post_exec_checks(
                        validation=validation, quality=quality,
                        exec_result=exec_result, task=task,
                        agent_cfg=agent_cfg, level=level, cwd=cwd,
                        changed=changed,
                        # 执行前快照的 ref 当 diff 基准。不传的话审查什么都看不到 ——
                        # worktree 里改动在 validate 之前就被 commit_wt 提交了。
                        base_ref=val_mod._diff_base(snap))

                # 审查结论接回决策: run_post_exec_checks 只改 quality["confidence"],
                # 而 _decide_cascade 读 validation.confidence (审查前就赋了值) → 惩罚传不到,
                # review_critical 仍可能被 cascade_accept (conf>=0.75) 放行合并。
                # 取二者较小值: 无发现时 quality 更高, min 取原值 → 行为不变。
                validation.confidence = min(
                    validation.confidence, quality.get("confidence", validation.confidence))

                last_validation = validation

                # ── QA 门禁 (supervisor): 硬证据失败 → 不合并 ──
                # 位置: merge_request 已构造、_decide_cascade 之前。清掉 merge_request
                # 后代码进不了主仓库 (v3 不 submit / v2 不 merge_back)。
                # 只拦 verdict=fail (硬证据); escalate/retry 是软信号, 不拦, 仅 SSE 通知 Owner。
                if pending_merge_req is not None:
                    try:
                        from .supervisor import our_side_stop_of, qa_context, supervise
                        _cons, _check = qa_context(task)
                        # 改动文件在 worktree (cwd) 里, 不在项目 repo 根 —— 传错根
                        # 会让 _check_artifact 的 py_compile/ruff 因 (root/f).exists()
                        # 为假而静默跳过 (端到端实测: 语法错误文件被放行合并)
                        sv = supervise(task.description, changed, _cons, _check,
                                       getattr(exec_result, 'raw_output', '') or '',
                                       task.id, repo_root=cwd,
                                       tests_result=(quality or {}).get("test_result"),
                                       # 让判据知道"这次是不是被我们掐断的"（审计 A4）
                                       our_side_stop=our_side_stop_of(exec_result))
                        qa_verdict = sv.verdict
                        qa_issues = list(sv.issues)
                        # "block" 必须和 "fail" 同等对待：supervise 在**模型隔离违规**
                        # （supervisor 与 implementer 同模型）时返回 "block"，注释写明是"硬锁"。
                        # 但消费端原来只认 "fail" / ("escalate","retry") —— "block" 两条都不中，
                        # 直接穿过去、**合并照走**，那个硬锁从来没生效过。
                        if sv.verdict in ("fail", "block"):
                            pending_merge_req = None
                            quality.setdefault("warnings", []).append(
                                ("QA 阻断: " if sv.verdict == "block" else "QA 硬证据失败: ")
                                + "; ".join(sv.issues[:2] or [getattr(sv, "reason", "")]))
                            quality["failure_kind"] = "qa_fail"
                            # 压低置信度, 否则 _decide_cascade 的 cascade_accept 会直接放行
                            validation.confidence = min(validation.confidence, 0.3)
                            validation.action = "retry" if turn < level_max else "abort"
                            validation.unverified.append(
                                "QA 硬证据失败 (未合并): " + "; ".join(sv.issues[:2]))
                        elif sv.verdict in ("escalate", "retry"):
                            _pending_sse_events.append({
                                "kind": "system",
                                "msg": f"[{tracker.short_id(task.id)}] QA 软信号 {sv.verdict} (不拦合并): "
                                       + "; ".join(sv.issues[:1]),
                                "ts": time.time(), "task_id": task.id,
                            })
                    except Exception as e:
                        witness.warn(task.id, f"qa_gate:{e}")

                cascade_action, payload = _decide_cascade(
                    task, level, turn, validation, disp_result, all_tool_events,
                    pending_merge_req, fallback_chain, tried_models, quality,
                )
                if cascade_action == "return":
                    payload.qa_verdict = qa_verdict
                    payload.qa_issues = qa_issues
                    return payload
                if cascade_action == "break":
                    # 低置信 cascade_skip: 标记 tried 后 break 升级
                    tried_models.add(agent_cfg.get("model", ""))
                    fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                    if fallback_chain:
                        witness.warn("exec", f"cascade_skip:{agent_cfg.get('model','')}→{fallback_chain[0].get('model','')} conf={validation.confidence:.2f}"[:200])
                    break  # 跳出 turn loop，用更好的模型 (finally 清理本 wt)
                # cascade_action == "continue": 中置信 retry
                feedback = payload
                continue

            next_level = disp_mod.escalate(level)
            if next_level is None:
                # escalate() **恒返回 None** —— `_ESCALATION` 是空表且全仓无人填，
                # 也就是说"升级到下一档"这条设计路径从未生效过。
                # 原来无论内层因为什么 break 到这里，终态一律写 escalation_exhausted，
                # 把真实原因（低置信 / cascade 跳过 / executor 全失败）从终态上抹掉了 ——
                # 排障时只看得到"升级用尽"，看不到"其实根本没有升级档"。
                # 带上最后一次校验的 action，让终态说真话。
                _why = getattr(last_validation, "action", "") or "unknown"
                term_reason = f"no_escalation_path (level={level}, last_action={_why})"
                break
            level = next_level
            # 升级后重建 fallback 链 (新层级的新 agent 列表)
            fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level)
            tried_models = set()
            feedback = ""
        finally:
            _cleanup_wt(wt)

    return BatchOutput(
        ok=False, task_id=task.id, dispatch_result=disp_result,
        # ⚠️ **`merge_request` 以前在这条 return 上是漏的** —— 而这条正是"没升级档 /
        # 兜底收尾"的公共出口。前面几轮辛苦 build 出来的合并请求到此**整份丢掉**：
        # `orchestrator` 只看 `batch.merge_request`（`orchestrator.py:494`，None 就不 submit），
        # 于是 worktree 里已 commit、已锚到 `refs/qidian/pending/<id>` 的产物**永远进不了合并队列**
        # —— 仓库看起来是空的，而活其实干完了（2026-09-16 真机坐实，见 OPEN.md 接手指针）。
        # 其它出口（pass / soft_quality_gate / merge_conflict）都带着它，只有这里漏了。
        merge_request=pending_merge_req,
        term_reason=term_reason, validation=last_validation,
        tool_events=all_tool_events, turn_count=final_turn,
        qa_verdict=qa_verdict, qa_issues=qa_issues,
        deadline_wrapup=deadline_wrapup,
    )

def _batch_evidence(b) -> int:
    """这一批交回来的东西**有多硬** —— 数字越大越硬，`0` = 空壳。

    **三种事实不是一个档次的，分成三档**（2026-09-20 真机坐实，见下）：

    | 档 | 判据 | 含义 |
    |---|---|---|
    | 3 | `merge_request is not None` | worktree 里已 commit 并锚到 `refs/qidian/pending/` —— **磁盘上最硬的事实** |
    | 2 | `changed_files` 非空 | 交了文件，但还没构成合并请求 |
    | 1 | `token_count > 0` | **只花过钱**，一样产物都没有 |
    | 0 | 以上都不是 | 连调用都没发起过的空壳 |

    🔴 **为什么要分档（原来是个 bool，真机吃了大亏）**：`round-20260920b` 的
    T3/T4/T6 三个任务，**产物全都在**（pending ref 上分别是 +1008 / +251 / +1178 行），
    最后却判「无文件改动」、5654 行**进仓 0 行**。形状是：

      ① 某一轮真干完，`_anchor_ref` + `_build_merge_request` 都做了（ref 就是那时候打的）；
      ② 表到点，`run()` 又起了一轮 —— 这一轮**烧了 4773~4920 token、0 个文件**，
         属于第 1 档；而上一轮是第 3 档；
      ③ 旧判据 `has_facts` 是个 bool ⇒ 档 1 也返回 True ⇒ **档 1 顶掉了档 3**；
      ④ `_run_with_retry` 交回空壳 ⇒ `finalize` 读到 `changed_files=[]` ⇒ 判词「无文件改动」
         ⇒ `orchestrator` 只看 `batch.merge_request`，None ⇒ **产物永远进不了合并队列**。

    ⇒ 判据从「**有没有**事实」改成「**哪个更硬**」：**弱的不许顶掉强的**。

    ⚠️ `token_count` 的 `None` 是"**不知道**"（§59）不是"没花钱"，所以按 `or 0` 折成 0 ——
    宁可把"不知道花没花钱"的那一批当成档 0（更保守的一侧）。

    ⚠️ **`turn_count` 仍然不能当判据**（原注释保留）：预算 6 秒那次**确实发起了**一轮，
    `turn_count` 会是 1，而它交回 0 文件 0 token。

    ⚠️ **`tool_events` 仍然不进判据**：只读文件、改了又回滚的那些轮确实"发生了点什么"，
    但拿它去换掉一份**真交了文件**的账，是净亏。
    """
    if getattr(b, "merge_request", None) is not None:
        return 3                          # worktree 里已 commit + 已锚 —— 最硬的事实
    er = getattr(getattr(b, "dispatch_result", None), "executor_result", None)
    if er is None:
        return 0
    if getattr(er, "changed_files", None):
        return 2
    return 1 if int(getattr(er, "token_count", 0) or 0) > 0 else 0


def _batch_has_facts(b) -> bool:
    """这一批里有没有**磁盘上真发生过的事实**（= 档位 > 0）。

    ⚠️ **"要不要用这一批"不该再调它** —— 它是个 bool，答不了「两份都有事实时留哪份」，
    而 2026-09-20 真机栽的正是那个问题（档 1 顶掉了档 3）。要比较就用 `_batch_evidence`。
    留着它是给"只想问有没有"的地方（以及既有测试）用的。
    """
    return _batch_evidence(b) > 0


def _run_with_retry(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """worker 线程入口: 纯执行 + 重试。

    修复 #7: 不写 tracker。重试时回传 retry 信号, 主线程决定是否再派发。
    本函数在 worker 线程跑, 只调 run() (纯执行), 不碰 tracker。
    """
    retry = 0
    prev_batch = None
    while retry <= task.max_retries:
        # 表到点 ⇒ **别再开新一轮**（见 `_budget_exhausted`）。新一轮的 turn 1 什么都不会
        # 发起，交回的 BatchOutput 里一条事实都没有（`dispatch_result=None`：0 文件 /
        # 0 token / 没有 merge_request），而 `finalize` 把它当**任务的最终结论** ⇒
        # 上一轮真干出来的账整份丢掉（2026-09-16 真机：判「无文件改动」，产物在 pending ref 上）。
        # ⇒ 手里有上一轮的结果就交它回去 —— 那才是磁盘上真发生过的事实。
        # ⚠️ 第一轮没有"上一轮"（`prev_batch is None`）⇒ 照常进 run()：那种情形下
        # 确实什么都还没发生，run() 里那道 guard 会立刻收尾，不会白烧一次调用。
        if prev_batch is not None and _budget_exhausted(ctx):
            return prev_batch
        ctx.retry_count = retry  # ponytail: 传入 run() 用于 force_premium 判定
        batch = run(task, ctx, agents)
        # 🔴 **空壳不许顶掉真结果**（2026-09-18，和上面那道 guard 是**同一件事的另一半**）。
        #
        # 上面那道 guard 拦的是「**下一轮**什么都不发起」；这一句拦的是
        # 「**这一轮**发起了、但什么都没干出来」—— 两者交回的空壳长得一模一样，
        # 而 `prev_batch` 原来是**无条件**赋值的 ⇒ 空壳一进来就把真账换走了，
        # 下一轮 guard 再忠实地把**空壳**交出去。**下半句修了、上半句没修。**
        #
        # 真机形状（2026-09-18 §77.8 复查）：预算是 `6s` 这种**正数**时
        # `_budget_exhausted` 不拦（它只拦 `≤0`）⇒ 照常发起 ⇒ 活着 39 毫秒、
        # 交回 0 文件 0 token。第 1 轮那份真干出来的账就是这么丢的。
        # ⇒ 这就是"太小也是没有"那个区间的**伤害面**：不去猜"多小算小"（没数据，见 OPEN.md），
        #    而是让"小到白跑"这件事**不再有后果** —— 浪费几秒可以接受，丢掉真账不行。
        #
        # 🔴 **2026-09-20 真机：bool 判据不够，要分档**（见 `_batch_evidence`）。
        # 档 1（只花过钱、0 产物）原来也返回 True ⇒ 它把档 3（带 `merge_request`、
        # pending ref 上躺着 1008 行）顶掉了 ⇒ 判「无文件改动」⇒ 产物进不了合并队列。
        # ⇒ 只在"这一批**不比手里那份软**"时才替换。
        if prev_batch is None or _batch_evidence(batch) >= _batch_evidence(prev_batch):
            prev_batch = batch

        # ── 执行后钩子 ──
        try:
            exec_result = batch.dispatch_result.executor_result if batch.dispatch_result else None
            if exec_result:
                snap = snap_mod.Snapshot(id=task.id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                post_warnings = val_mod.post_execution_hook(exec_result, snap)
                if post_warnings and post_warnings.get("warnings"):
                    batch.term_reason += f"; post_hook: {', '.join(post_warnings['warnings'])}"
        except Exception as e:
            try:
                witness.warn(task.id, f"post_hook:{e}")
            except Exception:
                pass

        if batch.ok or batch.planner_decomposed:
            return batch
        if batch.term_reason.startswith(("merge_conflict", "soft_quality_gate")):
            return batch
        if batch.term_reason.startswith("cancelled"):
            # 人工取消不是"失败可重试"：`_check_cancelled` 命中标记时**已经把标记删了**，
            # 于是重试那一轮 `_check_cancelled` 查不到东西、任务照常跑满 3 轮
            # （用户点了取消，token 继续烧）。取消是终态意图，早退，交给 finalize 收尾。
            # ⚠️ 前缀是 `"cancelled"` 不是 `"cancelled_by_user"` —— 还有
            # `cancelled_during_pause`（`:540`）那一族，漏了它等于同一个洞换个字符串
            # （2026-09-14，逆向审抓到、我核过）。
            return batch
        if getattr(batch, "deadline_wrapup", False):
            # 撞总预算收尾。重试 = 把剩下的时间再烧一遍，而且下次多半是被 orchestrator
            # 900s 无声收割 —— 这一份已经拿到手的账（token/文件）也跟着丢。收下就走。
            #
            # 🔴 **"收下就走"≠"收下这一份"**（2026-09-20 真机补的第二半）。
            # 上面那行 `prev_batch = ...` 修的是"**留**哪一份"，这句是"**交**哪一份" ——
            # 原来无条件交 `batch`，于是即使 `prev_batch` 攥着带 `merge_request` 的真产物，
            # 也**永远到不了 `orchestrator` 那句 `if batch.merge_request: mq.submit(...)`**。
            # 真机形状：T3/T4/T6 三条**全都**是从这儿原样交回空壳的（trace 里
            # `changed_files=[]` + `token_count>0` + `unverified` 是 deadline 那句，三样同时在场）。
            # ⇒ 手里那份更硬就交手里那份；它一样会让 `_run_with_retry` 立刻返回（不重试）。
            if prev_batch is not None and _batch_evidence(prev_batch) > _batch_evidence(batch):
                return prev_batch
            return batch

        retry += 1
        if retry > task.max_retries:
            # 🔴 **重试用尽 = "不再试了"，不是"交回最后那一份"**（2026-09-25）。
            # 紧邻上面 deadline 那一支在 2026-09-20 已改成"手里那份更硬就交手里那份"，
            # **这一支漏了** —— 同一个形状在这里原样重演：前几轮真写出文件、建好
            # `merge_request`，**最后一轮模型只想不写**（真机上这是常态，
            # `~/OPEN.md` 记着"15 个失败任务全败在没产出"）⇒ 交回空壳
            # ⇒ `finalize` 读成「无文件改动」⇒ `orchestrator` 那句
            # `if batch.merge_request: mq.submit(...)` 永远见不到产物。
            # 判据用**同一把尺**（`_batch_evidence`），别在这儿另发明一个。
            if prev_batch is not None and _batch_evidence(prev_batch) > _batch_evidence(batch):
                return prev_batch
            return batch

        # 重试 (v2: 主仓库可能有 merge 残留; v3: worktree 已在 run() 内部清理)
        if ctx.merge_queue is None:
            # v2: 主仓库 rollback 到快照基线 (只在主线程, 不并发)
            try:
                snap = snap_mod.Snapshot(id=ctx.batch_id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                from . import project as proj_mod
                snap_mod.rollback(snap, repo_root=proj_mod.repo_root_for(task))
            except Exception as e:
                witness.warn('exec', f'{e}')
        # v3: 不碰 PROJECT_ROOT —— 主仓库未动, worktree 已由 run() 内部 _cleanup_wt 清理
        # retry_count 由主线程在回收时按需写; worker 不写

    return batch


def _save_planner_patch(task_id: str, content: str) -> None:
    patch_path = config.PATCH_DIR / f"{task_id}_plan.md"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(content, encoding="utf-8")


def _read_planner_patch(task_id: str) -> str | None:
    """读 D 层的分析方案 patch，用于创建 E+ 修复任务。"""
    patch_path = config.PATCH_DIR / f"{task_id}_plan.md"
    if not patch_path.exists():
        return None
    try:
        return patch_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _save_trace(task, route, snap, disp_result, validation, rolled_back: bool,
                pre_search_skipped: bool = False, pre_search_reason: str = "",
                pre_search_top_decisions: list = None, pre_search_memory: dict = None,
                tool_events: list = None) -> None:
    """写 trace。

    `tool_events`：**没有 `disp_result` 但手里有事件时的兜底**（2026-09-13）。
    本函数的唯一输入本来是 `disp_result`，可**取消路径**（`_check_cancelled`）造的
    BatchOutput 是"有 tool_events、没有 dispatch_result" ⇒ 事件攥着也进不了 trace。
    """
    # ponytail: 幂等保护 — 终态路径互斥但防误调用
    trace_path = config.TRACE_DIR / f"{task.id}.json"
    if trace_path.exists():
        return  # 已写过的 trace 不覆盖, 避免不一致
    try:
        report = nj_mod.build_report(
            task=task.description, route=route,
            executor_result=disp_result.executor_result if disp_result else None,
            validation=validation, snapshot=snap, rolled_back=rolled_back,
            pre_search_skipped=pre_search_skipped,
            pre_search_reason=pre_search_reason,
            pre_search_top_decisions=pre_search_top_decisions,
            pre_search_memory=pre_search_memory,
            tool_events=tool_events,
        )
        nj_mod.save_trace(report, task.id)
    except Exception as e:
        witness.warn('exec', f'{e}')

    # ── MAGMA 多图记忆索引 + 状态更新 ──
    try:
        changed_files = disp_result.executor_result.changed_files if disp_result else []
        # raw_output = 这次实际产出的全文，一并存进记忆供 depth>=3 展开读。
        # 以前只存 task.description，于是"上次到底怎么做的"从来没进过记忆
        # （见 docs/经验分层-STAIR借鉴-20260912.md）。
        _er = disp_result.executor_result if disp_result else None
        _traj = (_er.raw_output or "") if _er else ""
        # 工具调用顺序 —— 只存事实（哪个工具、多久），"定位/改动/验证"三段
        # 由 _memory_graph.split_stages 在读的时候现算（不落 derived 值，§34）。
        # 只取 tool:done（带 elapsed），start 那条是重复的。
        _seq = [{"tool": e.get("tool", ""), "elapsed": e.get("elapsed", 0.0)}
                for e in ((getattr(_er, "tool_events", None) or []) if _er else [])
                if e.get("kind") == "tool:done"]
        mem_mod.index_task(
            task_id=task.id,
            description=task.description,
            changed_files=changed_files,
            depends_on=task.depends_on,
            created_at=task.created_at,
            trajectory=_traj,
            tool_seq=_seq,
        )
        # 补充事件属性: 终态 + route info
        # route_level 取自 task 而非 route —— RouteResult 没有 level 字段（两档制后
        # E/E+/D 已废弃，见 pre_search.apply_escalation 的注释）。曾经读 route.level，
        # 每任务必抛 AttributeError 被下面那层 except 吞掉，于是这条 update_attrs 从没执行过。
        final_status = "rolled_back" if rolled_back else task.status.value
        mem_mod.update_attrs(task.id,
            status=final_status,
            route_level=task.route_level,
            route_type=route.task_type if route else "",
        )
    except Exception as e:
        witness.warn('exec', f'{e}')

    # ── 模型范围纪律：改了"声明范围外"的文件就记一笔 ──
    # 这张表决定融合时**选谁定稿**（`execution_judge._pick_writer`），而实测定稿人
    # 是"乘法器"还是"过滤器"直接决定产物的范围纪律。原来只有人手动跑的离线脚本
    # （`tests/integration/coverage_audit.py`）会写它 → 表常年是旧的（2026-09-12 实测
    # 停在 9/10，且缺了当前主力模型 deepseek-flash）。
    # ⚠️ 拿不到"声明范围"就**不记** —— 记 0 等于编造"这次很干净"（见该模块头）。
    try:
        from singularity.scheduler import _model_discipline as _md
        _model = (disp_result.agent_cfg or {}).get("model", "") if disp_result else ""
        if _model:
            _md.record_scope(_model, changed_files, _declared_files_for(task))
    except Exception as _e:
        witness.warn('exec', f'discipline:{type(_e).__name__}'[:80])


def _declared_files_for(task) -> list[str]:
    """任务在架构里声明的 `estimated_files`；取不到返回 []（= **没有尺子**）。

    认领规则**调 `supervisor.task_def_matches`** —— 原来这里手抄了一份"和 `qa_context`
    一致"的匹配（`if str(tdef.get("title","")) in desc or str(tdef.get("id","")) in desc`），
    而**手抄的一致迟早会漂**：裸子串判 id 让 `T1` 命中 `[T11]`，这条又是"取第一个匹配就
    返回" ⇒ **拿到的是 T1 的文件清单**（2026-09-25，Qoder 外派审查 #7）。理由写在那边。

    实测探路2 的架构任务**全都没给这个字段** → 这条路的覆盖率取决于架构师给不给，
    所以"为什么没记"要能查得到，而不是默默记成 0 违例。
    """
    pid = getattr(task, "project_id", "") or ""
    if not pid:
        return []
    try:
        from . import project as proj_mod
        from .supervisor import task_def_matches  # 认领规则只留一份，别手抄
        proj = proj_mod.load(pid)
        desc = getattr(task, "description", "") or ""
        for tdef in ((getattr(proj, "architecture", None) or {}).get("tasks") or []):
            if not isinstance(tdef, dict):
                continue
            if task_def_matches(tdef, desc):
                f = tdef.get("estimated_files")
                return [str(x) for x in f] if isinstance(f, list) else []
    except Exception:
        pass
    return []


def _safe_dep_list(v):
    """depends_on_local_id: int or list[int] → list[int]."""
    if isinstance(v, int):
        return [v]
    if isinstance(v, list):
        return v
    return []


def decompose(planner_raw_output: str) -> list[dict]:
    """解析 planner stdout 里的 ```json 子任务块。

    返回 [{desc, suggested_level, depends_on_local_id}, ...]。
    无 JSON 块或解析失败 → [] (当普通方案, 不分解)。
    """
    import re as _re
    # 抓 ```json ... ``` 块
    m = _re.search(r"```json\s*\n(.*?)\n```", planner_raw_output, _re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    # 校验每条结构
    subtasks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if "desc" not in item:
            continue
        subtasks.append({
            "desc": str(item["desc"]),
            "suggested_level": str(item.get("suggested_level", "any")),
            "depends_on_local_id": _safe_dep_list(item.get("depends_on_local_id", [])),
        })
    return subtasks


