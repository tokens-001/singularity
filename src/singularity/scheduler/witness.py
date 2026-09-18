from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from singularity.scheduler import config
from singularity.scheduler import tracker


def _heartbeat_dir() -> Path:
    d = config.QIDIAN_DIR / "heartbeats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alerts_path() -> Path:
    return config.QIDIAN_DIR / "alerts.jsonl"


# ═══════════════════════════════════════════
# 告警：单独的 append-only 通道
# ═══════════════════════════════════════════
# 心跳文件是「每 (task, level) 一个、覆盖写、任务终态清理」的设计。曾用
# witness.heartbeat('_api', f'warn:{e}') 这么记告警，有两个问题：
#   1. 第二参数是 agent_level，告警文本被存成层级名，status 仍是 "running"
#   2. 第一参数是作用域名不是任务 id → 找不到 tasks/<id>.json → 判孤儿 unlink
# 实测：写两条告警，任何一次状态查询后全没了。告警必须走独立通道。

_ALERT_MAX_BYTES = 512 * 1024
_ALERT_KEEP = 1000


_ALERT_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,40}):")


def _derive_alert_key(msg: str) -> str:
    """从告警文本里取聚合键：`标识符:` 里的那个标识符。取不到返回 ""（= 不聚合）。

    ⚠️ **不认异常类名**。裸异常串（`warn('orch', f'{e}')`）长成 `KeyError: 'x'`，
    看着就像个 key —— 按它聚合等于**把所有 KeyError 并成一条**，
    真事故会被并进"常驻"那一栏，**比不聚合更坏**。全仓实测有 **25 处**是这种裸写法。
    """
    m = _ALERT_KEY_RE.match(msg)
    if not m:
        return ""
    k = m.group(1)
    return "" if k.endswith(("Error", "Exception", "Warning")) else k


