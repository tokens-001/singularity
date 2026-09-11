__all__ = ['_build_synthesis_prompt', '_dispatch_committee', '_run_executor', 'dispatch']

from singularity.scheduler.dispatcher import (
    load_agents, _ensure_agent_type, pick_agent_fallback_chain,
    agent_api_available, _build_agent_from_registry, DispatchResult,
    _EXECUTOR_BY_TYPE,
)
from singularity.scheduler._dispatch_skills import (
    _load_skills_for_agent, _load_mcp_for_agent, _make_permission_checker,
)
from singularity.scheduler import tracker, config
from singularity.scheduler.tracker import TaskStatus
# `BaseExecutor` 只用在下面那句局部变量注解上（`executor: BaseExecutor = …`）。
# 局部注解**运行时不求值**，所以少这个 import 不会炸 —— 但 F821 会一直报，
# 把"真有未定义名"的信号淹没掉。补上，让这条检查能当守卫用。
from singularity.scheduler.executors.base import BaseExecutor
from singularity.scheduler import witness
from singularity.scheduler.log import timed
from singularity.scheduler._io import apply_json_patch, _parse_patch_ops
from singularity.scheduler import model_registry
from singularity.scheduler import _model_breaker
import json, os, time, logging, threading

# ── 委员会收集初稿的时间预算 ──
# 单次模型调用本身有上限（claude-cli 300s / openai-agent 240s），所以一波的耗时
# 取决于最慢的那个模型。把波超时调小只会让慢模型白跑——输出被丢弃、token 照花。
# 它决定"何时去读已完成的结果"。**2026-09-11 修正**：原文说"调小它救不了总耗时，
# 因为调用点用 with ThreadPoolExecutor(...) 退出时 shutdown(wait=True) 会 join"——
# 现在调用点已改成显式 shutdown(wait=False)（见下方 _dispatch_committee），
# 所以这个 timeout 现在**真的是时限**了：到点就带着已完成的那部分返回，
# 没跑完的线程留在后台（不 join），不再拖住整条架构阶段。
_WAVE_TIMEOUT = float(os.environ.get("QIDIAN_DEBATE_TIMEOUT", "300"))


def _impl_role_veto(route_role: str) -> bool:
    """这个 route_role 是不是"执行阶段的角色"（= 这是实现活儿，不是架构设计）。

    不写死 `"implementer"`：planner 打的标是
    `get_phase_role(Phase.EXECUTING) or "implementer"`（_workflow_phases:303 /
    orchestrator:417），用户在界面上改过执行角色的话，写死就对不上了。
    取不到角色表时退回字面量，宁可多挡也不要放它进委员会。
    """
    if not route_role:
        return False
    try:
        from .project import Phase
        from .roles import get_phase_role
        return route_role == (get_phase_role(Phase.EXECUTING) or "implementer")
    except Exception:
        return route_role == "implementer"


def _committee_allowed(task: str, chain: list, route_role: str,
                       allow_committee: bool) -> bool:
    """委员会开不开 —— 四个条件全过才开。

    抽成纯函数（而不是把 `allow_committee` 塞进原来那个 if）是为了能单测：
    贵的那一半可以用**一个普通 list 当 chain** 验，不用造 agents dict、不用打桩。

    ``allow_committee=False`` = 项目被判为轻量（见 `project.resolve_flow`）。
    默认一路传 True —— **拿不准就开委员会**（防御模式 §47 fail-closed）。
    """
    from .execution_judge import _is_architecture_task   # 与 dispatch 内部同一个延迟导入
    return bool(
        allow_committee
        and len(chain) >= 2
        and _is_architecture_task(task)
        and not _impl_role_veto(route_role)
    )


