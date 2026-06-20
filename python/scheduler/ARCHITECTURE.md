# 奇点调度平台 — 架构约束文件

> 这不是设计文档。设计文档在代码之前写，这份文件在代码之后写。
> 它描述当前**实际**的模块分层和导入规则，用于后续每次改动时的自检。
> 最后更新: 2026-06-20，基于 14 个 commit 后的代码事实。

---

## 一、分层（当前实际，非目标状态）

```
python/
├── app.py                 ← Web 层: Flask 路由 + SSE + 请求校验
├── scheduler/             ← 核心层: 所有业务逻辑
│   ├── orchestrator.py    ← 调度中心: 循环、批处理、记忆维护
│   ├── _exec.py           ← 执行引擎: 单任务执行、Cascade Routing
│   ├── _planner.py        ← 规划器: 任务分解、D层 vs E层路由
│   ├── dispatcher.py      ← Agent 分发: 选agent、创建executor
│   ├── router.py          ← 路由决策: 拓扑分析、topology选择
│   ├── workflow.py        ← 工作流: 多阶段项目编排
│   ├── tracker.py         ← 状态追踪: 任务生命周期、Event Log
│   ├── memory.py          ← 记忆系统: MAGMA、生命周期、意图遍历
│   ├── permission.py      ← 权限引擎: Profile、Agent绑定
│   ├── goal_loop.py       ← Goal循环: [Goal]前缀多轮迭代
│   ├── mcp.py             ← MCP集成: 外部工具服务器发现与调用
│   ├── neijinglu.py       ← 内镜录: 执行观测、事件采集
│   ├── witness.py         ← 心跳/监控
│   ├── model_profile.py   ← 模型画像: 性能统计
│   ├── model_registry.py  ← 模型注册: 多提供商模型管理
│   ├── pre_search.py      ← 预搜索: 语义检索、强D命中判断
│   ├── chancellor.py      ← 衡准: 费用/预算控制
│   ├── api_store.py       ← API库: 多提供商endpoint管理
│   ├── execution_judge.py ← 执行评判: 结果质量判断
│   ├── task_templates.py  ← 任务模板
│   ├── merge.py           ← 合并冲突处理
│   ├── snapshot.py        ← 快照
│   ├── supervisor.py      ← 监督
│   ├── handoff.py         ← 任务交接
│   ├── project.py         ← 项目管理
│   ├── conductor.py       ← 编组: 项目agent编队
│   ├── roles.py           ← 角色定义
│   ├── codegraph.py       ← 代码图谱
│   ├── validator.py       ← 验证器
│   ├── config.py          ← 集中配置（路径/阈值/超时）
│   ├── log.py             ← 日志
│   ├── _auth.py           ← 认证/Token管理
│   ├── _cache.py          ← 缓存
│   ├── _profiler.py       ← 性能采集
│   ├── _token_budget.py   ← Token预算
│   ├── _types.py          ← 共享类型
│   ├── _worktree.py       ← Git worktree管理
│   ├── executors/         ← 执行器实现
│   │   ├── base.py        ← ExecutorResult + BaseExecutor
│   │   ├── openai_agent.py← OpenAI兼容Agent (主力)
│   │   ├── claude_cli.py  ← Claude Code CLI
│   │   ├── zhipu_api.py   ← 智谱API (单轮)
│   │   └── worktree.py    ← Worktree管理工具
│   └── __main__.py        ← CLI入口
├── skills/                ← Skill系统 (跨层，独立包)
└── tools/                 ← Dash TUI (独立)
```

## 二、导入规则（红线）

### 规则 1: 分层导入

```
Web层 (app.py)  → 可以导入 scheduler.* 的任何模块
scheduler/*.py  → 只能导入 scheduler 包内模块 (from .xxx)
executors/*.py  → 只能导入: .base, ..config, 标准库, httpx
skills/          → 独立包，不导入 scheduler
```

### 规则 2: executors 隔离（当前违反，目标状态）

**当前事实**: `executors/openai_agent.py` 导入了:
- `..config` ✅ 允许
- `..dispatcher` ❌ 违规（为了 `_find_agent_level`）
- `..witness` ❌ 违规（心跳上报）
- `..permission` ❌ 违规（工具权限检查）
- `..mcp` ❌ 违规（MCP工具调用）
- `skills.skill_loader` ❌ 违规（跨包）

**临时豁免**: 不改这些导入，但**不允许新增**executor对scheduler模块的依赖。
**目标**: 将权限检查、MCP调用、Skill加载上提到 `_exec.py` 层，executor只通过 `ExecutorResult.tool_events` 上报。

### 规则 3: 禁止新的延迟导入

当前已有的延迟导入（`from .X import Y` 在函数体内）维持现状。
**每次新增功能时禁止新增延迟导入**。如果必须延迟导入，说明存在循环依赖，需要先解环。

### 规则 4: 文件行数上限

| 模块 | 当前行数 | 上限 | 状态 |
|------|---------|------|------|
| app.py | 2409 | 800 | 🔴 超标3倍 |
| memory.py | 1007 | 500 | 🔴 超标2倍 |
| orchestrator.py | 820 | 400 | 🔴 超标2倍 |
| __main__.py | 633 | 400 | 🔴 超标 |
| workflow.py | 623 | 400 | 🔴 超标 |
| _exec.py | 523 | 400 | 🔴 超标 |
| 其余 | <500 | 400 | 🟢 |

**新增代码规则**: 新功能优先新建模块（<300行），不追加到大文件。

### 规则 5: orchestrator 依赖收敛

**当前事实**: orchestrator.py 导入 30+ 个模块。
**规则**: 新增 orchestrator 依赖前，先确认是否可以通过 `_exec.py` 或 `_planner.py` 间接调用。

## 三、两个入口的分工

| 职责 | app.py (Web) | __main__.py (CLI) |
|------|-------------|-------------------|
| 调度循环 | `_run_loop()` | `cmd_loop()` |
| 任务提交 | POST /api/tasks | `scheduler add` |
| 队列管理 | 通过 orchestrator | 通过 orchestrator |
| 事件推送 | SSE | stdout |
| 前端 | HTML+JS | 无 |

**规则**: 调度循环逻辑应复用 `orchestrator.run_queue_v3()`，不在两个入口各自维护。

## 四、已知技术债（不阻塞新功能，但需排期）

| # | 项 | 严重度 | 估时 |
|---|-----|--------|------|
| T1 | app.py 拆分为 blueprint 模块 | P0 | 4h |
| T2 | orchestrator 三版本统一 | P1 | 2h |
| T3 | executor 反向依赖解耦 | P1 | 3h |
| T4 | memory.py 拆分 | P2 | 3h |
| T5 | 清理顶层 scheduler/ 死目录 | P3 | 0.1h |
| T6 | __main__.py 去重（复用 orchestrator 公开函数） | P2 | 2h |

## 五、每次改代码前的自检清单

1. 我加的文件超过 300 行了吗？→ 拆
2. 我新增了 executor 对 scheduler 的导入吗？→ 不允许
3. 我新增了延迟导入吗？→ 不允许
4. 我往 app.py 里加了超过 5 行的业务逻辑吗？→ 下沉到 scheduler
5. 我的改动需要同时在 `app.py` 和 `__main__.py` 里加代码吗？→ 提取到 scheduler 公共函数