def warn(scope: str, msg: str, key: str = "") -> None:
    """记一条告警到 .qidian/alerts.jsonl（append-only，不受心跳清理影响）。

    `key` 是**聚合键** —— "常驻条件"按它归并（见 `alert_summary`）。
    不传就从句首的 `标识符:` 取；取不到就用整条 msg 当键（= 不聚合，安全的一侧）。
    写法不像 `标识符:` 的调用点可以显式传 key，别再往 msg 里塞格式化技巧。
    """
    try:
        p = _alerts_path()
        text = str(msg)[:500]
        rec = {"ts": time.time(), "scope": scope, "msg": text}
        _k = (key or _derive_alert_key(text))[:60]
        if _k:
            rec["key"] = _k
        line = json.dumps(rec, ensure_ascii=False)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        # ponytail: 单向追加，超过阈才重写一次；告警量小(一次任务几十条)，不值得做轮转
        if p.stat().st_size > _ALERT_MAX_BYTES:
            lines = p.read_text(encoding="utf-8").splitlines()
            p.write_text("\n".join(lines[-_ALERT_KEEP:]) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # 记告警失败**不该再抛**（否则错误处理本身变成错误源）—— 这个决定不变。
        # ⚠️ 但原来那个 `except OSError: pass` 是**静默**的，而这里是**全仓告警的唯一汇聚点**：
        # 写不进去 = **观测整体失明**，而外表看起来一切正常（2026-09-14，外派 D 反升级抓到）。
        # ⇒ 留**第二条通道**：走 `logging` 落到 stderr / 日志文件。
        # 不调 `witness.warn` 自己（会递归），也不上抛；用 `logging` 而不是 `print`，
        # 这样它能被调用方配的 handler 收走，且**没配 handler 时 Python 的 lastResort
        # 也会把它打到 stderr** —— 不会再一次消失。
        # 顺带把 `except OSError` 放宽到 `Exception`：`json.dumps` 撞上不可序列化的 msg
        # 会抛 TypeError，原来那条会一路传到调用方的 `except Exception: pass` —— 又静默一遍。
        logging.getLogger("witness").error(
            "告警写入失败（观测可能已整体失明）: %s: %s", type(e).__name__, e, exc_info=True)


def read_alerts(limit: int = 50, since: float = 0.0) -> list[dict]:
    """读最近告警，新→旧。since>0 时只要该时间戳之后的。"""
    p = _alerts_path()
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8").splitlines()[-_ALERT_KEEP * 2:]
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and d.get("ts", 0) < since:
            continue
        out.append(d)
    return out[-limit:][::-1]


def alert_summary(limit: int = 1000, since: float = 0.0, chronic_min: int = 3) -> list[dict]:
    """按聚合键把告警归并 —— 把"常驻条件"从事件流里分出来。

    **为什么需要它**：真机实测一段 26 分钟的窗口里 25 条告警，**22 条（88%）挤在
    两个 key 上**（`collect_changes` 12× / `constraints_checklist_fallback` 10×）。
    常亮**不是**"最近问题多"，是**判据跟配置脱节** —— 它该出现在配置问题栏，
    不该淹在事故流里把真事故盖住。

    返回 `[{key, scopes, n, first_ts, last_ts, chronic, sample}]`，按 n 降序。
    `key` 为 `""` = 这条没有可聚合的名字（退化成"按 scope + 整条 msg 各自成组"）。
    **不做时间窗** —— 窗口由调用方用 `since` 给，免得"常驻"的定义散在两个地方。

    ⚠️ **按 key 归并、不带 scope**。真机上 `collect_changes` 同时从 `oa_exec` 和
    `claude_cli` 两个 scope 报出来 —— 那是**同一个常驻条件被两个调用方各报一遍**。
    带上 scope 当身份会把它劈成两行、每行都不够"常驻"。范围信息不丢，`scopes` 里全带着。
    """
    g: dict[str, dict] = {}
    for d in read_alerts(limit=limit, since=since):
        msg = str(d.get("msg", ""))
        scope = d.get("scope", "")
        k = d.get("key") or _derive_alert_key(msg)
        ident = f"key:{k}" if k else f"msg:{scope}:{msg}"
        e = g.get(ident)
        if e is None:
            e = g[ident] = {"key": k, "scopes": [], "n": 0,
                            "first_ts": d.get("ts", 0), "last_ts": d.get("ts", 0),
                            "sample": msg[:120]}
        e["n"] += 1
        if scope not in e["scopes"]:
            e["scopes"].append(scope)
        e["first_ts"] = min(e["first_ts"], d.get("ts", 0))
        e["last_ts"] = max(e["last_ts"], d.get("ts", 0))
    out = sorted(g.values(), key=lambda e: e["n"], reverse=True)
    for e in out:
        e["chronic"] = e["n"] >= chronic_min
    return out


def _hb_path(task_id: str, agent_level: str) -> Path:
    # sanitize: agent_level 常被塞进 stderr/异常文本(含 /、换行、冒号等非法文件名字符)
    raw = f"{task_id}_{agent_level}"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)[:150]
    return _heartbeat_dir() / f"{safe}.json"


def heartbeat(task_id: str, agent_level: str, status: str = "running", detail: str = "") -> None:
    """写入心跳。异常时 status="error" + detail。

    ⚠️ **走 `atomic_write_json`，不再裸 `write_text`**（2026-09-19）——
    它原来是全 `.qidian` 里**唯一**一个不走 `_io.atomic_write_json` 的状态写入，
    而**裸写的代价在这个文件上格外重**：读侧见到半截 JSON 会直接 `unlink`
    （见 `_drop_corrupt_heartbeat`）。写侧是**每个 turn 一次**、进程被杀 /
    磁盘满 / 并发都可能撕 ⇒ **一次撕裂就足以让一个真卡死的任务从告警系统里消失**。
    撕裂本身在写侧堵死，比事后在读侧补救便宜得多。
    """
    from singularity.scheduler._io import atomic_write_json
    p = _hb_path(task_id, agent_level)
    payload = {"task_id": task_id, "level": agent_level, "last_beat": time.time(), "status": status}
    if detail:
        payload["detail"] = detail[:2000]
    atomic_write_json(p, payload)


