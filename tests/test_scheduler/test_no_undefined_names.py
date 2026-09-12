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
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "singularity"

# 显式豁免：命中 F821 但**不会**造成运行时报错的地方。
# 每一条都要写清"为什么无害"—— 这个列表长一寸，守卫就钝一分。
#
# 现在是**空的**，这是好事：唯一那条（`observer/state_sampler.py`）已于 2026-09-12
# 删除。它当时是半成品死模块 —— 数据源 `Dispatcher`/`TaskTracker` 全仓根本没写过，
# 采样全靠 `getattr(obj, x, 0)` 兜底，产出恒为全 0 的结构体；任务/队列那部分也已被
# `/api/status` + witness 的真实数据替代。判据见记忆里的「死代码处理标准」：
# 重复/被替代 → 删。
#
# **别让它再长回来**：新增豁免必须写清"为什么无害"，并确认它不会烂在这儿
# —— `test_allowlist_has_no_stale_entries` 会揪出修好的旧条目。
ALLOWED: set[str] = set()


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


# ═══════════════════════════════════════════════════════════════
# 盲区补丁：星号 import 会让整个文件的 F821 变成瞎子
# ═══════════════════════════════════════════════════════════════
# 2026-09-13 发现：上面那条守卫**漏掉了它本该抓的形状**。
# `_memory_consolidator.py` 开头一句 `from _memory_core import *`，ruff 解析不了
# 命名空间，于是**对整个文件不报 F821**。把那行摘掉，同一个文件立刻报 7 行 ——
# 其中 3 个名字是**真的没导入**（`auto_maintain` / `system2_extract` /
# `add_inferred_causal_edge`，占 4 行），全部被 `except Exception` 吞成 `consolidate:` 告警，
# 真机 alerts.jsonl 里已经报过 6 次，功能**从来没跑过**。
# 形状跟守卫开头写的那两次一模一样 —— 区别只是它藏在星号 import 后面。
# src 下有 10 个文件带星号 import，就是这么大的盲区。

def _star_import_files() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py"))
            if re.search(r"^from \S+ import \*", p.read_text(encoding="utf-8"), re.M)]


def _f821_with_star_stripped(p: Path) -> list[str]:
    """摘掉星号 import 后再跑一次 F821（走 stdin，不落临时文件）。

    摘掉之后它会**多报** —— 既报真未定义名，也报"本来是星号 import 供的"那些。
    所以调用方必须再用 `hasattr(真实模块, 名字)` 分一次类：星号 import 早就执行过了，
    供不上就是**真的供不上**。
    """
    ruff = Path(sys.executable).parent / "ruff"
    if not ruff.exists():
        pytest.skip("ruff 没装")
    stripped = re.sub(r"^from \S+ import \*.*$", "",
                      p.read_text(encoding="utf-8"), flags=re.M)
    r = subprocess.run([str(ruff), "check", "--select", "F821", "--no-cache",
                        "--output-format=concise", "--stdin-filename", str(p), "-"],
                       input=stripped, capture_output=True, text=True, timeout=120)
    return [l for l in r.stdout.splitlines() if "F821" in l]


def test_star_import_files_have_no_truly_undefined_names():
    """带星号 import 的文件里，也不许有**真的**未定义名。"""
    bad: list[str] = []
    for p in _star_import_files():
        rel = p.relative_to(SRC).with_suffix("")
        modname = "singularity." + ".".join(rel.parts)
        try:
            mod = importlib.import_module(modname)
        except Exception as e:                      # noqa: BLE001
            bad.append(f"{p}: 模块本身 import 就失败了: {type(e).__name__}: {e}")
            continue
        for line in _f821_with_star_stripped(p):
            m = re.search(r"F821 Undefined name `([^`]+)`", line)
            if m and not hasattr(mod, m.group(1)):
                bad.append(f"{p}: `{m.group(1)}` 星号 import 也供不上 —— 真·未定义名")
    assert not bad, (
        "带星号 import 的文件里有真·未定义名（F821 的盲区）。\n"
        "调用时必抛 NameError，多半会被 `except Exception` 吞成一条告警。\n"
        + "\n".join("  " + b for b in bad)
    )


def test_consolidate_memory_runs_without_nameerror(tmp_path, monkeypatch):
    """上面那条是**静态**判据（源码能不能解析出名字）；这条是**行为**判据。

    真跑一遍 `consolidate_memory()` 的三条支路（重活 / 抽象回填 / 潜因果边），
    假装它们都正常返回，然后断言**没有** `... is not defined` 这类告警。
    修复前这里必然红：`auto_maintain` / `system2_extract` / `add_inferred_causal_edge`
    三个都取不到名字，分别被 `except Exception` 吞成 `consolidate:name '...' is not defined`
    —— 真机 alerts.jsonl 里就是这么报的。
    """
    from singularity.scheduler import config
    from singularity.scheduler import _memory_consolidator as mc
    from singularity.scheduler import _memory_lifecycle as ml
    from singularity.scheduler import _memory_graph as mg

    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    monkeypatch.setattr(mc, "_heavy_due", lambda: True)
    monkeypatch.setattr(mc.consolidate_memory, "_last_run", 0, raising=False)
    monkeypatch.setattr(mc, "_consolidate_calls", 1)
    monkeypatch.setattr(ml, "auto_maintain", lambda: {"pruned": 0})
    monkeypatch.setattr(ml, "system2_extract", lambda: {"added": 0, "insights": []})
    monkeypatch.setattr(mc, "backfill_abstractions", lambda **kw: 0)
    monkeypatch.setattr(mg, "find_candidate_latent_edges", lambda: [])

    warns: list[str] = []
    monkeypatch.setattr(mc.witness, "warn", lambda scope, msg, **kw: warns.append(str(msg)))
    mc.consolidate_memory()
    bad = [w for w in warns if "is not defined" in w]
    assert not bad, f"还是未定义名（用户看不到，只留一条告警）: {bad}"
