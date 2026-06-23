# SkillOpt — 技能自进化框架

> 微软研究院 2026.5 开源 | arxiv: 2605.23904 | github.com/microsoft/SkillOpt

## 核心思路
把 SKILL.md 当"可训练参数"，用深度学习训练循环自动优化。

## 四步循环
1. **执行(Rollout)** — 用当前skill跑任务，记录轨迹和分数
2. **反思(Reflect)** — 另一个优化模型分析成功/失败，提出add/delete/replace编辑
3. **编辑(Edit)** — 合并去重，在文本学习率预算内应用（如每步最多4条编辑）
4. **验证(Gate)** — 在留出验证集上测试，只接受严格改进。拒绝的编辑进负反馈缓冲区

## 关键数据
- 集成 Claude Code / Codex / 直接对话
- 平均提升 +19.1 分（vs 无skill基线）
- 跨环境迁移：Codex训练的skill转Claude Code +59.7分
- 部署时零推理开销（只加载最终best_skill.md）

## 适用时机
- 需要足够的实际使用数据（执行→评估循环）
- teach-me / CLAUDE.md 等高频使用的skill文件
- 当前状态：刚重写完teach-me，先手动验证再考虑自动化
