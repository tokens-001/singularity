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

import json
import logging
import subprocess
import time
from pathlib import Path

from singularity.scheduler import config, tracker, witness
from singularity.scheduler._worktree import (
    _salvage_ref,
)
from singularity.scheduler._worktree import (
    cleanup_task_artifacts as _cleanup_task_artifacts,
)
from singularity.scheduler.tracker import TaskStatus

# ═══════════════════════════════════════════════════════════════

def _scan_refs(namespace: str) -> dict[str, str]:
    """扫 `refs/qidian/<namespace>/`，收出 `{task_id: commit_sha}`。

    ⚠️ 只调 `git for-each-ref`（**每仓一次**），别在任务循环里逐条查 git。
    ⚠️ 仓库根用 `_project_repo_roots()` —— 这些 ref 打在**项目仓**上，不是奇点仓
    （"读错仓库"这一族本仓踩过三次）。
    """
    from singularity.scheduler._git_worktree import _project_repo_roots
    prefix = f"refs/qidian/{namespace}/"
    out: dict[str, str] = {}
    for root in _project_repo_roots():
        try:
            r = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname) %(objectname)", prefix],
                cwd=str(root), capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            witness.warn("_api", f"salvage_scan_failed:{type(e).__name__}"[:120],
                         key="salvage_scan_failed")
            continue
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].startswith(prefix):
                out[parts[0].rsplit("/", 1)[-1]] = parts[1]
    return out


def salvageable_refs() -> dict[str, str]:
    """一次扫描，收出所有**可打捞的产物**：`{task_id: commit_sha}`。

    🔴 **F3（2026-09-17 真机）**：任务判失败/超时，**产物不一定丢** ——
    executor 干完一轮会 `commit_wt` 并把提交**锚在 `refs/qidian/pending/<task_id>`** 上
    （`_worktree._anchor_ref` 打的，防 git gc 回收）。成功合并那条路会
    `_release_ref` 删掉它 ⇒ **ref 还在 = 这个任务有可打捞的产物**。

    真机那轮就是：3 个任务全判 `failed`，而产物好好躺在 pending ref 上
    （拼起来 `pytest 40 passed`）—— **界面上一个字都不显示**，只有 CLI 路径有一句提示。
    """
    return _scan_refs("pending")


def salvaged_refs() -> dict[str, str]:
    """**删任务时留下**的产物：`{task_id: commit_sha}`（`refs/qidian/salvaged/`）。

    `task_delete` 换桩换过来的（见 `_worktree._salvage_ref`）—— 任务已经删了，
    产物**没进仓也没丢**，就停在这儿等谁来捞。

    ⚠️ 和 `salvageable_refs()` 的区别：那批**挂得上任务行**（界面按 task_id 显示
    "可打捞"）；这批**没有行能挂**（任务文件删了）⇒ 除了 `delivery_facts.py --refs`
    那条只读的路，界面上看不见。线索在 `.qidian/salvaged.jsonl`（谁、什么项目、
    什么状态），改桩那一刻写的。
    """
    return _scan_refs("salvaged")


def orphan_refs() -> dict[str, str]:
    """**孤儿 pending ref**：`{task_id: sha}` —— ref 还在，而**任务文件已经不在了**。

    ## 什么算"孤儿"（2026-09-20 定的，这是那条待办要的答案）

    `refs/qidian/pending/<task_id>` 在项目仓里**存在**，而
    `.qidian/tasks/<task_id>.json` **不在了**。

    ## 为什么它**不能自动清**

    产物的**本体**没丢 —— ref 就指着那个提交，`git show` 得出来。
    丢的是「**它是什么**」：描述 / 状态 / 父项目 / 它当时在干什么，**全在那份没了的任务文件里**
    （这也是 `_api_tasks.task_delete` 的注释说的"任务 JSON 是锚的唯一线索"）。

    ⇒ 清掉一条孤儿 ref = **永久删掉一份还在的产物**，而且**没有任何人能判它该不该留**
    （判据本身已经没了）。所以它只能**报出来给人判**，不能进任何自动清理。
    ⚠️ **两条路的差别已经没了**（2026-09-20）：`rm` 直删任务 JSON 不碰 ref；
    走 `DELETE /api/tasks/<id>` 现在也**不丢产物**了 —— `task_delete` 改成了换桩
    （`_worktree._salvage_ref`：pending → `refs/qidian/salvaged/<id>`，
    线索记进 `.qidian/salvaged.jsonl`）。⇒ 以后盘上再出现孤儿，只可能是
    "绕过 `task_delete` 手工删的任务文件"。
    这里另开一条只读的，只数不删。

    ⚠️ **看不见的那一半**：项目仓**被删掉**时，它里面的 ref 跟着一起没了 ——
    那个盲区**扫不出来**（ref 的存储就是那个仓）。这里只数"仓还在、ref 还在"的那部分。
    """
    refs = salvageable_refs()
    tasks_dir = tracker.tasks_dir()
    have = {f.stem for f in tasks_dir.glob("*.json")} if tasks_dir.exists() else set()
    return {tid: sha for tid, sha in refs.items() if tid not in have}


