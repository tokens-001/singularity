"""「路由未判定」—— 分类没判出来时，`gate_required=False` 是**折出来的**，不是分类器的意见。

出处：`docs/判据错位审计-20260918.md` C 组还剩的两条（`router.py` / `_observer_tools.py`）。
原文建议"兜底 gate=True"，**逐行核过之后否掉**（会造出假失败，见 `router.RouteResult`
的 docstring）⇒ 2026-09-20 用户拍板走**第三态**：

  不阻断流程 · 门的行为**一个字不改**（仍靠文件级兜底 `_gate_check_by_files`）·
  但「**我们不知道**」这件事从此**可数、可查**。

钉三件事：
  ① 判据本身：分类挂了 ⇒ `classified=False` **且出声**
  ② **接线的两头**：生产侧（observer）真把 `route_gate_unknown` 写进任务；
     消费侧（validator）真在"没跑门"时记进 `unverified`
  ③ 边界：**门真跑了就不记那句** —— 那次的决定是**有依据的**，别乱扣帽子
"""
import json

import pytest

from singularity.scheduler import router as R
from singularity.scheduler import validator as V


# ═══════════════════════════════════════════════════════════════
# ① 判据：分类挂了 ⇒ 带上"没判出来"，并且出声
# ═══════════════════════════════════════════════════════════════

def test_默认是判过的():
    assert R.RouteResult().classified is True


def test_分类挂了要带上未判定并且出声(monkeypatch):
    """变异：把 `RouteResult(classified=False)` 改回 `RouteResult()` ⇒ 本条红。

    ⚠️ **必须喂一个"配好了的分类器"**才能走到调用的那一步 —— 不然 `_llm_classify`
    会在"没有可用 agent / 没有 key"那两个早退上就返回，**压根测不到异常分支**
    （第一版就是这么绿的：拿到的是配置缺失那条路）。
    """
    from singularity.scheduler import dispatcher as disp_mod
    from singularity.scheduler import witness
    hits = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **kw: hits.append((scope, msg, kw)))
    monkeypatch.setattr(disp_mod, "load_agents", lambda: {"any": [
        {"model": "m", "entry": "http://x/chat/completions", "api_key_env": "TEST_KEY"}]})
    monkeypatch.setattr(disp_mod, "agent_api_available", lambda a: True)
    monkeypatch.setenv("TEST_KEY", "k")

    import httpx
    def boom(*a, **k):
        raise RuntimeError("网络炸了")
    monkeypatch.setattr(httpx, "post", boom)

    R._CLASSIFY_CACHE.clear()
    r = R._llm_classify("一个足够长的任务描述，长到会真的去调分类器的那种")
    assert r.classified is False, "分类挂了却报成'判过了' —— 又回到'没判'冒充'判了'"
    assert r.gate_required is False, "折还是照旧折成 False（折 True 会造出假失败）"
    assert any(kw.get("key") == "classify_failed" for _s, _m, kw in hits), \
        f"没出声 —— 这件事必须可数：{hits}"


def test_压根没配分类器不算未判定(monkeypatch):
    """**边界（有意如此，别当漏了）**：没 agent / 没 key ⇒ `RouteResult()`，`classified=True`。

    为什么**不**标成"未判定"：那是**稳定的配置事实**（装了就没分类器），
    标上去等于**每个任务**都挂一句 → 直接变成"常亮的假红"，而真出事那次就没人看了
    （本仓"常亮的假红换掉一个真红"那条）。它该在**系统层报一次**，不是逐任务标。
    """
    from singularity.scheduler import dispatcher as disp_mod
    monkeypatch.setattr(disp_mod, "load_agents", lambda: {"any": []})
    R._CLASSIFY_CACHE.clear()
    r = R._llm_classify("一个足够长的任务描述，长到会真的去调分类器的那种")
    assert r.classified is True, "配置缺失被当成'这次没判出来' —— 会变成每任务一条的常亮红"
    assert r.task_type == "default"


