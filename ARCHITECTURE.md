# 奇点调度平台 — 全量审计 + 架构文档

> 2026-06-20，15 commits，35 文件，+1910/-842 行，88/88 测试绿。
> 基于 Opus 审计报告 + 全量 48 文件扫描 + 两轮修复。

---

## 一、审计范围

| 维度 | 方法 | 覆盖 |
|------|------|------|
| 首轮审计（Opus） | 7核心+12关联文件深读 | P0-P1 全覆盖 |
| 全量扫描（三代理并行） | 48文件代码质量/架构/安全 | P2-P3 全覆盖 |
| 交叉验证 | 逐条实测，去误报 | 剔除 commit message 幻觉×2、"待做"实际已做完×6 |

## 二、已解决问题（15 commits）

### CRITICAL — 不改会炸

| 问题 | 位置 | 修复 |
|------|------|------|
| `now` 变量未定义 → 全 API 500 | app.py:195 | 补 `now=time.time()` |
| `shell=True` 命令注入 | mcp.py:91 | `shlex.split` + `shell=False` |

### HIGH — 不改会泄漏/绕过

| 问题 | 位置 | 修复 |
|------|------|------|
| worktree 8条退出路径泄漏 | _exec.py | try/finally 框死生命周期 |
| fut.result() 裸调炸整批 | orchestrator.py | try/except 兜底标 FAILED |
| rollback 模块不存在，rollback_all 无守卫 | _api.py | 删死函数，task_rollback 保留 ImportError 守卫 |
| _safe_path 前缀绕过 | openai_agent.py | `+ os.sep` 防 `/a/b` 匹配 `/a/bb/` |

### MEDIUM — 不该腐化

| 问题 | 位置 | 修复 |
|------|------|------|
| tracker 双线程写 lost-update | tracker.py | `_LOCK(RLock)` 封 read-modify-write |
| worktree.py 放错包，7处分层违规 | executors/→scheduler/ | 上移为 `_git_worktree.py` |
| 安全响应头缺失 | app.py | CSP+X-Content-Type+X-Frame+Referrer |
| _RATE_BUCKETS 无线程锁 | app.py | `_RATE_LOCK` 保护读写 |
| _embed 在 SKIP_EMBED 下崩溃 | memory.py | None 守卫 |
| dispatcher 每次全量加载 skill/MCP | dispatcher.py | 按 (level,model) 缓存 + 7 失效点 |
| 40+ 裸 except → 吞 KeyboardInterrupt | 6 文件 | 全部 → except Exception |
| 3 处裸 except | _exec/orchestrator | → except Exception |
| 死代码 _process_batch | orchestrator.py | 删除（与 _finalize_result 90% 重复） |
| skills API 三连 bug | _api.py | args→arguments, dict→list, 补 source |
| 前端技能列表恒空 | app.js | 适配新 API 格式 |
| final_turn 恒为 0 | _exec.py | 每 turn 更新 |

### LOW — 清理

| 问题 | 位置 | 修复 |
|------|------|------|
| 死代码 _model_cost_tier | dispatcher.py | 删除 |
| 死代码 try_parse_json_list | _io.py | 删除 |
| 死代码 rollback_all | _api.py | 删除 |
| JSON 解析重复（conductor+execution_judge） | 2 文件 | 统一到 `_io.try_parse_json`，-33 行 |
| 类型注解 10 文件缺 `from __future__` | 10 文件 | 各加一行 |
| project_id 无格式校验 | app.py | 正则校验 `[a-zA-Z0-9_-]{1,64}` |
| beam/hops/depth 无 try/except | app.py | 补 ValueError 守卫 |
| TOML 加载 5 处重复 | 5 文件 | 统一到 `_io.load_toml` |
| 前端深色主题/字号/层级/状态色 | style.css+app.js | Opus token 体系完整实施 |

### 前端

| 问题 | 修复 |
|------|------|
| 4 个 500 端点 | 路由修正 |
| 字号/层级/状态色/交互态/事件流 | 重设计 |
| 精致深色主题 | Opus token 体系 |
| Skills 面板恒空 | API 格式适配 |
| tool/turn/approval/subagent SSE 事件 | 5 种新事件 + 前端渲染 |
| SSE 错误处理 | try/catch + 重连 |

