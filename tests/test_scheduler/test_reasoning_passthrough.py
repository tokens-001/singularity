"""历史里的 `reasoning_content`：判据是**这条消息带不带 `tool_calls`**，不是这一轮带不带 `tools`。

2026-09-19 深夜真机解出来的那条 400（`.qidian/llm_400_unknown.jsonl`，task `1789829637698`）：

- 11 条消息里第 **[9]** 条是凶手：`assistant` + **带 1 个 `tool_calls`** + **`reasoning_content` 键不在**
  （对照 [3]/[6]：同样带 tool_calls，键在、有内容 ⇒ 没事）；
- **同一份请求体回放真端点**：原样 **400**，只给它补 `reasoning_content: ""` 就 **200**。

它怎么来的（三步，缺一不可）：

1. `_exec` 那两处 `tools = []`（`测试通过即停` / `已达最大工具调用轮次`）**中途撤掉工具**，
   而历史里**已经躺着带 tool_calls 的 assistant 消息**；
2. 模型这一轮没工具可用、**还是吐了 XML 形式的工具调用** ⇒ 被 `_parse_xml_tool_calls`
   捞成合成 tool_call（id `xml_0`）；
3. 追加历史时 `_assistant_msg_for_history(msg, tools)` 拿**这一轮的 `tools`** 判 ⇒
   `if tools:` 为假 ⇒ **把这个消息的 `reasoning_content` 整个 pop 掉**。

⇒ 下一条请求就是「带 `tool_calls` 却没有 `reasoning_content`」⇒ 400 ⇒ **整条任务挂掉**。

⚠️ 上一轮（09-19 夜）六个探针形状全 200，不是"这条 400 不存在"，是**那六个都没有这个组合**。
**一次"没红"的实验，先问它有没有发力。**
"""
import json

from singularity.scheduler.executors import openai_agent as oa

_B = "｜｜"
_O = "<" + _B + "DSML" + _B


def _dsml(path: str) -> str:
    return (
        _O + " tool_calls>\n"
        + _O + ' invoke name="write_file">\n'
        + _O + ' parameter name="path" string="true">' + path + "</" + _B + "DSML" + _B + " parameter>\n"
        + _O + ' parameter name="content" string="true">x</' + _B + "DSML" + _B + " parameter>\n"
        + "</" + _B + "DSML" + _B + " invoke>\n"
        + "</" + _B + "DSML" + _B + " tool_calls>"
    )


_A_CALL = {"id": "call_1", "type": "function",
           "function": {"name": "run_command", "arguments": '{"command": "echo ok"}'}}


class TestPerMessageRule:
    """规则本身：带 tool_calls 的消息**永远**留 reasoning（模型没给就补空串）。"""

    def test_带tool_calls且带reasoning_原样保留(self):
        msg = {"role": "assistant", "content": "", "reasoning_content": "想过了",
               "tool_calls": [_A_CALL]}
        out = oa._assistant_msg_for_history(msg, [])
        assert out["reasoning_content"] == "想过了", "撤掉工具那一轮，历史里的 reasoning 被抹了"

    def test_带tool_calls但模型没给_补空串而不是删键(self):
        """🔴 这条是那次 400 的**精确形状**：键在不在，比内容是什么要紧。

        空串 ≠ 没有 —— DeepSeek 只在字段**整个缺失**时报 400，回 `""` 就没事
        （真端点回放实测：补 `""` 后 200）。
        """
        msg = {"role": "assistant", "content": "正文", "tool_calls": [_A_CALL]}
        out = oa._assistant_msg_for_history(msg, [])
        assert "reasoning_content" in out, "键被整个丢了 ⇒ 下一条请求必然 400"
        assert out["reasoning_content"] == ""

    def test_无tool_calls且不带tools_照旧剥掉(self):
        """零回归：`no_tools` 委员会那条路（架构/规划）**保持剥掉**。

        对 Kimi/GLM 维持原行为 —— 它们不吃这个字段。
        """
        msg = {"role": "assistant", "content": "答案", "reasoning_content": "想过"}
        assert "reasoning_content" not in oa._assistant_msg_for_history(msg, [])

    def test_不带tools但带tool_calls_这一条才是漏网的那个组合(self):
        """把判据写死在用例名里：**这一轮有没有 tools 不是判据**。"""
        msg = {"role": "assistant", "content": "x", "reasoning_content": "想过",
               "tool_calls": [_A_CALL]}
        assert oa._assistant_msg_for_history(msg, [])["reasoning_content"] == "想过"


