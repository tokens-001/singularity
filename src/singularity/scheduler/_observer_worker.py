"""观察者智能体 — Worker 线程 + 启动/停止"""

from __future__ import annotations

import json
import queue
import threading
import time

from singularity.scheduler import witness
from singularity.scheduler._observer_answer import _answer_question
from singularity.scheduler._observer_client import _check_anomalies
from singularity.scheduler._observer_shared import (
    _chat_queue,
    _log,
    _pending_replies,
    _replies_lock,
    _stop_event,
    _worker_thread,
)

# ═══════════════════════════════════════════════════════════════
# 告警出口 + 活性痕迹（2026-09-17 真机加）
#
# 背景：**观察者是全仓唯一会主动发现"任务停滞"的角色**
# （`witness.check_stalled()` 只有它和一个 admin 接口在用，调度器一次都不调），
# 而真机那晚一个任务卡了 50 分钟、**它一声没吭**。查下来两件事：
#   ① 异常**一个字都不落盘** ⇒ "出声了但没人听"和"根本没出声"分不开；
#   ② 它唯一的出口 `_pending_replies` **几乎永远是空的** ⇒ 广播给空字典、无声消失。
# ═══════════════════════════════════════════════════════════════

_loops = [0]        # 跑过多少圈（活性痕迹用；list 是因为闭包里不能重绑外层名字）
_found = [0]        # 累计发现过多少条异常


def _state_path():
    from singularity.scheduler import config
    return config.QIDIAN_DIR / "observer_state.json"


def _persist_alert(alert: dict) -> None:
    """把异常落进 `alerts.jsonl` —— **它唯一的持久痕迹**。

    ⚠️ **不套 try**：`witness.warn` 自己**永不抛**（内部全兜住，写不进去还会走
    `logging` 那条第二通道 —— 见它的实现）。套一层只会让"这条落盘路断了"更难看出来。
    """
    tid = alert.get("task_id") or ""
    witness.warn(
        "observer",
        f"{alert.get('kind', '?')}: {alert.get('message', '')}"
        f"{(' task=' + str(tid)) if tid else ''}"[:400],
        key=f"observer_{alert.get('kind', 'alert')}")


def _broadcast_via_bridge(alert: dict) -> None:
    """走 `bridge.broadcast_observer` —— 真正连着界面的那条通道。

    ⚠️ **延迟导入**：`bridge` 会拉起 Observer Server（`singularity.observer.server`），
    在 worker 线程启动那一刻未必该加载它。
    """
    try:
        from singularity.scheduler import bridge as _bridge
        _bridge.broadcast_observer("observer_alert", alert)
    except Exception as e:      # noqa: BLE001 —— 广播塌了不该连累落盘/状态
        witness.warn("observer",
                     f"observer_broadcast_failed:{type(e).__name__}:{e}"[:160],
                     key="observer_broadcast_failed")


def _write_state(*, loops: int, found_total: int, checks_last: int) -> None:
    """落一条**可被证伪的活性痕迹**：**痕迹断了 = 它聋了**。

    它的失效方式是"什么都不发生"，而"什么都不发生"与"一切正常"长得一模一样 ——
    所以必须主动留痕，而且**留在一个外面看得到的地方**（`/api/observer/status` 读它），
    不能只写进它自己的日志（那等于没说给任何人听）。
    """
    try:
        from singularity.scheduler._io import atomic_write_json
        atomic_write_json(_state_path(), {
            "last_beat": time.time(),
            "loops": loops,
            "found_total": found_total,
            "checks_last_loop": checks_last,
        })
    except Exception as e:      # noqa: BLE001 —— 这条恰恰是"它聋了"的证据，必须出声
        witness.warn("observer",
                     f"observer_state_write_failed:{type(e).__name__}:{e}"[:160],
                     key="observer_state_write_failed")


def read_state() -> dict | None:
    """读那条活性痕迹。**读不到返回 None**（不是 {} —— "没有"和"空"要分得开）。"""
    p = _state_path()
    if not p.exists():
        return None              # 还没跑过 —— 这才是"没有"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # ⚠️ **文件在、读不出来**，这不是"还没跑过"，是**坏了** —— 两者必须分得开
        # （本仓反复咬人的那个病："损坏和没有长得一样"）。
        witness.warn("observer",
                     f"observer_state_unreadable:{type(e).__name__}"[:160],
                     key="observer_state_unreadable")
        return None


# ═══════════════════════════════════════════════════════════════
# 只读查询工具（纯 Python 函数，直接读取现有数据）
# ═══════════════════════════════════════════════════════════════


_GATE_LABEL = {"gate1": "第一道门（审调研报告）",
               "gate2": "第二道门（审架构方案）",
               "gate3": "第三道门（审交付物）"}