### 测试

| 套件 | 数量 | 覆盖 |
|------|------|------|
| smoke_test.py | 43 | API CRUD + 前端 + 边界 |
| test_exec_run.py | 21 | run() 8 条路径 + worktree 对称不变量 |
| unit_tests.py | 24 | 项目状态/路由/调度策略/缓存/拓扑 |

---

## 三、遗留问题（有意不修）

### 不能修：修了反而出事

| 问题 | 原因 |
|------|------|
| _api.py 40+ 延迟导入 | 刻意打断 `orchestrator→_exec→dispatcher→orchestrator` 循环依赖。提到顶层 = 启动炸模块 |
| 消灭全部延迟导入（P2-5） | 同上，ROI 极低 |

### 不用修：不会出事

| 问题 | 原因 |
|------|------|
| API 路由统一 try/except | Flask 默认 500 兜底，不泄露堆栈 |
| cancel 竞态孤儿文件 | benign，最多延迟一个 turn，自动回收 |
| executor 内延迟导入 witness | 只用于失败日志，影响极小 |
| _auth/_profiler/_token_budget 静默 pass | 观测失败不影响主流程 |

### 不值修：太贵，感觉不到

| 问题 | 原因 |
|------|------|
| 循环导入架构解耦（真正根治） | 需重新设计模块边界，3-5h Opus 级别判断，当前无预算 |
| thinking delta 字符流 | SSE 扛不住碎片，Web 不需要打字机效果 |
| 实时文件 diff 预览 | TUI 特有能力，Web 做要 Monaco+diff 库，太重 |
| 审批超时自动拒绝 | 先手动审批，写一堆代码几乎用不到 |

### 暂不做：等需要时再做

| 问题 | 原因 |
|------|------|
| Skill `type: flow` 多步骤工作流 | tool+prompt 两种够用，等不够了再加 |
| Skill 市场和下载 | 单人本地工具，无多用户需求 |
| Skill 间依赖声明 | 当前 skill 数量少，手动管理即可 |
| Skill 热加载 | 开发期重启两秒可接受 |

---

## 四、架构

### 分层

```
┌──────────────────────────────────────────────┐
│  app.py            Web 层 (Flask/SSE/限流/鉴权) │
├──────────────────────────────────────────────┤
│  _api.py           API Handler 层              │
│  __main__.py       CLI 入口                    │
├──────────────────────────────────────────────┤
│  orchestrator.py   ── 调度闭环 facade ──        │
│  router.py             任务路由                 │
│  tracker.py            任务状态机 (文件持久化)      │
│  dispatcher.py         模型调度+skill/MCP装配      │
│  _exec.py              执行引擎 (core loop)       │
│  _worktree.py          worktree 生命周期          │
│  _git_worktree.py      git 操作原语              │
│  merge.py              多 worktree 合并          │
│  workflow.py           项目工作流               │
│  _planner.py           任务分解+D层委员会          │
├──────────────────────────────────────────────┤
│  memory.py        ── 数据层 ──                  │
│    MAGMA 多图记忆 (事件/语义/锚点/时序)            │
│  _lifecycle.py         记忆衰减与清理            │
│  project.py            项目状态机               │
│  snapshot.py           写入前快照               │
│  model_registry.py     模型注册表               │
│  model_profile.py      模型画像                 │
│  api_store.py          API Key 库               │
│  roles.py              角色/人格面具             │
│  mcp.py                MCP 集成                 │
│  skills/skill_loader   Skill 系统               │
├──────────────────────────────────────────────┤
│  executors/        ── 执行器层 (隔离) ──          │
│    base.py             抽象基类                 │
│    openai_agent.py     OpenAI function calling │
│    claude_cli.py       Claude CLI (D层)         │
│    zhipu_api.py        智谱 HTTP API (E+层)      │
├──────────────────────────────────────────────┤
│  裁判/质量层                                    │
│    validator.py        校验闭环                 │
│    execution_judge.py  执行裁判                 │
│    goal_loop.py        Goal 循环               │
│    conductor.py        项目自动推进             │
│    supervisor.py       独立校验引擎             │
│    judge_monitor.py    裁判监控                 │
│    chancellor.py       报错总管                 │
│    neijinglu.py        交付完整性报告           │
├──────────────────────────────────────────────┤
│  基础设施                                      │
│    config.py           集中配置                 │
│    _io.py              统一 I/O (TOML+JSON)     │
│    _cache.py           TTL 缓存                 │
│    _auth.py            认证                    │
│    _types.py           数据类型                 │
│    _token_budget.py    Token 预算               │
│    _profiler.py        性能分析                 │
│    permission.py       权限引擎                 │
│    witness.py          心跳+观测                 │
│    log.py              文件日志                 │
│    handoff.py          Agent 交接记录            │
│    pre_search.py       I 层预检                 │
│    task_templates.py   任务模板                 │
│    codegraph.py        代码知识图谱             │
│    snapshot.py         快照                     │
│    router.py           二维路由判定             │
└──────────────────────────────────────────────┘
```

