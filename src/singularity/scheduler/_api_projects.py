"""_api.py — API handler 层 (所有路由处理函数)。

Section 分组:
  - 辅助函数
  - 任务 CRUD
  - 项目 CRUD + workflow
  - Agent / Model / API Store
  - Skill / Permission
  - MCP 服务器
  - 监控 / Auth / Health / 模板
  - Memory / Conflict
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from singularity.scheduler import tracker, witness

# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 项目 CRUD + Workflow  (ex _api_projects.py)
# ═══════════════════════════════════════════════════════════════

def project_list() -> tuple[dict, int]:
    """GET /api/projects"""
    from . import project as proj_mod
    projects = proj_mod.list_all()
    return {"projects": [p.to_dict() if hasattr(p, 'to_dict') else p for p in projects]}, 200


def project_create(name: str, template: str = "product_dev",
                   description: str = "", scope: str = "",
                   constraints: list = None, budget: float = 5.0,
                   flow_weight: str = "auto") -> tuple[dict, int]:
    """POST /api/projects

    响应带 ``suggested_flow`` —— 系统**只建议**（`suggest_flow`），不生效。
    人点了"采纳"才走 PUT /api/projects/<id>/flow-weight 落状态（防御模式 §47）。
    """
    from . import project as proj_mod
    try:
        p = proj_mod.create(name=name, template=template, description=description,
                            scope=scope, constraints=constraints or [], budget=budget,
                            flow_weight=flow_weight or "auto")
    except ValueError as e:
        return {"error": str(e)}, 400
    sug = proj_mod.suggest_flow(p)
    return {"ok": True,
            "project": {"id": p.id, "name": p.name},
            "suggested_flow": ({"weight": sug.weight, "reason": sug.reason} if sug else None)}, 200


def project_detail(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    d = proj.to_dict() if hasattr(proj, 'to_dict') else {"ok": True}
    d["repo_dir"] = str(proj_mod.repo_dir(project_id))  # 成品保存路径
    # 重量判据**在服务端算完给前端**，前端不许在 TS 里重推（§5 过线同理）
    fd = proj_mod.resolve_flow(proj)
    d["flow_decision"] = {"weight": fd.weight, "research": fd.research,
                          "committee": fd.committee, "source": fd.source,
                          "reason": fd.reason}
    return d, 200


def project_delete(project_id: str) -> tuple[dict, int]:
    """DELETE /api/projects/<id> —— 删项目**之前先把它的任务停掉**。

    🔴 **2026-09-19 真机踩过**：`proj_mod.delete` 只清项目那几份文件，任务一个不动
    ⇒ 那些任务**还是活的**：杀掉项目 + 重启后端，它们会被回收成 PENDING **又派下去
    接着烧钱**（实测烧了 9 分钟，还和别的轮抢模型）。
    ⇒ 所以删项目必须连同"停掉它在跑的任务"，而那一步以前只在人脑子里。

    ⚠️ **任务 JSON 一个都不删**（09-19 拍板）。锚 `refs/qidian/pending/<task_id>` 打在
    **项目仓**上，而任务文件是"这个锚是什么"的**唯一**线索（描述/状态/父项目）——
    删掉它就等于把可打捞的产物变成查无来处的孤儿 ref。
    所以这里只做两件：**取消还在跑的** + **把留下的报出来**（别让它悄悄攒）。

    返回多带两个计数（`cancelled` / `left_tasks`），前端只读 `ok` 也不受影响。
    """
    from . import _api_tasks
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    task_ids = list(proj.task_ids) if proj is not None else []

    cancelled = 0
    for tid in task_ids:
        if tracker.read_task(tid) is None:
            continue
        _, code = _api_tasks.task_cancel(tid)
        if code == 200:
            cancelled += 1

    ok = proj_mod.delete(project_id)
    left = sum(1 for tid in task_ids if tracker.read_task(tid) is not None)
    if ok and left:
        # 用 `key=` 显式聚合：这条是"要注意的事"，不是每个 tick 都刷的事件，
        # 但也不该让每次删项目各占一条（别的删项目现场会看到同一个 key 在涨）。
        witness.warn("project", f"project_deleted_left_tasks:{left}"[:80],
                     key="project_deleted_left_tasks")
    return {"ok": ok, "cancelled": cancelled, "left_tasks": left}, (200 if ok else 404)


def project_set_flow_weight(project_id: str, flow_weight: str = "") -> tuple[dict, int]:
    """PUT /api/projects/<id>/flow-weight —— 定点 setter（仿 lineup 那个）。

    非法值**直接 400，不静默改写成 auto**（防御模式 §47）。
    """
    from . import project as proj_mod
    if flow_weight not in ("auto", "light", "heavy"):
        return {"error": f"flow_weight 只能是 auto/light/heavy，收到 {flow_weight!r}"}, 400
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    before = proj_mod.resolve_flow(proj)
    proj.flow_weight = flow_weight
    proj.updated_at = time.time()
    after = proj_mod.resolve_flow(proj)
    proj.add_lineage({"action": "flow_weight_set", "from": before.weight, "to": after.weight,
                      "weight": flow_weight, "reason": after.reason})
    proj_mod.save(proj)
    return {"ok": True, "flow_weight": flow_weight,
            "flow_decision": {"weight": after.weight, "research": after.research,
                              "committee": after.committee, "source": after.source,
                              "reason": after.reason}}, 200


def projects_root_get() -> tuple[dict, int]:
    """GET /api/projects-root"""
    from . import project as proj_mod
    return {"root": str(proj_mod.get_projects_root())}, 200


def projects_root_set(path: str) -> tuple[dict, int]:
    """PUT /api/projects-root"""
    from . import project as proj_mod
    try:
        root = proj_mod.set_projects_root(path)
    except Exception as e:
        return {"error": str(e)}, 400
    return {"ok": True, "root": str(root)}, 200


def fs_list(path: str = "") -> tuple[dict, int]:
    """GET /api/fs/ls —— 列出目录的子目录（目录选择器用）。"""
    base = Path(path or str(Path.home())).expanduser()
    if not base.is_dir():
        return {"error": f"目录不存在: {base}"}, 404
    try:
        dirs = [e.name for e in sorted(base.iterdir()) if e.is_dir() and not e.name.startswith(".")]
    except PermissionError:
        return {"error": "无权限访问"}, 403
    return {"path": str(base), "parent": str(base.parent), "dirs": dirs}, 200


def fs_mkdir(path: str, name: str) -> tuple[dict, int]:
    """POST /api/fs/mkdir —— 在 path 下新建目录 name。"""
    base = Path(path).expanduser()
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name:
        return {"error": "非法目录名"}, 400
    target = base / name
    if target.exists():
        return {"error": "目录已存在"}, 400
    target.mkdir(parents=True)
    return {"ok": True, "path": str(target)}, 200


def fs_pick() -> tuple[dict, int]:
    """POST /api/fs/pick —— 用 macOS Finder 原生对话框选择文件夹。"""
    import sys
    if sys.platform != "darwin":
        return {"error": "仅支持 macOS"}, 400
    script = 'POSIX path of (choose folder with prompt "选择项目根目录")'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "选择超时"}, 500
    if r.returncode != 0:
        return {"error": "已取消"}, 400
    return {"path": r.stdout.strip()}, 200


# 归**调度循环**推的四档（权威表在 `project.py` 顶部）。其余档归 `run_phase`。
_LOOP_OWNED_PHASES = ("executing", "integrating", "reviewing", "delivering")


def _loop_status(next_phase) -> dict:
    """批准之后，"接下来谁推这个项目"要说在明面上（2026-09-19 外派评审 B3）。

    `next_phase` 只是**阶段名**，它不说"有没有人在推"。落到
    EXECUTING/INTEGRATING/REVIEWING/DELIVERING 这几档时推手是**调度循环**，
    而调度循环是 web 进程里的一个线程（`web/app.py` 的 `_loop_running`）——
    它没开的话，项目就停在那儿等人，而界面上只显示"实现中"，看不出是没人点火。
    这正是防御模式 §28 那个形状（返回里每个"像成功"的字段都要追得到一个副作用）。

    ⚠️ **查不到返回 None（= 不知道），不返回 False** —— "不知道"和"没在跑"
    在界面上必须分得开，否则这里就成了一个新的"看着像"。
    """
    nxt = getattr(next_phase, "value", "") or ""
    if nxt not in _LOOP_OWNED_PHASES:
        # 这几档归 `run_phase`（人在界面上点），没有"循环开没开"这回事
        return {"driven_by": "run_phase"}
    try:
        from singularity.web import app as web_app
        running = bool(getattr(web_app, "_loop_running", False))
    except Exception as e:
        # 这条理论上够不到（web 进程里 `app` 早已加载完，非 web 进程才可能炸），
        # 但**不能静默** —— "我查不到循环在不在跑"本身就是要记一笔的事，
        # 而静默 except 有棘轮在数（test_no_silent_except）。
        witness.warn("project", f"loop_status_unknown:{type(e).__name__}"[:120],
                     key="loop_status_unknown")
        return {"driven_by": "scheduler_loop", "loop_running": None}
    out = {"driven_by": "scheduler_loop", "loop_running": running}
    if not running:
        out["warning"] = ("调度循环没在跑 —— 这一档由它推，项目会停在原地等人。"
                          "在界面上启动调度循环。")
    return out


def project_gate_confirm(project_id: str, gate: str = "", decision: str = "",
                          feedback: str = "") -> tuple[dict, int]:
    """POST /api/projects/<id>/gate-confirm"""
    from . import project as proj_mod
    from .project import Phase
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404

    if gate:
        # Phase(gate) 对垃圾值直接抛 ValueError → 未捕获 → 500 + HTML。
        # 前端路由也没校验（_VALID_DECISIONS 定义了却没人用），所以这里必须挡。
        try:
            gate_phase = Phase(gate)
        except ValueError:
            valid = [p.value for p in Phase if p.value.startswith("gate")]
            return {"error": f"非法 gate: {gate!r}，可选 {valid}"}, 400
    else:
        gate_phase = proj.phase
    if decision == "approved":
        # 架构校验没过 → confirm_gate 会拒绝放行（返回 None）。
        # 这时必须**如实报错**，不能顺着往下写成 `next_phase: done` ——
        # 那会让用户以为批准成功了，而项目其实一步没动。
        bad_arch = next((i for i in proj.issues if i.get("type") == "arch_invalid"), None)
        next_p = proj.confirm_gate(gate_phase, "approved")
        proj_mod.save(proj)
        if next_p is None:
            return {"ok": False, "gate": gate, "decision": "approved",
                    "error": "架构校验未通过，不能放行。"
                             + (str(bad_arch.get("detail", "")) if bad_arch else "")
                             + "  请先打回（rejected）让它重新规划。"}, 409
        # ── 批准后要不要**顺手启动**下一阶段？ ──
        # 分两档，判据是"那个阶段归谁推"：
        #   · planning  —— **只有 run_phase 能推**：调度循环只管 EXECUTING/INTEGRATING/
        #     DELIVERING，而前端根本没有 run-phase 调用者（`api.runPhase` 定义了没人用）。
        #     不推的话项目批准完就永远停在那儿：2026-09-12 实测空等 14 分钟，
        #     界面上只显示"架构设计中"，看不出是没人点火。
        #   · executing / integrating / delivering —— 归调度循环，**不能在这儿推**，
        #     推了就是两套驱动抢着写同一个 phase。
        if next_p == Phase.PLANNING:
            from . import dispatcher as disp_mod
            from . import workflow as wf_mod
            _start_background(project_id, "planning", wf_mod.run_phase,
                              disp_mod.load_agents())
            return {"ok": True, "gate": gate, "decision": "approved",
                    "next_phase": next_p.value, "started_phase": "planning"}, 200
        return {"ok": True, "gate": gate, "decision": "approved",
                "next_phase": next_p.value, **_loop_status(next_p)}, 200
    elif decision == "rejected":
        # feedback 一路传到 `confirm_gate` —— 它负责写 lineage（两条入口共用一处，
        # 防御模式 #5）。**以前这里不传**：参数签了、`handle_gate3_reject` 也接了，
        # 但 HTTP 路由 `app.py` 根本没把 body 里的 feedback 递进来 ⇒ 恒为 ""，
        # 用户写了理由等于没写（防御模式 #70）。
        proj.confirm_gate(gate_phase, "rejected", feedback)
        proj_mod.save(proj)
        resp = {"ok": True, "gate": gate, "decision": "rejected",
                "next_phase": proj.phase.value}
        # GATE3 打回: D出修复方案
        if gate_phase == Phase.GATE3:
            from . import dispatcher as disp_mod
            from . import workflow as wf_mod
            agents = disp_mod.load_agents()
            result = wf_mod.handle_gate3_reject(proj, agents, feedback)
            resp = {"ok": True, "gate": "gate3", "decision": "rejected",
                    "result": result, "next_phase": proj.phase.value}
        # ── 打回后**必须有人点火** ──
        # 判据和上面批准那条一样，是"那个阶段归谁推"：RESEARCHING / PLANNING
        # **只有 `run_phase` 能推**（调度循环只管 EXECUTING 之后，前端也没有
        # run-phase 调用者）。⚠️ 打回退到这两档却没人推，就是**项目永久停在原地**：
        # 界面上只显示"调研中/架构设计中"，看不出是没人点火（批准那条实测空等 14 分钟）。
        # 这一段同时覆盖 GATE3 的 design 路由（`handle_gate3_reject` 把 phase 设成
        # PLANNING 之后原本同样没人点）。
        if proj.phase in (Phase.RESEARCHING, Phase.PLANNING):
            from . import dispatcher as disp_mod
            from . import workflow as wf_mod
            started = _start_background(project_id, proj.phase.value,
                                        wf_mod.run_phase,
                                        disp_mod.load_agents())
            resp["started"] = started
            if not started:
                # 防御模式 #28：返回里每个"像成功"的字段都要追得到一个副作用。
                # 没点着火却回一个纯 ok=true 的包，就是让调用方以为项目在动。
                resp["warning"] = (f"{proj.phase.value} 打回后没能自动启动"
                                   f"（该项目已有阶段在跑）—— 请稍后重试")
        return resp, 200
    return {"ok": True, "gate": gate, "decision": decision or "pending"}, 200


def project_run_phase(project_id: str, phase_name: str = "",
                      task_desc: str = "", agent_override: str = "",
                      push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/run-phase"""
    from . import dispatcher as disp_mod
    from . import project as proj_mod
    from . import workflow as wf_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    if not hasattr(proj, 'phase') or proj.phase is None:
        return {"error": "项目未设定阶段"}, 400
    phase = phase_name or proj.phase.value

    # run_phase 对这几档只会"等人"就 break（`workflow.run_phase` 的 TEMPLATE / GATE 分支），
    # 后台线程跑了等于没跑 —— 而返回 `{"ok":true,"started":true}` 会让调用方以为开始了。
    # 防御模式 §28：**返回 200 不等于动了手**。（前端没调这个接口，改它不影响 UI。）
    _waiting = {
        "template": "template 阶段不自推 —— 先填好需求，用 POST /api/projects/<id>/start 立项",
        "gate1": "gate1 是人工门，用 POST /api/projects/<id>/gate-confirm 批",
        "gate2": "gate2 是人工门，用 POST /api/projects/<id>/gate-confirm 批",
        "gate3": "gate3 是人工门，用 POST /api/projects/<id>/gate-confirm 批",
    }
    if phase in _waiting:
        return {"ok": False, "phase": phase, "started": False,
                "error": _waiting[phase]}, 409

    agents = disp_mod.load_agents()
    if not _start_background(project_id, phase, wf_mod.run_phase, agents):
        return {"ok": True, "phase": phase, "running": True,
                "note": "该项目已有阶段在跑，本次未重复启动"}, 200
    if push_event:
        push_event("system", f"[{tracker.short_id(project_id)}] {phase} 阶段已启动")
    return {"ok": True, "phase": phase, "started": True}, 200