@timed(name="dispatcher")
def dispatch(
    task: str,
    level: str,
    task_id: str,
    agents: dict,
    feedback: str = "",
    baseline_ref: str = "",
    cwd: str = "",
    project_lineup: dict[str, list[str]] = None,
    restrict_to_lineup: bool = False,
    route_role: str = "",
    phase: str = "",
    no_tools: bool = False,
    project_id: str = "",
    allow_committee: bool = True,
) -> DispatchResult:
    """选 executor 并执行。架构任务: 委员会并行→合成; 其他: 单模型 fallback 链。

    ``restrict_to_lineup=True`` 时 lineup 就是全部候选（委员会席位靠它才限制得住）。

    ``route_role`` 是"这活儿是谁的角色"，用来挡委员会误入（见下面的守卫）。
    ``phase`` 是阶段名（researching/planning/executing/…），用来解析**阶段级**技能绑定
    （见 `skill_loader.get_agent_skills` 的两条轴）。留空 = 只用模型级绑定。

    ``no_tools`` 给"产出就是一段 JSON、不该碰磁盘"的阶段（调研 / 架构）。不给的话
    模型会把它当实现任务干：2026-09-11 实测调研员在**项目仓库里把整个项目实现完了**
    （wc_lite.py + 测试 + 真跑了一遍），5 个工具轮次耗尽后执行器只回一句
    "(达到最大工具轮次, 已产出文件)" —— 报告解析失败，GATE1 无物可审，这轮白烧。
    委员会那条路一直自带禁工具（`_run_no_tools`），这里补的是**单模型**那条。

    ``allow_committee`` 默认 True = **拿不准就开委员会**（防御模式 §47 fail-closed）。
    只有项目被判为轻量（`project.resolve_flow`）时才传 False，见 `_committee_allowed`。
    """
    chain = pick_agent_fallback_chain(agents, level, project_lineup=project_lineup,
                                      restrict_to_lineup=restrict_to_lineup)
    if not chain:
        raise RuntimeError(f"无可用 {level} 层 agent")
    # 冷启动先验: 任务关键词匹配模型 strengths, 擅长的模型排到链首。
    # 受限时跳过 —— 用户点名了席位，再按关键词重排同样是"界面点 A、实际调 B"。
    if not restrict_to_lineup:
        chain = _prefer_by_strengths(task, chain)

    # ── 架构任务: 委员会模式 (多模型并行 → fuse_architecture_v2 合成) ──
    # 仅架构/系统设计类任务走 3 模型碰撞, research/QA/安全/实现 单模型即可。
    #
    # 两道条件缺一不可。**只有关键词那道会误伤**：planner 拆出来的子任务描述
    # 是从架构 JSON 的 title/desc 抄的，天然继承「技术栈 / 模块划分」这些词，
    # 于是实现任务也被送进委员会 —— 而委员会是 no_tools 路径，跑几波拿回来的
    # 是一份架构 JSON 而不是代码。所以再加一道角色否决：执行阶段的角色
    # （planner 给每个子任务打的 route_role，见 _workflow_phases:303 / orchestrator:417）
    # 明确说"这是实现活儿"时不进委员会。
    #
    # 架构阶段自己不受影响：它走 `_safe_dispatch(...)`，**不带 route_role**（默认 ""）。
    # 用户手打的独立架构任务同理 —— 没有角色标，关键词判据照旧生效。
    if _committee_allowed(task, chain, route_role, allow_committee):
        return _dispatch_committee(task, level, task_id, agents, chain, feedback,
                                   baseline_ref, cwd, project_id=project_id)

    # ── 单模型 fallback 链 ──
    last_error = ""
    for attempt, agent_cfg in enumerate(chain[:3]):
        agent_cfg = _ensure_agent_type(agent_cfg)
        etype = agent_cfg.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if not executor_cls:
            last_error = f"未知 executor type: {etype}"
            continue

        if no_tools:
            # 与 `_run_no_tools` 同一条禁令、同一个告警：claude-cli 这类自带工具的执行器
            # 禁不掉 —— 让它在 trace 里可见，而不是假装禁住了。
            if not getattr(executor_cls, "honors_no_tools", False):
                witness.warn("dispatcher", f"no_tools_not_enforced:{etype}"[:80])
            agent_cfg = {**agent_cfg, "no_tools": True}

        full_task = task
        if feedback:
            full_task = (
                f"{task}\n\n"
                f"---\n[上一轮校验反馈, 请据此修正]\n{feedback}"
            )

        try:
            result = _run_executor(
                executor_cls, agent_cfg, full_task, task_id, level,
                baseline_ref=baseline_ref, cwd=cwd, phase=phase,
            )
            if result and result.raw_output:
                _model_breaker.record_success(agent_cfg.get("model", ""))
                return DispatchResult(
                    level=level, agent_cfg=agent_cfg,
                    executor_result=result, attempts=attempt + 1,
                )
            exec_error = getattr(result, 'error', '') if result else 'no result'
            last_error = f"{agent_cfg.get('model', '?')}: 空输出" + (f" [{exec_error}]" if exec_error else "")
            _model_breaker.record_failure(agent_cfg.get("model", ""))
        except Exception as e:
            last_error = f"{agent_cfg.get('model', '?')}: {type(e).__name__}: {e}"[:200]
            _model_breaker.record_failure(agent_cfg.get("model", ""))

    raise RuntimeError(f"{level} 层所有 agent 均失败: {last_error}")