### 数据流

```
Web / CLI 入口
    │
    ▼
API Handler (_api.py)
    │ 创建任务 → tracker.create()
    ▼
orchestrator.run_queue()
    │ 取就绪任务 → tracker.ready_tasks()
    │ 路由判定 → router.assign()
    ▼
_exec.run(task, ctx, agents)
    │
    ├─ worktree 创建 (_git_worktree.py)
    │    └─ git worktree add 隔离环境
    │
    ├─ 执行循环 (最多 N turns)
    │    │
    │    ├─ dispatcher.dispatch()
    │    │    ├─ skill 加载 + MCP 工具装配
    │    │    └─ executor.run() → 模型调用
    │    │
    │    ├─ 收集 tool_events → SSE 推送
    │    │
    │    ├─ validator.validate()
    │    │    └─ Gate 过门 + 置信度评分
    │    │
    │    └─ _decide_cascade()
    │         ├─ pass → return (带 merge_request)
    │         ├─ retry → continue (复用 worktree)
    │         └─ cascade_skip → break (升级模型)
    │
    ├─ planner 分支 (D层)
    │    └─ 分解任务 → 子任务 → 并行执行
    │
    ├─ merge (v2/v3)
    │    └─ 多 worktree 产出合并
    │
    └─ finally: _cleanup_wt(wt)  ← 资源对称
```

### 关键设计决策

**1. 文件持久化而非数据库**

所有任务状态存为 `{task_id}.json` 文件，`os.replace` 原子写入。理由：零依赖、可 grep、可手动修。代价：并发下只有文件级原子性，单文件内 read-modify-write 需应用层锁（tracker._LOCK）。

**2. worktree 隔离**

每个任务在独立 git worktree 中执行，避免任务间文件冲突。类 Docker 的隔离效果，但用 git 原生能力实现，零额外依赖。

**3. 循环依赖用延迟导入破**

`orchestrator → _exec → dispatcher → orchestrator` 形成环。模块顶层互相 import 会炸，解决方式是 dispatcher 在函数体内 `from .orchestrator import ...`。这是刻意的、有文档的、不是 bug。

**4. 三模型分工**

| 模型 | 角色 | 做什么 |
|------|------|--------|
| Opus | 判断 | 审计结论、回归测试、失效点清单、难重构 |
| GLM-5.2 | 执行 | 照规格施工，全栈 |
| DeepSeek | 体力 | 加一行/改一字/删一段 |

**5. SSE 事件驱动前端**

不做轮询。后端主动推送 5 种事件（task/system/tool/turn/approval/subagent），前端按事件类型渲染对应 UI。一条管道复用。

**6. 测试约束**

scheduler 测试绝不调真模型 API。`QIDIAN_SKIP_EMBED=1` 跳过 embedding 下载。smoke 只测 CRUD 不驱动 loop。

### 文件规模

| 层 | 文件数 | 总行数 |
|----|--------|--------|
| Web 层 | 1 | ~1200 |
| API 层 | 2 | ~1800 |
| 调度核心 | 12 | ~5000 |
| 数据层 | 10 | ~3500 |
| 执行器 | 4 | ~1000 |
| 裁判/质量 | 7 | ~1800 |
| 基础设施 | 14 | ~2000 |
| 前端 | 2 | ~2200 |
| 测试 | 3 | ~720 |
| **总计** | **55** | **~17,200** |