# 正在跑阶段的项目。run_phase 是分钟级的（连续调 _run_research/_run_planning/
# _run_execution，每个都是模型调用），原来**同步跑在 Flask 请求线程里** ——
# 前端 fetch 早就超时了，结果也拿不到。改为后台线程 + 立即返回，
# 进度走 SSE（推送/项目页本来就靠它）。同时挡重复启动。
_RUNNING_PHASES: set[str] = set()
_PHASE_LOCK = threading.Lock()


def _start_background(project_id: str, label: str, fn, agents: dict) -> bool:
    """在后台线程跑 `fn(proj, agents)`。已有同名项目在跑 → 返回 False（不重复启动）。

    ⚠️ **收 `project_id`，不收 `proj` 对象**（2026-09-18 外派评审第二轮坐实）。

    原来四个调用点都把**请求线程 `load()` 出来的那个对象**直接交进后台线程 ——
    而这线程是**分钟级**的（`run_phase` 里可能跑多模型委员会，实测 621 秒）。
    而 `project.save()` 是 `to_dict()` **整份覆盖写**：没有字段级合并、没有版本号、
    也不检查"盘上那份是不是比我新"。⇒ 后台线程一存盘，就把这整段时间里**别人写的一切**
    （`owner_confirm` 里人的批准、`review_failures`/`integrate_failures` 的棘轮复位、
    `task_ids`、`issues`、`phase`）**整份退回**；而 `set_phase` 还会给这次倒退
    记一条 lineage —— **轨迹上看起来像有人推了它**。

    隔壁 `orchestrator._run_integration_merge_async` 早就是这么写的（收 id、进线程
    再 `load()`），注释里写明就是为这件事。**两条后台路，这条对齐那条。**

    ⚠️ 这**只治"传对象"这一条路**，治不了"两个写入者各自 load 再整份 save" ——
    那是同一个根的另一半，见 `docs/外派评审-第二轮-20260918.md` §S1。
    """
    with _PHASE_LOCK:
        if project_id in _RUNNING_PHASES:
            return False
        _RUNNING_PHASES.add(project_id)

    def _worker():
        try:
            # **进线程之后再 load** —— 拿到的是"现在"那份，不是请求线程那份快照。
            from . import project as proj_mod
            proj = proj_mod.load(project_id)
            if proj is None:
                # 排队期间项目被删了。出声，别拿 None 去喂 fn（那会是一个
                # 只在后台线程里炸、且和"没启动"长得一样的失败）。
                witness.warn("workflow", f"{label}:project_gone:{project_id}"[:120],
                             key="background_project_gone")
                return
            fn(proj, agents)
        except Exception as e:
            from singularity.scheduler import witness as _w
            _w.warn("workflow", f"{label}:{type(e).__name__}:{e}"[:120])
        finally:
            with _PHASE_LOCK:
                _RUNNING_PHASES.discard(project_id)

    threading.Thread(target=_worker, name=f"phase-{tracker.short_id(project_id)}", daemon=True).start()
    return True


