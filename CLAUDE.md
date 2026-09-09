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

- `pytest tests/test_scheduler/ -q`（306 个，~0.9s，全绿基线）
- `python3 tests/test_exec_run.py`（`_exec.run()` 退出路径，桩测试不碰真 API）
- `python3 tests/smoke_test.py`（走 HTTP，需先 `python3 -m singularity.web.app` 起后端）
