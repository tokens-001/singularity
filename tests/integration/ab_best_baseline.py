"""公平对照：融合 vs「N 份里最好的那一份」。

现有 A/B 的 B 是 plans[0]（链上第一个模型），不是最强的 —— 会放大融合的优势。
本脚本一次调用给 N 份单稿 + 1 份融合稿打同一把尺子的分（同一次调用内评分标准一致，
避免跨调用漂移），然后比 融合 vs max(单稿)。

用法: .venv/bin/python tests/integration/ab_best_baseline.py <cache.json> [<cache2.json> ...]
"""
import sys, json, random, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入
from singularity.scheduler import execution_judge as ej

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ab_fusion", _here / "ab_fusion.py")
_ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ab)
BRIEFS = _ab.BRIEFS

JUDGE_MODEL = "glm-5.2"
LABELS = ["甲", "乙", "丙", "丁", "戊", "己"]
MAX_CHARS = 10000          # 每份截断，防止 prompt 过长；四份同等对待，相对比较仍成立

RUBRIC = """你是架构评审员。下面是同一个需求的 {n} 份架构方案。

需求:
{brief}

{docs}

按 5 个维度各打 0-10 分:
1. modules: 模块划分职责单一、依赖合理
2. data: 数据模型完整（实体/字段/关系/索引）
3. api: API 契约明确（方法/路径/输入/输出/错误）
4. tasks: 任务拆解可并行、粒度合适
5. risk: 约束与风险覆盖到位、可验证

只输出 JSON（不要其他内容），每份一个键，total 是五项之和:
{{"甲":{{"modules":0,"data":0,"api":0,"tasks":0,"risk":0,"total":0}}, ...}}"""


def score_all(brief, docs):
    """一次调用给所有文档打分。返回 {原索引: 分数} 或 None。"""
    n = len(docs)
    order = list(range(n))
    random.shuffle(order)                       # 打乱顺序，避免位置偏好
    blocks, mapping = [], {}
    for pos, idx in enumerate(order):
        lab = LABELS[pos]
        mapping[lab] = idx
        blocks.append(f"【方案{lab}】\n{docs[idx][:MAX_CHARS]}")
    prompt = RUBRIC.format(n=n, brief=brief[:1500], docs="\n\n".join(blocks))
    raw = ej._call_model(prompt, JUDGE_MODEL, max_tokens=4000)
    if not raw:
        return None
    d = ej.try_parse_json(raw)
    if not isinstance(d, dict):
        return None
    out = {}
    for lab, idx in mapping.items():
        v = d.get(lab)
        if not isinstance(v, dict) or "total" not in v:
            return None
        out[idx] = v["total"]
    return out


def main():
    files = sys.argv[1:]
    if not files:
        print("用法: ab_best_baseline.py <cache.json> [...]")
        return
    for f in files:
        cache = json.loads(Path(f).read_text())
        print(f"\n{'='*66}\n{f}  （{len(cache)} 组）")
        print(f"{'brief':>6} {'融合':>6} {'最好单稿':>9} {'第一份':>7} {'差(融合-最好)':>13}")
        diffs, win = [], 0
        for k in sorted(cache):
            i = int(k) - 1
            v = cache[k]
            docs = list(v["plans"]) + [v["fused"]]
            sc = score_all(BRIEFS[i], docs)
            if not sc:
                print(f"{k:>6}   ✗ 打分失败")
                continue
            fused = sc[len(v["plans"])]
            best = max(sc[j] for j in range(len(v["plans"])))
            first = sc[0]
            diffs.append(fused - best)
            win += fused > best
            print(f"{k:>6} {fused:>6} {best:>9} {first:>7} {fused-best:>+13}")
        if diffs:
            print(f"{'均值':>6} {'':>6} {'':>9} {'':>7} {sum(diffs)/len(diffs):>+13.1f}")
            print(f"融合胜 {win}/{len(diffs)}")


if __name__ == "__main__":
    main()
