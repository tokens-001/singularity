# 奇点（Singularity）项目

> 详细文档 → `docs/项目速查.md`

## 项目地图（冷启动速查）

**一句话**：AI Agent 调度平台，多模型按角色协作完成软件开发任务。数据都在 `.qidian/` 下（JSON 文件，零数据库，可 grep 排查）。

### 模块 → 文件

| 模块 | 文件 |
|------|------|
| 调度循环 / 阶段流转 | `scheduler/orchestrator.py` |
| 任务状态机 + 持久化 | `scheduler/tracker.py`（→ `.qidian/tasks/`） |
| 执行器核心（`run()` 最复杂） | `scheduler/_exec.py` |
| 分发到 executor | `scheduler/dispatcher.py` + `_dispatch_*.py` |
| GATE 审查 | `scheduler/validator.py` |
| worktree 生命周期 | `scheduler/_git_worktree.py` + `_worktree.py` |
| merge / 冲突 | `scheduler/merge.py` |
| 任务 API handler | `scheduler/_api_tasks.py` |
| 目录常量 | `scheduler/config.py` |
| Web 前端 | `web/app.py` (Flask) + `web/frontend/` (React) |

### 改 bug 先看哪

- 调度超时 / 轮次 / dispatch 次数 → `_exec.py` + `dispatcher.py` + `orchestrator.py`
- 任务残留 / 删除 / 状态 → `_api_tasks.py` + `config.py` + `tracker.py`
- worktree / merge 残留 → `_git_worktree.py` + `_worktree.py`

**动手前**：改状态机 / 门禁 / worktree 路径 / 子进程 / 清理 / 模型调用前，先扫一遍 `docs/防御模式.md`（症状 → 根因 → 规则）。
**踩了 P0/P1**：按 `docs/postmortem-模板.md` 复盘，防护措施要可执行，不是「注意点」。

### 验证

- **`make check`**（= `lint` + `test-fast`，退出码 0 = 全绿）—— **本地判据，和 CI 同一条 lint 命令**。
  ⚠️ 2026-09-19 才修好：它原来调裸 `python3`/`ruff`，而系统 python3 没装 singularity、
  `ruff` 只在 `.venv/bin/` ⇒ **这条"完成判据"从来跑不起来**（审计见 `docs/CI与发布审计-20260919.md`）。
- `pytest tests/test_scheduler/ -q`（**~48s**，1670 条；全量 `pytest tests/` **1677 条**）
  （⚠️ 2026-09-19 更正：原来这儿写"~11s / 全量 823"，是旧的）
- `.venv/bin/python tests/test_exec_run.py`（`_exec.run()` 退出路径，桩测试不碰真 API）
- `.venv/bin/python tests/test_review_gate.py`（门禁能不能看到改动 —— 真 git + 真快照，不 mock；桩测试测不出这个时序）
- `.venv/bin/python tests/smoke_test.py`（走 HTTP，40 项；需先 `.venv/bin/python -m singularity.web.app` 起后端）
- 🔴 **前端源码改完必须重建**：`cd src/singularity/web/frontend && npm run build`（`tsc && vite build` → `static/dist/`）

> 🔴 **最后那条最容易漏，2026-09-15 真机验证时实锤**：后端**服务的是构建产物**
> （`app.py` 读 `static/dist/index.html`），而 **`static/dist/` 在 `.gitignore` 里**
> —— 改了 `frontend/src/` **不会**改到界面，也**没有任何东西会提醒你**。
> 实测那次：`dist` 停在 **09-13 19:53**，之后**前端源码动了 17 个提交**都没进去
> （含"任务卡把阻断渲染成绿勾"、"SSE 断了不重连"、"useRun 吞响应"…）。
> **症状是"代码明明改了、界面一点没变"** —— 会被误判成"修复没生效 / 功能是死的"。
> 判据：`ls -la src/singularity/web/static/dist/assets/ | head -1` 的时间
> **必须晚于** `git log -1 --format=%ad -- src/singularity/web/frontend/src/`。

> 后三条必须用 venv 解释器：系统 `python3`（homebrew 3.14）没装 singularity，直接跑会 `ModuleNotFoundError`。
> `pytest` 那条不受影响 —— `pyproject.toml` 给 pytest 配了 `pythonpath`。

⚠️ **dev 依赖要装**：`.venv/bin/pip install -e '.[dev]'`（至少 `ruff`）。
不装的话 `tests/test_scheduler/test_no_undefined_names.py` 里**三条 F821 闸门会静默 SKIP** ——
它们正是 2026-09-14 §64 那个「用了没导入的名字（被 `import *` 挡住 ruff）」事故之后加的守卫，
**跳过就等于没装**。2026-09-14 实测：本机 ruff 一直没装，那三条**从未运行过**、
而 pytest 汇总只显示 "3 skipped"，没人会去看。（外派⑬ 报的，已装并复跑：6 条全绿。）

> ⚠️ **本机 `pypi.org` 不通**（curl 返回 000；阿里云镜像 200）⇒ `pip install` 会报
> "No matching distribution found"。要装东西加 `-i https://mirrors.aliyun.com/pypi/simple/`。
> 2026-09-19 实测：`pytest-cov` / `mypy` / `pre-commit` 之所以一直没装上，就是因为这个。

> ✅ **CI 的门 2026-09-19 才第一次全绿**（此前 120 次运行全 failure，红在第 2 步 ruff，
> 后面 `pytest` 那几步**每次都被 skip** —— 也就是说"测试全绿"以前从没被第三方复核过）。
> 现在 `.github/workflows/test.yml` 是 `lint` / `test` **两个 job**，别合回去。
> lint 债已清到 0，四条豁免在 `pyproject.toml` 里、**每条都写了具体理由**。
> **别为了让门变绿改成 `|| true` / `continue-on-error`** —— 那是把红藏起来（§78：假门比没门坏）。
