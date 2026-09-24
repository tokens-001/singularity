"""交付物被清空 / 只写在回答里 —— 三层防线各一个钉子（2026-09-15 真机坐实）。

现场（项目 `1789481895784` 的任务 `…690`）：
· `a5978ef` 22:49:07 → `test_fizzbuzz.py` **+311 行**（模型写对了）；
· `1b4c8b2` 22:52:22 → **同一个任务自己删光**（文件还在、**0 字节**）；
· 模型收尾原话：「注意：磁盘上的 `test_fizzbuzz.py` 当前为 0 字节，以下代码块即为
  权威交付物…」—— **它看见了，但工具已被劝停，交不进去**；
· 最终 `validation.verdict = 通过` · `changed_files` 非空 · 任务 `done`。

三层全漏：写文件不挡空内容 · "停手"那句不说要落盘 · 验收只看"改没改"不看"有没有东西"。
"""
import pytest

from singularity.scheduler import supervisor
from singularity.scheduler.executors.openai_agent import _write_file


# ── 第一层：写文件的工具不许把非空文件清空 ──────────────────

def test_空内容不许覆盖非空文件(tmp_path):
    """**这条钉的就是真机那个 311 行 → 0 字节。**"""
    f = tmp_path / "test_x.py"
    f.write_text("def test_a():\n    assert 1\n" * 20, encoding="utf-8")
    before = f.stat().st_size

    out = _write_file({"path": "test_x.py", "content": ""}, tmp_path)

    assert "拒绝" in out, f"空内容覆盖必须被拒，实际返回：{out}"
    assert f.stat().st_size == before, "文件被清空了"
    assert "assert 1" in f.read_text(encoding="utf-8"), "内容被动过"


def test_空内容仍然可以新建空文件(tmp_path):
    """拦的只是"把已有内容抹掉"，不是"不许建空文件"（`__init__.py` 那种）。"""
    out = _write_file({"path": "pkg/__init__.py", "content": ""}, tmp_path)
    assert "已写入" in out
    assert (tmp_path / "pkg" / "__init__.py").exists()


def test_正常写入不受影响(tmp_path):
    out = _write_file({"path": "a.py", "content": "x = 1\n"}, tmp_path)
    assert "已写入 a.py (6 字符)" == out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


# ── 第一层（续）：闸门必须挂在**实盘真的会走**的那条路上（2026-09-25）───────
# 🔴 Qoder 外派审查 #2（`docs/Qoder-审查-20260924.md`）报的，我核过：
# 上面三条测的是 `_write_file`，而它**只有 anthropic 的执行器在用** ——
# 本部署三个 agent 全是 `openai-agent`（`.qidian/agents_custom.json` 实读）
# ⇒ **上面测的那条路，实盘一次都不走**。
# 这正是本仓反复栽的「测了函数，没测走的是不是它」：闸门装在一个没人的房间里。

def _openai_executor(tmp_path):
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
    return OpenAIAgentExecutor({"model": "m", "api_key_env": "K"}, "写文件", "tid_w",
                               cwd=str(tmp_path), skill_tools=[], mcp_tools=[])


def test_接线_装出来的执行器写空内容必须被拒(tmp_path):
    """**这条钉"实盘走的那条路"**：同一个工具名两套实现，闸门只加在没用的那套上。

    判据走**真的分发入口**（`_execute_tool`）而不是直接调 `_tool_write` ——
    `_execute_tool` 里那一跳（`name == "write_file"` 派给谁）正是会断的地方。
    变异：让 `_tool_write` 自己 `p.write_text(content)`（不派给共享实现）⇒ 这条红。
    """
    f = tmp_path / "test_x.py"
    f.write_text("def test_a():\n    assert 1\n" * 20, encoding="utf-8")
    before = f.stat().st_size

    out = _openai_executor(tmp_path)._execute_tool(
        "write_file", {"path": "test_x.py", "content": ""})

    assert "拒绝" in out, f"走 openai 这条路的写文件没有闸门，实际返回：{out}"
    assert f.stat().st_size == before, \
        "311 行 → 0 字节那个病，在 openai 这条路上还在（闸门装在了没人走的那个执行器上）"


