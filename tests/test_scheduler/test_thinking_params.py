"""思考参数透传。

设计取舍：不维护「哪个模型支持哪个思考参数」的能力表 —— 那张表一定会过期
（GLM-5.2 能关思考、5.3 强制开；kimi-k2.6 能关、k2.7 强制开）。所以配了就传，
不支持由 400 容错自动摘掉。
"""
import pytest

from singularity.scheduler import _dispatch_crud as crud
from singularity.scheduler.executors.openai_agent import (
    _apply_think_params, _drop_rejected_think_param)


# ── 透传 ────────────────────────────────────────────────

def test_apply_passes_only_whitelisted_keys():
    body = {}
    _apply_think_params(body, {"model": "m", "max_tokens": 100,
                               "thinking": {"type": "disabled"}, "reasoning_effort": "low"})
    assert body == {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}


def test_apply_skips_previously_rejected_keys():
    """body 每轮重建，不记住被拒的键就会每轮再撞一次 400。"""
    body = {}
    _apply_think_params(body, {"thinking": {"type": "disabled"}}, skip={"thinking"})
    assert body == {}


# ── 400 容错 ────────────────────────────────────────────

def test_drop_removes_only_the_named_param():
    body = {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
    assert _drop_rejected_think_param(body, 'HTTP 400: unknown field "thinking"') == "thinking"
    assert body == {"reasoning_effort": "low"}


def test_drop_ignores_unrelated_400():
    body = {"thinking": {"type": "disabled"}}
    assert _drop_rejected_think_param(body, "HTTP 400: bad temperature") == ""
    assert body == {"thinking": {"type": "disabled"}}   # 没误删


# ── request_template 局部更新 ───────────────────────────

def test_merge_tmpl_keeps_existing_fields():
    """只传 thinking 时不能把 model/max_tokens 冲掉 —— 漏写 model 会让运行时
    setdefault 失效（key 已存在），body 里没有 model，请求直接废掉。"""
    base = {"request_template": {"model": "deepseek-v4-flash", "max_tokens": 131072}}
    got = crud._merge_tmpl(base, {"request_template": {"thinking": {"type": "disabled"}}})
    assert got["request_template"] == {
        "model": "deepseek-v4-flash", "max_tokens": 131072,
        "thinking": {"type": "disabled"}}


def test_merge_tmpl_fills_floor_when_agent_had_none():
    """出厂 agent 没有 request_template，只写 thinking 时必须补上 model/额度。"""
    base = {"model": "glm-5.3-flash"}
    got = crud._merge_tmpl(base, {"request_template": {"reasoning_effort": "low"}})
    tmpl = got["request_template"]
    assert tmpl["model"] == "glm-5.3-flash"
    assert tmpl["max_tokens"] > 0
    assert tmpl["reasoning_effort"] == "low"


def test_merge_tmpl_none_deletes_key():
    """前端「恢复默认」传 null —— merge 语义本身删不掉键，不给这条路就回不去。"""
    base = {"model": "m", "request_template": {"model": "m", "max_tokens": 100,
                                              "reasoning_effort": "low"}}
    got = crud._merge_tmpl(base, {"request_template": {"reasoning_effort": None}})
    assert "reasoning_effort" not in got["request_template"]
    assert got["request_template"]["model"] == "m"      # 其他键没被误删


def test_agent_update_then_list_roundtrip(tmp_path, monkeypatch):
    """端到端：PUT 写进去 → GET 读得回来。前端下拉的回显就靠这条链。"""
    import json
    from singularity.scheduler import config, _api_admin
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    (tmp_path / ".qidian" / "agents_custom.json").write_text(json.dumps(
        {"any": [{"model": "test-model-xyz", "type": "openai-agent", "max_turns": 5}]}))

    _, code = _api_admin.agent_update("any", "test-model-xyz",
                                      {"request_template": {"reasoning_effort": "low"}})
    assert code == 200
    listed, _ = _api_admin.agent_list()
    a = next(x for x in listed["any"] if x["model"] == "test-model-xyz")
    assert a["request_template"]["reasoning_effort"] == "low"
    assert a["request_template"]["model"] == "test-model-xyz"   # floor 补上了


def test_merge_tmpl_noop_without_tmpl_update():
    updates = {"max_turns": 3}
    assert crud._merge_tmpl({}, updates) == updates
    assert "request_template" not in crud._merge_tmpl({}, updates)


# ═══════════════════════════════════════════════════════════════
# `reasoning_content` 的回传（2026-09-15 真机 + DeepSeek 官方文档）
# ═══════════════════════════════════════════════════════════════
# 真机症状：`any 层所有 agent 均失败: deepseek-flash: 空输出
#   [HTTP 400: The `reasoning_content` in the thinking mode must be passed back to the API.]`
# 原来对 `reasoning_content` 是**一刀切剥掉**（注释："API输入不接受此字段"——旧经验）。
#
# 官方规则（thinking-mode 文档原文）：
#   · **带 `tools` 参数** → 必须**原样回传**（含没有工具调用的轮次）——「must be fully
#     passed back to the API in all subsequent requests」；不回传 = 400；
#   · **不带 `tools`** → 不必回传，传了也会被忽略。
#
# 🔴 **但"这次请求带不带 tools"不是完整判据**（2026-09-19 深夜，真机请求体解出来的）：
# 两处 `tools = []`（`测试通过即停` / `已达最大工具调用轮次`）会**中途撤掉工具**，
# 而历史里**已经躺着带 `tool_calls` 的 assistant 消息**；模型这一轮没工具可用、
# 还是吐了 XML 形式的工具调用 ⇒ 被捞成合成 tool_call ⇒ 按旧判据**把 reasoning 剥掉**了。
# 下一条请求就是「带 `tool_calls` 却没有 `reasoning_content`」→ **400**。
# 现场 + 回放：`.qidian/llm_400_unknown.jsonl`（task `1789829637698`）；
# **同一份请求体原样发真端点是 400，只给那条补 `reasoning_content: ""` 就 200**。
# ⇒ **完整判据是"这条消息带不带 `tool_calls`"**，不是"这一轮带不带 tools"。
# （同一条规则流式那侧早就写着 —— 见 `openai_agent.py` 里 `msg.setdefault("reasoning_content", "")`。）

from singularity.scheduler.executors.openai_agent import _assistant_msg_for_history

_ASSISTANT = {"role": "assistant", "content": "", "reasoning_content": "我先看看文件…",
              "tool_calls": [{"id": "t1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]}


def test_带工具时必须回传_reasoning_content():
    """⚠️ 这条不回传就是 **HTTP 400**，而且换 agent 也没用（同一个剥法）⇒ 整个任务挂掉。"""
    out = _assistant_msg_for_history(dict(_ASSISTANT), tools=[{"type": "function"}])
    assert "reasoning_content" in out, "带 tools 却把 reasoning_content 剥了 —— DeepSeek 会 400"
    assert out["reasoning_content"] == "我先看看文件…"


def test_不带工具但带tool_calls_也必须留着():
    """🔴 **这条原来钉的是反的**（2026-09-20 反向重写）。

    原来叫 `test_不带工具时照旧剥掉_对别家零回归`，断言 `"reasoning_content" not in out`
    —— 而 `_ASSISTANT` **带 tool_calls**。真机上正是这个形状撞出 400：
    「撤掉工具那一轮」+「模型还是吐了 XML 工具调用被捞回来」⇒ 历史里留下
    **带 tool_calls 却没有 reasoning_content** 的消息 ⇒ **整条任务挂掉**。
    （现场与回放见本文件顶上那段。）

    ⚠️ 别把它改回去 —— 要改成 `tools=[]` 的**无 tool_calls** 消息再断言剥掉，
    那才是"对别家零回归"真正指的那条路（见下一条）。
    """
    out = _assistant_msg_for_history(dict(_ASSISTANT), tools=[])
    assert "reasoning_content" in out, "撤掉工具那一轮把 reasoning 剥了 ⇒ 下一条请求必然 400"


def test_不带工具且无tool_calls_照旧剥掉_对别家零回归():
    """真正的那条"零回归"路：`no_tools` 委员会（架构/规划）出的是**纯文本**消息。

    只有这类消息保持剥掉 —— 对 Kimi/GLM 的旧行为一字未动。
    """
    text_only = {k: v for k, v in _ASSISTANT.items() if k != "tool_calls"}
    out = _assistant_msg_for_history(text_only, tools=[])
    assert "reasoning_content" not in out


def test_其它字段一个都不许动():
    """对照组：这条判据只管 `reasoning_content`，别顺手改了别的字段。"""
    for tools in ([{"type": "function"}], []):
        out = _assistant_msg_for_history(dict(_ASSISTANT), tools=tools)
        assert out["role"] == "assistant"
        assert out["tool_calls"] == _ASSISTANT["tool_calls"]
        assert out["content"] == ""


def test_没有_reasoning_content_时两种都无所谓():
    """普通模型（非思考）压根没这个字段 —— 两种路径都该原样过去。"""
    plain = {"role": "assistant", "content": "好的"}
    for tools in ([{"type": "function"}], []):
        assert _assistant_msg_for_history(dict(plain), tools=tools) == plain


# ═══════════════════════════════════════════════════════════════
# `tool_choice="required"` 也是"被拒一次就该记住"的同一件事
# ═══════════════════════════════════════════════════════════════
#
# thinking 模式（DeepSeek 等）**不收** `tool_choice="required"`：
#   `400 Thinking mode does not support this tool_choice`
# 降级分支原来**只改当次的 body**，而 body 每轮重建 ⇒ 每个带工具的轮次都重撞一发。
# 2026-09-19 夜用真端点复现的量：3 个工具轮 = 6 次 HTTP，**3 次是白打的**。
#
# 钉住的是**跨轮次的记忆**，不是单次的降级 —— 单次降级原来就是对的，坏的是一直没记住。

class TestToolChoiceRequiredRemembered:
    """判据：**学过一次之后，换轮次、换实例都不再要 `required`**。

    ⚠️ 判据在 2026-09-20 改过：原来只钉"**同一个实例**的第二轮"，而真机上执行器
    **每轮都是新建的** ⇒ 那条用例全绿、生产上照样每轮白打一发
    （task `1789834349419`：告警响 7 次，侧车 `dispatches` 也是 7 条）。
    ⇒ 记的地方从实例挪到**按模型的模块级集合**，用例也跟着钉**跨实例**。

    变异：判据改回 `self._no_required_tool_choice`（实例级）→ `test_换了实例也不重撞` 红。
    """

    @pytest.fixture(autouse=True)
    def _清模块级记忆(self, monkeypatch):
        """🔴 **必须清**：那条记忆是**进程级**的（这正是修复本身），
        不清的话用例之间串味 —— 前一条学到的"m 不吃 required"会让后一条的
        `test_第一轮确实要了_required` 直接红，而红的是测试脏、不是代码坏。"""
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setattr(oa, "_NO_REQUIRED_TOOL_CHOICE", set())

    def _bodies(self, monkeypatch, model="m"):
        """跑一次真 `run()`，把每次 HTTP 的 body 按顺序记下来。"""
        from singularity.scheduler.executors import openai_agent as oa

        monkeypatch.setenv("TEST_KEY", "k")
        cfg = {"model": model, "api_key_env": "TEST_KEY", "entry": "http://x",
               "max_turns": 3}
        ex = oa.OpenAIAgentExecutor(cfg, "任务", "tid", skill_tools=[], mcp_tools=[])

        seen: list[dict] = []
        ok_calls = {"n": 0}

        def fake(body):
            # ⚠️ **必须拷一份**：降级分支会 `body["tool_choice"] = "auto"` **就地改**，
            # 存引用的话记下来的是"改完之后"的值 —— 第一版就栽在这儿：断言报
            # "第一轮没要 required"，其实是它自己把证据改了。
            seen.append(dict(body))
            if body.get("tool_choice") == "required":
                # 真机上那句原话
                raise oa._FormatError(
                    'HTTP 400: {"error":{"message":"Thinking mode does not support '
                    'this tool_choice","type":"invalid_request_error"}}')
            ok_calls["n"] += 1
            if ok_calls["n"] == 1:
                # 第一轮：回一个工具调用 ⇒ 循环进第二轮（不然测不到"跨轮"）
                return {"choices": [{"message": {
                    "content": "", "reasoning_content": "想一下",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": "{}"}}]}}]}
            return {"choices": [{"message": {"content": "收尾"}}]}

        monkeypatch.setattr(ex, "_api_call", fake)
        monkeypatch.setattr(ex, "_execute_tool", lambda name, args: "ok")
        ex.run()
        return seen

    def test_第一轮确实要了_required(self, monkeypatch):
        """先钉住前提：这个判据只有在**真的撞过**那次 400 之后才有意义。"""
        seen = self._bodies(monkeypatch)
        assert seen[0]["tool_choice"] == "required", \
            f"第一轮没要 required ⇒ 下面那条测的是空气：{seen[0].get('tool_choice')}"

    def test_第二轮不再重撞_required(self, monkeypatch):
        seen = self._bodies(monkeypatch)
        assert len(seen) >= 3, f"没跑到第二轮的请求，测不到'记没记住'：{len(seen)} 次"
        assert seen[2]["tool_choice"] == "auto", (
            f"第二轮又把 required 拼回来了（第 3 次请求 tool_choice={seen[2]['tool_choice']}）"
            " —— 这正是每个工具轮白撞一次 400 的成因")

    def test_学到的那次只出声一次(self, monkeypatch, caplog):
        """告警不能被自己的重试刷屏：一个 dispatch 只该报一条。"""
        from singularity.scheduler import witness
        hits: list[str] = []
        monkeypatch.setattr(witness, "warn",
                            lambda scope, msg, **kw: hits.append(msg))
        self._bodies(monkeypatch)
        learned = [h for h in hits if "tool_choice_required_rejected" in h]
        assert len(learned) == 1, f"报了 {len(learned)} 条：{learned}"

    def test_换了实例也不重撞_跨轮次记忆(self, monkeypatch):
        """🔴 **真机抓到的那个洞**：执行器每轮重建 ⇒ 记在实例上等于没记。

        判据：**同一个进程里、同一个模型，第二个执行器（= 第二轮 dispatch）
        的第一次请求就不该再要 `required`**。

        变异：把 `_NO_REQUIRED_TOOL_CHOICE` 改回 `self._no_required_tool_choice`
        （实例级）→ 本条红（第二个实例又会先撞一发）。
        """
        first = self._bodies(monkeypatch)                 # 第一个实例：撞一发、学到
        assert first[0]["tool_choice"] == "required", "前提没成立：第一个实例压根没要 required"
        second = self._bodies(monkeypatch)                # 第二个实例：**新对象，同一模型**
        assert second[0]["tool_choice"] == "auto", (
            "换了实例又去撞 required 了 —— 记忆记在实例上就等于没记"
            "（真机：一个任务白打 7 发，侧车 dispatches 正好 7 条）")


def test_那条_reasoning_400_不许当成_模型不吃思考参数():
    """地雷（2026-09-19 夜实测坐实）：那条 400 的原文里**含 `thinking` 这个词** ——

        `The `reasoning_content` in the **thinking** mode must be passed back to the API.`

    而 `_THINK_KEYS` 第一项就是 `"thinking"` ⇒ 光按子串匹配会把它**误读成
    "模型不吃这个参数"**：把参数悄悄摘掉重试，而**真问题一个字都不留痕**，
    连 `_dump_unknown_400` 也永远不会触发（它只接 `else:` 那一支）。

    **今天是隐性的**，只因 `request_template` 没配 thinking（`k in body` 不成立）——
    哪天有人配了，发作的样子是"模型突然变笨"，不是报错。

    变异：删掉 `_drop_rejected_think_param` 开头那句 `if "reasoning_content" in low` → 红。
    """
    err = ('HTTP 400: {"error":{"message":"The `reasoning_content` in the thinking '
           'mode must be passed back to the API.","type":"invalid_request_error"}}')
    body = {"thinking": {"type": "enabled"}, "tools": []}

    assert _drop_rejected_think_param(body, err) == "", "把 reasoning 的 400 当成思考参数被拒了"
    assert body == {"thinking": {"type": "enabled"}, "tools": []}, "思考参数被误摘了"


def test_真_不认某个思考参数时照旧摘掉():
    """**命门**：上面那条排除不能把正常功能一起打死。"""
    body = {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
    assert _drop_rejected_think_param(
        body, 'HTTP 400: unknown field "thinking"') == "thinking"
    assert body == {"reasoning_effort": "low"}
