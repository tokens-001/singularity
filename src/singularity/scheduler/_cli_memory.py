__all__ = ['_cmd_memory']

"""CLI sub-commands."""
import json, os, sys, time
from pathlib import Path
from singularity.scheduler import config, tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import orchestrator
from singularity.scheduler.tracker import TaskStatus

def _cmd_memory(argv: list) -> int:
    """scheduler memory stats|rebuild|query|latent|traverse [参数]"""
    from . import memory as mem_mod

    if not argv:
        print("用法: scheduler memory stats|rebuild|query|latent|traverse [参数]",
              file=sys.stderr)
        return 2

    sub = argv[0]
    if sub == "stats":
        s = mem_mod.stats()
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    if sub == "rebuild":
        config.ensure_dirs()
        n = mem_mod.rebuild_from_traces()
        print(f"[memory] 从 traces 重建: {n} 条任务已索引")
        return 0

    if sub == "latent":
        candidates = mem_mod.find_candidate_latent_edges()
        print(f"慢通道候选: {len(candidates)} 对")
        for c in candidates[:10]:
            print(f"  {c['task_a'][-8:]} ↔ {c['task_b'][-8:]} "
                  f"共享:{c['shared_files']} sim={c['semantic_sim']} gap={c['time_gap_hours']}h")
        return 0

    if sub == "chain" and len(argv) >= 2:
        task_id = argv[1]
        direction = "up"
        if "--down" in argv:
            direction = "down"
        elif "--both" in argv:
            direction = "both"
        chain = mem_mod.find_causal_chain(task_id, direction=direction)
        print(f"因果链 ({direction}): {len(chain)} 个关联任务")
        for c in chain:
            indent = "  " * c["depth"]
            print(f"{indent}{c['task_id'][-8:]} [{c['depth']}] {c['description'][:60]}")
        return 0

    if sub == "traverse" and len(argv) >= 2:
        rest, _ = _parse_concurrent(argv[1:])
        query_text = " ".join(rest) if rest else ""
        beam = 3
        hops = 3
        i = 0
        while i < len(argv):
            if argv[i] == "--beam" and i + 1 < len(argv):
                beam = int(argv[i+1]); i += 2; continue
            if argv[i] == "--hops" and i + 1 < len(argv):
                hops = int(argv[i+1]); i += 2; continue
            i += 1
        result = mem_mod.traverse(query_text, beam_width=beam, max_hops=hops)
        narrative = mem_mod.synthesize(result, query_text)
        print(json.dumps(narrative, ensure_ascii=False, indent=2))
        return 0

    if sub == "query" and len(argv) >= 2:
        rest, _ = _parse_concurrent(argv[1:])
        query_text = " ".join(rest) if rest else ""
        # 提取 --files
        files = None
        i = 0
        while i < len(argv):
            if argv[i] == "--files" and i + 1 < len(argv):
                files = [f.strip() for f in argv[i + 1].split(",")]
                i += 2
                continue
            i += 1

        result = mem_mod.query(query_text, files=files)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("用法: scheduler memory stats|rebuild|query|latent|traverse [参数]",
          file=sys.stderr)
    return 2


# ═══════════════════════════════════════════════════════════
# project 子命令
# ═══════════════════════════════════════════════════════════

