"""_io.py 单元测试 — try_parse_json / _format_kv 纯工具函数。"""

import pytest


class TestFormatKV:
    def test_list_value(self):
        from singularity.scheduler._io import _format_kv
        r = _format_kv("files", ["a.py", "b.py"])
        assert "a.py" in r
        assert "files =" in r

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
        from singularity.scheduler._io import _format_kv
        r = _format_kv("score", 3.14)
        assert "3.14" in r and "score" in r

    def test_string_value(self):
        from singularity.scheduler._io import _format_kv
        r = _format_kv("name", "hello")
        assert 'hello' in r and '"' in r


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
