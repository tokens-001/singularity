from __future__ import annotations
# Singularity Agent 调度平台 — Web 控制台
# Flask 后端：查看调度状态、提交任务、处理合并冲突
# v2: 调度循环后台线程，面板即控制中心

import json
import os
import sys
import time
import threading
from collections import deque
from pathlib import Path

from urllib.parse import urlparse

from flask import Flask, Response, g, request, jsonify

# ── 加载 .env ──────────────────────────────────────────────
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


def _write_env(key: str, value: str) -> None:
    """把 key=value 写入项目 .env（已存在则更新，否则追加），并注入当前进程 os.environ。"""
    lines = _ENV_PATH.read_text().splitlines() if _ENV_PATH.exists() else []
    out, seen = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(out) + "\n")
    os.environ[key] = value


# ── 调度器模块路径 ──────────────────────────────────────────
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler import config as sched_config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import witness
from singularity.scheduler import orchestrator
from singularity.scheduler import project as proj_mod
from singularity.scheduler.project import Phase
from singularity.scheduler.log import info as _log_info, warn as _log_warn, get_logger
from singularity.scheduler import mcp as mcp_mod
from singularity.scheduler import bridge as ws_bridge

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 安全加固：拒绝 >2MB 的请求体
app.config["TEMPLATES_AUTO_RELOAD"] = True           # ponytail: 开发时不缓存模板
# secret_key: env > 持久化文件 > 随机（仅首次）
_secret_key = os.environ.get("FLASK_SECRET_KEY")
if not _secret_key:
    _key_file = Path(__file__).resolve().parents[3] / ".qidian" / "secret_key"
    try:
        _secret_key = _key_file.read_text().strip()
    except Exception:
        _secret_key = os.urandom(24).hex()
        _key_file.parent.mkdir(parents=True, exist_ok=True)
        _key_file.write_text(_secret_key)
app.secret_key = _secret_key

_CSRF_TOKEN = os.environ.get("QIDIAN_CSRF_TOKEN") or os.urandom(16).hex()

import logging as _al
_audit_logger = _al.getLogger("qidian.audit")
sched_config.ensure_dirs()
(_ad := sched_config.QIDIAN_DIR / "logs").mkdir(parents=True, exist_ok=True)
_ah = _al.FileHandler(str(_ad / "audit.log"))
_ah.setFormatter(_al.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}', datefmt='%Y-%m-%dT%H:%M:%S'))
_audit_logger.addHandler(_ah); _audit_logger.setLevel(_al.INFO); _audit_logger.propagate = False

def audit_log(action, detail="", user="", ip=""):
    import json as _j
    _audit_logger.info(_j.dumps({"action":action,"detail":detail[:500],"user":user or "-","ip":ip or "-"}))

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
_VALID_LEVELS = frozenset({"any"})  # 两档后统一 any
_VALID_AGENT_TYPES = frozenset({"openai-agent", "claude-cli", "zhipu-api"})
_VALID_SANDBOXES = frozenset({"worktree", "inline", "none"})
_VALID_ROLES = frozenset({"admin", "operator", "viewer"})
_VALID_SPEEDS = frozenset({"fast", "medium", "slow"})
_VALID_COSTS = frozenset({"budget", "standard", "premium"})
_VALID_TIERS = frozenset({"any", "定义", "架构", "实现", "审查", "验收", "交付"})
_VALID_TEMPLATES = frozenset({"product_dev", "bug_fix", "refactor", "agent_dev", "feature", "bugfix", "test", "review"})
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
    from singularity.scheduler._auth import get_auth, require_auth, require_write
    _admin = get_auth().bootstrap()
    _log_info("auth", f"认证已启用, admin token: {_admin.token[:8]}...")

