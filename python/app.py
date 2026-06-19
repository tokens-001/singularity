# 奇点 Agent 调度平台 — Web 控制台
# Flask 后端：查看调度状态、提交任务、处理合并冲突
# v2: 调度循环后台线程，面板即控制中心

import json
import os
import sys
import time
import threading
from collections import deque
from pathlib import Path

from flask import Flask, Response, render_template, request, jsonify

# ── 加载 .env ──────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

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
from scheduler import orchestrator
from scheduler import project as proj_mod
from scheduler.project import Phase
from scheduler import api_store
from scheduler.log import info as _log_info, warn as _log_warn
from scheduler import model_registry

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

# 可选认证: QIDIAN_AUTH=1 时启用
_AUTH_ENABLED = os.environ.get("QIDIAN_AUTH") == "1"
if _AUTH_ENABLED:
    from scheduler._auth import get_auth
    _admin = get_auth().bootstrap()
    _log_info("auth", f"认证已启用, admin token: {_admin.token[:8]}...")

# ═══════════════════════════════════════════════════════════
# 调度循环后台线程
# ═══════════════════════════════════════════════════════════

_loop_thread: threading.Thread | None = None
_loop_stop: threading.Event = threading.Event()
_loop_concurrent: int = 1
_loop_events: deque = deque(maxlen=50)  # 最近 50 个事件
_loop_running: bool = False
_loop_lock = threading.Lock()
_sse_clients: list = []  # SSE 连接的客户端队列


def _loop_worker():
    """后台调度循环：持续取队 → 执行，面板可随时停止。"""
    global _loop_running
    import signal

    sched_config.ensure_dirs()
    agents = disp_mod.load_agents()

    recovered = tracker.recover()
    if recovered:
        _push_event("system", f"恢复 {recovered} 个中断任务")

    _log_info("loop", "scheduler loop started")
    _push_event("system", "loop started")
    idle_ticks = 0

    while not _loop_stop.is_set():
        try:
            results = orchestrator.run_queue(agents, max_concurrent=_loop_concurrent)
            count = len(results) if results else 0

            if count == 0:
                idle_ticks += 1
                if idle_ticks == 1:
                    _push_event("idle", "队列空，等待新任务...")
                # 空转时也推送状态(低频)
                if idle_ticks % 5 == 0:
                    _sse_broadcast("heartbeat", "", time.time())
                time.sleep(3)
            else:
                idle_ticks = 0
                for tid, reason, validation in results:
                    t = tracker._read(tid)
                    level = t.route_level if t else "?"
                    verdict = getattr(validation, "action", "?")
                    _push_event("task", f"[{tid[:8]}] level={level} {verdict}: {reason}")
                    # 任务失败时推送桌面通知
                    if verdict != "pass":
                        try:
                            import subprocess, sys
                            subprocess.run([
                                "osascript", "-e",
                                f'display notification "任务 {tid[:8]} {verdict}" with title "奇点调度"'
                            ], capture_output=True, timeout=3)
                        except Exception:
                            pass

                # MAGMA 慢通道整合
                try:
                    added = orchestrator.consolidate_memory()
                    if added:
                        _push_event("memory", f"慢通道: +{added} 条隐含因果边")
                except Exception:
                    pass

                # 项目工作流推进: 检查已完成的任务是否属于某个项目
                try:
                    for tid, reason, validation in results:
                        for proj in proj_mod.recover_all():
                            if tid in proj.task_ids and proj.phase in (Phase.EXECUTING, Phase.GATE3):
                                # 检查是否所有子任务完成 → 推进到 Gate3
                                all_done = True
                                for tid2 in proj.task_ids:
                                    t = tracker._read(tid2)
                                    if t and t.status not in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK):
                                        all_done = False
                                        break
                                if all_done and proj.phase == Phase.EXECUTING:
                                    proj.phase = Phase.GATE3
                                    proj_mod.save(proj)
                                    _push_event("workflow", f"项目 {proj.name[:20]}: 执行完成 → Gate3")
                except Exception:
                    pass
        except Exception as e:
            _push_event("error", f"loop error: {e}")
            time.sleep(5)

    _push_event("system", "loop stopped")
    _loop_running = False


def _push_event(kind: str, msg: str):
    ts = time.time()
    _loop_events.appendleft({"kind": kind, "msg": msg, "ts": ts})
    _sse_broadcast(kind, msg, ts)


def _sse_broadcast(kind: str, msg: str, ts: float = None):
    """向所有 SSE 客户端推送事件。"""
    if ts is None:
        ts = time.time()
    data = json.dumps({"kind": kind, "msg": msg, "ts": ts})
    dead = []
    for q in _sse_clients:
        try:
            q.append(data)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def start_loop(concurrent: int = 1):
    global _loop_thread, _loop_stop, _loop_concurrent, _loop_running
    with _loop_lock:
        if _loop_running:
            return False
        _loop_stop.clear()
        _loop_concurrent = concurrent
        _loop_running = True
        _loop_thread = threading.Thread(target=_loop_worker, daemon=True)
        _loop_thread.start()
        return True


