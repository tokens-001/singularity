import json
import os
import re
import time
from pathlib import Path

from singularity.scheduler import config as sched_config
from singularity.scheduler import tracker, witness
from singularity.scheduler._memory_core import *  # noqa: F401,F403
from singularity.scheduler._types import _pending_sse_events

__all__ = ['_llm_judge_causal', '_resolve_causal_direction', 'consolidate_memory',
           'abstract_trajectory', 'backfill_abstractions', 'adapt_experience']
# Slow Channel: 记忆整合 + 因果推断 (ex _memory_consolidator.py)
# ═══════════════════════════════════════════════════════════

_consolidate_calls = 0

# 重活（auto_maintain / system2_extract / 分层抽象）多久跑一次。
# 两个条件**满足其一** —— 计数管"忙"、时间管"闲"。
_HEAVY_EVERY_CALLS = 10
_HEAVY_EVERY_SEC = 3600


def _heavy_state_path() -> Path:
    # 读时现算：conftest 改 `config.QIDIAN_DIR` 时要管得住它（防御模式 §56 那族）
    return sched_config.QIDIAN_DIR / "memory" / "consolidate_state.json"


def _heavy_due() -> bool:
    """这一次要不要跑重活。

    ⚠️ **原来的判据是 `_consolidate_calls % 10 == 0`，而 `_consolidate_calls` 是
    模块级变量、后端每次重启归零** ⇒ 只要"单个进程生命周期内的整合次数 < 10"，
    这一步**永远不跑**。实测（2026-09-12）：三个失败任务的 `attrs.abstraction`
    全是 None（有个任务有 3414 字真轨迹也没被抽象），那晚重启 4 次、没有一个进程
    跑到 10 —— **这个功能从来没运行过一次**。

    改成把计数和上次时间**落盘**：重启不再清零；再加一条时间兜底，
    免得系统闲下来时永远攒不够 10 次。

    ⚠️ **`_heavy_due` 只说"该试了"，不说"跑成了"**（2026-09-14 改）。
    原来 due 的那一刻就把 `calls` 清零 + 打时间戳 ⇒ **重活抛异常那一次也被记成
    "成功过"**，账上再也分不出"上次真跑成了"和"上次试了但炸了"。
    现在三个字段各管一件事：
      · `calls` / `last_success` —— 管**该不该跑**（自上次**成功**起攒够 10 次调用、或满 1 小时）
      · `last_attempt` —— 管**能不能再试**（两次尝试之间至少隔 1 小时）
    重活要调模型、**要花钱** —— 失败了无限重试比晚一小时重试坏得多，所以节流必须留。
    **失败不重置 `calls`/`last_success`** ⇒ 节流一过就会再试；**成功才归零**
    （`_mark_heavy_done(True)`）。
    """
    now = time.time()
    st = {}
    try:
        st = json.loads(_heavy_state_path().read_text(encoding="utf-8"))
    except Exception:
        st = {}
    calls = int(st.get("calls", 0) or 0) + 1
    # 老状态文件只有 `last_heavy`（那时的语义是"due 那一刻就归零+打戳"）——
    # 它是"上次真跑过重活"的时间 ⇒ 当 `last_success` 用；`last_attempt` 那时没记过，
    # 缺省 0 = "不知道"，于是老文件的判定跟以前一模一样（不会凭空多一道节流）。
    legacy = float(st.get("last_heavy", 0) or 0)
    last_success = float(st.get("last_success", legacy) or 0)
    last_attempt = float(st.get("last_attempt", 0) or 0)
    if not last_success:
        # 首次：只记时间起点，**不跑** —— 否则每换一个新目录就先烧一轮重活
        _save_heavy_state(calls, now, now)
        return calls >= _HEAVY_EVERY_CALLS
    want = calls >= _HEAVY_EVERY_CALLS or (now - last_success) >= _HEAVY_EVERY_SEC
    cooled = (now - last_attempt) >= _HEAVY_EVERY_SEC
    if want and cooled:
        # ⚠️ 这里只打"**尝试**"戳：`calls` **不清零** —— 清不清零是
        # `_mark_heavy_done` 的事，它才知道这次到底跑成没跑成。
        _save_heavy_state(calls, last_success, now)
        return True
    _save_heavy_state(calls, last_success, last_attempt)
    return False


