from __future__ import annotations
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
from scheduler import dispatcher as disp_mod
from scheduler import witness
from scheduler import orchestrator
from scheduler import project as proj_mod
from scheduler.project import Phase
from scheduler.log import info as _log_info, warn as _log_warn
from scheduler import mcp as mcp_mod

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 安全加固：拒绝 >2MB 的请求体
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

# ── 安全限制 ──────────────────────────────────────────
_MAX_CONCURRENT = 8          # loop concurrent 上限
_MAX_SSE_CLIENTS = 20        # SSE 同时连接上限
_MAX_TASK_DESC_LEN = 8000    # 任务描述最大字符数
_MAX_PROJECT_NAME_LEN = 200  # 项目名最大字符数
_MAX_DEPENDS_ON = 50         # depends_on 最大依赖数
_MAX_CONSTRAINTS = 50        # constraints 最大条数
_MAX_TURNS = 50              # agent max_turns 上限
_MIN_BUDGET = 0.01           # 最小项目预算
_MAX_BUDGET = 100000.0       # 最大项目预算

# ── 枚举校验集 ────────────────────────────────────────
_VALID_LEVELS = frozenset({"E", "E+", "D"})
_VALID_AGENT_TYPES = frozenset({"openai-agent", "claude-cli", "zhipu-api"})
_VALID_SANDBOXES = frozenset({"worktree", "inline", "none"})
_VALID_ROLES = frozenset({"admin", "operator", "viewer"})
_VALID_SPEEDS = frozenset({"fast", "medium", "slow"})
_VALID_COSTS = frozenset({"budget", "standard", "premium"})
_VALID_TIERS = frozenset({"E", "E+", "D"})
_VALID_TEMPLATES = frozenset({"product_dev", "bug_fix", "refactor", "agent_dev"})
_VALID_DECISIONS = frozenset({"approved", "rejected"})

import re as _re_valid
_TASK_ID_RE = _re_valid.compile(r"^\d{13,20}$")  # task_id 格式：13-20 位数字

def _validate_task_id(task_id: str) -> bool:
    """校验 task_id 格式，防止路径穿越。"""
    return bool(_TASK_ID_RE.match(task_id))


@app.before_request
def _guard_task_id():
    """安全加固：校验 URL 中 task_id 参数格式（防止路径穿越）。"""
    if request.endpoint in (
        "api_task_detail", "api_cancel_task", "api_delete_task",
        "api_retry_task", "api_hold_task", "api_release_task",
        "api_override_route", "api_rollback",
        "api_task_trace", "api_task_timeline", "api_apply_patch",
        "api_supervise", "api_resolve_conflict",
        "api_memory_chain",
    ):
        tid = request.view_args.get("task_id", "") if request.view_args else ""
        if tid and not _validate_task_id(tid):
            return jsonify({"error": "非法的 task_id 格式"}), 400


_PROJECT_ID_RE = _re_valid.compile(r"^[a-zA-Z0-9_-]{1,64}$")

def _validate_project_id(project_id: str) -> bool:
    """校验 project_id 格式，防止路径穿越。"""
    return bool(_PROJECT_ID_RE.match(project_id))


@app.before_request
def _guard_project_id():
    """安全加固：校验 URL 中 project_id 参数格式（防止路径穿越）。"""
    if request.view_args:
        pid = request.view_args.get("project_id", "")
        if pid and not _validate_project_id(pid):
            return jsonify({"error": "非法的 project_id 格式"}), 400


# 可选认证: QIDIAN_AUTH=1 时启用
_AUTH_ENABLED = os.environ.get("QIDIAN_AUTH") == "1"
if _AUTH_ENABLED:
    from scheduler._auth import get_auth, require_auth, require_write
    _admin = get_auth().bootstrap()
    _log_info("auth", f"认证已启用, admin token: {_admin.token[:8]}...")

# 无需认证的公开端点
_PUBLIC_ENDPOINTS = {
    "api_auth_status", "api_auth_bootstrap",
    "api_status", "api_loop_status",
    "index",
}

# 只读端点（viewer 可访问的 GET 端点）
_READONLY_ENDPOINTS = {
    "api_token_usage", "api_token_budget",
    "api_perf", "api_tasks", "api_task_detail",
    "api_task_trace", "api_task_timeline",
    "api_conflicts", "api_memory", "api_memory_chain",
    "api_projects", "api_project_detail",
    "api_templates", "api_project_cost", "api_project_lineage",
    "api_reports", "api_reports_critical",
    "api_agents", "api_api_store",
    "api_models", "api_models_tier",
    "api_memory_rebuild",
    "api_project_lineup",
    "api_skills", "api_agent_skills",
}


