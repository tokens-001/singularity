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


def test_散文验收真的会被致命档拦下_端到端接线():
    """🔴 **这条才是 bf571c57 缺的那条测试** —— 它把三个函数**串起来**跑。

    原来两条测试各自绿着：一条只测 `_validate_architecture` **报不报违规**，
    另一条把一个**手写的字符串**直接喂给 `classify_arch_issues` 看它判不判致命。
    而 `_run_planning` 中间还有一行过滤 —— `bf571c57` 那条文案三个关键词一个都不含
    ⇒ 被滤掉 ⇒ 分类器压根没见过它 ⇒ **致命档永远是空的**（13 条测试全绿，门是装饰）。
    2026-09-21 真机实测：散文验收走完这条路，`blockers == []`、`fatal == []`。

    判据：**删掉 `split_arch_issues` 里那句前缀判断** ⇒ 这条红。
    """
    from singularity.scheduler.workflow import (
        _validate_architecture, split_arch_issues, classify_arch_issues, ACCEPTANCE_UNSTATED)
    arch = _arch("CONTRACTS.md 列出三个契约的全部字段")   # ← 旧写法：整条散文
    audit, _noted = split_arch_issues(_validate_architecture(arch))
    assert any(ACCEPTANCE_UNSTATED in i for i in audit), \
        f"「没表态」被那行过滤滤掉了，分类器看不到它: {audit}"
    fatal, _ = classify_arch_issues(audit)
    assert fatal, "致命档是空的 ⇒ GATE2 照过，那道门等于没建"


def test_只记的那类仍然只记():
    """反例也要钉：`建议补充字段` 是**有意**不拦的（单文件 CLI 本来就没有 data_model）。"""
    from singularity.scheduler.workflow import split_arch_issues, classify_arch_issues
    audit, noted = split_arch_issues(["建议补充字段: api_contracts", "缺少必填字段: data_model"])
    assert audit == ["缺少必填字段: data_model"], audit
    assert noted == ["建议补充字段: api_contracts"], noted
    fatal, _ = classify_arch_issues(audit)
    assert not fatal, "缺 data_model 不该致命 —— 那会把好活挡在门外"


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


# ── 架构**内部**自相矛盾：2026-09-27 `round-20260926` ────────────────────
#
# 那一轮 `tech_stack.language` 写着「Go 1.22+」，而 **16 条约束的 `check.argv`
# 16/16 全是 `python3 -m pytest`**、测试任务全是 `tests/*.py`。
# ⇒ 干活的人**没法同时满足** ⇒ 全图唯一入口那个任务打转 **811 秒 / 275,806 token /
# 6 个工具轮 / 0 个文件改动** ⇒ 13 个依赖它的任务全灭。
#
# ⚠️ 原来的校验拦的是「**字段缺不缺**」，这两个字段**都在、都非空** —— 只是说的不是一种语言。

def test_架构语言自相矛盾要拦在GATE2_端到端接线():
    """🔴 三个函数**串起来**跑 —— 只测函数会漏掉中间那行过滤（这个仓栽过，见
    `split_arch_issues` 的 docstring：新规则三个关键词一个都不含 ⇒ 被滤掉 ⇒ 门是装饰）。

    判据：把 `classify_arch_issues` 里 `ARCH_SELF_CONTRADICTION in i` 那段删掉 ⇒ 这条红。
    """
    from singularity.scheduler.workflow import (
        _validate_architecture, split_arch_issues, classify_arch_issues,
        ARCH_SELF_CONTRADICTION)
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tech_stack"] = {"language": "Go 1.22+：CGO_ENABLED=0 产出单静态二进制；bufio 流式"}
    arch["constraints"] = [{"rule": "流式读取", "check": {
        "argv": ["python3", "-m", "pytest", "-q", "tests/test_parser.py"], "expect_exit": 0}}]
    audit, _noted = split_arch_issues(_validate_architecture(arch))
    assert any(ARCH_SELF_CONTRADICTION in i for i in audit), \
        f"矛盾没进分类器（被 split 滤掉了？）: {audit}"
    fatal, _ = classify_arch_issues(audit)
    assert fatal, "致命档是空的 ⇒ GATE2 照过，矛盾照样流到执行层去打转"


def test_语言说Go判据也是Go_不许误报():
    """反例：真 Go 项目里 argv 是 `go test` ⇒ **一点矛盾都没有**，不许拦。"""
    from singularity.scheduler.workflow import (
        _validate_architecture, classify_arch_issues)
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tech_stack"] = {"language": "Go 1.22+"}
    arch["constraints"] = [{"rule": "r", "check": {
        "argv": ["go", "test", "./..."], "expect_exit": 0}}]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert not fatal, f"真 Go 项目被误拦了 —— 误报会把好活挡在门外: {fatal}"


