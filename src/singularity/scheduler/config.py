"""集中配置 —— 路径 / 超时 / 阈值 / 快照上限

审计修了什么:
  - 所有魔数集中到此, 不散落在各模块 (修 I010 批的"同一结论散落多处")
  - pre_search 超时降级路径显式化 (审计 2.4)
  - 强 D 阈值可调 (审计 6.2)
"""

import logging
import os
import subprocess
import time
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
PAUSE_DIR = QIDIAN_DIR / "pauses"    # 暂停标记：逐步确认模式 / 手动暂停按钮 (GATE 不走这里)
PARKED_DIR = QIDIAN_DIR / "parked"   # 合并冲突 parking 持久化
# 执行中累计的 token（每次 dispatch 后落一盘）。
# 为什么需要它：超时被杀的任务**不走收尾记账**（`_archive_task_outcome` 到不了），
# 于是"烧了多少 token"整条丢掉。执行器自己那个累加值在线程里，超时方拿不到 ——
# 只能边跑边落盘。见 `docs/防御模式.md` §59。
PARTIAL_USAGE_DIR = QIDIAN_DIR / "partial_usage"
AGENTS_TOML = SCHEDULER_DIR / "agents.toml"  # stdlib tomllib, 不依赖 pyyaml

# ── 超时 ──────────────────────────────────────────────────────────────
PRE_SEARCH_TIMEOUT = 15.0     # 秒; 首次调用需加载句向量模型(24MB)+ChromaDB, 给足时间
VALIDATE_TIMEOUT = 30.0
GATE_TIMEOUT = 120.0          # eval.py 要跑 30 条 golden, 不快
CLAUDE_CLI_TIMEOUT = 300.0
ZHIPU_API_TIMEOUT = 240.0     # 代码生成任务长 (审计 5.3); 与 openai_agent 的 httpx 240s 对齐 — 慢模型出长 JSON 120s 不够

# ── 单任务总时限 ──────────────────────────────────────────────────────
# **两个地方共用这一个数**（以前 orchestrator 里硬编码 900，执行器不知道）:
#   · orchestrator._reap_futures —— 到点无声收割（写取消标记 + 判 FAILED）
#   · executor 自己（openai_agent）—— 提前 TASK_WRAPUP_MARGIN_S 收尾返回
# 分成两份写必然漂移；漂了就回到"干到被砍、砍完无账"（2026-09-13 复查的 8003）。
# 🔴 **验证轮要能跑得快**（2026-09-17 用户提：「修 bug 的代价太大了，每次都好长时间」）。
# 慢的不是改 bug（改+测+变异约 15 分钟），是**跑一轮**：任务挂着不动时，
# 每个都要**耗满这 900 秒**才死 ⇒ 8 个任务并发 2 ⇒ 光等就 60 分钟起。
# ⇒ 起后端时带上 `QIDIAN_TASK_DEADLINE_S=180`，一轮从 ~90 分钟压到 ~20 分钟。
# ⚠️ 这**只适合验证轮**（故意让任务早点死，好快点走到要验的那条路）；
#    跑真实产出别调小 —— 那是在砍正常任务的时间。
# ⚠️ 别调到 ≤ `TASK_WRAPUP_MARGIN_S`（90）：收尾余量会把执行预算吃光，
#    任务一点活都干不了，验证就变成"验证它什么都不干"。
TASK_DEADLINE_S = float(os.environ.get("QIDIAN_TASK_DEADLINE_S", "900"))
# 留给收尾/合并的余量: 执行器预算 = 900 - 90 = 810s。
# 执行器从自己起跑算，比 orchestrator 的 submit 时刻晚十几秒（建 worktree/text 预检），
# 90s 够盖住这点差 + 让 finalize 跑完。
TASK_WRAPUP_MARGIN_S = 90.0