def _run_executor(executor_cls, agent_cfg: dict, full_task: str, task_id: str,
                  level: str, baseline_ref: str = "", cwd: str = "", phase: str = ""):
    """构建 executor 并执行。"""
    skill_tools, skill_prompt, skills = _load_skills_for_agent(
        level, agent_cfg.get("model", ""), task_desc=full_task, phase=phase)
    mcp_tools, mcp_executor = _load_mcp_for_agent()
    perm_checker = _make_permission_checker()

    executor: BaseExecutor = executor_cls(
        agent_cfg, full_task, task_id, baseline_ref=baseline_ref, cwd=cwd,
        agent_level=level,
        skills=skills, skill_tools=skill_tools, skill_prompt=skill_prompt,
        mcp_tools=mcp_tools, mcp_executor=mcp_executor,
        permission_checker=perm_checker,
    )
    return executor.run()


def _run_no_tools(agent_cfg: dict, prompt: str, tag: str, level: str,
                  baseline_ref: str = "", cwd: str = "") -> tuple[str, int, float] | None:
    """跑一个禁工具的单模型调用。

    返回 `(raw_output, token_count, elapsed)` 或 None。带上用量是必须的：
    原来只回 raw_output，调用方拿不到 token，架构阶段（委员会 + 融合）
    就成了唯一**不进 token 账**的阶段（见 _FusionResult 的注释）。
    """
    agent_cfg = _ensure_agent_type(agent_cfg)
    agent_cfg = {**agent_cfg, "no_tools": True}
    etype = agent_cfg.get("type", "claude-cli")
    executor_cls = _EXECUTOR_BY_TYPE.get(etype)
    if not executor_cls:
        return None
    # 禁工具是委员会的前提（纯文本出方案，别改磁盘）。claude-cli 这类自带工具的执行器
    # 禁不掉 —— 告警让它在 trace 里可见，而不是假装禁住了。
    if not getattr(executor_cls, "honors_no_tools", False):
        witness.warn("dispatcher", f"no_tools_not_enforced:{etype}"[:80])
    try:
        result = _run_executor(executor_cls, agent_cfg, prompt, tag, level,
                               baseline_ref=baseline_ref, cwd=cwd)
    except Exception as e:
        # 以前这里静默 return None —— 委员会里模型失败完全看不见。
        # 实测 3 家阵容有 2 家无声无息没产出，只能靠猜（超时？空输出？）。
        witness.warn("dispatcher", f"no_tools_fail:{tag}:{type(e).__name__}"[:80])
        return None
    if result and result.raw_output:
        return (result.raw_output,
                int(getattr(result, "token_count", 0) or 0),
                float(getattr(result, "elapsed", 0.0) or 0.0))
    err = getattr(result, "error", "") if result else "no result"
    witness.warn("dispatcher", f"no_tools_empty:{tag}:{err}"[:80])
    return None


# 任务关键词 → strengths 能力标签 (benchmark/手填的 strengths 作冷启动先验)
_STRENGTH_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("sql", "数据库", "查询", "query"), "数据查询"),
    (("重构", "refactor", "多文件"), "多文件重构"),
    (("长程", "多步", "多阶段"), "长程任务"),
]