def _list_all_tasks() -> list[dict]:
    tasks_dir = tracker.tasks_dir()
    if not tasks_dir.exists():
        return []
    result = []
    for f in sorted(tasks_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.stem
            result.append(data)
        except Exception as e:
            witness.warn('_api', f'{e}')
    return result


def _read_task_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            witness.warn('_api', 'read_task_file')
        except Exception as _e:
            logging.getLogger(__name__).warning("task file read failed: %s", _e)
        return None


# ═══════════════════════════════════════════════════════════════
# 任务 CRUD  (ex _api_tasks.py)
# ═══════════════════════════════════════════════════════════════

def task_list(status_filter: str = "", level_filter: str = "") -> tuple[dict, int]:
    """GET /api/tasks?status=&level=

    ⚠️ `level` 比的**是 `route_level`**（2026-09-14 改正）：这里原来比的是 `route_type`
    —— 公开 API 的语义错了，而且**静默错**（不报错、返回一批看起来合理的任务）。
    前端从不传这个参数（它在自己那边过滤），所以一直没人踩；但任何脚本按
    `?level=` 过滤都会拿到"按类型过滤"的结果。
    """
    all_tasks = _list_all_tasks()
    # 🔴 **一次扫描**（不是在任务循环里逐条查 git）：判失败但产物可打捞的那些任务
    salvage = salvageable_refs()
    result = []
    now = time.time()
    for t in all_tasks:
        if status_filter and t.get("status") != status_filter:
            continue
        if level_filter and t.get("route_level") != level_filter:
            continue
        created = t.get("created_at", 0)
        updated = t.get("updated_at", created)
        result.append({
            "id": t.get("id", t["_filename"]),
            "project_id": t.get("project_id", ""),
            "description": (t.get("description", "") or "")[:120],
            "status": t.get("status", "unknown"),
            "route_level": t.get("route_level", ""),
            "route_type": t.get("route_type", ""),
            "priority": t.get("priority", 0),
            "depends_on": t.get("depends_on", []),
            "children": t.get("children", []),
            "error": (t.get("error", "") or "")[:200],
            "retry_count": t.get("retry_count", 0),
            "created_at": created,
            "wait_sec": round(now - created) if created else 0,
            "duration_sec": round(updated - created) if t.get("status") in ("done", "failed") else None,
            # 🔴 **F3：判失败但产物可打捞**（2026-09-17）。非空 = 这个任务有一份
            # **已经提交、已经锚定**的产物躺在 `refs/qidian/pending/<id>` 上，
            # 可以人工捞回来。原来**界面上一个字都不显示**，只有 CLI 路径有提示。
            "salvage_ref": salvage.get(t.get("id", t["_filename"]), ""),
        })
    return {"tasks": result, "total": len(result)}, 200


def task_detail(task_id: str) -> tuple[dict, int]:
    """GET /api/tasks/<id>"""
    task_path = tracker.tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return {"error": "任务不存在"}, 404
    data = _read_task_file(task_path)
    if not data:
        return {"error": "读取失败"}, 500
    now = time.time()
    created = data.get("created_at", 0)
    updated = data.get("updated_at", created)
    data["wait_sec"] = round(now - created) if created else 0
    data["duration_sec"] = round(updated - created) if created else 0
    # 🔴 F3：详情页也要看得见（判失败但产物可打捞）
    data["salvage_ref"] = salvageable_refs().get(task_id, "")
    # DAG 关系
    data["_dag_parents"] = []
    data["_dag_children"] = []
    for dep_id in data.get("depends_on", []):
        dep_path = tracker.tasks_dir() / f"{dep_id}.json"
        if dep_path.exists():
            dep_data = _read_task_file(dep_path)
            if dep_data:
                data["_dag_parents"].append({
                    "id": dep_id,
                    "description": (dep_data.get("description", "") or "")[:80],
                    "status": dep_data.get("status", "unknown"),
                })
    for child_id in data.get("children", []):
        child_path = tracker.tasks_dir() / f"{child_id}.json"
        if child_path.exists():
            child_data = _read_task_file(child_path)
            if child_data:
                data["_dag_children"].append({
                    "id": child_id,
                    "description": (child_data.get("description", "") or "")[:80],
                    "status": child_data.get("status", "unknown"),
                })
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    data["_has_trace"] = trace_path.exists()
    return data, 200


def task_trace(task_id: str, section: str = "", fmt: str = "") -> tuple:
    """GET /api/tasks/<id>/trace"""
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    if not trace_path.exists():
        return {"error": "Trace 文件不存在"}, 404
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"error": "Trace 文件读取失败"}, 500

    if fmt == "md":
        from singularity.scheduler.neijinglu import DeliveryReport, format_report
        report = DeliveryReport.from_dict(data)
        return format_report(report), 200, {"Content-Type": "text/plain; charset=utf-8"}

    if section == "route":
        route = data.get("route", {})
        return {
            "level": route.get("level", "?"),
            "gate_required": route.get("gate_required", False),
            "task_type": route.get("task_type", "default"),
            "matched_signals": route.get("matched_signals", []),
        }, 200
    elif section == "pre_search":
        ps = data.get("pre_search", {})
        return {
            "skipped": ps.get("skipped", True),
            "reason": ps.get("reason", ""),
            "top_decisions": ps.get("top_decisions", []),
            "memory": ps.get("memory", {}),
        }, 200
    elif section == "validation":
        val = data.get("validation", {})
        return {
            "verdict": val.get("verdict", "?"),
            "action": val.get("action", "?"),
            "validate_verdict": val.get("validate_verdict", ""),
            "validate_reason": val.get("validate_reason", ""),
            "gate_passed": val.get("gate_passed"),
            "gate_message": val.get("gate_message", ""),
            "turns_used": val.get("turns_used", 0),
            "unverified": val.get("unverified", []),
            "changed_files": data.get("changed_files", []),
            "agent_output": data.get("agent_output", ""),
            "token_count": data.get("token_count", 0),
            "elapsed": data.get("elapsed", 0),
        }, 200
    return data, 200


