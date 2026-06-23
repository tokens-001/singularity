"""chancellor.py 单元测试 — assess() 规则分类 6 分支 (167行纯规则)。

ponytail: assess 不调 LLM，纯规则，直接测输入→输出。
"""

import pytest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# assess()
# ═══════════════════════════════════════════════════════════════

class TestAssess:
    def test_core_file_changed_critical(self):
        """改动核心文件 → critical。"""
        from singularity.scheduler.chancellor import assess
        r = assess("重构代码", "pass", changed_files=["src/core.py", "app.py"])
        assert r.severity == "critical"
        assert "core.py" in r.title

    def test_core_file_tokenizer(self):
        """改动 tokenizer.py → critical。"""
        from singularity.scheduler.chancellor import assess
        r = assess("优化分词", "pass", changed_files=["src/tokenizer.py"])
        assert r.severity == "critical"

    def test_merge_conflict_critical(self):
        """合并冲突 → critical。"""
        from singularity.scheduler.chancellor import assess
        r = assess("合并分支", "merge_conflict at file.py")
        assert r.severity == "critical"
        assert "合并冲突" in r.title

    def test_conflict_in_reason_lowercase(self):
        """term_reason 含 conflict → critical。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复bug", "conflict in merge")
        assert r.severity == "critical"

    def test_escalation_exhausted_alert(self):
        """升级链耗尽 → alert。"""
        from singularity.scheduler.chancellor import assess
        r = assess("实现认证", "escalation_exhausted (level=E+)")
        assert r.severity == "alert"
        assert "全部 agent 都试了" in r.what

    def test_retry_two_or_more_alert(self):
        """retry >= 2 → alert。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复登录", "failed: 测试不通过",
                   retry_count=2, agent_tried=["claude", "gpt-4"])
        assert r.severity == "alert"
        assert "2 次" in r.title

    def test_retry_three_alert(self):
        """retry=3 → alert。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复", "failed: error", retry_count=3)
        assert r.severity == "alert"
        assert "3 次" in r.title

    def test_first_failure_routine(self):
        """首次失败 (retry<=1) → routine。"""
        from singularity.scheduler.chancellor import assess
        r = assess("写个页面", "failed: validation error",
                   retry_count=0)
        assert r.severity == "routine"
        assert "小问题" in r.title

    def test_first_failure_retry_one_routine(self):
        """retry=1 + failed → routine。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复", "failed: error", retry_count=1)
        assert r.severity == "routine"

    def test_default_noise(self):
        """不匹配任何规则 → noise。"""
        from singularity.scheduler.chancellor import assess
        r = assess("正常任务", "pass", changed_files=["app.py"])
        assert r.severity == "noise"

    def test_pass_no_failure_noise(self):
        """pass 结果 + 非核心文件 → noise（不触发 failed 分支）。"""
        from singularity.scheduler.chancellor import assess
        r = assess("正常任务", "pass", changed_files=["app.py"], retry_count=0)
        assert r.severity == "noise"

    def test_non_core_file_not_critical(self):
        """改普通文件不触发 critical。"""
        from singularity.scheduler.chancellor import assess
        r = assess("重构", "pass", changed_files=["src/app.py", "src/utils.py"])
        assert r.severity == "noise"

    def test_empty_changed_files(self):
        """无改动文件 → 跳过核心文件检查。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复", "pass", changed_files=[])
        assert r.severity == "noise"

    def test_escalation_priority_over_retry(self):
        """escalation_exhausted 优先于 retry>=2（规则顺序）。"""
        from singularity.scheduler.chancellor import assess
        r = assess("复杂任务", "escalation_exhausted (level=E+)",
                   retry_count=5)
        assert r.severity == "alert"
        assert "全部 agent 都试了" in r.what  # escalation 消息，非 retry 消息

    def test_core_file_priority_over_all(self):
        """核心文件检查在最前面，即使也有冲突。"""
        from singularity.scheduler.chancellor import assess
        r = assess("改核心", "merge_conflict",
                   changed_files=["src/core.py"])
        assert r.severity == "critical"
        assert "核心文件" in r.title  # 核心文件消息优先

    def test_agent_tried_in_alert(self):
        """alert 报告包含尝试过的 agent 列表。"""
        from singularity.scheduler.chancellor import assess
        r = assess("修复", "failed: 重复失败",
                   retry_count=2, agent_tried=["gpt-4", "claude"])
        assert "gpt-4" in r.what or "gpt-4" in r.why