def stop_loop():
    global _loop_stop, _loop_running
    with _loop_lock:
        if not _loop_running:
            return False
        _loop_stop.set()
        return True


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
    """扫描 tasks 目录，返回全部任务 dict 列表（按创建时间倒序）。

    2秒 TTL 缓存: /api/tasks 被前端每秒轮询时避免重复读盘。
    任何写操作 (create/transition/delete) 通过 _cache_invalidate() 清缓存。
    """
    from scheduler._cache import task_cache

    tasks_dir = tracker._tasks_dir()
    if not tasks_dir.exists():
        return []

    # 缓存 key = 目录 mtime (文件有增删改时自动失效)
    cache_key = f"all_tasks_{int(tasks_dir.stat().st_mtime)}"
    cached = task_cache.get(cache_key)
    if cached is not None:
        return cached

    tasks = []
    for p in sorted(tasks_dir.glob("*.json"), reverse=True):
        data = _read_task_file(p)
        if data:
            data["_filename"] = p.stem
            tasks.append(data)

    task_cache.set(cache_key, tasks)
    return tasks


def _invalidate_task_cache() -> None:
    """任务写操作后调用，清缓存。"""
    from scheduler._cache import task_cache
    task_cache.invalidate()


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
# Loop Control API — 面板即控制中心
# ═══════════════════════════════════════════════════════════

@app.route("/api/loop/start", methods=["POST"])
def api_loop_start():
    data = request.get_json(silent=True) or {}
    concurrent = int(data.get("concurrent", 1))
    ok = start_loop(concurrent)
    return jsonify({"ok": ok, "running": _loop_running, "concurrent": _loop_concurrent})


@app.route("/api/loop/stop", methods=["POST"])
def api_loop_stop():
    ok = stop_loop()
    return jsonify({"ok": ok, "running": _loop_running})


@app.route("/api/loop/status")
def api_loop_status():
    events = list(_loop_events)[:20]
    return jsonify({
        "running": _loop_running,
        "concurrent": _loop_concurrent,
        "events": [{"kind": e["kind"], "msg": e["msg"], "ts": e["ts"]} for e in events],
    })


# ═══════════════════════════════════════════════════════════
# GET /api/status — 聚合状态 (含 loop 信息)
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
        "loop_running": _loop_running,
        "loop_concurrent": _loop_concurrent,
    })


# ═══════════════════════════════════════════════════════════
# POST /api/cleanup — 清理残留 (心跳/任务/缓存)
# ═══════════════════════════════════════════════════════════

@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    import shutil
    cleaned = {"heartbeats": 0, "tasks": 0}
    try:
        from scheduler.witness import _heartbeat_dir
        hb_dir = _heartbeat_dir()
        if hb_dir.exists():
            for f in hb_dir.glob("*.json"):
                f.unlink()
                cleaned["heartbeats"] += 1
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # 清任务缓存
    try:
        _invalidate_task_cache()
    except Exception:
        pass
    return jsonify({"ok": True, "cleaned": cleaned})


# ═══════════════════════════════════════════════════════════
# GET /api/token-usage — Token 消耗 & 预算
# ═══════════════════════════════════════════════════════════

@app.route("/api/token-usage")
def api_token_usage():
    try:
        from scheduler._token_budget import get_usage_stats
        return jsonify(get_usage_stats())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/token-budget", methods=["PUT"])
def api_token_budget():
    try:
        from scheduler._token_budget import get_budget
        data = request.get_json(force=True) or {}
        daily = float(data.get("daily", 0))
        monthly = float(data.get("monthly", 0))
        get_budget().set_budget(daily=daily, monthly=monthly)
        return jsonify({"ok": True, "daily": daily, "monthly": monthly})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ═══════════════════════════════════════════════════════════
# GET /api/perf — 性能分析
# ═══════════════════════════════════════════════════════════

@app.route("/api/perf")
def api_perf():
    try:
        from scheduler._profiler import get_perf_stats
        return jsonify(get_perf_stats())
    except Exception as e:
        return jsonify({"error": str(e)})


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
# GET /api/tasks/<id>/trace — 交付报告 (支持 ?section= / ?format=)
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/trace")
def api_task_trace(task_id):
    """支持 ?section=route|pre_search|validation，按需取决策证据。"""
    trace_path = sched_config.TRACE_DIR / f"{task_id}.json"
    if not trace_path.exists():
        return jsonify({"error": "Trace 文件不存在"}), 404
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
        section = request.args.get("section", "")
        fmt = request.args.get("format", "")

        if fmt == "md":
            from scheduler.neijinglu import DeliveryReport, format_report
            report = DeliveryReport.from_dict(data)
            return format_report(report), 200, {"Content-Type": "text/plain; charset=utf-8"}

        if section == "route":
            route = data.get("route", {})
            return jsonify({
                "level": route.get("level", "?"),
                "gate_required": route.get("gate_required", False),
                "task_type": route.get("task_type", "default"),
                "matched_signals": route.get("matched_signals", []),
            })
        elif section == "pre_search":
            ps = data.get("pre_search", {})
            return jsonify({
                "skipped": ps.get("skipped", True),
                "reason": ps.get("reason", ""),
                "top_decisions": ps.get("top_decisions", []),
                "memory": ps.get("memory", {}),
            })
        elif section == "validation":
            val = data.get("validation", {})
            return jsonify({
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
            })
        return jsonify(data)
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "Trace 文件读取失败"}), 500


