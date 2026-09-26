"""回归门**压根没装**时，不许判成"gate失败"。

出处：Qoder CN 第二轮第 1 条（`docs/Qoder-审查-20260925-第二轮.md`），逐条核过成立。

**病**：`_run_gate()` 把"脚本不存在"折成 `passed=False`，而调用方那句
`if not g.get("passed"):` 把它读成「gate失败 ⇒ retry/rollback」——
对一个**从来没有 `eval.py`** 的仓（本仓就是：`data/knowledge/scripts/` 下只有
`validate.py`，`git ls-files` 全历史也没有过 `eval.py`），任何碰到核心文件名
（`config.py`/`core.py`/`search.py`…）的任务**必定死**，理由是一句和任务无关的
"eval.py 不存在"。⇒ **一场保证会发生的假失败。**

**钉三件事**（缺一条，这个修复就是半截的）：
  ① 门没装 ⇒ **不判失败**，照常走完后面的检查（LLM 验收 / 硬规则 / 人审）；
  ② 但**必须出声** —— "没跑门"和"跑过了"不能长得一样（记进 `unverified`）；
  ③ **别修过头**：门**装了但没过**照旧 `gate失败`/`rollback`；门**跑了且通过**
     不许乱扣"未执行"的帽子。
"""
import json
import sys

import pytest

from singularity.scheduler import config as C
from singularity.scheduler import validator as V


# ═══════════════════════════════════════════════════════════════
# 夹具：真跑 validate()，只桩掉会花钱/碰外部的两处
# ═══════════════════════════════════════════════════════════════

def _run_validate(monkeypatch, changed, gate_required=False, tmp_path=None):
    """跑真的 `V.validate()`。

    `_run_gate` **不桩** —— 这一条测的就是它。`_run_validate` 桩掉（它是子进程调
    `validate.py`，与本条无关，桩掉只是为了快和确定）。
    返回 `(report, validate 被调到的次数)` —— **那个计数是接线判据**：
    如果哪一版又把早期的 `return report` 加回来，"继续往下走"这半条就断了。
    """
    calls = []
    monkeypatch.setattr(V, "_run_validate",
                        lambda c: (calls.append(c), {"verdict": "通过", "verdict_reason": ""})[1])
    rep = V.validate(candidate="改完了", gate_required=gate_required, task_type="bugfix",
                     changed_files=changed, snap=None, turn=1, max_turns=2,
                     cwd=str(tmp_path) if tmp_path else None)
    return rep, calls


def _fake_eval(tmp_path, payload):
    """造一个真的 eval.py（好让 `_run_gate` 走"脚本在"那条路）。"""
    p = tmp_path / "eval.py"
    p.write_text(f"import json,sys\nprint(json.dumps({payload!r}))\n", encoding="utf-8")
    return p


# ═══════════════════════════════════════════════════════════════
# ① 门没装 ⇒ 不判失败
# ═══════════════════════════════════════════════════════════════

def test_门脚本不存在时_改了核心文件名也不许判失败(monkeypatch, tmp_path):
    """变异：把 `validate()` 里 `if not g.get("ran", True):` 那一段删掉（= 退回旧行为）⇒ 本条红。"""
    monkeypatch.setattr(C, "EVAL_SCRIPT", tmp_path / "根本没有这个文件.py")
    rep, calls = _run_validate(monkeypatch, changed=["someproj/config.py"], tmp_path=tmp_path)

    assert rep.verdict != "gate失败", \
        f"门没装却报 gate失败 —— 这是一场保证会发生的假失败：{rep.unverified}"
    assert rep.action != "rollback", "最后一轮会被判 rollback，任务必死"
    assert rep.action == "pass", f"本该继续走完剩下的检查：{rep.action}"
    assert rep.gate_passed is None, "门没跑过，不该有任何'通过/不通过'的取值"
    assert calls, "早期的 return 又回来了 —— '没跑门就继续往下走'这半条断了"


def test_门没装要出声(monkeypatch, tmp_path):
    """变异：删掉那句 `report.unverified.append(f"回归门未执行…")` ⇒ 本条红。

    **命门是这一条**：不判失败是对的，但**静默地不判失败**就成了"无人质疑的假通过"
    —— 本仓另一侧的老毛病。两侧都错，要的是"如实说、然后接着走"。
    """
    monkeypatch.setattr(C, "EVAL_SCRIPT", tmp_path / "根本没有这个文件.py")
    rep, _ = _run_validate(monkeypatch, changed=["someproj/config.py"], tmp_path=tmp_path)

    hit = [u for u in rep.unverified if "回归门未执行" in u]
    assert hit, f"门没跑一个字都没说 —— 和'跑过了'长得一模一样：{rep.unverified}"
    assert "eval.py 不存在" in hit[0], f"得说清为什么没跑：{hit[0]}"


