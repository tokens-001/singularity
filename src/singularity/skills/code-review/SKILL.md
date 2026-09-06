---
name: code-review
description: 审查代码的 bug、性能和风格问题
type: tool
arguments: file_path focus
---

# Code Review

审查文件 `${file_path}`，关注点：${focus}。

## 执行方式

1. 读取目标文件
2. 按关注点逐段审查
3. 列出发现的问题，标注严重程度（严重/一般/建议）
4. 给出具体修改建议

## 审查维度

- 逻辑错误和边界条件
- 性能瓶颈
- 安全风险
- 代码风格和可读性
- 复用和简化机会