def _save_heavy_state(calls: int, last_success: float, last_attempt: float = 0.0) -> None:
    """落盘。三个字段各管一件事，见 `_heavy_due` 的说明。

    ⚠️ `last_heavy` **仍然写**（= `last_success`）：它是对外可见的那个数 ——
    真机排查时看的就是它（2026-09-13 那次"它自己触发了"的验证，看的就是
    `consolidate_state.last_heavy` 更新没更新）。**别把它删了。**
    """
    try:
        p = _heavy_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"calls": calls, "last_success": last_success,
                                 "last_attempt": last_attempt or last_success,
                                 "last_heavy": last_success}),
                     encoding="utf-8")
    except Exception:
        pass    # 状态落不下去只影响"跑得勤不勤"，不该把整合带崩


def _mark_heavy_done(ok: bool) -> None:
    """重活跑完了 —— 记结果。**只有成功才归零重来。**

    ⚠️ **失败时什么都不用写**：`_heavy_due` 已经把 `last_attempt` 打了（那是节流用的），
    而 `calls` / `last_success` 原样留着 ⇒ 节流一过就会再试。这正是这次改动的目的：
    让"没跑成"在账上看得出来，而不是被当成"跑成了"。
    """
    if not ok:
        return
    now = time.time()
    _save_heavy_state(0, now, now)      # 成功了：清计数 + 推进"上次成功"


def consolidate_memory() -> int:
    global _consolidate_calls; _consolidate_calls += 1
    now = time.time()
    if _consolidate_calls > 1 and now - getattr(consolidate_memory, '_last_run', 0) < 300:
        return 0
    consolidate_memory._last_run = now
    _MAX_LLM = 5

    if _heavy_due():
        # ⚠️ 这两个原来**没导入** —— `auto_maintain`/`system2_extract` 定义在
        # `_memory_lifecycle`，而本模块只 `from _memory_core import *`，`_memory_core`
        # 有自己的 `__all__`、不带它们。调用必抛 NameError、被下面的 `except` 吞成
        # 一条 `consolidate:` 告警 ⇒ **内存维护 + 洞察提取从来没跑过**
        # （真机 alerts.jsonl 实测：`consolidate:name 'auto_maintain' is not defined`）。
        # F821 守卫本该拦住，但本文件开头有**星号 import** ⇒ ruff 解析不了命名空间、
        # 直接不报（2026-09-13 实测：把那行星号去掉，同一个文件立刻报 7 处）。
        from singularity.scheduler._memory_lifecycle import auto_maintain, system2_extract
        # 三件里**任何一件炸了**都算"这次没跑成" ⇒ 不归零、节流一过再来（见 `_heavy_due`）。
        _heavy_ok = True
        try:
            lc = auto_maintain()
            if lc.get("pruned", 0) > 0:
                _pending_sse_events.append({"kind":"memory","msg":f"pruned {lc['pruned']} events","ts":time.time()})
        except Exception as e:
            _heavy_ok = False
            witness.warn('memory', f'consolidate:{e}')
        try:
            s2 = system2_extract()
            if s2.get("added", 0) > 0:
                for ins in s2.get("insights", []):
                    _pending_sse_events.append({"kind":"insight","msg":ins.get("summary",""),"ts":time.time()})
        except Exception as e:
            _heavy_ok = False
            witness.warn('memory', f'consolidate:{e}')
        try:
            # 分层抽象：给有轨迹、还没抽象过的节点补上（一次最多 3 条）。
            # 论文的核心那步（完整分层 80.0 vs 原始轨迹 57.6），这里用便宜模型在线做。
            got = backfill_abstractions(limit=3)
            if got:
                _pending_sse_events.append({"kind":"memory","msg":f"抽象 {got} 条轨迹","ts":time.time()})
        except Exception as e:
            _heavy_ok = False
            witness.warn('memory', f'abstract:{e}')
        _mark_heavy_done(_heavy_ok)

    try:
        # `add_inferred_causal_edge` 原来**也没导入** —— 跟上面那两个同一形状：
        # 定义在 `_memory_graph`，本模块只星号 import `_memory_core`，拿不到。
        # 于是**高置信那支一进去就抛 NameError**，整个"潜因果边"步骤 `return 0`。
        from singularity.scheduler._memory_graph import add_inferred_causal_edge, find_candidate_latent_edges
        candidates = find_candidate_latent_edges()
        added = 0
        tier3_all = [c for c in candidates if 0.55 <= c.get("semantic_sim", 0) < 0.85]
        tier3_capped = set()
        if len(tier3_all) > _MAX_LLM:
            tier3_all.sort(key=lambda x: -x["semantic_sim"])
            tier3_capped = {id(c) for c in tier3_all[_MAX_LLM:]}

        for c in candidates:
            sim = c["semantic_sim"]; gap = c["time_gap_hours"]; shared = c["shared_files"]
            if sim >= 0.85 and gap < 4.0 and len(shared) >= 1:
                src, dst = _resolve_causal_direction(c)
                if src:
                    add_inferred_causal_edge(src, dst,
                        reason=f"high_conf:shared:{','.join(shared)} sim={sim:.2f} gap={gap:.1f}h")
                    added += 1
                continue
            if sim < 0.55: continue
            if id(c) in tier3_capped: continue
            src, dst = _resolve_causal_direction(c)
            if not src: continue
            judge = _llm_judge_causal(c, src, dst)
            if judge.get("is_causal"):
                add_inferred_causal_edge(src, dst, reason=f"llm:{judge.get('reason','')}")
                added += 1
        return added
    except Exception as e:
        try: witness.warn("memory", f"consolidate:{e}")
        except Exception: pass
        return 0


