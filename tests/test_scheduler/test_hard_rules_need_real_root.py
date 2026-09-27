"""**没拿到任务的工作目录时，硬规则不许拿别的根顶替**（2026-09-27 拆 `疑2` 那颗地雷）。

## 症状（真发生过一次）
trace `1788793813324`（2026-09-07 23:10）：任务"在项目根目录写 fibonacci.py，
**再写 test_fibonacci.py**" —— 是**新建**一个测试文件。结果：

    unverified: ["HardRule[no-delete-tests]: 测试文件缺失或已删除: test_fibonacci.py"]
    validation: {"verdict": "阻断", "action": "abort", "validate_verdict": "通过", "validate_reason": "ok"}
    final_status: "blocked"

**同一份报告里，真正的 validate 判词是"通过"** —— 判死的只有那条硬规则，而且判反了。

## 根因
`_hard_diff_rules` 里 `root = _Path(cwd) if cwd else config.PROJECT_ROOT`，
而 `validate()` 那行是 `cwd=cwd or str(config.PROJECT_ROOT)`。cwd 拿不到时它**回退到
"奇点自己那个仓"**，可那批改动在**项目仓 / worktree** 里 ⇒ `root / f` 当然不存在
⇒ 一个**新建**的 test 文件被读成"**已删除**"。而 `passed=False` 在 `validate()` 里是：

    if not hard.get("passed"): report.verdict = "阻断"; report.action = "abort"; return report

⇒ 直接判死，后面整条流程都不走。

⇒ 老形状又一次：**`cwd` 这一个值同时表示「任务的工作目录」和「随便哪个仓」**。

## 修法
拿不到 cwd ⇒ **三条检查一条都不做 + 如实披露**，同这个函数里 `base` 为空那一套
（docstring 原话："不能静默当作通过"）。⚠️ **不是 fail-open 放行** —— 那三条的前提
（"root 里看得见这批文件"）根本不成立，跑它们得到的是**反的结论**，不是保守。
"""

import pytest

from singularity.scheduler import validator as val_mod


def _validate(monkeypatch, changed):
    """摆平门和 validate 那两跳，只留硬规则这一段。抄 `test_route_gate_unknown` 的桩法。"""
    monkeypatch.setattr(val_mod, "_run_gate", lambda: {"passed": True, "message": "ok"})
    monkeypatch.setattr(val_mod, "_run_validate", lambda c: {"verdict": "通过", "verdict_reason": ""})
    monkeypatch.setattr(val_mod, "post_execution_hook", lambda *a, **k: {}, raising=False)
    return val_mod.validate(candidate="改完了", gate_required=False, task_type="bugfix",
                            changed_files=changed, snap=None, turn=1, max_turns=2, cwd=None)


def test_没给工作目录时一条检查都不做():
    """判据：不产出 issue（尤其不许有 critical），并且**说明自己没跑**。

    变异：把 `if not cwd:` 那段删掉 ⇒ 回退到 `config.PROJECT_ROOT`、产出 critical ⇒ 本条红。
    """
    r = val_mod._hard_diff_rules(["tests/test_x.py"], cwd=None)

    assert r["issues"] == [], f"没给根却还是跑出了结论：{r['issues']}"
    assert r["passed"] is True
    assert "没拿到任务工作目录" in r.get("skipped", ""), (
        f"不跑可以，但**必须披露** —— 静默当作通过是本模块的底线（见文件头）：{r}")


def test_反方向_给了根且文件真没了照旧报critical(tmp_path):
    """对照：**别把这条检查修没了**。根是对的、文件真不在 ⇒ 照旧 critical。

    这条同时钉住"跳过"只针对"没根"，不是针对"有根但文件不存在"。
    """
    r = val_mod._hard_diff_rules(["tests/test_x.py"], cwd=str(tmp_path))

    assert any(i["rule"] == "no-delete-tests" and i["severity"] == "critical"
               for i in r["issues"]), f"真删了测试却不报了：{r['issues']}"
    assert r["passed"] is False
    assert not r.get("skipped"), "有根就不该走跳过那条路"


def test_validate不传cwd时不判死但要说出去(monkeypatch):
    """端到端那一跳：以前这里会用 `config.PROJECT_ROOT` 跑出 critical ⇒ abort。

    判据两条**都要**：① 不再 abort（不误杀）② `unverified` 里说得出"没跑"（不静默）。
    """
    rep = _validate(monkeypatch, changed=["tests/test_x.py"])

    assert rep.action != "abort", (
        f"又拿别的根判死了（09-07 那次就是这么杀掉一个合法任务的）："
        f"verdict={rep.verdict} action={rep.action} unverified={rep.unverified}")
    assert any("硬规则未执行" in u for u in rep.unverified), (
        f"不判死可以，但必须披露 —— 否则交付报告把『没跑』和『跑过了』混为一谈：{rep.unverified}")
