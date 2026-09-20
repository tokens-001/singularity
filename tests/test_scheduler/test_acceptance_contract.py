"""验收条件必须【表态】—— 2026-09-20 §88 的真根因。

来历（`验证-日志统计-20260920c`）：T1 的 acceptance 白纸黑字写着
「`CONTRACTS.md` 列出三个契约的全部字段与缺失值语义」，而那个文件**全历史不存在**，
任务**照样判 done** —— 因为那条验收**从没进过机器检查**。
下游 T2–T6 全都以为契约冻结过了，T4 按自己理解的接口实现 ⇒ 主线跑一次就崩。

判据三态（与 `constraints[].check` 共用一套 `parse_check`）：

    兑现  ``check`` 是 ``{argv, expect_exit}``
    诚实  ``check`` 是 ``{text_only_reason: "<非空>"}``   ← **必须放行**
    漏    ``check`` 缺失 / null / 两种都不是               ← **只有这个算违规**

🔴 **"诚实"必须放行是这套东西的命门**：逼着模型"必须给出命令"，它就会**编一条假命令** ——
那是假门（§78：假门比没门坏）。所以下面 `test_说了验不了的验收要放行` 和
`test_没表态的验收会让校验致命` **两条必须同时绿**，只绿一条就说明判据被写歪了。
"""
import pytest

from singularity.scheduler import _machine_checks as mc
from singularity.scheduler import workflow  # noqa: F401  ← 先导它，绕开循环导入


# ── 函数级：三态 + 那个数 ──────────────────────────────────────────────

def test_兑现的验收算兑现():
    acc = [{"text": "import 成功",
            "check": {"argv": ["python3", "-c", "import jsonlstats"], "expect_exit": 0}}]
    items, problems = mc.parse_acceptance(acc)
    assert [i["kind"] for i in items] == ["argv"]
    assert problems == []
    assert mc.acceptance_coverage(acc) == (1, 1)


def test_说了验不了的验收要放行():
    """命门之一：逼着写命令 ⇒ 编假命令。所以"诚实承认验不了"必须算合格。"""
    acc = [{"text": "表格排版观感清晰", "check": {"text_only_reason": "观感指标，机器验不了"}}]
    items, problems = mc.parse_acceptance(acc)
    assert [i["kind"] for i in items] == ["text_only"]
    assert problems == [], "说了'验不了'不该算违规"


def test_没表态的验收会让校验致命():
    """命门之二：`check` 缺失 = 漏。**这正是 T1 那个洞的形状。**"""
    acc = [{"text": "CONTRACTS.md 列出三个契约的全部字段"}]   # ← 没有 check
    items, problems = mc.parse_acceptance(acc)
    assert [i["kind"] for i in items] == ["unstated"]
    assert len(problems) == 1 and "没表态" in problems[0]


def test_旧散文字符串形态算没表态而不是诚实():
    """🔴 全是散文的旧形态**不能**被当成"诚实" —— 否则改前改后一个数，白改。

    区别是整件事的判据：**"验不了"是表过态的，"没被要求表态"不是。**
    """
    items, problems = mc.parse_acceptance("python3 -c \"import x\" 退出 0；表格美观")
    assert [i["kind"] for i in items] == ["unstated"]
    assert problems, "旧散文形态必须报违规"
    assert mc.acceptance_coverage("随便一段话") == (0, 1)


def test_那个数的分母含诚实条():
    """`coverage()` 那条注释警告过的口径坑：分母只数兑现的 ⇒ 这个数恒等于 100%。"""
    acc = [
        {"text": "a", "check": {"argv": ["python3", "-m", "pytest", "-q", "t.py"], "expect_exit": 0}},
        {"text": "b", "check": {"text_only_reason": "验不了"}},
        {"text": "c", "check": {"text_only_reason": "也验不了"}},
    ]
    assert mc.acceptance_coverage(acc) == (1, 3), "分母必须是全部条目，不是兑现的条数"


def test_拍平给下游消费者用():
    acc = [{"text": "甲", "check": {"argv": ["python3", "-m", "pytest"], "expect_exit": 0}},
           {"text": "乙", "check": {"text_only_reason": "x"}}]
    assert mc.acceptance_text(acc) == "甲；乙"
    assert mc.acceptance_text("还是散文") == "还是散文"      # 旧形态原样透传，不炸
    assert mc.acceptance_text("") == ""