# ── 模型输出额度 ──────────────────────────────────────────────────────
# 是保险丝，不是油门：模型按需用额度（实测 trivial 任务只烧 41/20000），
# 设小了只会把**正常**输出掐断 —— 6000/16000/20000 三档都实测截断过
# （融合稿要 2.8 万字 ≈ 3.5 万 token，初稿也在 20000 撞顶、JSON 断在中间）。
# 直接顶到端点自报的上限，取最紧的那家：智谱 [1,131072]（DeepSeek 到 393216）。
# 实测 5.5 万字输入 + max_tokens=393216 仍返回 200 —— "输入+输出共享窗口会被拒"
# 的担心不成立，所以不再留余量，撞顶就让它撞模型自己的天花板。
MODEL_MAX_TOKENS = int(os.environ.get("QIDIAN_MODEL_MAX_TOKENS", "131072"))

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
# 这是**文件级兜底**：分类器说 gate=false（或分类本身挂了）时，靠它兜住
# "改的明明是核心引擎文件"这一类（`validator._gate_check_by_files`，按**文件名**比）。
#
# ⚠️ `config.py` 是 2026-09-14 补的（外派 ⑨ 抓到、我核过）：分类器自己的 prompt
# 里写着它要 gate（`router._CLASSIFY_PROMPT`：核心引擎文件 = core/tokenizer/
# graph/search/**config**.py），而**这张表里没有它** ⇒ "分类挂 + 只改 config.py"
# 这个窄窗里 gate 真会被跳过。同一件事写两遍、改的时候只改一遍 —— 本仓的老形状。
GATE_TRIGGER_FILES = {
    "core.py", "tokenizer.py", "graph.py", "search.py", "config.py",  # 引擎核心
    "embedder.py", "hybrid.py", "ingest.py",  # 语义搜索
}
# ⚠️ 这儿原来还有个 `GATE_TRIGGER_DIR_PARTS = ("qidian-knowledge",)`，注释写着
# "路径含此段 + 上面文件才触发" —— 它**从 2026-06-23 加进来起就没有任何读者**
# （`_gate_check_by_files` 只比文件名、根本不看路径）。一个"看着在限定范围、
# 实际没生效"的常量比没有更坏：读的人会以为那条限制是活的。**已删**。
# 真要限定目录段，得先定"限定哪个仓的引擎文件" —— 那是设计决定，不是塞个常量。


def ensure_dirs() -> None:
    """启动前建好产物目录; 剩余空间不足 fail fast (审计 4.4)。"""
    import shutil

    memory_dir = QIDIAN_DIR / "memory"
    worktrees_dir = QIDIAN_DIR / "worktrees"
    for d in (QIDIAN_DIR, SNAPSHOT_DIR, PATCH_DIR, TRACE_DIR, HOLD_DIR, CANCEL_DIR, PAUSE_DIR, PARKED_DIR, memory_dir, worktrees_dir, PARTIAL_USAGE_DIR):
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


