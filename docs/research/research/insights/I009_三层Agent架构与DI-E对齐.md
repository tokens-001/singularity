# I009 — 三层 Agent 架构与 D/I/E 体系对齐

## 发现
双模型架构（Planner + Executor）缺一层：**Validator（验证/约束层）**。DeepSeek 会执行但不一定执行正确意图。正确结构：

```
Planner（GPT/Opus）→ Validator（规则/schema）→ Executor（DeepSeek）
```

## 三层与体系前缀对齐

| Agent 层 | 模型 | 职能 | 对应体系 |
|----------|------|------|---------|
| Decision | GPT/Opus | 做什么 | D（决策） |
| Insight/Validate | 规则+schema | 方案是否合理 | I（洞察） |
| Execution | DeepSeek | 怎么做 | E（执行） |

判例助手里已经做了验证层（法条有效性校验、溯源越界检测），用 D/I/E 前缀给 Agent 分层是一个工程便利选择——把决策/执行/检查拆开后，每层可以独立选模型、控制成本。注意：I 在这里不是 insight（洞察），是 validate/inspect（校验），和 research/insights/ 里的 I 不是同一个含义。

## 关键约束
- **不要让 DeepSeek 直接做最终决策**——会出现结构漂移、逻辑不一致、任务偏离
- 规则/结构负责"做得对不对"——这层是 AI 之外的硬校验，和判例助手同一个逻辑

## 来源
2026-06-13，GPT 外部评审——校准"Opus决策+DeepSeek执行"的架构漏洞

## 操作规则
1. GPT/Opus 只用于不可逆决策
2. DeepSeek 负责所有执行
3. 每次调用必须先判断层级（D/I/E）

## 预算结构（月均）
- GPT（决策）$10-20
- DeepSeek（执行）$5-10
- Opus（卡点救援）按需，不固定充
- 合计：轻度 $15，中度 $30-40

## 架构图（v0.1）
```
用户 → D层(GPT/Opus: 做什么) → I层(结构化/校验: 怎么拆) → E层(DeepSeek/Codex: 怎么做) → 工具层
```
横向：GPT=选什么，Opus=为什么对，当前模型=怎么组织，DeepSeek=怎么做，Codex=Agent执行器

核心：模型=在系统中的位置，不是谁更强。D层贵但少量，I层中频，E层便宜高频。

## 状态
阶段 B 可落地为 E006 实验（多模型 Agent 系统设计图 + prompt 模板 + API 路由结构）
