"""告警通道 + 欠费标记的回归。

背景：告警曾用 `witness.heartbeat('_api', f'warn:{e}')` 记 —— 第二参数是 agent_level，
告警被存成层级名；第一参数是作用域名不是任务 id，清理逻辑找不到 tasks/<id>.json
就判孤儿 unlink。实测写两条、查一次状态就全没了，所以 heartbeats 目录永远是空的。
"""
import json
from pathlib import Path

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


# ── 心跳文件的完整性 ────────────────────────────────────
# 🔴 心跳是「这个任务还在跑」的**唯一**凭据，删它的**正当理由只有一条**：
# 任务已经终态（`_cleanup_terminal_heartbeat`）。⇒ 一个**读不出来**的心跳文件被
# 悄悄 unlink，意味着**可能有一个真卡死的任务从告警系统里消失** —— 之后
# "没心跳" = "不在跑" = 看起来正常（`check_stalled` 扫的正是这个目录）。
#
# 这一族原来两头都漏：
#   · **写侧**：`heartbeat()` 是全 `.qidian` 里唯一一个**裸 `write_text`** 的状态写入
#     ⇒ 能撕（进程被杀 / 磁盘满 / 并发）。撕一次就够了，不要求反复发生。
#   · **读侧**：三处 `except: p.unlink()` **一声不吭**。

def test_写到一半崩掉不会留下半截心跳(qdir, monkeypatch):
    """原子写的**可测行为**：把"写到一半就崩"造出来，正式文件必须还是**崩之前那一份**。

    变异：把 `heartbeat()` 改回 `p.write_text(json.dumps(...))` → 红
    （半截内容直接落在**正式文件**上，读侧下一轮就会把它 unlink 掉）。
    """
    tid = "1700000000001"
    p = qdir / "heartbeats" / f"{tid}_any.json"
    witness.heartbeat(tid, "any")
    assert json.loads(p.read_text(encoding="utf-8"))["task_id"] == tid

    real_write_text = Path.write_text

    def _torn(self, data, *a, **kw):
        real_write_text(self, str(data)[:10], *a, **kw)   # 只落前 10 个字符
        raise OSError("模拟写到一半进程被杀")

    # 用 context() 而不是裸 monkeypatch：`monkeypatch.undo()` 会把 qdir fixture
    # 指的那个 QIDIAN_DIR 一起撤掉，后面读的就是生产目录了。
    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", _torn)
        with pytest.raises(OSError):
            witness.heartbeat(tid, "any")

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["task_id"] == tid, f"正式文件被写坏了: {data!r}"
    assert data["status"] == "running"


class TestCorruptHeartbeatLeavesATrace:
    """🔴 损坏的心跳被删时**必须留痕**（2026-09-19）。三处同形状的现场都钉一遍。

    变异：把任一处改回 `except: p.unlink()` 不吭声 → 对应的参数化用例红。
    """

    def _seed_torn(self, qdir, tid="1700000000002", level="any") -> Path:
        d = qdir / "heartbeats"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tid}_{level}.json"
        p.write_text('{"task_id": "1700000', encoding="utf-8")   # 写了一半
        return p

    @pytest.mark.parametrize("call", [
        "check_stalled",             # 判"卡住"那条路
        "_heartbeat_task_levels",    # 状态面板那条路
        "force_cleanup_heartbeats",  # 启动时那条路
    ])
    def test_三处现场都留痕(self, qdir, call):
        p = self._seed_torn(qdir)
        getattr(witness, call)()

        msgs = [a["msg"] for a in witness.read_alerts()]
        assert any("corrupt_heartbeat_dropped" in m for m in msgs), \
            f"{call} 把损坏的心跳删了却一声不吭 —— 真卡死的任务就这么消失了: {msgs}"
        assert any("1700000000002" in m for m in msgs), \
            f"告警没带上 task_id（文件名里有，坏掉的 JSON 里取不到）: {msgs}"
        assert not p.exists(), "损坏的心跳文件没被清掉"

    def test_没坏就不许报(self, qdir):
        """命门：正常心跳**不许**触发这条告警 —— 否则它又成一条糊筛子的常驻噪声。

        ⚠️ 必须**先有一个还在跑的任务文件**：任务文件不在的心跳本来就会被
        `_cleanup_terminal_heartbeat` 当孤儿删掉（那是**有意**的，别去掉）。
        这里要钉的是另一半：任务还在跑 ⇒ 心跳**留着**、且**不报警**。
        """
        tid = "1700000000003"
        tasks = qdir / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f"{tid}.json").write_text(
            json.dumps({"id": tid, "status": "running"}), encoding="utf-8")

        witness.heartbeat(tid, "any")
        witness.check_stalled()
        witness._heartbeat_task_levels()
        assert [a["msg"] for a in witness.read_alerts()] == []
        assert (qdir / "heartbeats" / f"{tid}_any.json").exists(), \
            "还在跑的任务的心跳被误删了 —— check_stalled 再也看不见它"


