"""两份融合稿的配对比较 —— 回答"换个辩论模型，产物是赚是亏"。

为什么不用绝对打分：本仓既有的结论是绝对打分不可信、位置偏好是通病（见
`docs/融合实验-20260910.md` 与 pairwise_run.py 的说明）。所以这里复用 `pairwise_run._compare`：
**位置随机翻转**，同一个比较跑多轮，最后只报"谁赢几次"——不报分差。

裁判默认 `deepseek-v4-pro`：它**没参加**这两次融合的任何一个环节（提取是 glm-5.2、
辩论是 glm-5.3-flash/deepseek-chat/deepseek-v4-flash），避免选手当裁判。

用法:
    .venv/bin/python tests/integration/fused_pairwise.py A.json B.json [轮数 默认3]
    AB_JUDGE=glm-5.2 ...        # 换裁判
"""
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401  破循环导入

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_pwr = _load("pairwise_run", HERE / "pairwise_run.py")
_ab = _load("ab_fusion", HERE / "ab_fusion.py")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    fa, fb = Path(sys.argv[1]), Path(sys.argv[2])
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    judge = os.environ.get("AB_JUDGE", "deepseek-v4-pro")
    brief_no = int(os.environ.get("AB_PBRIEF", "3"))
    brief = _ab.BRIEFS[brief_no - 1]

    a, b = fa.read_text(), fb.read_text()
    print(f"甲 {fa.name} {len(a)} 字 | 乙 {fb.name} {len(b)} 字 | 裁判 {judge} | {rounds} 轮双向\n")

    win_a = win_b = fail = 0
    for i in range(rounds):
        for first_is_a in (True, False):
            na, nb = (("A", a), ("B", b)) if first_is_a else (("B", b), ("A", a))
            r = _pwr._compare(brief, na[0], na[1], nb[0], nb[1], judge)
            if r is None:
                fail += 1
                print(f"  {i+1} {'A先' if first_is_a else 'B先'}  → 判定失败")
                continue
            winner, reason, _ = r
            if winner == "A":
                win_a += 1
            else:
                win_b += 1
            print(f"  {i+1} {'A先' if first_is_a else 'B先'}  → {winner} 胜  {reason}")

    n = win_a + win_b
    print(f"\n结果: A {win_a} : {win_b} B   (判定失败 {fail})")
    if n:
        print(f"A 胜率 {win_a / n:.0%}  —— n={n}，样本很小，只看方向别看数字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