# "已经派出去过"的状态 —— 从状态机**推**出来，不手抄：
#   `_INFLIGHT` 去掉 `routed`（routed 是"等着被派"，不是"派过了"）
#   ∪ 终态 ∪ 两个**非终态**的中间态（派过之后才可能进）。
_POST_DISPATCH = (
    {s.value for s in tracker._INFLIGHT} - {tracker.TaskStatus.ROUTED.value}
) | {s.value for s in tracker._TERMINAL} | {
    tracker.TaskStatus.DECOMPOSED.value, tracker.TaskStatus.CONFLICT_HELD.value,
}
# 派过之后、但**不是终态**的两个：时间线要如实画成"当前停在哪儿"，
# 不许画成终点（见 tracker.is_terminal 的 docstring）。
_NOT_TERMINAL_ENDS = {
    tracker.TaskStatus.DECOMPOSED.value, tracker.TaskStatus.CONFLICT_HELD.value,
}


def task_timeline(task_id: str) -> tuple[dict, int]:
    """GET /api/tasks/<id>/timeline

    ⚠️ 这是**从任务文件的当前状态反推**出来的时间线，不是一份事件日志
    （没有逐步落盘的状态转移记录）⇒ 节点是"重建"的，中间跳过的状态看不见。
    所以规矩有两条：① 只对**真终态**画终点（`tracker.is_terminal`）；
    ② 其它状态画的节点都带 `terminal: False`，别让读的人以为它跑完了。
    """
    task_path = tracker.tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return {"error": "任务不存在"}, 404
    task_data = _read_task_file(task_path)
    if not task_data:
        return {"error": "读取失败"}, 500
    timeline = []
    status = task_data.get("status", "pending")
    created_at = task_data.get("created_at", 0)
    updated_at = task_data.get("updated_at", created_at)
    timeline.append({"from": None, "to": "pending", "timestamp": created_at, "meta": {}})
    route_level = task_data.get("route_level", "")
    if status not in ("pending",) and route_level:
        timeline.append({
            "from": "pending", "to": "routed",
            "timestamp": task_data.get("routed_at", updated_at),
            "meta": {"route_level": route_level, "route_gate": task_data.get("route_gate", False),
                     "route_type": task_data.get("route_type", "default")},
        })
    if status in _POST_DISPATCH:
        timeline.append({"from": "routed", "to": "dispatched", "timestamp": updated_at, "meta": {}})
    if task_data.get("snapshot_id"):
        timeline.append({"from": "dispatched", "to": "running", "timestamp": updated_at,
                         "meta": {"snapshot_id": task_data.get("snapshot_id", "")}})
    if tracker.is_terminal(status):
        prev = "validating" if status in ("done", "failed") else "running"
        meta = {}
        if status == "failed":
            meta["error"] = (task_data.get("error", "") or "")[:200]
        if status == "rolled_back":
            meta["rolled_back"] = True
        timeline.append({"from": prev, "to": status, "timestamp": updated_at, "meta": meta})
    elif status in _NOT_TERMINAL_ENDS:
        # ⚠️ 这两个**不是终态**（`tracker._TERMINAL` 里没有）：decomposed 等子任务聚合、
        # conflict_held 等人解决冲突，之后**都要回调度循环**。原来它们和
        # done/failed/rolled_back 并列在同一个 `if` 里 ⇒ 时间线给一个**还没跑完**的任务
        # 画出了"dispatched → running → 终态"的完整历程，读的人会以为它结束了。
        timeline.append({"from": "running", "to": status, "timestamp": updated_at,
                         "meta": {"terminal": False,
                                  "note": "非终态：还会回到调度循环"}})
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    if trace_path.exists():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            timeline.append({
                "from": None, "to": "_trace", "timestamp": updated_at,
                "meta": {
                    "route": trace.get("route", {}).get("matched_signals", []),
                    "elapsed": trace.get("elapsed"),
                    "token_count": trace.get("token_count"),
                    "changed_files": trace.get("changed_files", []),
                    "validation_verdict": trace.get("validation", {}).get("verdict"),
                    "pre_search_escalated": trace.get("pre_search", {}).get("escalated"),
                },
            })
        except Exception as e:
            witness.warn('_api', f'{e}')
    return {"task_id": task_id, "current_status": status, "timeline": timeline}, 200