@app.before_request
def _guard_auth():
    """鉴权钩子：QIDIAN_AUTH=1 时对所有 /api/ 端点要求认证。"""
    if not _AUTH_ENABLED:
        return None
    # 公开端点跳过
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    # 只对 /api/ 路径鉴权
    if not request.path.startswith("/api/"):
        return None
    # 非 API 端点跳过
    if request.endpoint is None:
        return None

    from scheduler._auth import require_auth, require_write

    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        user, err = require_write(request)
    else:
        # GET / HEAD / OPTIONS
        if request.endpoint in _READONLY_ENDPOINTS:
            user, err = require_auth(request)
        else:
            user, err = require_write(request)

    if err:
        return jsonify({"error": err}), 401
    # 注入 user 到 g，供路由内使用
    from flask import g
    g.auth_user = user
    return None


# ── Rate Limiting：滑动窗口限流 ──────────────────────────
# 默认：每 IP 每分钟 60 请求，突变端点 (POST/PUT/DELETE) 每分钟 30 请求
_RATE_WINDOW = 60  # 秒
_RATE_LIMIT_READ = 60   # GET 每窗口最大请求数
_RATE_LIMIT_WRITE = 30  # POST/PUT/DELETE 每窗口最大请求数
_RATE_BUCKETS: dict[str, list[float]] = {}  # ip → [timestamps]
_MAX_BUCKETS = 10000  # 防止内存无限增长
_RATE_LOCK = threading.Lock()  # 保护 _RATE_BUCKETS 的并发读写


def _cleanup_rate_buckets():
    """定期清理过期 bucket。"""
    now = time.time()
    stale_ips = []
    for ip, timestamps in _RATE_BUCKETS.items():
        # 移除窗口外的旧时间戳
        _RATE_BUCKETS[ip] = [t for t in timestamps if now - t < _RATE_WINDOW]
        if not _RATE_BUCKETS[ip]:
            stale_ips.append(ip)
    for ip in stale_ips:
        del _RATE_BUCKETS[ip]
    # 如果 bucket 总数超标，删最旧的
    if len(_RATE_BUCKETS) > _MAX_BUCKETS:
        oldest = sorted(_RATE_BUCKETS.keys(), key=lambda ip: min(_RATE_BUCKETS[ip]) if _RATE_BUCKETS[ip] else 0)[:100]
        for ip in oldest:
            del _RATE_BUCKETS[ip]


@app.before_request
def _guard_rate_limit():
    """限流钩子：滑动窗口计数，每个 IP 独立限制。"""
    # 非 /api/ 路径不限流
    if not request.path.startswith("/api/"):
        return None
    # 本地回环地址不限流（开发/调试）
    ip = request.remote_addr or "127.0.0.1"
    if ip in ("127.0.0.1", "::1", "localhost"):
        return None

    now = time.time()

    with _RATE_LOCK:
        if ip not in _RATE_BUCKETS:
            _RATE_BUCKETS[ip] = []

        # 清理窗口外旧记录
        window_start = now - _RATE_WINDOW
        _RATE_BUCKETS[ip] = [t for t in _RATE_BUCKETS[ip] if t > window_start]

        # 选择限制阈值
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            limit = _RATE_LIMIT_WRITE
        else:
            limit = _RATE_LIMIT_READ

        if len(_RATE_BUCKETS[ip]) >= limit:
            return jsonify({
                "error": "请求过于频繁",
                "retry_after": _RATE_WINDOW,
                "limit": limit,
                "window": f"{_RATE_WINDOW}s",
            }), 429

        _RATE_BUCKETS[ip].append(now)

        # 定期清理（每 100 请求触发一次）
        trigger_cleanup = sum(len(v) for v in _RATE_BUCKETS.values()) % 100 == 0

    if trigger_cleanup:
        with _RATE_LOCK:
            _cleanup_rate_buckets()

    return None


# ── SSRF 防护：URL 验证 ─────────────────────────────────
import ipaddress
import re as _re_url

# 已知 API 厂商域名（仅允许这些厂商的 API 调用）
_ALLOWED_API_HOSTS = {
    "api.deepseek.com",
    "api.moonshot.cn",
    "open.bigmodel.cn",
    "api.openai.com",
    "api.anthropic.com",
    "dashscope.aliyuncs.com",
    "api.minimax.chat",
    "api.baichuan-ai.com",
    "api.stepfun.com",
    "api.lingyiwanwu.com",
}

