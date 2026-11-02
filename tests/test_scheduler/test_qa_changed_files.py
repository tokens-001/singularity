"""`qa_changed_files`：执行器自报空时，用合并请求的**硬证据**。

2026-11-02 `round-20261102` 假失败：T2 的产物已 anchor 到
`refs/qidian/pending/1793625962153`（432 行）、MR 也建了，QA 却拿执行器自报的
`changed_files=[]` 判「无文件改动」⇒ 清掉 MR ⇒ 项目停滞。
"""
from pathlib import Path
from types import SimpleNamespace

import singularity.scheduler as _pkg
from singularity.scheduler._exec import qa_changed_files


def test_self_report_wins_when_non_empty():
    er = SimpleNamespace(changed_files=["a.py"])
    mr = SimpleNamespace(changed_files={"b.py"})
    assert qa_changed_files(er, mr) == ["a.py"]


def test_falls_back_to_merge_request_hard_evidence():
    er = SimpleNamespace(changed_files=[])
    mr = SimpleNamespace(changed_files={"jsonlstat/parser.py", "tests/test_parser.py"})
    assert qa_changed_files(er, mr) == ["jsonlstat/parser.py", "tests/test_parser.py"]


def test_none_executor_falls_back():
    mr = SimpleNamespace(changed_files={"x.py"})
    assert qa_changed_files(None, mr) == ["x.py"]


def test_no_evidence_stays_empty():
    assert qa_changed_files(SimpleNamespace(changed_files=[]), None) == []


def test_call_sites_are_wired():
    """钉接线：删掉任何一个调用点，这条就红（只测函数证明不了生产端用了它）。"""
    root = Path(_pkg.__file__).parent
    assert "qa_changed_files(exec_result, pending_merge_req)" in (
        root / "_exec.py").read_text(encoding="utf-8")
    assert "changed = qa_changed_files(" in (
        root / "_task_runner.py").read_text(encoding="utf-8")
