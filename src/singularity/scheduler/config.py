"""集中配置 —— 路径 / 超时 / 阈值 / 快照上限

审计修了什么:
  - 所有魔数集中到此, 不散落在各模块 (修 I010 批的"同一结论散落多处")
  - pre_search 超时降级路径显式化 (审计 2.4)
  - 强 D 阈值可调 (审计 6.2)
"""

import os
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[3]  # Singularity/
SCHEDULER_DIR = Path(__file__).resolve().parent

# I 层引擎
ENGINE_DIR = PROJECT_ROOT / "data" / "knowledge"
SEARCH_SCRIPT = ENGINE_DIR / "scripts" / "search.py"
VALIDATE_SCRIPT = ENGINE_DIR / "scripts" / "validate.py"
EVAL_SCRIPT = ENGINE_DIR / "scripts" / "eval.py"

# 快照与产物
QIDIAN_DIR = PROJECT_ROOT / ".qidian"
# 项目成品根目录（默认 home 下，可用 QIDIAN_PROJECTS_ROOT 覆盖）
PROJECTS_ROOT = Path(os.environ.get("QIDIAN_PROJECTS_ROOT", "") or (Path.home() / "qidian-projects"))
SNAPSHOT_DIR = QIDIAN_DIR / "snapshots"
PATCH_DIR = QIDIAN_DIR / "patches"  # E+ 智谱产出暂存, apply 前不落盘 (审计 6.5)
TRACE_DIR = QIDIAN_DIR / "traces"
HOLD_DIR = QIDIAN_DIR / "holds"      # 人工扣留标记
CANCEL_DIR = QIDIAN_DIR / "cancels"  # 取消标记
PAUSE_DIR = QIDIAN_DIR / "pauses"    # 暂停标记 (GATE 人审用)
PARKED_DIR = QIDIAN_DIR / "parked"   # 合并冲突 parking 持久化
AGENTS_TOML = SCHEDULER_DIR / "agents.toml"  # stdlib tomllib, 不依赖 pyyaml

# ── 超时 ──────────────────────────────────────────────────────────────
PRE_SEARCH_TIMEOUT = 15.0     # 秒; 首次调用需加载句向量模型(24MB)+ChromaDB, 给足时间
VALIDATE_TIMEOUT = 30.0
GATE_TIMEOUT = 120.0          # eval.py 要跑 30 条 golden, 不快
CLAUDE_CLI_TIMEOUT = 300.0
ZHIPU_API_TIMEOUT = 120.0     # 代码生成任务长 (审计 5.3)

# ── 阈值 ──────────────────────────────────────────────────────────────
# 强 D 命中: pre_search 返回 decision 域前 3 条里 >=2 条 score 超此值 → 升 D (审计 6.2)
STRONG_D_MIN_SCORE = 15.0
STRONG_D_MIN_HITS = 2
STRONG_D_TOPK = 3

# ── 重试 / 打回 ───────────────────────────────────────────────────────
DEFAULT_MAX_TURNS = 2          # validate 打回上限
ARCHITECT_FALLBACK_FAILS = 3   # 同 agent 连续失败 → 升 architect (审计 6.3)

# zhipu API 限流重试 (审计 5.3): 429 指数退避, 不计入 max_turns
ZHIPU_MAX_RETRIES = 3
ZHIPU_BACKOFF_BASE = 1.0       # 1s, 2s, 4s

# ── 快照 ──────────────────────────────────────────────────────────────
MAX_SNAPSHOTS = 5              # 文件拷贝兜底时保留最近 N 个 (审计 4.3)
MIN_DISK_MB = 500              # 启动前剩余空间检查 (审计 4.4)

# ── ChromaDB 语义搜索 ──────────────────────────────────────────────────
CHROMA_DIR = ENGINE_DIR / "data" / "chroma"

# ── gate (审计 1a: 引擎文件改动强制回归) ──────────────────────────────
GATE_TRIGGER_FILES = {
    "core.py", "tokenizer.py", "graph.py", "search.py",  # 引擎核心
    "embedder.py", "hybrid.py", "ingest.py",  # 语义搜索
}
GATE_TRIGGER_DIR_PARTS = ("qidian-knowledge",)  # 路径含此段 + 上面文件才触发


def ensure_dirs() -> None:
    """启动前建好产物目录; 剩余空间不足 fail fast (审计 4.4)。"""
    import shutil

    MEMORY_DIR = QIDIAN_DIR / "memory"
    WORKTREES_DIR = QIDIAN_DIR / "worktrees"
    for d in (QIDIAN_DIR, SNAPSHOT_DIR, PATCH_DIR, TRACE_DIR, HOLD_DIR, CANCEL_DIR, PAUSE_DIR, PARKED_DIR, MEMORY_DIR, WORKTREES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(QIDIAN_DIR).free / (1024 * 1024)
    if free < MIN_DISK_MB:
        raise RuntimeError(
            f"磁盘剩余 {free:.0f}MB < {MIN_DISK_MB}MB, 拒绝启动 (审计 4.4)"
        )

    # 清理上次崩溃残留
    try:
        from singularity.scheduler._git_worktree import cleanup_orphans
        n = cleanup_orphans()
        if n:
            import logging
            logging.getLogger("scheduler").info(f"cleaned {n} orphan worktrees from previous run")
    except Exception as _e:
        logging.getLogger(__name__).warning("worktree cleanup failed: %s", _e)


def missing_deps() -> list:
    """环境自检: 返回核心依赖中无法 import 的包名 (跑任务/测试必需)。"""
    import importlib
    missing = []
    for m in ("pytest", "psutil", "httpx", "flask"):
        try:
            importlib.import_module(m)
        except ImportError:
            missing.append(m)
    return missing
