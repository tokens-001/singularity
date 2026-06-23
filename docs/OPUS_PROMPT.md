# Opus Phase 2 — Singularity后端架构审计

## 你的角色

你是 Opus 4.8，对 GPT 的两轮审计报告做深度复核。质疑误判、做根因分析、给出最优修复方案。

## 工作目录

所有代码在 `/Users/jingzhe/Singularity/python/`。你自己读文件，不要等我传。

### 必读文档（4个）
- `python/scheduler/AUDIT_SPEC.md` — 7维31项审计规则
- `python/scheduler/ARCHITECTURE.md` — 架构约束+分层图
- `python/scheduler/personas.toml` + `python/scheduler/roles.toml` — 角色数据

### 必读代码（6个核心，逐函数审）
- `python/app.py`
- `python/scheduler/orchestrator.py`
- `python/scheduler/_exec.py`
- `python/scheduler/tracker.py`
- `python/scheduler/dispatcher.py`
- `python/scheduler/executors/openai_agent.py`
- `python/scheduler/memory.py`

### 按需读（其余41个 .py 文件）
`python/scheduler/` 下全部 .py、`python/scheduler/executors/` 下全部 .py、`python/skills/` 下全部 .py

---

## GPT 两轮审计报告（共 34 项）

### D1 分层违规

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D1-1 | claude_cli.py:155 | executor 延迟导入 witness | ❌ |
| D1-2 | — | executor 导入 skills | ✅ |
| D1-3 | _worktree.py:12, orchestrator.py:70, _exec.py:30, merge.py:28 | scheduler 导入 executor 内部函数 | ❌ |
| D1-4 | — | skills 导入 scheduler | ✅ |
| D1-5 | _api.py:397, orchestrator.py:296, memory.py:893, _planner.py:73, __main__.py:313 | 函数体内延迟导入 | ❌ |

### D2 文件结构

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D2-1 | app.py:1211 | 超过1200上限 | ❌ |
| D2-2 | _exec.py:147(244行), openai_agent.py:172(178行), memory.py:452(136行), orchestrator.py:405(112行) 等9个 | 函数超80行 | ❌ |
| D2-3 | app.py:1122-1168 | MCP CRUD直接写在app.py | ❌ |

### D3 重复代码

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D3-1 | roles.py:35,115 | _load_personas与_load_roles重复>10行 | ❌ |
| D3-2 | execution_judge.py:131, conductor.py:227, goal_loop.py:129 | JSON提取未复用workflow._try_parse_json | ❌ |
| D3-3 | _auth.py:86, _profiler.py:41, _token_budget.py:45 | except Exception: pass重复 | ❌ |
| D3-4 | dispatcher.py:41, mcp.py:392, model_registry.py:80, roles.py:41 | TOML读取未统一封装 | ❌ |

### D4 异常处理

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D4-1 | _exec.py:86,412, orchestrator.py:581, verify_laws.py:58,116,189 | 裸except: | ❌ |
| D4-2 | _lifecycle.py:81, dispatcher.py:282, router.py:233, mcp.py:233,278 | 吞异常无日志 | ❌ |
| D4-3 | app.py 多数route | API路由无try/except | ❌ |
| D4-4 | app.py:18, tracker.py:113, _exec.py:440, zhipu_api.py:153,188 | 文件操作无异常处理 | ❌ |

### D5 命名与风格

| # | 文件 | 问题 | 判定 |
|---|------|------|------|
| D5-1 | _api.py, dispatcher.py, tracker.py | 无法区分公开/内部函数 | 未验收 |
| D5-2 | — | 模块_前缀命名一致 | ✅ |
| D5-3 | app.py | 导入顺序正确 | ✅ |
| D5-4 | app.py 3%, _exec.py 50%, orchestrator.py 57%, mcp.py 72% | 类型注解<80% | ❌ |