def test_接线_正常写入照旧记进changed_files(tmp_path):
    """**这条是对照**：别为了挡空内容把正常写入一起挡了，而且 `changed_files` 照旧要记。"""
    ex = _openai_executor(tmp_path)
    out = ex._execute_tool("write_file", {"path": "a.py", "content": "x = 1\n"})

    assert "已写入" in out, out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert ex._changed_files == ["a.py"], ex._changed_files


# ── 第三层：验收把"全是空文件"等同于"空手回来" ──────────────

def test_改动文件全是空的算没产出(tmp_path):
    (tmp_path / "t.py").write_text("", encoding="utf-8")

    r = supervisor._check_completeness(["c1"], "干完了", ["t.py"], "写 t.py", tmp_path)

    assert not r.passed, "全是空文件必须判失败 —— 这正是漏过去的那一步"
    assert r.evidence.get("hard") is True, "要是软证据，只会 escalate，拦不住"


def test_有空文件但也有非空的照常放行(tmp_path):
    """多文件交付里个别空文件（`__init__.py`）是合法的，别误伤。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")

    r = supervisor._check_completeness(["c1"], "干完了", ["a.py", "__init__.py"], "写 a.py", tmp_path)

    assert r.passed


def test_接线_supervise真的把_root_传下去了(tmp_path):
    """**这条钉接线**：删掉 `supervise` 里 `_check_completeness(...)` 的最后一个实参，
    `root` 就是 None ⇒ `_all_empty` 直接返回 [] ⇒ 上面那条检查**静默失效**。

    只测 `_check_completeness` 自己验的是"函数对"，验不到"接线通"。
    """
    (tmp_path / "t.py").write_text("", encoding="utf-8")

    v = supervisor.supervise(
        task_description="写 t.py",
        changed_files=["t.py"],
        constraints=[],
        checklist=["c1"],
        agent_output="已按要求完成 t.py 的编写与自测。",
        repo_root=str(tmp_path),
    )

    assert v.verdict == "fail", f"0 字节的交付物必须判 fail，实际 {v.verdict}：{v.issues}"
    assert any("completeness" in i for i in v.issues), v.issues


def test_接线_对照组_非空产物不因这条判死(tmp_path):
    """别把这条检查写成"有文件就拦"——对照组必须照常通过。"""
    (tmp_path / "t.py").write_text("x = 1\n", encoding="utf-8")

    v = supervisor.supervise(
        task_description="写 t.py",
        changed_files=["t.py"],
        constraints=[],
        checklist=["c1"],
        agent_output="已按要求完成 t.py 的编写与自测。",
        repo_root=str(tmp_path),
    )

    assert not any("completeness" in i for i in v.issues), v.issues


# ── 第二层：收尾那句必须说清"交付物得落盘" ──────────────────

def test_收尾指令必须要求落盘():
    """**静态钉子**（同 `test_short_id` / fix_route 那两条）。

    那条注入的收尾指令是全仓**唯一"只劝、不硬撤工具"**的，模型最容易照做；
    它不说"交付物得在文件里"，模型就会像真机那样**把代码贴在回答正文里**收尾 ——
    而正文里的代码**永远不会被交付**。改措辞这种事测不了行为，就钉住这句话在不在。
    """
    from pathlib import Path
    src = Path(supervisor.__file__).with_name("executors").joinpath("openai_agent.py").read_text(encoding="utf-8")
    i = src.index("已收集足够信息。停止使用工具")
    # ⚠️ **两支分开钉**（2026-09-20 改）：这句现在**按任务分岔**（只读 / 普通）。
    # 原来是从第一处往后抓 400 字 —— 那样只会抓到只读那支，而"普通任务必须落盘"
    # 这句就**没人看着**了（删掉照样绿）。
    j = src.index("交付物必须以文件形式存在", i)
    assert "write_file" in src[j:j + 200] and "落盘" in src[j:j + 200], \
        "普通任务的收尾指令必须写清「先把交付物写进文件再收尾」，否则模型会只写在正文里"
    # 🔴 **只读那支要说反话**：它的协议（`config.READONLY_TAG`）就是"别改文件"，
    # 再叫它"交付物必须以文件形式存在"就是两条对着干 —— 真机 trace 里模型正是被
    # 这句逼得在"写"和"不写"之间反复横跳，把工具轮次烧光的。
    k = src.index("只读任务", i)
    assert "不要写任何文件" in src[k:k + 200], \
        "只读任务的收尾指令没说「别写文件」—— 它还会去写，而它的验收不认文件"
