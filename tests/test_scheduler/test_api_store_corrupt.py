"""`api_store` 的**自毁链**：读损坏 → `_seed()` → `_save()` 整库重置。

出处：扫bug-02 ②（我复核属实）。形状和 `_process_ledger` 那条一样 ——
「损坏」和「不存在」走了同一条路，下游拿空值照常跑、照常**写回整份**。

这里钉两件事：
  · **读**：损坏时降级成空（带告警），**绝不 `_seed()`**（`_seed` 末尾就是 `_save`）
  · **写**：这一轮读到过损坏 ⇒ `_save` **拒写**，不拿空表去重建整库
"""
import json

import pytest

from singularity.scheduler import api_store, config


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(api_store, "_CORRUPT", False)   # 每个用例都从"没坏过"开始
    return tmp_path / ".qidian" / "api_store.json"


def test_库文件损坏时不许拿种子数据盖掉它(store, monkeypatch):
    """**正题**：一次撕裂写之后，下一次读取不许把整库换成种子数据。

    原来的链：`_load()` 读到 JSONDecodeError → `pass` → `return _seed()`
    → `_seed()` 末尾 `_save(entries)` ⇒ **真库被种子数据盖掉了**，
    而 `api_store.json` 看起来完全正常（欠费标记/别名/自建 key 全没了）。
    """
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    raw = '{"deepseek": {"id": "deepseek", "欠费": true}, "还没写完":'
    store.write_text(raw, encoding="utf-8")

    got = api_store.list_all()

    assert got == {}, "损坏时该降级成空表（带告警），不该凭空出现种子数据"
    assert store.read_text(encoding="utf-8") == raw, \
        "原文件被盖成了种子数据 —— 这正是那条自毁链"
    assert (store.parent / "api_store.json.corrupt").exists(), "没留备份"
    assert warns, "降级了却一声不吭"


def test_读过损坏之后_写侧要拒写(store, monkeypatch):
    """读降级之后不能顺手把空表写回去 —— 那和"拿种子盖掉"是同一个后果，只是少一层。"""
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    raw = '{"deepseek": {"id": "deepseek"}, "坏":'
    store.write_text(raw, encoding="utf-8")

    api_store.list_all()                      # 这一步把 _CORRUPT 置上
    api_store.add("newone", "新", "https://x/v1", "NEW_KEY")

    assert store.read_text(encoding="utf-8") == raw, \
        "损坏的库被整份重建了（历史配置没了，而文件名一模一样）"
    assert any("save_skipped" in str(a) for a in warns), f"拒写了却没出声：{warns}"


def test_文件不存在才是真的空_该建库(store, monkeypatch):
    """对照：**真的没有**文件时仍然要建库（别把首次运行也堵死）。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    assert not store.exists()

    got = api_store.list_all()

    assert got, "首次运行没有建库"
    assert store.exists(), "种子没落盘"


def test_库好的时候照常读写(store, monkeypatch):
    """对照：正常路径完全不受影响。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    api_store.list_all()
    api_store.add("mykey", "我的", "https://api.example.com/v1", "MY_KEY")
    assert "mykey" in api_store.list_all()
