"""记账路径必须**读时现算**，不许在构造时冻住。

`_budget = TokenBudget()` 是模块级单例 —— 导入时就建好了。构造里把
`config.QIDIAN_DIR / "token_usage.json"` 存成 `self._path` 的话，这个路径在
conftest 隔离之前就定死成**生产** `.qidian/` 了，于是测试造的数据写进真实账本。

2026-09-11 实测到的现场：给阶段调用接上记账（`_safe_dispatch` → `_record_phase_usage`）
之后，`tests/test_scheduler/test_project.py` 的桩数据立刻出现在生产
`.qidian/token_usage.json` 里（`model="test"` / `project_name="test-wf"` / `tokens=1`）
—— 这条污染路径一直存在，只是以前没有阶段调用会去写它。

同族规矩见 `docs/防御模式.md` #34；内存模块和 `route_learner` 09-11 已改，
`_token_budget` 是漏掉的那一个。
"""
from singularity.scheduler import config
from singularity.scheduler import _token_budget as tb


def test_budget_path_follows_qidian_dir(tmp_path, monkeypatch):
    """属性必须跟着 `config.QIDIAN_DIR` 走，而不是停在导入时的那个值。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "other")
    assert tb._budget._path == tmp_path / "other" / "token_usage.json"
    assert tb._budget._history_path == tmp_path / "other" / "usage_daily.json"


def test_record_writes_into_current_qidian_dir(tmp_path, monkeypatch):
    """真写一次：文件必须落在当前隔离目录里。"""
    d = tmp_path / "q2"
    d.mkdir()
    monkeypatch.setattr(config, "QIDIAN_DIR", d)

    tb.record_tokens(model="m", tokens=5, project_id="p")

    assert (d / "token_usage.json").exists(), "写到了别处 —— 又去污染生产账本"

    # 收尾：把单例切回本测试的隔离目录，别让后面的测试读到这份状态
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    tb._budget._sync_dir()