### D6 循环导入

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D6-1 | _exec↔_planner↔dispatcher↔orchestrator, goal_loop→_exec | 显式导入环 | ❌ |
| D6-2 | dispatcher.py:317, _planner.py:73, orchestrator.py:296, api_store.py:70 | 延迟导入未标注破环原因 | ❌ |
| D6-3 | _api.py:22 | _api顶层导入orchestrator | ❌ |
| D6-4 | — | memory↔_lifecycle参数注入已破环 | ✅ |
| D6-5 | — | dispatcher↔executor参数注入已破环 | ✅ |

### D7 状态一致性

| # | 文件:行号 | 问题 | 判定 |
|---|---------|------|------|
| D7-1 | _api.py, __main__.py:117, _planner.py:31, merge.py:180 | API/CLI/planner/merge直接写tracker | ❌ |
| D7-2 | tracker.py:237-250 | CAS非原子，注释自认单线程 | ❌ |
| D7-3 | tracker.py:334-341 | recover()覆盖INFLIGHT态 | ✅ |
| D7-4 | _api.py:277 + _exec.py:211 | cancel信号竞态窗口 | ❌ |
| D7-5 | _exec.py:202-374 | worktree清理不在try/finally | ❌ |
| D7-6 | memory.py:53-57 | tmp+replace原子写 | ✅ |

### 汇总

| 严重度 | 数量 |
|--------|------|
| P0（会炸） | 10 |
| P1（会腐） | 11 |
| P2（会乱） | 11 |
| P3（会丑） | 2 |
| **总计** | **34** |

---

## 你的 5 项任务

### 任务 1：复核 GPT 结论

对上方每个 ❌ 和 ⚠，验证是否真的有问题。纠正误判、升级漏判、补充 GPT 漏报。漏报标注"GPT漏报"并说明 GPT 为什么容易漏掉。

### 任务 2：P0/P1 根因分析 + 最优方案

每个 P0/P1 输出：

```
### 问题: [简述]
- 根因: 什么设计决策导致（不是"代码写错了"）
- 后果: 什么场景会炸、怎么炸
- 方案A (最小): ... 估时: ...
- 方案B (最优): ... 估时: ...
- 推荐: ... 理由: ...
- 涉及文件: ...
- API/行为影响: 是/否
- smoke test: 能过/需调整
```

**约束**：必须具体到文件、函数、怎么改。公认最优解（如worktree加try/finally）只给一个方案标"无争议"。不允许"建议重构"。

### 任务 3：6 核心文件逐函数深审

orchestrator / _exec / tracker / dispatcher / openai_agent / memory。找 GPT 可能漏掉的问题。

### 任务 4：输出渐进式修复计划

按依赖排序。每步含改前/改后代码对比、风险（低/中/高）、smoke test 预估。每步改完 43/43 必须能过，做不到拆成两步。不大爆炸。

### 任务 5：最终优先级

不改会炸 > 不改会腐 > 改了更好。

---

## 约束

- **只读。不修改任何文件。** 你的产出是审计报告和修复计划，不是改代码
- 每项证据：文件:行号。不确定标"无法确认"，不猜
- 修复计划每步 smoke test 预估
- 不审代码风格和命名（GPT已覆盖D5）
- 全部完成后写"DONE"，不留待办

## 输出格式

```
# Singularity后端审计报告 — Phase 2 (Opus)

## 一、对 Codex 结论的复核
| 原结论 | 复核 | 理由 |

## 二、P0/P1 根因分析 & 最优方案
### 问题 N: ...
- 根因: ...
- 方案A: ... 估时: ...
- 方案B: ... 估时: ...
- 推荐: ...

## 三、核心文件深度审查
| 文件 | 函数 | 问题 | 严重度 |

## 四、渐进式修复计划
### Step 1: ... (risk: X, 估时: Xh)
- 涉及文件: ...
- 改前/改后代码对比
- Smoke预估: ...
- 依赖: 无/Step N

## 五、最终优先级
| 优先级 | 问题 | 方案 | 估时 | 不改后果 |

## 六、GPT漏报补充
DONE
```
