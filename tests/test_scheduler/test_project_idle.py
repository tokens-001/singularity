"""`idle_seconds` —— 详情接口上那个「多久没动」（2026-09-28）。

由来：`round-20260928i` 的项目 **02:14 之后完全静止 47 分钟**，盘上/界面上一个字都没有。
顶栏那行进度照样写着「4 done + 1 卡住 + 4 等待」——**沉默看起来和在干活一模一样**。

⚠️ **这个数是一个事实，不是一个判断**：一个任务正常跑满 900 秒**期间不产生 `lineage`**，
这个数照样涨。要判"卡住"得配上"有没有任务在跑"（判断在 `Chat.tsx` 的 `idleCopy` 里，
页面才同时看得到两样）。**别在别处把它直接读成"它卡住了"。**

🔴 **本文件钉住两条最容易写错的**：
  ① 判据是 `lineage` 末条，**不是 `updated_at`** —— 后者每次 `save()` 都动，
     而节流 30 秒的 tick 检查会定期 save ⇒ 那个数量的是"有人碰过"、不是"有进展"。
  ② **接线**：`project_detail` 真的把它带出去（只测 `_idle_seconds` 证明不了页面拿得到）。
"""
from __future__ import annotations

import time

import pytest

from singularity.scheduler import config
from singularity.scheduler import _api_projects as api
from singularity.scheduler import project as proj_mod


def _isolate(tmp_path, monkeypatch):
    """⚠️ **先隔离再 save** —— 不隔离就是往生产的 `.qidian/` 里写（本仓栽过，见
    `docs/防御模式.md`：拿真仓库跑探针 = 在生产数据上跑）。照 `test_project_integration` 那份抄的。"""
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projs")
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")


def _proj(**kw):
    p = proj_mod.ProjectState(id="idle-1", name="n")
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_取的是最后一次lineage而不是updated_at():
    """**判据必须是 `lineage`** —— 构造一个"很久没进展、但刚刚被碰过"的项目。

    `updated_at` 是 `time.time()`（刚刚），`lineage` 末条是 600 秒前。
    取错信号 ⇒ 这个用例给出 ≈0，而正确答案是 ≈600。
    """
    now = time.time()
    p = _proj(lineage=[{"action": "a", "ts": now - 600}], created_at=now - 9999,
              updated_at=now)
    assert api._idle_seconds(p) == pytest.approx(600, abs=5)


def test_取的是最新的那条不是第一条():
    """lineage 是**追加**的 ⇒ 末条最新。取首条会把"刚动过"报成"很久没动"（假红）。"""
    now = time.time()
    p = _proj(lineage=[{"action": "old", "ts": now - 9999},
                       {"action": "new", "ts": now - 30}], created_at=now - 99999)
    assert api._idle_seconds(p) == pytest.approx(30, abs=5)


def test_没有lineage就退回创建时间():
    """刚建的项目一条 lineage 都没有 —— 退回 `created_at`，不是 0。"""
    now = time.time()
    p = _proj(lineage=[], created_at=now - 120)
    assert api._idle_seconds(p) == pytest.approx(120, abs=5)


def test_两个都没有就如实返回0不许编():
    """连 `created_at` 都没有 ⇒ **说不出"多久"** ⇒ 返回 0（0 又会因为 <300 而不显示）。

    ⚠️ 这条守的是**不编一个数**：这里若兜底成 `9999`，界面上就会冒出一个
    凭空的"2.8 小时没有任何动作"。
    """
    p = _proj(lineage=[], created_at=None)
    assert api._idle_seconds(p) == 0.0


def test_lineage里有坏行不许炸():
    """盘上的 JSON 什么形状都可能（缺 ts / ts 是字符串）—— 坏行跳过，不许抛。"""
    now = time.time()
    p = _proj(lineage=[{"action": "no-ts"}, {"ts": "字符串"}, None,
                       {"action": "good", "ts": now - 45}], created_at=now - 999)
    assert api._idle_seconds(p) == pytest.approx(45, abs=5)


def test_未来的时间戳不许算出负数():
    """机器睡过一觉、或者两个进程的钟不一样 ⇒ ts 可能比 now 大。
    负数会被前端当成"不足 5 分钟"而静默不显示 —— 那正好是**该显示的时候不显示**。"""
    p = _proj(lineage=[{"action": "future", "ts": time.time() + 600}])
    assert api._idle_seconds(p) == 0.0


def test_详情接口带上了这个数(tmp_path, monkeypatch):
    """**接线**：删掉 `project_detail` 里那行 `d["idle_seconds"] = …` → 本用例红。

    只测 `_idle_seconds` 是没用的 —— 那正是"函数对 ≠ 接线通"
    （`Chat.tsx` 读的是**详情接口**，不是这个模块函数）。
    """
    _isolate(tmp_path, monkeypatch)
    p = _proj(lineage=[{"action": "a", "ts": time.time() - 900}])
    proj_mod.save(p)

    detail, code = api.project_detail(p.id)
    assert code == 200
    assert "idle_seconds" in detail, "详情里没有 idle_seconds —— 接线断了"
    assert detail["idle_seconds"] == pytest.approx(900, abs=5)


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
