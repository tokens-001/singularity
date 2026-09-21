"""静默 except 剩下的真形状（2026-09-21 按 `docs/静默except待修清单-20260913.md` §六 收尾）。

形状词表在文档里（S1~S6），**按形状修、不逐条办**。§六 复核后判"仍开着"的全部落在这里，
**两批**（`ffafcbb9` + 本文件所在的那批）：

**第一批 —— 文档点名的五处**
  ① `_review.run_post_exec_checks` 本地安全扫描 `except: pass` —— **S4 假干净**（最重）
  ② `validator.run_project_tests` 跑测试的循环 `except: continue` —— **S4 归因错**
  ③ `_exec._persist_partial_usage` 落盘 `except: pass` —— **S6 账无痕**
  ④ `_process_ledger.record` 的 `done = 0` —— **S2 编造数字**（连同 `digest()` 的渲染）
  ⑤ `_planner.materialize_plan` 的 `parent is None → return []` —— **S3 静默跳过**

**第二批 —— §六 里同样标了"没核"、但没逐条列出的**
  ⑥ `permission.list_pending_approvals` 坏审批文件 `continue` —— **S3 静默跳过**
     ＋ `permission.decide_approval` 读不出来 `return False` —— **S1/S4**：
       返回值照旧 fail-safe，但界面上那句"这条已经不在了"是**错的**（文件在盘上，只是坏了）
  ⑦ `_process_ledger.record` **落盘**失败 `except: pass` —— **S6 账无痕**（S2 那处的邻居）
  ⑧ `_api_tasks` 清残留的 `repo_root` 兜底 —— **§49 族"错仓库"**：任务读不到时拿
     **奇点自己的仓**去 glob `{task_id}_*`，那个任务留在项目仓的 worktree 永远找不到。
     两个调用点（`task_delete` / `task_retry`）原来各抄了一遍 ⇒ 收成一个助手。

⚠️ **八处的一字共同点**：改的**全是"说法/证据"**，`passed` / `action` / 门的强度
**一处没动**（这也是那份清单一贯的处置方式）。所以下面的断言都盯"有没有出声/有没有写进
evidence"，不盯判定。**每处都配了一条对照**（"正常时一个字不变"）——
没有对照的判据只会证明"我让它出声了"，证明不了"只在该出声的时候出声"。

变异验证（删哪一行会红）：
  · ① 把 `except Exception as e:` 收回 `except Exception: pass` → 第 1 条红；
  · ② 把 `broken_runners.append(...)` 删掉 → 第 4 条红；
  · ③ 把 `witness.warn(...)` 那一行删掉 → 第 6 条红；
  · ④ 把 `done = None` 改回 `done = 0` → 第 7 条红（第 8 条也会红）；
  · ⑤ 把 `witness.warn(...)` 那一行删掉 → 第 9 条红。
"""
from types import SimpleNamespace

import pytest

from singularity.scheduler import _exec as ex
from singularity.scheduler import _planner as planner
from singularity.scheduler import _process_ledger as pl
from singularity.scheduler import _review as rv
from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import tracker
from singularity.scheduler import validator as val_mod
from singularity.scheduler import witness


@pytest.fixture
def alerts(monkeypatch):
    """接住 `witness.warn` —— 断言"出声了没有"、以及出的是哪个 key。"""
    got = []
    monkeypatch.setattr(witness, "warn",
                        lambda scope, msg, key="": got.append((scope, str(msg), key)))
    return got


# ══════════════════════ ① _review：本地安全扫描 ══════════════════════

def _review_run(tmp_path, monkeypatch):
    """搭起 `run_post_exec_checks` 的最小台子（照 test_review_harness_attribution 那份）。"""
    monkeypatch.setattr(rv, "_is_trivial_change", lambda *a, **k: False)
    monkeypatch.setattr(rv, "_pick_reviewers", lambda *a, **k: (["m1"], ["m1", "m2"]))
    monkeypatch.setattr(disp_mod, "load_agents", lambda: {})
    monkeypatch.setattr(disp_mod, "_all_agents_list", lambda _a: [])
    monkeypatch.setattr(val_mod, "run_project_tests",
                        lambda *a, **k: {"runner": "none", "passed": True, "total": 0})
    monkeypatch.setattr(val_mod, "multi_model_review",
                        lambda **k: {"issues": [], "models_used": ["m1"], "verdicts": []})
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")

    validation = SimpleNamespace(action="pass", unverified=[])
    quality = {"warnings": [], "confidence": 0.5, "quality_signals": {},
               "failure_kind": "ok", "failure_reason": ""}
    return validation, quality