# ═══════════════════════════════════════════════════════════════
# 关键告警的**真出口**：桌面通知（2026-09-20）
# ═══════════════════════════════════════════════════════════════
# 落进 `alerts.jsonl` 只是"记下来了" —— 人不在看告警页时它等于没发生。
# 而盘上 4159 条告警里 `drain_dep_blocked` 一个 key 就占 3180（76%），
# ⇒ 只有**罕见 ∧ 人现在就得动手**的那几个才推出去，其余留在页面上。
class Test关键告警出口:

    @pytest.fixture(autouse=True)
    def _notify_on(self, monkeypatch):
        # conftest 的全局夹具把 `QIDIAN_NOTIFY` 关了（跑测试不该刷人桌面），
        # 这一组要测的正是"关掉/打开"本身 ⇒ 打开它，并把真正执行的那条命令换掉。
        monkeypatch.setenv("QIDIAN_NOTIFY", "1")
        monkeypatch.setattr(witness, "_last_notified", {})
        # 🔴 **平台也要钉**（2026-09-20，CI 当场抓到的）：出口只在 macOS 上开
        # （`sys.platform != "darwin"` 直接返回），而 **CI 跑在 ubuntu 上** ⇒
        # 不钉的话这几条在 CI 上必红 —— 又一出"本机绿、CI 红"。
        # 钉成 darwin 才是在测**真那条路**；"非 macOS 不推"另有一条专门的测试。
        monkeypatch.setattr(witness.sys, "platform", "darwin")

    @pytest.fixture
    def pushes(self, monkeypatch):
        """把"真的去调 osascript"换成记录 —— 测试**不许**弹窗。"""
        got: list[list[str]] = []
        monkeypatch.setattr(witness.subprocess, "run",
                            lambda cmd, **k: got.append(cmd))
        return got

    def test_关键key会推出去(self, qdir, pushes):
        """判据：命令里**带上了那句告警原文**（不是只推个"有新告警"——那样还得回页面找）。"""
        witness.warn("exec", "observer_stalled_task:任务卡了 12 分钟",
                     key="observer_stalled_task")
        assert len(pushes) == 1, f"关键告警没推出去：{pushes}"
        cmd = " ".join(pushes[0])
        assert "osascript" in cmd and "display notification" in cmd
        assert "任务卡了 12 分钟" in cmd, f"推出去的通知里没有正文 ⇒ 人还得回页面找：{cmd}"

    def test_常驻噪声不许推(self, qdir, pushes):
        """**边界**（这条才是这个白名单存在的理由）：占 76% 的那个 key 不许推。

        变异：把 `_CRITICAL_ALERT_KEYS` 改成"什么都进"（或去掉 warn 里那句判断）⇒ 本条红。
        """
        for noise in ("drain_dep_blocked", "decompose", "collect_changes",
                      "lazy_spoke_import_failed", "designated_reviewer_is_writer"):
            witness.warn("orch", f"{noise}:xxx", key=noise)
        assert pushes == [], f"常驻噪声也推了 —— 人会被刷到把通知关掉：{pushes}"

    def test_同一个key一分钟内只叫一次(self, qdir, pushes):
        """判据：连报 5 次只推 1 次。**冷却的是同一个 key**，不是全局 —— 换了 key 照推。"""
        for _ in range(5):
            witness.warn("exec", "observer_stalled_task:卡了", key="observer_stalled_task")
        assert len(pushes) == 1, f"同一个 key 刷了 {len(pushes)} 条通知"
        witness.warn("exec", "merge_queue_stuck:队列卡住", key="merge_queue_stuck")
        assert len(pushes) == 2, "换了个 key 也不推了 —— 冷却被写成了全局"

    def test_总闸能关(self, qdir, pushes, monkeypatch):
        """**通知发出去收不回来** ⇒ 得有一个不改代码就能关的开关。"""
        monkeypatch.setenv("QIDIAN_NOTIFY", "0")
        witness.warn("exec", "merge_queue_stuck:队列卡住", key="merge_queue_stuck")
        assert pushes == [], f"总闸关了还推：{pushes}"

    def test_通知炸了不许连累告警本身(self, qdir, monkeypatch):
        """`osascript` 挂了（没装/没权限）⇒ 告警**照旧落盘**，且**不许递归**再报一条。"""
        def boom(*a, **k):
            raise OSError("没有 osascript")

        monkeypatch.setattr(witness.subprocess, "run", boom)
        witness.warn("exec", "merge_queue_stuck:队列卡住", key="merge_queue_stuck")
        lines = [json.loads(x) for x in (qdir / "alerts.jsonl").read_text(
            encoding="utf-8").splitlines()]
        assert len(lines) == 1, f"通知炸了把告警本身也弄丢了 / 递归报了第二条：{lines}"
        assert lines[0]["key"] == "merge_queue_stuck"

    def test_引号换行不许把命令拼坏(self):
        """告警正文来自异常/模型输出，**引号和换行必须有转义**（否则 AppleScript 语法错、
        通知静默失败）。⚠️ 用 `json.dumps` 也不行 —— 它的 `\\uXXXX` AppleScript 不认。"""
        s = witness._as_applescript_string('他说"别动"\n还有\\反斜杠')
        assert s.startswith('"') and s.endswith('"')
        assert '\n' not in s, "换行没被处理 —— AppleScript 单行语法会断在这"
        assert '\\"' in s and "\\\\" in s

    def test_通知没弹出来不许静默(self, qdir, monkeypatch, caplog):
        """⚠️ **返回码要看**：`osascript` 语法错/没权限时是**非零退出**。

        不看的话"没弹出来"和"发出去了"长得一模一样 —— 而这条通道的**全部意义**
        就是"人真的看到了"。变异：把 `if r.returncode != 0` 那段删掉 ⇒ 本条红。
        """
        import logging as _logging

        class _R:
            returncode = 1
            stderr = "osascript: 语法错误"
            stdout = ""

        monkeypatch.setattr(witness.subprocess, "run", lambda *a, **k: _R())
        with caplog.at_level(_logging.WARNING, logger="witness"):
            witness.warn("exec", "merge_queue_stuck:队列卡住", key="merge_queue_stuck")
        assert any("桌面通知没发出去" in r.getMessage() for r in caplog.records), \
            f"通知没弹出来却一声不吭：{[r.getMessage() for r in caplog.records]}"

    def test_非macOS就不推(self, qdir, pushes, monkeypatch):
        """**有意限制**：出口只在 macOS 上开 —— 别的平台是"没验证过的不写"，
        不是漏了。这条把它钉住：哪天有人去掉平台判断、在 CI 上真去调 `osascript`，
        这里会红。

        ⚠️ 上一条夹具把平台钉成 darwin 了，这条**必须自己钉回 linux**。
        """
        monkeypatch.setattr(witness.sys, "platform", "linux")
        witness.warn("exec", "merge_queue_stuck:队列卡住", key="merge_queue_stuck")
        assert pushes == [], f"非 macOS 上也去弹通知了：{pushes}"
