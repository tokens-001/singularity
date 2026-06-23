# Singularity后端架构审计 — 执行计划

## 给 Codex / Opus 的指令

审计标准：`python/scheduler/AUDIT_SPEC.md`（7 维度 31 项，本文件同级目录）
架构约束：`python/scheduler/ARCHITECTURE.md`
项目根目录：`/Users/jingzhe/Singularity/python/`

---

## Phase 1: Codex 全面扫描（45 文件，31 项全覆盖）

### 步骤

1. 通读 `scheduler/AUDIT_SPEC.md` 了解全部 31 项检查规则
2. 从项目根目录执行验证命令，逐项填写审计表
3. 不确定的项标记"未验收"并注明原因，不写"通过"

### Phase 1 需要读的文件（按优先级）

**P0 必读（核心引擎，出错直接炸）**：
- `scheduler/orchestrator.py` — 调度循环
- `scheduler/_exec.py` — 执行引擎
- `scheduler/tracker.py` — 状态机
- `scheduler/dispatcher.py` — Agent 分发
- `scheduler/executors/openai_agent.py` — Agent 运行时
- `scheduler/memory.py` — MAGMA 记忆

**P1 应该读（架构边界）**：
- `app.py` — Web 层
- `scheduler/_api.py` — API handler 层
- `scheduler/workflow.py` — 项目编排
- `scheduler/router.py` — 路由决策
- `scheduler/_planner.py` — 任务规划
- `scheduler/mcp.py` — MCP 协议
- `scheduler/_lifecycle.py` — 记忆生命周期
- `scheduler/roles.py` — 角色定义
- `scheduler/permission.py` — 权限引擎
- `scheduler/config.py` — 配置中心

**P2 可选（辅助模块）**：
- 其余 `scheduler/*.py` 和 `scheduler/executors/*.py`

### Phase 1 输出格式

```
# Singularity后端审计报告 — Phase 1 (Codex 全面扫描)

## D1 分层违规
| # | 文件:行号 | 实际结果 | 通过? | 证据 |
| D1-1 | ... | ... | ✅/❌/⚠ | ... |
...

## D2 文件结构
...

（逐维度输出，共 7 个表格）

## 发现汇总
- P0 问题: N 项
- P1 问题: N 项
- P2 问题: N 项
- P3 问题: N 项
```

---

## Phase 2: Opus 深度审查（仅 P0 + P1 问题项 + 核心 6 文件）

### 输入
- Phase 1 的审计报告（Codex 输出）
- `scheduler/AUDIT_SPEC.md`
- `scheduler/ARCHITECTURE.md`

### 步骤

1. **审核 Codex 的结论**：对 Phase 1 中标记为 ❌（未通过）和 ⚠（豁免）的项，逐条验证 Codex 的判断是否正确。如果 Codex 漏了问题，补充。
2. **对 P0/P1 问题做根因分析**：不是"这行代码有问题"，而是"这个问题的根本原因是什么，会导致什么后果"。
3. **对核心 6 文件做深度代码审查**：逐函数检查以下文件是否有 Codex 漏掉的问题：
   - `scheduler/orchestrator.py` — 调度循环的正确性
   - `scheduler/_exec.py` — 执行引擎的完备性
   - `scheduler/tracker.py` — 状态机的原子性
   - `scheduler/dispatcher.py` — 依赖注入的正确性
   - `scheduler/executors/openai_agent.py` — Agent 运行时的健壮性
   - `scheduler/memory.py` — 图算法的正确性
4. **给出修复优先级排序**：按"不改会炸 > 不改会腐 > 改了更好"排列所有问题。

### Phase 2 输出格式

```
# Singularity后端审计报告 — Phase 2 (Opus 深度审查)

## 一、对 Codex 结论的复核
| Codex 结论 | 复核结果 | 理由 |
| D1-1: ❌ | 同意/纠正为 ✅ | ... |
...

## 二、P0/P1 问题根因分析
### 问题 1: [问题简述]
- 根因: ...
- 后果: ...
- 修复方案: ...
- 估时: ...

（逐问题输出）

## 三、核心文件深度审查
### orchestrator.py
| 函数 | 问题 | 严重度 |
...

（逐文件输出）

## 四、最终修复优先级
| 优先级 | 问题 | 估时 | 不改的后果 |
...
```

---

## 模型角色对照

| | Codex (Phase 1) | Opus (Phase 2) |
|--|----------------|---------------|
| 定位 | 广度扫描，不放过任何一个检查项 | 深度审查，只盯 P0/P1 和核心文件 |
| 输出 | 31 行审计表 + 发现汇总 | 根因分析 + 修复方案 + 优先级排序 |
| 风格 | 机械化执行，逐项对照规则 | 批判性思考，质疑 Codex 的结论 |
| 不做什么 | 不写修复代码，不做根因分析 | 不做全量扫描（Codex 已做） |
