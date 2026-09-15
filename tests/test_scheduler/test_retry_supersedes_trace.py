"""重试必须能写**新**的一份 trace（2026-09-15 真机坐实）。

症状：`1789477697814` / `…816` 重试后跑成功了，trace 却仍写着
「执行超时(>1588s) 被杀，未及输出总结」—— 盘上留着一份**看着像真的错历史**。

根因：`_exec._save_trace` 开头那道幂等守卫（`if trace_path.exists(): return`）
是对着"**同一个 task id**"判的，而重试复用同一个 id ⇒ 第二趟直接被挡在门外，
连后半截（`index_task` / `update_attrs` / `record_scope`）也整块跳过。

⚠️ 这组用例**只钉"旧 trace 有没有让位"**，不钉 `_save_trace` 内部 —— 后者要
拉 `build_report` / 记忆索引一大串，太重；而守卫判的就是下面断言的那个
`exists()`，钉住它等于钉住了那一步的输入。
"""
from types import SimpleNamespace

import pytest

from singularity.scheduler import _api_tasks, config
from singularity.scheduler.tracker import TaskStatus


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    """把 TRACE_DIR 指到 tmp。

    ⚠️ **只改 `QIDIAN_DIR` 是没用的** —— 各 `*_DIR` 是导入时从它算出来的模块级
    常量（本仓踩过，见 `docs/防御模式.md`）。要改就改常量本身。
    """
    d = tmp_path / "traces"
    d.mkdir()
    monkeypatch.setattr(config, "TRACE_DIR", d)
    return d


def _write_trace(d, task_id: str, text: str = '{"final_status": "blocked"}') -> None:
    (d / f"{task_id}.json").write_text(text, encoding="utf-8")


# ── 行为：挪走，而不是删掉 ─────────────────────────────────

def test_旧trace被挪进superseded且内容一字不动(trace_dir):
    _write_trace(trace_dir, "T1", '{"final_status": "blocked", "elapsed": 1588.6}')

    _api_tasks._supersede_trace("T1")

    assert not (trace_dir / "T1.json").exists(), "旧 trace 必须让位，否则新的一份写不进去"
    kept = list((trace_dir / "superseded").glob("T1.*.json"))
    assert len(kept) == 1, "旧 trace 要**留着**（本仓的规矩：不丢证据）"
    assert kept[0].read_text(encoding="utf-8") == '{"final_status": "blocked", "elapsed": 1588.6}'


def test_没有trace时不炸也不建目录(trace_dir):
    _api_tasks._supersede_trace("T1")
    assert not (trace_dir / "superseded").exists()


def test_挪走的是子目录所以不会被非递归glob扫到(trace_dir):
    """全仓对 `traces/` 的 glob 都是 `glob("*.json")`（`witness` / `_memory_lifecycle`）。

    旧证据要是被当成"当前 trace"扫进去，就等于把刚修好的病换个地方发。
    """
    _write_trace(trace_dir, "T1")
    _api_tasks._supersede_trace("T1")

    assert [p.name for p in trace_dir.glob("*.json")] == [], \
        "根目录下不该再有这个任务的 trace —— 各路 glob 都只看这一层"


# ── 接线：task_retry 真的调了它 ────────────────────────────

def test_task_retry接线到supersede(trace_dir, monkeypatch):
    """**这条是钉接线的**：把 `task_retry` 里那行 `_supersede_trace(task_id)` 删掉，
    它必须变红。只测 `_supersede_trace` 自己验的是"函数对"，验不到"接线通"。
    """
    _write_trace(trace_dir, "T1")
    monkeypatch.setattr(_api_tasks.tracker, "read_task",
                        lambda tid: SimpleNamespace(id=tid, status=TaskStatus.FAILED,
                                                    project_id="", title="t"))
    monkeypatch.setattr(_api_tasks.tracker, "transition", lambda *a, **k: None)
    monkeypatch.setattr(_api_tasks, "_cleanup_task_artifacts", lambda *a, **k: 0)

    data, code = _api_tasks.task_retry("T1")

    assert code == 200 and data.get("ok") is True
    # `_exec._save_trace` 的守卫判的就是这个 exists() —— 它必须是 False
    assert not (trace_dir / "T1.json").exists()
    assert list((trace_dir / "superseded").glob("T1.*.json")), "旧的那份应该在 superseded/ 里"
