"""把「防御模式」从教材变成审查员的**找茬清单**（分析 P5 的变体）。

原方案是"按任务类型匹配 2-3 条注入 prompt"。逐条过了一遍 58 条：**绝大多数是写给
"改平台代码的人"的**（`#8 SSE 不清理队列`、`#13 跨线程共享状态`、`#53 记账覆盖面和
调用点`……）—— 跑在流水线里的模型读这些没用，它不写平台代码。

**唯一站得住的用法**：那些条目的「**症状**」是失败长相，当**检查表**交给审查员 ——
不是"你别这么干"，是"**看看有没有长这样**"。

⚠️ 有没有用没验证。
"""
import pytest

from singularity.scheduler import _defense_patterns as dp


DOC = """# 防御模式

## 一、某节

### 1. 用 unlink 删链接形态的路径
- **症状**：删符号链接把目标文件删掉了。
- **规则**：先 lstat 再决定。

### 2. 凭证进仓库
- **症状**：API key 被提交进 git。
- **规则**：走环境变量。

### 28. API handler 返回 200 却没接真实动作
- **症状**：接口返回成功但什么都没发生；或者调用必 500。
- **规则**：返回 200 之前先确认真动手了。
"""


@pytest.fixture
def stub_doc(tmp_path, monkeypatch):
    p = tmp_path / "防御模式.md"
    p.write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(dp, "_doc_path", lambda: p)
    return p


class TestParse:
    def test_parses_entries(self, stub_doc):
        ps = dp.load_patterns()
        assert [x["id"] for x in ps] == ["1", "2", "28"]
        assert ps[2]["symptom"].startswith("接口返回成功")

    def test_missing_doc_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dp, "_doc_path", lambda: tmp_path / "没有.md")
        assert dp.load_patterns() == []

    def test_bad_format_returns_empty_not_garbage(self, tmp_path, monkeypatch):
        """格式变了 → 返回空，**不半解析出些错条目**。"""
        p = tmp_path / "x.md"
        p.write_text("# 随便什么\n\n没有 ### 也没有症状\n", encoding="utf-8")
        monkeypatch.setattr(dp, "_doc_path", lambda: p)
        assert dp.load_patterns() == []

    def test_real_doc_parses(self):
        """真文档能解析（它变了这个测试会红，提醒去看是不是改版式了）。"""
        ps = dp.load_patterns()
        assert len(ps) >= 50, f"真文档只解析出 {len(ps)} 条"
        assert all(x.get("symptom") for x in ps)


class TestPick:
    def test_picks_the_matching_one(self, stub_doc):
        hits = dp.pick_for("web/app.py 新加一个 endpoint，handler 返回 200")
        assert hits and hits[0]["id"] == "28"

    def test_unrelated_context_gets_nothing(self, stub_doc):
        """**宁可不给，别硬凑** —— 挑不中就该是空，不是"最不坏的三个"。"""
        assert dp.pick_for("React 组件 样式 布局 颜色 字体") == []

    def test_empty_context_gets_nothing(self, stub_doc):
        assert dp.pick_for("") == []
        assert dp.pick_for("   ") == []

    def test_caps_at_max_items(self, stub_doc, monkeypatch):
        monkeypatch.setattr(dp, "_MIN_COS", 0.0)
        monkeypatch.setattr(dp, "_MIN_OVERLAP", 1)
        hits = dp.pick_for("删 链接 路径 凭证 仓库 接口 返回 200 动作", n=10)
        assert len(hits) <= dp.MAX_ITEMS, "硬上限：prompt 膨胀是这条自带的险"


class TestChecklist:
    def test_renders_with_symptom(self, stub_doc):
        c = dp.checklist("新加 handler 返回 200")
        assert "挑毛病时留意" in c and "接口返回成功" in c

    def test_no_match_gives_empty_string(self, stub_doc):
        """挑不中 → **什么都不加**，别占 prompt 预算。"""
        assert dp.checklist("React 组件 样式 布局") == ""


class TestSingleSourceOfTruth:
    def test_reads_the_repo_doc_not_a_copy(self):
        """单一事实源：直接读仓库里那份文档。

        **不复制一份进代码** —— 本项目反复吃过"文档和代码各存一份、然后悄悄漂移"的亏。
        """
        from singularity.scheduler import config
        assert dp._doc_path() == config.PROJECT_ROOT / "docs" / "防御模式.md"

    def test_path_is_computed_at_call_time(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "one")
        assert dp._doc_path() == tmp_path / "one" / "docs" / "防御模式.md"
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "two")
        assert dp._doc_path() == tmp_path / "two" / "docs" / "防御模式.md"
