"""模型能力基准：同一批题，每个模型单独出稿，同一把尺子打分 → 能力排序。

为什么需要：`models.toml` 的 rating 实测不可靠（三家同标 S，实测差 16 分）。
不知道各模型真实水平，就没法挑「同档异源」的委员会阵容，辩论/融合的 A/B 也无从解释。

⚠️ 旧的 `.ab_cache*.json` 不能用于基准 —— `ab_fusion.run_committee` 只截获纯文本，
没存模型名，稿子无法署名。基准必须逐模型单独出稿。

用法: .venv/bin/python tests/integration/bench_models.py [重复次数 默认2]
      BENCH_MODELS=a,b,c   AB_JUDGE=deepseek-v4-pro
"""
import os, sys, json, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入
from singularity.scheduler import _dispatch_exec as de

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_ab = _load("ab_fusion", HERE / "ab_fusion.py")
_bl = _load("ab_best_baseline", HERE / "ab_best_baseline.py")
BRIEFS = _ab.BRIEFS

MODELS = [m.strip() for m in os.environ.get(
    "BENCH_MODELS",
    "deepseek-v4-flash,glm-5.3-flash,kimi-k2.7-code-highspeed,glm-5.2").split(",") if m.strip()]
JUDGE = os.environ.get("AB_JUDGE", "deepseek-v4-pro")
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

PLANS = HERE / ".bench_plans.json"
SOLO_SEED = HERE / ".ab_solo_cache.json"   # glm-5.2 的稿子已在这里，别重复生成

# 原 5 维全是「覆盖度」→ 长稿通吃。第 6 维压一下篇幅偏向。
RUBRIC = """你是架构评审员。下面是同一个需求的 {n} 份架构方案。

需求:
{brief}

{docs}

按 6 个维度各打 0-10 分:
1. modules: 模块划分职责单一、依赖合理
2. data: 数据模型完整（实体/字段/关系/索引）
3. api: API 契约明确（方法/路径/输入/输出/错误）
4. tasks: 任务拆解可并行、粒度合适
5. risk: 约束与风险覆盖到位、可验证
6. design: 设计取舍有理由（为什么这样分模块/为什么这个数据模型），不是罗列清单

只输出 JSON（不要其他内容），每份一个键，total 是六项之和:
{{"甲":{{"modules":0,"data":0,"api":0,"tasks":0,"risk":0,"design":0,"total":0}}, ...}}"""

_bl.RUBRIC = RUBRIC
_bl.JUDGE_MODEL = JUDGE
_bl.MAX_CHARS = 0


def get_plan(model, i, brief, store):
    """出稿，落盘缓存。返回 str 或 None。"""
    key = f"{model}|{i}"
    if store.get(key):
        return store[key]
    if model == "glm-5.2" and SOLO_SEED.exists():
        seed = json.loads(SOLO_SEED.read_text())
        if seed.get(str(i)):
            store[key] = seed[str(i)]
            PLANS.write_text(json.dumps(store, ensure_ascii=False))
            return store[key]
    out = de._run_no_tools({"model": model}, brief, f"bench{i}_{model[:8]}", "any")
    if out:
        store[key] = out
        PLANS.write_text(json.dumps(store, ensure_ascii=False))
    return out


def main():
    print(f"被测: {MODELS}\n评委: {JUDGE} | 重复: {REPEATS} | 截断: 关\n" + "=" * 68, flush=True)
    store = json.loads(PLANS.read_text()) if PLANS.exists() else {}
    scores = {}                                   # model -> brief -> [total,...]
    for i, brief in enumerate(BRIEFS, 1):
        docs, names = [], []
        for m in MODELS:
            p = get_plan(m, i, brief, store)
            if not p:
                print(f"  ✗ {m} brief{i} 无产出", flush=True)
                continue
            names.append(m)
            docs.append(p)
        if len(docs) < 2:
            print(f"[brief {i}] 有效稿不足，跳过", flush=True)
            continue
        print(f"[brief {i}] {[(n, len(d)) for n, d in zip(names, docs)]}", flush=True)
        for r in range(REPEATS):
            sc = _bl.score_all(brief, docs)
            if not sc:
                print(f"  第{r+1}次打分失败", flush=True)
                continue
            for j, n in enumerate(names):
                scores.setdefault(n, {}).setdefault(i, []).append(sc[j])
            print(f"  第{r+1}次: " + "  ".join(f"{n[:14]}={sc[j]}" for j, n in enumerate(names)), flush=True)

    if not scores:
        print("没有有效结果")
        return
    print("\n" + "=" * 68)
    briefs = sorted({i for d in scores.values() for i in d})
    head = f"{'model':<28}" + "".join(f"{'b'+str(b):>7}" for b in briefs) + f"{'均值':>8}{'字数':>8}"
    print(head)
    rows = []
    for m in MODELS:
        if m not in scores:
            continue
        per = {b: sum(v) / len(v) for b, v in scores[m].items()}
        avg = sum(per.values()) / len(per)
        chars = [len(store.get(f"{m}|{b}", "")) for b in briefs]
        rows.append((avg, m, per, chars))
        print(f"{m:<28}" + "".join(f"{per.get(b, 0):>7.1f}" for b in briefs)
              + f"{avg:>8.1f}{sum(chars)//max(len(chars),1):>8}")
    print("\n按均值排序:")
    for avg, m, per, chars in sorted(rows, reverse=True):
        print(f"  {avg:5.1f}  {m}")


if __name__ == "__main__":
    main()
