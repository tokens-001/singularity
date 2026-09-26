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

### 改 bug 先看哪（按症状查）

> 下面文件名一律相对 `src/singularity/`；`scheduler/` 略写。
> **只写能 grep 到的锚，不写行号** —— 行号漂了看不出来，锚漂了一眼就发现。

**先分三类，别一上来就翻代码：**

| 盘上长什么样 | 大概率是什么 | 先干什么 |
|---|---|---|
| 同一个 key **反复出现**（告警页的"常驻"栏） | 判据跟配置脱节，**不是新事故** | 别读成"最近问题多"。先看它是不是长亮 |
| 一个 key 只冒一两次 | 真事件 | 查下面的 ① |
| **什么都不报，但就是不对** | 静默失败（本仓最常见的形状） | 查下面的 ② |

#### ① 告警 key → 谁发的、什么意思

> 这张表**能重新生成，别手抄**：`.venv/bin/python scripts/alert_map.py`。
> 条数是 09-26 那次扫盘快照，**只用来判"长亮还是新事"**，别当现在的数读。

| 条数 | key | 先看 | 它说的是什么 |
|---|---|---|---|
| 3180 | `drain_dep_blocked` | `scheduler/merge.py` | 依赖永远不满足 ⇒ 这一轮排不下去（🌟**长亮**，先怀疑它是不是常驻条件） |
| 386 | `decompose` | `_planner.py` · `orchestrator.py` | 立项/拆解那步出错（示例是 `'str' object has no attribute 'get'`） |
| 104 | `stream_over_budget` | `execution_judge.py` · `executors/openai_agent.py` | 流式调用超了预算还没吐完（消息里带 `cap=`） |
| 80 | `lazy_spoke_import_failed` | `dispatcher.py` | 延迟导入破了（09-18 修过 `importlib` 探 dunder 那版） |
| 80 | `xml_tool_calls_recovered` | `executors/openai_agent.py` | 这轮没工具、模型还是吐了 XML 工具调用 ⇒ 被捞回 |
| 80 | `collect_changes` | `executors/*.py` | 收改动时拿不到基线（`no_baseline_ref`）⇒ **判据不完整** |
| 56 | `designated_reviewer_is_writer` | `_review.py` | 指定的审查员就是写手 ⇒ 自审 |
| 51 | `cascade_skip` | `_exec.py` | 级联换模型（消息里带 `conf=`） |
| 44 | `reasoning_kept_for_tool_call_without_tools` | `executors/openai_agent.py` | 撤工具那轮保留 reasoning 的判据信号（见 §85） |
| 36 | `observer_stalled_task` | `_observer_worker.py` | 观察者判某任务停滞 —— ⚠️ key 是拼出来的，见下面那条 |
| 35 | `tool_choice_required_rejected` | `executors/openai_agent.py` | `tool_choice=required` 被端点拒（HTTP 400，思考模式不兼容） |
| 34 | `phase_drift` | `orchestrator.py` | phase 和 lineage 对不上 ⇒ **有地方绕过 `set_phase` 改了状态** |
| 16 | `constraints_checklist_fallback` | `project.py` | 约束清单为空但架构里有条目 ⇒ 走兜底（§60 覆盖源仍未定位） |
| 16 | `llm_spin_no_output` | `executors/openai_agent.py` | 整轮只想不产出（消息带 `reasoning=`/`loops=`）⇒ 白烧预算 |
| 14 | `abstraction_backlog` | `_memory_consolidator.py` | 记忆里积压的抽象条目超阈值 |
| 12 | `degraded_dependency` | `workflow.py` | 依赖降级处理 |
| 11 | `integration_cases_missing` | `orchestrator.py` | 架构声明了集成用例，但没有匹配到测试 |
| 11 | `review_files_truncated` | `_review.py` | 送审的文件被截断（`4->3`）⇒ 审查没看到全部改动 |
| 9 | `task_file_overlap` | `workflow.py` | 任务之间的文件重叠 |
| 8 | `qa_verdict_missing` | `workflow.py` | 读不到 QA 判词 ⇒ **小心被当"通过"读** |
| 7 | `test_cases_missing_in_arch` | `_workflow_phases.py` | 架构没给 `test_cases` ⇒ 集成/E2E 清单注定是空的 |
| 6 | `integration_no_tests` | `orchestrator.py` | 项目里一条集成测试都没有 |
| 6 | `plans_truncated` | `execution_judge.py` | 计划被截断（消息里是字节数） |
| 6 | `review_pool_expanded` | `_review.py` | 审查池被扩容（可用的席位不够） |
| 6 | `committee_not_engaged` | `_workflow_phases.py` | 委员会没凑够席位（`seats=1`） |
| 5 | `task_killed_no_wrapup` | `orchestrator.py` | 任务被外层砍掉、没来得及收尾 |
| 5 | `extract_retry` | `execution_judge.py` | 抽取失败换模型重试（融合那套） |
| 4 | `extractor_stays_member` / `extractor_swapped` / `fusion_self_judge` / `fusion_confirm_empty` / `fusion_confirm_unresolved` | `execution_judge.py` | **融合实验那一族**（抽取者还在席位上 / 被换掉 / 自评 / 确认轮空） |
| 4 | `unknown_fields_dropped` | `project.py` | 反序列化丢了字段（如 `passed,summary`）⇒ 静默 |
| 4 | `load_failed` | `project.py` · `_memory_core.py` | 盘上的 JSON 读不回来（示例：构造函数缺 2 个必填位置参数） |
| 3 | `reasoning_only` | `execution_judge.py` | 只有 reasoning 没有正文 |
| 2 | `project_deleted_left_tasks` | `_api_projects.py` | 删项目留下孤儿任务 |
| 2 | `stale_write` | `project.py` | 拿旧快照往回写 ⇒ **会抹掉这期间别人的改动** |
| 2 | `no_permission_checker` | `executors/base.py` | 执行器没接权限检查器 |
| 2 | `project_stopped` | `_api_projects.py` | 人点了停整轮 |