def test_解析不出回复也算没判出来(monkeypatch):
    """**另一条路也要算**：HTTP 200 了、但回复里没有能解析的 JSON。

    ⚠️ 这一条目前**不算**未判定（`_parse_classify_reply` 回的是 `RouteResult()`）——
    写在这儿是**记录现状**，不是断言它是对的。要改先想清楚"空回复"和"判过说 default"
    能不能分开，别一把改。
    """
    r = R._parse_classify_reply("模型说了一堆人话，没有 JSON")
    assert r.classified is True, "现状就是如此（这是记录，不是认可）"


# ═══════════════════════════════════════════════════════════════
# ② 消费侧：`validate` 在"没跑门"时要把这件事记进 unverified
# ═══════════════════════════════════════════════════════════════

def _validate(monkeypatch, gate_unknown, changed, gate_required=False):
    monkeypatch.setattr(V, "_run_gate", lambda: {"passed": True, "message": "ok"})
    monkeypatch.setattr(V, "_run_validate", lambda c: {"verdict": "通过", "verdict_reason": ""})
    monkeypatch.setattr(V, "post_execution_hook", lambda *a, **k: {}, raising=False)
    return V.validate(candidate="改完了", gate_required=gate_required, task_type="bugfix",
                      changed_files=changed, snap=None, turn=1, max_turns=2,
                      gate_unknown=gate_unknown)


def test_未判定且门没跑_要记一笔(monkeypatch):
    """变异：把 `if gate_unknown and not _gate_ran:` 那段删掉 ⇒ 本条红。"""
    rep = _validate(monkeypatch, gate_unknown=True, changed=["src/app/normal.py"])
    assert any("路由未判定" in u for u in rep.unverified), \
        f"没留痕 —— 这个决定是在'不知道'的情况下做的，和真判过的长得一样：{rep.unverified}"


def test_未判定但文件级兜底命中了_门真跑了_就不记(monkeypatch):
    """**边界（别乱扣帽子）**：改了核心引擎文件 ⇒ 文件级兜底把门拉起来了 ⇒
    那次的决定**是有依据的**，不该再标"未判定"。"""
    rep = _validate(monkeypatch, gate_unknown=True, changed=["src/core.py"])
    assert not any("路由未判定" in u for u in rep.unverified), rep.unverified
    assert rep.gate_passed is True, "门应该真跑过"


def test_判过了且没跑门_不该记(monkeypatch):
    """**命门**：分类器**明确说**不用跑门 ⇒ 那不是"未判定"，别记。"""
    rep = _validate(monkeypatch, gate_unknown=False, changed=["src/app/normal.py"])
    assert not any("路由未判定" in u for u in rep.unverified), rep.unverified


# ═══════════════════════════════════════════════════════════════
# ③ 接线的另一头：任务上真有这个字段（不然 `transition` 会当未知键丢掉）
# ═══════════════════════════════════════════════════════════════

def test_任务真的有这个字段(tmp_path, monkeypatch):
    """🔴 **这条防的是本仓踩过一年的那个坑**：`route_role` 曾经字段不存在，
    `transition(**kwargs)` 用 `hasattr` 一判就静默丢弃，写入方以为设上了、
    读取方永远拿到默认值 —— 两边都"正常"（`_apply_attrs` 的 docstring 里记着）。

    所以这里既验字段在，也**验写进去读得回来**（而不是只验 `hasattr`）。
    """
    from singularity.scheduler import config, tracker
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    config.ensure_dirs()
    t = tracker.create("未判定接线用任务")
    assert hasattr(t, "route_gate_unknown"), "字段不在 —— transition 会当未知键丢掉"
    tracker.transition(t.id, tracker.TaskStatus.PENDING, route_gate_unknown=True)
    assert tracker.read_task(t.id).route_gate_unknown is True, "写进去读不回来"


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
