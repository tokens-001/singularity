"""第五批修复（2026-09-14 收尾段）—— 每条都钉在"删掉那一行它会红"上。

出处：外派 ⑨/⑩（`复核驳回-01` / `按形状扫-01`，我逐条核过调用链）+ `OPEN.md` 接手指针。
每条都在**当前树**上重读过，不是照抄报告。
"""
import subprocess
import types

import pytest


# ═══════════════════════════════════════════════════════════════
# ① validator.run_project_tests —— unittest 报 0 个测试时不能算"通过"
# ═══════════════════════════════════════════════════════════════
# Python **<3.12** 的 `python -m unittest discover` 在没找到测试时打印的是
# `Ran 0 tests in 0.000s` + `OK`，**退出码 0**（3.12 起才改成 "NO TESTS RAN" + rc=5）。
# 原来的匹配串只认 "no tests ran"，对不上这句话 ⇒ 一路落到 `rc == 0` 那支
# ⇒ 把"一个测试都没有"报成**"通过、0 个用例"**。
# ⚠️ 而 `pyproject.toml` 声明的地板就是 3.11 ⇒ 这不是历史包袱，是活的口子。

def _fake_run(stdout: str, code: int):
    def _run(cmd, **kw):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=code)
    return _run


def test_老版本_unittest_的_0_测试不算通过(monkeypatch, tmp_path):
    """**变异判据**：去掉 `"ran 0 tests" in low` 这一句，本用例必须红
    （会走到 `rc == 0` 那支，runner 变成 "unittest"、passed 保持 True）。"""
    real_run = subprocess.run

    def _run(cmd, **kw):
        if cmd[1:3] == ["-m", "unittest"]:
            # 3.9/3.11 的原话，逐字抄的（rc=0 是关键）
            return types.SimpleNamespace(
                stdout="Ran 0 tests in 0.000s\n\nOK\n", stderr="", returncode=0)
        raise FileNotFoundError(cmd[0])   # pytest / npm 都不存在
    monkeypatch.setattr(subprocess, "run", _run)

    from singularity.scheduler.validator import run_project_tests
    r = run_project_tests(cwd=str(tmp_path))
    assert r["runner"] == "none", (
        f"runner={r['runner']!r} —— 报成了「跑过了」，而它其实一个测试都没找到")
    assert "没找到测试" in r["output"], r["output"]
    assert r["passed"] is True, "没找到测试 ≠ 测试挂了（不能矫枉过正）"


def test_有测试时不受这句话影响(monkeypatch, tmp_path):
    """反向保护：`Ran 1 tests` 这类正常输出不能被新判据误伤成"没找到测试"。"""
    def _run(cmd, **kw):
        if cmd[1:3] == ["-m", "unittest"]:
            return types.SimpleNamespace(
                stdout="Ran 3 tests in 0.001s\n\nOK\n", stderr="", returncode=0)
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(subprocess, "run", _run)

    from singularity.scheduler.validator import run_project_tests
    r = run_project_tests(cwd=str(tmp_path))
    assert r["runner"] == "unittest", f"正常跑通的 unittest 被误判：{r}"
    assert r["passed"] is True
