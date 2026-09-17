"""拆任务时喂给执行器的上下文 —— **判据要的输入，必须和判据一起交出去**。

🔴 2026-09-17 真机：架构声明了 10 条 `constraints`，每条 `check.argv` 指定一个测试文件
（`python3 -m pytest -q tests/test_contract_normal.py` 这种），而 `decompose_architecture`
**只把 `rule` 的文字**放进 `context_snippet` —— 干活的人**从头到尾没见过那个文件名**
⇒ 收尾时机器检查 `ran=10 passed=0`，十条全是
`ERROR: file or directory not found: tests/test_contract_*.py`。

⇒ 和 `<项目仓>/test_cases.json` 那条是**同一个形状**（三个读者都在等一份**没人被要求写**的文件）。
"""
from singularity.scheduler.execution_judge import decompose_architecture


def _arch(*, with_check=True, with_modules=True):
    """一个最小的架构 JSON —— 一个任务 + 一条约束。"""
    return {
        "modules": [{"name": "logstat.parser", "responsibility": "解析 JSONL 并跳过坏行"}],
        "tasks": [{
            "id": "T1", "title": "解析模块 parser 的实现",
            "description": "实现 logstat/parser.py 的坏行容错",
            "layer": "any", "depends_on": [],
            "related_modules": ["logstat.parser"] if with_modules else [],
        }],
        "api_contracts": [],
        "constraints": [{
            "type": "reliability",
            "rule": "解析模块 parser：坏行必须被跳过并计数，且 stderr 恰好一行",
            "check": ({"argv": ["python3", "-m", "pytest", "-q",
                                "tests/test_contract_badlines.py"], "expect_exit": 0}
                      if with_check else {}),
        }],
        "test_cases": {},
    }


def test_约束的机器检查命令要进上下文():
    """**这条就是真机那十枪的病根。** 文件名没传到，干活的人不可能去造它。"""
    out = decompose_architecture(_arch())
    assert len(out) == 1
    ctx = out[0]["context_snippet"]
    assert "tests/test_contract_badlines.py" in ctx, (
        "机器检查要跑的那个测试文件**没进上下文** ⇒ 干活的人不会去造它 "
        f"⇒ 机器检查必然全失败。实际上下文：\n{ctx}")
    assert "pytest" in ctx, f"命令本身也该在（不然只知道文件名不知道跑什么）：\n{ctx}"


def test_约束的文字本身还得在():
    """**别把修法改宽成"只传命令"** —— 规则文字是干活的人理解"要做什么"的依据。"""
    ctx = decompose_architecture(_arch())[0]["context_snippet"]
    assert "坏行必须被跳过并计数" in ctx, f"规则文字丢了：\n{ctx}"


def test_没有_check_时不硬塞一行空的():
    """架构没给 `check`（纯人工约束）⇒ **不许**塞一句空命令进去。"""
    ctx = decompose_architecture(_arch(with_check=False))[0]["context_snippet"]
    assert "机器检查会真跑" not in ctx, f"没有检查却写了一行假命令：\n{ctx}"
    assert "坏行必须被跳过并计数" in ctx, f"规则文字还是该在：\n{ctx}"


def test_关键词对不上也必须进_真机那条筛子永远匹配不上():
    """🔴 **真机那轮 10 条约束、11 个任务，0 命中。**

    因为筛子用的是 `type`（**分类**，如 `"reliability"`）和 `rule[:20]`（**散文**）——
    这俩在结构上就不该出现在任务标题里。⇒ 干活的人**连规则文字都没见过**。

    这条钉的是「**筛子没了**」：标题/描述里**一个关键词都不含**，约束照样要进。
    """
    arch = _arch()
    arch["tasks"][0]["title"] = "写测试"
    arch["tasks"][0]["description"] = "给解析模块补测试"
    ctx = decompose_architecture(arch)[0]["context_snippet"]
    assert "tests/test_contract_badlines.py" in ctx, (
        "标题/描述里没有关键词，约束就没进 ⇒ 这正是真机 10/10 全失败的原因。"
        f"实际上下文：\n{ctx}")
    assert "坏行必须被跳过并计数" in ctx, f"规则文字也没进：\n{ctx}"


def test_check_写成散文也不许炸整条建任务流程():
    """🔴 2026-09-18 真机：模型在 `check` 里写了**散文**，整条建任务流程崩了。

    架构第 11 条约束的 `check` 是：
    「机器验不了：表格排版是观感指标，没有稳定可判定的判定式……故如实交由人工目视验收，
      不编造命令。」
    ⇒ `(c.get("check") or {}).get("argv")` 抛 `'str' object has no attribute 'get'`
    ⇒ `decompose_architecture` **整条崩** ⇒ 建任务那步反复失败回滚
    （**每 3 秒一条告警**、刷了 331 条），项目**永远拿不到任务**（`task_ids` 恒为 0）。

    ⚠️ **散文写法是设计允许的** —— `_machine_checks.parse_check` 的文档明写：
    "两种写法是故意的：能机器跑的给 argv，验不了的如实写散文"。
    所以这里该跟它**用同一份判据**，不是自己假设 `check` 一定是 dict。

    ⚠️ 判据：①**不抛**（这是崩的那条）②散文不许被当成命令塞进上下文。
    """
    arch = _arch()
    arch["constraints"][0]["check"] = "机器验不了：排版是观感指标，交由人工目视验收"
    out = decompose_architecture(arch)          # ① 不抛
    assert len(out) == 1, "散文 check 把整条拆解带崩了"
    ctx = out[0]["context_snippet"]
    assert "机器检查会真跑" not in ctx, f"散文被当成命令了：\n{ctx}"
    assert "坏行必须被跳过并计数" in ctx, f"规则文字还是该在：\n{ctx}"


def test_check_是别的垃圾类型也不许炸():
    """同一格的其它长相：数字 / 列表 / None —— 一律当"没给命令"，不许崩。"""
    for junk in (123, ["python3", "-m", "pytest"], None):
        arch = _arch()
        arch["constraints"][0]["check"] = junk
        out = decompose_architecture(arch)
        assert len(out) == 1, f"check={junk!r} 把拆解带崩了"
        assert "机器检查会真跑" not in out[0]["context_snippet"]