def _strength_label_for(task: str) -> str:
    """从任务描述提取匹配的 strengths 标签, 无匹配返回空串。"""
    t = (task or "").lower()
    for kws, label in _STRENGTH_KEYWORDS:
        if any(k in t for k in kws):
            return label
    return ""


def _prefer_by_strengths(task: str, chain: list[dict]) -> list[dict]:
    """strengths 匹配重排: 擅长该任务能力的模型排到链首(冷启动先验)。"""
    label = _strength_label_for(task)
    if not label or len(chain) <= 1:
        return chain

    def _matches(a: dict) -> bool:
        m = model_registry.get(a.get("model", ""))
        return bool(m and label in (m.strengths or []))

    matched = [a for a in chain if _matches(a)]
    rest = [a for a in chain if not _matches(a)]
    return matched + rest if matched else chain




def _dispatch_committee(task: str, level: str, task_id: str, agents: dict,
                        chain: list[dict], feedback: str = "",
                        baseline_ref: str = "", cwd: str = "",
                        project_id: str = "") -> DispatchResult:
    """多模型委员会: 所有可用D模型并行产出→合成。"""
    import concurrent.futures

    # 席位视角: 默认关闭 —— A/B 盲评证伪（有视角 31 vs 无视角 32，略输）：
    # 一句"你关注风险/创新"的提示词 = 伪碰撞，不产生真实差异。
    # 真正有效的是辩论轮（盲评 +3~5 分），别把两者搞混。
    # QIDIAN_COMMITTEE_PERSPECTIVE=1 可重新打开（保留做后续 A/B）。
    if os.environ.get("QIDIAN_COMMITTEE_PERSPECTIVE") == "1":
        _PERSPECTIVES = [
            "你关注: 风险点、边界条件、回滚策略。方案必须稳健,不能炸。",
            "你关注: 有没有完全不同的思路?业界最新实践是什么?大胆提替代方案。",
            "你关注: 这方案能落地吗?需要多少个文件?现有代码风格兼容吗?复杂度实际是多少?",
            "你关注: 和现有架构的一致性。不要引入不兼容的变更。",
        ]
    else:
        _PERSPECTIVES = [None, None, None, None]

    # 并行派发初稿 (禁工具, 直接输出 JSON 方案)
    outputs = []
    member_tokens = 0            # 委员会成员的实际用量，回填给 _FusionResult
    member_elapsed = 0.0
    # 按**成员**留一份用量。合成一个总数再配一个 "fusion(a,b)" 的合成名字的话，
    # 计价表里查不到这个名字 → 最贵的架构阶段记了账却算不出钱
    # （2026-09-11 探路轮实测：项目 cost 恒 $0.0000）。
    # per-member 的 token 本来就在手上（_run_no_tools 会回传），以前直接扔了。
    member_usage: list[dict] = []
    # 不能用 `with ThreadPoolExecutor(...)`: 退出时 shutdown(wait=True) 会去 join，
    # `_WAVE_TIMEOUT` 就只是个"延迟判定"而不是时限 —— 某个模型调用挂死就把整条
    # 架构阶段拖住（实测 A/B 探针三次这样卡住）。显式 shutdown(wait=False)。
    _ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chain), 4))
    try:
        futures = {}
        for i, a in enumerate(chain):
            full_task = task
            if feedback:
                full_task = f"{task}\n\n---\n[上一轮校验反馈]\n{feedback}"
            perspective = _PERSPECTIVES[i % len(_PERSPECTIVES)]
            if perspective:
                full_task = f"{full_task}\n\n[你的视角] {perspective}"
            futures[_ex.submit(_run_no_tools, a, full_task,
                               f"{task_id}_{a.get('model','?')[:8]}",
                               level, baseline_ref, cwd)] = a
        # 等待最多 _WAVE_TIMEOUT 收集任意数量的完成结果
        done, _ = concurrent.futures.wait(futures, timeout=_WAVE_TIMEOUT, return_when='ALL_COMPLETED')
        for fut in done:
            agent_cfg = futures[fut]
            try:
                got = fut.result()
                if got:
                    raw, _tk, _el = got
                    outputs.append((agent_cfg.get("model", "?"), raw))
                    member_tokens += _tk
                    member_elapsed += _el
                    member_usage.append({
                        "model": agent_cfg.get("model", "?"),
                        "tokens": int(_tk or 0),
                        "elapsed": float(_el or 0.0),
                    })
            except Exception:
                pass  # 单个模型失败不阻断委员会
    finally:
        _ex.shutdown(wait=False)   # 不 join：挂死的调用不能拖住整条流水线

    # 部分模型没产出 → 告警。否则委员会"3 家碰撞"实际只有 1 家，外面完全看不出来
    if len(outputs) < len(chain):
        got = {m for m, _ in outputs}
        miss = [a.get("model", "?") for a in chain if a.get("model") not in got]
        witness.warn("dispatcher",
                     f"committee_partial:{len(outputs)}/{len(chain)} 缺:{','.join(miss)}"[:80])

    if not outputs:
        raise RuntimeError("委员会所有模型均无产出")

    if len(outputs) == 1:
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        # 同样要带上用量：这条路径原来连 token_count 都没有，整个架构阶段不进账。
        return DispatchResult(
            level=level,
            agent_cfg=chain[0],
            executor_result=ExecutorResult(success=True, raw_output=raw,
                                           token_count=member_tokens,
                                           elapsed=member_elapsed),
            attempts=1,
        )

    # 合成: 架构任务用专用 fusion，其他用通用委员会合成
    from .execution_judge import _is_architecture_task, fuse_architecture_v2

    if _is_architecture_task(task):
        # v2 是唯一路径。旧两阶段 2026-09-11 删除 —— 它本身有致命缺陷（取并集膨胀到
        # 输入之和 1.8×、撞 max_tokens 腰斩、丢过整个 tasks 段），却被当作 v2 失败时的
        # 兜底；而告警日志显示它**从未在真机触发过**。现在的兜底是 v2 内部的
        # 「提取失败换模型重试」，那条比它强得多。QIDIAN_FUSION_V2 开关随之一并删除
        # （它的语义本来就是"回退旧流程"，没有旧流程了）。
        _rulings: dict = {}          # v2 把裁决记录写进来（见其 docstring 的 rulings 参数）
        fused = fuse_architecture_v2(task, list(outputs), rulings=_rulings,
                                     project_id=project_id)
        if not fused:
            # 落到下面的通用合成：每条产出截断到 3000 字。架构方案 20k+ 字，
            # 这是**降级**不是等价替换，必须留痕。
            witness.warn("dispatcher", "fusion_empty_fallback_synthesis"[:80])
        if fused:
            # 委员会产物随 DispatchResult 回传给调用方（_workflow_phases 再落 ProjectState）。
            # 曾经写 QIDIAN_DIR/.last_fusion.json 这个全局单文件 —— 并发下会串项目：
            # Flask threaded=True + 调度循环 concurrent=2，HTTP(_api_projects.run_phase)
            # 和后台循环(app.py 结果处理)两条路径都能进委员会，两个架构任务先后写同一路径，
            # 后写的覆盖先写的，先写的那家读到的是**别人的**模型/产物；读不到时整段静默跳过。
            # 走内存没有这些问题，顺带修掉「非委员会路径捡到上一轮残留文件」。
            fusion_meta = {
                "models": [m for m, _ in outputs],
                "outputs": [o for _, o in outputs],
                "fused": fused,
                "count": len(outputs),
                # 谁定稿、辩了几轮、哪些分歧判给谁、哪些独有做法采纳/驳回。
                # 没有它 GATE2 看到的只是一份"结果"，查不到融合过程。
                "rulings": _rulings,
            }
            # 包装成 ExecutorResult 兼容格式
            class _FusionResult:
                # 字段要和 neijinglu.build_report / _save_trace 读的契约对齐,
                # 缺一个就 AttributeError → trace 静默不落盘
                raw_output = fused
                success = True
                error = ""
                changed_files: list = []
                patch_path = ""
                token_count = 0     # 占位，真实值在类体外回填（见下）
                elapsed = 0.0
                tool_events: list = []

            # 各模型初稿 + 融合稿，给 _run_planning 落 ProjectState 用。
            # 必须在类体**外**赋值：class body 不做闭包查找，写在里面会 NameError。
            # 不带这个属性时调用方取到 None 直接跳过（非委员会路径）。
            _FusionResult.fusion_meta = fusion_meta
            # 回填真实用量。原来这里是硬编码的 token_count = 0 / elapsed = 0.0，
            # 而 _task_runner._archive_task_outcome 正是读这两个字段，
            # record_tokens 又有 `if tokens > 0` 闸 —— 于是架构阶段（委员会 + 融合，
            # 整个流水线最贵的一段）**一条用量都不进账**，成本统计永远对不上。
            # 注意：这里只覆盖**成员初稿**的用量；v2 融合自己那几步模型调用
            # （提取/辩论/定稿）仍没回传，是已知的剩余缺口。
            _FusionResult.token_count = member_tokens
            _FusionResult.elapsed = member_elapsed
            # 按成员记账用。消费方（workflow._record_phase_usage）拿不到这个属性时
            # 退回"一条记录 + agent_cfg 里的模型名"的老行为（非委员会路径）。
            _FusionResult.member_usage = member_usage
            return DispatchResult(
                level=level,
                agent_cfg={"model": f"fusion({','.join(m for m,_ in outputs)})"},
                executor_result=_FusionResult(),
                attempts=len(outputs) + 2,
            )
        # fusion 失败 → fallback 到通用合成

    # 通用委员会合成
    synthesizer = chain[0]
    synthesis_prompt = _build_synthesis_prompt(task, outputs)
    try:
        etype = synthesizer.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if executor_cls:
            # no_tools 必须带上 —— 委员会全程不改磁盘（见 _run_no_tools 的说明：
            # "禁工具是委员会的前提（纯文本出方案，别改磁盘）"）。兜底这条以前漏了：
            # 合成 agent 带着工具、cwd 又是奇点自己的仓库根，于是把目标项目的架构
            # 直接写进了**奇点仓库的 docs/**（2026-09-11 实测产出 docs/ARCHITECTURE.json）。
            synth_result = _run_executor(
                executor_cls, {**synthesizer, "no_tools": True}, synthesis_prompt,
                f"{task_id}_synth", level,
                baseline_ref=baseline_ref, cwd=cwd,
            )
            if synth_result and synth_result.raw_output:
                # 成员初稿 + 这次合成调用，都按**真实模型名**留一份用量。
                # 不带的话调用方只有 "committee(a,b)" 这个合成名 —— 计价表查不到，
                # 这一段就整段算不出钱（同 fusion(...) 那个坑）。
                synth_result.member_usage = list(member_usage) + [{
                    "model": synthesizer.get("model", ""),
                    "tokens": int(getattr(synth_result, "token_count", 0) or 0),
                    "elapsed": float(getattr(synth_result, "elapsed", 0.0) or 0.0),
                }]
                return DispatchResult(
                    level=level,
                    agent_cfg={"model": f"committee({','.join(m for m,_ in outputs)})"},
                    executor_result=synth_result,
                    attempts=len(outputs) + 1,
                )

        # 合成失败: 返回第一个产出
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=ExecutorResult(success=True, raw_output=raw),
                              attempts=len(outputs))
    except Exception:
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=ExecutorResult(success=True, raw_output=raw),
                              attempts=len(outputs))


def _build_synthesis_prompt(task: str, outputs: list[tuple]) -> str:
    """构建委员会合成 prompt。"""
    parts = [f"【原始需求】\n{task}\n\n【委员会各模型产出】"]
    for i, (model, result) in enumerate(outputs, 1):
        parts.append(f"\n── 模型{i}: {model} ──\n{result[:3000]}")
    parts.append("""

【你的任务】
你是委员会主席。综合以上各模型的方案，产出一份最终方案。
- 取各方案之长，避各方案之短
- 如有冲突，选择论证更充分的观点
- 保持原有 JSON 格式（如各模型都输出 JSON）
- 不要引入各模型都没提到的新内容""")
    return "\n".join(parts)



