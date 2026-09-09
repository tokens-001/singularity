"""glm-5.2 单干 vs「三家 S 融合」—— 委员会到底有没有必要？

背景：融合稿盲评输给「最好单稿」−3.3 / −16.5，但那两组阵容都有一家碾压
（第一份 28 分 vs 最好 44 分），不满足「同档」。既然如此，直接问更根本的问题：
**一个 SSS 模型单干，能不能赢过三个 S 模型融合？** 赢 → 委员会前提不成立。

评委不能是 glm-5.2（自偏好），默认换 deepseek-v4-pro（SS+）。注意它有同厂偏向
（deepseek-v4-flash 是参赛者之一，而它是三家最弱那份）—— 偏向方向对结论不利，安全。

用法: .venv/bin/python tests/integration/ab_solo.py [brief数 默认3]
      AB_SOLO=glm-5.2  AB_JUDGE=deepseek-v4-pro  AB_MAX_CHARS=0
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

CACHE = HERE / os.environ.get("AB_CACHE", ".ab_cache_3v_nodebate.json")
SOLO_CACHE = HERE / ".ab_solo_cache.json"
SOLO = os.environ.get("AB_SOLO", "glm-5.2")
_bl.JUDGE_MODEL = os.environ.get("AB_JUDGE", "deepseek-v4-pro")
_bl.MAX_CHARS = int(os.environ.get("AB_MAX_CHARS", "0"))


def get_solo(i, brief):
    c = json.loads(SOLO_CACHE.read_text()) if SOLO_CACHE.exists() else {}
    if c.get(str(i)):
        return c[str(i)]
    out = de._run_no_tools({"model": SOLO}, brief, f"solo{i}", "any")
    if out:
        c[str(i)] = out
        SOLO_CACHE.write_text(json.dumps(c, ensure_ascii=False))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cache = json.loads(CACHE.read_text())
    print(f"单干: {SOLO} | 评委: {_bl.JUDGE_MODEL} | 缓存: {CACHE.name} | 截断: {_bl.MAX_CHARS or '关'}\n")

    rows = []
    for i, brief in enumerate(BRIEFS[:n], 1):
        v = cache.get(str(i))
        if not v:
            continue
        solo = get_solo(i, brief)
        if not solo:
            print(f"[{i}] ✗ {SOLO} 没产出")
            continue
        docs = list(v["plans"]) + [v["fused"], solo]      # 3 单稿 + 融合 + 单干
        sc = _bl.score_all(brief, docs)
        if not sc:
            print(f"[{i}] ✗ 打分失败")
            continue
        singles = [sc[j] for j in range(len(v["plans"]))]
        fused, solo_s = sc[len(v["plans"])], sc[len(v["plans"]) + 1]
        best = max(singles)
        rows.append((i, solo_s, fused, best, singles))
        print(f"[{i}] 单干 {solo_s:>3}  融合 {fused:>3}  最好单稿 {best:>3}  "
              f"各家单稿 {singles}  → 单干-融合 {solo_s-fused:+3}  单干-最好 {solo_s-best:+3}")

    if not rows:
        print("没有有效结果")
        return
    print("\n" + "=" * 62)
    d_f = sum(r[1] - r[2] for r in rows) / len(rows)
    d_b = sum(r[1] - r[3] for r in rows) / len(rows)
    print(f"均值: 单干 − 融合 = {d_f:+.1f}   单干 − 最好单稿 = {d_b:+.1f}")
    print(f"单干胜融合 {sum(1 for r in rows if r[1] > r[2])}/{len(rows)}；"
          f"单干胜最好单稿 {sum(1 for r in rows if r[1] > r[3])}/{len(rows)}")


if __name__ == "__main__":
    main()
