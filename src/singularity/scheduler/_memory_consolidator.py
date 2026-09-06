from singularity.scheduler._memory_core import *  # noqa: F401,F403
from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events
import json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

__all__ = ['_llm_judge_causal', '_resolve_causal_direction', 'consolidate_memory']
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
        except Exception as e: witness.heartbeat('memory', f'warn:consolidate:{e}')
        try:
            s2 = system2_extract()
            if s2.get("added", 0) > 0:
                for ins in s2.get("insights", []):
                    _pending_sse_events.append({"kind":"insight","msg":ins.get("summary",""),"ts":time.time()})
        except Exception as e: witness.heartbeat('memory', f'warn:consolidate:{e}')

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
        try: witness.heartbeat("memory", f"warn:consolidate:{e}")
        except Exception: pass
        return 0


def _resolve_causal_direction(c: dict) -> tuple:
    a, b = c["task_a"], c["task_b"]
    events = _load_events()
    node_a = events.get(a); node_b = events.get(b)
    if not node_a or not node_b: return None, None
    return (a, b) if node_a.timestamp <= node_b.timestamp else (b, a)


def _llm_judge_causal(c: dict, src: str, dst: str) -> dict:
    import httpx
    from . import dispatcher as disp_mod
    prompt = f"""Determine if there is a causal relationship between these two tasks.

Task A [{src[:8]}]: {c.get('desc_a','')}
Task B [{dst[:8]}]: {c.get('desc_b','')}
Shared files: {', '.join(c.get('shared_files',[]))}
Semantic sim: {c.get('semantic_sim',0):.3f}
Time gap: {c.get('time_gap_hours',0):.1f}h

Answer ONLY JSON: {{"is_causal": true/false, "reason": "one sentence"}}
If task A caused task B, is_causal=true. Otherwise false. When unsure, false."""

    try:
        agents = disp_mod.load_agents()
        e_agents = agents.get("any", [])
        if not e_agents: return {"is_causal": False, "reason": "no_e_agent"}
        e_cfg = e_agents[0]
        api_key = os.environ.get(e_cfg.get("api_key_env", ""), "")
        if not api_key: return {"is_causal": False, "reason": "no_api_key"}
        base_url = e_cfg.get("base_url", "https://api.deepseek.com/v1")
        model = e_cfg.get("model", "deepseek-chat")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200, "temperature": 0.1}
        client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        resp = client.post(f"{base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r'\{[^}]+\}', raw)
        return json.loads(m.group()) if m else {"is_causal": False, "reason": "parse_error"}
    except Exception as e:
        try: import logging; logging.getLogger("qidian").warning("llm_judge_causal: %s", e)
        except Exception: pass
        return {"is_causal": False, "reason": f"llm_error:{e}"}
