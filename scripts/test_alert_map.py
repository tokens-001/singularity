#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alert_map.py 的自测。跑法：`.venv/bin/python scripts/test_alert_map.py`（或 pytest）。

**喂的是造的假源码，不是真 `src/`** —— 考的是"能不能认出发出点"这套判据本身，
而真 `src/` 每天都在变，拿它当夹具的话，测试会因为**无关的改动**红。
（本仓栽过这个：被测的东西读这台机器 / 读真仓库 ⇒ 本机绿只在"它不读"时才等于绿。）

每条都对着一个**会真实误导人**的失败模式：
  · 咬 ①：把 key 写死的调用点，必须认出来，而且标"全串"（强证据）。
  · 咬 ②：f-string 拼的 key（`f"xml_tool_calls_{scope}:"`）**字面量根本不存在**，
    只按全串 grep 会判"没有发出点" —— 回退要能找到，而且只能标"弱"。
  · 咬 ③：退到几个字母才命中时，命中一片文件 —— 那不是弱证据是噪声，
    必须返回空（"查不到"），**不许把一个甩锅用的文件列表交出去**。
  · 咬 ④：`frozenset({...})` 那种**登记处**不是发出点。删掉 `_LITERAL_CTORS`
    这条就红 —— 这是"白名单长得跟调用点一样"那个坑。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_map as AM  # noqa: E402

EMITTER = '''
def go(tid):
    warnings.warn(f"xml_tool_calls_{tid}:run")
    warnings.warn("drain_dep_blocked: 依赖永远不来", key="drain_dep_blocked")
'''

# 登记处：名字跟调用点长得一模一样，但不是发出点
REGISTRY = '''
_CRITICAL = frozenset({
    "observer_stalled_task",
    "drain_dep_blocked",
})
'''

# 到处都在提这个词，但**不在任何调用点里** ⇒ 不是发出点。
# ⚠️ 故意**不**放 `logger.info("observer_stalled_task: ...")` 这种：它跟真告警
# **在文本上一模一样**（告警 key 就是从"msg 里第一个 `标识符:`"推出来的），
# 工具分不出来，也不该假装分得出来。考能分清的那部分。
MENTIONS_OUTSIDE_CALLS = [
    'X = "observer_stalled_task"',
    'Y = {"k": "observer_stalled_task"}',
    'Z = ["observer_stalled_task", "observer_stalled_task"]',
    'W = "observer_stalled_task"',
    'V = "observer_stalled_task"',
]


def _tree(files: dict[str, str]) -> dict[str, list[str]]:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for name, body in files.items():
            (root / name).write_text(body, encoding="utf-8")
        return AM.call_segments(root)


class TestFindEmitters(unittest.TestCase):
    def test_01_写死的_key_认得出且算强证据(self):
        calls = _tree({"a.py": EMITTER})
        files, exact = AM.find_emitters("drain_dep_blocked", calls)
        self.assertEqual(files, ["a.py"])
        self.assertTrue(exact, "写死的 key 应该是全串命中（强证据）")

    def test_02_fstring_拼的_key_回退能找到但只能算弱(self):
        calls = _tree({"a.py": EMITTER})
        files, exact = AM.find_emitters("xml_tool_calls_recovered", calls)
        self.assertEqual(files, ["a.py"], "f-string 前缀要能回退命中")
        self.assertFalse(exact, "回退命中不能标成全串 —— 那是把弱证据当强证据")

    def test_03_退到没判别力就不许猜(self):
        """5 个文件，各自**在调用点里**含有 `observer`，但没有一个含更长的前缀。

        ⚠️ 这些文件必须**真的有调用点** —— 否则 `call_segments` 直接不收它们，
        断言会因为"输入本来就是空的"而绿，**根本走不到要考的那个分支**
        （2026-09-26 第一版就是这么写的：把 `_MAX_WEAK_HITS` 那段删掉它照样绿）。
        """
        noisy = {f"n{i}.py": f'def f{i}():\n    print("observer")\n' for i in range(5)}
        calls = _tree(noisy)
        self.assertEqual(len(calls), 5, "5 个文件都该被收进来，否则这条测试在考空气")
        files, exact = AM.find_emitters("observer_stalled_task", calls)
        self.assertEqual(files, [], "命中一片文件时必须是空 —— 指错地方比说不知道坏")
        self.assertFalse(exact)

    def test_04_登记处不算发出点(self):
        calls = _tree({"registry.py": REGISTRY})
        files, _ = AM.find_emitters("observer_stalled_task", calls)
        self.assertEqual(files, [], "frozenset 白名单不是发出点")

    def test_05_不在调用点里提到的不算发出点(self):
        calls = _tree({f"m{i}.py": body for i, body in enumerate(MENTIONS_OUTSIDE_CALLS)})
        self.assertEqual(calls, {}, "这些文件里根本没有调用点，不该被收进来")
        files, _ = AM.find_emitters("observer_stalled_task", calls)
        self.assertEqual(files, [], "光提到不算发 —— 不然表会指到一堆无关文件上")


class TestReadCounts(unittest.TestCase):
    def test_key_取冒号前那段_半行跳过(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "alerts.jsonl"
            p.write_text(
                '{"key": "xml_tool_calls_recovered:1:run"}\n'
                '{"key": "drain_dep_blocked"}\n'
                '{"key": ""}\n'
                '这行是坏的\n',
                encoding="utf-8",
            )
            self.assertEqual(AM.read_counts(p), Counter({
                "xml_tool_calls_recovered": 1, "drain_dep_blocked": 1,
            }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
