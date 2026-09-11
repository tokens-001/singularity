"""外部依赖预检（"地板"）。

2026-09-12 立：嵌入模型联网查 huggingface metadata → 断网时挂死，
而**挂起不是异常**，`except` 拦不住，整条调研阶段无声停摆（防御模式 §57）。
这里钉住：探不通要**明着报**，探得通**不许误报**。
"""
import httpx
import pytest

# `_workflow_phases` 与 `workflow` 互相 import（workflow.py 末尾 `import *` 回填）。
# 直接第一个导 `_workflow_phases` 会撞上"半初始化"，所以先走完 `workflow`。
import singularity.scheduler.workflow  # noqa: F401
from singularity.scheduler import _workflow_phases as wp
from singularity.scheduler import _memory_core as mc
from singularity.scheduler import dispatcher as disp


class TestProbeUrl:
    def test_any_http_response_counts_as_reachable(self, monkeypatch):
        """404 / 405 也算通 —— 只要对面回话了。"""
        monkeypatch.setattr(httpx, "head", lambda *a, **k: object())
        assert wp._probe_url("https://api.example.com/v1") is True

    def test_connection_error_is_unreachable(self, monkeypatch):
        def _boom(*a, **k):
            raise httpx.ConnectError("断了")
        monkeypatch.setattr(httpx, "head", _boom)
        assert wp._probe_url("https://api.example.com/v1") is False


class TestPreflightExternal:
    @pytest.fixture(autouse=True)
    def _no_real_probe(self, monkeypatch):
        """默认：模型在、API 通。各用例按需打破其中一项。"""
        monkeypatch.setattr(mc, "_get_embed_model", lambda: object())
        monkeypatch.setattr(disp, "_all_agents_list", lambda ag: [])
        monkeypatch.setattr(wp, "_probe_url", lambda u, timeout=5.0: True)

    def test_all_good_reports_nothing(self):
        assert wp.preflight_external({}) == []

    def test_reports_missing_embed_model(self, monkeypatch):
        monkeypatch.setattr(mc, "_get_embed_model", lambda: None)
        problems = wp.preflight_external({})
        assert any("嵌入模型" in p for p in problems)

    def test_reports_unreachable_api(self, monkeypatch):
        monkeypatch.setattr(disp, "_all_agents_list",
                            lambda ag: [{"base_url": "https://api.example.com/v1"}])
        monkeypatch.setattr(wp, "_probe_url", lambda u, timeout=5.0: False)
        problems = wp.preflight_external({})
        assert any("模型 API 不可达" in p for p in problems)

    def test_same_base_url_probed_once(self, monkeypatch):
        """同一家多个别名 → 只探一次，不刷屏。"""
        calls = []
        monkeypatch.setattr(disp, "_all_agents_list", lambda ag: [
            {"base_url": "https://api.example.com/v1"},
            {"base_url": "https://api.example.com/v1"},
            {"base_url": "https://api.example.com/v1"},
        ])
        monkeypatch.setattr(wp, "_probe_url",
                            lambda u, timeout=5.0: (calls.append(u), False)[1])
        problems = wp.preflight_external({})
        assert len(calls) == 1
        assert len(problems) == 1

    def test_probe_exception_is_reported_not_raised(self, monkeypatch):
        """探测自己炸了也不能把阶段带崩 —— 但要报出来。"""
        monkeypatch.setattr(disp, "_all_agents_list",
                            lambda ag: (_ for _ in ()).throw(RuntimeError("炸了")))
        problems = wp.preflight_external({})
        assert any("探测异常" in p for p in problems)