def test_没改核心文件时门本就不该跑_不许乱扣帽子(monkeypatch, tmp_path):
    """**边界**：分类器明说不用跑门、也没命中核心文件名 ⇒ 那不是"未执行"，别记。"""
    monkeypatch.setattr(C, "EVAL_SCRIPT", tmp_path / "根本没有这个文件.py")
    rep, _ = _run_validate(monkeypatch, changed=["someproj/normal_app.py"], tmp_path=tmp_path)
    assert not any("回归门未执行" in u for u in rep.unverified), rep.unverified


# ═══════════════════════════════════════════════════════════════
# ② 别修过头：门真的装了，行为一个字节都不许变
# ═══════════════════════════════════════════════════════════════

def test_门跑了但没过_照旧判失败(monkeypatch, tmp_path):
    """**这次修改最危险的边** —— 很容易顺手把整扇门拆了。门在、跑砸了，就该死。"""
    monkeypatch.setattr(C, "EVAL_SCRIPT",
                        _fake_eval(tmp_path, {"gate": {"passed": False, "message": "退化"}}))
    rep, _ = _run_validate(monkeypatch, changed=["someproj/config.py"], tmp_path=tmp_path)

    assert rep.verdict == "gate失败", rep.unverified
    assert rep.action == "retry", "第 1 轮、max_turns=2 ⇒ 该 retry 不是 rollback"
    assert rep.gate_passed is False
    assert any("gate failed" in u for u in rep.unverified), rep.unverified


def test_门跑到底了该rollback还是rollback(monkeypatch, tmp_path):
    """最后一轮（turn >= max_turns）撞上真失败 ⇒ 仍旧 rollback。"""
    monkeypatch.setattr(C, "EVAL_SCRIPT",
                        _fake_eval(tmp_path, {"gate": {"passed": False, "message": "退化"}}))
    monkeypatch.setattr(V, "_run_validate", lambda c: {"verdict": "通过", "verdict_reason": ""})
    rep = V.validate(candidate="改完了", gate_required=True, task_type="bugfix",
                     changed_files=["someproj/config.py"], snap=None, turn=2, max_turns=2,
                     cwd=str(tmp_path))
    assert rep.action == "rollback", rep.unverified


def test_门跑了且通过_不记那句未执行(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "EVAL_SCRIPT",
                        _fake_eval(tmp_path, {"gate": {"passed": True, "message": "ok"}}))
    rep, calls = _run_validate(monkeypatch, changed=["someproj/config.py"], tmp_path=tmp_path)

    assert rep.gate_passed is True
    assert not any("回归门未执行" in u for u in rep.unverified), \
        f"门真跑了还扣'没跑'的帽子 —— 假红换真红：{rep.unverified}"
    assert calls


# ═══════════════════════════════════════════════════════════════
# ③ `_run_gate` 自己的三态
# ═══════════════════════════════════════════════════════════════

def test_run_gate_三态(monkeypatch, tmp_path):
    """`ran` 只有两种取法：**脚本不在** ⇒ False（唯一来源）；脚本在、跑砸了 ⇒ True（事故）。"""
    monkeypatch.setattr(C, "EVAL_SCRIPT", tmp_path / "没有.py")
    g = V._run_gate()
    assert g["ran"] is False and g["passed"] is False, g

    monkeypatch.setattr(C, "EVAL_SCRIPT", _fake_eval(tmp_path, {"gate": {"passed": False}}))
    g = V._run_gate()
    assert g["ran"] is True and g["passed"] is False, \
        "脚本在、只是没过 —— 这是事故，不能跟'没装门'混成一个（那正是本条的病根）"

    # 脚本在、但输出是垃圾（不是 JSON）⇒ 也算"跑了、没过"，不许翻成"没跑"
    (tmp_path / "eval.py").write_text("print('我不是 JSON')\n", encoding="utf-8")
    g = V._run_gate()
    assert g["ran"] is True and g["passed"] is False, g


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
