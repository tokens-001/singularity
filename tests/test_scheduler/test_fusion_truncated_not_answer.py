"""**被我们掐断的半截思考，不许当答案返回**（2026-09-27 追 `round-20260922` 那笔"原因未定"）。

## 症状
`round-20260922` 规划层跑了 **100 分钟 / 351,604 token**（成绩单原话「原因未定，需要对照轮」）。
同一窗口里，三条 `stream_over_budget:fusion:600s` **全是 `chars=0`**，
紧跟三条 `reasoning_only:<模型>:`，一路退化到 `fusion_empty_fallback_synthesis`，
收尾那条是 `test_cases_missing_in_arch`（"集成测试和 E2E 清单都会是空的"）。

## 根因
`_stream_once` 被 600s 那把尺掐断时，`finish` **停在空串**；而下游 `_call_model` 那句
`if not content and reasoning and finish != "length": return reasoning` 是靠
"不是 length" 来判断"reasoning 里装的是答案、可以回退"的 —— 掐断时 `finish` 也满足
⇒ **半截思考被当提取结果返回**。它头顶的注释原话就是在防这个，只是漏了这条路。

⇒ 本仓那个老形状：**`finish` 为空同时表示「服务端正常收尾」和「我方掐断」**（一个值两件事）。
修法：掐断时给一个**能 grep 的明确值** `"over_budget"`，并把它加进守卫的排除名单。

🔴 **接线比函数容易断**：`_over_budget` 是 `_stream_once` 里的局部变量，光设 `finish`
还不够 —— 守卫那头的排除名单也得改。两条都钉。
"""

import time

from singularity.scheduler import execution_judge as ej

# 一条能解析出 reasoning_content 的 SSE 行（按行吐，末尾要有换行）。
_SSE_REASONING = ('data: {"choices":[{"delta":{"reasoning_content":"想了很久"}}]}\n'
                  'data: {"choices":[{"delta":{"reasoning_content":"但没写"}}]}\n')


class _FakeStream:
    """够 `_stream_once` 用的假流：有 `status_code`，`iter_text` 按批吐字节。

    ⚠️ **停顿必须发生在"吐第二批之前"**，也就是这里 —— 不能放在 `_FakeClient.stream()`
    里：那个调用发生在 `_deadline` 起表**之前**，睡完了表才起，等于没拖到过期
    （第一版就这么写的，测出来 `_over_budget` 恒假、白跑一趟）。
    """

    def __init__(self, batches, gap=0.0):
        self._batches = batches
        self._gap = gap

    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_text(self):
        for i, b in enumerate(self._batches):
            if i and self._gap:
                # 第一批照常吐（reasoning 进得来），之后拖到超过 cap。
                time.sleep(self._gap)
            yield b


class _FakeClient:
    def __init__(self, batches, gap=0.2):
        self._batches = batches
        self._gap = gap

    def stream(self, *a, **kw):
        return _FakeStream(list(self._batches), self._gap)


def _ready(monkeypatch, stream_once):
    """把 `_call_model` 那几道前置闸门摆平，只留要测的那段逻辑。"""
    from singularity.scheduler import api_store, dispatcher

    monkeypatch.setattr(dispatcher, "load_agents",
                        lambda: {"any": [{"model": "deepseek-flash", "type": "openai-agent"}]})
    monkeypatch.setattr(api_store, "is_available", lambda _p: True)
    monkeypatch.setattr(ej, "_resolve_api", lambda m: ("FAKE_KEY", "http://x"))
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr(ej, "_stream_once", stream_once)


def test_被掐断时_finish_不许留在空串(monkeypatch):
    """`_stream_once` 这一头：掐断了就得说"是我们断的"，别用空串兼职两种含义。

    判据是 `finish == "over_budget"` —— 下面 `_call_model` 那条守卫靠它分辨。
    变异：把 `finish = "over_budget"` 删掉 ⇒ 本条红。
    """
    monkeypatch.setattr(ej, "_FUSION_CALL_CAP", 0.05)
    client = _FakeClient([_SSE_REASONING, _SSE_REASONING], gap=0.2)

    status, content, finish, err, reasoning = ej._stream_once(
        client, "http://x", {}, {"model": "m"})

    assert status == 200
    assert content == "", f"这次压根没吐正文，content 该是空的：{content!r}"
    assert reasoning, "夹具没喂进 reasoning ⇒ 这条测的不是目标场景"
    assert finish == "over_budget", (
        f"掐断了却把 finish 留在 {finish!r} ⇒ 下游分不清「正常收尾」和「我方掐断」")


def test_半截思考不许当答案返回(monkeypatch):
    """`_call_model` 这一头：`finish == "over_budget"` ⇒ 不许回退那段 reasoning。

    判据不只断言返回空 —— 还断言**没把那句思考吐出去**（返回空串但内容其实是思考，
    是另一种骗法）。
    """
    _ready(monkeypatch, lambda *a, **kw: (200, "", "over_budget", "", "半截思考，不是答案"))

    out = ej._call_model("hi", "deepseek-flash")

    assert out == "", f"半截思考被当答案返回了：{out!r}"


def test_撞输出上限时也不许回退(monkeypatch):
    """对照组（原有行为，别改坏）：`finish == "length"` 是半截思考，同样不许回退。"""
    _ready(monkeypatch, lambda *a, **kw: (200, "", "length", "", "半截思考"))

    assert ej._call_model("hi", "deepseek-flash") == ""


def test_模型自己收尾时_reasoning_照旧回退(monkeypatch):
    """反方向对照：**别把守卫修成"谁都不许回退"**。

    思考模型把答案落在 reasoning_content 里是真实存在的形状（`reasoning_only` 那条路），
    正常收尾时它必须照旧回退。
    """
    _ready(monkeypatch, lambda *a, **kw: (200, "", "stop", "", "答案在这里"))

    assert ej._call_model("hi", "deepseek-flash") == "答案在这里"