# 无需认证的公开端点
_PUBLIC_ENDPOINTS = {
    "api_auth_status",
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
def _guard_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"): return None
    if not request.path.startswith("/api/"): return None
    # 本地请求免 CSRF (开发/调试 + 浏览器未带 JS header)
    if request.remote_addr in ("127.0.0.1", "::1"): return None
    t = request.headers.get("X-CSRF-Token", "")
    x = request.headers.get("X-Requested-With", "")
    if t == _CSRF_TOKEN or x == "XMLHttpRequest": return None
    return jsonify({"error": "CSRF token required"}), 403


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

    from singularity.scheduler._auth import require_auth, require_write

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

        # 定期清理（每 100 请求触发一次，同一把锁内执行防竞态）
        if sum(len(v) for v in _RATE_BUCKETS.values()) % 100 == 0:
            _cleanup_rate_buckets()

    return None


# ── Body 大小守卫 ──────────────────────────────────────────
_MAX_BODY_BYTES = 1_000_000  # 1MB

@app.before_request
def _guard_body_size():
    """拒绝超大请求体，防止内存耗尽。"""
    if request.method in ("POST", "PUT", "PATCH") and request.path.startswith("/api/"):
        cl = request.content_length
        if cl is not None and cl > _MAX_BODY_BYTES:
            return jsonify({"error": "请求体过大", "max_bytes": _MAX_BODY_BYTES}), 413


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
_sse_event_id = 0             # 全局递增事件 ID
_sse_event_lock = threading.Lock()
_sse_event_buffer: deque = deque(maxlen=200)  # 事件回放缓冲区 (event_id, data_json)
_sse_heartbeat_interval = 15  # SSE 心跳间隔(秒)


def _loop_worker():
    """后台调度循环：持续取队 → 执行，面板可随时停止。"""
    global _loop_running
    import signal

    sched_config.ensure_dirs()
    agents = disp_mod.load_agents()

    # ── API 可用性启动检测 ──
    unavailable = []
    for level, cfgs in agents.items():
        for c in cfgs:
            if not disp_mod.agent_api_available(c):
                unavailable.append(f"{level}:{c.get('model','?')}")
    if unavailable:
        _push_event("system", f"API 不可用 ({len(unavailable)}): {', '.join(unavailable)}")
    else:
        _push_event("system", "全部 API 可用")

    recovered = tracker.recover()
    if recovered:
        _push_event("system", f"恢复 {recovered} 个中断任务")

    _log_info("loop", "scheduler loop started")
    _push_event("system", "loop started")
    idle_ticks = 0

    while not _loop_stop.is_set():
        try:
            agents = disp_mod.load_agents()  # 每轮刷新 agent 配置
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
                    t = tracker.read_task(tid)
                    level = t.route_level if t else "?"
                    verdict = getattr(validation, "action", "?")
                    _push_event("task", f"[{tid[:8]}] level={level} {verdict}: {reason}")
                    # 任务失败时推送桌面通知
                    if verdict != "pass":
                        try:
                            import subprocess, sys
                            subprocess.run([
                                "osascript", "-e",
                                f'display notification "任务 {tid[:8]} {verdict}" with title "Singularity Dispatch"'
                            ], capture_output=True, timeout=3)
                        except Exception:
                            pass

                # MAGMA 慢通道整合
                try:
                    from singularity.scheduler.memory import consolidate_memory
                    added = consolidate_memory()
                    if added:
                        _push_event("memory", f"慢通道: +{added} 条隐含因果边")
                except Exception:
                    pass

                # 项目工作流推进: 检查已完成的任务是否属于某个项目
                try:
                    for tid, reason, validation in results:
                        for proj in proj_mod.recover_all():
                            if tid not in proj.task_ids:
                                continue
                            if proj.phase not in (Phase.EXECUTING, Phase.FIXING):
                                continue
                            # 检查是否所有子任务完成
                            all_done = True
                            for tid2 in proj.task_ids:
                                t = tracker.read_task(tid2)
                                if t and t.status not in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK):
                                    all_done = False
                                    break
                            if not all_done:
                                continue

                            from singularity.scheduler import workflow as wf_mod
                            if proj.phase == Phase.EXECUTING:
                                _push_event("workflow", f"项目 {proj.name[:20]}: 执行完成 → 内循环")
                                msg = wf_mod.run_test_fix_loop(proj, agents)
                                _push_event("workflow", f"项目 {proj.name[:20]}: {msg}")
                                # auto_mode: 继续推进
                                if proj.auto_mode:
                                    try:
                                        wf_mod.run_phase(proj, agents)
                                    except Exception:
                                        pass
                            elif proj.phase == Phase.FIXING:
                                _push_event("workflow", f"项目 {proj.name[:20]}: 修复任务完成 → 回到审查")
                                wf_mod.run_phase(proj, agents)
                                if proj.auto_mode:
                                    try:
                                        wf_mod.run_phase(proj, agents)
                                    except Exception:
                                        pass
                except Exception:
                    pass
        except Exception as e:
            _push_event("error", f"loop error: {e}")
            time.sleep(5)

    _push_event("system", "loop stopped")
    _loop_running = False


