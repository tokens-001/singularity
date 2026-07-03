# 奇点 vs Ruflo 工作流对比

> 2026-07-03。基于奇点源码 + Ruflo 公开文档/源码分析。

---

## 一句话

| | 奇点 | Ruflo |
|---|---|---|
| **本质** | 独立调度引擎，直接调 LLM API | Claude Code 的 MCP 编排层 |
| **运行方式** | `python3 -m singularity.web.app` | `npx ruflo` → 注册为 Claude Code 的 MCP server |
| **依赖** | httpx + websockets，不依赖 Claude Code | **强依赖 Claude Code**，agent 执行靠 `child_process.spawn('claude', ...)` |

---

## 架构对比

| 维度 | 奇点 | Ruflo |
|------|------|------|
| **调度模型** | 固定 6 阶段流水线（定义→架构→实现→审查→验收→交付） | 灵活蜂群拓扑（Hierarchical/Mesh/Ring/Star 4 种） |
| **角色体系** | 13 个固定角色，每个有明确输入/输出 schema | 3 Queen + 8 Worker，角色可扩展 |
| **模型路由** | 两档（便宜/强力）+ route_learner Hedge 权重 | 3-tier（显式指定/复杂度评分/类型默认）+ MoE 8专家 + Q-Learning |
| **任务分类** | LLM 分类（router.py `_llm_classify`，调 DeepSeek） | enhanced-model-router.js 复杂度评分 |
| **执行方式** | 直接 HTTP 调 LLM API（OpenAI 兼容） | `child_process.spawn('claude', ['--dangerously-skip-permissions', ...])` |
| **人工卡点** | 3 个 GATE（定义完/架构完/交付前），人审 | 无固定卡点，完全自动 |
| **测试策略** | 多段测试（实现期核心测→审查期回归→验收期全方位+E2E） | Worker 中有 Tester 角色，无阶段化测试策略 |
| **冲突裁决** | 合成器裁决（给理由，不投票） | 3 种共识协议（Majority/Weighted/Byzantine Fault Tolerant） |
| **上下文传递** | 阶段间结构化 JSON 交接，下游只读自己需要的片段 | memory_store/search + 3-scope（project/local/user）+ HNSW 向量检索 |
| **多模型碰撞** | 仅 4 角色（架构/安全/研究员），其余单模型 | 蜂群内所有 Worker 可并行，无碰撞策略区分 |

---

## 工作流实际执行对比

### 奇点当前能跑的

```
用户输入 → Observer (WebSocket) → Router (LLM分类任务类型)
         → Dispatcher (route_learner权重选模型) → _exec (worktree隔离执行)
         → validator 校验 → merge 合并 → 返回结果
```

**已落地**：Observer 对话、Router LLM 分类、Dispatcher 选模型、_exec 执行、校验合并。约 1500 行 Python。

### Ruflo 当前能跑的

```
用户输入 → Claude Code → MCP tool call → MCPServerManager
         → agent_spawn (创建 JS Map entry) → Queen prompt 分解任务
         → spawn('claude', [...]) 启动子进程 → Worker 执行
         → memory_store 共享结果 → Queen 收集整合 → 返回
```

**已落地**：MCP server（215+ tools）、agent spawn、蜂群协调、记忆系统。v3.6.12 生产可用。

---

## 关键差异（选型参考）

| 差异点 | 奇点 | Ruflo | 影响 |
|--------|------|------|------|
| **独立性** | 不依赖 Claude Code | 强依赖 Claude Code | Ruflo 随 Claude Code 升级可能 break；奇点自己控制全链路 |
| **流程刚性** | 固定 6 阶段，强制走完 | 灵活拓扑，用户自选 | 奇点适合标准化交付；Ruflo 适合探索性任务 |
| **质量保障** | 3 个 GATE + 多段测试 + AI 审查触顶降级 | Tester/Reviewer Worker 角色 | 奇点有系统性质保机制；Ruflo 靠 Worker prompt 质量 |
| **模型成本** | 两档显式控制 | MoE+Q-Learning 自动选 | 奇点成本可预测；Ruflo 可能自动选贵模型 |
| **成熟度** | 原型阶段（Observer+Dispatcher 能跑，Planner/委员会待激活） | v3.6.12，21K+ star，npm 可安装 | Ruflo 生产可用；奇点还需落地 5 个阶段 |
| **代码量** | ~1500 行 Python 核心 | 大型 TypeScript 项目（215+ MCP tools） | 奇点轻量；Ruflo 功能多但复杂度高 |
| **审计结论** | — | 有报告称 agent_spawn 只创建 Map entry 从不激活；hive-mind 靠 spawn claude + prompt 角色扮演 | Ruflo 的 "swarm" 本质是多个 Claude CLI 子进程跑不同 prompt |

---

## Ruflo 的实质

搜到的独立审计指出两个关键事实：

1. **agent_spawn 的"agent"不是真正的进程/线程**：只是一个 JS Map entry，状态 idle，没有证据表明它会自动转变为 active 并执行。实际的"执行"靠 Queen 的 prompt 里写"你现在是 XX 角色"。

2. **hive-mind = spawn claude + prompt 角色扮演**：不是原生多进程编排，而是 `child_process.spawn('claude', ['--dangerously-skip-permissions', '...'])` 启动 Claude CLI 子进程，prompt 里告诉它"你是一只蜂后/工蜂"。这和奇点的 `_exec.py` worktree 隔离执行本质相同——都是起子进程跑 LLM。

**Ruflo 的价值不在"蜂群架构"本身，而在**：
- 215+ 预制 MCP tools（省去自己写工具定义）
- 记忆系统（SQLite + HNSW，比奇点的 JSON 文件检索更强）
- Claude Code 集成（对已在用 Claude Code 的团队零摩擦）
- 社区生态（21K star，插件市场）

---

## 奇点不该学 Ruflo 的

1. **MoE + Q-Learning 路由**：奇点只有 ~5 个模型，两档+Hedge 权重够用。8 专家路由是过度设计。
2. **4 种蜂群拓扑**：软件开发流程是线性的，不需要 Mesh/Ring。固定 6 阶段更匹配。
3. **spawn claude 子进程**：奇点直接调 API，更轻量，不依赖 Claude Code 安装。
4. **共识协议**：代码合并不需要 Byzantine Fault Tolerance。合成器裁决（给理由）够用。

## 奇点可以学 Ruflo 的

1. **记忆检索**：HNSW 向量索引比奇点当前的关键词+Jaccard 更准。但奇点已有 MAGMA 四图（语义/时间/因果/实体），基础不差。
2. **MCP 工具生态**：Ruflo 的 MCP server 模式让 Claude Code 能直接调它的工具。奇点未来可以作为 MCP server 暴露能力。
3. **Token 优化**：Ruflo 的 pattern caching + batching 值得参考，奇点目前没做 token 层面优化。

---

## 总结

| | 奇点 | Ruflo |
|---|---|---|
| **适合** | 独立开发者要完整软件交付，不想装 Claude Code | 已用 Claude Code 的团队，要快速多 agent 协作 |
| **核心壁垒** | 6 阶段软件工程流程 + 3 GATE 人审 + 角色契约 | 215+ MCP tools + Claude Code 深度集成 + 社区 |
| **当前阶段** | 原型，Observer+Dispatcher 能跑 | 生产可用，v3.6.12 |
| **技术债务** | Planner/委员会/合成器/4 阶段待落地 | agent_spawn 实质存疑，spawn claude 子进程开销大 |
