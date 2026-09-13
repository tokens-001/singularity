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


# ═══════════════════════════════════════════════════════════════
# ⑤ 第四个接入点：settings.json（项目成品根目录那份）
# ═══════════════════════════════════════════════════════════════
# 它坏了而写侧不拦：先读成 `{}`、再只写 `{"projects_root": ...}` 回去
# ⇒ **settings.json 里别的用户设置全没了**，而文件看起来完好。
# 另外读侧静默回落默认值 ⇒ "我配过"这件事**无声地消失**。

def _settings_env(tmp_path, monkeypatch):
    from singularity.scheduler import project as P
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    return P, tmp_path / ".qidian" / "settings.json"


def test_设置文件坏了_写侧拒写并且抛(tmp_path, monkeypatch):
    """**正题**：读不出来就不许写回整份 —— 否则别的用户设置被一起抹掉。"""
    P, p = _settings_env(tmp_path, monkeypatch)
    raw = '{"projects_root": "/tmp/my-projects", "别的设置": "要保住", "坏":'
    p.write_text(raw, encoding="utf-8")

    P.get_projects_root()                        # 读一次 → 置上损坏标记
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        P.set_projects_root("/tmp/new")

    assert p.read_text(encoding="utf-8") == raw, \
        "坏文件被整份重建了 —— 别的用户设置全没，而文件名一模一样"
    assert (p.parent / "settings.json.corrupt").exists(), "没留备份"


def test_设置文件好的时候照常写且保住别的键(tmp_path, monkeypatch):
    """对照：正常路径要把**别的设置保住**（这是"读改写"该有的样子）。"""
    P, p = _settings_env(tmp_path, monkeypatch)
    p.write_text(json.dumps({"projects_root": "/tmp/a", "别的设置": "要保住"}),
                 encoding="utf-8")

    P.set_projects_root("/tmp/b")

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["projects_root"].endswith("/tmp/b"), data
    assert data["别的设置"] == "要保住", "读改写把别的键吃了"


# ═══════════════════════════════════════════════════════════════
# ⑥ 第五、六处接入点：自定义模型表（两份实现）
# ═══════════════════════════════════════════════════════════════
# `api_store.load_custom_models` / `save_custom_model` 和
# `model_registry._load_custom` / `_save_custom` 是**同一件事的两份实现**，
# 两边都是"读坏落空表 → 读改写整份写回" —— 会把用户扫出来/手配过的模型全盖掉。

def test_自定义模型表坏了_api_store_不许整份重建(tmp_path, monkeypatch):
    from singularity.scheduler import api_store as A
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p = A._custom_models_path()
    raw = '{"扫出来的模型": {"id": "扫出来的模型"}, "坏":'
    p.write_text(raw, encoding="utf-8")

    assert A.load_custom_models() == {}, "读侧该降级成空表"
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        A.save_custom_model("新模型", "厂商")

    assert p.read_text(encoding="utf-8") == raw, \
        "坏文件被整份重建了 —— 用户扫出来/手配的模型全没"


def test_自定义模型表坏了_model_registry_不许整份重建(tmp_path, monkeypatch):
    from singularity.scheduler import model_registry as MR
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    p = MR._custom_path()
    raw = '{"m1": {"id": "m1"}, "坏":'
    p.write_text(raw, encoding="utf-8")

    assert MR._load_custom() == {}
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        MR._save_custom({})

    assert p.read_text(encoding="utf-8") == raw, "坏文件被整份重建了"


def test_自定义模型表好的时候照常读写(tmp_path, monkeypatch):
    """对照：两份实现都要保住正常路径。"""
    from singularity.scheduler import api_store as A
    from singularity.scheduler import model_registry as MR
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()

    A.save_custom_model("我的模型", "厂商", display="显示名")
    assert A.load_custom_models()["我的模型"]["display"] == "显示名"

    MR._save_custom({})
    assert MR._load_custom() == {}


# ═══════════════════════════════════════════════════════════════
# ⑦ 最后三处：fusion 配置（只读）+ 范围纪律表（读改写）
# ═══════════════════════════════════════════════════════════════