def _drop_corrupt_heartbeat(p: Path, exc: Exception) -> None:
    """删掉一个读不出来的心跳文件 —— **必须出声**（2026-09-19）。

    ⚠️ 这**不只是"清垃圾"**。心跳文件是"这个任务还在跑"的**唯一**凭据，而删它的
    正当理由**只有一条**：任务已经终态/被删（`_cleanup_terminal_heartbeat`）。
    ⇒ 一个心跳文件**本来就该一直留到任务终态为止**。

    旧代码里调用方是 `except: p.unlink()` **一声不吭** ⇒ **一次撕裂 = 一个真卡死的
    任务从告警系统里消失**，之后"没心跳" = "不在跑" = **看起来正常**
    （`check_stalled` 扫的正是这个目录，文件没了它自然什么都不报）。
    这跟 §77 那一族是同一个形状：**量的是"声明在不在"，不是"实际跑没跑"。**

    撕裂已在写侧堵住（`heartbeat()` 现在原子写），所以走到这里是**异常情况**
    —— 磁盘有问题，或出现了绕过 `heartbeat()` 的写者。那种事必须留痕。

    文件名是 `_hb_path` 拼的 `<task_id>_<level>.json`，坏掉的 JSON 里取不到
    task_id，所以从**文件名**取；真取不到就原样报，别为了让报告好看而漏掉这条。
    """
    tid = p.name.rsplit("_", 1)[0] if "_" in p.name else p.stem
    try:
        p.unlink()
    except OSError as del_err:
        # 删不掉**不许**影响调用方（`check_stalled` 还要往下扫别的文件），
        # 但也不许沉默：删不掉 = 它下一轮还会被读到、还会走到这里 ⇒ 会变成一个
        # **每轮重复**的循环。走 logging 不走 witness.warn —— 上面那条告警已经
        # 说过"这个文件坏了"，这里补的是"而且我没能清掉"，是同一件事的补充，
        # 不该再占一个聚合键（同族：把"持续状态"塞进事件流会糊筛子）。
        logging.getLogger("witness").info(
            "损坏心跳 %s 没删成（下一轮还会读到）: %s: %s",
            p.name, type(del_err).__name__, del_err)
    # 走本模块的 `warn()`（这里就在 witness.py 里，没有 `witness.` 这个名字可调）。
    # ⚠️ 这条告警的消费方是**告警系统**（alerts.jsonl → observer 讲给用户听），
    # 所以必须走 `warn` 而不是只 logging —— "静默删输入"这个毛病本身说的就是
    # **告警通道没收到**，换成日志等于没修。
    warn("heartbeat",
         f"corrupt_heartbeat_dropped:{tid}:{type(exc).__name__}"[:200],
         key="corrupt_heartbeat")


def _cleanup_terminal_heartbeat(p: Path, tid: str) -> bool:
    """清理终态任务的心跳文件。返回 True 表示已清理。"""
    task_file = tracker.tasks_dir() / f"{tid}.json"
    if not task_file.exists():
        try: p.unlink()
        except OSError: pass
        return True
    try:
        data = json.loads(task_file.read_text(encoding="utf-8"))
        # ⚠️ 用状态机那份判据，别手写集合（同族：`task_timeline` 自己抄了一份、
        # 还多抄了两个非终态 ⇒ 给没跑完的任务画了终点。见 `tracker.is_terminal`）。
        if tracker.is_terminal(data.get("status")):
            try: p.unlink()
            except OSError: pass
            return True
    except (json.JSONDecodeError, OSError):
        pass
    return False


def force_cleanup_heartbeats() -> tuple[int, int]:
    """强制清理所有终态/孤儿/损坏的心跳文件。返回 (清理数, 任务文件数)。"""
    n_hb = 0
    for p in _heartbeat_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _drop_corrupt_heartbeat(p, e)   # 同一个形状，同一个理由
            n_hb += 1
            continue
        tid = data.get("task_id", "")
        if tid and _cleanup_terminal_heartbeat(p, tid):
            n_hb += 1
    n_tasks = len(list(tracker.tasks_dir().glob("*.json")))
    return n_hb, n_tasks


# 「心跳陈旧」的默认阈值（秒）。
#
# ⚠️ **它必须大于「一次 dispatch 的合法上限」，否则会把正常的长任务报成卡住。**
# `witness.heartbeat` 全仓**只有一个调用点**（`_exec.run` 的外层 turn 循环），
# 也就是**每派发一次才更新一次**；而一次 dispatch 现在合法能跑满
# `TASK_DEADLINE_S − TASK_WRAPUP_MARGIN_S = 810` 秒（执行器自己的预算，见 §67），
# 两次心跳之间还夹着校验 / 审查 / 反馈拼装。
#
# 原来的 **600 秒低于这个上限** ⇒ 任何跑过一次长 dispatch 的任务都会被报成 stalled。
# 而它的三个消费方（admin 的 `status_overview`、observer 的 `_get_system_status` /
# `_list_stalled_tasks`）会把这句**直接讲给用户听** —— observer 是个模型，
# 它会照着念"有任务卡住了"。**假警报比晚报更坏**，所以这里取宽不取紧。
STALLED_AFTER_S = config.TASK_DEADLINE_S + 300.0     # = 1200s