def task_hold(task_id: str, reason: str = "") -> tuple[dict, int]:
    """POST /api/tasks/<id>/hold"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持扣留"}, 400
    tracker.transition(task_id, task.status, held=True, held_reason=reason)
    return {"ok": True, "held": True, "reason": reason}, 200


def task_release(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/release"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if not task.held:
        return {"error": "任务未被扣留"}, 400
    tracker.transition(task_id, task.status, held=False, held_reason="")
    return {"ok": True, "held": False}, 200


def _validate_route_level(level: str) -> bool:
    """route_level 拼进 worktree 路径，必须只含 ASCII 字母数字下划线连字符（防路径穿越）。"""
    return bool(level) and len(level) <= 64 and all(c.isascii() and (c.isalnum() or c in "_-") for c in level)


def task_override_route(task_id: str, level: str, locked: bool = True) -> tuple[dict, int]:
    """POST /api/tasks/<id>/override-route"""
    if not _validate_route_level(level):
        return {"error": "非法的 route_level 格式"}, 400
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持覆盖路由"}, 400
    tracker.transition(task_id, task.status, route_level=level, route_locked=locked)
    return {"ok": True, "route_level": level, "locked": locked}, 200


def task_cancel(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/cancel"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK, TaskStatus.DECOMPOSED):
        return {"error": f"终态任务 {task.status.value} 不可取消"}, 400
    config.ensure_dirs()
    if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED, TaskStatus.PAUSED):
        # PAUSED 状态下也接受取消: 删 pause 文件 + 写 cancel 文件
        pause_path = config.PAUSE_DIR / f"{task_id}.json"
        if pause_path.exists():
            pause_path.unlink()
        cancel_path = config.CANCEL_DIR / f"{task_id}.json"
        cancel_path.write_text(json.dumps({"task_id": task_id, "cancelled_at": time.time()}), encoding="utf-8")
        return {"ok": True, "message": "已发送取消信号"}, 200
    else:
        tracker.transition(task_id, TaskStatus.FAILED, error="用户手动取消")
        return {"ok": True, "message": "已取消"}, 200


def task_pause(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/pause — 手动暂停任务 (Tasks 页的暂停按钮)。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
        return {"error": f"只有运行中的任务可暂停, 当前状态: {task.status.value}"}, 400
    config.ensure_dirs()
    pause_path = config.PAUSE_DIR / f"{task_id}.json"
    pause_path.write_text(json.dumps({"task_id": task_id, "paused_at": time.time()}), encoding="utf-8")
    return {"ok": True, "message": "暂停信号已发送, 当前 turn 结束后生效"}, 200


def task_resume(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/resume — 恢复被暂停的任务。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status != TaskStatus.PAUSED:
        return {"error": f"只有暂停中的任务可恢复, 当前状态: {task.status.value}"}, 400
    pause_path = config.PAUSE_DIR / f"{task_id}.json"
    if pause_path.exists():
        pause_path.unlink()
    return {"ok": True, "message": "已发送恢复信号"}, 200


def task_set_mode(task_id: str, mode: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/mode — 切换执行模式 (auto_edit | confirm_changes)。"""
    if mode not in ("auto_edit", "confirm_changes"):
        return {"error": f"无效模式: {mode}，可选 auto_edit / confirm_changes"}, 400
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    tracker.transition(task_id, task.status, execution_mode=mode)  # 锁内更新, 防 read-modify-write 竞态
    return {"ok": True, "task_id": task_id, "execution_mode": mode}, 200


