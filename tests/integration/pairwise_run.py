"""用配对比较代替绝对打分，看结论稳不稳。

要回答的不是"融合赢几分"，是「同档裁判在配对范式下分不分得开选手」——
CI 重叠 = 评委根本分不开，那 +3 / -5 都是噪声，讨论没意义。

对照设计：同一批稿子，同档裁判（glm-5.2）与跨档裁判（deepseek-v4-pro）各跑一遍。
如果同档裁判在配对范式下也能稳定分开 → 说明之前的 ±9 是「绝对打分」这个范式的问题，
不是「同档判断力不足」—— 那验证困境就有解了。

用法:
    .venv/bin/python tests/integration/pairwise_run.py
    AB_PJUDGES=glm-5.2,deepseek-v4-pro  AB_PREPEAT=3  AB_PBRIEF=3
"""
import os, sys, json, random, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入
from singularity.scheduler import config as _cfg
from singularity.scheduler import execution_judge as ej

_MODEL_MAX_TOKENS = _cfg.MODEL_MAX_TOKENS

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


_pw = _load("pairwise", HERE / "pairwise.py")
_ab = _load("ab_fusion", HERE / "ab_fusion.py")

BRIEF_NO = int(os.environ.get("AB_PBRIEF", "3"))
# 数据来源：默认本脚本配套的 .v2_plans.json（单 brief，带 members）。
# AB_PCACHE 可指向别的缓存（如 .ab_cache_3v_nodebate.json，多 brief、无 members），
# AB_PBRIEFS 指定要跑哪几个。
CACHE = HERE / os.environ.get("AB_PCACHE", ".v2_plans.json")
BRIEFS_SEL = [int(x) for x in os.environ.get("AB_PBRIEFS", str(BRIEF_NO)).split(",")]
JUDGES = [j.strip() for j in os.environ.get("AB_PJUDGES", "glm-5.2").split(",")]
REPEAT = int(os.environ.get("AB_PREPEAT", "2"))

PROMPT = """你是架构评审员。下面是同一个需求的两份架构方案，请判断哪一份更好。

需求:
{brief}

【方案甲】
{a}

【方案乙】
{b}

只输出 JSON: {"winner": "甲" 或 "乙", "reason": "一句话理由"}"""


def _compare(brief: str, name_a: str, text_a: str, name_b: str, text_b: str, judge: str):
    """比较两份稿子。随机左右位置消除位置偏好，返回（胜者名, 理由）或 None。"""
    flip = random.random() < 0.5
    first, second = (text_b, text_a) if flip else (text_a, text_b)
    prompt = PROMPT.replace("{brief}", brief[:1500]).replace("{a}", first).replace("{b}", second)
    # 额度必须给足：glm-5.2 是思考模型，给 2000 会把额度全烧在思考上 → content 空 →
    # 解析失败（实测 12 次里失败 4 次）。这一坑今天在别处刚踩过一遍。
    raw = ej._call_model(prompt, judge, max_tokens=_MODEL_MAX_TOKENS)
    d = ej.try_parse_json(raw) if raw else None
    if not isinstance(d, dict) or d.get("winner") not in ("甲", "乙"):
        return None
    picked_first = d["winner"] == "甲"
    winner = (name_b if picked_first else name_a) if flip else (name_a if picked_first else name_b)
    return winner, str(d.get("reason", ""))[:60], picked_first


def _load_docs(brief_no: int) -> dict:
    """载入某个 brief 的选手稿 + 融合稿。有 members 就用真名，否则用位置标签。"""
    store = json.loads(CACHE.read_text())
    v = store[str(brief_no)]
    raw = v["plans"] if isinstance(v, dict) else v
    members = v.get("members") if isinstance(v, dict) else None
    # 两种缓存格式：.v2_plans.json 是 [(模型名, 文本), ...]，.ab_cache*.json 是 [文本, ...]
    if raw and isinstance(raw[0], (list, tuple)):
        members = [m for m, _ in raw]
        plans = [p for _, p in raw]
    else:
        plans = raw
        if not members:
            members = [f"方案{i+1}" for i in range(len(plans))]
    docs = {m: p for m, p in zip(members, plans)}
    fused = v.get("fused") if isinstance(v, dict) else None
    if fused:
        docs["融合"] = fused
    else:
        f = Path(f"/tmp/v2_fused_{brief_no}.json")
        if f.exists():
            docs["融合"] = f.read_text()
    return docs


def main():
    print(f"缓存 {CACHE.name} | briefs {BRIEFS_SEL} | 裁判 {JUDGES} | 每对 {REPEAT}×双向\n")

    for brief_no in BRIEFS_SEL:
      docs = _load_docs(brief_no)
      brief = _ab.BRIEFS[brief_no - 1]
      members = [m for m in docs if m != "融合"]
      print(f"##### brief {brief_no} | 选手 {members} #####", flush=True)

      for judge in JUDGES:
        print(f"=== 裁判 {judge} ===")
        comparisons, log, first_picks = [], [], []
        # 比哪些对：融合 vs 每个单稿（核心问题），以及单稿互比（做参照锚）
        pairs = [("融合", m) for m in members]
        for x, y in pairs:
            for rep in range(REPEAT):
                # 双向各跑一次 → 位置偏好会互相抵消，也顺带量出自我一致率
                for a, b in ((x, y), (y, x)):
                    r = _compare(brief, a, docs[a], b, docs[b], judge)
                    if r is None:
                        print(f"  {a} vs {b}: 解析失败", flush=True)
                        continue
                    comparisons.append((r[0], b if r[0] == a else a))
                    first_picks.append(r[2])
                    # 逐条实时打印：跑完才打印的话，几十分钟的长跑完全看不到进度
                    line = f"  {a:>6} vs {b:<16} → 胜: {r[0]:<16} {r[1]}"
                    log.append(line)
                    print(line, flush=True)
        if not comparisons:
            print("  无有效比较\n")
            continue
        n_first = sum(first_picks)
        ratio = n_first / len(first_picks)
        print(f"  位置偏好: 选甲位 {n_first}/{len(first_picks)} 次" +
              ("  ← 可疑，几乎总选第一个" if ratio > 0.8 else ""))
        r = _pw.bt_ratings(comparisons)
        ci = _pw.bootstrap_ci(comparisons, n=300, seed=0)
        print(f"\n  BT 评分: " + "  ".join(f"{k}={v:.2f}" for k, v in sorted(r.items())))
        for k in sorted(ci):
            print(f"    {k:<8} CI {ci[k][0]:.2f} ~ {ci[k][1]:.2f}")
        print()
        # 核心问题：融合和最强单稿分得开吗
        best_member = max(members, key=lambda m: r.get(m, 0))
        print(f"  融合 vs 最强单稿({best_member}): " +
              ("分得开 ✅" if _pw.separable(ci.get("融合", (0, 0)), ci.get(best_member, (0, 0)))
               else "CI 重叠 ❌ —— 这个裁判分不开，差距在噪声内"))
        print()


if __name__ == "__main__":
    main()
