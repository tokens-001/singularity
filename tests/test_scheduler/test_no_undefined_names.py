"""未定义名守卫（F821）—— 2026-09-11 一天被同一个形状咬了两次。

两次的形状**一模一样**：用了没 import 的名字 → `NameError` →
被 `except Exception` 吞掉 → 静默降级成一个"看起来在工作"的默认值：

  · `_memory_core._get_embed_model` 用 `SentenceTransformer` 但没 import
    → 嵌入路径**从来没生效过**（技能过滤 / 记忆语义检索双双退化）
  · `execution_judge._usable` 用 `model_registry` 但只 import 了 `api_store`
    → "提取员可用性检查"**从来没生效过**（每次融合都白撞）

两次都表现为"功能一直在跑、结果一直是正常的"—— 因为降级默认值恰好等于旧行为。
**读码发现不了**（代码看着是完整的），静态检查一眼就能扫出来。

同一天还扫出两个**会真炸**的（已修，不是静默型）：
  · `_api_admin.cleanup` 裸调 `_cleanup_orphan_worktrees`（定义在 `_api_monitor`）
    → `POST /api/cleanup` 必 500
  · `_cli_tasks` 用 `_LOOP_POLL_SECS`（定义在单向 import 它的 `__main__` 里）
    → CLI `loop` 一空闲就崩

守这条比守"记得写 import"可靠。判据是**行为**（真跑一次检查器），不是文本。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "singularity"

# 显式豁免：命中 F821 但**不会**造成运行时报错的地方。
# 每一条都要写清"为什么无害"—— 这个列表长一寸，守卫就钝一分。
ALLOWED = {
    # 半成品死模块：`Dispatcher` / `TaskTracker` 这两个类**全仓根本没写过**，
    # 只出现在它的类型注解里。文件顶部有 `from __future__ import annotations`
    # → 注解不求值，所以不炸。生产代码无人 import 它（只有测试引用）。
    # ponytail: 要么补完要么删，别让豁免长期挂着 —— 见 docs/防御模式.md 的判据。
    "observer/state_sampler.py",
}


def _ruff_f821() -> list[str]:
    ruff = Path(sys.executable).parent / "ruff"
    if not ruff.exists():
        pytest.skip("ruff 没装（pyproject 的 dev 依赖里有；pip install -e '.[dev]'）")
    r = subprocess.run([str(ruff), "check", "--select", "F821", "--no-cache",
                        "--output-format=concise", str(SRC)],
                       capture_output=True, text=True, timeout=120)
    return [l for l in r.stdout.splitlines() if "F821" in l]


def test_no_undefined_names_in_src():
    """src/ 里不能有**未豁免**的未定义名。"""
    bad = []
    for line in _ruff_f821():
        path = line.split(":", 1)[0]
        rel = str(Path(path).resolve().relative_to(SRC))
        if rel not in ALLOWED:
            bad.append(line)
    assert not bad, (
        "出现未定义名（F821）。先确认它是不是被 `except Exception` 吞掉的：\n"
        + "\n".join("  " + b for b in bad)
        + f"\n\n确实无害才能加进 ALLOWED，并写清理由（当前豁免 {len(ALLOWED)} 处）。"
    )


def test_allowlist_has_no_stale_entries():
    """豁免名单不能烂：已经修好的条目要摘掉，否则它会一直掩盖真问题。"""
    hits = {str(Path(l.split(":", 1)[0]).resolve().relative_to(SRC)) for l in _ruff_f821()}
    stale = ALLOWED - hits
    assert not stale, f"这些豁免已经不再命中 F821 了，从 ALLOWED 里删掉: {stale}"
