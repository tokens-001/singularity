"""handoff.py 单元测试 — _parse_handoff_block 纯字符串解析。"""

import pytest


class TestParseHandoffBlock:
    def test_full_block(self):
        from singularity.scheduler.handoff import _parse_handoff_block
        output = """代码实现完成。

[HANDOFF]
deliverable: src/auth.py
conclusion: 实现了登录功能
next: reviewer
human_confirm: false"""
        r = _parse_handoff_block(output)
        assert r["deliverable"] == "src/auth.py"
        assert r["conclusion"] == "实现了登录功能"
        assert r["next"] == "reviewer"
        assert r["human_confirm"] == "false"

    def test_no_handoff_block(self):
        from singularity.scheduler.handoff import _parse_handoff_block
        assert _parse_handoff_block("普通输出没有handoff") == {}

    def test_empty_output(self):
        from singularity.scheduler.handoff import _parse_handoff_block
        assert _parse_handoff_block("") == {}

    def test_none_output(self):
        from singularity.scheduler.handoff import _parse_handoff_block
        assert _parse_handoff_block(None) == {}

    def test_handoff_at_beginning(self):
        """HANDOFF 在开头也应能解析。"""
        from singularity.scheduler.handoff import _parse_handoff_block
        output = """[HANDOFF]
deliverable: README.md
conclusion: done
next: none"""
        r = _parse_handoff_block(output)
        assert r["deliverable"] == "README.md"

    def test_multiple_handoff_uses_last(self):
        """多个 [HANDOFF] → 取最后一个。"""
        from singularity.scheduler.handoff import _parse_handoff_block
        output = """[HANDOFF]
deliverable: old.py
conclusion: old
[HANDOFF]
deliverable: new.py
conclusion: new"""
        r = _parse_handoff_block(output)
        assert r["deliverable"] == "new.py"

    def test_ignores_section_headers(self):
        """以 [ 开头的行（如 [SECTION]）不当作键值对。"""
        from singularity.scheduler.handoff import _parse_handoff_block
        output = """[HANDOFF]
deliverable: code.py
[DEPENDS]
conclusion: passed"""
        r = _parse_handoff_block(output)
        assert "[DEPENDS]" not in r
        assert "deliverable" in r

    def test_line_without_colon_skipped(self):
        from singularity.scheduler.handoff import _parse_handoff_block
        output = """[HANDOFF]
deliverable: output.py
这是一行没有冒号的注释
conclusion: done"""
        r = _parse_handoff_block(output)
        assert "deliverable" in r
        assert "conclusion" in r
