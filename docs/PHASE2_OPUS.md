# Opus Phase 2 审计指令

## 你的角色

你是 Opus 4.8，对 Codex（GPT）的 Phase 1 审计报告做深度复核。你的价值不是再扫一遍——是质疑 Codex（GPT）两轮扫描的 34 项结论、做根因分析、给修复方案。
你有全部 48 个文件，不需要盲区猜测。

## 输入文件（我会全部上传）

**审计文档**（3 个）：
- AUDIT_SPEC.md
- ARCHITECTURE.md
- 本文件（PHASE2_OPUS.md，含完整 Phase 1 + 1b 报告）

**全部代码**（48 个 .py 文件）：
- app.py
- scheduler/ 下全部 .py 文件（35 个）
- scheduler/executors/ 下全部 .py 文件（5 个）
- skills/ 下全部 .py 文件（2 个）
- *.toml 配置文件（3 个）

---

## Phase 1 完整报告（Codex 输出）

### D1 分层违规

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D1-1 | executor 导入 scheduler 内部 | claude_cli.py:155 | 延迟导入 witness | ❌ | `from .. import witness` |
| D1-2 | executor 导入 skills | — | 无匹配 | ✅ | — |
| D1-3 | scheduler 导入 executor 内部函数 | _worktree.py:12, orchestrator.py:70, _exec.py:30 | 直接导入 worktree 内部函数 | ❌ | create/cleanup/merge_back/commit_wt |
| D1-4 | skills 导入 scheduler | — | 无匹配 | ✅ | — |
| D1-5 | 函数体内延迟导入 | _api.py:397, orchestrator.py:296, memory.py:893 等 | 多处延迟导入 | ❌ | `from . import memory / from ._types import BatchOutput` |

### D2 文件结构

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D2-1 | 文件行数 | app.py:1211 | 超过 1200 上限 | ❌ | wc -l |
| D2-2 | 函数行数 | _exec.py:147, openai_agent.py:172, memory.py:452, orchestrator.py:405 等 | 9 个函数超 80 行, _exec.run 244 行 | ❌ | — |
| D2-3 | 模块职责单一 | app.py:1122-1168 | MCP CRUD 逻辑直接写在 app.py | ❌ | 直接操作 mcp registry/config |

### D3 重复代码

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D3-1 | 相似函数体 | roles.py:35, roles.py:115 | _load_personas 与 _load_roles 重复 >10 行 | ❌ | — |
| D3-2 | JSON 解析重复 | workflow.py:22, execution_judge.py:131, conductor.py:228 | 其他模块未用 _try_parse_json | ❌ | — |
| D3-3 | 错误处理重复 | _auth.py:86, _profiler.py:41, _token_budget.py:45 | 多处 `except Exception: pass` | ❌ | — |
| D3-4 | 配置加载重复 | dispatcher.py:41, mcp.py:392, model_registry.py:80, roles.py:41 | TOML 读取未统一封装 | ❌ | — |

### D4 异常处理

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D4-1 | 裸 except 无类型 | _exec.py:86, _exec.py:412, orchestrator.py:581 | 3 处裸 except | ❌ | `except: pass` |
| D4-2 | 吞异常无日志 | _lifecycle.py:81, dispatcher.py:282, router.py:233 | 多处静默忽略 | ❌ | — |
| D4-3 | API 路由无异常处理 | app.py:467, app.py:489, app.py:1122 | 多数 route 无 try/except | ❌ | — |
| D4-4 | 文件操作无异常处理 | app.py:18, tracker.py:113, _exec.py:440 | read_text/write_text 未包裹 | ❌ | — |

### D5 命名与风格

| # | 检查项 | 文件:行号 | 通过? | 证据 |
|---|--------|---------|------|------|
| D5-1 | 私有函数前缀 | _api.py, dispatcher.py, tracker.py | 未验收 | 无法仅凭命令区分公开/内部 |
| D5-2 | 模块名一致性 | — | ✅ | _ 前缀区分内部/公开 |
| D5-3 | 导入顺序 | app.py | ✅ | 标准库→第三方→本地 |
| D5-4 | 类型注解覆盖率 | app.py 3.06%, _exec.py 50%, orchestrator.py 57% | ❌ | 低于 80% 目标 |

