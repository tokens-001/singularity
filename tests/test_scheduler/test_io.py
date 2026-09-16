"""_io.py 单元测试 — try_parse_json / _format_kv 纯工具函数。"""

import pytest


class TestFormatKV:
    def test_list_value(self):
        """⚠️ **原来只断子串**（`"a.py" in r`）—— 外派⑬ 变异实测：把 list 分支删掉、
        值落进下面的引号分支，输出 `files = "['a.py', 'b.py']"`，两个子串**照样成立**。
        ⇒ 改成**精确等值**（同文件 `test_int_value` 就是这么写的）。
        变异：删掉 `isinstance(v, list)` 那个分支 → 红。"""
        from singularity.scheduler._io import _format_kv
        assert _format_kv("files", ["a.py", "b.py"]) == 'files = ["a.py", "b.py"]'

    def test_bool_true(self):
        from singularity.scheduler._io import _format_kv
        r = _format_kv("passed", True)
        assert "true" in r

    def test_bool_false(self):
        from singularity.scheduler._io import _format_kv
        r = _format_kv("passed", False)
        assert "false" in r

    def test_int_value(self):
        from singularity.scheduler._io import _format_kv
        assert _format_kv("count", 42) == "count = 42"

    def test_float_value(self):
        """同上：原来 `"3.14" in r` 分辨不出 `score = 3.14` 和 `score = "3.14"`
        （后者在 TOML 里是字符串，读回来类型就变了，而断言看不出来）。
        变异：删 `(int, float)` 分支 → 红。"""
        from singularity.scheduler._io import _format_kv
        assert _format_kv("score", 3.14) == "score = 3.14"

    def test_string_value(self):
        """同上：对 `hello` 来说，删掉 str 分支落 else 输出**逐字相同**。
        真正该钉的是 **str 分支独有的转义** —— 值里带引号/换行会把 TOML 写坏，
        而读侧 `except: return {}` 会把解析失败吞成空配置（用户的设置静默消失）。"""
        from singularity.scheduler._io import _format_kv
        assert _format_kv("name", "hello") == 'name = "hello"'
        assert _format_kv("note", 'say "hi"') == 'note = "say \\"hi\\""'
        assert _format_kv("note", "a\nb") == 'note = "a\\nb"'
        assert _format_kv("note", "a\tb") == 'note = "a\\tb"'


