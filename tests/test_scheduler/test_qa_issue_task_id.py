"""QA 报告里每条 issue 的 `task_id` —— 这个文件**是谁改的**。

背景：GATE3 打回走 impl 分支时，"只重做有问题的任务"一直没人做，卡点是
"任务不记录自己改过哪些文件"。这条边补上之后，匹配从"按标题模糊猜"变成
"按 trace 里的真实改动精确查"。

🔴 **`task_id` 必须由平台填，不许模型写** —— 模型只能凭记忆说"这条问题像是 T3 的"，
而这里要的是"这个文件**确实**是谁改的"（同一份 `changed_files`，与
`workflow._flag_file_overlap` 同源）。

变异验证（删哪一行会红）：
  · 删掉 `_run_verification` 里的 `task_of_file=_task_of_file_map(project)` → 前两条红；
  · `_task_of_file_map` 改成恒返回 `{}` → 前两条红；
  · `build_qa_report` 不再写 `task_id` 键 → 前两条红（KeyError）；
  · 去掉 lineage 里那个 `precise_reset_would_be` → 第三条红。
"""
import json

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import validator
from singularity.scheduler import workflow
from singularity.scheduler.project import Phase


def _seed(tmp_path, monkeypatch, *, files, task_ids=("t1",)):
    """一个能走到 `build_qa_report` 的项目 + 各任务的 trace（trace 里带 changed_files）。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(config, "TRACE_DIR", tmp_path / "trace")
    (tmp_path / "trace").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _id: tmp_path / "repo")
    (tmp_path / "repo").mkdir(exist_ok=True)

    p = proj_mod.ProjectState(id="q1", name="QA task_id")
    # ⚠️ **必须有约束**：`_run_verification` 进门就有一句早退（同
    # `test_e2e_checklist_source`）。给一条**不带 `check`** 的，免得真去跑机械检查。
    p.architecture = {"constraints": [{"type": "security", "rule": "别硬编码密钥"}]}
    p.task_ids = list(task_ids)
    proj_mod.save(p)

    for tid, fs in files.items():
        (tmp_path / "trace" / f"{tid}.json").write_text(
            json.dumps({"changed_files": fs}), encoding="utf-8")
    return p


def _fake_qa(monkeypatch, issues):
    """把 QA 那一步换成固定的 issues —— 不真派发模型（也就不会真花钱）。"""
    monkeypatch.setattr(workflow, "_safe_dispatch", lambda *a, **k: (None, None))
    monkeypatch.setattr(workflow, "_qa_verdict_from_raw",
                        lambda _raw: ({"issues": issues, "passed": []}, "no_go", "有改动要修"))


def _qa_report():
    return json.loads((config.QIDIAN_DIR / "projects" / "q1.qa_report.json")
                      .read_text(encoding="utf-8"))


def test_报告里每条issue带着改它的任务(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, files={"t1": ["src/foo.py"]})
    _fake_qa(monkeypatch, [{"file": "src/foo.py", "severity": "critical",
                            "detail": "少了校验", "fix_route": "impl"}])

    try:
        workflow._run_verification(proj_mod.load("q1"), agents={})
    except Exception:
        pass  # 后面几段要真依赖；这里只关心 qa_report 那一份

    assert _qa_report()["issues"][0]["task_id"] == "t1", "没标出是谁改的 —— 边没接上"


def test_文件没有任何任务改过就留空串(tmp_path, monkeypatch):
    """**空串是有意义的**，不是缺失：报告指着一条没有任务改过的文件

    —— 那本身该被看见（报告在说别的项目 / 路径写错了）。
    """
    _seed(tmp_path, monkeypatch, files={"t1": ["src/foo.py"]})
    _fake_qa(monkeypatch, [{"file": "src/没人改过.py", "severity": "warning",
                            "detail": "x", "fix_route": "impl"}])

    try:
        workflow._run_verification(proj_mod.load("q1"), agents={})
    except Exception:
        pass

    assert _qa_report()["issues"][0]["task_id"] == ""


def test_模型写的是路径_按basename对得上():
    """模型给 `file` 常常是带目录的路径，而 trace 里的 `changed_files` 记的是 basename
    （`_changed_files_of` 就取 basename）—— 两边对不上的话这条边等于没接。"""
    rep = validator.build_qa_report([], [{"file": "a/b/foo.py"}], "no_go", "x",
                                    task_of_file={"foo.py": "t9"})
    assert rep["issues"][0]["task_id"] == "t9"


def test_GATE3打回impl时记账_精准做会重置哪几个(tmp_path, monkeypatch):
    """🔵 **零行为变更**：全量重置照旧（理由见 `handle_gate3_reject` 的 docstring），
    但把"若按 task_id 精准做、本会重置哪几个"记进 lineage ——
    那句"没有真机数据证明匹配可靠之前不换"要的**就是这一行数据**。
    """
    p = _seed(tmp_path, monkeypatch, files={}, task_ids=("t1", "t2"))
    p.phase = Phase.GATE3
    proj_mod.save(p)
    (config.QIDIAN_DIR / "projects" / "q1.qa_report.json").write_text(json.dumps({
        "issues": [{"file": "src/foo.py", "fix_route": "impl", "task_id": "t2"}],
        "summary": {"verdict": "no_go", "verdict_reason": "impl"},
    }), encoding="utf-8")

    workflow.handle_gate3_reject(p, {}, feedback="人打回")

    route = [e for e in p.lineage if e.get("action") == "gate3_route"][-1]
    assert route["route"] == "impl"
    assert route["precise_reset_would_be"] == ["t2"]
    assert route["reset_tasks"] == 0, "全量重置的行为不该被这次改动碰到"