def test_认不出语言就不表态_宁可漏报():
    """tech_stack 是一段没提语言的散文 ⇒ 认不出 ⇒ **不报**（不猜）。"""
    from singularity.scheduler.workflow import (
        _validate_architecture, classify_arch_issues)
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tech_stack"] = {"language": "单静态二进制、零依赖、启动快"}
    arch["constraints"] = [{"rule": "r", "check": {
        "argv": ["python3", "-m", "pytest"], "expect_exit": 0}}]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert not fatal, f"认不出来还敢判 —— 这是在猜: {fatal}"


def test_验收里的命令也要跟声明语言对得上():
    """🔴 第一版只扫了 `constraints`，**漏了 `acceptance`** —— 而那一轮 T0 的
    `acceptance` 里**也**写着 `python3 -m pytest`：**同一个任务对象自己就前后打架**
    （描述要写 Go、验收却用 pytest）。漏扫一半等于漏一半。

    判据：把 `_arch_language_conflict` 里扫 `tasks[].acceptance` 那段删掉 ⇒ 这条红。
    """
    from singularity.scheduler.workflow import (
        _validate_architecture, classify_arch_issues, ARCH_SELF_CONTRADICTION)
    arch = _arch([{"text": "契约就位",
                   "check": {"argv": ["python3", "-m", "pytest", "-q", "tests/test_build.py"],
                             "expect_exit": 0}}])
    arch["tech_stack"] = {"language": "Go 1.22+"}
    arch["constraints"] = [{"rule": "r", "check": {"text_only_reason": "人看"}}]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert any(ARCH_SELF_CONTRADICTION in i for i in fatal), fatal


def test_依赖不存在的id是致命的():
    """任务依赖一个**盘上不存在的 id** ⇒ 它永远卡 `blocked`（等一个不会来的前置）
    ⇒ 判据是"下一步还能不能干" ⇒ **致命档**，不是"只记"。"""
    from singularity.scheduler.workflow import _validate_architecture, classify_arch_issues
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tasks"][0]["depends_on"] = ["T999"]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert any("不存在" in i for i in fatal), fatal


def test_依赖成环是致命的():
    """环上每个任务都在等对方 ⇒ 谁也起不来。同属"必然卡死"。"""
    from singularity.scheduler.workflow import _validate_architecture, classify_arch_issues
    acc = [{"text": "契约就位", "check": {"text_only_reason": "人看"}}]
    arch = _arch(acc)
    arch["tasks"] = [
        {"id": "T1", "title": "a", "description": "d", "complexity": "low",
         "layer": "backend", "acceptance": acc, "depends_on": ["T2"]},
        {"id": "T2", "title": "b", "description": "d", "complexity": "low",
         "layer": "backend", "acceptance": acc, "depends_on": ["T1"]},
    ]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert any("成环" in i for i in fatal), fatal


def test_未澄清只记不致命_别把它做成死结():
    """🔴 **这条钉的是"不许把它归致命"** —— 想改之前先读 `_arch_open_questions` 的 docstring。

    奇点**没有"编辑架构"的接口** ⇒ 拦成致命的唯一后果是**人也改不了、只能打回重出**，
    而它本来就要在 GATE2 被人看一遍。**加一道必须打回才能过的门 = 白加一道门。**

    判据：把 `classify_arch_issues` 的致命判据里加上 `ARCH_NEEDS_CLARIFICATION` ⇒ 这条红。
    """
    from singularity.scheduler.workflow import _validate_architecture, classify_arch_issues
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tech_stack"] = {"language": "NEEDS CLARIFICATION: 题面未指定实现语言"}
    fatal, noted = classify_arch_issues(_validate_architecture(arch))
    assert any("未澄清" in i for i in noted), f"没报出来，人在 GATE2 上看不见: {noted}"
    assert not any("未澄清" in i for i in fatal), \
        f"归了致命档 ⇒ 人也没法改（没有编辑架构的接口）⇒ 死结: {fatal}"


def test_go_不许匹配到别的词里():
    """词边界：`goal-oriented` / `good` 里的 `go` **不算**说 Go。

    裸 `in` 会在这里误判；这条钉的就是那对 `(?<![a-z0-9])…(?![a-z0-9])`。
    """
    from singularity.scheduler.workflow import (
        _validate_architecture, classify_arch_issues)
    arch = _arch([{"text": "契约就位", "check": {"text_only_reason": "人看"}}])
    arch["tech_stack"] = {"language": "goal-oriented 的模块划分，good 的命名"}
    arch["constraints"] = [{"rule": "r", "check": {
        "argv": ["python3", "-m", "pytest"], "expect_exit": 0}}]
    fatal, _ = classify_arch_issues(_validate_architecture(arch))
    assert not fatal, f"`goal`/`good` 里的 go 被当成了 Go 语言: {fatal}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
