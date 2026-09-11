"""模型范围纪律表：谁写的方案"超范围"少，融合时就选谁定稿。

这张表原来**只有人手动跑的离线脚本**会写（`tests/integration/coverage_audit.py`）
→ 表常年是旧的（2026-09-12 实测停在 9/10，且缺了当前主力 `deepseek-flash`），
而它决定"选谁定稿" —— 实测定稿人是**乘法器还是过滤器**直接决定产物的范围纪律。

**铁律：量不出来就不记。** 没有尺子时记 0 = 编造"这次很干净"。
"""
import json
import threading

import pytest

from singularity.scheduler import _model_discipline as md


class TestRecord:
    def test_accumulates(self, tmp_path, monkeypatch):
        assert md.record("m1", 2)
        assert md.record("m1", 1)
        d = md.load()["m1"]
        assert d["violations"] == 3 and d["audits"] == 2

    def test_rejects_bogus_input(self, tmp_path, monkeypatch):
        assert md.record("", 1) is False, "没模型名不记"
        assert md.record("m1", None) is False, "没有违例数不记"
        assert md.record("m1", -1) is False, "负数不记"

    def test_stats_reports_per_audit(self, tmp_path, monkeypatch):
        md.record("m1", 2)
        md.record("m1", 4)
        assert md.stats()["m1"]["per_audit"] == 3.0

    def test_missing_file_reads_as_empty(self, tmp_path, monkeypatch):
        assert md.load() == {}


class TestRecordScope:
    def test_counts_files_outside_declared(self, tmp_path, monkeypatch):
        assert md.record_scope("m1", ["a.py", "b.py", "evil.py"], ["a.py", "b.py"])
        assert md.load()["m1"]["violations"] == 1

    def test_no_declared_means_no_record(self, tmp_path, monkeypatch):
        """**铁律**：没有"声明范围"这把尺子 → 不记。
        记 0 等于编造"这次很干净"，表会越用越假，而它还决定选谁定稿。"""
        assert md.record_scope("m1", ["a.py"], []) is False
        assert md.record_scope("m1", ["a.py"], None) is False
        assert md.load() == {}, "一个字都不该写进去"

    def test_all_within_scope_records_zero_violation_but_still_audits(self, tmp_path, monkeypatch):
        """**测了、结果是 0** 和 **没测** 是两回事 —— 前者要记（那次审计发生了）。"""
        assert md.record_scope("m1", ["a.py"], ["a.py"]) is True
        assert md.load()["m1"] == {"violations": 0, "audits": 1,
                                   "last_ts": md.load()["m1"]["last_ts"]}

    def test_pycache_not_a_violation(self, tmp_path, monkeypatch):
        assert md.record_scope("m1", ["a.py", "__pycache__/x.pyc"], ["a.py"]) is True
        assert md.load()["m1"]["violations"] == 0

    def test_under_changing_is_not_a_violation(self, tmp_path, monkeypatch):
        """声明是**上限不是配额** —— 少改了不算超出范围。"""
        assert md.record_scope("m1", ["a.py"], ["a.py", "b.py", "c.py"]) is True
        assert md.load()["m1"]["violations"] == 0

    def test_path_normalization(self, tmp_path, monkeypatch):
        assert md.record_scope("m1", ["./a.py"], ["a.py"]) is True
        assert md.load()["m1"]["violations"] == 0


class TestConcurrency:
    def test_concurrent_records_do_not_lose_data(self, tmp_path, monkeypatch):
        """并发任务会同时记 —— 读-改-写不串起来会互相覆盖（§13 / #46）。"""
        def _hit():
            for _ in range(10):
                md.record("m1", 1)
        ts = [threading.Thread(target=_hit) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert md.load()["m1"]["audits"] == 80, "80 次一次都不许丢"


class TestPathNotFrozen:
    def test_path_computed_at_call_time(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "one")
        assert md._path() == tmp_path / "one" / "model_discipline.json"
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "two")
        assert md._path() == tmp_path / "two" / "model_discipline.json"


class TestReaderContract:
    def test_pick_writer_reads_the_same_file(self, tmp_path, monkeypatch):
        """写给 `_pick_writer` 读的表必须是同一份（两边各写各的 = 表永远是旧的）。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        md.record("deepseek-flash", 0)
        p = tmp_path / "model_discipline.json"
        assert p.exists()
        assert "deepseek-flash" in json.loads(p.read_text(encoding="utf-8"))
