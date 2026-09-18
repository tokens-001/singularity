"""集成检查必须跑 `test_cases.json` 里**声明的那几条**，不是写死的 `-k test_integration`。

## 症状（2026-09-16 真机撞见，两个 bug 叠在一起）

① `test_cases.json` **全仓没人写** ⇒ `if tc_path.exists()` 恒假 ⇒
   **集成测试从来没跑过**，而且静默（跟"本来就没有集成用例"长得一样）。
   → 写入端修在 `_workflow_phases._materialize_test_cases`。

② 输入有了之后才暴露：那个检查跑的是 `pytest -k test_integration`，而清单里声明的名字
   是 `test_normal_path_output_n5` / `test_default_n_is_15` 这类 —— **一个都不含
   "test_integration"**。`-k` 一条也选不中，pytest 退 **5**（no tests collected），
   而老代码 `if r.returncode != 0: return False` ⇒ **整轮交付判失败**。
   → 按声明的名字拼 `-k`（真机实测：声明的 5 条 5/5 全中、退出码 0）。

## 三条判据

1. 发的命令里 `-k` 是**声明的那些名字**，不是 `test_integration`
2. 退出码 **5** ⇒ **不出声不算完，但不能判失败**（判失败会因"名字一变"误伤一整轮交付；
   静默又正是这个洞的成因 ⇒ **出声 + 放行**）
3. 真正的测试失败（非 0 非 5）⇒ **照旧判失败**（别把修法改宽）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import config                    # noqa: E402
from singularity.scheduler import orchestrator as orch       # noqa: E402
from singularity.scheduler import project as proj_mod        # noqa: E402


def _mk(tmp_path, monkeypatch, cases):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    repo = tmp_path / "projects" / "演示"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    import json
    (repo / "test_cases.json").write_text(
        json.dumps({"integration": cases}, ensure_ascii=False), encoding="utf-8")
    p = proj_mod.ProjectState(
        id="P1", name="演示", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    proj_mod.save(p)
    return p, repo


def _stub_subprocess(monkeypatch, pytest_rc: int):
    """拦 `subprocess.run`：git 回"干净"，pytest 回指定的退出码，并**记下发出去的 argv**。"""
    import subprocess as _sp
    seen = {"pytest_argv": None}
    real_run = _sp.run

    def fake_run(argv, *a, **k):
        if isinstance(argv, list) and argv and argv[0] == "git":
            return _sp.CompletedProcess(argv, 0, stdout="", stderr="")
        if isinstance(argv, list) and "pytest" in " ".join(argv):
            seen["pytest_argv"] = list(argv)
            return _sp.CompletedProcess(argv, pytest_rc, stdout="fake", stderr="")
        return real_run(argv, *a, **k)          # 其它照常（本用例不会走到 docker）

    monkeypatch.setattr(_sp, "run", fake_run)
    return seen


def test_按声明的用例名拼_k(tmp_path, monkeypatch):
    """**正题**：`-k` 里必须是清单声明的名字 —— 写死 `test_integration` 时一条都选不中。"""
    p, _ = _mk(tmp_path, monkeypatch, [
        {"name": "test_normal_path_output_n5"},
        {"name": "test_default_n_is_15"},
    ])
    seen = _stub_subprocess(monkeypatch, pytest_rc=0)
    ok, detail = orch._run_integration_merge(p)

    assert ok, detail
    argv = seen["pytest_argv"]
    assert argv, "压根没跑 pytest —— 集成用例被跳过了"
    k = argv[argv.index("-k") + 1]
    assert "test_normal_path_output_n5" in k and "test_default_n_is_15" in k, k
    assert k != "test_integration", "还是那个选不中任何东西的写死过滤器"


def test_退出码5要出声_而且退到跑全部_没测试才放行(tmp_path, monkeypatch):
    """pytest 退 5 = 「一条都没收集到」，不是「测试挂了」。

    判失败 ⇒ 声明的名字一变，**整轮交付就炸**（老代码正是这么写的）；
    静默 ⇒ 又变回"看不出集成测试没跑"。

    🔴 **2026-09-17 真机追加**：原来退 5 就**直接放行** —— 而放行 = **这条检查等于没跑**，
    而"跑过了"和"没跑"在交付报告上**长得一模一样**。
    真机那轮就是：声明的名字全是中文描述 ⇒ `-k` 一条都选不中 ⇒ 退 5 ⇒ 放行 ⇒
    **8 个集成用例压根没跑，报告上写着"集成通过"**。
    ⇒ 现在退 5 之后**退一步跑项目里的全部测试**；只有**项目里压根没有测试**（也是退 5）
    才放行，并把那个处境如实说出来。
    """
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "test_根本不存在的用例"}])
    _stub_subprocess(monkeypatch, pytest_rc=5)

    ok, detail = orch._run_integration_merge(p)
    assert ok is True, f"退 5 被判成失败了 —— 名字一变就误伤一整轮交付：{detail}"
    assert any("integration_cases_not_implemented" in str(a) for a in warns), \
        f"放行了却一声不吭 —— 那又变成「看不出集成测试没跑」：{warns}"


def test_真失败照旧判失败(tmp_path, monkeypatch):
    """**对照**：退出码 1（真挂了）必须判失败 —— 别把修法改宽成"凡非 0 都放行"。"""
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "test_x"}])
    _stub_subprocess(monkeypatch, pytest_rc=1)
    ok, detail = orch._run_integration_merge(p)
    assert ok is False and "集成测试失败" in detail, detail


def test_没有集成用例时不下结论(tmp_path, monkeypatch):
    """清单里 `integration` 是空的 ⇒ 不跑、不判失败（这是原有语义，别动）。"""
    p, _ = _mk(tmp_path, monkeypatch, [])
    seen = _stub_subprocess(monkeypatch, pytest_rc=1)     # 真跑了就会红
    ok, _ = orch._run_integration_merge(p)
    assert ok is True and seen["pytest_argv"] is None, "没有集成用例却跑了 pytest"


def _stub_subprocess_seq(monkeypatch, pytest_rcs: list):
    """pytest 按**调用次序**回不同退出码（第一条退 5、退一步那条退 0 这种）。"""
    import subprocess as _sp
    seen = {"argv": []}
    real_run = _sp.run
    rcs = list(pytest_rcs)

    def fake_run(argv, *a, **k):
        if isinstance(argv, list) and argv and argv[0] == "git":
            return _sp.CompletedProcess(argv, 0, stdout="", stderr="")
        if isinstance(argv, list) and "pytest" in " ".join(argv):
            seen["argv"].append(list(argv))
            rc = rcs.pop(0) if rcs else 0
            return _sp.CompletedProcess(argv, rc, stdout="fake", stderr="")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(_sp, "run", fake_run)
    return seen


def test_名字选不中要退到跑全部_不能就此放行(tmp_path, monkeypatch):
    """🔴 **2026-09-17 真机改的那一处**：退 5 之后**放行** = 这条检查等于没跑。

    真机那轮声明的名字全是**中文描述**（`parse_ts 时区归一化`）⇒ `-k` 一条选不中
    ⇒ 退 5 ⇒ 放行 ⇒ **8 个集成用例压根没跑，而报告上写着"集成通过"**。

    ⇒ 退一步：**跑项目里的全部测试**。这一步真挂了就要判失败。
    """
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: warns.append(a))
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "parse_ts 时区归一化"}])
    seen = _stub_subprocess_seq(monkeypatch, [5, 1])      # 第一条退 5；退一步那条退 1

    ok, detail = orch._run_integration_merge(p)

    assert len(seen["argv"]) == 2, (
        "没有退到「跑全部」那一步 ⇒ 这条检查等于没跑（而报告上照样写集成通过）: "
        f"{seen['argv']}")
    assert "-k" not in seen["argv"][1], "退一步那条不该再带 -k 过滤器"
    assert ok is False, "退一步跑出真失败却放行了 —— 那就是「检查没跑却显示通过」"


def test_退一步全绿就通过(tmp_path, monkeypatch):
    """**对照**：退一步跑全绿 ⇒ 集成通过（不是"凡退 5 都失败"）。"""
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "parse_ts 时区归一化"}])
    seen = _stub_subprocess_seq(monkeypatch, [5, 0])
    ok, detail = orch._run_integration_merge(p)
    assert ok is True and len(seen["argv"]) == 2, (ok, detail)


# ═══════════════════════════════════════════════════════════
# 「集成通过」有两条来路 —— 真跑过测试 / 项目里压根没有测试可跑。
# 后端只回 `ok` 的话这两者在门上长得一模一样（2026-09-19，外派评审 ③）。
# ═══════════════════════════════════════════════════════════

def _merge_note(p):
    """取最后一次集成留痕。"""
    rows = [e for e in (p.lineage or []) if e.get("action") == "integration_merge"]
    assert rows, f"一条集成留痕都没记 —— 门上读不到「这轮跑没跑到测试」：{p.lineage}"
    return rows[-1]


def test_真跑过测试_留痕说跑到了(tmp_path, monkeypatch):
    """**对照**：跑过就是跑过，detail 不许带上"没跑到"那句话。"""
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "test_x"}])
    _stub_subprocess(monkeypatch, pytest_rc=0)
    ok, detail = orch._run_integration_merge(p)

    assert ok is True, detail
    assert "没跑到" not in detail, f"跑过了却说没跑 —— 反着撒谎：{detail}"
    assert _merge_note(p)["tests_ran"] is True, p.lineage


def test_项目里没有测试_留痕说没跑到(tmp_path, monkeypatch):
    """两条 pytest 都退 5（清单里的名字选不中 + 项目里一条测试都没有）⇒ 放行但**留痕**。

    这条正是 2026-09-17 真机的处境：8 个集成用例压根没跑，报告上写着"集成通过"。
    """
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "parse_ts 时区归一化"}])
    _stub_subprocess_seq(monkeypatch, [5, 5])

    ok, detail = orch._run_integration_merge(p)

    assert ok is True, f"退 5 不该判失败（名字一变就误伤一整轮交付）：{detail}"
    assert "没跑到" in detail, f"没跑测试却说得跟跑过一样：{detail}"
    assert _merge_note(p)["tests_ran"] is False, p.lineage


def test_清单里没声明集成用例_也留痕说没跑到(tmp_path, monkeypatch):
    """`integration: []` ⇒ 压根没进过测试分支。**以前这种"没测"完全看不出来。**"""
    p, _ = _mk(tmp_path, monkeypatch, [])
    seen = _stub_subprocess(monkeypatch, pytest_rc=1)     # 真跑了就会红
    ok, detail = orch._run_integration_merge(p)

    assert ok is True and seen["pytest_argv"] is None, "没有集成用例却跑了 pytest"
    assert "没跑到" in detail, detail
    assert _merge_note(p)["tests_ran"] is False, p.lineage


def test_调用方把detail摆到门上_不再写死(tmp_path, monkeypatch):
    """**接线**：`_run_integration_merge_async` 的 ok 分支原来写死「集成合并通过」，
    把刚拿到的那个区别当场丢掉 ⇒ 门上看不见。

    ⚠️ 判据钉在**门上的理由**（`set_phase` 写进 lineage 的那条）上，不是"函数返回了什么"
    —— 只测 `_run_integration_merge` 的话，调用方那行写死照样绿（假接线）。
    """
    p, _ = _mk(tmp_path, monkeypatch, [{"name": "test_x"}])
    monkeypatch.setattr(orch, "_merge_inflight", set())
    monkeypatch.setattr(proj_mod, "load", lambda _pid: p)
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    monkeypatch.setattr(orch, "_run_integration_merge", lambda _p: (
        True, "集成合并通过（这一轮没跑到集成测试）"))
    from singularity.scheduler import workflow as wf
    monkeypatch.setattr(wf, "run_test_fix_loop", lambda proj, agents: "ok")

    orch._run_integration_merge_async(p.id, {})

    assert p.phase == proj_mod.Phase.REVIEWING, p.phase
    reason = p.lineage[-1].get("reason", "")
    assert "没跑到" in reason, (
        f"调用方又把 detail 丢了 —— 门上读到的还是那句写死的「集成合并通过」：{p.lineage[-1]}")