class TestTryParseJson:
    def test_empty_input(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json("")
        assert r.get("parse_error")

    def test_none_input(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json(None)
        assert r.get("parse_error")

    def test_code_fence_json(self):
        from singularity.scheduler._io import try_parse_json
        raw = '```json\n{"key": "value", "num": 42}\n```'
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["key"] == "value"
        assert r["num"] == 42

    def test_bare_json_object(self):
        from singularity.scheduler._io import try_parse_json
        raw = '前面有些文字 {"a": 1, "b": 2} 后面也有'
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["a"] == 1

    def test_trailing_comma_fix(self):
        from singularity.scheduler._io import try_parse_json
        raw = '{"name": "test", "value": 1,}'  # 尾逗号
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["name"] == "test"

    def test_trailing_comma_in_array(self):
        from singularity.scheduler._io import try_parse_json
        raw = '{"items": [1, 2, 3,]}'
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["items"] == [1, 2, 3]

    def test_no_json_found(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json("这只是一段普通文本")
        assert r.get("parse_error")

    def test_code_fence_without_json_tag(self):
        from singularity.scheduler._io import try_parse_json
        raw = '```\n{"ok": true}\n```'
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["ok"] is True

    def test_invalid_json_no_repair(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json("{invalid", try_repair=False)
        assert r.get("parse_error")

    def test_multiple_code_fences_uses_first_valid(self):
        from singularity.scheduler._io import try_parse_json
        raw = '```json\n{"first": 1}\n```\n```json\n{"second": 2}\n```'
        r = try_parse_json(raw)
        assert not r.get("parse_error")
        assert r["first"] == 1


class TestApplyJsonPatch:
    """apply_json_patch — 辩论轮增量修订: 补丁只改差异, 失败保留原方案。"""

    def _apply(self, original, patch):
        from singularity.scheduler._io import apply_json_patch
        import json
        return json.loads(apply_json_patch(original, patch))

    def test_replace_nested_list_field(self):
        original = '{"tasks": [{"id": "1", "description": "旧"}]}'
        patch = '[{"op": "replace", "path": "/tasks/0/description", "value": "新"}]'
        r = self._apply(original, patch)
        assert r["tasks"][0]["description"] == "新"

    def test_replace_top_level_field(self):
        original = '{"constraints": ["a", "b"]}'
        patch = '[{"op": "replace", "path": "/constraints", "value": ["a"]}]'
        assert self._apply(original, patch)["constraints"] == ["a"]

    def test_add_new_field(self):
        original = '{"tasks": []}'
        patch = '[{"op": "add", "path": "/notes", "value": "补充"}]'
        r = self._apply(original, patch)
        assert r["notes"] == "补充"
        assert r["tasks"] == []

    def test_remove_field(self):
        original = '{"tasks": [{"id": "1"}], "draft": true}'
        patch = '[{"op": "remove", "path": "/draft"}]'
        assert "draft" not in self._apply(original, patch)

    def test_patch_with_code_fence(self):
        original = '{"a": 1}'
        patch = '```json\n[{"op": "replace", "path": "/a", "value": 2}]\n```'
        assert self._apply(original, patch)["a"] == 2

    def test_invalid_patch_keeps_original(self):
        original = '{"a": 1}'
        assert self._apply(original, "这不是补丁")["a"] == 1

    def test_non_json_original_kept(self):
        from singularity.scheduler._io import apply_json_patch
        original = '这是一段非 JSON 文本'
        # 原方案非 JSON → 无法增量, 原样返回不抛异常
        assert apply_json_patch(original, '[{"op": "replace", "path": "/a", "value": 1}]') == original

    def test_bad_path_falls_back_to_original(self):
        original = '{"a": {"b": 1}}'
        patch = '[{"op": "replace", "path": "/x/y", "value": 2}]'
        r = self._apply(original, patch)
        assert r["a"]["b"] == 1


class TestAtomicWrite:
    """**原子性本身**的钉子 —— 外派⑬ 报：全仓没有一条用例分辨得出"原子写"和"裸写"
    （`test_json_concurrency.py::test_saved_file_is_valid_json` 变异实测：把
    `atomic_write_json` 换回裸 `write_text`，它照样绿 —— 单线程单次写本来就不会撕裂）。

    "裸写一次撕裂 = 那个文件从此拒写"（`_io` 里那句原话），所以这条不是洁癖。
    """

    def test_写一半崩了_原文件不许坏(self, tmp_path, monkeypatch):
        """模拟"tmp 写完、换名之前进程没了"：这一刻**正式文件必须还是旧的、完好的**。

        变异：把 `atomic_write_json` 换成裸 `path.write_text(...)` → 红。
        """
        import json as _json
        import os as _os
        from singularity.scheduler import _io

        p = tmp_path / "x.json"
        p.write_text('{"old": 1}', encoding="utf-8")

        def boom(*a, **k):
            raise OSError("模拟：换名那一刻进程没了")
        monkeypatch.setattr(_os, "replace", boom)

        with pytest.raises(OSError):
            _io.atomic_write_json(p, {"new": 2})

        assert _json.loads(p.read_text(encoding="utf-8")) == {"old": 1}, \
            "崩在换名之前，正式文件却被写坏了 —— 这不是原子写"

    def test_正常写完好内容也对(self, tmp_path):
        """对照：正常路径要真的写进去（别为了"原子"变成不写）。"""
        import json as _json
        from singularity.scheduler import _io
        p = tmp_path / "y.json"
        _io.atomic_write_json(p, {"a": [1, 2]})
        assert _json.loads(p.read_text(encoding="utf-8")) == {"a": [1, 2]}
        assert not list(tmp_path.glob("*.tmp")), f"留下 tmp 了：{list(tmp_path.iterdir())}"


class TestParseErrorFallbackTellsTheTruth:
    """解析失败时的兜底**必须说清自己是被截断的** —— 2026-09-17 真机。

    那天模型吐的 JSON 字符串里带裸换行 ⇒ 四层修复一层都不覆盖 ⇒ 兜底
    `{"raw_output": raw[:5000], "parse_error": True}`。前端只读
    `competitive_analysis.products` / `pitfalls` / `recommendation` 三个结构化字段，
    兜底那份**一个都没有** ⇒ **渲染成空框**；而且**连「这是被截断的」都看不出来**。
    用户原话：「**我怎么不能看报告**」。

    ⚠️ 全文**故意不存进这份 dict**：项目 json 在调度循环里每圈被 `list_all()` 读一遍，
    真机那份原文 2 万字、存进去就是给热路径加 ~45KB。全文在 `<id>.research.md`，
    由 `/api/projects/<id>/research-raw` 端出去 —— 所以这里要给出**够拼出那个地址**的线索。
    """

    @staticmethod
    def _bad(total: int) -> str:
        """一个**修不好**的输入：带围栏，且 JSON 字符串里有裸换行（非法控制字符）。"""
        head = '```json\n{"a": "x\ny", "pad": "'
        tail = '"}\n```'
        return head + ("k" * max(0, total - len(head) - len(tail))) + tail

    def test_兜底要说清原文多长_以及这是不是截断的(self):
        from singularity.scheduler._io import try_parse_json
        raw = self._bad(20000)
        r = try_parse_json(raw)
        assert r.get("parse_error"), f"这份输入本该解析失败（否则这条测的是别的东西）: {list(r)[:6]}"
        assert r.get("raw_chars") == len(raw), \
            f"没说清原文多长 ⇒ 前端以为手里那份就是全部: {r.get('raw_chars')} vs {len(raw)}"
        assert r.get("raw_truncated") is True, "被截断了却不说 ⇒ 用户以为报告就这么短"

    def test_短原文不谎报截断(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json(self._bad(200))       # 远小于 5000
        assert r.get("parse_error")
        assert r.get("raw_truncated") is False, \
            f"没截断却报截断 —— 「狼来了」会让真的截断没人信: {r.get('raw_truncated')}"

    def test_兜底给出取全文的地址线索(self):
        from singularity.scheduler._io import try_parse_json
        r = try_parse_json(self._bad(20000))
        assert r.get("raw_ref") == "research-raw", \
            f"没给全文的入口线索 ⇒ 界面上还是没地方看: {r.get('raw_ref')}"
