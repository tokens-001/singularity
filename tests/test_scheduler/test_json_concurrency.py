"""JSON 读-改-写的并发与损坏留底（2026-09-11 审计）。

**lost update**：`route_learner` 原来是 `load → record → save` 三步平铺、无锁。
调度线程与 Flask 请求线程都会走这条路，后写者拿整份内存快照覆盖前者。
实测 20 个线程并发写，**只存活 2 条（丢 18）**。

修法：`atomic_write_json`（防撕裂）+ 一把模块锁盖住整个 RMW（防丢更新），
调用方改走 `record_outcome()`。

**损坏 JSON**：`_memory_core._read_json` 原来解析失败**静默返回空**，
调用方随后 `index_task → _save_events` 会把整份可抢救的文件覆写成只剩新内容。
现在先改名 `.corrupt` 留底再返回空。
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import _memory_core as mc          # noqa: E402
from singularity.scheduler import _token_budget as tb         # noqa: E402
from singularity.scheduler import config as cfg               # noqa: E402
from singularity.scheduler import route_learner as rl         # noqa: E402

N = 20


@pytest.fixture
def learner_path(tmp_path, monkeypatch):
    # 只改 config.QIDIAN_DIR 就够 —— `rl._learner_path()` 是读时现算的
    # （2026-09-11 之前是模块级常量，那时这里必须单独打补丁）。
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    return tmp_path / "route_learner.json"


class TestRouteLearnerNoLostUpdate:
    def test_concurrent_record_outcome_keeps_every_update(self, learner_path):
        """全部 20 条都必须落盘（旧写法只活下来 2 条）。"""
        def work(i):
            rl.record_outcome(task_type="default", model=f"m{i % 3}", level="any",
                              success=True, elapsed_ms=10, tokens=100)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads(learner_path.read_text(encoding="utf-8"))
        total = sum(s.get("sample_count", 0) for s in data["stats"].values())
        assert total == N, f"丢了 {N - total} 条更新（lost update）"

    def test_saved_file_is_valid_json(self, learner_path):
        rl.record_outcome(task_type="default", model="m", level="any", success=True)
        json.loads(learner_path.read_text(encoding="utf-8"))


class TestTokenBudgetNoLostUpdate:
    def test_concurrent_record_keeps_every_entry(self, tmp_path, monkeypatch):
        b = tb.TokenBudget()
        monkeypatch.setattr(b, "_path", tmp_path / "token_usage.json")

        def work(i):
            b.record(project_id="p", project_name="P", task_id=f"t{i}",
                     model="m", level="any", tokens=10)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads(b._path.read_text(encoding="utf-8"))
        assert len(data["daily"]) == N, f"丢了 {N - len(data['daily'])} 条"


class TestCorruptJsonBackedUp:
    def test_corrupt_file_is_renamed_not_silently_emptied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        p = tmp_path / "events.json"
        p.write_text("{ 这不是 JSON", encoding="utf-8")

        assert mc._read_json(p) == {}
        assert not p.exists(), "损坏文件被直接覆盖了，没留底"
        assert (tmp_path / "events.json.corrupt").exists(), "损坏内容没有留底，无法人工抢救"

    def test_missing_file_returns_empty_without_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        assert mc._read_json(tmp_path / "nope.json") == {}
        assert not list(tmp_path.glob("*.corrupt"))