def runtime_identity() -> dict:
    """这个**跑着的进程**是谁 —— 代码版本 + 界面产物跟不跟得上。

    ## 为什么需要它

    2026-09-20 查清：`pyproject.toml` 里那个 `2.1.156` **全仓没有任何人读**
    （没有 `importlib.metadata` / `__version__` / 发布流水线 / `Dockerfile` / 界面显示），
    所以"真机跑的到底是哪一版"**在 git 层面不可回答**，只能拿
    `ps -o lstart= -p <pid>` 去和 `git log -1` 对时间 —— **间接、又容易看错**。
    真痛点不是"号写错了"，是**跑着的进程不知道自己是谁**。

    ## 两个字段各治一个病

    · `git` / `dirty_src` —— 「这轮代码是哪一版」。按轮打的 `round-*` tag 是唯一锚点：
      `round-20260920b-7-g455d2445` 里的 `7` = **这轮带着但没验过的修复有几笔**。
    · `dist_built` / `frontend_stale` —— 「界面产物跟不跟得上」。`dist/` 是构建产物
      且在 `.gitignore` 里，**改了 `frontend/src/` 不会改到界面、没有任何东西提醒你**
      （09-15 实锤：dist 停在 09-13，之后 17 个提交没进去，症状是"代码改了界面没变"、
      看着像"修复没生效"）。原来判据是**比时间戳**，这里把它换成**读事实**。

    ⚠️ **拿不到就回 `unknown`，不抛**：无 git 仓库 / 打包分发 / 竞态都算正常处境，
    为它让启动失败是本末倒置。整个函数**没有任何一条路径会抛异常**。

    ⚠️ **`dirty_src` 只看 `src/`**：`.qidian/` 是运行数据、天天在变，
    拿它当"脏"的话这个字段永远说"脏"，等于没说（本仓"常亮的假红换掉一个真红"那条）。
    """
    out: dict = {"git": "unknown", "dirty_src": None, "dist_built": None,
                 "frontend_stale": None}

    def _git(*args: str) -> str:
        # ⚠️ **不回退成静默**（本仓 `test_no_silent_except` 那台守卫会点它的名，点得对）：
        # "git 说不是仓库"（非零退出）是**正常处境**，字段回 unknown 就是答案、不用出声；
        # 而"git 压根起不来 / 超时"（抛异常）是**另一回事**，得留一句 ——
        # 不然两种处境在盘上长得一模一样，这正是本仓反复吃亏的「没走到 ≠ 修好了」。
        try:
            p = subprocess.run(["git", *args], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, timeout=5)
            return p.stdout.strip() if p.returncode == 0 else ""
        except Exception as e:
            logging.getLogger(__name__).info(
                "runtime_identity: git %s 起不来（%s）—— 这一项回 unknown", args[0], e)
            return ""

    desc = _git("describe", "--tags", "--always")
    if desc:
        out["git"] = desc
        out["dirty_src"] = bool(_git("status", "--porcelain", "--", "src/"))

    dist = PROJECT_ROOT / "src" / "singularity" / "web" / "static" / "dist"
    if dist.is_dir():
        try:
            newest = max((f.stat().st_mtime for f in dist.rglob("*") if f.is_file()),
                         default=None)
        except OSError as e:
            # 读不动 dist（权限 / 竞态 / 被删）⇒ 这一项回 unknown，但**别不出声**：
            # 否则"界面产物落后"和"压根没读到"会共用一个 `None`，分不开。
            logging.getLogger(__name__).info(
                "runtime_identity: dist 目录读不动（%s）—— 这一项回 unknown", e)
            newest = None
        if newest is not None:
            out["dist_built"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
            fe_commit = _git("log", "-1", "--format=%ct", "--",
                             "src/singularity/web/frontend/src")
            if fe_commit.isdigit():
                # ⚠️ **方向**：dist 比前端源码最后一次提交**旧** = 落后（要重新 build）。
                #
                # ⚠️ **要留秒级余量**（写这行的当天就踩到）：git 的 `%ct` 是**整秒**、
                # 文件 mtime 有小数位，"提交完紧接着构建"这两者会**差几秒**，
                # 严格 `<` 于是把刚构建完的 dist 报成落后 —— 而"常亮的假红"正是
                # 这个字段想避免的东西（喊狼来了几次之后，真的落后也没人看了）。
                # 取 90 秒：够盖住"构建→提交"和"提交→构建"两个方向的手抖，
                # 而真正要抓的那种落后是**以小时/天计**的（09-15 那次差了两天）。
                out["frontend_stale"] = newest < int(fe_commit) - 90
    return out


def missing_deps() -> list:
    """环境自检: 返回核心依赖中无法 import 的包名 (跑任务/测试必需)。"""
    import importlib
    missing = []
    for m in ("pytest", "httpx", "flask"):
        try:
            importlib.import_module(m)
        except ImportError:
            missing.append(m)
    return missing