class TestFixFiresAlert:
    """🔵 **修复自己出声** —— 没有这声，"没再冒 400"永远说不清是修好了还是没走到。

    真机实测两种都见过：09-19 21:33 那次捞回后 1 秒就 400；09-20 00:00 那两次捞回后什么
    都没发生 ⇒ **光看"没 400"分不出"修好了"和"这一轮没走到那个形状"**。
    """

    def _alerts(self, monkeypatch):
        got = []
        from singularity.scheduler import witness as w
        monkeypatch.setattr(w, "warn", lambda scope, msg, **kw: got.append((scope, msg)))
        return got

    def test_不带tools但带tool_calls_要出声(self, monkeypatch):
        got = self._alerts(monkeypatch)
        oa._assistant_msg_for_history({"role": "assistant", "tool_calls": [_A_CALL]}, tools=[])
        assert [m for _, m in got if m.startswith("reasoning_kept_for_tool_call_without_tools")], \
            f"修复兜住那个形状时没出声 ⇒ 下次真机没法判「修好了」：{got}"

    def test_带tools时不出声_那不是修复在起作用(self, monkeypatch):
        """带 tools 时旧判据本来就留 reasoning —— 那不算修复生效，别刷屏。"""
        got = self._alerts(monkeypatch)
        oa._assistant_msg_for_history({"role": "assistant", "tool_calls": [_A_CALL]},
                                      tools=[{"type": "function"}])
        assert not got, f"不该出声：{got}"

    def test_无tool_calls时不出声(self, monkeypatch):
        got = self._alerts(monkeypatch)
        oa._assistant_msg_for_history({"role": "assistant", "content": "答案",
                                       "reasoning_content": "想过"}, tools=[])
        assert not got, f"不该出声：{got}"


class TestWiring:
    """🔴 **接线**：光规则对没用，得**真的落到发给 API 的那个 body 里**。

    变异：把 `_assistant_msg_for_history` 改回 `if tools: …` ⇒ 本用例红。
    """

    def _make(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "k")
        cfg = {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x", "max_turns": 6}
        ex = oa.OpenAIAgentExecutor(cfg, "任务", "tid", cwd=str(tmp_path))
        # 让"测试通过即停"这条路被走到 —— 本用例要的不是测试真绿，是**工具被撤掉那一刻**
        monkeypatch.setattr(ex, "_tests_green", lambda cmd, res: True)
        return ex

    def test_撤掉工具后捞回的XML调用_必须带reasoning(self, tmp_path, monkeypatch):
        ex = self._make(tmp_path, monkeypatch)
        sent = []

        def fake_api(body):
            # ⚠️ **必须当场快照**：`body["messages"]` 是**那份 list 的引用**，
            # 循环后面还会往里 append —— 直接存 body，三个快照会看到同一份终态，
            # 断言就成了"对着最后一份自己看自己"（这条我第一版就写错过）。
            sent.append({"tools": body.get("tools"),
                         "messages": [dict(m) for m in body["messages"]]})
            n = len(sent)
            if n == 1:
                # 正常一轮：带 reasoning 的 tool_calls
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "", "reasoning_content": "第一轮想过",
                    "tool_calls": [_A_CALL]}}], "usage": {}}
            if n == 2:
                # 工具已被撤掉这一轮 —— 模型还是吐了 XML 形式的工具调用（**无 reasoning**）
                return {"choices": [{"message": {
                    "role": "assistant", "content": _dsml("out.txt")}}], "usage": {}}
            return {"choices": [{"message": {
                "role": "assistant", "content": "做完了"}}], "usage": {}}

        monkeypatch.setattr(ex, "_api_call", fake_api)
        ex.run()

        # 前提先钉住：第 2 条请求真的**不带工具**，历史里真的躺着那条捞回来的消息。
        # （前提没成立 → 下面的断言是空过，那正是"变异打偏了"那种假绿。）
        assert len(sent) >= 3, f"没跑到第三条请求，测不到接线：{len(sent)}"
        assert sent[1]["tools"] == [], "第 2 条请求没撤工具 ⇒ 这个用例根本没走到那条路"
        recovered = [m for m in sent[2]["messages"]
                     if m.get("role") == "assistant" and m.get("tool_calls")]
        assert recovered, "XML 捞回的工具调用没进历史 ⇒ 用例没测到该测的东西"

        offenders = [m for m in sent[2]["messages"]
                     if m.get("role") == "assistant"
                     and m.get("tool_calls")
                     and "reasoning_content" not in m]
        assert not offenders, (
            "历史里有「带 tool_calls 却缺 reasoning_content」的消息 —— "
            f"真端点回放这就是那条 400：{json.dumps(offenders, ensure_ascii=False)[:200]}")