def _pick_api() -> tuple[str, str, str]:
    """挑一个能用的 (model, api_key_env, base_url)。取不到 → ("", "", "")。

    ⚠️ **以前这里读的是 `agent_cfg["api_key_env"]` —— 那个字段是空的。**
    实测：agent 配置里 `api_key_env=''`、`base_url=None`，真正的连接信息在
    **api_store**（要过 `model_registry.provider_for_model` 查 provider 才拿得到）。
    所以 `os.environ.get("")` 恒为空串 → `_llm_judge_causal`
    **每次都返回 `no_api_key`、从来没生效过** —— 和"嵌入路径从来没生效过"
    是同一族：静默降级，外面看不出来（`is_causal=False` 与"判断为否"长得一样）。

    正确姿势：`execution_judge._resolve_api`（走注册表 + api_store）。
    """
    from . import dispatcher as disp_mod
    from . import execution_judge as ej_mod
    for cfg in (disp_mod.load_agents().get("any") or []):
        model = (cfg or {}).get("model", "")
        if not model:
            continue
        env_var, base_url = ej_mod._resolve_api(model)
        if env_var and base_url:
            return model, env_var, base_url
    return "", "", ""


def _chat(base_url: str, api_key: str, model: str, prompt: str,
          max_tokens: int, timeout: float = 60.0) -> dict:
    """一次非流式调用（记忆用的都是短输出，不需要停滞检测）。"""
    import httpx
    client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
    resp = client.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": 0.2},
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _record(model: str, data: dict) -> None:
    """记账。**新开的调用路径必须进账**，否则又是 `_unknown` 桶的新住户（§53）。"""
    try:
        from singularity.scheduler._token_budget import record_system_tokens
        tk = int((data.get("usage") or {}).get("total_tokens", 0) or 0)
        if tk > 0:
            record_system_tokens(model=model, level="memory", tokens=tk)
    except Exception:
        pass          # 记账失败不能影响记忆本身


_ABSTRACT_PROMPT = """把下面这一次任务执行，压成**三层**。只输出 JSON：

{{"concrete": "具体做了什么 —— 改了哪个文件的什么、跑了什么命令。只对这一次有效",
 "strategy": "可复用的套路 —— 下次遇到同一类问题该怎么办。跨项目也适用",
 "principle": "一般性的道理 —— 一句话，为什么会这样"}}

要求：
- 三层都要有；每层 1-3 句，**别写成流水账**
- 只写**实际发生**的，没发生的不许编
- concrete 别写成"改了文件 X"这种废话，要写**改了什么**

【任务】{task}
【动作序列（实际调用的工具，按顺序）】{stages}
【完整产出（可能被截断）】
{trajectory}
"""


