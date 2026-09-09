"""成对比较 → Bradley-Terry 排名 + bootstrap 置信区间。

为什么不用「打 0-10 分」：绝对分在不同调用间会漂移（实测同一份稿子，
只因为让不让评委看全，从 −16.5 翻到 +5.5）。成对判断稳得多 ——
"甲比乙好"即使评委水平一般也判得准，而且聚合后自带置信区间。

CI 重叠 = 评委分不开这两个方案 —— 把"评委天花板"从感觉变成可观测信号。

方法参考 lmarena/arena-rank（其实现依赖 JAX，这里用纯 Python 重写，够小就够用）。

用法（库）:
    from pairwise import bt_ratings, bootstrap_ci
    comparisons = [("甲", "乙"), ("甲", "丙"), ("乙", "丙")]   # 每条 = 一次"前者胜"
    bt_ratings(comparisons)
    bootstrap_ci(comparisons, n=200, seed=0)

自检: .venv/bin/python tests/integration/pairwise.py
"""
import random
from collections import defaultdict

# BT 评分上下限。全胜的选手 MLE 无上界、全败的无下界（bootstrap 里很容易出现，
# 实测归一后 CI 上界爆到 7 万）。夹在 [0.1, 10] 后 CI 变成"≥/≤某值"，可比较、可判定。
# 真要更严谨得上 Hessian 正则（arena-rank 的做法）。
_RATING_FLOOR, _RATING_CAP = 0.1, 10.0


def bt_ratings(comparisons, iters=500, eps=1e-9):
    """Bradley-Terry 极大似然（MM 算法）。comparisons: [(winner, loser), ...]。

    返回 {选手: 评分}，几何均值归一到 1.0。平局请拆成两条相反方向传入。
    """
    models = sorted({m for pair in comparisons for m in pair})
    if not models:
        return {}
    wins = defaultdict(float)
    games = defaultdict(float)          # (i,j) 对局数，无序
    for w, l in comparisons:
        wins[w] += 1
        games[(w, l) if w <= l else (l, w)] += 1

    r = {m: 1.0 for m in models}
    for _ in range(iters):
        new = {}
        for i in models:
            denom = 0.0
            for (a, b), n in games.items():
                if a == i or b == i:
                    j = b if a == i else a
                    denom += n / (r[i] + r[j])
            v = wins[i] / denom if denom > eps and wins[i] > 0 else _RATING_FLOOR
            new[i] = min(max(v, _RATING_FLOOR), _RATING_CAP)
        # 归一（几何均值 = 1），避免整体漂移；上下限保证 gmean 不塌缩
        gmean = 1.0
        for v in new.values():
            gmean *= v
        gmean **= 1.0 / len(new)
        r = {k: min(max(v / gmean, _RATING_FLOOR), _RATING_CAP) for k, v in new.items()}
    return r


def bootstrap_ci(comparisons, n=200, seed=0, lo=2.5, hi=97.5):
    """对比较结果重采样 n 次，返回 {选手: (下界, 上界)}（百分位法）。"""
    models = sorted({m for pair in comparisons for m in pair})
    if not models:
        return {}
    rnd = random.Random(seed)
    samples = defaultdict(list)
    for _ in range(n):
        resample = [comparisons[rnd.randrange(len(comparisons))] for _ in comparisons]
        for m, v in bt_ratings(resample).items():
            samples[m].append(v)
    out = {}
    for m in models:
        vals = sorted(samples[m])
        if not vals:
            out[m] = (0.0, 0.0)
            continue
        out[m] = (vals[int(len(vals) * lo / 100)], vals[min(int(len(vals) * hi / 100), len(vals) - 1)])
    return out


def separable(ci_a, ci_b):
    """两个选手的置信区间是否不重叠（= 评委能把他俩分开）。"""
    return ci_a[1] < ci_b[0] or ci_b[1] < ci_a[0]


def _demo():
    # 甲明显强于乙、乙明显强于丙 → 排序应正确且 甲/丙 的 CI 不重叠
    comps = [("甲", "乙")] * 8 + [("乙", "甲")] * 2 \
          + [("乙", "丙")] * 8 + [("丙", "乙")] * 2 \
          + [("甲", "丙")] * 9 + [("丙", "甲")] * 1
    r = bt_ratings(comps)
    assert r["甲"] > r["乙"] > r["丙"], r
    ci = bootstrap_ci(comps, n=60, seed=42)
    assert separable(ci["甲"], ci["丙"]), ci          # 差距大 → 分得开
    # 五五开 → 不应分得开
    even = [("甲", "乙")] * 5 + [("乙", "甲")] * 5
    ci2 = bootstrap_ci(even, n=60, seed=42)
    assert not separable(ci2["甲"], ci2["乙"]), ci2
    print("pairwise self-check OK")
    print("  甲/乙/丙 评分:", {k: round(v, 3) for k, v in r.items()})
    print("  CI:", {k: (round(a, 2), round(b, 2)) for k, (a, b) in ci.items()})


if __name__ == "__main__":
    _demo()
