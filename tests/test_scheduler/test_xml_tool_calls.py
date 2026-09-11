"""XML 形式的工具调用要**接住**，不能静默当普通回答丢掉。

2026-09-12 探路2 的 T3 就栽在这儿：模型吐了 9599 字的
`<tool_calls><invoke name="write_file">…`，而平台只认 OpenAI 的 `tool_calls` →
整段被当"普通回答" → `changed_files` 为空 → 判"执行器未产出任何文件"失败。
**模型以为自己写成功了。**

讽刺的是 prompt 里早就写着"不要输出 `<invoke>` 块"——知道这个格式，
却只有禁令、没有解析器。禁令挡不住换了模型/换了心情的那一次。
"""
import json

import pytest

from singularity.scheduler.executors import openai_agent as oa

# ── DeepSeek 的 DSML：分隔符是**两个全角竖线**，不是 ASCII 的 `|` ──
# 用码点拼出来（上一个版本我按 ASCII 写正则，拿真实输出一跑才发现对不上）。
_B = "｜｜"
_O = "<" + _B + "DSML" + _B          # 开标签前缀
_C = "</" + _B + "DSML" + _B         # 闭标签前缀


def _dsml(path: str, content: str) -> str:
    return (
        _O + " tool_calls>\n"
        + _O + ' invoke name="write_file">\n'
        + _O + ' parameter name="path" string="true">' + path + "</" + _B + "DSML" + _B + " parameter>\n"
        + _O + ' parameter name="content" string="true">' + content + "</" + _B + "DSML" + _B + " parameter>\n"
        + "</" + _B + "DSML" + _B + " invoke>\n"
        + "</" + _B + "DSML" + _B + " tool_calls>"
    )


class TestDeepSeekDSML:
    """**这不是"模型不听话"，是模型换了厂商的协议。**

    探路2 的 T3 就是这么死的：DeepSeek 吐 DSML，平台只认 OpenAI 的 tool_calls →
    整段 9599 字被当"普通回答" → changed_files 为空 → 判"无文件改动"。
    """

    def test_real_shape_is_parsed(self):
        calls = oa._parse_xml_tool_calls(_dsml("txtstat.py", "print(1)\n"))
        assert calls is not None and len(calls) == 1
        fn = calls[0]["function"]
        assert fn["name"] == "write_file"
        args = json.loads(fn["arguments"])
        assert args["path"] == "txtstat.py"
        assert args["content"].strip() == "print(1)"

    def test_multiline_content_survives(self):
        body = "\n".join(f"line{i}" for i in range(50))
        args = json.loads(oa._parse_xml_tool_calls(_dsml("a.py", body))[0]["function"]["arguments"])
        assert args["content"].count("\n") == 49, "多行内容被切坏了等于写坏文件"

    def test_dsml_without_calls_wrapper_still_parsed(self):
        """有的输出省掉外层 tool_calls 包裹 —— 只要 invoke 在就要认。"""
        raw = _O + ' invoke name="ls"></' + _B + "DSML" + _B + " invoke>"
        calls = oa._parse_xml_tool_calls(raw)
        assert calls and calls[0]["function"]["name"] == "ls"

    def test_ascii_pipe_variant_also_parsed(self):
        """半角竖线那种变体也认 —— **宁可多认，别把活丢了**。"""
        raw = "<||DSML|| invoke " + 'name="ls">' + "</||DSML|| invoke>"
        calls = oa._parse_xml_tool_calls(raw)
        assert calls and calls[0]["function"]["name"] == "ls"

    def test_plain_text_still_returns_none(self):
        assert oa._parse_xml_tool_calls("我就写了点普通文字，没有任何标记") is None

    def test_mentioning_the_word_invoke_is_not_markup(self):
        """模型正常说一句"I will invoke the tool"不该被当工具调用。"""
        assert oa._parse_xml_tool_calls("接下来 I will invoke the tool 来完成") is None


# 与真实失败输出同形（原文 9599 字，这里取骨架）
REAL_SHAPE = (
    '<tool_calls>\n'
    '<invoke name="write_file">\n'
    '<parameter name="path">txtstat.py</parameter>\n'
    '<parameter name="content">### txtstat.py\n"""docstring"""\n</parameter>\n'
    '</invoke>\n'
    '</tool_calls>'
)


class TestParseXmlToolCalls:
    def test_real_shape_is_parsed(self):
        calls = oa._parse_xml_tool_calls(REAL_SHAPE)
        assert calls is not None and len(calls) == 1
        fn = calls[0]["function"]
        assert fn["name"] == "write_file"
        args = json.loads(fn["arguments"])
        assert args["path"] == "txtstat.py"
        assert args["content"].startswith("### txtstat.py")

    def test_multiple_invokes(self):
        content = ('<invoke name="read_file"><parameter name="path">a</parameter></invoke>'
                   '<invoke name="run_command"><parameter name="command">pytest</parameter></invoke>')
        calls = oa._parse_xml_tool_calls(content)
        assert [c["function"]["name"] for c in calls] == ["read_file", "run_command"]

    def test_missing_format_returns_none(self):
        """没这格式 → None。**和"空列表"含义不同**，调用方靠这个区分。"""
        assert oa._parse_xml_tool_calls("我就写了点普通文字") is None
        assert oa._parse_xml_tool_calls("") is None
        assert oa._parse_xml_tool_calls(None) is None

    def test_malformed_invoke_returns_empty_list(self):
        """有这格式但一条都解析不出来 → []，调用方据此**告警**（别再静默）。"""
        assert oa._parse_xml_tool_calls("<invoke name=") == []

    def test_no_parameter_tags_gives_empty_args(self):
        calls = oa._parse_xml_tool_calls('<invoke name="ls"></invoke>')
        assert json.loads(calls[0]["function"]["arguments"]) == {}

    def test_ids_are_unique(self):
        calls = oa._parse_xml_tool_calls('<invoke name="a"></invoke><invoke name="a"></invoke>')
        assert len({c["id"] for c in calls}) == 2

    def test_single_quotes_also_work(self):
        calls = oa._parse_xml_tool_calls(
            "<invoke name='read_file'><parameter name='path'>x.py</parameter></invoke>")
        assert json.loads(calls[0]["function"]["arguments"]) == {"path": "x.py"}

    def test_multiline_content_survives(self):
        """跨行内容要原样保留 —— 切坏了等于写坏文件。"""
        calls = oa._parse_xml_tool_calls(
            '<invoke name="write_file"><parameter name="content">a\nb\nc</parameter></invoke>')
        assert json.loads(calls[0]["function"]["arguments"])["content"] == "a\nb\nc"
