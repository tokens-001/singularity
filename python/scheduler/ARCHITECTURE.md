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

### 规则 2: executors 隔离 ✅ 已解决 (2026-06-20)

**当前事实**: `executors/openai_agent.py` 只导入:
- `..config` ✅ 允许
- 标准库 + httpx ✅ 允许

所有跨层依赖（skills/permission/mcp/witness/dispatcher）已通过 dispatcher 依赖注入消除。

### 规则 3: 禁止新的延迟导入

当前已有的延迟导入（`from .X import Y` 在函数体内）维持现状。
**每次新增功能时禁止新增延迟导入**。如果必须延迟导入，说明存在循环依赖，需要先解环。

### 规则 4: 文件行数上限 (2026-06-20 更新)

| 模块 | 优化后 | 上限 | 状态 | 备注 |
|------|--------|------|------|------|
| app.py | 1211 | 1200 | 🟢 | Web层+SSE+限流+安全, 已从2409减半 |
| _api.py | 1032 | 1200 | 🟢 | 新文件, 路由handler业务逻辑 |
| memory.py | 953 | 1000 | 🟢 | MAGMA图算法, 生命周期已提取 |
| orchestrator.py | 661 | 700 | 🟢 | 已从820减少, v3统一入口 |
| __main__.py | 633 | 700 | 🟢 | CLI入口, 每个命令独立函数 |
| workflow.py | 623 | 700 | 🟢 | 项目阶段编排, 结构清晰 |
| _exec.py | 523 | 600 | 🟢 | 核心执行引擎 |
| openai_agent.py | 597 | 600 | 🟢 | Agent runtime, 已完全隔离 |
| roles.py | 287 | 400 | 🟢 | 数据已迁TOML, 只剩逻辑 |
| 其余 | <500 | 500 | 🟢 | — |

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

## 四、技术债清理记录 (2026-06-20)

| # | 项 | 状态 | commit |
|---|-----|------|--------|
| T1 | app.py 拆分 (2409→1211) | ✅ | 6b48d94 |
| T2 | orchestrator 三版本统一 (820→661) | ✅ | 430c58d |
| T3 | executor 完全隔离 (0违规导入) | ✅ | 31c74bf, 3cd4c2e |
| T4 | memory.py 生命周期提取 | ✅ | e38a3e5 |
| T5 | 清理顶层死目录 | ✅ | c795829 |
| T6 | __main__.py 去重确认 | ✅ | 无需改动 |
| T7 | roles.py 静态数据迁TOML (507→287) | ✅ | 2992a00 |

## 五、每次改代码前的自检清单

1. 我加的文件超过 300 行了吗？→ 拆
2. 我新增了 executor 对 scheduler 的导入吗？→ 不允许
3. 我新增了延迟导入吗？→ 不允许
4. 我往 app.py 里加了超过 5 行的业务逻辑吗？→ 下沉到 scheduler
5. 我的改动需要同时在 `app.py` 和 `__main__.py` 里加代码吗？→ 提取到 scheduler 公共函数