def _run_review(tmp_path, monkeypatch):
    validation, quality = _review_run(tmp_path, monkeypatch)
    rv.run_post_exec_checks(
        validation=validation, quality=quality, exec_result=None,
        task=SimpleNamespace(project_id="", description="t"),
        agent_cfg={"model": "w"}, level="any", cwd=str(tmp_path),
        changed=["a.py"], base_ref="")
    return validation, quality


def test_安全扫描挂了_不能读成没发现危险代码(tmp_path, monkeypatch, alerts):
    """🔴 本批最重的一条：**"扫描没跑成" ≠ "扫描没发现问题"**。

    它是零成本前置防线（正则、不过模型）；原来挂掉时裸 `pass`，
    于是这份改动照写"没发现危险代码" —— "没报"和"没跑"在结论上长得一模一样。
    """
    monkeypatch.setattr(val_mod, "security_review",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("正则引擎炸了")))
    validation, quality = _run_review(tmp_path, monkeypatch)

    blob = " ".join(quality["warnings"] + validation.unverified)
    assert "本地安全扫描**未完成**" in blob, f"扫描挂掉必须披露，实际：{blob}"
    assert "不是没发现危险代码" in blob, blob
    assert "正则引擎炸了" in blob, f"要带上原因，否则排查不了：{blob}"
    assert any("security_scan_failed" in m for _, m, _ in alerts), alerts


def test_安全扫描正常时一个字不变(tmp_path, monkeypatch):
    """**对照**：扫描跑通了（没发现东西），不该多出任何话。"""
    monkeypatch.setattr(val_mod, "security_review",
                        lambda *a, **k: {"issues": []})
    validation, quality = _run_review(tmp_path, monkeypatch)

    blob = " ".join(quality["warnings"] + validation.unverified)
    # ⚠️ 「未完成」这三个字同函数的 **LLM 安全审计**那支也有（两条文案共有）
    # ⇒ 只许用**这条独有的**字串，否则判据没有判别力（这个坑当场踩过一次）。
    assert "本地安全扫描" not in blob, blob


def test_安全扫描真发现问题照旧拦(tmp_path, monkeypatch):
    """**对照**：真发现有危险代码，原来那套（retry + 进 unverified）不能变。"""
    monkeypatch.setattr(val_mod, "security_review",
                        lambda *a, **k: {"issues": [{"detail": "eval 用户输入"}]})
    validation, quality = _run_review(tmp_path, monkeypatch)

    assert validation.action == "retry"
    blob = " ".join(quality["warnings"] + validation.unverified)
    assert "1 处危险模式" in blob, blob


# ══════════════════════ ② validator：runner 卡住 vs 启动不了 ══════════════════════

def test_runner超时_不能说成三个都启动不了(tmp_path, monkeypatch):
    """`TimeoutExpired` 走的是和 `FileNotFoundError` 同一个 `except: continue` ——
    但一个是"环境没装"、一个是"**测试挂住了**"，排查方向完全相反。"""
    def _boom(*a, **k):
        raise __import__("subprocess").TimeoutExpired(cmd="pytest", timeout=60)

    monkeypatch.setattr(val_mod.subprocess, "run", _boom)
    r = val_mod.run_project_tests(cwd=str(tmp_path))

    assert "没能跑完" in r["output"], r["output"]
    assert "pytest(TimeoutExpired)" in r["output"], r["output"]
    assert "都启动不了" not in r["output"], (
        f"runner 起来了、只是卡住 —— 报成'启动不了'是把排查方向指错：{r['output']}")


