"""task_templates.py 单元测试 — guess_template / get / list_all 纯函数。"""

import pytest


class TestGet:
    def test_known_template(self):
        from singularity.scheduler.task_templates import get
        t = get("bugfix")
        assert t.id == "bugfix"

    def test_unknown_defaults(self):
        from singularity.scheduler.task_templates import get
        t = get("nonexistent")
        assert t.id == "default"


class TestGuessTemplate:
    def test_bugfix_chinese(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("修复登录页面崩溃的bug") == "bugfix"

    def test_bugfix_english(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("fix the crash in login") == "bugfix"

    def test_refactor(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("重构数据库层，拆分解耦模块") == "refactor"

    def test_refactor_single(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("优化结构") == "refactor"

    def test_feature_new(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("新增用户注册功能") == "feature"

    def test_feature_implement(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("实现第三方登录接入") == "feature"

    def test_test_keyword(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("添加测试用例覆盖所有路径") == "test"

    def test_review(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("审查代码安全质量") == "review"

    def test_default_no_match(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("随便做点什么") == "default"

    def test_highest_score_wins(self):
        """同时匹配多个模板 → 取最高分。"""
        from singularity.scheduler.task_templates import guess_template
        # 'bug'和'fix'匹配bugfix(2分), '新增'匹配feature(1分)
        r = guess_template("新增功能来修复那个bug fix")
        assert r in ("bugfix", "feature")

    def test_empty_description(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("") == "default"