# ═══════════════════════════════════════════════════════════
# GET /api/tasks/<id>/timeline — 任务流转时间线
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/timeline")
def api_task_timeline(task_id):
    """从 task 文件 + trace + heartbeat 重建状态流转时间线。"""
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return jsonify({"error": "任务不存在"}), 404

    task_data = _read_task_file(task_path)
    if not task_data:
        return jsonify({"error": "读取失败"}), 500

    timeline = []
    status = task_data.get("status", "pending")
    created_at = task_data.get("created_at", 0)
    updated_at = task_data.get("updated_at", created_at)

    # Step 1: 创建
    timeline.append({
        "from": None, "to": "pending",
        "timestamp": created_at,
        "meta": {},
    })

    # Step 2: 如果已被路由，添加 routed 节点
    route_level = task_data.get("route_level", "")
    if status not in ("pending",) and route_level:
        timeline.append({
            "from": "pending", "to": "routed",
            "timestamp": task_data.get("routed_at", updated_at),
            "meta": {
                "route_level": route_level,
                "route_gate": task_data.get("route_gate", False),
                "route_type": task_data.get("route_type", "default"),
            },
        })

    # Step 3: dispatched (v3)
    if status in ("dispatched", "running", "validating", "done", "failed", "rolled_back", "decomposed", "conflict_held"):
        timeline.append({
            "from": "routed", "to": "dispatched",
            "timestamp": updated_at,
            "meta": {},
        })

    # Step 4: running
    if task_data.get("snapshot_id"):
        timeline.append({
            "from": "dispatched", "to": "running",
            "timestamp": updated_at,
            "meta": {"snapshot_id": task_data.get("snapshot_id", "")},
        })

    # Step 5: 终态
    if status in ("done", "failed", "rolled_back", "decomposed", "conflict_held"):
        prev = "validating" if status in ("done", "failed") else "running"
        meta = {}
        if status == "failed":
            meta["error"] = (task_data.get("error", "") or "")[:200]
        if status == "rolled_back":
            meta["rolled_back"] = True
        timeline.append({
            "from": prev, "to": status,
            "timestamp": updated_at,
            "meta": meta,
        })

    # Step 6: 补充 trace 数据（如果有）
    trace_path = sched_config.TRACE_DIR / f"{task_id}.json"
    if trace_path.exists():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            timeline.append({
                "from": None, "to": "_trace",
                "timestamp": updated_at,
                "meta": {
                    "route": trace.get("route", {}).get("matched_signals", []),
                    "elapsed": trace.get("elapsed"),
                    "token_count": trace.get("token_count"),
                    "changed_files": trace.get("changed_files", []),
                    "validation_verdict": trace.get("validation", {}).get("verdict"),
                    "pre_search_escalated": trace.get("pre_search", {}).get("escalated"),
                },
            })
        except Exception:
            pass

    return jsonify({
        "task_id": task_id,
        "current_status": status,
        "timeline": timeline,
    })


# ═══════════════════════════════════════════════════════════
# POST /api/tasks/<id>/hold — 暂扣任务
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/hold", methods=["POST"])
def api_hold_task(task_id):
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return jsonify({"error": "任务不存在"}), 404
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")
    task = tracker._read(task_id)
    if task is None:
        return jsonify({"error": "任务读取失败"}), 500
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return jsonify({"error": f"当前状态 {task.status.value} 不支持扣留"}), 400
    tracker.transition(task_id, task.status, held=True, held_reason=reason)
    return jsonify({"ok": True, "held": True, "reason": reason})


