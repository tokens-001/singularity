# 奇点 Agent 调度平台 — Web 控制台
# Flask 后端：查看调度状态、提交任务、处理合并冲突

import json
import os
import sys
import time
from pathlib import Path

from flask import Flask, render_template, request, jsonify

# ── 调度器模块路径 ──────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from scheduler import tracker
from scheduler.tracker import TaskStatus
from scheduler import config as sched_config
from scheduler import witness
from scheduler import neijinglu
from scheduler import dispatcher as disp_mod
from scheduler import snapshot as snap_mod
from scheduler import merge as merge_mod

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

def _read_task_file(path: Path) -> dict | None:
    """读单个任务 JSON 文件，容错。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _list_all_tasks() -> list[dict]:
    """扫描 tasks 目录，返回全部任务 dict 列表（按创建时间倒序）。"""
    tasks_dir = tracker._tasks_dir()
    if not tasks_dir.exists():
        return []
    tasks = []
    for p in sorted(tasks_dir.glob("*.json"), reverse=True):
        data = _read_task_file(p)
        if data:
            # 补 computed 字段
            data["_filename"] = p.stem
            tasks.append(data)
    return tasks


def _format_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}min"
    return f"{sec / 3600:.2f}h"


# ═══════════════════════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════
# GET /api/status — 聚合状态
# ═══════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    sched_config.ensure_dirs()
    counts = witness._count_by_status()
    loads = witness._heartbeat_task_levels()
    pending_waits, done_durations = witness._timing_stats()
    token_totals = witness._token_stats()
    stalled = witness.check_stalled(timeout_seconds=600)

    agents = {}
    try:
        raw_agents = disp_mod.load_agents()
        for level, cfgs in raw_agents.items():
            agents[level] = [{"model": c.get("model", ""), "roles": c.get("roles", [])} for c in cfgs]
    except Exception:
        pass

    return jsonify({
        "counts": counts,
        "heartbeat_levels": loads,
        "running_total": sum(loads.values()),
        "avg_wait": _format_duration(sum(pending_waits) / len(pending_waits)) if pending_waits else "--",
        "avg_done": _format_duration(sum(done_durations) / len(done_durations)) if done_durations else "--",
        "token_totals": token_totals,
        "stalled": stalled,
        "agents": agents,
    })


# ═══════════════════════════════════════════════════════════
# GET /api/tasks — 任务列表
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks")
def api_tasks():
    status_filter = request.args.get("status", "")
    level_filter = request.args.get("level", "")

    all_tasks = _list_all_tasks()
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
        })

    return jsonify({"tasks": result, "total": len(result)})


# ═══════════════════════════════════════════════════════════
# GET /api/tasks/<id> — 单任务详情
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>")
def api_task_detail(task_id):
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return jsonify({"error": "任务不存在"}), 404

    data = _read_task_file(task_path)
    if not data:
        return jsonify({"error": "读取失败"}), 500

    # 补充 computed 字段
    now = time.time()
    created = data.get("created_at", 0)
    updated = data.get("updated_at", created)
    data["wait_sec"] = round(now - created) if created else 0
    data["duration_sec"] = round(updated - created) if created else 0

    # DAG 关系：展开依赖链和子任务链
    data["_dag_parents"] = []
    data["_dag_children"] = []
    for dep_id in data.get("depends_on", []):
        dep_path = tracker._tasks_dir() / f"{dep_id}.json"
        if dep_path.exists():
            dep_data = _read_task_file(dep_path)
            if dep_data:
                data["_dag_parents"].append({
                    "id": dep_id,
                    "description": (dep_data.get("description", "") or "")[:80],
                    "status": dep_data.get("status", "unknown"),
                })
    for child_id in data.get("children", []):
        child_path = tracker._tasks_dir() / f"{child_id}.json"
        if child_path.exists():
            child_data = _read_task_file(child_path)
            if child_data:
                data["_dag_children"].append({
                    "id": child_id,
                    "description": (child_data.get("description", "") or "")[:80],
                    "status": child_data.get("status", "unknown"),
                })

    # Trace 文件是否存在
    trace_path = sched_config.TRACE_DIR / f"{task_id}.json"
    data["_has_trace"] = trace_path.exists()

    return jsonify(data)


# ═══════════════════════════════════════════════════════════
# GET /api/tasks/<id>/trace — 交付报告
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/trace")
def api_task_trace(task_id):
    trace_path = sched_config.TRACE_DIR / f"{task_id}.json"
    if not trace_path.exists():
        return jsonify({"error": "Trace 文件不存在（任务未完成或未生成）"}), 404
    try:
        return jsonify(json.loads(trace_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "Trace 文件读取失败"}), 500


# ═══════════════════════════════════════════════════════════
# GET /api/conflicts — 合并冲突列表
# ═══════════════════════════════════════════════════════════

@app.route("/api/conflicts")
def api_conflicts():
    all_tasks = _list_all_tasks()
    conflicts = [t for t in all_tasks if t.get("status") == TaskStatus.CONFLICT_HELD.value]
    result = []
    for t in conflicts:
        result.append({
            "id": t.get("id", t["_filename"]),
            "description": (t.get("description", "") or "")[:120],
            "error": (t.get("error", "") or "")[:300],
            "created_at": t.get("created_at", 0),
            "route_level": t.get("route_level", ""),
        })
    return jsonify({"conflicts": result, "total": len(result)})


# ═══════════════════════════════════════════════════════════
# GET /api/memory — MAGMA 多图记忆查询
# ═══════════════════════════════════════════════════════════

@app.route("/api/memory")
def api_memory_query():
    """MAGMA 完整查询流水线: Stage1→4 (意图分类+R RF锚点+Beam Search遍历+叙事合成)。"""
    query_text = request.args.get("q", "").strip()
    files_str = request.args.get("files", "")
    beam_width = int(request.args.get("beam", 3))
    max_hops = int(request.args.get("hops", 3))

    try:
        from scheduler import memory as mem_mod

        files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else None
        result = mem_mod.query(query_text or "", files=files,
                               beam_width=beam_width, max_hops=max_hops)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"记忆查询失败: {e}"}), 500


@app.route("/api/memory/rebuild", methods=["POST"])
def api_memory_rebuild():
    """从 traces/ 重建全部记忆索引。"""
    try:
        from scheduler import memory as mem_mod
        count = mem_mod.rebuild_from_traces()
        return jsonify({"ok": True, "indexed": count})
    except Exception as e:
        return jsonify({"error": f"重建失败: {e}"}), 500


# ═══════════════════════════════════════════════════════════
# GET /api/agents — Agent 配置
# ═══════════════════════════════════════════════════════════

@app.route("/api/agents")
def api_agents():
    try:
        raw = disp_mod.load_agents()
        result = {}
        for level, cfgs in raw.items():
            result[level] = []
            for c in cfgs:
                result[level].append({
                    "model": c.get("model", ""),
                    "type": c.get("type", ""),
                    "roles": c.get("roles", []),
                    "max_turns": c.get("max_turns", 0),
                })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"读取 agents.toml 失败: {e}"}), 500


# ═══════════════════════════════════════════════════════════
# POST /api/tasks — 创建任务
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    data = request.get_json(silent=True)
    if not data or not data.get("description", "").strip():
        return jsonify({"error": "缺少 description"}), 400

    desc = data["description"].strip()
    priority = data.get("priority", 0)
    depends_on = data.get("depends_on", [])
    route_level = data.get("route_level", "")

    sched_config.ensure_dirs()
    task = tracker.create(desc, priority=priority, depends_on=depends_on)

    # 如果指定了 route_level 且 route_locked，直接设置
    if route_level and route_level in ("E", "D", "E+"):
        tracker.transition(task.id, TaskStatus.PENDING, route_level=route_level, route_locked=True)

    return jsonify({
        "ok": True,
        "task_id": task.id,
        "description": desc[:120],
    })


# ═══════════════════════════════════════════════════════════
# POST /api/tasks/<id>/rollback — 回滚任务
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/rollback", methods=["POST"])
def api_rollback(task_id):
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return jsonify({"error": "任务不存在"}), 404

    data = _read_task_file(task_path)
    snapshot_id = data.get("snapshot_id", "")
    if not snapshot_id:
        return jsonify({"error": "该任务没有快照，无法回滚"}), 400

    try:
        snap_mod.rollback(snapshot_id)
        tracker.transition(task_id, TaskStatus.ROLLED_BACK,
                           error=f"Web 控制台手动回滚到 snapshot {snapshot_id}")
        return jsonify({"ok": True, "message": f"已回滚到 {snapshot_id}"})
    except Exception as e:
        return jsonify({"error": f"回滚失败: {e}"}), 500


# ═══════════════════════════════════════════════════════════
# POST /api/conflicts/<id>/resolve — 解决合并冲突
# ═══════════════════════════════════════════════════════════

@app.route("/api/conflicts/<task_id>/resolve", methods=["POST"])
def api_resolve_conflict(task_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return jsonify({"error": "任务不存在"}), 404

    task_data = _read_task_file(task_path)
    if task_data.get("status") != TaskStatus.CONFLICT_HELD.value:
        return jsonify({"error": "该任务不在冲突状态"}), 400

    if action == "manual":
        # 标记为人工已解决 → DONE
        tracker.transition(task_id, TaskStatus.DONE,
                           error="Web 控制台手动标记冲突已解决")
        return jsonify({"ok": True, "message": "已标记为已解决"})
    elif action == "abort":
        # 放弃 → ROLLED_BACK
        snapshot_id = task_data.get("snapshot_id", "")
        if snapshot_id:
            try:
                snap_mod.rollback(snapshot_id)
            except Exception:
                pass
        tracker.transition(task_id, TaskStatus.ROLLED_BACK,
                           error="Web 控制台放弃合并")
        return jsonify({"ok": True, "message": "已放弃合并并回滚"})
    else:
        return jsonify({"error": "action 必须是 manual 或 abort"}), 400


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("奇点调度面板已启动 → http://127.0.0.1:5050")
    app.run(debug=True, host="0.0.0.0", port=5050)
