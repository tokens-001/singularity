"""用「排序」代替「打分」评判方案，聚合成 Bradley-Terry 排名 + 置信区间。

为什么：绝对打分在不同调用间会漂移（同一份稿子实测 −16.5 翻 +5.5）。
排序只问「谁比谁好」，稳得多；一次调用得到完整顺序 → 分解成 C(4,2)=6 条成对胜负，
比逐对调用省 12 倍。

判据：**CI 不重叠 = 评委分得开这两个方案**；重叠 = 分不开，别下结论。
这比"差几分"诚实 —— 它把"评委天花板"变成了可观测信号。

用法: .venv/bin/python tests/integration/ab_rank.py [cache.json]
      AB_JUDGES=glm-5.3,kimi-k3  AB_REPEATS=2  AB_MAX_CHARS=0
"""
import os, sys, json, random, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入
from singularity.scheduler import execution_judge as ej

import pairwise as _pair

HERE = Path(__file__).resolve().parent
_ab = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("ab_fusion", HERE / "ab_fusion.py"))
importlib.util.spec_from_file_location("ab_fusion", HERE / "ab_fusion.py").loader.exec_module(_ab)
BRIEFS = _ab.BRIEFS

CACHE = HERE / os.environ.get("AB_CACHE", ".ab_debate_plans.json")
JUDGES = [j.strip() for j in os.environ.get("AB_JUDGES", "glm-5.3,kimi-k3").split(",")]
REPEATS = int(os.environ.get("AB_REPEATS", "2"))
JUDGE_MAX_TOKENS = int(os.environ.get("AB_JUDGE_MAX_TOKENS", "16000"))
MAX_CHARS = int(os.environ.get("AB_MAX_CHARS", "0"))
LABELS = ["甲", "乙", "丙", "丁", "戊", "己"]

RUBRIC = """你是架构评审员。下面是同一个需求的 {n} 份架构方案。

需求:
{brief}

{docs}

请**只按优劣排序**（不要打分），从最好到最差输出 JSON:
{{"order": ["甲","乙","丙","丁"], "reason": "一句话"}}"""


def rank_once(brief, docs, judge, seed):
    """一次排序 → 成对胜负列表 [(胜者idx, 败者idx)]，失败返回 None。"""
    order = list(range(len(docs)))
    random.Random(seed).shuffle(order)          # 打乱呈现顺序，抵消位置偏好
    blocks, mapping = [], {}
    for pos, idx in enumerate(order):
        lab = LABELS[pos]
        mapping[lab] = idx
        d = docs[idx]
        blocks.append(f"【方案{lab}】\n{d if MAX_CHARS <= 0 else d[:MAX_CHARS]}")
    prompt = RUBRIC.format(n=len(docs), brief=brief[:1500], docs="\n\n".join(blocks))
    raw = ej._call_model(prompt, judge, max_tokens=JUDGE_MAX_TOKENS)
    if not raw:
        return None
    d = ej.try_parse_json(raw)
    if not isinstance(d, dict) or not isinstance(d.get("order"), list):
        return None
    seq = [mapping[l] for l in d["order"] if l in mapping]
    if len(seq) != len(docs):
        return None                            # 顺序不完整 → 丢弃，别猜
    return [(seq[i], seq[j]) for i in range(len(seq)) for j in range(i + 1, len(seq))]


def main():
    if not CACHE.exists():
        print(f"没有缓存 {CACHE} —— 先跑 ab_debate_vs_ref.py")
        return
    cache = json.loads(CACHE.read_text())
    print(f"裁判: {JUDGES} | 每题重复: {REPEATS} | 缓存: {CACHE.name}\n" + "=" * 68, flush=True)
    verdicts = []
    for key in sorted(cache, key=int):
        v = cache[key]
        names = list(v["members"]) + ["融合", v["ref_model"]]
        docs = list(v["plans"]) + [v["fused"], v["ref"]]
        ref_name, fused_name = v["ref_model"], "融合"
        brief = BRIEFS[int(key) - 1]
        comps = []
        for judge in JUDGES:
            for r in range(REPEATS):
                pairs = rank_once(brief, docs, judge, seed=f"{key}-{judge}-{r}")
                if not pairs:
                    print(f"  [{key}] {judge} 第{r+1}次排序失败", flush=True)
                    continue
                comps += [(names[w], names[l]) for w, l in pairs]
                top = pairs[0][0]
                print(f"  [{key}] {judge} 第{r+1}次 头名 = {names[top]}", flush=True)
        if not comps:
            continue
        r = _pair.bt_ratings(comps)
        ci = _pair.bootstrap_ci(comps, n=300, seed=42)
        print(f"\n[brief {key}] {len(comps)} 条成对胜负")
        for n, val in sorted(r.items(), key=lambda kv: -kv[1]):
            lo, hi = ci[n]
            print(f"    {n:<30}{val:>8.3f}   [{lo:.2f}, {hi:.2f}]")
        if fused_name in ci and ref_name in ci:
            ok = _pair.separable(ci[fused_name], ci[ref_name])
            verdicts.append(ok)
            print(f"    融合 vs {ref_name}: " + ("分得开" if ok else "**分不开（CI 重叠）**"))
        print(flush=True)

    if verdicts:
        print("=" * 68)
        print(f"分得开的题数: {sum(verdicts)}/{len(verdicts)}")


if __name__ == "__main__":
    main()