# ═══════════════════════════════════════════════════════════
# POST /api/tasks/<id>/release — 释放扣留任务
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/release", methods=["POST"])
def api_release_task(task_id):
    task = tracker._read(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    if not task.held:
        return jsonify({"error": "任务未被扣留"}), 400
    tracker.transition(task_id, task.status, held=False, held_reason="")
    return jsonify({"ok": True, "held": False})


# ═══════════════════════════════════════════════════════════
# POST /api/tasks/<id>/override-route — 覆盖路由级别
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/override-route", methods=["POST"])
def api_override_route(task_id):
    data = request.get_json(silent=True) or {}
    level = data.get("level", "")
    locked = data.get("locked", True)
    if level not in ("E", "D", "E+"):
        return jsonify({"error": "level 必须是 E / D / E+"}), 400
    task = tracker._read(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return jsonify({"error": f"当前状态 {task.status.value} 不支持覆盖路由"}), 400
    tracker.transition(task_id, task.status, route_level=level, route_locked=locked)
    return jsonify({"ok": True, "route_level": level, "locked": locked})


# ═══════════════════════════════════════════════════════════
# POST /api/tasks/<id>/cancel — 取消任务
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    task = tracker._read(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED,
                       TaskStatus.ROLLED_BACK, TaskStatus.DECOMPOSED):
        return jsonify({"error": f"终态任务 {task.status.value} 不可取消"}), 400

    sched_config.ensure_dirs()
    if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
        cancel_path = sched_config.CANCEL_DIR / f"{task_id}.json"
        cancel_path.write_text(json.dumps({
            "task_id": task_id, "cancelled_at": time.time(),
        }), encoding="utf-8")
        return jsonify({"ok": True, "message": "已发送取消信号，将在当前 turn 结束后生效"})
    else:
        tracker.transition(task_id, TaskStatus.FAILED, error="用户手动取消")
        return jsonify({"ok": True, "message": "已取消"})


@app.route("/api/tasks/<task_id>/delete", methods=["POST"])
def api_delete_task(task_id):
    """彻底删除任务文件。"""
    sched_config.ensure_dirs()
    deleted = 0
    for d in [sched_config.CANCEL_DIR, sched_config.TRACE_DIR, tracker._tasks_dir()]:
        p = d / f"{task_id}.json"
        try:
            if p.exists(): p.unlink(); deleted += 1
        except Exception: pass
    if deleted: return jsonify({"ok": True, "message": f"已删除 {deleted} 个文件"})
    return jsonify({"error": "任务文件不存在"}), 404


@app.route("/api/tasks/<task_id>/retry", methods=["POST"])
def api_retry_task(task_id):
    """重试失败任务。"""
    task = tracker._read(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    if task.status not in (TaskStatus.FAILED, TaskStatus.ROLLED_BACK):
        return jsonify({"error": f"当前状态 {task.status.value} 不支持重试"}), 400
    tracker.transition(task_id, TaskStatus.PENDING, error="", retry_count=0)
    return jsonify({"ok": True, "new_status": "pending"})


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


@app.route("/api/memory/chain/<task_id>")
def api_memory_chain(task_id):
    """因果链查询: 溯源(up) / 追果(down) / 双向(both)。"""
    direction = request.args.get("direction", "up")
    try:
        from scheduler import memory as mem_mod
        chain = mem_mod.find_causal_chain(task_id, direction=direction)
        return jsonify({"task_id": task_id, "direction": direction, "chain": chain})
    except Exception as e:
        return jsonify({"error": f"因果链查询失败: {e}"}), 500


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
# Project CRUD API
# ═══════════════════════════════════════════════════════════

@app.route("/api/projects", methods=["GET", "POST"])
def api_projects():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not data.get("name", "").strip():
            return jsonify({"error": "缺少 name"}), 400
        p = proj_mod.create(
            name=data["name"].strip(),
            template=data.get("template", "product_dev"),
            description=data.get("description", ""),
            scope=data.get("scope", ""),
            constraints=data.get("constraints", []),
            budget=float(data.get("budget", 5.0)),
        )
        return jsonify({"ok": True, "project": p.to_dict()})
    projects = proj_mod.list_all()
    return jsonify({"projects": [p.to_dict() for p in projects]})


@app.route("/api/projects/<project_id>")
def api_project_detail(project_id):
    p = proj_mod.load(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    return jsonify(p.to_dict())


@app.route("/api/projects/<project_id>/gate-confirm", methods=["POST"])
def api_project_gate(project_id):
    p = proj_mod.load(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    data = request.get_json(silent=True) or {}
    gate_str = data.get("gate", "")
    decision = data.get("decision", "")
    # 如果前端没传 gate，从项目当前 phase 自动推断
    if not gate_str and p.phase.value.startswith("gate"):
        gate_str = p.phase.value
    try:
        gate = Phase(gate_str)
    except ValueError:
        return jsonify({"error": f"无效 gate: {gate_str}, 可选: {[p.value for p in Phase if p.value.startswith('gate')]}"}), 400
    next_phase = p.confirm_gate(gate, decision)
    proj_mod.save(p)
    return jsonify({
        "ok": True, "phase": p.phase.value,
        "next_phase": next_phase.value if next_phase else None,
    })


@app.route("/api/templates")
def api_templates():
    from scheduler.project import TEMPLATES
    return jsonify(TEMPLATES)


# ═══════════════════════════════════════════════════════════
# Workflow API
# ═══════════════════════════════════════════════════════════

from scheduler import workflow as wf_mod

@app.route("/api/projects/<project_id>/run-phase", methods=["POST"])
def api_project_run_phase(project_id):
    """手动触发当前 phase 的执行动作 (后台线程，避免超时)。"""
    p = proj_mod.load(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    import threading
    def _run():
        try:
            agents = disp_mod.load_agents()
        except Exception:
            agents = {}
        p2 = proj_mod.load(project_id)
        if not p2:
            return
        wf_mod.run_phase(p2, agents)
        proj_mod.save(p2)
        _push_event("system", f"项目 {p2.name}: {p2.phase.value}", time.time())

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "phase": p.phase.value, "message": "后台执行中，稍后刷新查看"})


@app.route("/api/projects/<project_id>/start", methods=["POST"])
def api_project_start(project_id):
    """启动项目工作流 (后台线程执行，避免浏览器超时)。"""
    p = proj_mod.load(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    if p.phase != Phase.TEMPLATE:
        return jsonify({"error": f"项目已在 {p.phase.value} 阶段"}), 400
    if not p.description:
        return jsonify({"error": "请先填写需求描述"}), 400

    import threading
    def _run():
        try:
            agents = disp_mod.load_agents()
        except Exception:
            agents = {}
        p2 = proj_mod.load(project_id)
        if not p2:
            return
        wf_mod.start_project_workflow(p2, agents)
        proj_mod.save(p2)
        _push_event("system", f"项目 {p2.name}: {p2.phase.value}", time.time())

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "phase": p.phase.value, "message": "后台执行中，稍后刷新查看"})


# ═══════════════════════════════════════════════════════════
# Patch Apply + Supervisor API
# ═══════════════════════════════════════════════════════════

from scheduler.supervisor import supervise

@app.route("/api/tasks/<task_id>/apply", methods=["POST"])
def api_apply_patch(task_id):
    from scheduler.executors.zhipu_api import ZhipuApiExecutor
    try:
        result = ZhipuApiExecutor.apply_patch(task_id)
        if result.get("error"):
            return jsonify({"ok": False, "error": result["error"]})
        return jsonify({"ok": True, "applied": result["applied"], "failed": result["failed"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════
# P3 保障层: Cost / Lineage / Project Snapshot
# ═══════════════════════════════════════════════════════════

@app.route("/api/projects/<project_id>/cost")
def api_project_cost(project_id):
    p = proj_mod.load(project_id)
    if not p: return jsonify({"error": "项目不存在"}), 404
    cost_rates = {Phase.RESEARCHING: 0.02, Phase.PLANNING: 2.50, Phase.REVIEWING: 1.00}
    phase_levels = {Phase.RESEARCHING: "E", Phase.PLANNING: "D", Phase.REVIEWING: "D"}
    phase = p.phase
    cost = 0
    level = "-"
    if phase == Phase.TEMPLATE:
        # 从start_project_workflow判断下一步
        from scheduler.workflow import _needs_research
        if _needs_research(p):
            cost = cost_rates.get(Phase.RESEARCHING, 0)
            level = phase_levels.get(Phase.RESEARCHING, "-")
    elif phase in cost_rates:
        cost = cost_rates[phase]
        level = phase_levels[phase]
    return jsonify({
        "cost": round(cost, 2),
        "phase": phase.value,
        "level": level,
        "token_spent": p.token_spent,
        "token_budget_total": p.token_budget_total,
        "over_budget": p.over_budget(),
        "remaining": max(0, p.token_budget_total - p.token_spent),
    })

@app.route("/api/projects/<project_id>/lineage")
def api_project_lineage(project_id):
    p = proj_mod.load(project_id)
    if not p: return jsonify({"error": "项目不存在"}), 404
    limit = request.args.get("limit", 50, type=int)
    lineage = p.lineage[-limit:] if limit > 0 else p.lineage
    return jsonify({"total": len(p.lineage), "shown": len(lineage), "entries": lineage})

@app.route("/api/projects/<project_id>/snapshot", methods=["POST"])
def api_project_snapshot(project_id):
    p = proj_mod.load(project_id)
    if not p: return jsonify({"error": "项目不存在"}), 404
    snap = snap_mod.take(f"proj_{project_id}")
    p.add_lineage({"event": "snapshot", "snapshot_id": snap.id, "method": snap.method})
    proj_mod.save(p)
    return jsonify({"ok": True, "snapshot_id": snap.id, "method": snap.method})

# ═══════════════════════════════════════════════════════════
# Conductor API — 自动推进项目流程
# ═══════════════════════════════════════════════════════════

from scheduler import conductor as _conductor

@app.route("/api/projects/<project_id>/auto", methods=["POST"])
def api_conductor_auto(project_id):
    """自动推进一个阶段。"""
    p = proj_mod.load(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    try:
        agents = disp_mod.load_agents()
    except Exception:
        agents = {}

    result = _conductor.auto_advance(project_id, agents)
    return jsonify(result)


@app.route("/api/projects/<project_id>/autopilot", methods=["POST"])
def api_conductor_start_autopilot(project_id):
    """启动后台自动推进（一直推到 done）。"""
    result = _conductor.start_autopilot(project_id)
    return jsonify(result)


@app.route("/api/projects/<project_id>/autopilot", methods=["DELETE"])
def api_conductor_stop_autopilot(project_id):
    """停止后台自动推进。"""
    result = _conductor.stop_autopilot(project_id)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════
# Supervisor API
# ═══════════════════════════════════════════════════════════

from scheduler.supervisor import supervise

@app.route("/api/tasks/<task_id>/supervise", methods=["POST"])
def api_supervise(task_id):
    """对已完成的任务执行 Supervisor 校验。"""
    t = tracker._read(task_id)
    if not t:
        return jsonify({"error": "任务不存在"}), 404

    # 尝试获取关联项目的约束+checklist
    constraints = []
    checklist = []
    for proj in proj_mod.recover_all():
        if task_id in proj.task_ids:
            constraints = proj.constraints_checklist
            if proj.architecture:
                tasks = proj.architecture.get("tasks", [])
                for td in tasks:
                    checklist.append(td.get("acceptance", ""))
            break

    changed = []  # 从 trace 或 task 记录中获取
    trace_path = sched_config.TRACE_DIR / f"{task_id}.json"
    if trace_path.exists():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            changed = trace.get("changed_files", [])
        except Exception:
            pass

    result = supervise(
        task_description=t.description,
        changed_files=changed,
        constraints=constraints,
        checklist=checklist,
        agent_output=getattr(t, "error", "") or "",
        task_id=task_id,
    )
    return jsonify({
        "verdict": result.verdict,
        "checks": {k: {"passed": v.passed, "reason": v.reason}
                    for k, v in result.checks.items()},
        "issues": result.issues,
        "hard_evidence": result.hard_evidence_count,
        "escalate_to_owner": result.soft_escalation,
    })


# ═══════════════════════════════════════════════════════════
# GET /api/agents — Agent 配置
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 奇点奏报 API
# ═══════════════════════════════════════════════════════════

from scheduler import chancellor as chan_mod


@app.route("/api/reports")
def api_reports():
    """列出奏报。?min=alert 只看重要的，?limit=30。"""
    min_sev = request.args.get("min", "routine")
    limit = request.args.get("limit", 30, type=int)
    return jsonify(chan_mod.list_reports(limit=limit, min_severity=min_sev))


@app.route("/api/reports/critical")
def api_reports_critical():
    """最近的关键奏报。"""
    return jsonify(chan_mod.recent_critical())


@app.route("/api/agents")
def api_agents():
    try:
        raw = disp_mod.load_agents()
        custom = disp_mod._load_custom_agents()
        order_map = custom.get("_order", {}) or {}
        result = {}
        for level, cfgs in raw.items():
            # 按用户自定义排序
            rank = {m: i for i, m in enumerate(order_map.get(level, []))}
            sorted_cfgs = sorted(cfgs, key=lambda c: rank.get(c.get("model", ""), 999))
            result[level] = []
            for c in sorted_cfgs:
                result[level].append({
                    "model": c.get("model", ""),
                    "type": c.get("type", ""),
                    "roles": c.get("roles", []),
                    "max_turns": c.get("max_turns", 0),
                    "entry": c.get("entry", ""),
                    "api_key_env": c.get("api_key_env", ""),
                    "default": c.get("default", False),
                    "mode": c.get("mode", ""),
                    "sandbox": c.get("sandbox", ""),
                })
        result["_order"] = order_map
        result["_disabled"] = custom.get("_disabled", {}) or {}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"读取 agents.toml 失败: {e}"}), 500


@app.route("/api/agents", methods=["POST"])
def api_agents_add():
    """添加自定义 agent 到指定层。"""
    data = request.get_json(silent=True)
    if not data or not data.get("model") or not data.get("level"):
        return jsonify({"error": "缺少 model / level"}), 400
    try:
        cfg = disp_mod.add_agent(
            level=data["level"],
            model=data["model"],
            agent_type=data.get("type", "openai-agent"),
            entry=data.get("entry", ""),
            api_key_env=data.get("api_key_env", ""),
            max_turns=data.get("max_turns", 5),
            roles=data.get("roles", []),
            sandbox=data.get("sandbox", "worktree"),
            mode=data.get("mode", ""),
            request_template=data.get("request_template"),
        )
        return jsonify({"ok": True, "agent": cfg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents/<level>/<model>", methods=["PUT"])
def api_agents_update(level, model):
    """更新 agent 配置 (default 等)。"""
    data = request.get_json(silent=True) or {}
    try:
        cfg = disp_mod.update_agent(level, model, data)
        return jsonify({"ok": True, "agent": cfg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents/<level>/<model>", methods=["DELETE"])
def api_agents_remove(level, model):
    """删除自定义 agent。"""
    try:
        ok = disp_mod.remove_agent(level, model)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
# API 库 & 模型库 & Agent 编组
# ═══════════════════════════════════════════════════════════

@app.route("/api/api-store")
def api_store_list():
    """列出所有 API 条目。"""
    try:
        entries = api_store.list_all()
        return jsonify({k: {
            "id": v.id, "provider": v.provider, "base_url": v.base_url,
            "api_key_env": v.api_key_env, "status": v.status,
            "notes": v.notes, "available": api_store.is_available(v.id),
            "updated_at": v.updated_at,
        } for k, v in entries.items()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/api-store", methods=["POST"])
def api_store_add():
    """添加或更新 API 条目，自动扫描该厂商的模型列表。"""
    data = request.get_json(silent=True)
    if not data or not data.get("id"):
        return jsonify({"error": "缺少 id"}), 400
    try:
        entry = api_store.add(
            api_id=data["id"],
            provider=data.get("provider", data["id"]),
            base_url=data.get("base_url", ""),
            api_key_env=data.get("api_key_env", ""),
            notes=data.get("notes", ""),
        )
        # 自动扫描模型
        scanned = []
        try:
            models = api_store.scan_models(data["id"])
            for m in models:
                api_store.save_custom_model(
                    m["id"], m["provider"], m.get("display", m["id"]))
                scanned.append(m["id"])
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "entry": entry.to_dict(),
            "scanned_models": scanned,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/api-store/<api_id>", methods=["DELETE"])
def api_store_remove(api_id):
    """删除 API 条目。"""
    try:
        ok = api_store.remove(api_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/api-store/<api_id>/status", methods=["PUT"])
def api_store_status(api_id):
    """更新 API 状态。"""
    data = request.get_json(silent=True)
    if not data or "status" not in data:
        return jsonify({"error": "缺少 status"}), 400
    try:
        entry = api_store.set_status(api_id, data["status"],
                                      data.get("notes", ""))
        if not entry:
            return jsonify({"error": f"API {api_id} 不存在"}), 404
        return jsonify({"ok": True, "entry": entry.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 认证 & 用户管理 (QIDIAN_AUTH=1 时启用)
# ═══════════════════════════════════════════════════════════

@app.route("/api/auth/status")
def api_auth_status():
    from scheduler._auth import get_auth
    users = get_auth().list_users()
    return jsonify({"enabled": _AUTH_ENABLED, "users": users})


@app.route("/api/auth/bootstrap", methods=["POST"])
def api_auth_bootstrap():
    from scheduler._auth import get_auth
    admin = get_auth().bootstrap()
    return jsonify({"ok": True, "user": admin.to_dict(),
                    "message": "Admin token — 请保存: " + admin.token})


@app.route("/api/auth/users", methods=["POST"])
def api_auth_add_user():
    if _AUTH_ENABLED:
        from scheduler._auth import require_auth
        user, err = require_auth(request)
        if err:
            return jsonify({"error": err}), 401
        if not user.can_manage:
            return jsonify({"error": "需要 admin 权限"}), 403
    from scheduler._auth import get_auth
    data = request.get_json(silent=True) or {}
    uid = data.get("id", "").strip()
    name = data.get("name", "").strip()
    role = data.get("role", "viewer")
    if not uid:
        return jsonify({"error": "需要 id"}), 400
    try:
        u = get_auth().add_user(uid, name, role)
        return jsonify({"ok": True, "user": u.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/users/<user_id>", methods=["DELETE"])
def api_auth_remove_user(user_id):
    if _AUTH_ENABLED:
        from scheduler._auth import require_auth
        user, err = require_auth(request)
        if err:
            return jsonify({"error": err}), 401
        if not user.can_manage:
            return jsonify({"error": "需要 admin 权限"}), 403
    from scheduler._auth import get_auth
    if get_auth().remove_user(user_id):
        return jsonify({"ok": True})
    return jsonify({"error": "用户不存在"}), 404


@app.route("/api/models")
def api_models():
    """列出所有模型及其能力。"""
    try:
        models = model_registry.load_models()
        return jsonify({mid: {
            "id": m.id, "provider": m.provider, "display": m.display,
            "tiers": m.tiers, "speed": m.speed, "cost": m.cost,
            "reasoning": m.reasoning, "max_turns": m.max_turns,
            "strengths": m.strengths, "notes": m.notes,
            "api_available": api_store.is_available(m.provider),
        } for mid, m in models.items()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models/tier/<tier>")
def api_models_for_tier(tier):
    """获取某一层可用的模型列表 (含 API 状态)。"""
    try:
        models = model_registry.for_tier(tier, available_only=False)
        return jsonify([{
            "id": m.id, "provider": m.provider, "display": m.display,
            "cost": m.cost, "speed": m.speed,
            "api_available": api_store.is_available(m.provider),
        } for m in models])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["POST"])
def api_models_add():
    """添加自定义模型。"""
    data = request.get_json(silent=True)
    if not data or not data.get("id"):
        return jsonify({"error": "缺少 id"}), 400
    try:
        m = model_registry.add_model(
            model_id=data["id"],
            provider=data.get("provider", ""),
            display=data.get("display", ""),
            tiers=data.get("tiers", ["E"]),
            speed=data.get("speed", "medium"),
            cost=data.get("cost", "standard"),
            reasoning=data.get("reasoning", False),
            max_turns=data.get("max_turns", 5),
            notes=data.get("notes", ""),
        )
        return jsonify({"ok": True, "model": m.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models/<model_id>", methods=["DELETE"])
def api_models_remove(model_id):
    """删除自定义模型。"""
    try:
        ok = model_registry.remove_model(model_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models/<model_id>", methods=["PUT"])
def api_models_update(model_id):
    """更新模型属性 (层级/速度/价格等)。"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "缺少更新数据"}), 400
    try:
        m = model_registry.add_model(
            model_id=model_id,
            provider=data.get("provider", ""),
            display=data.get("display", ""),
            tiers=data.get("tiers", None),
            speed=data.get("speed", ""),
            cost=data.get("cost", ""),
            reasoning=data.get("reasoning", None),
            max_turns=data.get("max_turns", 0),
            notes=data.get("notes", ""),
        )
        return jsonify({"ok": True, "model": m.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<project_id>/lineup")
def api_project_lineup(project_id):
    """获取项目的 Agent 编组。"""
    try:
        p = proj_mod.load(project_id)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        return jsonify({"lineup": p.agent_lineup})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<project_id>/lineup", methods=["PUT"])
def api_project_lineup_update(project_id):
    """更新项目的 Agent 编组。"""
    data = request.get_json(silent=True)
    if not data or "lineup" not in data:
        return jsonify({"error": "缺少 lineup"}), 400
    try:
        p = proj_mod.load(project_id)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        p.agent_lineup = data["lineup"]
        proj_mod.save(p)
        return jsonify({"ok": True, "lineup": p.agent_lineup})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
# SSE 事件流 — 替代轮询,服务器主动推送状态变更
# ═══════════════════════════════════════════════════════════

@app.route("/api/events")
def api_sse_events():
    """SSE 端点: 服务器主动推送调度事件。"""
    import queue
    q = queue.Queue()
    _sse_clients.append(q)

    def generate():
        # 初始状态
        try:
            counts = witness._count_by_status()
            events_data = list(_loop_events)[:20]
            initial = json.dumps({
                "kind": "init",
                "counts": counts,
                "running_total": sum(witness._heartbeat_task_levels().values()),
                "running": _loop_running,
                "events": events_data,
            })
            yield f"data: {initial}\n\n"
        except Exception:
            pass

        while True:
            try:
                data = q.get(timeout=15)
                yield f"data: {data}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"
            except GeneratorExit:
                break

    try:
        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    finally:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


# ═══════════════════════════════════════════════════════════
# Judge Monitor API — 裁判监控
# ═══════════════════════════════════════════════════════════

@app.route("/api/judge-monitor")
def api_judge_monitor():
    """裁判监控统计：通过率、模型偏差、异常检测。"""
    try:
        from scheduler.orchestrator import _get_judge_monitor
        jm = _get_judge_monitor()
        return jsonify(jm.get_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
# Model Profile API — 模型画像
# ═══════════════════════════════════════════════════════════

@app.route("/api/model-profile")
def api_model_profile():
    """模型画像全量数据。"""
    try:
        from scheduler.model_profile import ProfileStore
        from scheduler import config as sched_config
        store = ProfileStore(sched_config.QIDIAN_DIR / "model_profile.json")
        store.load()
        stats = {}
        for (model, tt), s in store._stats.items():
            key = f"{model}/{tt}"
            stats[key] = s.to_dict()
        return jsonify({"profiles": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/model-profile/pattern")
def api_model_profile_pattern():
    """模式画像：按 (task_type, template_id) 的模型排名。"""
    task_type = request.args.get("task_type", "default")
    template_id = request.args.get("template_id", "default")
    try:
        from scheduler.model_profile import ProfileStore
        from scheduler import config as sched_config
        store = ProfileStore(sched_config.QIDIAN_DIR / "model_profile.json")
        store.load()
        ranked = store.rank_by_pattern(task_type, template_id)
        return jsonify({
            "task_type": task_type,
            "template_id": template_id,
            "ranking": ranked,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """健康检查端点 — Docker/monitoring 用。"""
    import shutil
    disk = shutil.disk_usage(str(sched_config.QIDIAN_DIR))
    return jsonify({
        "status": "ok",
        "disk_free_mb": disk.free // (1024*1024),
        "loop_running": _loop_running,
        "sse_clients": len(_sse_clients),
        "projects": len(proj_mod.list_all()),
    })


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("奇点调度面板已启动 → http://127.0.0.1:5050")
    app.run(debug=False, host="0.0.0.0", port=5050)
