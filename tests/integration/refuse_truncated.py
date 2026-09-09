"""只重跑「融合」这一步，验证截断假设。

背景：三题融合稿全部撞 max_tokens=16000 被腰斩，分数和"截断到哪"完美单调
（写完的 43-45、写一半的 38-39、没写到的 32）。所以"融合低于输入"很可能是
截断造成的，不是机制缺陷。

不用重跑委员会 —— 成员稿都在缓存里，只重做 fuse_architecture + 一次打分。

用法: .venv/bin/python tests/integration/refuse_truncated.py [brief号 默认3]
      AB_CACHE=.ab_debate_plans_v2.json  QIDIAN_FUSION_MAX_TOKENS=32000  AB_JUDGE=kimi-k3
"""
import os, sys, json, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入
from singularity.scheduler import execution_judge as ej

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_ab = _load("ab_fusion", HERE / "ab_fusion.py")
_bl = _load("ab_best_baseline", HERE / "ab_best_baseline.py")
BRIEFS = _ab.BRIEFS

CACHE = HERE / os.environ.get("AB_CACHE", ".ab_debate_plans_v2.json")
JUDGE = os.environ.get("AB_JUDGE", "kimi-k3")
BRIEF_NO = int(sys.argv[1]) if len(sys.argv) > 1 else 3

_bl.MAX_CHARS = 0
_bl.JUDGE_MODEL = JUDGE
_bl.JUDGE_MAX_TOKENS = int(os.environ.get("AB_JUDGE_MAX_TOKENS", "16000"))


def main():
    cache = json.loads(CACHE.read_text())
    v = cache[str(BRIEF_NO)]
    brief = BRIEFS[BRIEF_NO - 1]
    plans = list(v["plans"])

    print(f"brief {BRIEF_NO} | 裁判 {JUDGE} | 定稿 max_tokens={ej._FUSION_MAX_TOKENS}")
    print(f"成员稿: " + " ".join(f"{m}={len(p)}" for m, p in zip(v["members"], plans)), flush=True)

    print("重跑融合…", flush=True)
    new_fused = ej.fuse_architecture(brief, plans)
    print(f"  新融合稿 {len(new_fused)} 字 | tasks={'✓' if '\"tasks\"' in new_fused else '✗'} "
          f"risks={'✓' if '\"risks\"' in new_fused else '✗'}", flush=True)

    docs = plans + [new_fused, v["ref"]]
    names = list(v["members"]) + ["新融合", v["ref_model"]]
    sc = _bl.score_all(brief, docs)
    if not sc:
        print("打分失败")
        return
    print("\n同池打分:")
    for j, n in enumerate(names):
        print(f"  {n:<22} {sc[j]}")
    best = max(sc[j] for j in range(len(plans)))
    fused_i, ref_i = len(plans), len(docs) - 1
    print(f"\n新融合 {sc[fused_i]} vs 最好成员 {best} → {sc[fused_i] - best:+d}")
    print(f"新融合 {sc[fused_i]} vs 参照 {sc[ref_i]} → {sc[fused_i] - sc[ref_i]:+d}")
    print(f"\n对比：旧融合稿 {len(v['fused'])} 字（截断）")


if __name__ == "__main__":
    main()
