"""超时路径的 token 账（防御模式 §59）。

**为什么单独测这条**：超时任务走不到 `_archive_task_outcome`（唯一记账入口，
它在 finalize 那条路上），所以"这个任务烧了多少"原来整条丢 —— trace 里
`token_count: null`、`token_usage.json` 一条不写。同轮**跑完的**任务正常记了
10 万 token ⇒ 账本没坏，是超时这条没接上。

修法：执行器每 dispatch 一次就把**累计**用量落一盘，超时方读回来。
所以这里钉三件事：落得对（累加不是覆盖）、读得回、抢救失败时**账不跟着丢**。
"""
from types import SimpleNamespace

from singularity.scheduler import config
from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import _exec


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", tmp_path / "partial_usage")


def test_persist_accumulates_across_dispatches(tmp_path, monkeypatch):
    """一次任务可能换 agent 重试 —— 每次的 `token_count` 是**那一次**的，得累加。"""
    _isolate(tmp_path, monkeypatch)
    _exec._persist_partial_usage("t1", "any", "m-a", 100)
    _exec._persist_partial_usage("t1", "any", "m-b", 250)
    assert _exec.read_partial_usage("t1") == (350, "m-a")


def test_read_missing_is_zero_not_crash(tmp_path, monkeypatch):
    """没落过盘 = 不知道，返回 0 让调用方自己决定留 None —— 不是抛。"""
    _isolate(tmp_path, monkeypatch)
    assert _exec.read_partial_usage("nope") == (0, "")


def test_salvage_carries_partial_tokens(tmp_path, monkeypatch):
    """§59 的正题：超时 trace 里的 token_count 不再恒 None。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(proj_mod, "repo_root_for", lambda t: tmp_path)
    _exec._persist_partial_usage("t2", "any", "glm-5.2", 4200)

    disp = orch._salvage_timed_out(SimpleNamespace(id="t2"), 901.0, None)
    r = disp.executor_result
    assert r.token_count == 4200
    assert disp.agent_cfg["model"] == "glm-5.2", "记账要按模型算钱，名字不能丢"
    assert "4200" in r.raw_output
    assert "下界" in r.raw_output, "必须写明是下界 —— 最后那次调用不在里面"


def test_salvage_still_carries_tokens_when_git_part_throws(tmp_path, monkeypatch):
    """git 那段砸了**不等于账也不用记** —— 用量是先读的，跟 git 没关系。

    原来 except 分支直接 `return None`，调用方连 token 都拿不到。
    """
    _isolate(tmp_path, monkeypatch)

    def _boom(t):
        raise RuntimeError("worktree 没了")

    monkeypatch.setattr(proj_mod, "repo_root_for", _boom)
    _exec._persist_partial_usage("t3", "any", "glm-5.2", 777)

    disp = orch._salvage_timed_out(SimpleNamespace(id="t3"), 901.0, None)
    assert disp is not None, "抢救失败也该回一个对象，别回 None"
    assert disp.executor_result.token_count == 777
    assert disp.agent_cfg["model"] == "glm-5.2"


def test_no_usage_yet_stays_none_not_zero(tmp_path, monkeypatch):
    """一轮都没跑完就超时 → 如实留 None。**None 是"不知道"，不是"没花钱"。**"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(proj_mod, "repo_root_for", lambda t: tmp_path)
    disp = orch._salvage_timed_out(SimpleNamespace(id="t4"), 901.0, None)
    assert disp.executor_result.token_count is None


def test_cleanup_removes_the_sidecar(tmp_path, monkeypatch):
    """正常跑完的任务不会消费它 —— 不清就是每个任务留一个孤儿文件。"""
    _isolate(tmp_path, monkeypatch)
    _exec._persist_partial_usage("t5", "any", "m", 10)
    from singularity.scheduler import _worktree
    _worktree.cleanup_task_artifacts("t5", tmp_path)
    assert _exec.read_partial_usage("t5") == (0, ""), "sidecar 没被清掉"
