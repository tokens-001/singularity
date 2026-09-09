"""A/B: 辩论后「融合」(stage1+stage2, 2 波) 值不值？

同一 brief 跑一次真实委员会，截获辩论后的 N 份方案，然后：
  A = fuse_architecture(方案们)   ← 现状（7 波）
  B = 方案[0]                     ← 跳过融合（5 波，省约 30% 时间）
两臂共用同一次委员会运行，差异只在最后一步 —— 隔离「融合」这一变量。

盲评：用委员会之外的模型打分，甲/乙 顺序互换各评一次取平均，抵消位置偏好。

用法: .venv/bin/python tests/integration/ab_fusion.py [brief数 默认1]
"""
import sys, os, json, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.scheduler.dispatcher as disp
from singularity.scheduler import _dispatch_exec as de
from singularity.scheduler import execution_judge as ej
from singularity.scheduler import config as cfg

# 委员会之外，避免自偏好。必须选非思考模型 —— glm-5.3 是思考模型，
# 实测把 2000 token 全烧在 reasoning 上，content 为空（finish_reason=length）。
JUDGE_MODEL = os.environ.get("AB_JUDGE", "glm-5.2")

BRIEFS = [
    "设计一个任务队列系统：生产者提交任务，多个 worker 并发消费，失败重试带退避，"
    "支持优先级和延时任务，任务状态可查询。需要模块划分、数据模型、API契约、任务拆解。",
    "设计一个文件同步服务：本地目录与云端双向同步，冲突检测与解决，增量传输，"
    "断点续传。需要模块划分、数据模型、API契约、任务拆解。",
    "设计一个多租户计费模块：按用量计费，支持套餐/超额/退款，账单生成与对账，"
    "租户隔离。需要模块划分、数据模型、API契约、任务拆解。",
]

RUBRIC = """你是架构评审员。下面是同一个需求的两份架构方案（甲/乙）。

需求:
{brief}

【方案甲】
{a}

【方案乙】
{b}

按 5 个维度各打 0-10 分:
1. modules: 模块划分职责单一、依赖合理
2. data: 数据模型完整（实体/字段/关系/索引）
3. api: API 契约明确（方法/路径/输入/输出/错误）
4. tasks: 任务拆解可并行、粒度合适
5. risk: 约束与风险覆盖到位、可验证

只输出 JSON（不要其他内容）:
{{"甲":{{"modules":0,"data":0,"api":0,"tasks":0,"risk":0,"total":0}},
  "乙":{{"modules":0,"data":0,"api":0,"tasks":0,"risk":0,"total":0}},
  "winner":"甲|乙|平","reason":"一句话"}}"""


def run_committee(brief, agents, chain, task_id):
    """跑真实委员会路径，截获辩论后的方案。

    返回 list[str]：_dispatch_committee 传给 fuse_architecture 的是
    `raw_outputs = [o for _, o in outputs]`，已经是纯文本列表。
    """
    captured, tmp = {}, Path(tempfile.mkdtemp())
    orig_fuse, orig_dir = ej.fuse_architecture, cfg.QIDIAN_DIR

    def _capture(task, outputs, **kw):
        captured["plans"] = list(outputs)
        return '{"architecture":"(intercepted)"}'

    ej.fuse_architecture, cfg.QIDIAN_DIR = _capture, tmp
    try:
        de._dispatch_committee(brief, "any", task_id, agents, chain)
    finally:
        ej.fuse_architecture, cfg.QIDIAN_DIR = orig_fuse, orig_dir
    return captured.get("plans", [])


def ask_judge(brief, a, b):
    """甲=a 乙=b 评一次。返回 dict 或 None。"""
    prompt = RUBRIC.format(brief=brief[:1500], a=a[:6000], b=b[:6000])
    raw = ej._call_model(prompt, JUDGE_MODEL, max_tokens=4000)
    if not raw:
        return None
    d = ej.try_parse_json(raw)
    if not isinstance(d, dict) or "甲" not in d or "乙" not in d:
        return None
    for k in ("甲", "乙"):
        if not isinstance(d.get(k), dict) or "total" not in d[k]:
            return None
    return d