@pytest.mark.parametrize("bad", [123, {"a": 1}, [{"nocheck": 1}], [{"text": ""}]])
def test_畸形输入不抛异常只报违规(bad):
    """09-18 的教训：一条脏数据不该毁掉整条建任务流程（那次 331 条告警、任务永远建不出来）。"""
    items, problems = mc.parse_acceptance(bad)
    assert isinstance(items, list) and isinstance(problems, list)
    assert problems, f"{bad!r} 应该报违规而不是静默通过"


# ── 接线级：校验器真的会拦 ─────────────────────────────────────────────

def _arch(acceptance):
    return {
        "architecture": "x", "modules": [{"name": "m"}], "data_model": {"entities": []},
        "tech_stack": {"language": "python"}, "constraints": [{"rule": "r"}],
        "tasks": [{"id": "T1", "title": "t", "description": "d", "complexity": "low",
                   "layer": "backend", "acceptance": acceptance}],
    }


def test_架构校验对没表态的验收报违规():
    from singularity.scheduler.workflow import _validate_architecture, ACCEPTANCE_UNSTATED
    issues = _validate_architecture(_arch([{"text": "CONTRACTS.md 列出全部字段"}]))
    assert any(ACCEPTANCE_UNSTATED in i and "T1" in i for i in issues), issues


def test_架构校验对诚实的验收放行():
    from singularity.scheduler.workflow import _validate_architecture, ACCEPTANCE_UNSTATED
    issues = _validate_architecture(
        _arch([{"text": "表格美观", "check": {"text_only_reason": "观感指标"}}]))
    assert not any(ACCEPTANCE_UNSTATED in i for i in issues), issues


def test_没表态是致命档不只是一条warning():
    """🔴 **这条钉的是档位**：T1 那种洞就算报了违规，只要归"只记"档就等于没拦。

    这正是它原来的形状 —— `fatal = [i for i in blockers if "tasks" in i]`，
    而「任务 T1: 缺少 acceptance」不含 "tasks" ⇒ 永远进不了致命档。
    """
    from singularity.scheduler.workflow import classify_arch_issues, ACCEPTANCE_UNSTATED
    fatal, noted = classify_arch_issues([
        f"任务 T1: {ACCEPTANCE_UNSTATED} —— 第 1 条没表态",
        "任务 T2: 缺少 data_model（示例的只记项）",
    ])
    assert len(fatal) == 1 and ACCEPTANCE_UNSTATED in fatal[0], fatal
    assert len(noted) == 1 and "data_model" in noted[0], noted


def test_执行器提示词里的验收要拍平不能是字典字面(monkeypatch):
    """🔴 `bf571c57` 改了 `acceptance` 的**形状**（str → list[dict]），这是漏掉的第 5 个消费者。

    `_exec_context._build_project_context` 直接 `f"验收标准: {acceptance}"` 插值 ——
    执行器会在提示词里收到 `[{'text': ..., 'check': {...}}]` 这种 Python 字面样子。
    （同族教训：改"值的形状"必须扫全部消费者、看到最后一跳怎么渲染。）
    """
    from types import SimpleNamespace
    from singularity.scheduler import project as pm
    from singularity.scheduler import _exec_context as ctx

    acc = [{"text": "import 成功",
            "check": {"argv": ["python3", "-c", "import jsonlstats"], "expect_exit": 0}}]
    proj = SimpleNamespace(
        name="演示", research_report=None, handoffs=[], constraints_checklist=[],
        architecture={"tasks": [{"id": "T1", "title": "T1 解析器", "acceptance": acc}]})
    monkeypatch.setattr(pm, "load", lambda pid: proj)

    out = ctx._build_project_context(
        SimpleNamespace(project_id="p1", description="T1 解析器：写它"))
    # 先钉"这段真的产出了" —— 那个函数用 `except Exception: return ""` 兜底，
    # 光断言"不含字典"的话，它整个哑掉也照样绿（假绿）。
    assert "验收标准" in out, f"上下文根本没产出（被兜底吞了）: {out!r}"
    assert "{" not in out, f"喂了字典字面给执行器: {out!r}"
    assert "import 成功" in out, out


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
