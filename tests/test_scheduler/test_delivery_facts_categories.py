"""`delivery_facts.py` 的「按类」分栏 —— 钉住分类器那两条**会静默歪掉**的边界。

为什么值得单钉：这一行的存在理由是治"总分被最容易那栏稀释"这个假象，
**它自己歪了就会造出新的假象**，而且歪得很安静 —— 屏幕上照样整齐，
只是四轮全都分错栏（第一版按正文分类就是这么错的：实现任务描述里都带
「与 tests/test_filter.py」⇒ 整批被算成"写测试"）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import delivery_facts as df  # noqa: E402


@pytest.mark.parametrize("desc, want", [
    # ⚠️ 正文里带 tests/ 的**实现任务** —— 这是第一版判歪的地方，钉死它
    ("[T2] 实现 filter 模块：时间范围过滤: 创建 jsonstat/filter.py 与 tests/test_filter.py",
     "实现模块"),
    ("[T3] 实现 stats 模块：单遍分组聚合: 创建 jsonstat/stats.py 与 tests/test_stats.py",
     "实现模块"),
    ("[T5] 实现 cli 入口与管道接线: 创建 jsonstat/cli.py、jsonstat/__main__.py 与 tests/",
     "接线/集成"),
    ("[T6] 端到端集成测试与分层/依赖边界检查: 创建 tests/fixtures/",
     "写测试"),
    ("[T7] [只读] 独立验收：干净检出跑测试并逐条核对需求: 不改动任何文件",
     "验收"),
    # 没写 [Tn] 前缀 —— 别炸，也别落进空桶
    ("随手写的一条任务", "实现模块"),
])
def test_类别按标题判_不判正文(desc, want):
    assert df._category_of(desc) == want


def test_分栏的过用任务状态_不用进仓(tmp_path):
    """只读验收任务**按定义没有产物**（changed_files=[]），拿"进仓"当判据它永远是 0/1。

    真机实测（d 轮 T7）：状态 done、交付事实也对，而进仓那栏是 0。
    ⇒ 这一行必须认 `status == "done"`。**把实现改回按 merged 判，这条会红。**
    """
    ts = [
        {"id": "a", "status": "done", "description": "[T1] 实现 X 模块: 与 tests/test_x.py"},
        {"id": "b", "status": "done", "description": "[T8] [只读] 独立验收: 不改文件"},
        {"id": "c", "status": "failed", "description": "[T9] 输出模块单元测试: 创建 tests/"},
    ]
    line = df._category_line(ts)
    assert "实现模块 1/1" in line
    assert "验收 1/1" in line, f"只读验收任务被算成没过：{line}"
    assert "写测试 0/1" in line


def test_四栏覆盖每个任务_没有落空的桶():
    """分类器是"最后 else 兜底"，保证任何标题都落进四栏之一 ——
    落空会让 `agg[c]` KeyError，而它外面那层 try 会把整行**静默**降级成
    「⚠️ 集成读不出来」（函数对 ≠ 接线通那条：错了但不出声）。"""
    for desc in ["", "x", "[T1] 无标题冒号", "只读", "端到端"]:
        assert df._category_of(desc) in df._CATEGORIES
