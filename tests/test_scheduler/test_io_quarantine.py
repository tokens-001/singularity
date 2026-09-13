"""S1 第一批：损坏的 JSON/TOML **不许被当成"空"** —— 试点在流程账本。

形状（`docs/静默except待修清单-20260913.md` 的 S1，C 的草案给了共用函数）：
  `json.loads` 失败 → `return {}` 会让"文件坏了"和"文件是空的"走同一条路。
  下游拿到空值照常跑、照常**写回整份** ⇒ 一次撕裂写在下一次读取时把真数据全盖掉。
  `api_store` 那条"读到损坏即 `_seed()` 整库重置"就是这个形状里最狠的一例。

这里只钉**第一批**（H 的建议：先拿一处跑通，别一晚铺 16 个接入点）：
共用函数 `load_json_or_quarantine` + 第一个接入点 `_process_ledger`。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler import _io


# ═══════════════════════════════════════════════════════════════
# ① 共用函数：三态 + 隔离 + 出声
# ═══════════════════════════════════════════════════════════════

def test_文件不存在才算真的空(tmp_path):
    assert _io.load_json_or_quarantine(tmp_path / "nope.json") == {}
    assert _io.load_json_or_quarantine(tmp_path / "nope.json", expect=list) == []


def test_好好的文件原样返回(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert _io.load_json_or_quarantine(p) == {"a": 1}


def test_损坏返回_None_而不是空(tmp_path, monkeypatch):
    """**正题**：坏文件必须返回 None —— 让调用方**有机会**区分"坏了"和"空的"。"""
    seen = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: seen.append(a))
    p = tmp_path / "bad.json"
    p.write_text('{"a": 1', encoding="utf-8")      # 半截 JSON

    assert _io.load_json_or_quarantine(p) is None
    assert seen, "隔离了却没说 —— 下一个读日志的人只会看到'数据凭空没了'"


def test_损坏时原文件一字不动且留了证据(tmp_path, monkeypatch):
    """备份是**原始字节**，不是"重新序列化一遍" —— 否则证据本身就失真了。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p = tmp_path / "bad.json"
    raw = '{"a": 1, "还没写完":'
    p.write_text(raw, encoding="utf-8")

    _io.load_json_or_quarantine(p)

    assert p.read_text(encoding="utf-8") == raw, "原文件被动过了"
    bak = tmp_path / "bad.json.corrupt"
    assert bak.exists(), "没留备份"
    assert bak.read_text(encoding="utf-8") == raw, "备份不是原样字节"


def test_二次损坏不毁掉第一次的证据(tmp_path, monkeypatch):
    """再坏一次要**轮转**出新文件，不能把上一份 `.corrupt` 盖掉。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p = tmp_path / "bad.json"
    p.write_text("第一次坏", encoding="utf-8")
    _io.load_json_or_quarantine(p)
    p.write_text("第二次坏", encoding="utf-8")
    _io.load_json_or_quarantine(p)

    baks = sorted(tmp_path.glob("bad.json.corrupt*"))
    assert len(baks) == 2, f"第二次把第一次的证据盖掉了：{baks}"
    assert "第一次坏" in (tmp_path / "bad.json.corrupt").read_text(encoding="utf-8")


def test_顶层类型不对也算坏(tmp_path, monkeypatch):
    """`expect=list` 而文件里是个 dict ⇒ 不能当空表放过去（下面那个消费端会 append）。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p = tmp_path / "shape.json"
    p.write_text(json.dumps({"不是": "列表"}), encoding="utf-8")
    assert _io.load_json_or_quarantine(p, expect=list) is None
    assert (tmp_path / "shape.json.corrupt").exists()


def test_toml_版同契约(tmp_path, monkeypatch):
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    assert _io.load_toml_or_quarantine(tmp_path / "nope.toml") == {}
    p = tmp_path / "bad.toml"
    p.write_text("这不是 = = toml", encoding="utf-8")
    assert _io.load_toml_or_quarantine(p) is None
    assert (tmp_path / "bad.toml.corrupt").exists()