def abstract_trajectory(trajectory: str, task: str = "",
                        tool_seq: list | None = None,
                        max_chars: int = 4000) -> dict | None:
    """把一条轨迹压成三层（具体 / 套路 / 原则）。失败返回 None。

    论文（arXiv 2607.29658）的消融：**完整分层 80.0 vs 原始轨迹 57.6**（差 22 分），
    而且"只留一层"也都不行。这里用**一次便宜模型的调用在线做** —— 不做论文那种
    离线批量（479 条 ≈ $211），我们的样本量撑不起那个开销。

    ⚠️ **效果没验证**（样本太少，验不出来）。它立刻的价值是**省 prompt**：
    deep 展开时甩 3000 字原文，压完 ≈500 字，而且更像"经验"而不是"流水账"。

    记账走 record_system_tokens(level="memory") —— **新开的调用路径必须进账**，
    否则又是 `_unknown` 桶的新住户（§53）。
    """
    traj = (trajectory or "").strip()
    if not traj:
        return None
    stages = ""
    try:
        from . import _memory_graph as _mg
        stages = _mg.stage_summary(tool_seq or [])
    except Exception:
        pass

    prompt = _ABSTRACT_PROMPT.format(task=(task or "")[:300], stages=stages or "(无)",
                                     trajectory=traj[:max_chars])
    try:
        model, env_var, base_url = _pick_api()
        if not model:
            return None                      # 没有可用连接 —— 明确失败，别假装成功
        api_key = os.environ.get(env_var, "")
        if not api_key:
            return None
        # ⚠️ max_tokens 要**连"思考"一起算**：deepseek-flash 关不掉思考
        # （见 [[qidian-thinking-params]]），思考的 token 计进 completion_tokens。
        # 实测 max_tokens=700 时，这个 ~4300 字的 prompt 让思考把预算吃光，
        # 返回 finish_reason="length" 且 **content 为空** —— 看起来像"模型没答",
        # 其实是没预算答。提到 3000 就正常了。
        data = _chat(base_url, api_key, model, prompt, max_tokens=3000)
        _record(model, data)
        raw = data["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", raw, re.S)      # 嵌套 JSON —— 不能用 [^}]+
        if not m:
            return None
        d = json.loads(m.group())
        levels = {k: str(d.get(k, "")).strip()
                  for k in ("concrete", "strategy", "principle")}
        if not any(levels.values()):
            return None
        return {**levels, "model": model, "ts": time.time()}
    except Exception as e:
        try:
            import logging
            logging.getLogger("qidian").warning("abstract_trajectory: %s", e)
        except Exception:
            pass
        return None


_ADAPT_PROMPT = """下面是**从历史任务里检索到的经验**。请针对**当前任务**，把其中适用的部分改写成一份可执行的计划。

要求：
- **适用的**留住并具体化 —— 写清"在这个任务里，这一步具体该怎么做"
- **不适用的明确丢掉**，别硬套：历史任务和当前任务不一样的地方，套上去是害人
- **别复述原文**，别写"可以参考历史经验"这种空话
- **5 行以内**，直接给计划，不要任何前后缀

【当前任务】
{task}

【历史经验（可能多条，未必都适用）】
{experiences}
"""


def adapt_experience(task: str, items: list[dict], max_chars: int = 2500) -> str:
    """把检索到的历史经验**改写成针对当前任务的计划**。失败 / 没料 → ""。

    论文（arXiv 2607.29658）原话说得很直白：老办法把检索到的摘要当**通用提示**
    塞进 prompt，而不是**针对当前问题的具体计划** —— **这是它区分成败的那一条**。
    我们原来干的正是被批评的那件事：`pre_search` 捞到什么就原样贴什么。

    ⚠️ 多花一次便宜模型的调用，所以**只在 deep 路径上调**（那条路本来就 opt-in）。
    """
    task = (task or "").strip()
    if not task or not items:
        return ""
    blocks = []
    for it in items[:3]:                       # 多给几条没意义，反而稀释
        body = it.get("full_text") or it.get("description") or ""
        if not body:
            continue
        blocks.append(f"— 历史任务：{str(it.get('description', ''))[:120]}\n{body}")
    if not blocks:
        return ""
    prompt = _ADAPT_PROMPT.format(task=task[:600],
                                  experiences="\n\n".join(blocks)[:max_chars])
    try:
        model, env_var, base_url = _pick_api()
        if not model:
            return ""
        api_key = os.environ.get(env_var, "")
        if not api_key:
            return ""
        # ⚠️ max_tokens 要**连思考一起算**（deepseek-flash 关不掉思考，reasoning
        # 计进 completion_tokens）。实测 1200 时**三次里两次返回空 content**
        # （finish_reason=length，思考吃光预算）—— 而且当时是**静默返回空串**，
        # 外面只看到"没产出计划"，看不出为什么。提到 3000 才稳。
        data = _chat(base_url, api_key, model, prompt, max_tokens=3000)
        _record(model, data)
        choice = data["choices"][0]
        out = (choice["message"].get("content") or "").strip()
        if not out:
            # **不许静默**：空 content 多半是预算被思考吃光，不是"模型不想答"
            try:
                import logging
                logging.getLogger("qidian").warning(
                    "adapt_experience: 空 content (finish_reason=%s, usage=%s)",
                    choice.get("finish_reason"), data.get("usage"))
            except Exception:
                pass
        return out
    except Exception as e:
        try:
            import logging
            logging.getLogger("qidian").warning("adapt_experience: %s", e)
        except Exception:
            pass
        return ""


# 待补积压到多少条就出声。**常驻是正常的** —— 它就是"长期欠账"的信号，
# 本来就该待在 `alert_summary` 的常驻栏里，而不是淹在事件流里（见 §3.3 那条）。
_ABSTRACTION_BACKLOG_WARN_AT = 20


