"""架构辩论能不能"以弱胜强"：v4-pro + glm-5.2 辩论融合，能否追平/超过 glm-5.3-flash 单干？

判据（用户定，2026-09-10）：
  融合 − glm-5.3-flash  ≥ +3   → 超过
  融合 − glm-5.3-flash  ∈ (-3,3) → 追平
  融合 − glm-5.3-flash  ≤ −3   → 落后
（±3 = 实测评委噪声；不预先定死的话，事后容易把 ±2 说成"追平"）

注意：负向结论不能直接否定辩论 —— 可能是 ① 裁判偏心 ② 样本不够
③ 5.3-flash 在架构任务上本来就强于榜单排名。正向结论才有说服力。

关键：走真实生产路径（不替换 fuse_architecture），跑完读 .qidian/.last_fusion.json
拿署名 —— 这是之前两轮实验丢掉的归因。

用法: .venv/bin/python tests/integration/ab_debate_vs_ref.py [brief数 默认1]
      AB_REPEATS=2  AB_REF=glm-5.3-flash  AB_JUDGES=glm-5.3,kimi-k3
"""
import os, sys, json, time, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp
from singularity.scheduler import _dispatch_exec as de
from singularity.scheduler import config as cfg

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_ab = _load("ab_fusion", HERE / "ab_fusion.py")
_bl = _load("ab_best_baseline", HERE / "ab_best_baseline.py")
BRIEFS = _ab.BRIEFS

MEMBERS = [m.strip() for m in os.environ.get("AB_MEMBERS", "deepseek-v4-pro,glm-5.2").split(",")]
REF = os.environ.get("AB_REF", "glm-5.3-flash")
JUDGES = [j.strip() for j in os.environ.get("AB_JUDGES", "glm-5.3,kimi-k3").split(",")]
REPEATS = int(os.environ.get("AB_REPEATS", "2"))
THRESH = 3.0
PLANS = HERE / ".ab_debate_plans.json"

_bl.MAX_CHARS = 0
_bl.JUDGE_MAX_TOKENS = 16000

# ⚠️ 生产默认：v4-pro（reasoning=true）被判为 slow → 只出初稿、不参与辩论，
# 实际只有 glm-5.2 单边评审。本实验要测「两个都辩论」，故脚本内临时解除这道闸。
# 与生产默认不同 —— 结论只对「两个都辩论」这个假设情形成立。
if os.environ.get("AB_FORCE_DEBATE", "1") == "1":
    de._is_slow_model = lambda m: False
    print("⚠️ 已解除慢模型闸：两个成员都参与辩论（生产默认 v4-pro 不辩论）", flush=True)


def run_committee(brief, i):
    """走真实路径跑委员会（含辩论+融合），返回 (署名的稿子, 融合稿, 耗时)。"""
    chain = [{"model": m} for m in MEMBERS]
    meta_path = cfg.QIDIAN_DIR / ".last_fusion.json"
    if meta_path.exists():
        meta_path.unlink()                       # 确保读到的是这一次的
    t0 = time.time()
    de._dispatch_committee(brief, "any", f"abref{i}", {}, chain)
    dt = time.time() - t0
    if not meta_path.exists():
        return None, None, dt
    meta = json.loads(meta_path.read_text())
    return list(zip(meta["models"], meta["outputs"])), meta["fused"], dt


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"阵容: {MEMBERS} → 辩论 → 融合 | 参照: {REF} | 裁判: {JUDGES} | 重复: {REPEATS}")
    print(f"判据: |融合−参照| < {THRESH} 为追平\n" + "=" * 72, flush=True)

    store = json.loads(PLANS.read_text()) if PLANS.exists() else {}
    results = []
    for i, brief in enumerate(BRIEFS[:n], 1):
        key = str(i)
        if key in store:
            v = store[key]
            print(f"[{i}] 用缓存（{v['dt']:.0f}s）", flush=True)
        else:
            print(f"[{i}] 跑委员会… {brief[:34]}…", flush=True)
            plans, fused, dt = run_committee(brief, i)
            if not plans or not fused:
                print(f"  ✗ 无产出（{dt:.0f}s）—— 看 .qidian/heartbeats 的 warn: 告警", flush=True)
                continue
            print(f"  委员会+辩论+融合 {dt:.0f}s | 成员稿 " +
                  " ".join(f"{m}={len(p)}" for m, p in plans) + f" | 融合={len(fused)}", flush=True)
            ref = de._run_no_tools({"model": REF}, brief, f"abref{i}_ref", "any")
            if not ref:
                print(f"  ✗ 参照 {REF} 无产出", flush=True)
                continue
            print(f"  参照 {REF}={len(ref)} 字", flush=True)
            v = {"members": [m for m, _ in plans], "plans": [p for _, p in plans],
                 "fused": fused, "ref": ref, "ref_model": REF, "dt": dt}
            store[key] = v
            PLANS.write_text(json.dumps(store, ensure_ascii=False))

        names = v["members"]
        docs = list(v["plans"]) + [v["fused"], v["ref"]]
        labels_desc = names + ["融合", v["ref_model"]]
        idx_ref = len(docs) - 1

        for judge in JUDGES:
            _bl.JUDGE_MODEL = judge
            for r in range(REPEATS):
                sc = _bl.score_all(brief, docs)
                if not sc:
                    print(f"  裁判 {judge} 第{r+1}次打分失败", flush=True)
                    continue
                line = "  ".join(f"{labels_desc[j]}={sc[j]}" for j in range(len(docs)))
                diff = sc[len(docs) - 2] - sc[idx_ref]
                verdict = "超过" if diff >= THRESH else ("追平" if diff > -THRESH else "落后")
                print(f"  裁判 {judge} 第{r+1}次: {line}  → 融合−{v['ref_model']}={diff:+d} [{verdict}]", flush=True)
                results.append({"brief": i, "judge": judge, "scores": sc,
                                "names": labels_desc, "diff": diff})

    if not results:
        print("没有有效结果")
        return
    print("\n" + "=" * 72)
    for r in results:
        print(f"brief{r['brief']} {r['judge']:<14} 融合−参照 = {r['diff']:+d}")
    avg = sum(r["diff"] for r in results) / len(results)
    verdict = "超过" if avg >= THRESH else ("追平" if avg > -THRESH else "落后")
    print(f"\n均值 {avg:+.1f} → {verdict}（{len(results)} 次测量）")


if __name__ == "__main__":
    main()