# Observer WS 频道映射：SSE kind → WS channels
_WS_CHANNEL_MAP: dict[str, set[str]] = {
    "task": {"tasks"},
    "task_create": {"tasks"},
    "system": {"system"},
    "idle": {"system"},
    "error": {"alerts"},
    "workflow": {"system"},
    "agent_change": {"system"},
    "observer_answer": {"system"},
}


def _push_event(kind: str, msg: str, ts: float = None):
    if ts is None:
        ts = time.time()
    _loop_events.appendleft({"kind": kind, "msg": msg, "ts": ts})
    _sse_broadcast(kind, msg, ts)
    # T5: 同步推送到 Observer WS（前端 subscribe 后接收）
    if kind in _WS_CHANNEL_MAP:
        try:
            count = ws_bridge.broadcast_observer(kind, {"msg": msg}, channels=_WS_CHANNEL_MAP[kind])
        except Exception as e:
            _al.getLogger("ws").warning("broadcast_observer failed for %s: %s", kind, e)


def _next_event_id():
    global _sse_event_id
    with _sse_event_lock:
        _sse_event_id += 1
        return _sse_event_id


def _sse_broadcast(kind: str, msg: str, ts: float = None):
    """向所有 SSE 客户端推送事件，附加递增 event_id 并存入回放缓冲区。"""
    if ts is None:
        ts = time.time()
    eid = _next_event_id()
    data = json.dumps({"kind": kind, "msg": msg, "ts": ts})
    # 回放缓冲区（心跳不入缓冲区，免浪费空间）
    if kind != "ping":
        _sse_event_buffer.append((eid, data))
    payload = (eid, data)
    dead = []
    for q in _sse_clients:
        try:
            q.put(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass
    # T1: 同时推送到 WebSocket 客户端
    if kind != "ping":
        try:
            ws_bridge.broadcast_json({
                "jsonrpc": "2.0", "method": "event",
                "params": {"kind": kind, "msg": msg, "ts": ts}}
            )
        except Exception:
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


def _is_local_origin(origin: str) -> bool:
    """精确检查 origin 是否为本地地址 (防 startswith 绕过)。"""
    try:
        hostname = urlparse(origin).hostname
        if not hostname:
            return False
        return hostname in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    except Exception:
        return False


_api_log = get_logger("api")

@app.before_request
def _api_timing_start():
    """记录 API 请求开始时间。"""
    if request.path.startswith("/api/"):
        g._start_time = time.perf_counter()


@app.after_request
def _api_timing_log(response):
    """记录 API 请求耗时。"""
    t0 = getattr(g, "_start_time", None)
    if t0 is not None:
        elapsed = (time.perf_counter() - t0) * 1000
        status = response.status_code
        _api_log.info("%s %s → %s | %.0fms", request.method, request.path, status, elapsed)
    return response


@app.after_request
def add_cors_headers(response):
    # 仅允许本地来源（安全加固：不再使用 *）
    origin = request.headers.get("Origin", "")
    allowed = (
        not origin
        or _is_local_origin(origin)
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
    from singularity.scheduler._cache import task_cache
    task_cache.invalidate()


# ── API handler 层 ──
from singularity.scheduler import _api as _api_handler

# ═══════════════════════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════════════════════

@app.route("/", defaults={"path": ""}, strict_slashes=False)
@app.route("/<path:path>")
def spa(path=""):
    """React SPA。API 路由优先匹配，其他全部 fallback。"""
    dist_index = Path(__file__).parent / "static" / "dist" / "index.html"
    return dist_index.read_text(encoding="utf-8")


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
# Fusion 配置
# ═══════════════════════════════════════════════════════════

@app.route("/api/fusion/config", methods=["GET", "PUT"])
def api_fusion_config():
    fusion_path = sched_config.SCHEDULER_DIR / "fusion.toml"
    from singularity.scheduler._io import load_toml, save_toml
    if request.method == "GET":
        try:
            return jsonify(load_toml(fusion_path))
        except Exception:
            return jsonify({})
    # PUT: 更新指定 tier 配置 (dual/triple/super/custom)
    data = request.get_json(silent=True) or {}
    try:
        cfg = load_toml(fusion_path)
        for tier in ["dual", "triple", "super", "custom"]:
            if tier in data:
                cfg[tier] = {**cfg.get(tier, {}), **data[tier]}
        save_toml(fusion_path, cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route("/api/files/reveal", methods=["POST"])
def api_files_reveal():
    """在访达/文件管理器中定位文件 (macOS open -R)。路径限制在项目根目录内。"""
    import subprocess
    body = request.get_json(silent=True) or {}
    rel = (body.get("path") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "路径为空"}), 400
    root = Path(sched_config.PROJECT_ROOT).resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root) + os.sep):
        return jsonify({"ok": False, "error": "路径越界"}), 403
    if not target.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        subprocess.run(["open", "-R", str(target)], check=False, timeout=5)
        return jsonify({"ok": True, "path": str(target)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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


@app.route("/api/tasks/<task_id>/pause", methods=["POST"])
def api_pause_task(task_id):
    data, code = _api_handler.task_pause(task_id)
    return jsonify(data), code


@app.route("/api/tasks/<task_id>/resume", methods=["POST"])
def api_resume_task(task_id):
    data, code = _api_handler.task_resume(task_id)
    return jsonify(data), code


@app.route("/api/tasks/<task_id>/mode", methods=["POST"])
def api_task_set_mode(task_id):
    body = request.get_json(silent=True) or {}
    data, code = _api_handler.task_set_mode(task_id, body.get("mode", ""))
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

@app.route("/api/tasks/<task_id>", methods=["PUT"])
def api_task_update(task_id):
    data = request.get_json(silent=True) or {}
    _invalidate_task_cache()
    result, code = _api_handler.task_update(task_id, data)
    return jsonify(result), code


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
        return jsonify({"error": f"route_level 必须是 any"}), 400
    route_type = data.get("route_type", "")
    _VALID_TYPES = frozenset({"default", "bugfix", "feature", "refactor", "docs", "fusion"})
    if route_type and route_type not in _VALID_TYPES:
        return jsonify({"error": f"route_type 无效，允许: {','.join(sorted(_VALID_TYPES))}"}), 400
    _invalidate_task_cache()
    result, code = _api_handler.task_submit(desc, priority=priority, depends_on=depends_on,
                                             route_level=route_level, route_type=route_type, push_event=_push_event)
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
        if result.get("ok"):
            _push_event("project", json.dumps({
                "project_id": result["project"]["id"],
                "name": result["project"]["name"],
                "phase": "template", "task_count": 0,
            }))
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

@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_project_delete(project_id):
    from singularity.scheduler import project as proj_mod
    ok = proj_mod.delete(project_id)
    return jsonify({"ok": ok}), 200 if ok else 404

@app.route("/api/projects/<project_id>/start", methods=["POST"])
def api_project_start(project_id):
    data, code = _api_handler.project_start(project_id, _push_event)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/files")
def api_project_files(project_id):
    """列出项目文件树 (git ls-files)。"""
    import subprocess, os
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        result = subprocess.run(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
            capture_output=True, text=True, cwd=root, timeout=5)
        files = [f.strip() for f in result.stdout.split('\n') if f.strip() and not f.strip().startswith('.qidian/')]
        # 只显示源代码, 排除文档/数据/配置/测试
        skip_prefixes = ('node_modules/', 'docs/', 'data/', '.agents/', '.git/', '.claude/', '.codegraph/', '__pycache__/')
        skip_suffixes = ('.pyc', '.md', '.html', '.json', '.toml', '.yaml', '.yml', '.lock', '.gitignore', '.txt')
        files = [f for f in files if not any(f.startswith(p) for p in skip_prefixes) and not any(f.endswith(s) for s in skip_suffixes)]
        return jsonify({"files": sorted(files)})
    except Exception as e:
        return jsonify({"files": [], "error": str(e)})

@app.route("/api/projects/<project_id>/files/<path:filepath>")
def api_project_file_content(project_id, filepath):
    """读取文件内容。"""
    import subprocess, os
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        fpath = os.path.join(root, filepath)
        if not os.path.exists(fpath) or not fpath.startswith(root):
            return jsonify({"content": "", "error": "file not found"}), 404
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()[:50000]
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"content": "", "error": str(e)})

