"""委员会那一波：**窗口不许比被等的东西短**，丢了席不许一声不出。

出处：Qoder CN 第二轮 #5（`docs/Qoder-审查-20260925-第二轮.md`），**用户 09-26 拍板"抬阈值"**。

**病**：`_WAVE_TIMEOUT` 硬写 **300 秒**，而席位单次调用能跑到 `_EXEC_BUDGET`（默认 **810**）
—— **窗口比被等的东西短** ⇒ 慢的那一席结果被丢、`member_usage` 里也没有它
⇒ **这笔钱花掉了、账上没有**（委员会是全场最贵的一段）。
它还给 09-22 那个标着"原因未定"的 `committee_partial: 2/3 缺` 提供了一个不依赖"题难不难"的解释。

**另外两个洞（同一段代码，顺带）**：被丢的那几席、和抛异常的席，原来**一个字都不说** ——
从外面看和"委员会本来就只配了几席"长得一模一样。

钉四件事：
  ① 波窗口**同源于**席位预算（不是另一个字面量）；② 被丢的席出声；③ 抛异常的席出声；
  ④ **对照组**：一切正常时**不许**报这两条（常亮的告警会淹掉真的）。
"""
import time

import pytest

from singularity.scheduler import _dispatch_exec as de
from singularity.scheduler import execution_judge as ej
from singularity.scheduler.executors import openai_agent as oa


@pytest.fixture
def warns(monkeypatch):
    """把 `witness.warn` 收集起来 —— 断言"出声了没"必须看这个，别看日志文本。"""
    hits = []
    monkeypatch.setattr(de.witness, "warn",
                        lambda scope, msg, **kw: hits.append((scope, msg, kw)))
    return hits


def _committee(monkeypatch, member):
    monkeypatch.setattr(de, "_run_no_tools", member)
    monkeypatch.setattr(ej, "_is_architecture_task", lambda t: True)
    monkeypatch.setattr(ej, "fuse_architecture_v2", lambda *a, **k: '{"architecture":"fused"}')
    return de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                                  [{"model": "fast"}, {"model": "slow"}])


# ═══════════════════════════════════════════════════════════════
# ① 同源：窗口必须 ≥ 席位自己的预算
# ═══════════════════════════════════════════════════════════════

def test_波窗口不短于席位预算():
    """变异：把 `_WAVE_TIMEOUT` 改回字面量 `"300"` ⇒ 本条红。

    ⚠️ 这一条**在旧代码上就是红的**（300 < 810）—— 它钉的正是那个"窗口比被等的东西短"。
    """
    assert de._EXEC_BUDGET is oa._EXEC_BUDGET, \
        "窗口和席位预算各算各的 —— 本文件刚为'两个写字面量迟早会漂开'栽过一次"
    assert de._WAVE_TIMEOUT >= oa._EXEC_BUDGET, \
        (f"收集窗口 {de._WAVE_TIMEOUT:.0f}s < 席位单次能跑的 {oa._EXEC_BUDGET:.0f}s "
         f"⇒ 慢的那一席结果被丢、账上也漏")


# ═══════════════════════════════════════════════════════════════
# ②③ 丢了 / 挂了，都要出声
# ═══════════════════════════════════════════════════════════════

def test_被丢的席要出声(monkeypatch, warns):
    """变异：删掉 `if not_done:` 那段 `witness.warn` ⇒ 本条红。"""
    def member(agent_cfg, prompt, tag, level, baseline_ref="", cwd=""):
        if agent_cfg.get("model") == "slow":
            time.sleep(3)          # 拖过窗口
        return ('{"architecture":"x"}', 100, 1.0)

    monkeypatch.setattr(de, "_WAVE_TIMEOUT", 0.3)
    _committee(monkeypatch, member)

    hit = [m for _s, m, kw in warns if kw.get("key") == "committee_dropped"]
    assert hit, f"一席被丢却一个字不说 —— 从外面看和'本来就只配了两席'一样：{warns}"
    assert "slow" in hit[0], f"得说清丢的是哪一家：{hit[0]}"


def test_席位抛异常要出声(monkeypatch, warns):
    """变异：把 `except Exception as e:` 改回裸 `except Exception: pass` ⇒ 本条红。"""
    def member(agent_cfg, prompt, tag, level, baseline_ref="", cwd=""):
        if agent_cfg.get("model") == "slow":
            raise RuntimeError("这家 400 了")
        return ('{"architecture":"x"}', 100, 1.0)

    _committee(monkeypatch, member)

    hit = [m for _s, m, kw in warns if kw.get("key") == "committee_seat_failed"]
    assert hit, f"一席挂了却一个字不说：{warns}"
    assert "slow" in hit[0] and "RuntimeError" in hit[0], hit[0]


# ═══════════════════════════════════════════════════════════════
# ④ 对照组：一切正常时不许报（常亮的告警会淹掉真的）
# ═══════════════════════════════════════════════════════════════

def test_对照_全部正常时不报这两条(monkeypatch, warns):
    """变异：让那两条告警**无条件**发（不看条件）⇒ 本条红。"""
    def member(agent_cfg, prompt, tag, level, baseline_ref="", cwd=""):
        return ('{"architecture":"x"}', 100, 1.0)

    _committee(monkeypatch, member)

    keys = {kw.get("key") for _s, _m, kw in warns}
    assert "committee_dropped" not in keys, f"两席都回来了还报'被丢'：{warns}"
    assert "committee_seat_failed" not in keys, f"两席都好好的还报'挂了'：{warns}"


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
