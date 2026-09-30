"""`scripts/wiring_check.py` 的自检 —— 造一个小源码树，钉住"写/读"两条正则。

来历：2026-09-30 一天两次栽在**有生产者 ≠ 送到了消费者**上
（`unverified` 塞进去了没人读 · `run_check` 写 `stdout` 而我读 `output`）。
这个脚本是那把尺。**尺自己出过一次错**：第一版只认 `"k":` 这种字典键写法，
而代码实际写成 kwarg（`unverified=[...]`）⇒ 两边都数成 0 ⇒ **被当成文案静默跳过**
—— 静默跳过正是要抓的那种洞。所以这条钉在这儿。
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "wiring_check", ROOT / "scripts" / "wiring_check.py")
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)


def _src(tmp_path: Path, body: str, monkeypatch) -> None:
    d = tmp_path / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(wc, "SRC", d)


def test_写了没人读要被看见(tmp_path, monkeypatch):
    _src(tmp_path, 'def f():\n    return {"solo_key": 1}\n', monkeypatch)
    w, r = wc.scan("solo_key")
    assert len(w) == 1, f"写点没认出来：{w}"
    assert r == [], f"不该有读点：{r}"


def test_kwarg_写法也要算作写(tmp_path, monkeypatch):
    """🔴 回测逼出来的那条：`unverified=[...]` 是 kwarg，不是字典键。"""
    _src(tmp_path, "def f():\n    return R(unverified=['x'])\n", monkeypatch)
    w, _ = wc.scan("unverified")
    assert len(w) == 1, "kwarg 写法没被算成写点 ⇒ 会被静默跳过（正是要抓的洞）"


def test_属性写法要算作读(tmp_path, monkeypatch):
    """`validation.unverified` 是属性读取，`["unverified"]` 那类正则认不出它。"""
    _src(tmp_path, "def f(v):\n    if v.unverified:\n        pass\n", monkeypatch)
    _, r = wc.scan("unverified")
    assert len(r) == 1, f"属性读没被认出来：{r}"


def test_写了没人读要判硬的(tmp_path, monkeypatch):
    """端到端那一跳：真的跑一遍 main，退出码必须是 1。"""
    import io
    import sys as _sys

    _src(tmp_path, 'def f():\n    return {"solo_key": 1}\n', monkeypatch)
    monkeypatch.setattr(wc, "new_keys", lambda rev: ["solo_key"])
    monkeypatch.setattr(_sys, "argv", ["wiring_check.py"])
    buf = io.StringIO()
    monkeypatch.setattr(_sys, "stdout", buf)
    assert wc.main() == 1, "写了没人读没被判硬 ⇒ 退出码不会报警"
    assert "solo_key" in buf.getvalue()