# ═══════════════════════════════════════════════════════════════
# ② 第一个接入点：流程账本（读降级 + 写拒写）
# ═══════════════════════════════════════════════════════════════

def _project():
    return type("P", (), {"id": "p1", "name": "测试项目", "task_ids": [],
                          "issues": [], "phase": None})()


def test_账本损坏时_读侧降级但原文件还在(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    from singularity.scheduler import _process_ledger as L
    p = L._path()
    raw = '[{"ts": 1}, {"ts":'          # 半截
    p.write_text(raw, encoding="utf-8")

    assert L.load() == [], "读侧该降级成空表（调用方没崩）"
    assert p.read_text(encoding="utf-8") == raw, "读了一下就把原文件弄没了"
    assert (tmp_path / ".qidian" / "process_ledger.json.corrupt").exists()


def test_账本损坏时_写侧拒绝整份重建(tmp_path, monkeypatch):
    """**这条才是重点**：坏账本期间记一笔，**不许**把历史账本换成只有这一行的新账本。

    不拒写的话：坏账本 → record 拿到空表 → append 一行 → 写回整份
    ⇒ 历史全没了，而 `process_ledger.json` 看起来**完全正常**。
    """
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    from singularity.scheduler import _process_ledger as L
    p = L._path()
    raw = '[{"ts": 1, "name": "历史那一行"}]\n这后面是坏的'
    p.write_text(raw, encoding="utf-8")

    row = L.record(_project())          # 不许抛

    assert row and row["project_id"] == "p1", "本轮该照常返回这一行"
    assert p.read_text(encoding="utf-8") == raw, \
        "坏账本被整份重建了 —— 历史账本没了，而文件名一模一样"
    assert any("record_skip" in str(a) for a in warns), f"拒写了却没出声：{warns}"


def test_账本好的时候照常记账(tmp_path, monkeypatch):
    """对照：正常路径必须照写，别把功能修没了。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    from singularity.scheduler import _process_ledger as L

    L.record(_project())
    L.record(_project())

    rows = json.loads(L._path().read_text(encoding="utf-8"))
    assert len(rows) == 2, f"正常路径没记上：{rows}"


# ═══════════════════════════════════════════════════════════════
# ③ 第二个接入点：MCP 服务器配置（TOML，两侧）
# ═══════════════════════════════════════════════════════════════

def _mcp_env(tmp_path, monkeypatch):
    from singularity.scheduler import mcp as M
    cfg = tmp_path / "mcp_servers.toml"
    monkeypatch.setattr(M, "MCP_CONFIG_PATH", cfg)
    monkeypatch.setattr(M, "_MCP_CONFIG_CORRUPT", False)
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    return M, cfg


def test_mcp_配置坏了不许拿默认值冒名(tmp_path, monkeypatch):
    """**正题**：读坏了 → 空列表，**不是** `_default_configs()`。

    原来是"读失败 → 用默认配置"，而 add / delete / refresh 都会拿手里这份去
    `save_mcp_configs` ⇒ **默认配置覆盖掉用户配的服务器**（命令/env/headers 全没），
    而 `mcp_servers.toml` 看起来完好。
    """
    M, cfg = _mcp_env(tmp_path, monkeypatch)
    raw = '[[servers]]\nname = "myserver"\ncommand = "npx x"'
    cfg.write_text(raw + "\n这不是 = = toml", encoding="utf-8")

    assert M.load_mcp_configs() == [], "读坏了却返回了默认配置 —— 它随后会被写回去"
    assert cfg.read_text(encoding="utf-8").startswith(raw), "原文件被动过了"
    assert (tmp_path / "mcp_servers.toml.corrupt").exists(), "没留备份"


def test_mcp_读坏之后写侧拒写(tmp_path, monkeypatch):
    M, cfg = _mcp_env(tmp_path, monkeypatch)
    raw = '[[servers]]\nname = "myserver"\n坏 = ='
    cfg.write_text(raw, encoding="utf-8")

    M.load_mcp_configs()                      # 置上损坏标记
    ok = M.save_mcp_configs([])               # 任何保存都该被拒

    assert ok is False, "拒写要如实返回 False"
    assert cfg.read_text(encoding="utf-8") == raw, "损坏的配置被整份重建了"


def test_mcp_文件不存在才用默认配置(tmp_path, monkeypatch):
    """对照：真的没有文件时，`_default_configs()` 仍然是对的。"""
    M, _cfg = _mcp_env(tmp_path, monkeypatch)
    assert M.load_mcp_configs() == M._default_configs()


# ═══════════════════════════════════════════════════════════════
# ④ 第三个接入点：agents_custom.json —— **一个文件两个写者**
# ═══════════════════════════════════════════════════════════════
# `_dispatch_crud`（增删 agent）和 `skill_loader`（绑技能）写的是**同一个文件**。
# 各记各的"损坏标记"迟早漏一个 ⇒ 标记记在**路径**上（`_io.is_quarantined`），
# 两个写者问的是同一个。下面第 3 条专门钉这件事。

def _agents_env(tmp_path, monkeypatch):
    from singularity.scheduler import _dispatch_crud as crud
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    return crud, tmp_path / ".qidian" / "agents_custom.json"


def test_读自定义_agent_损坏时降级成空(tmp_path, monkeypatch):
    crud, p = _agents_env(tmp_path, monkeypatch)
    raw = '{"any": [{"model": "m1"}], "坏":'
    p.write_text(raw, encoding="utf-8")
    assert crud._load_custom_agents() == {}
    assert p.read_text(encoding="utf-8") == raw, "原文件被动过了"
    assert (p.parent / "agents_custom.json.corrupt").exists()


def test_读坏之后_增删_agent_要拒写(tmp_path, monkeypatch):
    """**正题**：拿空 dict + 新条目整份写回去 = 用户配的所有 agent 全没。"""
    crud, p = _agents_env(tmp_path, monkeypatch)
    raw = '{"any": [{"model": "m1"}], "坏":'
    p.write_text(raw, encoding="utf-8")

    crud._load_custom_agents()                    # 置上损坏标记
    crud._save_custom_agents({"any": [{"model": "新的"}]})

    assert p.read_text(encoding="utf-8") == raw, "坏文件被整份重建了 —— 用户配的 agent 全没"


def test_两个写者共享同一个损坏标记(tmp_path, monkeypatch):
    """`_dispatch_crud` 读坏之后，**`skill_loader` 那边也不许写**。

    这正是"标记记在路径上、不记在模块上"的理由 —— 各记各的会漏。
    """
    from singularity.skills import skill_loader as SL
    crud, p = _agents_env(tmp_path, monkeypatch)
    monkeypatch.setattr(SL, "_qidian_dir", lambda: p.parent)
    raw = '{"any": [{"model": "m1"}], "_skills": {"any": {"m1": ["a"]}}, "坏":'
    p.write_text(raw, encoding="utf-8")

    crud._load_custom_agents()                    # 一个写者读了
    SL.set_agent_skills("any", "m1", ["b"])       # 另一个写者不许写

    assert p.read_text(encoding="utf-8") == raw, \
        "另一个写者没看见损坏标记，把整份盖了 —— 所有 skills 也一起没了"


def test_文件好的时候两个写者都照常(tmp_path, monkeypatch):
    """对照：正常路径不许被这套闸门挡住。"""
    from singularity.skills import skill_loader as SL
    crud, p = _agents_env(tmp_path, monkeypatch)
    monkeypatch.setattr(SL, "_qidian_dir", lambda: p.parent)

    crud._save_custom_agents({"any": [{"model": "m1"}]})
    SL.set_agent_skills("any", "m1", ["skill_a"])

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["any"][0]["model"] == "m1", "agent 那半丢了"
    assert data["_skills"]["any"]["m1"] == ["skill_a"], "skills 那半丢了"
