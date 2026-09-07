# executor 写文件链修复记录

> 2026-09-07，commit `049ec04` + `7ae49ec`。记录「流水线验证时 executor 写不出代码」的 8 个叠加 bug 和修复。

## 一句话结论

「写不出代码」不是一个 bug，是一条 **8 个 bug 叠加的链**——thinking 模型（deepseek-v4-pro）拒绝 `tool_choice=required` → 降级 auto 后「光说不做」→ 就算写了文件也被 git 追踪漏掉 → worktree 的 `.git` 指针被删导致 git 解析错位。逐个修完，executor 现在能写文件了；但流水线仍跑不通，剩模型过度测试撞超时。

## 8 个 bug + 修复

| # | 层 | bug | 修法 | 文件 |
|---|----|-----|------|------|
| 1 | 工具调用 | thinking 模型拒 `tool_choice=required`（HTTP 400「Thinking mode does not support this tool_choice」）→ 降级 auto 后光说不做 | 降级时注入「必须调工具」指令 | `openai_agent.py` |
| 2 | 文件追踪 | `_track_changed_files` 用 `git diff --name-only` 漏 untracked 新文件（模型用 run_command heredoc 写的新文件抓不到） | 改 `git status --porcelain` + 过滤 `__pycache__` | `openai_agent.py` |
| 3 | 路径 | `write_file` 的 `/tmp→/private/tmp` 符号链接致 `relative_to` 炸 | `self._cwd` 也 `.resolve()` | `openai_agent.py` |
| 4 | 返回 | max_turns 错误路径漏掉 `changed_files`（文件写了结果不带） | 有文件就算 `success=True` | `openai_agent.py` |
| 5 | 钩子 | `post_execution_hook` 返回 dict 被当 list join（"warnings, quality_signals..." 是 dict keys） | 取 `["warnings"]` | `_exec.py` |
| 6 | 误杀 | validator 裸 `DROP\s+TABLE`/`DELETE\s+FROM` 拦了合法数据库 CRUD | 收窄成 `';\s*(DROP TABLE\|DELETE FROM)` 注入特征 | `validator.py` |
| 7 | 定位 | worktree 落在 singularity `.qidian/worktrees/`（主仓库内），任务执行时 `.git` 指针被删，git 解析到主仓库漏文件 | worktree 挪到 repo 同级 `.{name}-worktrees` | `_git_worktree.py` / `_worktree.py` |
| 8 | 命令 | `run_command` 用 `shell=False`，模型 shell 命令的 `&&`/`source` 被当参数生成 `&&`/`source` 垃圾目录 | `shell=True`（危险命令黑名单仍前置拦截） | `openai_agent.py` |

## 验证结果

修完 8 个 bug 后，T1（database 模块）的 worktree 终于有 `database.py` + `task.db`（还跑起来建了库）+ 正确 `.git` 指针。

**但流水线仍跑不通**——新死法 `执行超时(>900s)`：thinking 模型写完后反复测试不收敛，5 内部轮 × ~150s ≈ 750s 撞超时。

## 结论（推测/确认分开）

- **确认**：代码层 8 个 bug 已修完，executor 能写文件了（282 测试绿）。
- **推测**：剩余卡在 deepseek-v4-pro（thinking 模式）慢 + 爱绕，要跑通得调参（`max_turns` 5→3 / 「测试通过即停」prompt）或换非 thinking 模型跑执行阶段。这是模型选型/调参问题，不是代码 bug。

## 坑（重要）

有未提交改动时跑任务，会被任务 merge 卷进 `agent changes in <task_id>` 提交（本次 `_exec.py`/`validator.py` 改动被卷进 `0fd8631`，soft reset 清掉重提）。