@app.route("/api/projects/<project_id>/diff")
def api_project_diff(project_id):
    """最近的 git diff (HEAD~1..HEAD)。"""
    import subprocess, os
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        result = subprocess.run(
            ['git', 'diff', 'HEAD~3..HEAD', '--stat'],
            capture_output=True, text=True, cwd=root, timeout=5)
        stat = result.stdout.strip()
        result2 = subprocess.run(
            ['git', 'diff', 'HEAD~3..HEAD', '--', ':(exclude).qidian', ':(exclude)node_modules', ':(exclude)*.pyc'],
            capture_output=True, text=True, cwd=root, timeout=5)
        diff = result2.stdout[:50000]
        return jsonify({"stat": stat, "diff": diff})
    except Exception as e:
        return jsonify({"stat": "", "diff": "", "error": str(e)})

@app.route("/api/projects/<project_id>/cost")
def api_project_cost(project_id):
    data, code = _api_handler.project_cost(project_id)
    return jsonify(data), code

@app.route("/api/cost")
def api_cost():
    from singularity.scheduler.model_profile import get_cost_summary
    return jsonify(get_cost_summary())


@app.route("/api/quality/trends")
def api_quality_trends():
    # ponytail: judge_monitor 已移除，仅返回模型画像成本数据
    from singularity.scheduler.model_profile import get_cost_summary
    return jsonify({"cost": get_cost_summary()})

