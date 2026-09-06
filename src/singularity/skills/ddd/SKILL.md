---
name: ddd
description: Doubt-Driven Development — 五步反证法：CLAIM→EXTRACT→DOUBT→RECONCILE→STOP
type: prompt
category: practice
---

# Doubt-Driven Development

你必须对每个产出执行反证审查。关键原则：**审查者永远看不到原始 CLAIM（你的方案/结论），只能看到 ARTIFACT（实际产出）+ CONTRACT（原始需求）**，防止审查者被原结论带偏。

## 审查流程

### 1. EXTRACT — 从产出中提取主张
从你的代码/方案中提取所有隐含主张。每一个 `if` 分支、每一个异常处理、每一个架构选择都是可以被挑战的主张。

### 2. DOUBT — 假设作者过度自信
审查者的任务不是验证，是**找问题**："Assume the author is overconfident."
- 什么输入会让这段代码崩溃？
- 什么边界条件没覆盖？
- 谁的既有代码会被这个改动破坏？
- 三个月后这个方案会有什么问题？
- 是否有更简单的实现被跳过了？

### 3. RECONCILE — 修正或反驳
- 有效反例 → 修正方案
- 反例不成立 → 记录为什么（防止重复审查）
- 不确定 → 标注 "uncertain: 具体不确定点"

### 4. STOP — 收敛条件
- 两轮反证后无新发现 → 通过
- 发现致命缺陷 → 换方向，回到步骤 1
- 3 轮上限，防止死循环
- 不确定 → 标注 "uncertain: ..."，由人工判断

## 跨模型升级

单模型审查完毕后，考虑：
"单模型审查完毕。要跨模型第二意见吗？（DeepSeek / GLM / Kimi / 跳过）"

## 输出格式

```
## 主张清单 (Claim Ledger)
- 主张1: ...
- 主张2: ...

## 反证 (Doubt)
- 反例1: ... (分类: Contract misread / Valid+Actionable / Valid Trade-off / Noise)
- 反例2: ...

## 修正 (Reconcile)
[修正后的方案，标注哪些反例被采纳、哪些被驳回及原因]

## 结论 (Stop)
[通过 / 换方向 / 跨模型第二意见 / 不确定: ...]
```
