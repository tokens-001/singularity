#!/usr/bin/env python3
"""pending_refs_probe.py — 把 pending ref 上的产物捞出来跑一遍，量一量它到底行不行。

## 为什么存在

任务判 `failed` ⇒ 产物**不合并** ⇒ 留在 `refs/qidian/pending/<task_id>`。
那批产物**没丢**，但**没有任何东西会告诉你它行不行** ——
`delivery_facts.py --refs` 数的是「任务还在不在」（它的判据是孤儿），不是「产物进没进仓」。

于是形成闭环：**能抓到实现 bug 的那把尺子（测试），每轮被扔掉；下一轮重新踩同一个坑。**
2026-09-20 实测（`验证-日志统计-20260920c`）：
    `refs/qidian/pending/*` 上有 1,680 行测试 → 捞回来一跑
    → **主线代码 `Accumulator.result()` 跑一次就崩**（`StatResult` 缺 3 个字段）。
而 QA 的判词是「代码质量高」—— 它只做静态审查，没跑过。

## 用法

    .venv/bin/python scripts/pending_refs_probe.py <project_id>
    .venv/bin/python scripts/pending_refs_probe.py --last

🔴 **只读**：全程在 `tempfile` 的临时克隆里做，**不碰项目仓、不碰 `.qidian/`**。
（项目仓是「奇点产出长什么样」的证据，动它就把证据毁了。）
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from singularity.scheduler import config  # noqa: E402
from singularity.scheduler import project as proj_mod


def _run(*args, cwd):
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def _project_files():
    return list((config.QIDIAN_DIR / "projects").glob("*.json"))


def probe(pid: str) -> int:
    repo = proj_mod.repo_dir(pid)
    if not repo or not Path(repo).is_dir():
        print(f"找不到项目仓（repo_dir={repo!r}）")
        return 1
    print(f"项目仓：{repo}")

    with tempfile.TemporaryDirectory(prefix="pending-probe-") as tmp:
        work = Path(tmp) / "probe"
        rc, out = _run("git", "clone", "-q", str(repo), str(work), cwd=tmp)
        if rc != 0:
            print(f"clone 失败：{out.strip()[:200]}")
            return 1

        _run("git", "fetch", "-q", "origin", "+refs/qidian/*:refs/qidian/*", cwd=work)
        rc, out = _run("git", "for-each-ref", "refs/qidian/pending/",
                       "--format=%(refname)", cwd=work)
        refs = [r for r in out.split() if r.strip()]
        if not refs:
            print("这个项目没有 pending ref —— 无产物可捞。")
            return 0
        print(f"pending ref {len(refs)} 条，逐条合并（在临时克隆里）")

        merged, conflicted = [], []
        for ref in refs:
            rc, out = _run("git", "merge", "--no-edit", "-q", ref,
                           "-m", f"probe {ref.rsplit('/', 1)[-1]}", cwd=work)
            (merged if rc == 0 else conflicted).append(ref.rsplit("/", 1)[-1])

        print(f"  合并成功 {len(merged)} 条" + (f" · 冲突 {len(conflicted)} 条：{conflicted}"
                                              if conflicted else ""))

        rc, out = _run("git", "ls-files", "tests", cwd=work)
        tests = [t for t in out.split("\n") if t.endswith(".py")]
        if not tests:
            print("  ⚠️ 合并后仓里仍没有 tests/*.py —— 这批产物里没测试。")
            return 0
        print(f"  捞出测试文件 {len(tests)} 个：")
        for t in tests:
            n = len((work / t).read_text(encoding="utf-8", errors="replace").split("\n"))
            print(f"     {t}  ({n} 行)")

        print("\n跑测试：")
        rc, out = _run(sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", cwd=work)
        tail = [ln for ln in out.strip().split("\n") if ln.strip()][-3:]
        for ln in tail:
            print(f"  {ln}")
        print("\n⚠️ 只摆事实：红的可能是实现 bug，也可能是这批测试自己没写完 —— 别默认是哪一个。")
        print("   要改就在这个临时克隆里改，别改项目仓。")
        return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--last":
        cands = sorted(_project_files(), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("没有项目")
            return 1
        return probe(cands[-1].stem)
    return probe(args[0])


if __name__ == "__main__":
    sys.exit(main())
