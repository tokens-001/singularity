"""跑一次真 v2 融合，验证两件事：输出有没有变短（不膨胀）+ 融合 ≥ max(输入)。

用缓存里的成员稿（不重跑委员会），只跑 ②③④ 这几步。
_call_model 被包了一层记录 prompt/回复，跑完把对话过程打出来 —— 别只看分数。

用法: .venv/bin/python tests/integration/v2_fusion_run.py [brief号 默认3]
      AB_CACHE=.ab_debate_plans_v2.json  AB_JUDGE=kimi-k3  QIDIAN_FUSION_V2_ROUNDS=5
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
BRIEFS, arch_task = _ab.BRIEFS, _ab.arch_task
_bl = _load("ab_best_baseline", HERE / "ab_best_baseline.py")
BRIEF_NO = int(sys.argv[1]) if len(sys.argv) > 1 else 3
# 委员会很贵，跑过就存下来，重跑融合时不用再花钱
PLANS_CACHE = HERE / ".v2_plans.json"

_bl.MAX_CHARS = 0
_bl.JUDGE_MODEL = os.environ.get("AB_JUDGE", "kimi-k3")
_bl.JUDGE_MAX_TOKENS = int(os.environ.get("AB_JUDGE_MAX_TOKENS", "16000"))

STEPS = [("② 提取三类", "架构委员会秘书"), ("③ 轮1 陈述", "陈述己方理由"),
         ("③ 回应", "逐条回应"), ("③ 确认", "对你的论证给出了回应"),
         ("④ 定稿", "架构定稿人"), ("④ 确认", "检查三件事")]


def _step_of(prompt: str) -> str:
    for name, marker in STEPS:
        if marker in prompt:
            return name
    return "?"


def _get_plans(brief_no: int) -> list[tuple[str, str]]:
    """方案来源：AB_PLANS 指定缓存则复用，否则用**生产形态**任务现跑一次委员会初稿。

    旧缓存（.ab_debate_plans_v2.json 等）是拿裸 brief 跑的 —— 模型各写各的格式，
    不代表生产。要比就得用 arch_task() 的完整任务重跑。
    """
    if os.environ.get("AB_PLANS"):
        v = json.loads(Path(os.environ["AB_PLANS"]).read_text())[str(brief_no)]
        print("（复用缓存方案 —— 旧缓存是裸 brief 跑的，不代表生产）")
        return list(zip(v["members"], v["plans"]))
    if PLANS_CACHE.exists():
        d = json.loads(PLANS_CACHE.read_text())
        if str(brief_no) in d:
            print("（复用本脚本上次跑的委员会初稿）")
            return [tuple(x) for x in d[str(brief_no)]]
    import singularity.scheduler.dispatcher as disp
    import singularity.scheduler._dispatch_exec as de
    de._debate = lambda task, members, chain, task_id, **kw: members   # v2 自带对话
    agents = disp.load_agents()
    chain = disp.pick_agent_fallback_chain(agents, "any")
    print(f"跑委员会初稿（生产形态任务，{len(chain)} 席）…", flush=True)
    pairs = _ab.run_committee(arch_task(BRIEFS[brief_no - 1]), agents, chain, f"v2_{brief_no}")
    d = json.loads(PLANS_CACHE.read_text()) if PLANS_CACHE.exists() else {}
    d[str(brief_no)] = pairs
    PLANS_CACHE.write_text(json.dumps(d, ensure_ascii=False))
    return pairs


def main():
    brief = BRIEFS[BRIEF_NO - 1]
    task = arch_task(brief)                 # 融合用生产形态任务；打分用短 brief
    plans = _get_plans(BRIEF_NO)

    log = []
    real = ej._call_model

    def spy(prompt, model, max_tokens=2000):
        out = real(prompt, model, max_tokens)
        log.append((_step_of(prompt), model, prompt, out))
        return out

    ej._call_model = spy
    # AB_EXTRACT 可换掉提取模型：fusion.toml 默认的 glm-5.3 是思考模型，
    # 16000 token 全烧在 reasoning 上 → content 空 → 整条 v2 回退。
    extractor = os.environ.get("AB_EXTRACT", "") or ej._v2_extractor_model()
    print(f"brief {BRIEF_NO} | 阵容 {[m for m, _ in plans]} | 单稿 "
          f"{[len(p) for _, p in plans]} 字 | ② 提取用 {extractor}\n")
    print("跑 v2 融合…", flush=True)
    fused = ej.fuse_architecture_v2(task, plans, judge_model=extractor)
    ej._call_model = real

    # 失败也要先打明细 —— 不然只能靠猜是哪一步空
    print("── 调用明细 ──")
    for step, model, prompt, out in log:
        print(f"  {step:<12} {model:<24} prompt {len(prompt):>6} 字 → 回复 {len(out or '')} 字")

    if not fused:
        print("\n❌ 融合返回空（会回退旧流程）")
        return
    out_path = Path("/tmp/v2_fused.json")
    out_path.write_text(fused)
    print(f"  融合稿 {len(fused)} 字 | tasks={'✓' if '\"tasks\"' in fused else '✗'} "
          f"risks={'✓' if '\"risks\"' in fused else '✗'} | 调用 {len(log)} 次")
    print(f"  存到 {out_path}（结尾 200 字：{fused[-200:]!r}）\n")

    # ── 对话过程（别只看分数）──
    for step, model, prompt, out in log:
        if step.startswith("③"):
            print(f"\n── {step}（{model}）──\n{(out or '(空)')[:700]}")

    # ── 打分 ──
    print("\n打分中…", flush=True)
    docs = [p for _, p in plans] + [fused]
    sc = _bl.score_all(brief, docs)
    if not sc:
        print("打分失败")
        return
    names = [m for m, _ in plans] + ["v2 融合"]
    print("\n同池打分:")
    for j, n in enumerate(names):
        print(f"  {n:<22} {sc[j]}")
    best = max(sc[j] for j in range(len(plans)))
    fi = len(plans)
    print(f"\n▶ v2 融合 {sc[fi]} vs 最好输入 {best} → {sc[fi] - best:+d}"
          f"   （判据：≥0 才算机制对了）")
    print(f"▶ 长度: 融合 {len(fused)} 字 / 单稿最长 {max(len(p) for _, p in plans)} 字 "
          f"= {len(fused) / max(len(p) for _, p in plans):.2f}×（目标 1.1~1.3×）")


if __name__ == "__main__":
    main()
