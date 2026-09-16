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


def test_退出码5是出声放行不是判失败(tmp_path, monkeypatch):
    """**这个修法最危险的一处**：pytest 退 5 = 「一条都没收集到」，不是「测试挂了」。

    判失败 ⇒ 声明的名字一变，**整轮交付就炸**（而老代码正是这么写的，只是从没被触发过，
    因为输入文件压根不存在）。静默 ⇒ 又变回"看不出集成测试没跑"。所以：**出声，放行**。
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