def test_范围纪律表坏了_不许整份重建(tmp_path, monkeypatch):
    """`record()` 是读改写 —— 拿空表 + 这一次的计数写回 = **攒了很久的历史全没**。"""
    from singularity.scheduler import _model_discipline as MD
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    p = MD._path()
    raw = '{"deepseek": {"violations": 3, "audits": 10}, "坏":'
    p.write_text(raw, encoding="utf-8")

    assert MD.load() == {}, "读侧该降级成空表"
    assert MD.record("glm", 2) is False, "损坏期间该拒写（返回 False，不抛）"

    assert p.read_text(encoding="utf-8") == raw, "坏文件被整份重建了 —— 历史全没"
    assert any("record_skip" in str(a) for a in warns), f"拒写了却没出声：{warns}"


def test_范围纪律表好的时候照常记(tmp_path, monkeypatch):
    from singularity.scheduler import _model_discipline as MD
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    assert MD.record("m1", 2) is True
    assert MD.record("m1", 1) is True
    d = MD.load()["m1"]
    assert d["violations"] == 3 and d["audits"] == 2, d


def test_fusion_配置坏了降级成空表但出声(tmp_path, monkeypatch):
    """只读路径：降级是对的（没有写回），但**不能一声不吭**。"""
    from singularity.scheduler import execution_judge as EJ
    monkeypatch.setattr(config, "SCHEDULER_DIR", tmp_path)
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    (tmp_path / "fusion.toml").write_text("这不是 = = toml", encoding="utf-8")

    assert EJ._load_fusion_config() == {}
    assert warns, "融合配置被无声忽略了"
    assert (tmp_path / "fusion.toml.corrupt").exists()


def test_范围纪律的只读那份坏了也出声(tmp_path, monkeypatch):
    """`execution_judge._model_discipline` 是同一份文件的**另一个读者**（只读）。"""
    from singularity.scheduler import execution_judge as EJ
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    (tmp_path / ".qidian" / "model_discipline.json").write_text("{坏", encoding="utf-8")

    assert EJ._model_discipline() == {}
    assert warns, "读坏了却没出声"


# ═══════════════════════════════════════════════════════════════
# ⑧ 角色覆盖表（只读，但"无声回退"是它的问题）+ tmp 清扫的新旧之分
# ═══════════════════════════════════════════════════════════════

def test_角色覆盖坏了不能只留一行_log(tmp_path, monkeypatch):
    """**"我配过的角色没了"必须让人看得见** —— 原来只写一行 `logging.warning` 就 return。

    那行 log 在调度器日志里；而**用户看的是界面上的告警面板**。
    ⇒ 走 S1 那套：隔离 + `witness`（进告警面板）+ `.corrupt` 备份。
    """
    from singularity.scheduler import roles as R
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(a))
    p = tmp_path / ".qidian" / "roles_custom.json"
    raw = '{"my_role": {"name": "我的角色"}, "坏":'
    p.write_text(raw, encoding="utf-8")

    R._apply_overrides()            # 不许抛

    assert warns, "回退到出厂角色了却只在日志里说一声 —— 用户在告警面板上看不到"
    assert (tmp_path / ".qidian" / "roles_custom.json.corrupt").exists(), "没留备份"


# ═══════════════════════════════════════════════════════════════
# ⑧ **第一次触碰**（2026-09-14 外派⑦ 抓到 —— 上面那批测试全体漏掉的那一格）
# ═══════════════════════════════════════════════════════════════
# 🔴 **上面每一条"写侧拒写"测试都先手动读了一次**（`MD.load()` / `load_custom_models()` /
# `get_projects_root()` / `_load_custom_agents()`），注释里明写"置上损坏标记"。
# 那一行**正是缺陷的前提** —— 于是"第一次触碰"这一格被整套测试漏掉了。
#
# 原来的写侧判据是 `is_quarantined(path)`，而**那个标记要有人先读过才有**；
# 进程刚起来、坏文件还没人读时，是**写侧自己那次读**才把标记置上的 —— 太晚了。
# 实测后果（⑦ 复现、我复核）：`model_discipline.json` 只剩新记的那一条、
# `settings.json` 的 `user_theme` 等键全没 —— **文件还在、名字没变，内容换成了
# "刚重建的空表 + 这一条"**。
#
# ⇒ 这组测试**一律不预读**，直接调写侧。判据是「我这次读出来的是什么」。

