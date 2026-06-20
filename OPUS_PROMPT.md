# Opus Phase 2 — 最终提示词

> 复制下方全部内容，附带所有文件，发给 Opus。

---

你是 Opus 4.8，执行奇点后端架构审计 Phase 2。

## 工作目录

所有文件在 `/Users/jingzhe/奇点/`，代码在 `python/scheduler/`。

你先读以下文件再开始：
1. `PHASE2_OPUS.md` — 任务书（含 GPT 两轮 34 项审计报告 + 5 项任务指令 + 输出模板）
2. `python/scheduler/AUDIT_SPEC.md` — 审计规则（7 维 31 项）
3. `python/scheduler/ARCHITECTURE.md` — 架构约束 + 分层图
4. `python/scheduler/` 下全部 .py 文件（按需读）
5. `python/scheduler/executors/` 下全部 .py 文件（按需读）
6. `python/skills/` 下全部 .py 文件（按需读）
7. `python/app.py`

**不要等我传文件，自己读。**

## 你的 5 项任务

1. **复核 GPT 的 34 项结论** — 纠正误判、升级漏判、补充 GPT 漏报的新问题。如果是 GPT 漏报，标注"GPT漏报"并说明为什么 GPT 容易漏掉它
2. **P0/P1 根因分析 + 最优方案** — 每项给 A（快）/B（好）两方案，推荐一个。注明是否影响现有 API 行为和 smoke test。已公认的最优解（如 worktree 加 try/finally）不给两个方案，直接给最优并标"无争议"
3. **6 核心文件逐函数深审** — orchestrator / _exec / tracker / dispatcher / openai_agent / memory
4. **输出渐进式修复计划** — 按依赖排序，每步含改前/改后关键代码对比、风险等级（低/中/高）、smoke test 预估。计划不"大爆炸"——每步改完 smoke test 能过，做不到的拆成两步
5. **最终优先级排序** — 不改会炸 → 不改会腐 → 改了更好

## 约束

- 每项必须有文件:行号证据，不确定的标"无法确认"，不猜
- 修复方案必须具体到改哪个文件、哪个函数、怎么改。不允许"建议重构"这类模糊词
- 修复计划每步改完后 smoke test 43/43 必须能通过，做不到的拆成两步
- 不审代码风格和命名（D5 维度已由 GPT 覆盖）
- 输出严格遵守 PHASE2_OPUS.md 里的格式模板
- 全部任务完成后在报告末尾写"DONE"，不要留待办