@app.route("/api/projects/<project_id>/lineage")
def api_project_lineage(project_id):
    data, code = _api_handler.project_lineage(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/snapshot", methods=["POST"])
def api_project_snapshot(project_id):
    data, code = _api_handler.project_snapshot(project_id)
    return jsonify(data), code

@app.route("/api/projects/<project_id>/traceability", methods=["GET"])
def api_project_traceability(project_id):
    """GATE3: 返回需求追溯表 + 符合性检查结果。"""
    from singularity.scheduler.supervisor import check_requirement_conformance
    from singularity.scheduler.project import _projects_dir
    import json as _json
    # 读追溯表
    tp = _projects_dir() / f"{project_id}.traceability.json"
    traceability = []
    if tp.exists():
        try:
            traceability = _json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 读测试方案
    testp = _projects_dir() / f"{project_id}.test-plan.md"
    test_plan = None
    if testp.exists():
        try:
            test_plan = testp.read_text(encoding="utf-8")
        except Exception:
            pass
    # 需求符合性
    req_check = check_requirement_conformance(project_id)
    return jsonify({
        "ok": True,
        "traceability": traceability,
        "test_plan": test_plan,
        "conformance": {
            "passed": req_check.passed,
            "reason": req_check.reason,
            "evidence": req_check.evidence,
        },
    })

@app.route("/api/projects/<project_id>/auto", methods=["POST"])
def api_project_auto(project_id):
    data, code = _api_handler.project_auto(project_id)
    return jsonify(data), code

# ponytail: autopilot 已移除，人控流程通过 gate confirm API

# ═══════════════════════════════════════════════════════════
# Observer 智能体聊天 API
# ═══════════════════════════════════════════════════════════

@app.route("/api/observer/chat", methods=["POST"])
def api_observer_chat():
    """提交问题给观察者智能体，答案通过 SSE 推送。"""
    import uuid
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    exec_mode = body.get("execution_mode", "auto_edit")
    project_id = body.get("project_id", "")
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空"}), 400
    cid = uuid.uuid4().hex[:12]
    def _on_reply(payload):
        text = (payload.get("params") or {}).get("text", "")
        _push_event("observer_answer", json.dumps({"client_id": cid, "answer": text}))
    try:
        from singularity.scheduler.observer_agent import submit_question
        # 注入执行模式到问题上下文
        mode_hint = "【执行模式：每一步确认，变更前暂停】" if exec_mode == "confirm_changes" else ""
        full_question = f"{question}\n{mode_hint}" if mode_hint else question
        submit_question(cid, full_question, _on_reply, project_id=project_id)
        return jsonify({"ok": True, "client_id": cid, "question": question})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
    data, code = _api_handler.reports_list()
    return jsonify(data), code

@app.route("/api/reports/critical")
def api_reports_critical():
    data, code = _api_handler.reports_critical()
    return jsonify(data), code

@app.route("/api/reports/<report_id>", methods=["DELETE"])
def api_report_dismiss(report_id):
    from singularity.scheduler import chancellor as chan_mod
    ok = chan_mod.dismiss_report(report_id)
    return jsonify({"ok": ok})

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
    if not data or not data.get("model"):
        return jsonify({"error": "缺少 model"}), 400
    model = data["model"].strip()
    if not model or len(model) > 100:
        return jsonify({"error": "model 不能为空且不超过 100 字符"}), 400
    level = data.get("level", "any")
    if level not in _VALID_LEVELS:
        return jsonify({"error": "level 必须是 any"}), 400
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
    if entry_url and agent_type != "claude-cli" and not _is_safe_api_url(entry_url):
        return jsonify({"error": "不允许的 entry URL（仅支持已知 API 厂商域名）"}), 400
    result, code = _api_handler.agent_add(level, model, agent_type, entry_url,
        data.get("api_key_env", ""), max_turns, data.get("roles", []), sandbox,
        data.get("mode", ""), data.get("request_template"))
    _push_event("agent_change", f"{level}:+{model}")
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
    _push_event("agent_change", f"{level}:-{model}")
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
    api_key_env = data.get("api_key_env", "")
    api_key = data.get("api_key", "")
    if api_key and api_key_env:
        _write_env(api_key_env, api_key)
    result, code = _api_handler.api_store_add(data["id"], data.get("provider", data["id"]),
        base_url, api_key_env, data.get("notes", ""))
    return jsonify(result), code

@app.route("/api/api-store/<api_id>", methods=["DELETE"])
def api_store_remove(api_id):
    data, code = _api_handler.api_store_remove(api_id)
    return jsonify(data), code

@app.route("/api/api-store/<api_id>/scan", methods=["POST"])
def api_store_scan(api_id):
    data, code = _api_handler.api_store_scan(api_id)
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
        from singularity.scheduler._auth import require_auth
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
        from singularity.scheduler._auth import require_auth
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
    tiers = data.get("tiers", ["any"])
    if not isinstance(tiers, list) or not tiers:
        return jsonify({"error": "tiers 非空数组"}), 400
    for t in tiers:
        if t not in _VALID_TIERS:
            return jsonify({"error": "tiers 不合法"}), 400
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

@app.route("/api/models/import", methods=["POST"])
def api_models_import():
    data = request.get_json(silent=True)
    # 兼容两种格式: 前端传数组 [m...] 或对象 {"models":[m...]}
    if isinstance(data, list):
        models, auto_assign = data, False
    elif isinstance(data, dict) and isinstance(data.get("models"), list):
        models, auto_assign = data["models"], data.get("auto_assign", False)
    else:
        return jsonify({"error": "缺少 models 数组"}), 400
    result, code = _api_handler.models_import(models, auto_assign=auto_assign)
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

@app.route("/api/roles")
def api_roles():
    """返回所有角色定义 + 人格面具列表。"""
    from singularity.scheduler.roles import ROLES, PERSONAS
    roles = {}
    for k, r in ROLES.items():
        roles[k] = {
            "key": r.key, "name": r.name, "level": r.level,
            "description": r.description, "persona": r.persona,
            "capabilities": r.capabilities,
            "system_prompt": r.system_prompt[:500],  # 截断，完整版单独取
        }
    personas = {}
    for k, p in PERSONAS.items():
        personas[k] = {
            "key": p.key, "name": p.name, "description": p.description,
            "style_prompt": p.style_prompt[:300], "philosophy": p.philosophy,
            "voice": p.voice,
        }
    return jsonify({"roles": roles, "personas": personas}), 200


@app.route("/api/roles/<key>", methods=["PATCH"])
def api_roles_update(key):
    """更新角色配置（人格、层级等）。写入 roles_custom.json 覆盖。"""
    data = request.get_json(silent=True) or {}
    overrides_path = sched_config.QIDIAN_DIR / "roles_custom.json"
    overrides = {}
    if overrides_path.exists():
        try: overrides = json.loads(overrides_path.read_text())
        except Exception: pass
    overrides[key] = {k: v for k, v in data.items() if v}
    overrides_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
    # Reload roles
    from singularity.scheduler.roles import _init
    _init()
    return jsonify({"ok": True, "key": key, "updated": list(data.keys())}), 200


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
        body.get("type", "prompt"), body.get("args", []), body.get("body", ""),
        body.get("category", ""))
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
    """SSE 端点: 服务器主动推送调度事件，支持 Last-Event-ID 断线重连回放。"""
    import queue
    if len(_sse_clients) >= _MAX_SSE_CLIENTS:
        return jsonify({"error": "SSE 连接数已满"}), 503
    q = queue.Queue()
    _sse_clients.append(q)

    # 解析 Last-Event-ID（浏览器 EventSource 重连时自动携带）
    # 优先 HTTP 头(浏览器原生重连), 其次 query param(手动重连)
    last_eid = 0
    try:
        hdr = request.headers.get("Last-Event-ID") or request.args.get("last_event_id", "")
        if hdr:
            last_eid = int(hdr)
    except (ValueError, TypeError):
        pass

    def generate():
        # 1) 回放断线期间遗漏的事件
        if last_eid > 0:
            replayed = 0
            # 缓冲区按时间排序，找到所有 >last_eid 的事件
            for eid, data in _sse_event_buffer:
                if eid > last_eid:
                    yield f"id: {eid}\ndata: {data}\n\n"
                    replayed += 1
            if replayed:
                _log_info("sse", f"回放 {replayed} 个遗漏事件 (Last-Event-ID={last_eid})")

        # 2) 初始状态快照（作为当前连接的首个事件）
        try:
            init_eid = _next_event_id()
            counts = witness._count_by_status()
            events_data = list(_loop_events)[:20]
            initial = json.dumps({"kind": "init", "counts": counts,
                "running_total": sum(witness._heartbeat_task_levels().values()),
                "running": _loop_running, "events": events_data})
            yield f"id: {init_eid}\ndata: {initial}\n\n"
        except Exception as _e:
            yield f"data: {json.dumps({'kind': 'error', 'msg': f'init failed: {_e}'})}\n\n"

        # 3) 持续推送
        while True:
            try:
                eid, data = q.get(timeout=_sse_heartbeat_interval)
                yield f"id: {eid}\ndata: {data}\n\n"
            except queue.Empty:
                # 心跳保活
                ping_eid = _next_event_id()
                ping = json.dumps({"kind": "ping", "ts": time.time()})
                yield f"id: {ping_eid}\ndata: {ping}\n\n"
            except GeneratorExit:
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ponytail: judge-monitor 已移除

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