# ⚠️ 措辞是给**非开发者**看的（用户原话：「奇点的用户不可能都是专业开发者」）。
#    "别复述这段指令"是必须的 —— 不写的话模型会把上面三条当成要输出的小标题。
_REPORT_PROMPT = (
    "项目刚停在{gate}等人工审批。请用**大白话**跟用户说三件事："
    "① 现在到哪一步、这一步在干什么；"
    "② 他要拍板的是什么、有没有该提醒他注意的风险；"
    "③ 他现在该做什么。"
    "⚠️ 不要说术语、不要复述这段指令、不要列小标题。三五句话说完，别长篇大论。"
)

_report_loops = [0]


def _maybe_report_gates() -> None:
    """项目停在门上、还没跟用户讲过 → 让观察者说一段白话，推到**那个项目的对话框**。

    🔴 为什么要这个（2026-09-17 用户提）：原话「奇点的用户不可能都是专业开发者……
    专业架构方案看不懂，可以让观察者**翻译为白话在对话框汇报**，当然原本架构方案汇报还是保留」。
    在此之前观察者**只在被问时才开口**，而且连项目内容都看不见（同一天下午才接上）。

    ⚠️ **判据是「最后一次进门」vs「最后一次汇报」的先后**，不是"有没有报过" ——
       同一道门可能进两次（打回重跑再回来），第二次也该说一遍。
    ⚠️ **"报过了"落在项目 json 的 `lineage` 里，不放内存**：重启一次就丢的话，
       "改完代码到下一道门重启"这个例行操作正好会把该说的话吞掉。
    ⚠️ **推的是 `/api/events`**（`orchestrator._pending_sse_events` → 事件泵 → SSE），
       不是 `bridge.broadcast_observer` —— 后者前端**压根没有监听者**（grep 零命中），
       走它等于"出声没人听"，这个仓栽过（见本文件上面那段注释）。
    """
    from singularity.scheduler import orchestrator as _orch
    from singularity.scheduler import project as _proj

    for p in _proj.list_all():
        pid = p.id
        try:
            if p.phase.value not in _GATE_LABEL:
                continue
            lin = p.lineage or []
            last_gate = last_report = -1
            for i, e in enumerate(lin):
                if not isinstance(e, dict):
                    continue
                if e.get("action") == "phase" and e.get("to") == p.phase.value:
                    last_gate = i
                elif e.get("action") == "observer_report" and e.get("gate") == p.phase.value:
                    last_report = i
            # 判据：**汇报记录在最后一次进门之后** ⇒ 这次已经说过了，跳过。
            # ⚠️ 别写成 `last_gate < 0 → 跳过`（我第一版就是，被测试当场抓出）：
            #    "进门记录"是**汇报该不该说的依据之一，不是前提**。项目停在门上这件事
            #    本身就是证据 —— 记录缺了该照说，而不是不吭声。
            #    （真机上 `set_phase` 一定会写进门记录，但判据不该靠那个巧合成立。）
            if last_report >= 0 and last_report > last_gate:
                continue
            from singularity.scheduler._observer_answer import _answer_question_inner
            text = _answer_question_inner(
                _REPORT_PROMPT.format(gate=_GATE_LABEL[p.phase.value]), pid)
            if not text:
                continue
            # 🔴 **写回前重新读盘**（2026-09-17 真机，钉到毫秒）。
            #    上面那次读盘发生在**调模型之前**，而模型要跑 10~20 秒 ——
            #    这期间世界会变：**人按了门、调度循环把任务建出来了**。
            #    而 `save()` 写的是 `to_dict()` **全量快照**，锁只防"同时写"、
            #    **防不住"拿旧快照写"** ⇒ 拿上面那份 `p` 写回，等于把这 20 秒
            #    里发生的一切**抹掉**。
            #    实测：GATE2 批准（+ 随后建出的 8 个任务）被紧随其后的一次汇报写回清零 ——
            #    判据是 json mtime 与 `observer_report` 的 ts **只差 1.1 毫秒**。
            #    ⚠️ 窗口正好压在"人看见门 → 拍板"这段上，**不是小概率**。
            #    ⇒ 落痕和推消息都基于**现在**的盘上状态；这期间已经过门了就不吭声
            #    （那段白话说的已经不是现在的处境了）。
            fresh = _proj.load(pid)
            if fresh is None or fresh.phase != p.phase or fresh.phase.value not in _GATE_LABEL:
                continue
            # 先落痕再推：推失败也不至于下一圈重复说（"报过了"以盘为准）
            fresh.add_lineage({"action": "observer_report", "gate": fresh.phase.value})
            _proj.save(fresh)
            _orch._pending_sse_events.append({
                "kind": "observer_report", "project_id": pid,
                "msg": json.dumps({"project_id": pid, "text": text}, ensure_ascii=False),
            })
        except Exception as e:      # noqa: BLE001 —— 汇报塌了不该连累巡检
            witness.warn("observer", f"gate_report_failed:{pid[-4:]}:{type(e).__name__}:{e}"[:160],
                         key="observer_gate_report_failed")