def project_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/start"""
    from . import dispatcher as disp_mod
    from . import project as proj_mod
    from . import workflow as wf_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    agents = disp_mod.load_agents()
    if not _start_background(project_id, "start_workflow",
                             wf_mod.start_project_workflow, agents):
        return {"ok": True, "running": True,
                "note": "该项目已有流程在跑，本次未重复启动"}, 200
    if push_event:
        push_event("system", f"[{tracker.short_id(project_id)}] workflow 已启动")
    return {"ok": True, "started": True}, 200


def project_cost(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/cost — 该项目**今日**的真实花费。

    原来这里返回的是"即将进入的阶段预计花多少"，来自一张写死的价目表
    （调研 $0.02 / 架构 $2.50 / 审查 $1.00）—— 编的，和 _cli_projects 里那张是同一份。
    各模型单价差几十倍、又不知道这次会落到哪个模型上，向前预估没有依据，所以不再做。
    改为报**已发生**的真实数字；`unpriced_models` 非空时 cost 是下限。
    """
    from . import project as proj_mod
    from .project import Phase
    from .workflow import _needs_research
    p = proj_mod.load(project_id)
    if p is None:
        return {"error": "项目不存在"}, 404

    from ._token_budget import get_usage_stats
    try:
        stats = get_usage_stats()
    except Exception:
        stats = {}
    cost = next((r.get("cost", 0.0) for r in stats.get("by_project", [])
                 if r.get("project_id") == project_id), 0.0)

    # 下一步会调哪一档 agent —— 这个不是编的，照实说
    phase_levels = {Phase.RESEARCHING: "any", Phase.PLANNING: "any", Phase.REVIEWING: "any"}
    phase = p.phase
    level = "-"
    if phase == Phase.TEMPLATE:
        if _needs_research(p):
            level = phase_levels.get(Phase.RESEARCHING, "-")
    else:
        level = phase_levels.get(phase, "-")

    # 这里原来还带一个 `token_spent: p.token_spent` —— 那个字段全仓无人赋值、
    # 恒为 0，和上面算出来的真 `cost` 并排摆着，看着像"这个项目花了 0 元"。
    # 已删字段（前端本来也不读这个接口）。
    fd = proj_mod.resolve_flow(p)
    return {"cost": round(cost, 6), "phase": phase.value, "level": level,
            "flow_weight": p.flow_weight, "flow_reason": fd.reason,
            "unpriced_models": stats.get("unpriced_models", []),
            "token_budget_total": p.token_budget_total or 0}, 200