### D6 循环导入

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D6-1 | 显式环检测 | orchestrator.py:27, _exec.py:20, dispatcher.py:317, _planner.py:73 | AST 检出 _exec↔_planner↔dispatcher↔orchestrator 环 | ❌ | 延迟导入破环 |
| D6-2 | 延迟导入破环手段 | dispatcher.py:317, _planner.py:73, orchestrator.py:296 | 多处延迟导入，未标注破环原因 | ❌ | — |
| D6-3 | _api↔orchestrator 潜在环 | _api.py:22 | _api 顶层导入 orchestrator | ❌ | `from . import orchestrator` |
| D6-4 | memory↔_lifecycle 已破环 | — | 参数注入，无环 | ✅ | — |
| D6-5 | dispatcher↔executor 已破环 | — | 参数注入，无环 | ✅ | — |

### D7 状态一致性

| # | 检查项 | 文件:行号 | 实际结果 | 通过? | 证据 |
|---|--------|---------|---------|------|------|
| D7-1 | 谁在写 tracker | orchestrator.py + _api.py | API handler 直接写 tracker，违反契约 | ❌ | task_cancel/task_submit 直接 transition |
| D7-2 | CAS 原子性 | tracker.py:237-250 | 非原子，注释自认"单线程无竞争" | ❌ | `_read → 判断 → _write` |
| D7-3 | 崩溃恢复 | tracker.py:334-341 | recover() 覆盖 _INFLIGHT 态 | ✅ | — |
| D7-4 | 取消信号竞态 | _api.py:277 + _exec.py:211 | cancel 文件写与读之间存在窗口 | ❌ | — |
| D7-5 | worktree 泄漏 | _exec.py:202-374 | 清理不在 try/finally | ❌ | — |
| D7-6 | 内存事件持久化 | memory.py:53-57 | tmp + replace 原子写 | ✅ | — |

### Phase 1b 新增发现（补审 38 文件）

| 维度 | # | 文件:行号 | 问题 | 严重度 |
|------|---|---------|------|--------|
| D1 | D1-3 | merge.py:28 | scheduler 导入 executor 内部函数 merge_ref, merge_tree_probe | P1 |
| D1 | D1-5 | _planner.py:73, __main__.py:313, pre_search.py:113 | 函数体内延迟导入 | P1 |
| D2 | D2-2 | _planner.py:206, conductor.py:124, pre_search.py:47 | 新增 3 个超 80 行函数 | P2 |
| D3 | D3-2 | conductor.py:227, execution_judge.py:131, goal_loop.py:129 | JSON 提取未复用统一解析 | P2 |
| D3 | D3-3 | _auth.py:86, _profiler.py:41, _token_budget.py:45 | except Exception: pass 模式重复 | P2 |
| D3 | D3-4 | mcp.py:392, model_registry.py:80, roles.py:41 | TOML 配置加载未统一封装 | P2 |
| D4 | D4-1 | skills/legal/verify_laws.py:58,116,189 | skills 文件裸 except | P1 |
| D4 | D4-2 | mcp.py:233, mcp.py:278, verify_laws.py:135 | 吞异常无日志 | P1 |
| D4 | D4-4 | zhipu_api.py:153,188, skill_loader.py:262 | 文件读写无 try | P1 |
| D5 | D5-4 | _planner.py, mcp.py, verify_laws.py | 类型注解 <80% | P3 |
| D6 | D6-1 | _planner.py:18,73, goal_loop.py:23 | 新文件参与导入环 | P0 |
| D6 | D6-2 | _planner.py:73, api_store.py:70, model_registry.py:149 | 延迟导入未标注破环原因 | P0 |
| D7 | D7-1 | __main__.py:117, _planner.py:31, merge.py:180 | CLI、planner、merge 直接写 tracker | P0 |

### 全量审计汇总（Phase 1 + 1b 合并）

| 严重度 | Phase 1 | 新增 | 合计 |
|--------|---------|------|------|
| P0 | 7 | +3 | **10** |
| P1 | 7 | +4 | **11** |
| P2 | 7 | +4 | **11** |
| P3 | 1 | +1 | **2** |
| **总计** | **22** | **+12** | **34** |

---

## Phase 2 任务（逐项执行）

### 任务 1：复核 Codex 结论