def _is_safe_api_url(url: str) -> bool:
    """检查 URL 是否安全（允许的 API 厂商 + 非内网）。"""
    if not url:
        return True  # 空 URL 允许（使用默认值）
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # 禁止内网 IP
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return False
        except ValueError:
            pass  # 不是 IP 地址，继续检查 hostname
        # 只允许已知 API 厂商
        for allowed in _ALLOWED_API_HOSTS:
            if hostname == allowed or hostname.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False

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
                # ── 刷新工具/轮次事件（_exec 推到全局队列）──
                try:
                    events_to_flush = list(orchestrator._pending_sse_events)
                    orchestrator._pending_sse_events.clear()
                    for evt in events_to_flush:
                        _push_event(evt.get("kind", "tool"), evt.get("msg", ""), evt.get("ts"))
                except Exception:
                    pass
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


def _push_event(kind: str, msg: str, ts: float = None):
    if ts is None:
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
    # 仅允许本地来源（安全加固：不再使用 *）
    origin = request.headers.get("Origin", "")
    allowed = (
        not origin
        or origin.startswith("http://localhost")
        or origin.startswith("http://127.0.0.1")
        or origin.startswith("http://0.0.0.0")
    )
    if allowed and origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    elif not origin:
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:5050"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # 安全响应头
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # CSP: 仅允许本站资源 (localhost 工具)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' http://127.0.0.1:* http://localhost:*; "
        "img-src 'self' data:; "
        "font-src 'self'"
    )
    # Rate limit headers (读 _RATE_BUCKETS 加锁防并发竞态)
    ip = request.remote_addr or "127.0.0.1"
    with _RATE_LOCK:
        bucket = list(_RATE_BUCKETS.get(ip, []))
    now = time.time()
    active = len([t for t in bucket if now - t < _RATE_WINDOW])
    limit = _RATE_LIMIT_WRITE if request.method in ("POST", "PUT", "DELETE", "PATCH") else _RATE_LIMIT_READ
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(max(0, limit - active))
    response.headers["X-RateLimit-Reset"] = str(int(now + _RATE_WINDOW))
    return response


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

def _invalidate_task_cache() -> None:
    """任务写操作后调用，清缓存。"""
    from scheduler._cache import task_cache
    task_cache.invalidate()


# ── API handler 层 ──
from scheduler import _api as _api_handler

# ═══════════════════════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/dashboard")
def dashboard():
    from flask import redirect
    return redirect("/static/dashboard.html")


# ═══════════════════════════════════════════════════════════
# Loop Control API — 面板即控制中心
# ═══════════════════════════════════════════════════════════

@app.route("/api/loop/start", methods=["POST"])
def api_loop_start():
    data = request.get_json(silent=True) or {}
    concurrent = min(max(int(data.get("concurrent", 1)), 1), _MAX_CONCURRENT)
    ok = start_loop(concurrent)
    return jsonify({"ok": ok, "running": _loop_running, "concurrent": _loop_concurrent})

@app.route("/api/loop/stop", methods=["POST"])
def api_loop_stop():
    ok = stop_loop()
    return jsonify({"ok": ok, "running": _loop_running})

@app.route("/api/loop/status")
def api_loop_status():
    events = list(_loop_events)[:20]
    return jsonify({"running": _loop_running, "concurrent": _loop_concurrent,
                    "events": [{"kind": e["kind"], "msg": e["msg"], "ts": e["ts"]} for e in events]})

# ═══════════════════════════════════════════════════════════
# 监控
# ═══════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    data, code = _api_handler.status_overview()
    return jsonify(data), code

@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    data, code = _api_handler.cleanup()
    return jsonify(data), code

@app.route("/api/token-usage")
def api_token_usage():
    data, code = _api_handler.token_usage()
    return jsonify(data), code

@app.route("/api/token-budget", methods=["PUT"])
def api_token_budget():
    data = request.get_json(silent=True) or {}
    budget = float(data.get("budget", 0))
    result, code = _api_handler.token_budget_set(budget)
    return jsonify(result), code

@app.route("/api/perf")
def api_perf():
    data, code = _api_handler.perf_stats()
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 任务 CRUD
# ═══════════════════════════════════════════════════════════

