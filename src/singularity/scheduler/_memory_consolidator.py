from singularity.scheduler._memory_core import *  # noqa: F401,F403
from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events
import json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

__all__ = ['_llm_judge_causal', '_resolve_causal_direction', 'consolidate_memory',
           'abstract_trajectory', 'backfill_abstractions']
# Slow Channel: 记忆整合 + 因果推断 (ex _memory_consolidator.py)
# ═══════════════════════════════════════════════════════════

_consolidate_calls = 0


def consolidate_memory() -> int:
    global _consolidate_calls; _consolidate_calls += 1
    now = time.time()
    if _consolidate_calls > 1 and now - getattr(consolidate_memory, '_last_run', 0) < 300:
        return 0
    consolidate_memory._last_run = now
    _MAX_LLM = 5

    if _consolidate_calls % 10 == 0:
        try:
            lc = auto_maintain()
            if lc.get("pruned", 0) > 0:
                _pending_sse_events.append({"kind":"memory","msg":f"pruned {lc['pruned']} events","ts":time.time()})
        except Exception as e: witness.warn('memory', f'consolidate:{e}')
        try:
            s2 = system2_extract()
            if s2.get("added", 0) > 0:
                for ins in s2.get("insights", []):
                    _pending_sse_events.append({"kind":"insight","msg":ins.get("summary",""),"ts":time.time()})
        except Exception as e: witness.warn('memory', f'consolidate:{e}')
        try:
            # 分层抽象：给有轨迹、还没抽象过的节点补上（一次最多 3 条）。
            # 论文的核心那步（完整分层 80.0 vs 原始轨迹 57.6），这里用便宜模型在线做。
            got = backfill_abstractions(limit=3)
            if got:
                _pending_sse_events.append({"kind":"memory","msg":f"抽象 {got} 条轨迹","ts":time.time()})
        except Exception as e: witness.warn('memory', f'abstract:{e}')

    try:
        from singularity.scheduler._memory_graph import find_candidate_latent_edges
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


def backfill_abstractions(limit: int = 3) -> int:
    """给**还没抽象过**、但有轨迹的节点补上。一次最多 limit 条。

    有上限，而且调用方能看到返回几条 —— 别静默截断。失败一条不影响后面的。
    """
    events = _load_events()
    todo = [n for n in events.values()
            if (n.trajectory or "").strip() and not (n.attrs or {}).get("abstraction")]
    todo.sort(key=lambda n: -n.timestamp)
    done = 0
    for node in todo[:limit]:
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

Task A [{src[:8]}]: {c.get('desc_a','')}
Task B [{dst[:8]}]: {c.get('desc_b','')}
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