def check_stalled(timeout_seconds: float = STALLED_AFTER_S) -> list[str]:
    now = time.time()
    stalled: list[str] = []
    for p in _heartbeat_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _drop_corrupt_heartbeat(p, e)   # 损坏的心跳文件清理 —— **要出声**，见该函数
            continue
        tid = data.get("task_id", "")
        if tid and _cleanup_terminal_heartbeat(p, tid):
            continue
        last = data.get("last_beat", 0)
        if now - last > timeout_seconds:
            if tid:
                stalled.append(tid)
    return stalled


def _count_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in tracker.tasks_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = data.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
    return counts


def _heartbeat_task_levels() -> dict[str, int]:
    """{level: 有心跳文件的任务数}。跳过终态/已删除任务的残留心跳。"""
    loads: dict[str, int] = {}
    for p in _heartbeat_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _drop_corrupt_heartbeat(p, e)   # 同上：同一个形状，同一个理由
            continue
        tid = data.get("task_id", "")
        if tid and _cleanup_terminal_heartbeat(p, tid):
            continue
        lvl = data.get("level", "?")
        loads[lvl] = loads.get(lvl, 0) + 1
    return loads


def _timing_stats() -> tuple[list[float], list[float]]:
    """返回 (pending 等待秒数列表, done 完成秒数列表)。

    done 完成时间 = updated_at - created_at。
    """
    now = time.time()
    pending_waits: list[float] = []
    done_durations: list[float] = []
    for p in tracker.tasks_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = data.get("status", "")
        created = data.get("created_at", 0)
        if st == tracker.TaskStatus.PENDING.value and created:
            pending_waits.append(now - created)
        elif st == tracker.TaskStatus.DONE.value:
            done_durations.append(data.get("updated_at", created) - created)
    return pending_waits, done_durations


def _fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec/60:.1f}min"
    return f"{sec/3600:.2f}h"


def _fmt_avg(values: list[float]) -> str:
    return _fmt_duration(sum(values) / len(values)) if values else "--"


def status(agents: dict | None = None) -> str:
    counts = _count_by_status()
    pending = counts.get(tracker.TaskStatus.PENDING.value, 0)
    failed = counts.get(tracker.TaskStatus.FAILED.value, 0)
    done = counts.get(tracker.TaskStatus.DONE.value, 0)
    loads = _heartbeat_task_levels()  # 含清理逻辑，必须先于 running 计算
    running = sum(loads.values())

    pending_waits, done_durations = _timing_stats()
    avg_wait = _fmt_avg(pending_waits)
    avg_done = _fmt_avg(done_durations)

    lines = [
        "## Singularity Dispatch状态",
        f"- 队列中 (pending): {pending}",
        f"- 运行中 (heartbeat): {running}",
        f"- 失败 (failed): {failed}",
        f"- 已完成 (done): {done}",
        f"- 平均等待时间 (pending): {avg_wait}",
        f"- 平均完成时间 (done): {avg_done}",
        "",
        "### 各 agent level 负载",
    ]

    if agents:
        for level, cfgs in agents.items():
            model = cfgs[0].get("model", "") if cfgs else ""
            n = loads.get(level, 0)
            lines.append(f"- {level}: {n} 个任务在跑{(' (' + model + ')') if model else ''}")
    else:
        lines.append("- -- (未传 agents, 不展示 level 负载)")

    # token 统计
    token_totals = _token_stats()
    if token_totals:
        lines += ["", "### Token 消耗"]
        total_all = sum(token_totals.values())
        lines.append(f"- 总计: {_fmt_tokens(total_all)}")
        for lvl, tokens in sorted(token_totals.items()):
            lines.append(f"- {lvl}: {_fmt_tokens(tokens)}")

    return "\n".join(lines)


def _token_stats() -> dict[str, int]:
    """从 trace 文件汇总各层 token 消耗。"""
    from . import config as _cfg
    totals: dict[str, int] = {}
    for p in (_cfg.TRACE_DIR / ".." / "traces").glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tc = data.get("token_count", 0) or 0
        route = data.get("route", {}) or {}
        lvl = route.get("level", "?")
        totals[lvl] = totals.get(lvl, 0) + tc
    return totals


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)