def test_runner真的不存在_照旧说启动不了(tmp_path, monkeypatch):
    """**对照**：`FileNotFoundError` 那条路（runner 压根没装）一个字不该变。"""
    def _boom(*a, **k):
        raise FileNotFoundError("no such file: pytest")

    monkeypatch.setattr(val_mod.subprocess, "run", _boom)
    r = val_mod.run_project_tests(cwd=str(tmp_path))

    assert "no test runner found" in r["output"], r["output"]
    assert "没能跑完" not in r["output"], r["output"]


# ══════════════════════ ③ _exec：侧车落盘失败 ══════════════════════

def test_侧车落盘失败_必须出声(tmp_path, monkeypatch, alerts):
    """侧车是**超时任务唯一的账**（收尾记账走不到）⇒ 写失败 = 整笔账没了。
    任务照旧不该被带崩（不上抛），但一个字不说是不行的。"""
    # 让落盘那一步真的炸：把目录指到一个**文件**底下
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", blocker / "sub")

    ex._persist_partial_usage("t1", "any", "m", 123)   # 不抛

    assert alerts, "落盘失败必须出声 —— 侧车是超时任务唯一的账"
    assert any("partial_usage_persist_failed" in m for _, m, _ in alerts), alerts


def test_侧车落盘成功时不出声(tmp_path, monkeypatch, alerts):
    """**对照**：正常落盘不该有告警（否则这条会变成常亮的假红）。"""
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", tmp_path / "pu")

    ex._persist_partial_usage("t2", "any", "m", 5)

    assert not alerts, alerts
    assert (tmp_path / "pu" / "t2.json").exists()


# ══════════════════════ ④ _process_ledger：数不出来就别写 0 ══════════════════════

def test_数不出来时写None_不写0(monkeypatch, tmp_path):
    """**S2**：写 0 = 编造"一条都没成"（缺值有人问，0 没人问）。"""
    from singularity.scheduler import project as pm
    monkeypatch.setattr(tracker, "read_task",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("磁盘挂了")))
    proj = pm.ProjectState(
        id="p1", name="演示", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=["a"], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})

    row = pl.record(proj)          # 不抛

    assert row["tasks_done"] is None, (
        f"数不出来时必须是 None —— 写 0 就是把'不知道'说成'一条都没成'：{row}")


def test_digest里未知不许折成0(monkeypatch, tmp_path):
    """`digest()` 会拼进架构 prompt（"上一轮实际发生了什么"）——
    这里再折一次 0，上面那条修的编造就**换个地方又编了一遍**。"""
    monkeypatch.setattr(pl, "load", lambda *a, **k: [
        {"tasks_total": 5, "tasks_done": None, "issues": {}}])

    text = pl.digest()

    assert "任务 ?/5 成功" in text, text
    assert "失败 5" not in text, f"不知道就不能说失败：{text}"


def test_digest已知时照旧(monkeypatch, tmp_path):
    """**对照**：数得出来时那行一个字不变。"""
    monkeypatch.setattr(pl, "load", lambda *a, **k: [
        {"tasks_total": 5, "tasks_done": 3, "issues": {}}])

    text = pl.digest()

    assert "任务 3/5 成功" in text, text
    assert "失败 2" in text, text


# ══════════════════════ ⑤ _planner：父任务读不到 ══════════════════════

def test_父任务读不到时出声(monkeypatch, alerts):
    """分解出 0 个子任务，和"这个任务本来就不需要分解"长得一模一样 —— 必须出声区分。"""
    monkeypatch.setattr(tracker, "read_task", lambda tid: None)

    assert planner.materialize_plan("t-parent", [{"local_id": "x", "title": "子任务"}]) == []

    assert alerts, "父任务读不到却静默返回空列表"
    scope, msg, _ = alerts[0]
    assert scope == "decompose", alerts
    # ⚠️ 不能冒用 `task_file_corrupt`：read_task 返回 None 有两个来源
    # （文件不存在 / 已隔离且已报过），冒用会让读告警的人去查不存在的损坏。
    assert "parent_task_unreadable" in msg, msg
    assert "t-parent" in msg, f"要指认是哪个父任务：{msg}"


# ══════════════════════ ⑥ permission：待审列表 / 决策 ══════════════════════