def _observer_worker() -> None:
    _log.info("Observer agent worker started")
    while not _stop_event.is_set():
        try:
            # 1. 处理聊天消息
            while not _chat_queue.empty():
                try:
                    item = _chat_queue.get_nowait()
                    if len(item) == 4:
                        client_id, question, reply_callback, project_id = item
                    else:
                        client_id, question, reply_callback = item
                        project_id = ""
                except queue.Empty:
                    break
                # 如果没有注册 callback，用入参 callback
                if client_id:
                    with _replies_lock:
                        _pending_replies[client_id] = reply_callback
                answer = _answer_question(question, project_id=project_id)
                payload = {
                    "jsonrpc": "2.0",
                    "method": "observer_chat",
                    "params": {"type": "answer", "text": answer, "ts": time.time()},
                }
                reply_callback(payload)

            # 2. 主动异常检测
            _alerts = _check_anomalies()
            for alert in _alerts:
                # ① 🔴 **先落盘**（2026-09-17 真机）—— 原来**一个字都不落盘**：
                #    异常在盘上**连痕迹都没有**（全量搜 `alerts.jsonl` = 0 条 observer）。
                #    而它唯一的出口是下面的广播，那条路**几乎永远是空的**（见 ②）
                #    ⇒ **"出声了但没人听"和"根本没出声"分不开**，
                #    而落盘是唯一分得开的手段。
                _persist_alert(alert)
                # ② **再走真正连着界面的那条通道**（`bridge.broadcast_observer`）。
                #    原来走 `_pending_replies` —— 而那个字典**几乎永远是空的**：
                #    唯一正经的注册入口 `register_client` **全仓零调用者**，
                #    唯一写入点是**聊天路径**（有人问问题的那一刻才写），
                #    `unregister_client` 同样零调用者。
                #    ⇒ 没人聊天时，告警**广播给一个空字典、无声消失**。
                #    ⚠️ **桥里早就有能用的** `broadcast_observer`（走 Observer Server、
                #    带频道、还返回"发给了几个客户端"）—— `app.py` 的调度事件就在用它，
                #    偏偏这里没走。（"出声了但没人听"——同 §16 那个病。）
                _broadcast_via_bridge(alert)
                # ③ 聊天路径注册的老通道**保留**：有人正卡在一个没答完的问题上时，
                #    它确实能把告警送到那个正在等的客户端手里。
                payload = {
                    "jsonrpc": "2.0",
                    "method": "observer_alert",
                    "params": alert,
                }
                with _replies_lock:
                    callbacks = list(_pending_replies.values())
                for callback in callbacks:
                    try:
                        callback(payload)
                    except Exception:
                        _log.warning("广播告警失败", exc_info=True)

            # 3. **留一条可被证伪的活性痕迹**（2026-09-17 真机）。
            #    观察者要治的病和它自己得的病是**同一个**：任务静默停滞没人知道 /
            #    它静默失效没人知道 —— 而它的失效方式是"什么都不发生"，
            #    与"一切正常"**长得一模一样**。
            #    ⇒ 每圈落一条状态；**痕迹断了 = 它聋了**，而不是"静默即失效"。
            #    ⚠️ 落在 `.qidian/observer_state.json`（`/api/observer/status` 读它）
            #    —— **不能只写进它自己的日志**，那样等于没说给任何人听。
            _loops[0] += 1
            _found[0] += len(_alerts)
            _write_state(loops=_loops[0], found_total=_found[0], checks_last=len(_alerts))

            # 3. 主动汇报：项目停在门上就跟用户说一段白话（2026-09-17 用户提）。
            # ⚠️ **每 6 圈（≈30 秒）才查一次**：它要读全部项目 json
            #    （真机那个 60KB），每 5 秒读一遍是白烧 I/O；而"停在门上"是分钟级的事。
            _report_loops[0] += 1
            if _report_loops[0] % 6 == 0:
                _maybe_report_gates()

        except Exception:
            _log.exception("observer worker loop error")

        # 使用 wait 代替 sleep，便于立即响应 stop
        _stop_event.wait(5.0)

    _log.info("Observer agent worker stopped")


def start_observer() -> None:
    """启动观察者智能体守护线程。"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        _log.warning("Observer agent already running")
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_observer_worker, name="observer-agent", daemon=True)
    _worker_thread.start()
    _log.info("Observer agent started")


def stop_observer() -> None:
    """停止观察者智能体守护线程。"""
    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5.0)
        if _worker_thread.is_alive():
            _log.warning("Observer agent thread did not stop in time")
        _worker_thread = None
    _log.info("Observer agent stopped")


def is_running() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()