def _干净环境(tmp_path, monkeypatch):
    """把 `.qidian` 隔离到 tmp，并把"已隔离"标记清成进程刚起来的样子。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(_io, "_QUARANTINED", set())
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: warns.append(str(a)))
    return warns


def test_第一次触碰_范围纪律表不许整份重建(tmp_path, monkeypatch):
    """进程刚起来、坏文件还没人读过 ⇒ 写侧自己那次读不能成为"先读后写"的例外。"""
    from singularity.scheduler import _model_discipline as MD
    warns = _干净环境(tmp_path, monkeypatch)
    p = MD._path()
    raw = '{"deepseek": {"violations": 3, "audits": 10}, "坏":'
    p.write_text(raw, encoding="utf-8")

    assert MD.record("glm", 2) is False, "第一次触碰就照写了 —— 历史全没"

    assert p.read_text(encoding="utf-8") == raw, "坏文件被整份重建了"
    assert any("record_skip" in w for w in warns), f"拒写了却没出声：{warns}"


def test_第一次触碰_设置文件不许整份重建(tmp_path, monkeypatch):
    from singularity.scheduler import project as P
    warns = _干净环境(tmp_path, monkeypatch)
    p = P._settings_path()
    raw = '{"projects_root": "/tmp/a", "user_theme": "dark", "坏":'
    p.write_text(raw, encoding="utf-8")

    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        P.set_projects_root("/tmp/new")

    assert p.read_text(encoding="utf-8") == raw, "user_theme 等别的设置全没"
    assert warns, "拒写了却没出声"


def test_第一次触碰_自定义模型表不许整份重建(tmp_path, monkeypatch):
    from singularity.scheduler import api_store as A
    warns = _干净环境(tmp_path, monkeypatch)
    p = A._custom_models_path()
    raw = '{"扫出来的模型": {"id": "扫出来的模型"}, "坏":'
    p.write_text(raw, encoding="utf-8")

    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        A.save_custom_model("新模型", "厂商")

    assert p.read_text(encoding="utf-8") == raw, "用户扫出来/手配的模型全没"
    assert warns, "拒写了却没出声"


def test_第一次触碰_agent技能绑定不许整份重建(tmp_path, monkeypatch):
    """这个文件有**两个写者** —— 更该测。"""
    from singularity.skills import skill_loader as S
    warns = _干净环境(tmp_path, monkeypatch)
    p = S._qidian_dir() / "agents_custom.json"
    raw = '{"_skills": {"any": {"m1": ["code-review"]}}, "坏":'
    p.write_text(raw, encoding="utf-8")

    S.set_agent_skills("any", "m2", ["ddd"])       # 不预读，直接写

    assert p.read_text(encoding="utf-8") == raw, "所有 agent 配置和 skill 绑定全没"
    assert any("agents_custom" in w for w in warns), f"拒写了却没出声：{warns}"


def test_第一次触碰的对照组_文件好的时候四处都照常写(tmp_path, monkeypatch):
    """**别把闸门修成"什么都不动"** —— 文件好时，四条写路径都要真的写下去。"""
    from singularity.scheduler import _model_discipline as MD, api_store as A, project as P
    from singularity.skills import skill_loader as S
    _干净环境(tmp_path, monkeypatch)
    (tmp_path / ".qidian").mkdir(exist_ok=True)

    assert MD.record("glm", 2) is True
    assert json.loads(MD._path().read_text(encoding="utf-8"))["glm"]["violations"] == 2

    P.set_projects_root("/tmp/new-root")
    assert json.loads(P._settings_path().read_text(encoding="utf-8"))["projects_root"].endswith(
        "/tmp/new-root")

    A.save_custom_model("我的模型", "厂商", display="显示名")
    assert A.load_custom_models()["我的模型"]["display"] == "显示名"

    S.set_agent_skills("any", "m1", ["code-review"])
    data = json.loads((tmp_path / ".qidian" / "agents_custom.json").read_text(encoding="utf-8"))
    assert data["_skills"]["any"]["m1"] == ["code-review"]


def test_第一次触碰的对照组_改完的文件仍保住别的键(tmp_path, monkeypatch):
    """文件**好**的时候，读改写必须把别的键带过去（"读在前"没把数据弄丢）。"""
    from singularity.scheduler import _model_discipline as MD
    _干净环境(tmp_path, monkeypatch)
    p = MD._path()
    p.write_text(json.dumps({"deepseek": {"violations": 3, "audits": 10}}), encoding="utf-8")

    assert MD.record("glm", 2) is True

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["deepseek"] == {"violations": 3, "audits": 10}, "老模型的历史被吃掉了"
    assert data["glm"]["violations"] == 2


# ═══════════════════════════════════════════════════════════════
# ⑨ S1 覆盖的那几个文件，写 JSON **必须**走 `atomic_write_json`
# ═══════════════════════════════════════════════════════════════
# 为什么只有这几个文件受这条约束（别扩到全仓）：这套新语义下，
# **裸写一次撕裂 → 文件损坏 → 那个文件从此拒写到重启**。
# 非原子写把代价从"丢一轮"放大成"停摆"，所以诱因不能留在原地。
# 而那 8 个不在 S1 名单里的文件（`witness` / `codegraph` / `snapshot` …）没这个放大效应，
# 它们各自的写法有自己的理由 —— **这条闸门不是"全仓禁止裸写"**。

_S1读写改写的文件 = [
    "scheduler/_process_ledger.py",
    "scheduler/api_store.py",          # 主库 + models_custom.json
    "scheduler/project.py",            # settings.json
    "scheduler/_model_discipline.py",
    "scheduler/model_registry.py",
    "scheduler/_dispatch_crud.py",     # agents_custom.json 的另一个写者
    "skills/skill_loader.py",          # agents_custom.json
]


def test_S1那族里不许裸写JSON():
    """07-14 外派⑦ 点名"改了一半"：同族的 `_dispatch_crud`/`model_registry` 换了原子写，
    另外几处还在裸写 —— 等于把诱因留在原地、还加重了后果。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "singularity"
    犯 = []
    for rel in _S1读写改写的文件:
        p = root / rel
        assert p.exists(), f"文件挪了，闸门该跟着改：{rel}"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "write_text(json.dumps" in line and not line.lstrip().startswith("#"):
                犯.append(f"{rel}:{i}")
    assert not 犯, (
        "S1 覆盖的文件里有裸 JSON 写 —— 撕裂一次那个文件就**从此拒写到重启**：\n  "
        + "\n  ".join(犯)
        + "\n改用 `_io.atomic_write_json(path, data)`（缩进不是 2 就传 `indent=`）。")


def test_tmp_清扫只清陈旧的(tmp_path, monkeypatch):
    """**正在写的那个进程的 tmp 不许被清掉** —— 它只存在几毫秒，但那一刻是活的。"""
    import os as _os
    import time as _time
    p = tmp_path / "x.json"
    stale = tmp_path / "x.json.99999.tmp"; stale.write_text("崩溃残留", encoding="utf-8")
    _os.utime(stale, (_time.time() - 9999, _time.time() - 9999))
    fresh = tmp_path / "x.json.88888.tmp"; fresh.write_text("别的进程正在写", encoding="utf-8")

    _io.atomic_write_json(p, {"a": 1})

    assert not stale.exists(), "陈旧残留没清掉"
    assert fresh.exists(), "把正在写的那个进程的 tmp 清了 —— 会踩掉它的数据"
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