def test_待审文件坏了要出声(tmp_path, monkeypatch, alerts):
    """S3：跳过**必须出声**。这是待审列表 —— 坏文件被 `continue` 掉，
    界面上那条审批**凭空消失**，而等它的那一边会一直等到超时（"什么都没发生"）。"""
    from singularity.scheduler import permission as perm
    monkeypatch.setattr(config, "HOLD_DIR", tmp_path)
    (tmp_path / "t1.json").write_text("{坏掉的 json", encoding="utf-8")
    (tmp_path / "t2.json").write_text('{"task_id": "t2", "requested_at": 1e18}',
                                      encoding="utf-8")

    out = perm.list_pending_approvals(now=1e18)

    assert [o["task_id"] for o in out] == ["t2"], f"坏文件照旧跳过：{out}"
    assert any("approval_file_corrupt" in m for _, m, _ in alerts), alerts


def test_待审文件都好的时候不出声(tmp_path, monkeypatch, alerts):
    """**对照**：正常列表不该有告警（否则这条会变成常亮的假红）。"""
    from singularity.scheduler import permission as perm
    monkeypatch.setattr(config, "HOLD_DIR", tmp_path)
    (tmp_path / "t1.json").write_text('{"task_id": "t1", "requested_at": 1e18}',
                                      encoding="utf-8")

    out = perm.list_pending_approvals(now=1e18)

    assert [o["task_id"] for o in out] == ["t1"]
    assert not alerts, alerts


def test_审批文件读不出来_不能说成已经不在了(tmp_path, monkeypatch, alerts):
    """返回值照旧 `False`（**fail-safe 不变**：绝不谎报"我拦下了"）；
    但界面上那句话是"这条已经不在了" —— 文件明明在盘上、只是坏了，那句话是错的。"""
    from singularity.scheduler import permission as perm
    monkeypatch.setattr(config, "HOLD_DIR", tmp_path)
    (tmp_path / "t9.json").write_text("{坏掉的 json", encoding="utf-8")

    assert perm.decide_approval("t9", perm._APPROVE) is False
    assert any("approval_unreadable" in m for _, m, _ in alerts), alerts


# ══════════════════════ ⑦ _process_ledger：账落盘失败 ══════════════════════

def test_账本落盘失败要出声(monkeypatch, alerts):
    """S6：这一格是 `digest()` 的唯一来源，而 `digest()` 会被拼进**架构 prompt**
    的"上一轮实际发生了什么" —— 丢了这一轮，下一轮看到的"上一轮"是**残的**。"""
    from singularity.scheduler import _process_ledger as _pl
    from singularity.scheduler import project as pm
    monkeypatch.setattr(tracker, "read_task", lambda tid: None)
    monkeypatch.setattr(_pl, "_path", lambda: __import__("pathlib").Path("/不存在/x.json"))
    monkeypatch.setattr("singularity.scheduler._io.atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满")))

    proj = pm.ProjectState(
        id="p1", name="演示", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    _pl.record(proj)                       # 不抛

    assert any("record_write_failed" in m for _, m, _ in alerts), alerts


# ══════════════════════ ⑧ _api_tasks：清残留的 repo_root ══════════════════════

def test_任务读不到时_不拿奇点仓顶上且出声(alerts):
    """`cleanup_task_artifacts` 里只有 worktree 那段依赖 repo_root；拿奇点仓去
    glob `{task_id}_*`，那个任务留在**项目仓**的 worktree 永远找不到 ⇒ 残留留着了。"""
    from singularity.scheduler import _api_tasks as api

    got = api._repo_root_for_cleanup(None, "t-404")

    assert got == config.PROJECT_ROOT, "行为不改：照旧兜底"
    assert any("cleanup_repo_unknown" in m for _, m, _ in alerts), alerts


def test_任务读得到时_按项目仓且不出声(monkeypatch, alerts):
    """**对照**：能读到任务时，走 `repo_root_for`、一个字不说。"""
    from singularity.scheduler import _api_tasks as api
    from singularity.scheduler import project as pm
    monkeypatch.setattr(pm, "repo_root_for", lambda t: __import__("pathlib").Path("/项目仓"))

    got = api._repo_root_for_cleanup(object(), "t-1")

    assert str(got) == "/项目仓"
    assert not alerts, alerts