def _record_salvaged(task, task_id: str, sha: str, repo_root) -> None:
    """把"删任务留下来的产物"记一行进 `.qidian/salvaged.jsonl`（只增不删）。

    任务文件是「这个 ref 是什么」（描述 / 状态 / 父项目 / 当时在干什么）的**唯一**线索，
    它马上就被删了 ⇒ 不记这一行，产物就成了查无来处的孤儿 ref
    （09-19 清那 10 个孤儿任务时，正因为没有对照表才得先人工补一份）。
    """
    row = {
        "task_id": task_id,
        "repo": str(repo_root),
        "ref": f"refs/qidian/salvaged/{task_id}",
        "sha": sha,
        "description": (getattr(task, "description", "") or "")[:200],
        "project_id": getattr(task, "project_id", "") or "",
        "status": str(getattr(getattr(task, "status", ""), "value", "") or ""),
        "deleted_at": time.time(),
    }
    try:
        p = config.QIDIAN_DIR / "salvaged.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        # 静默 = 线索没了而产物还在，以后没人说得清它是谁的
        witness.warn('_api', f'salvaged_record:{task_id}:{type(e).__name__}'[:100],
                     key="salvaged_record_failed")


def task_delete(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/delete — 清任务本体 + 衍生残留 (worktree/snapshot/标记)。

    ⚠️ **锚不在这里释放**（2026-09-20 改）：还没进仓的产物**换桩**留着，见下面那段注释。
    """
    config.ensure_dirs()
    task = tracker.read_task(task_id)  # 先读: worktree/ref 清理需要 repo_root
    try:
        from singularity.scheduler.project import repo_root_for
        repo_root = repo_root_for(task) if task else config.PROJECT_ROOT
    except Exception:
        repo_root = config.PROJECT_ROOT

    deleted = _cleanup_task_artifacts(task_id, repo_root)

    # 🔴 **锚在这里「换桩」，不是「释放」**（2026-09-20 改）。
    #
    # 原来是 `_release_ref` —— 删任务顺手剪断"产物可打捞"那根绳，产物**真丢**
    # （09-18 实测掉过 3 个）。当时的取舍是"任务文件没了 ⇒ 界面看不见这个 ref ⇒
    # 留着就是孤儿"，代价写着"**用户要留就先别删任务**"。可界面上**没有任何地方
    # 说过这句话**，用户也没有"先留"的动作可用（F3 只给徽标），而这套系统里
    # 最贵的恰恰就是产物（09-16 那轮："完全交付，只是系统没认出来"）。
    #
    # 换桩两件事都占：`refs/qidian/salvaged/<id>` 让对象永远是 gc root（产物不丢），
    # 而 `refs/qidian/pending/` 底下干干净净（"有可打捞的产物"不再挂在已删的任务上）。
    # 任务文件一删就丢了「这个 ref 是什么」的线索 ⇒ 顺手往 `.qidian/salvaged.jsonl`
    # 记一行（只增不删的账，跟 alerts.jsonl 一个性质，给人查的）。
    #
    # ⚠️ **搬不成时一个字都不动**（`_salvage_ref` 返回空串）：旧绳留着 ——
    #    那种形态 `orphan_refs()` 数得出来、人也捞得回，比丢掉强。
    salvaged = ""
    try:
        salvaged = _salvage_ref(task_id, repo_root=repo_root)
    except Exception as e:
        witness.warn('_api', f'salvage_ref:{task_id}:{type(e).__name__}:{e}'[:100],
                     key="task_delete_salvage_ref_failed")
    if salvaged:
        _record_salvaged(task, task_id, salvaged, repo_root)

    # 任务本体/取消/暂停/parking/扣留 单文件 (delete 一并清, retry 不动)
    def _rm(p: Path) -> None:
        nonlocal deleted
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception as e:
            witness.warn('_api', f'del:{e}')
    for d in (tracker.tasks_dir(), config.CANCEL_DIR, config.PAUSE_DIR,
              config.PARKED_DIR, config.HOLD_DIR):
        _rm(d / f"{task_id}.json")
    _rm(config.TRACE_DIR / f"{task_id}.json")

    # 反引用清理: 父任务 children + project.task_ids
    # (否则父任务 maybe_complete_parent 读不到已删子任务 → 永久卡在 BLOCKED/DECOMPOSED)
    if task is not None:
        for p in tracker.tasks_dir().glob("*.json"):
            if p.stem == task_id:
                continue
            try:
                parent = tracker.read_task(p.stem)
                if parent is not None and task_id in parent.children:
                    tracker.set_children(p.stem, [c for c in parent.children if c != task_id])
            except Exception as e:
                # 静默 = 父任务里残留已删子任务的 id，之后查依赖关系会撞鬼
                witness.warn('_api', f'orphan_child:{p.stem}:{e}'[:80])
        if getattr(task, 'project_id', ''):
            try:
                from . import project as proj_mod
                proj = proj_mod.load(task.project_id)
                if proj is not None and task_id in proj.task_ids:
                    proj.task_ids = [t for t in proj.task_ids if t != task_id]
                    proj_mod.save(proj)
            except Exception as e:
                witness.warn('_api', f'orphan_project_task:{task.project_id}:{e}'[:80])

    if deleted:
        msg = f"已删除 {deleted} 个文件"
        if salvaged:
            msg += (f"；它还没进仓的产物**没丢** —— 已改挂 "
                    f"refs/qidian/salvaged/{task_id}（{salvaged[:7]}），"
                    f"账记在 .qidian/salvaged.jsonl")
        return {"ok": True, "message": msg, "salvaged": salvaged}, 200
    if salvaged:
        # 任务文件早就不在了（手工删过），可盘上确实动了一下 —— 别让响应说"什么都没发生"
        return {"error": f"任务文件不存在（但它的产物没丢，已改挂 "
                         f"refs/qidian/salvaged/{task_id}）", "salvaged": salvaged}, 404
    return {"error": "任务文件不存在"}, 404


def _supersede_trace(task_id: str) -> None:
    """把**上一次尝试**的 trace 挪进 `superseded/`，让这次重试能写新的一份。

    ⚠️ **为什么必须挪走**（2026-09-15 真机坐实）：`_exec._save_trace` 开头有一道
    幂等守卫 —— 见到 trace 文件已存在就 `return`。而重试**复用同一个 task id**
    （`tracker.transition(PENDING)`），重试前调的 `cleanup_task_artifacts` 又
    **唯独不清 trace**（只有 `task_delete` 清）⇒ 第二趟跑完时那道守卫直接返回：
    trace 永远停在**失败那一版**，而且因为 `return` 在函数开头，
    **后半截（`mem_mod.index_task` / `update_attrs` / `record_scope`）整块跳过**
    —— 记忆里那条任务于是永远以为自己是失败的。
    真机现场：`1789477697814` / `1789477697816` 重试成功后，trace 仍写着
    「执行超时(>1588s) 被杀，未及输出总结」，而两个任务其实都 `done` 了。

    ⚠️ **别改成删、也别改成覆盖**：删 = 丢证据（本仓的大忌）；覆盖 = 把那道守卫
    要防的"同一次尝试里被写两遍"一起拆掉。挪进子目录两边都保住 ——
    全仓对 `traces/` 的 glob 都是**非递归**的（`witness` / `_memory_lifecycle`
    用的都是 `glob("*.json")`），不会把旧证据当成"当前 trace"扫进去。
    """
    src = config.TRACE_DIR / f"{task_id}.json"
    if not src.exists():
        return
    try:
        dst_dir = config.TRACE_DIR / "superseded"
        dst_dir.mkdir(parents=True, exist_ok=True)
        src.rename(dst_dir / f"{task_id}.{int(src.stat().st_mtime)}.json")
    except Exception as e:
        # 挪不动**必须出声**：症状是"重试完 trace 还停在旧版"——那正是本次要修的
        # 东西；静默失败会把刚修好的又变回没修，而且没人看得出来。
        witness.warn('_api', f'supersede_trace:{type(e).__name__}:{e}'[:80])


def task_retry(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/retry"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.FAILED, TaskStatus.ROLLED_BACK):
        return {"error": f"当前状态 {task.status.value} 不支持重试"}, 400
    # 重跑前清衍生残留 (worktree/pending ref/snapshot)，避免 anchor_ref 冲突 + 脏 worktree
    try:
        from singularity.scheduler.project import repo_root_for
        repo_root = repo_root_for(task) if task else config.PROJECT_ROOT
    except Exception:
        repo_root = config.PROJECT_ROOT
    _cleanup_task_artifacts(task_id, repo_root)
    # 🔵 **锚定 ref 在这里「不」松手**（2026-09-19 反过来；这里原来是显式 `_release_ref`）。
    #
    # 释放的语义只有一种读法（`_worktree.cleanup_task_artifacts` 的 docstring 写明）：
    # **释放 = 断言"这个任务的产物已经安全进项目仓了"**。
    # 而重试那一刻这句**恰恰是假的** —— 不然重试什么？
    # `test_anchor_ref_lifecycle.py` 的文件头也是这么定的：「merged 是**唯一**让这句话
    # 成立的分支」。原来这次释放和那条原则是矛盾的。
    #
    # 原来的理由：「重试 = 新的一次尝试 ⇒ 旧锚已被取代」。但「被取代」在点下重试那一瞬
    # 是**假设、不是事实**：要真跑起来、真产出，旧锚才真的被取代。而 `_anchor_ref` 用的是
    # `git update-ref ref <sha>` —— **无条件覆盖** ⇒ **新尝试一旦产出，旧锚自己就被盖掉，
    # 根本不需要提前松手。**
    #
    # ⇒ 提前释放只在一种情况下产生差别：**重试没跑成**（调度循环没开 / 预算耗尽 /
    #   被取消 / 进程重启）—— 那时旧产物**既没被取代、又被释放**，纯丢。
    #   而"产物在、只是没进仓"恰恰是本仓最常见的形态（09-18 一天 62 次被 240s 掐断）。
    #
    # 重试成功那条路完全不受影响：有产出 ⇒ 新锚覆盖旧锚；真合并进仓 ⇒
    # `_release_ref` 由**知道产物落没落地**的那条路（merge）来做。
    # ⚠️ 代价（如实记）：重试中途挂掉时，界面会对着这个任务说"有可打捞的产物"，
    #   指的是**上一版**。**那不是误报 —— 产物确实还在**，正是 `salvageable_refs` 的定义。
    #   要消除这个歧义该改的是**标签**，不是删产物。
    # 重试 = **新的一次尝试** ⇒ 旧 trace 必须先让位，否则这一趟白跑（见上面 docstring）
    _supersede_trace(task_id)
    tracker.transition(task_id, TaskStatus.PENDING, error="", retry_count=0)
    return {"ok": True, "new_status": "pending"}, 200


def task_approval(task_id: str, decision: str = "reject", action: str = "",
                  push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/approval —— 人对**工具级审批请求**的答复。

    以前这里只推一条 SSE 就返回，**没有任何执行器读得到它** ——
    `require_approval` 因此是个只播报不拦的半成品（见 permission.py 的通道说明）。
    现在真正落盘，卡在 `permission.request_approval` 里轮询的那个 worker 会取走。

    返回里的 `found` 是**真的找到一条待审请求**没有。界面上点一个已经失效的
    审批（超时了 / 任务被删了）要说清"没找到"，不能一律回 ok 让人以为拦住了。
    """
    from .permission import decide_approval
    found = decide_approval(task_id, decision)
    if push_event:
        push_event("system",
                   f"[{tracker.short_id(task_id)}] 用户{decision}了 {action}"
                   + ("" if found else "（没有待审的请求，未生效）"))
    return {"ok": found, "found": found, "decision": decision}, 200


def task_apply(task_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/apply — 应用 E+ 智谱 patch 到工作区。"""
    from .executors.zhipu_api import ZhipuApiExecutor
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    result = ZhipuApiExecutor.apply_patch(task_id)
    success = bool(result.get("applied"))
    msg = result.get("message", "")
    if push_event:
        push_event("system", f"[{tracker.short_id(task_id)}] apply: {msg}")
    return {"ok": success, "message": msg}, 200


def task_rollback(task_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/rollback — 回滚到该任务的执行前快照。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    snapshot_id = getattr(task, "snapshot_id", "") or ""
    if not snapshot_id:
        return {"error": "该任务没有执行前快照, 无法回滚"}, 400
    meta_path = config.SNAPSHOT_DIR / f"{snapshot_id}.json"
    if not meta_path.exists():
        return {"error": f"快照元数据不存在: {snapshot_id}"}, 400
    from . import snapshot as snap_mod
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        snap = snap_mod.Snapshot(
            id=meta["id"], method=meta["method"], ref=meta["ref"],
            created_at=meta.get("created_at", 0.0), repo_root=meta.get("repo_root", ""),
        )
    except (json.JSONDecodeError, KeyError, OSError) as e:
        return {"error": f"快照元数据损坏: {e}"}, 500
    from .project import repo_root_for
    ok = snap_mod.rollback(snap, repo_root=Path(snap.repo_root or str(repo_root_for(task))))
    msg = f"已回滚到快照 {snapshot_id}" if ok else f"回滚失败 (快照 {snapshot_id}), 需人工处理"
    if push_event:
        push_event("system", f"[{tracker.short_id(task_id)}] rollback: {msg}")
    return {"ok": ok, "message": msg}, (200 if ok else 400)


def task_supervise(task_id: str, data: dict, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/supervise — 监督者介入。"""
    from .project import repo_root_for
    from .supervisor import supervise
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    verdict = supervise(
        task_description=task.description,
        changed_files=data.get("changed_files", []),
        constraints=data.get("constraints", []),
        checklist=data.get("checklist", []),
        agent_output=data.get("agent_output", ""),
        task_id=task_id,
        repo_root=str(repo_root_for(task)),
    )
    result = {"verdict": verdict.verdict, "action": verdict.verdict, "issues": verdict.issues}
    if push_event:
        push_event("system", f"[{tracker.short_id(task_id)}] 监督介入: {verdict.verdict}")
    return result, 200


def task_submit(desc: str, priority: int = 0, depends_on: list = None,
                route_level: str = "", route_locked: bool = True,
                route_type: str = "",
                project_id: str = "",
                push_event=None) -> tuple[dict, int]:
    """POST /api/tasks — 创建新任务。project_id 非空则挂到该项目（同步写 project.task_ids）。"""
    if route_level and not _validate_route_level(route_level):
        return {"error": "非法的 route_level 格式"}, 400
    config.ensure_dirs()
    task = tracker.create(desc, priority=priority, depends_on=depends_on or [], project_id=project_id)
    if project_id:
        # 只写 task.project_id 不够：项目页的任务数读的是 project.task_ids，
        # orchestrator 也只认 task_ids 里的任务
        from . import project as proj_mod
        try:
            proj = proj_mod.load(project_id)
            if proj is not None:
                proj.task_ids.append(task.id)
                proj_mod.save(proj)
        except Exception as e:
            # ⚠️ **建了任务却没登记进项目 = 它永远不会被派发**（项目页数不到它、
            # orchestrator 只认 `task_ids`），可从界面上看它就是一条正常的 pending。
            # 撤回，并且**如实告诉调用方没建成** —— 别让人以为建好了（静默成功是这类
            # 事故最坏的形状，见 tracker.rollback_create 的说明）。
            # ⚠️ 出声写在**这里**、不靠 `rollback_create` 里那句：静默 except 那把尺子
            # 看不见 helper 里的出声（本仓记过的盲区），会平白涨基线。
            # 两个 key 各司其职：这条是**原因**，helper 那条是**动作**（撤了几条）。
            witness.warn("_api", f"task_attach_failed:{project_id}:{type(e).__name__}"[:160],
                         key="task_attach_failed")
            tracker.rollback_create([task.id],
                                    why=f"task_submit 登记进项目失败: {type(e).__name__}")
            return {"error": f"任务创建失败（未建成功，已撤回）: {type(e).__name__}: {e}"[:300]}, 500
    if route_level or route_type:
        kwargs = {}
        if route_level:
            kwargs["route_level"] = route_level
            kwargs["route_locked"] = route_locked
        if route_type:
            kwargs["route_type"] = route_type
        tracker.transition(task.id, TaskStatus.PENDING, **kwargs)
    if push_event:
        push_event("task", json.dumps({"task_id": task.id, "status": "pending", "desc": desc[:120], "project_id": project_id}))
    return {"ok": True, "task_id": task.id, "description": desc[:120]}, 200


def task_update(task_id: str, data: dict) -> tuple[dict, int]:
    """PUT /api/tasks/<id> — 更新任务描述等字段。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    kwargs = {}
    if "description" in data:
        kwargs["description"] = str(data["description"])[:8000]
    if not kwargs:
        return {"error": "无可更新字段"}, 400
    tracker.transition(task_id, task.status, **kwargs)
    return {"ok": True, "task_id": task_id}, 200


