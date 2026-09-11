"""api_store._is_major_model 单元测试 —— 锁住"当前代主力模型"的识别。

这个 bug 的形状很朴素：模型 ID 不带代际数字时(deepseek-flash)，白名单模式匹配不上，
扫描时被**静默丢掉**。用户看到的只是"deepseek 怎么只能扫描一个模型"，
列表里少一个，没有任何报错。
"""

import pytest

from singularity.scheduler.api_store import _is_major_model


class TestKeepsCurrentGen:
    @pytest.mark.parametrize("mid", [
        "deepseek-v4-pro",
        "deepseek-flash",       # 不带代际数字 —— 就是它被漏掉过
        "deepseek-pro",
        "deepseek-chat",
        "deepseek-reasoner",
        "claude-sonnet-4-6",    # Anthropic 早就是这么处理的
        "gpt-5",
        "kimi-k2",
        "glm-4",
    ])
    def test_kept(self, mid):
        assert _is_major_model(mid) is True, f"{mid} 应该被保留，否则扫描会静默少一个模型"


class TestDropsOthers:
    @pytest.mark.parametrize("mid", [
        "bge-large-zh",
        "whisper-1",
        "some-internal-model",
    ])
    def test_dropped(self, mid):
        assert _is_major_model(mid) is False