def _pick_backfill_targets(todo: list, limit: int) -> list:
    """从待补清单里挑这一轮要处理的 node —— **新旧兼顾**，不是清一色最新的。

    ⚠️ **为什么不能只挑最新的**（2026-09-13 真机量化）：原实现是
    `todo.sort(key=lambda n: -n.timestamp)` 然后取前 `limit` 条，配合 `limit=3`，
    等于**每次大扫除都只处理刚产生的那 3 条**。实测后果：
    待补 25 条里 **09-12 积压的 22 条一条没动**，覆盖率停在 8/33 = 24%，
    而且**补的速度 ≈ 新增的速度** ⇒ **老账永远排不上**。

    改成一半给最新的、一半给最旧的（limit 为奇数时偏向新的）。
    **不改变一次处理几条**（那要花钱，是另一个决定），只改"挑哪几条"。
    """
    if len(todo) <= limit:
        return list(todo)
    oldest = limit // 2
    newest = limit - oldest
    return todo[:newest] + (todo[-oldest:] if oldest else [])


def backfill_abstractions(limit: int = 3) -> int:
    """给**还没抽象过**、但有轨迹的节点补上。一次最多 limit 条。

    有上限，而且调用方能看到返回几条 —— 别静默截断。失败一条不影响后面的。

    ⚠️ **挑哪几条**见 `_pick_backfill_targets`（新旧兼顾）；
    **欠账太多会出声**（`abstraction_backlog`）—— 积压是常驻状态，
    不该只有翻盘才看得见。
    """
    events = _load_events()
    todo = [n for n in events.values()
            if (n.trajectory or "").strip() and not (n.attrs or {}).get("abstraction")]
    todo.sort(key=lambda n: -n.timestamp)

    if len(todo) >= _ABSTRACTION_BACKLOG_WARN_AT:
        try:
            from . import witness
            witness.warn("memory", f"abstraction_backlog:{len(todo)}"[:120],
                         key="abstraction_backlog")
        except Exception:
            pass

    done = 0
    for node in _pick_backfill_targets(todo, limit):
        got = abstract_trajectory(node.trajectory, task=node.content,
                                  tool_seq=(node.attrs or {}).get("tool_seq"))
        if not got:
            continue
        update_attrs(node.task_id, abstraction=got)
        done += 1
    return done


def _resolve_causal_direction(c: dict) -> tuple:
    a, b = c["task_a"], c["task_b"]
    events = _load_events()
    node_a = events.get(a); node_b = events.get(b)
    if not node_a or not node_b: return None, None
    return (a, b) if node_a.timestamp <= node_b.timestamp else (b, a)


def _llm_judge_causal(c: dict, src: str, dst: str) -> dict:
    prompt = f"""Determine if there is a causal relationship between these two tasks.

Task A [{tracker.short_id(src)}]: {c.get('desc_a','')}
Task B [{tracker.short_id(dst)}]: {c.get('desc_b','')}
Shared files: {', '.join(c.get('shared_files',[]))}
Semantic sim: {c.get('semantic_sim',0):.3f}
Time gap: {c.get('time_gap_hours',0):.1f}h

Answer ONLY JSON: {{"is_causal": true/false, "reason": "one sentence"}}
If task A caused task B, is_causal=true. Otherwise false. When unsure, false."""

    try:
        # ⚠️ 这里以前读的是 `agent_cfg["api_key_env"]` —— **那个字段是空的**，
        # 于是每次都返回 no_api_key、这条 LLM 因果判断**从来没生效过**（外面看
        # `is_causal=False` 跟"判断为否"一模一样，静默）。改用 _pick_api()。
        model, env_var, base_url = _pick_api()
        if not model:
            return {"is_causal": False, "reason": "no_api"}
        api_key = os.environ.get(env_var, "")
        if not api_key:
            return {"is_causal": False, "reason": "no_api_key"}
        # 同 abstract_trajectory：思考也吃 max_tokens，200 太紧（原来就是这么写的，
        # 不过那时更早就因为取不到 key 返回了，所以没暴露出来）。
        _data = _chat(base_url, api_key, model, prompt, max_tokens=1000, timeout=30.0)
        _record(model, _data)
        raw = _data["choices"][0]["message"]["content"]
        m = re.search(r'\{.*\}', raw, re.S)   # 嵌套 JSON：不能用 [^}]+
        return json.loads(m.group()) if m else {"is_causal": False, "reason": "parse_error"}
    except Exception as e:
        try: import logging; logging.getLogger("qidian").warning("llm_judge_causal: %s", e)
        except Exception: pass
        return {"is_causal": False, "reason": f"llm_error:{e}"}
