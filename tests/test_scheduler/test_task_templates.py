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
        """同时匹配多个模板 → **取最高分**（不是随便取一个匹配上的）。

        ⚠️ 原来断言是 `r in ("bugfix", "feature")` —— **两边都收**，
        于是"取最高分"这件事根本没被钉住：把 `max(...)` 改成**取最低分的非零项**，
        这条照样绿（外派⑬ 的 A7 批次报的，2026-09-14 我变异复核坐实：
        那次只有隔壁 `test_test_keyword` 变红，这条没红）。
        这里把期望值钉死成具体那一个。
        """
        from singularity.scheduler.task_templates import guess_template
        # 'bug'和'fix'匹配bugfix(2分), '新增'匹配feature(1分) ⇒ 必须取分高的 bugfix
        assert guess_template("新增功能来修复那个bug fix") == "bugfix"

    def test_empty_description(self):
        from singularity.scheduler.task_templates import guess_template
        assert guess_template("") == "default"