对上方 Phase 1 报告中的每个 ❌ 和 ⚠，验证 Codex 的判断是否正确。Codex 可能：
- 判错了（把符合规则的东西标成了违规）→ 纠正为 ✅
- 判对了但证据不足 → 补充证据
- 判对了但漏了严重度 → 升级严重度
- 漏判了 → 补充新问题

### 任务 2：P0 + P1 问题根因分析 & 最优方案

对每个 P0 和 P1 问题：
```
### 问题: [简述]
- **根因**: 什么设计决策导致了它（不是"代码写错了"）
- **实际后果**: 什么场景下会炸，怎么炸
- **方案A** (最小改动): ... 估时: ...
- **方案B** (最优方案): ... 估时: ...
- **推荐**: 方案B/A，理由: ...
- **涉及文件**: ...
```

**约束**：
- 必须给出两个方案：一个求快、一个求好。推荐最优。
- 方案必须具体到改哪个文件的哪个函数、怎么改。
- 如果问题已经有公认的最优解（如 worktree 加 try/finally），不给两个方案，直接给最优解并标注"无争议"。

### 任务 3：核心 6 文件深度审查

逐函数检查以下文件，找 Codex 可能漏掉的问题：

1. `scheduler/orchestrator.py` — 调度循环的正确性
2. `scheduler/_exec.py` — 执行引擎的完备性
3. `scheduler/tracker.py` — 状态机的原子性
4. `scheduler/dispatcher.py` — 依赖注入的正确性
5. `scheduler/executors/openai_agent.py` — Agent 运行时的健壮性
6. `scheduler/memory.py` — 图算法正确性

### 任务 4：修复优先级排序

按"不改会炸 > 不改会腐 > 改了更好"排列所有问题。

---

## 输出格式

```
# Singularity后端审计报告 — Phase 2 (Opus 深度审查)

## 一、对 Codex 结论的复核
| Codex 结论 | 复核 | 理由 |
| D1-1: ❌ | 同意/纠正 | ... |
...

## 二、P0/P1 根因分析 & 最优方案
### 问题 1: ...
- 根因: ...
- 后果: ...
- 方案A (最小改动): ... 估时: ...
- 方案B (最优方案): ... 估时: ...
- 推荐: ... 理由: ...
- 涉及文件: ...
- 是否会破坏 API/行为: 是/否, 说明
- smoke test 可否通过: 是/否, 说明

## 三、核心文件深度审查
### orchestrator.py
| 函数 | 问题 | 严重度 |
...

### 任务 3：核心 6 文件深度审查

逐函数检查，找 Codex 可能漏掉的问题：
1. `scheduler/orchestrator.py`
2. `scheduler/_exec.py`
3. `scheduler/tracker.py`
4. `scheduler/dispatcher.py`
5. `scheduler/executors/openai_agent.py`
6. `scheduler/memory.py`

### 任务 4：输出修复实施文档

基于任务 2 的推荐方案，输出一份可执行的修复文档 `FIX_PLAN.md`：

**要求**：
- 每个修复项：涉及文件、具体改动描述、改前/改后关键代码对比
- 按依赖排序：先修不依赖其他的，后修依赖前置修复的
- 每项标注预估改动行数和风险等级（低/中/高）
- 修复顺序必须保证每步改完 smoke test 仍能通过（渐进式，不大爆炸）

### 任务 5：最终优先级排序

## 输出格式

```
# Singularity后端审计报告 — Phase 2 (Opus 深度审查)

## 一、对 Codex 结论的复核
| Codex 结论 | 复核 | 理由 |
...

## 二、P0/P1 根因分析 & 最优方案
### 问题 1: ...
- 根因: ...
- 方案A (最小): ... 估时: ...
- 方案B (最优): ... 估时: ...
- 推荐: ... 理由: ...

## 三、核心文件深度审查
### orchestrator.py
| 函数 | 问题 | 严重度 |
...

## 四、渐进式修复计划
（按依赖排序，每步后标注 risk + smoke test 状态预估）

### Step 1: [问题] (risk: 低/中/高, 估时: Xh)
- 涉及文件: ...
- 改前: ```python ... ```
- 改后: ```python ... ```
- Smoke: 应通过/可能影响X测试
- 依赖: 无 / Step N

### Step 2: ...
...

## 五、最终优先级
| 优先级 | 问题 | 推荐方案 | 估时 | 不改的后果 |
...

## 六、Codex 漏报补充
```