@app.route("/api/tasks")
def api_tasks():
    status_filter = request.args.get("status", "")
    level_filter = request.args.get("level", "")
    data, code = _api_handler.task_list(status_filter, level_filter)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>")
def api_task_detail(task_id):
    data, code = _api_handler.task_detail(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/trace")
def api_task_trace(task_id):
    section = request.args.get("section", "")
    fmt = request.args.get("format", "")
    result = _api_handler.task_trace(task_id, section, fmt)
    if len(result) == 3:  # (data, status, headers)
        return result[0], result[1], result[2]
    data, code = result
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/timeline")
def api_task_timeline(task_id):
    data, code = _api_handler.task_timeline(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/hold", methods=["POST"])
def api_hold_task(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.task_hold(task_id, body.get("reason", ""))
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/release", methods=["POST"])
def api_release_task(task_id):
    data, code = _api_handler.task_release(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/override-route", methods=["POST"])
def api_override_route(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.task_override_route(task_id, body.get("level", ""), body.get("locked", True))
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    data, code = _api_handler.task_cancel(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/delete", methods=["POST"])
def api_delete_task(task_id):
    data, code = _api_handler.task_delete(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/retry", methods=["POST"])
def api_retry_task(task_id):
    data, code = _api_handler.task_retry(task_id)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/approval", methods=["POST"])
def api_task_approval(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.task_approval(task_id, body.get("decision", "reject"), body.get("action", ""), _push_event)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/apply", methods=["POST"])
def api_apply_patch(task_id):
    data, code = _api_handler.task_apply(task_id, _push_event)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/rollback", methods=["POST"])
def api_rollback(task_id):
    data, code = _api_handler.task_rollback(task_id, _push_event)
    return jsonify(data), code

@app.route("/api/tasks/<task_id>/supervise", methods=["POST"])
def api_supervise(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.task_supervise(task_id, body, _push_event)
    return jsonify(data), code

@app.route("/api/tasks", methods=["POST"])
def api_task_submit():
    data = request.get_json(silent=True)
    if not data or not data.get("description", "").strip():
        return jsonify({"error": "缺少 description"}), 400
    desc = data["description"].strip()
    if len(desc) > _MAX_TASK_DESC_LEN:
        return jsonify({"error": f"description 不能超过 {_MAX_TASK_DESC_LEN} 字符"}), 400
    try:
        priority = int(data.get("priority", 0))
        if priority < 0 or priority > 100:
            return jsonify({"error": "priority 在 0-100"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "priority 必须是整数"}), 400
    depends_on = data.get("depends_on", [])
    if not isinstance(depends_on, list):
        return jsonify({"error": "depends_on 必须是数组"}), 400
    if len(depends_on) > _MAX_DEPENDS_ON:
        return jsonify({"error": f"depends_on 不能超过 {_MAX_DEPENDS_ON}"}), 400
    route_level = data.get("route_level", "")
    if route_level and route_level not in _VALID_LEVELS:
        return jsonify({"error": f"route_level 必须是 E/E+/D"}), 400
    _invalidate_task_cache()
    result, code = _api_handler.task_submit(desc, priority=priority, depends_on=depends_on,
                                             route_level=route_level, push_event=_push_event)
    return jsonify(result), code

# ═══════════════════════════════════════════════════════════
# 冲突
# ═══════════════════════════════════════════════════════════

@app.route("/api/conflicts")
def api_conflicts():
    data, code = _api_handler.conflict_list()
    return jsonify(data), code

@app.route("/api/conflicts/<task_id>/resolve", methods=["POST"])
def api_resolve_conflict(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.conflict_resolve(task_id, body, _push_event)
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 记忆
# ═══════════════════════════════════════════════════════════

@app.route("/api/memory")
def api_memory_query():
    q = request.args.get("q", "").strip()
    files_str = request.args.get("files", "")
    try:
        beam = int(request.args.get("beam", 3))
        hops = int(request.args.get("hops", 3))
        depth = int(request.args.get("depth", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "beam/hops/depth 必须为整数"}), 400
    data, code = _api_handler.memory_query(q, files_str, beam, hops, depth)
    return jsonify(data), code

@app.route("/api/memory/chain/<task_id>")
def api_memory_chain(task_id):
    data, code = _api_handler.memory_chain(task_id)
    return jsonify(data), code

@app.route("/api/memory/rebuild", methods=["POST"])
def api_memory_rebuild():
    data, code = _api_handler.memory_rebuild()
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 项目
# ═══════════════════════════════════════════════════════════

@app.route("/api/projects", methods=["GET", "POST"])
def api_projects():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "缺少 name"}), 400
        if len(name) > _MAX_PROJECT_NAME_LEN:
            return jsonify({"error": f"项目名不能超过 {_MAX_PROJECT_NAME_LEN} 字符"}), 400
        template = data.get("template", "product_dev")
        if template not in _VALID_TEMPLATES:
            return jsonify({"error": f"template 必须是 {', '.join(sorted(_VALID_TEMPLATES))} 之一"}), 400
        constraints = data.get("constraints", [])
        if not isinstance(constraints, list):
            return jsonify({"error": "constraints 必须是数组"}), 400
        if len(constraints) > _MAX_CONSTRAINTS:
            return jsonify({"error": f"constraints 不能超过 {_MAX_CONSTRAINTS} 条"}), 400
        try:
            budget = float(data.get("budget", 5.0))
        except (TypeError, ValueError):
            return jsonify({"error": "budget 必须是数字"}), 400
        import math
        if math.isnan(budget) or math.isinf(budget):
            return jsonify({"error": "budget 不能是 NaN 或 Infinity"}), 400
        if budget < _MIN_BUDGET or budget > _MAX_BUDGET:
            return jsonify({"error": f"budget 必须在 {_MIN_BUDGET}-{_MAX_BUDGET} 之间"}), 400
        result, code = _api_handler.project_create(
            name, template=template, description=data.get("description", ""),
            scope=data.get("scope", ""), constraints=constraints, budget=budget)
        return jsonify(result), code
    else:
        data, code = _api_handler.project_list()
        return jsonify(data), code

@app.route("/api/projects/<project_id>")
def api_project_detail(project_id):
    data, code = _api_handler.project_detail(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/gate-confirm", methods=["POST"])
def api_project_gate_confirm(project_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.project_gate_confirm(project_id, body.get("gate", ""), body.get("decision", ""))
    return jsonify(data), code

@app.route("/api/projects/<project_id>/run-phase", methods=["POST"])
def api_project_run_phase(project_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.project_run_phase(project_id, body.get("phase", ""), body.get("desc", ""), body.get("agent", ""), _push_event)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/start", methods=["POST"])
def api_project_start(project_id):
    data, code = _api_handler.project_start(project_id, _push_event)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/cost")
def api_project_cost(project_id):
    data, code = _api_handler.project_cost(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/lineage")
def api_project_lineage(project_id):
    data, code = _api_handler.project_lineage(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/snapshot", methods=["POST"])
def api_project_snapshot(project_id):
    data, code = _api_handler.project_snapshot(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/auto", methods=["POST"])
def api_project_auto(project_id):
    data, code = _api_handler.project_auto(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/autopilot", methods=["POST"])
def api_project_autopilot_start(project_id):
    data, code = _api_handler.project_autopilot_start(project_id, _push_event)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/autopilot", methods=["DELETE"])
def api_project_autopilot_stop(project_id):
    data, code = _api_handler.project_autopilot_stop(project_id, _push_event)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/lineup")
def api_project_lineup(project_id):
    data, code = _api_handler.project_lineup_get(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/lineup", methods=["PUT"])
def api_project_lineup_update(project_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.project_lineup_set(project_id, body.get("lineup", {}))
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 模板
# ═══════════════════════════════════════════════════════════

@app.route("/api/templates")
def api_templates():
    data, code = _api_handler.template_list()
    return jsonify(data), code

@app.route("/api/reports")
def api_reports():
    return jsonify({"reports": []})

@app.route("/api/reports/critical")
def api_reports_critical():
    data, code = _api_handler.reports_critical()
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# Agent 管理
# ═══════════════════════════════════════════════════════════

@app.route("/api/agents")
def api_agents():
    data, code = _api_handler.agent_list()
    return jsonify(data), code

@app.route("/api/agents", methods=["POST"])
def api_agents_add():
    data = request.get_json(silent=True)
    if not data or not data.get("model") or not data.get("level"):
        return jsonify({"error": "缺少 model / level"}), 400
    model = data["model"].strip()
    if not model or len(model) > 100:
        return jsonify({"error": "model 不能为空且不超过 100 字符"}), 400
    level = data["level"]
    if level not in _VALID_LEVELS:
        return jsonify({"error": f"level 必须是 E/E+/D 之一"}), 400
    agent_type = data.get("type", "openai-agent")
    if agent_type not in _VALID_AGENT_TYPES:
        return jsonify({"error": f"type 必须是 {', '.join(sorted(_VALID_AGENT_TYPES))} 之一"}), 400
    sandbox = data.get("sandbox", "worktree")
    if sandbox not in _VALID_SANDBOXES:
        return jsonify({"error": f"sandbox 必须是 {', '.join(sorted(_VALID_SANDBOXES))} 之一"}), 400
    try:
        max_turns = int(data.get("max_turns", 5))
        if max_turns < 1 or max_turns > _MAX_TURNS:
            return jsonify({"error": f"max_turns 必须在 1-{_MAX_TURNS} 之间"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "max_turns 必须是整数"}), 400
    entry_url = data.get("entry", "")
    if entry_url and not _is_safe_api_url(entry_url):
        return jsonify({"error": "不允许的 entry URL（仅支持已知 API 厂商域名）"}), 400
    result, code = _api_handler.agent_add(level, model, agent_type, entry_url,
        data.get("api_key_env", ""), max_turns, data.get("roles", []), sandbox,
        data.get("mode", ""), data.get("request_template"))
    return jsonify(result), code

@app.route("/api/agents/<level>/<model>", methods=["PUT"])
def api_agents_update(level, model):
    data = request.get_json(silent=True) or {}
    if data.get("entry") and not _is_safe_api_url(data["entry"]):
        return jsonify({"error": "不允许的 entry URL"}), 400
    if "type" in data and data["type"] not in _VALID_AGENT_TYPES:
        return jsonify({"error": f"type 不合法"}), 400
    if "sandbox" in data and data["sandbox"] not in _VALID_SANDBOXES:
        return jsonify({"error": f"sandbox 不合法"}), 400
    if "max_turns" in data:
        try:
            mt = int(data["max_turns"])
            if mt < 1 or mt > _MAX_TURNS:
                return jsonify({"error": f"max_turns 在 1-{_MAX_TURNS}"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "max_turns 整数"}), 400
    result, code = _api_handler.agent_update(level, model, data)
    return jsonify(result), code

@app.route("/api/agents/<level>/<model>", methods=["DELETE"])
def api_agents_remove(level, model):
    data, code = _api_handler.agent_remove(level, model)
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# API 库
# ═══════════════════════════════════════════════════════════

@app.route("/api/api-store")
def api_store_list():
    data, code = _api_handler.api_store_list()
    return jsonify(data), code

@app.route("/api/api-store", methods=["POST"])
def api_store_add():
    data = request.get_json(silent=True)
    if not data or not data.get("id"):
        return jsonify({"error": "缺少 id"}), 400
    base_url = data.get("base_url", "")
    if base_url and not _is_safe_api_url(base_url):
        return jsonify({"error": "不允许的 base_url"}), 400
    result, code = _api_handler.api_store_add(data["id"], data.get("provider", data["id"]),
        base_url, data.get("api_key_env", ""), data.get("notes", ""))
    return jsonify(result), code

@app.route("/api/api-store/<api_id>", methods=["DELETE"])
def api_store_remove(api_id):
    data, code = _api_handler.api_store_remove(api_id)
    return jsonify(data), code

@app.route("/api/api-store/<api_id>/status", methods=["PUT"])
def api_store_status(api_id):
    body = request.get_json(silent=True)
    if not body or "status" not in body:
        return jsonify({"error": "缺少 status"}), 400
    data, code = _api_handler.api_store_set_status(api_id, body["status"], body.get("notes", ""))
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 认证
# ═══════════════════════════════════════════════════════════

@app.route("/api/auth/status")
def api_auth_status():
    data, code = _api_handler.auth_status()
    return jsonify(data), code

@app.route("/api/auth/bootstrap", methods=["POST"])
def api_auth_bootstrap():
    data, code = _api_handler.auth_bootstrap()
    return jsonify(data), code

@app.route("/api/auth/users", methods=["POST"])
def api_auth_add_user():
    if _AUTH_ENABLED:
        from scheduler._auth import require_auth
        user, err = require_auth(request)
        if err:
            return jsonify({"error": err}), 401
        if not user.can_manage:
            return jsonify({"error": "需要 admin 权限"}), 403
    body = request.get_json(silent=True) or {}
    uid = (body.get("id") or "").strip()
    role = body.get("role", "viewer")
    if role not in _VALID_ROLES:
        return jsonify({"error": f"role 必须是 {', '.join(sorted(_VALID_ROLES))} 之一"}), 400
    if not uid:
        return jsonify({"error": "需要 id"}), 400
    data, code = _api_handler.auth_add_user(uid, (body.get("name") or "").strip(), role)
    return jsonify(data), code

@app.route("/api/auth/users/<user_id>", methods=["DELETE"])
def api_auth_remove_user(user_id):
    if _AUTH_ENABLED:
        from scheduler._auth import require_auth
        user, err = require_auth(request)
        if err:
            return jsonify({"error": err}), 401
        if not user.can_manage:
            return jsonify({"error": "需要 admin 权限"}), 403
    data, code = _api_handler.auth_remove_user(user_id)
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 模型库
# ═══════════════════════════════════════════════════════════

@app.route("/api/models")
def api_models():
    data, code = _api_handler.model_list()
    return jsonify(data), code

@app.route("/api/models/tier/<tier>")
def api_models_tier(tier):
    data, code = _api_handler.model_list_for_tier(tier)
    return jsonify(data), code

@app.route("/api/models", methods=["POST"])
def api_models_add():
    data = request.get_json(silent=True)
    if not data or not data.get("id"):
        return jsonify({"error": "缺少 id"}), 400
    model_id = data["id"].strip()
    if not model_id or len(model_id) > 100:
        return jsonify({"error": "id 不能为空且不超过 100 字符"}), 400
    tiers = data.get("tiers", ["E"])
    if not isinstance(tiers, list) or not tiers:
        return jsonify({"error": "tiers 非空数组"}), 400
    for t in tiers:
        if t not in _VALID_TIERS:
            return jsonify({"error": "tiers 元素 E/E+/D"}), 400
    speed = data.get("speed", "medium")
    if speed not in _VALID_SPEEDS:
        return jsonify({"error": f"speed 必须是 {', '.join(sorted(_VALID_SPEEDS))} 之一"}), 400
    cost = data.get("cost", "standard")
    if cost not in _VALID_COSTS:
        return jsonify({"error": f"cost 必须是 {', '.join(sorted(_VALID_COSTS))} 之一"}), 400
    try:
        max_turns = int(data.get("max_turns", 5))
        if max_turns < 1 or max_turns > _MAX_TURNS:
            return jsonify({"error": f"max_turns 1-{_MAX_TURNS}"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "max_turns 整数"}), 400
    result, code = _api_handler.model_add(model_id, data.get("provider", ""),
        data.get("display", ""), tiers, speed, cost, data.get("reasoning", False),
        max_turns, data.get("strengths", ""), data.get("notes", ""))
    return jsonify(result), code

@app.route("/api/models/<model_id>", methods=["DELETE"])
def api_models_remove(model_id):
    data, code = _api_handler.model_remove(model_id)
    return jsonify(data), code

@app.route("/api/models/<model_id>", methods=["PUT"])
def api_models_update(model_id):
    data = request.get_json(silent=True) or {}
    result, code = _api_handler.model_update(model_id, data)
    return jsonify(result), code

# ═══════════════════════════════════════════════════════════
# Skill 管理
# ═══════════════════════════════════════════════════════════

@app.route("/api/skills")
def api_skills():
    data, code = _api_handler.skill_list()
    return jsonify(data), code

@app.route("/api/skills", methods=["POST"])
def api_skills_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "缺少 name"}), 400
    data, code = _api_handler.skill_add(name, body.get("description", ""),
        body.get("type", "prompt"), body.get("args", []), body.get("body", ""))
    return jsonify(data), code

@app.route("/api/skills/<name>", methods=["DELETE"])
def api_skills_delete(name):
    data, code = _api_handler.skill_delete(name)
    return jsonify(data), code

@app.route("/api/agents/<level>/<model>/skills")
def api_agent_skills(level, model):
    data, code = _api_handler.agent_skill_list(level, model)
    return jsonify(data), code

@app.route("/api/agents/<level>/<model>/skills", methods=["PUT"])
def api_agent_skills_update(level, model):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.agent_skill_update(level, model, body.get("skill_names", []))
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# 权限管理
# ═══════════════════════════════════════════════════════════

@app.route("/api/permissions/profiles")
def api_perm_profiles():
    data, code = _api_handler.perm_profiles()
    return jsonify(data), code

@app.route("/api/permissions/profiles", methods=["POST"])
def api_perm_profiles_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "缺少 name"}), 400
    data, code = _api_handler.perm_profiles_add(name, body.get("profile", {}))
    return jsonify(data), code

@app.route("/api/permissions/profiles/<name>", methods=["DELETE"])
def api_perm_profiles_delete(name):
    data, code = _api_handler.perm_profiles_delete(name)
    return jsonify(data), code

@app.route("/api/permissions/bindings", methods=["PUT"])
def api_perm_bind():
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.perm_bind(body.get("level", ""), body.get("model", ""), body.get("profile", ""))
    return jsonify(data), code

@app.route("/api/permissions/bindings/<level>/<model>", methods=["DELETE"])
def api_perm_unbind(level, model):
    data, code = _api_handler.perm_unbind(level, model)
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# MCP 服务器管理 (handler 逻辑已在 mcp.py + app.py 内，保持现状)
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 监控 & 观测
# ═══════════════════════════════════════════════════════════

@app.route("/api/events")
def api_sse_events():
    """SSE 端点: 服务器主动推送调度事件。"""
    import queue
    if len(_sse_clients) >= _MAX_SSE_CLIENTS:
        return jsonify({"error": "SSE 连接数已满"}), 503
    q = queue.Queue()
    _sse_clients.append(q)
    def generate():
        try:
            counts = witness._count_by_status()
            events_data = list(_loop_events)[:20]
            initial = json.dumps({"kind": "init", "counts": counts,
                "running_total": sum(witness._heartbeat_task_levels().values()),
                "running": _loop_running, "events": events_data})
            yield f"data: {initial}\n\n"
        except Exception:
            pass
        while True:
            try:
                data = q.get(timeout=10)
                yield f"data: {data}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'kind': 'ping', 'ts': time.time()})}\n\n"
            except GeneratorExit:
                break
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/judge-monitor")
def api_judge_monitor():
    data, code = _api_handler.judge_monitor_status()
    return jsonify(data), code

@app.route("/api/model-profile")
def api_model_profile():
    data, code = _api_handler.model_profile_status()
    return jsonify(data), code

@app.route("/api/model-profile/pattern")
def api_model_profile_pattern():
    data, code = _api_handler.model_profile_pattern()
    return jsonify(data), code

@app.route("/api/dag-metrics")
def api_dag_metrics():
    data, code = _api_handler.dag_metrics()
    return jsonify(data), code

@app.route("/health")
def health():
    data, code = _api_handler.health_check(_loop_running, len(_sse_clients))
    # 补充 projects 数量
    data["projects"] = len(proj_mod.list_all())
    return jsonify(data), code

# ═══════════════════════════════════════════════════════════
# MCP 路由 (业务逻辑已下沉 _api.py，路由仅做参数转发)
# ═══════════════════════════════════════════════════════════

@app.route("/api/mcp/servers")
def api_mcp_servers():
    data, code = _api_handler.mcp_server_list()
    return jsonify(data), code

@app.route("/api/mcp/servers", methods=["POST"])
def api_mcp_add_server():
    data, code = _api_handler.mcp_server_add(request.get_json(force=True))
    if code == 200:
        disp_mod.invalidate_mcp_cache()
    return jsonify(data), code

@app.route("/api/mcp/servers/<name>", methods=["DELETE"])
def api_mcp_delete_server(name):
    data, code = _api_handler.mcp_server_delete(name)
    if code == 200:
        disp_mod.invalidate_mcp_cache()
    return jsonify(data), code

@app.route("/api/mcp/servers/<name>/reconnect", methods=["POST"])
def api_mcp_reconnect_server(name):
    data, code = _api_handler.mcp_server_reconnect(name)
    if code == 200:
        disp_mod.invalidate_mcp_cache()
    return jsonify(data), code

@app.route("/api/mcp/tools")
def api_mcp_tools():
    data, code = _api_handler.mcp_tool_list()
    return jsonify(data), code

@app.route("/api/mcp/refresh", methods=["POST"])
def api_mcp_refresh():
    data, code = _api_handler.mcp_refresh()
    if code == 200:
        disp_mod.invalidate_mcp_cache()
    return jsonify(data), code


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("奇点调度面板已启动 → http://127.0.0.1:5050")
    # ── 初始化 MCP 连接 ──
    try:
        mcp_configs = mcp_mod.load_mcp_configs()
        if mcp_configs:
            mcp_mod.get_registry().load_configs(mcp_configs)
            print(f"MCP: {mcp_mod.get_registry().server_count} 服务器, {mcp_mod.get_registry().tool_count} 工具")
    except Exception as e:
        print(f"MCP 初始化失败 (非致命): {e}")
    app.run(debug=False, host="127.0.0.1", port=5050)
