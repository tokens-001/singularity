#!/usr/bin/env python3
"""GATE 审查能不能看到改动 — 真机验证 (2026-09-11 审计 P0-1)。

为什么存在:
  这条路径**单元测试测不出来**。bug 只在特定时序下出现: worktree 里改动在
  `validate()` 之前就被 `_exec._process_planner_or_merge` 的 `commit_wt` 提交了,
  于是裸 `git diff` 恒为空 → `_is_trivial_change` 判 trivial → 五道门禁全静默放行。
  桩测试里不会自己去 commit, 所以永远是绿的 —— 之前两轮真机验证也都因此作废。

本测试**不 mock 任何东西**: 真 git 仓库、真 snapshot 模块、真 validator/_review
代码路径, 并手动复刻"先提交, 再审查"的时序。对照断言:
  · 裸 `git diff`          → 0 行   → 空 diff 看不见任何改动
  · `git diff <快照ref>`   → 128 行 → trivial=False (基准生效)
  · 无基准 (`base_ref=""`) → trivial=False (fail-closed, 不再假装"改得小")
  · `_hard_diff_rules`     → 新基准拦下被删的 require_auth, 旧基准 0 issue

**2026-09-11 外派评审后补的一层**: 除了"基准接没接上", 还钉住"基准丢了会怎样"。
曾经 `_SnapProxy` 漏了 `method` 属性 → `_diff_base` 恒返回空串 → 这条路径上
**永远**走 `base_ref=""` 分支, 而它当时判 trivial=True → 五道检查全跳过, 且披露
文案谎称"改动被判为小改动"。现在无基准一律 fail-closed。

跑法:  .venv/bin/python tests/test_review_gate.py
不依赖 pytest。退出码 0=全过, 1=有不符合预期项。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from singularity.scheduler import config

# 隔离：别往生产 .qidian/snapshots 里写
_tmp_qidian = Path(tempfile.mkdtemp(prefix="verify_qidian_"))
config.SNAPSHOT_DIR = _tmp_qidian / "snapshots"
config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import validator as val_mod
from singularity.scheduler._review import _is_trivial_change


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def main() -> int:
    repo = Path(tempfile.mkdtemp(prefix="verify_repo_"))
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.name", "verify", cwd=repo)
    git("config", "user.email", "verify@local", cwd=repo)

    # 基线：一个 60 行的模块，改动后要 >50 行才不叫 "trivial"
    # 基线上放一行 require_auth() —— 待会儿删掉它，触发 no-weaken-security 硬规则
    target = repo / "service.py"
    target.write_text("def check(user):\n    require_auth(user)\n    return True\n" + "# filler\n" * 60)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "baseline", cwd=repo)

    # ① 执行前快照（真实调用，非 mock）
    snap = snap_mod.take("verify-task", repo_root=repo)
    base_ref = val_mod._diff_base(snap)
    print(f"快照: method={snap.method!r} ref={snap.ref!r}")
    print(f"_diff_base(snap) -> {base_ref!r}")
    assert snap.method == "git" and base_ref, "快照不是 git 型 → 基准取不到，验证无意义"

    # ② 制造一处**该被拦下**的改动：删掉 require_auth() 那一行（安全弱化）+ 大量行变更
    target.write_text("def check(user):\n    return True\n" + "# changed\n" * 60)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "agent change (commit_wt 模拟)", cwd=repo)   # ← 关键时序

    print("\n--- 裸 git diff（旧基准）---")
    r = git("diff", "service.py", cwd=repo)
    print(f"输出行数: {len(r.stdout.splitlines())}")

    print("\n--- git diff <快照ref>（新基准）---")
    r2 = git("diff", base_ref, "service.py", cwd=repo)
    print(f"输出行数: {len(r2.stdout.splitlines())}")

    # ③ 走**真实**代码路径判定
    no_base_trivial = _is_trivial_change(["service.py"], str(repo), base_ref="")
    new_trivial = _is_trivial_change(["service.py"], str(repo), base_ref=base_ref)
    print(f"\n_is_trivial_change(base_ref='')   -> {no_base_trivial}")
    print(f"_is_trivial_change(base_ref=快照) -> {new_trivial}")

    # ④ 真硬规则检查：diff 类检查必须真的执行
    #    注意 no-weaken-security 是 warning 级 —— passed 只看 critical，
    #    所以断言要落在 issues 上，不能落在 passed 上。
    hard = val_mod._hard_diff_rules(["service.py"], cwd=str(repo), base=base_ref)
    hard_old = val_mod._hard_diff_rules(["service.py"], cwd=str(repo), base="")
    rules = [i["rule"] for i in hard.get("issues", [])]
    print(f"_hard_diff_rules(base=快照) -> issues={rules}")
    print(f"_hard_diff_rules(base='')   -> issues={[i['rule'] for i in hard_old.get('issues', [])]}")

    print("\n--- 判定 ---")
    ok = True
    if no_base_trivial is not False:
        print("✗ 预期无基准时 fail-closed 判 trivial=False，实际 %s" % no_base_trivial); ok = False
    else:
        print("✓ 无基准: trivial=False —— 拿不到基准就不敢说'改得小'，审查照跑（fail-closed）")
    if new_trivial is not False:
        print("✗ 预期新基准判 trivial=False，实际 %s" % new_trivial); ok = False
    else:
        print("✓ 新基准: trivial=False —— 改动静下被看到，审查真的跑")
    if "no-weaken-security" in rules:
        print("✓ 硬规则真拦下了被删的 require_auth —— diff 类检查确实拿到了改动")
    else:
        print("✗ diff 类硬规则没跑起来（应撞到 no-weaken-security），实际 %s" % rules); ok = False
    if hard_old.get("issues"):
        print("✗ 旧基准下硬规则也报了 issue？不该 —— 裸 diff 恒空"); ok = False
    else:
        print("✓ 旧基准: 硬规则 0 issue —— 空 diff 下检查全静默，正是 bug 的样子")

    print("\n结果:", "全部符合预期" if ok else "有不符合预期的项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