def project_lineage(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/lineage —— 项目的**血缘日志**。

    ⚠️ 2026-09-14 改正（外派 ⑩ 抓到、我核过）：这个端点叫 lineage，**却从来没返回过
    项目的 lineage**。它原来拿 `project_id` 去**任务模板表**里查
    （`task_templates.TEMPLATES` 里是 `bugfix` / `feature` 这种任务类型名），
    而项目 id 是纯数字 ⇒ 那一支恒不命中；落到的另一支返回的是"这个项目下的任务列表"。
    而项目自己那份 `ProjectState.lineage`（`add_lineage` 一直在写：phase 流转 /
    权重调整 / 打回…）**没有任何地方读**。

    ⇒ 现在返回真血缘。**响应的键是 `lineage`**，前端那条（`api.ts` 里没有任何地方调
    `/lineage`，纯死功能）哪天真接上时，拿到的就是该拿的东西。
    """
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": f"项目不存在: {project_id}"}, 404
    return {"project_id": project_id, "lineage": list(proj.lineage or [])}, 200


def project_phase_history(project_id: str, filename: str) -> tuple[str | None, int]:
    """GET /api/projects/<id>/history/<filename> —— 阶段产出的**历史版本**（纯文本）。

    🔴 2026-09-19 补：`_save_phase_output` 从 09-17 起就在覆盖前把上一版归档进
    `<项目目录>/history/<文件名>.<n>`，**数据早就在盘上，界面上一点入口都没有**
    （用户当场想看"打回前后的对比"）。同族的 `research-raw` 只解决"这一版看得到全文"，
    **不解决跨版本**。

    ⚠️ **回纯文本、一次性把各版拼起来**，不做列表接口 —— 同 `research-raw` 的理由：
    前端只要给个链接就能看，不用加异步取数的状态，"对比改进"就是上下滚动的事。
    各版之间插分隔头（版本号 + 落盘时间 + 字节数），**新→旧**排（想对比时先看到新的）。

    返回 `(text, code)`；调用方直接当 text/plain 吐出去。
    """
    import time as _time
    from pathlib import Path

    from . import project as proj_mod
    # 边界先挡：历史文件名来自 URL。**合法的只有"纯文件名"** —— 带路径分隔符或
    # `..` 一律拒（这个仓在 `task_override_route` 上已经栽过一次路径穿越的形状）。
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None, 400

    # ⚠️ 用 `_projects_dir()` 直接拼、**不调 `get_project_dir()`** —— 后者会 mkdir，
    # 查一个不存在的项目会顺手把它的目录建出来（`7bdeb34` 刚修过同族那个坑）。
    # 与 `workflow._phase_history_dir()` 拼的是同一条路径。
    hist = Path(proj_mod._projects_dir()) / project_id / "history"
    if not hist.is_dir():
        return None, 404
    versions: list[tuple[int, Path]] = []
    for f in hist.glob(f"{filename}.*"):
        tail = f.name.rsplit(".", 1)[-1]
        if tail.isdigit() and f.is_file():
            versions.append((int(tail), f))
    if not versions:
        return None, 404
    versions.sort(key=lambda t: t[0], reverse=True)      # 新 → 旧

    blocks: list[str] = []
    for n, f in versions:
        try:
            body = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # 读不出来 ≠ 这一版不存在（"损坏和没有长得一样" —— 本仓反复咬人的那个病）。
            # 两处都要出声：给用户的那句在返回文本里，**给运维的那句走 witness**
            # （只把错误拼进返回文本不算"出声" —— 静默 except 棘轮按 AST 形状看，
            #  它判得对：用户不一定点开这个链接，而盘上坏了就是坏了）。
            witness.warn("project",
                         f"phase_history_unreadable:{project_id}:{f.name}:{type(e).__name__}"[:160],
                         key="phase_history_unreadable")
            body = f"（这一版读不出来：{type(e).__name__}——它还在盘上：{f.name}）"
        blocks.append(
            f"===== 版本 {n} · {_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(f.stat().st_mtime))}"
            f" · {f.stat().st_size} 字节 =====\n{body}")
    head = (f"# {filename} 的历史版本（共 {len(versions)} 版，新 → 旧）\n"
            f"# 当前生效的那一版不在这里 —— 它在 .qidian/projects/<id>.{filename}\n\n")
    return head + "\n\n".join(blocks), 200


def project_snapshot(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/snapshot — 快照项目 repo (修复 #1 遗漏: 原先快照的是奇点仓库)。"""
    from . import project as proj_mod
    from . import snapshot as snap_mod
    snap = snap_mod.take(project_id, repo_root=proj_mod.repo_dir(project_id))
    return {"ok": True, "snapshot_id": snap.id, "ref": snap.ref}, 200


# project_auto 已删（2026-09-11 审计）：它调的 project.advance_phase 在 09-09 死代码清理时
# 就被删了，之后每次调用必 500；且全仓无调用方（前端也只有定义没用）。
# autopilot 本身早已移除（见 web/app.py "autopilot 已移除"），人控流程走 gate-confirm。


def project_lineup_get(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/lineup"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    lineup = getattr(proj, 'agent_lineup', {}) or {}
    return {"lineup": lineup}, 200


def project_lineup_set(project_id: str, lineup: dict) -> tuple[dict, int]:
    """PUT /api/projects/<id>/lineup"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    proj.agent_lineup = lineup
    proj_mod.save(proj)
    return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# Agent / Model / API Store  (ex _api_agents.py)
# ═══════════════════════════════════════════════════════════════

# ── token 估算常量 ──
_TOKEN_PER_CHAR = 0.6