⚠️ **`observer_stalled_task` 这张表查不到"谁发的"，不是漏了**：它是 `f"observer_{kind}"` 拼出来的，
源码里既没有全串也没有可用的前缀。真发出点在 `_observer_worker.py`（消费
`_observer_client.py` 那句 `"kind": "stalled_task"`）。
⇒ **key 是拼出来的那类，`alert_map.py` 会明确报"查不到"，而不是指到一个看起来像的文件上。**

#### ② 不报错但不对

| 症状 | 先看 | 备注 |
|---|---|---|
| 任务卡在 running 却没人管 | `_task_runner.py` · `supervisor.py` | 🔴 `witness.check_stalled()` **调度器一次都不调**，只有观察者和一个 admin 接口在用 ⇒ 别指望它自己报 |
| 项目卡着不推进 | `orchestrator.py` | 先看 `.qidian/projects/<id>/` 的 phase 和 lineage |
| 调度超时 / 轮次 / dispatch 次数 | `_exec.py` · `dispatcher.py` · `orchestrator.py` | |
| 任务残留 / 删除 / 状态不对 | `_api_tasks.py` · `tracker.py` · `config.py` | 项目级是 `_api_projects.py` |
| worktree / merge 残留 | `_git_worktree.py` · `_worktree.py` · `merge.py` | |
| GATE 该拦没拦 / 该过没过 | `validator.py` · `_machine_checks.py` | 机器检查那层的形状见 §60 |
| 账（token / 费用）不对 | `_token_budget.py` · `model_prices.py` | 单价比模型表更容易被"整行重建"擦掉 |
| 模型调用 400 / 返回空 | `executors/openai_agent.py` · `.qidian/llm_400_unknown.jsonl` | 那个文件存**认不出来的 400 的请求体原文** |
| 代码改了、界面一点没变 | 前端没重建 ⇒ `web/frontend/` 改完要 `npm run build` | 判据是 `GET /api/status` 的 `identity.frontend_stale` |

**动手前**：改状态机 / 门禁 / worktree 路径 / 子进程 / 清理 / 模型调用前，先扫一遍 `docs/防御模式.md`（症状 → 根因 → 规则）。
**踩了 P0/P1**：按 `docs/postmortem-模板.md` 复盘，防护措施要可执行，不是「注意点」。

### 验证

- **`make check`**（= `lint` + `test-fast` + `audit-fast`，退出码 0 = 全绿）——
  **本地判据，和 CI 同一条 lint 命令**。
  ⚠️ 2026-09-20 加了 `audit-fast`（审计脚本的自测 + `preflight` 形状棘轮）。
  **`make audit`（全量）比 `make check` 多一条 `audit-preflight`** —— 那条要
  `git archive` 5 个历史 rev 整棵重扫（~100s），**只在 CI 跑**，
  刻意不放进日常那条（2 分钟的完成判据没人会跑）。**但它仍然是门，只是不在本机那条路上。**
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
> ✅ **判据 2026-09-20 换成机器读了**（原来要人自己 `ls -la` 比时间戳）：
> `GET /api/status` 的 `identity.frontend_stale`，启动日志里也有一行。
> ⚠️ **别再用旧的"比 `git log -1 --format=%ad`"那条** —— 它是**错的**，写那个字段的当天
> 就被咬了一次：正常顺序是"改 → `npm run build` → 提交"，**提交必然晚于构建几分钟**
> ⇒ 那条会把**刚构建完**的 dist 报成落后（加了 90 秒余量还是假红）。
> 病根是**拿"提交时间"当"源码变了"的代理** —— 提交不改变源码内容。
> 现在比的是**前端源码文件的 mtime**（git 会改 mtime 的地方 —— checkout / 合并 / 拉取 ——
> 恰好都是"这次构建不再可信"的地方）。

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
