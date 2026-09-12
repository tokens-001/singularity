"""告警通道 + 欠费标记的回归。

背景：告警曾用 `witness.heartbeat('_api', f'warn:{e}')` 记 —— 第二参数是 agent_level，
告警被存成层级名；第一参数是作用域名不是任务 id，清理逻辑找不到 tasks/<id>.json
就判孤儿 unlink。实测写两条、查一次状态就全没了，所以 heartbeats 目录永远是空的。
"""
import json

import pytest

from singularity.scheduler import config, witness, api_store


@pytest.fixture
def qdir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    return tmp_path / ".qidian"


def _seed_api(qdir, api_id="deepseek", status="active"):
    (qdir / "api_store.json").write_text(json.dumps({
        api_id: {"id": api_id, "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                 "api_key_env": "DEEPSEEK_API_KEY", "status": status, "notes": "",
                 "created_at": 0.0, "updated_at": 0.0}}), encoding="utf-8")


# ── 告警通道 ────────────────────────────────────────────

def test_warn_survives_heartbeat_cleanup(qdir):
    """核心回归：老写法存在心跳文件里，一次状态查询就被当孤儿删光。"""
    witness.warn("_api", "boom")
    witness._heartbeat_task_levels()   # 老写法的行刑现场
    witness.check_stalled()
    assert [a["msg"] for a in witness.read_alerts()] == ["boom"]


def test_read_alerts_newest_first_and_limit(qdir):
    for i in range(5):
        witness.warn("s", f"m{i}")
    assert [a["msg"] for a in witness.read_alerts(limit=3)] == ["m4", "m3", "m2"]


def test_read_alerts_since_is_inclusive(qdir):
    witness.warn("s", "old")
    ts = witness.read_alerts()[0]["ts"]
    witness.warn("s", "new")
    assert [a["msg"] for a in witness.read_alerts(since=ts)] == ["new", "old"]  # 含边界
    assert witness.read_alerts(since=ts + 1) == []                             # 未来时间 → 空


def test_read_alerts_empty_when_no_file(qdir):
    assert witness.read_alerts() == []


def test_review_module_can_reach_witness():
    """接线检查：_review 里用了模块级 witness.warn，顶部就必须有 import。

    漏过一次（写 warn 时该文件顶部只有 subprocess/time/pathlib，别处用的是局部
    import）—— 测试全绿但生产走到那行就 NameError。
    """
    from singularity.scheduler import _review, witness
    assert _review.witness is witness


def test_warn_never_raises(qdir):
    """告警本身失败不该变成新的错误源。"""
    witness.warn(None, None)
    assert witness.read_alerts(limit=1)[0]["msg"] == "None"


# ── 欠费标记 ────────────────────────────────────────────

def _pin_provider(monkeypatch, provider="deepseek"):
    from singularity.scheduler import model_registry as mr
    monkeypatch.setattr(mr, "provider_for_model", lambda m: provider)


def test_note_api_error_marks_quota_exhausted(qdir, monkeypatch):
    _seed_api(qdir)
    _pin_provider(monkeypatch)
    assert api_store.note_api_error("any-model", 402, "Insufficient Balance") == "deepseek"
    assert api_store.get("deepseek").status == "quota_exhausted"
    assert any("quota_exhausted" in a["msg"] for a in witness.read_alerts())
    # 标了之后调度不再选它
    assert api_store.is_available("deepseek") is False


def test_note_api_error_catches_chinese_wording(qdir, monkeypatch):
    """智谱用 400 + 中文"余额不足"，不是 402。"""
    _seed_api(qdir)
    _pin_provider(monkeypatch)
    assert api_store.note_api_error("glm-5.2", 400, '{"error":{"message":"余额不足"}}') == "deepseek"
    assert api_store.get("deepseek").status == "quota_exhausted"


def test_note_api_error_ignores_generic_400(qdir, monkeypatch):
    """普通 400 不能误标 —— 否则一次参数错误就把整个 provider 摘了。"""
    _seed_api(qdir)
    _pin_provider(monkeypatch)
    assert api_store.note_api_error("any-model", 400, "bad temperature") == ""
    assert api_store.get("deepseek").status == "active"
    assert witness.read_alerts() == []


def test_executor_actually_wires_the_hook(qdir, monkeypatch):
    """接线验证：执行器真收到 402 时标记 provider（不只是函数本身可用）。"""
    from singularity.scheduler.executors import openai_agent as oa
    _seed_api(qdir)
    _pin_provider(monkeypatch)

    class FakeResp:
        status_code = 402
        text = '{"error":{"message":"Insufficient Balance"}}'

    class FakeExec:
        _model = "any-model"

    with pytest.raises(oa._FormatError):     # 该抛的还是抛，标记只是顺带
        oa.OpenAIAgentExecutor._raise_for_status(FakeExec(), FakeResp())
    assert api_store.get("deepseek").status == "quota_exhausted"