@app.route("/sw.js")
def serve_sw():
    """T19: Service Worker — 必须从根路径 /sw.js 注册才能控制全站。"""
    from flask import send_from_directory
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import signal as _signal
    import logging as _logging

    _startup_log = _logging.getLogger("startup")

    def _graceful_shutdown(signum, frame):
        _startup_log.info("收到信号, 优雅关闭中...")
        stop_loop()
        try:
            from singularity.scheduler.observer_agent import stop_observer
            stop_observer()
        except Exception:
            pass
        ws_bridge.stop_observer_server()
        ws_bridge.stop_ws_server()
        import singularity.scheduler.mcp as _mcp
        try:
            reg = _mcp.get_registry()
            for name in list(reg._clients.keys()):
                reg._clients[name].disconnect()
        except Exception:
            pass
        _startup_log.info("关闭完成")
        sys.exit(0)

    _signal.signal(_signal.SIGTERM, _graceful_shutdown)
    _signal.signal(_signal.SIGINT, _graceful_shutdown)

    # ── 启动自检 ──
    _startup_log.info("Singularity Dispatch面板 → http://127.0.0.1:5050")
    try:
        from singularity.scheduler import model_registry, api_store
        models = model_registry.load_models()
        available = sum(1 for m in models.values() if api_store.is_available(m.provider))
        _startup_log.info("模型: %d 注册, %d 可用", len(models), available)
    except Exception as e:
        _startup_log.warning("模型检查失败: %s", e)
    try:
        _ = tracker.ready_tasks()  # 预热缓存
    except Exception:
        pass
    # ── 初始化 MCP 连接 ──
    try:
        mcp_configs = mcp_mod.load_mcp_configs()
        if mcp_configs:
            mcp_mod.get_registry().load_configs(mcp_configs)
            _startup_log.info("MCP: %d 服务器, %d 工具",
                              mcp_mod.get_registry().server_count,
                              mcp_mod.get_registry().tool_count)
    except Exception as e:
        _startup_log.warning("MCP 初始化失败 (非致命): %s", e)
    # T1: 启动 WebSocket 服务器 (与 Flask HTTP 并行)
    try:
        ws_bridge.start_ws_server(host="127.0.0.1", port=5051)
        _startup_log.info("WebSocket 服务器已启动 ws://127.0.0.1:5051")
    except Exception as e:
        _startup_log.warning("WebSocket 启动失败: %s", e)
    # 启动 Observer WebSocket 服务器 (实时事件推送)
    try:
        ws_bridge.start_observer_server(host="127.0.0.1", port=8765)
        _startup_log.info("Observer WebSocket 已启动 ws://127.0.0.1:8765")
    except Exception as e:
        _startup_log.warning("Observer WebSocket 启动失败: %s", e)
    # 自动启动调度循环
    try:
        start_loop(concurrent=2)
        _startup_log.info("调度循环已自动启动 (concurrent=2)")
    except Exception as e:
        _startup_log.warning("调度循环启动失败: %s", e)
    # 自动启动观察者智能体
    try:
        from singularity.scheduler.observer_agent import start_observer
        start_observer()
        _startup_log.info("观察者智能体已自动启动")
    except Exception as e:
        _startup_log.warning("观察者启动失败: %s", e)
    app.run(debug=False, host="127.0.0.1", port=5050)
