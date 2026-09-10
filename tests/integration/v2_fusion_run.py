"""跑一次真 v2 融合，验证两件事：输出有没有变短（不膨胀）+ 融合 ≥ max(输入)。

用缓存里的成员稿（不重跑委员会），只跑 ②③④ 这几步。
_call_model 被包了一层记录 prompt/回复，跑完把对话过程打出来 —— 别只看分数。

用法: .venv/bin/python tests/integration/v2_fusion_run.py [brief号 默认3]
      AB_CACHE=.ab_debate_plans_v2.json  AB_JUDGE=kimi-k3  QIDIAN_FUSION_V2_ROUNDS=5
"""
import os, sys, json, time, importlib.util
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

# AB_WRITER：强制指定定稿人。默认 writer = _first_speaker(disagreements, members)，
# 即「最先提出分歧的那个模型」—— 于是「谁先开口，谁的设计就被保留」。
# 用来验证「定稿人自我偏好」假设：换定稿人，看被保留的是不是也跟着换。
if os.environ.get("AB_WRITER"):
    # 两个都要打：_pick_writer 会先查 model_discipline 纪律表，命中就不走 _first_speaker，
    # 只补后者的话这个开关会被静默忽略（"设了但没生效"）。
    # 名字别用 `_w` —— 本文件下面 `_w` 是 witness 模块的别名，闭包按引用捕获，
    # 撞名会让闭包在调用时取到模块对象（实测崩在 json 序列化：winner 是个 module）。
    _forced_writer = os.environ["AB_WRITER"]
    ej._first_speaker = lambda d, m: _forced_writer
    ej._pick_writer = lambda d, m: _forced_writer
    print(f"⚠️ 强制定稿人 = {_forced_writer}（默认取最先提分歧者）", flush=True)

_bl.MAX_CHARS = 0
_bl.JUDGE_MODEL = os.environ.get("AB_JUDGE", "glm-5.2")   # kimi 已停用（余额不足）
_bl.JUDGE_MAX_TOKENS = int(os.environ.get("AB_JUDGE_MAX_TOKENS", "16000"))

# 告警实时打出来 —— 以前只打"回复 N 字"，分不清是限流还是额度烧光。
# 注意 hook 的是 warn()：告警 2026-09-10 起从 heartbeat 独立到 alerts.jsonl 了。
from singularity.scheduler import witness as _w
_real_warn = _w.warn


def _warn(scope, msg):
    print(f"   ⚠ {scope}: {msg}", flush=True)
    return _real_warn(scope, msg)


_w.warn = _warn

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

    # AB_REMAP=旧成员:新模型 → 只换名字不换稿子，用来测「辩论步换模型省多少时间、产物差多少」。
    # 稿子不变是刻意的：这样变量只剩「谁在辩」，不掺「谁写的初稿」。
    if os.environ.get("AB_REMAP"):
        _old, _new = os.environ["AB_REMAP"].split(":")
        plans = [(_new if m == _old else m, t) for m, t in plans]
        print(f"⚠️ 成员 {_old} → {_new}（稿子仍是原模型的）", flush=True)

    log = []
    real = ej._call_model

    min_tok = int(os.environ.get("V2_MIN_TOKENS", "0"))

    def spy(prompt, model, max_tokens=2000):
        mt = max(max_tokens, min_tok) if min_tok else max_tokens
        t0 = time.perf_counter()
        out = real(prompt, model, mt)
        log.append((_step_of(prompt), model, prompt, out, time.perf_counter() - t0))
        return out

    ej._call_model = spy
    # AB_EXTRACT 可换掉提取模型：fusion.toml 默认的 glm-5.3 是思考模型，
    # 16000 token 全烧在 reasoning 上 → content 空 → 整条 v2 回退。
    extractor = os.environ.get("AB_EXTRACT", "") or ej._v2_extractor_model()
    print(f"brief {BRIEF_NO} | 阵容 {[m for m, _ in plans]} | 单稿 "
          f"{[len(p) for _, p in plans]} 字 | ② 提取用 {extractor}\n")
    print("跑 v2 融合…", flush=True)
    fused = ej.fuse_architecture_v2(task, plans, extract_model=extractor)
    ej._call_model = real

    # 失败也要先打明细 —— 不然只能靠猜是哪一步空
    print("── 调用明细（按发生顺序）──")
    total = 0.0
    for step, model, prompt, out, dt in log:
        total += dt
        print(f"  {step:<12} {model:<24} prompt {len(prompt):>6} 字 → "
              f"回复 {len(out or ''):>6} 字   {dt:6.1f}s")
    print(f"  {'合计':<12} {len(log)} 次调用，串行累计 {total:.0f}s")
    print(f"  {'最慢一步':<12} {max(log, key=lambda r: r[4])[0] if log else '-'}")

    if not fused:
        print("\n❌ 融合返回空（会回退旧流程）")
        return
    out_path = Path(f"/tmp/v2_fused_{BRIEF_NO}.json")
    out_path.write_text(fused)
    print(f"  融合稿 {len(fused)} 字 | tasks={'✓' if '\"tasks\"' in fused else '✗'} "
          f"risks={'✓' if '\"risks\"' in fused else '✗'} | 调用 {len(log)} 次")
    print(f"  存到 {out_path}（结尾 200 字：{fused[-200:]!r}）\n")

    # ── 对话过程（别只看分数）──
    for step, model, prompt, out, _dt in log:
        if step.startswith("③"):
            print(f"\n── {step}（{model}）──\n{(out or '(空)')[:700]}")

    if os.environ.get("AB_SKIP_SCORE") == "1":
        print("\n(AB_SKIP_SCORE=1，跳过打分 —— 判据改用 pairwise_run.py 的配对比较)")
        return

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