def test_no_message_written_into_heartbeat_level():
    """不变量：`witness.heartbeat(task_id, level)` 的第二参数只能是真的 agent level。

    往里塞 f-string 消息会：① 被存成一个伪 level（status 还是 "running"），
    状态面板的"运行中"虚高、负载列表出现垃圾条目；② 任务到终态时那个心跳文件
    被 `_cleanup_terminal_heartbeat` unlink —— 信息等于没记。
    非任务状态的信息走 `witness.warn`（append-only，不受清理影响）。

    这条防的是回归：2026-09-10 在 _worktree / _exec 里又抓到 4 处这么写的。
    """
    import re
    from pathlib import Path
    import singularity.scheduler as pkg

    bad = []
    for py in Path(pkg.__file__).parent.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "def heartbeat" in line or line.lstrip().startswith("#"):
                continue
            m = re.search(r"heartbeat\(\s*[^,]+,\s*(.*?)\)\s*$", line)
            if m and m.group(1).lstrip().startswith(("f'", 'f"')):
                bad.append(f"{py.name}:{i}: {line.strip()[:70]}")
    assert not bad, "心跳第二参数被当成消息用了（应改走 witness.warn）:\n" + "\n".join(bad)


# ── 告警聚合：把"常驻条件"从事件流里分出来 ──────────────────────
#
# 背景（2026-09-13）：真机一段 26 分钟的窗口里 25 条告警，**22 条（88%）挤在两个
# key 上**（collect_changes 12× / constraints_checklist_fallback 10×）。
# 常亮不是"最近问题多"，是判据跟配置脱节 —— 淹在事故流里会把真事故盖住。

class TestAlertKey:

    def test_clean_prefix_is_the_key(self):
        assert witness._derive_alert_key("collect_changes:no_baseline_ref") == "collect_changes"

    def test_bare_exception_is_not_a_key(self):
        """裸 `f'{e}'` 长成 `KeyError: 'x'`，看着像 key —— 认了就等于把所有
        KeyError 并成一条，真事故会被并进噪声栏（比不聚合更坏）。"""
        assert witness._derive_alert_key("KeyError: 'x'") == ""
        assert witness._derive_alert_key("RuntimeError: boom") == ""
        assert witness._derive_alert_key("TimeoutException: t") == ""

    def test_no_colon_is_not_a_key(self):
        assert witness._derive_alert_key("read_task_file") == ""
        assert witness._derive_alert_key("拒绝非法流转 a→b (task=x)") == ""

    def test_explicit_key_wins(self, qdir):
        """写法不像 `标识符:` 的调用点可以显式传 key。"""
        witness.warn("budget", "ValueError: x", key="budget_probe_failed")
        assert witness.read_alerts()[0]["key"] == "budget_probe_failed"


class TestAlertSummary:

    def test_groups_by_key_and_marks_chronic(self, qdir):
        for _ in range(3):
            witness.warn("orch", "collect_changes:no_baseline_ref")
        witness.warn("exec", "cascade_skip:m1→m2")
        s = witness.alert_summary(chronic_min=3)
        assert s[0]["key"] == "collect_changes" and s[0]["n"] == 3 and s[0]["chronic"]
        assert s[1]["key"] == "cascade_skip" and s[1]["n"] == 1 and not s[1]["chronic"]

    def test_two_different_exceptions_do_not_collapse(self, qdir):
        """这条是**不聚合**那一侧的安全网：两条无关的裸异常各自成组，
        不许因为都叫 `KeyError` 就被并成"常驻条件"。"""
        witness.warn("orch", "KeyError: 'alpha'")
        witness.warn("orch", "KeyError: 'beta'")
        s = witness.alert_summary(chronic_min=2)
        assert len(s) == 2, f"两条不相干的异常被并成一条了: {s}"
        assert all(not e["chronic"] for e in s)

    def test_same_key_across_scopes_merge(self, qdir):
        """同一个 key 从两个 scope 报出来 = **一个常驻条件被两个调用方各报一遍**。
        拿 scope 当身份会把它劈成两行、每行都不够"常驻"（真机上 `collect_changes`
        就是从 `oa_exec` / `claude_cli` 两边报出来的）。范围信息不丢，`scopes` 带着。"""
        witness.warn("a", "same_key:x")
        witness.warn("b", "same_key:x")
        s = witness.alert_summary(chronic_min=2)
        assert len(s) == 1 and s[0]["n"] == 2 and s[0]["chronic"]
        assert sorted(s[0]["scopes"]) == ["a", "b"]

    def test_since_window_filters(self, qdir):
        p = qdir / "alerts.jsonl"
        p.write_text("\n".join([
            json.dumps({"ts": 100.0, "scope": "s", "msg": "old_key:x", "key": "old_key"}),
            json.dumps({"ts": 900.0, "scope": "s", "msg": "new_key:x", "key": "new_key"}),
        ]) + "\n", encoding="utf-8")
        keys = [e["key"] for e in witness.alert_summary(since=500.0)]
        assert keys == ["new_key"], f"时间窗没起作用: {keys}"

    def test_same_key_different_details_aggregate(self, qdir):
        """**真机的形状就是这样**：同一个 key、后面跟着不同明细 ——
        `collect_changes:no_baseline_ref（判据不完整）` 与 `...（跟 HEAD 比，已提交的看不见）`。
        按整条 msg 分组的实现会在这里把它们算成两组，聚合就等于没做。"""
        witness.warn("oa_exec", "collect_changes:no_baseline_ref（判据不完整）")
        witness.warn("claude_cli", "collect_changes:no_baseline_ref（跟 HEAD 比，已提交的看不见）")
        s = witness.alert_summary(chronic_min=2)
        assert len(s) == 1 and s[0]["key"] == "collect_changes" and s[0]["n"] == 2, s
