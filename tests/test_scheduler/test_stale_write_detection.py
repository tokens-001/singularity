"""`save()` 要在写盘前问一句「盘上那份是不是比我手里这份新」（2026-09-18）。

`save()` 是 `to_dict()` **整份覆盖写** —— 没有字段级合并、没有版本号。
所以手里这份比盘上旧的时候一写，就把他这期间的改动整份抹掉；而被抹掉的那一方
常常是**人的动作**（`owner_confirm` 里点的批准）。09-17 观察者那条 🔴、
09-18 `_start_background` 那条，都是这个根。

⚠️ 判据是**只报不改**，所以测试钉的是"**有没有出声**"，不是"有没有拦下来"。

⚠️ 第二个用例是这条判据的**命门**：`save()` 全仓有几百个调用点，**误报会把
witness 糊成筛子**（本仓踩过，"告警只报出事了不报为什么"那条就是后果）。
所以"自己连着存两次不吭声"必须钉住。
"""
import time

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import witness


def _mk():
    return proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[],
        supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
    )


def _capture(monkeypatch):
    warns = []
    monkeypatch.setattr(witness, "warn",
                        lambda scope, msg, **k: warns.append(msg))
    return warns


def test_手里这份比盘上旧时_存盘要出声(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = _mk()
    proj_mod.save(p)                    # 盘上先有一份

    stale = proj_mod.load(p.id)         # 有人 load 了一份
    assert stale is not None
    proj_mod.save(p)                    # 期间盘上又被写过一次

    # 把手里这份的令牌拨回 10 秒前 —— 模拟"这份快照是 10 秒前拿的"
    stale.updated_at = time.time() - 10

    warns = _capture(monkeypatch)
    proj_mod.save(stale)

    assert any("stale_write" in w for w in warns), (
        f"手里这份比盘上旧，写下去会抹掉这期间别人的改动，却一声不吭: {warns}")


def test_自己连着存两次_不许误报(tmp_path, monkeypatch):
    """命门：`save()` 调用点几百个，误报会把告警糊成筛子。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = _mk()
    proj_mod.save(p)
    warns = _capture(monkeypatch)

    proj_mod.save(p)
    proj_mod.save(p)

    assert warns == [], f"自己存自己还报，这一族告警立刻失效: {warns}"


def test_首次保存没得比_不出声(tmp_path, monkeypatch):
    """盘上还没有文件时不该报 —— 那是"首次保存"，不是"丢更新"。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = _mk()
    warns = _capture(monkeypatch)

    proj_mod.save(p)

    assert warns == [], f"首次保存就报，全是噪声: {warns}"