def score_pair(brief, a, b):
    """顺序互换评两次取平均，抵消位置偏好。返回 (score_a, score_b) 或 None。"""
    r1 = ask_judge(brief, a, b)      # 甲=a 乙=b
    r2 = ask_judge(brief, b, a)      # 甲=b 乙=a
    if not r1 or not r2:
        return None
    a_scores = [r1["甲"]["total"], r2["乙"]["total"]]
    b_scores = [r1["乙"]["total"], r2["甲"]["total"]]
    return sum(a_scores) / 2, sum(b_scores) / 2


CACHE = Path(__file__).resolve().parent / ".ab_cache.json"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    # 测的是「融合」，辩论是无关变量（其价值已单独盲评过 +3~5 分）。
    # 跳掉辩论：委员会 5 波 → 1 波（只留初稿），快 3 倍以上。AB_NO_DEBATE=0 恢复。
    if os.environ.get("AB_NO_DEBATE", "1") == "1":
        de._debate = lambda task, members, chain, task_id, level, **kw: members
        print("（已跳过辩论：只跑初稿 → 融合，隔离「融合」这一变量）")
    agents = disp.load_agents()
    chain = disp.pick_agent_fallback_chain(agents, "any")
    print(f"委员会成员: {[a.get('model') for a in chain]} | 盲评模型: {JUDGE_MODEL}")
    print(f"briefs: {n}\n" + "=" * 70)

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    rows = []
    for i, brief in enumerate(BRIEFS[:n], 1):
        key = str(i)
        t0 = time.time()
        if key in cache:                       # 委员会很贵，跑过就复用，只重跑盲评
            plans, fused = cache[key]["plans"], cache[key]["fused"]
            print(f"[{i}/{n}] 用缓存（{len(plans)} 份方案）", flush=True)
        else:
            print(f"[{i}/{n}] 跑委员会… {brief[:36]}…", flush=True)
            plans = run_committee(brief, agents, chain, f"ab{i}")
            if len(plans) < 2:
                print(f"  ✗ 辩论后只有 {len(plans)} 份方案，跳过\n")
                continue
            print(f"  辩论完成 {time.time()-t0:.0f}s，{len(plans)} 份方案（各 {[len(p) for p in plans]} 字）", flush=True)
            t1 = time.time()
            fused = ej.fuse_architecture(brief, list(plans))
            print(f"  融合完成 {time.time()-t1:.0f}s（{len(fused)} 字）", flush=True)
            cache[key] = {"plans": plans, "fused": fused}
            CACHE.write_text(json.dumps(cache, ensure_ascii=False))

        # B = 直接取第一份
        single = plans[0]

        t2 = time.time()
        sc = score_pair(brief, fused, single)
        if not sc:
            print(f"  ✗ 盲评失败\n")
            continue
        a, b = sc
        print(f"  盲评 {time.time()-t2:.0f}s  →  A(融合) {a:.1f}  vs  B(单份) {b:.1f}   "
              f"{'A 胜' if a > b else ('B 胜' if b > a else '平')}\n", flush=True)
        rows.append({"brief": i, "A_fused": a, "B_single": b})

    if not rows:
        print("没有有效结果")
        return
    print("=" * 70)
    print(f"{'brief':>6} {'A融合':>8} {'B单份':>8} {'差':>7}")
    for r in rows:
        print(f"{r['brief']:>6} {r['A_fused']:>8.1f} {r['B_single']:>8.1f} {r['A_fused']-r['B_single']:>+7.1f}")
    avg_a = sum(r["A_fused"] for r in rows) / len(rows)
    avg_b = sum(r["B_single"] for r in rows) / len(rows)
    wins = sum(1 for r in rows if r["A_fused"] > r["B_single"])
    print(f"{'均值':>6} {avg_a:>8.1f} {avg_b:>8.1f} {avg_a-avg_b:>+7.1f}")
    print(f"\nA(融合) 胜 {wins}/{len(rows)}；样本 {len(rows)} 组")


if __name__ == "__main__":
    main()
