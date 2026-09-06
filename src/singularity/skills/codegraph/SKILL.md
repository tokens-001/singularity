---
name: codegraph
description: 代码地图——查函数调用链、影响范围、复杂度、项目结构（CodeGraph CLI）
type: prompt
category: practice
---

# CodeGraph 代码地图

可用命令（在项目根目录执行）：

| 命令 | 用途 | 示例 |
|------|------|------|
| `codegraph explain <函数>` | 查函数详情：调用链、复杂度、健康指标 | `codegraph explain _rrf_anchors` |
| `codegraph impact <文件>` | 改一个文件影响哪些其他文件 | `codegraph impact python/scheduler/memory.py` |
| `codegraph brief <文件>` | 文件摘要：符号列表、风险等级 | `codegraph brief python/scheduler/_exec.py` |
| `codegraph batch fn-impact <函数1> <函数2>` | 批量查多个函数的依赖 | `codegraph batch fn-impact run dispatch` |
| `codegraph deps <文件>` | 文件的直接依赖和被依赖 | `codegraph deps python/app.py` |
| `codegraph audit <函数>` | 复合报告：explain+impact+health | `codegraph audit run` |

## 使用时机

- **改代码前**：先跑 `codegraph explain <要改的函数>` 看清调用链
- **改完验证**：跑 `codegraph impact <改过的文件>` 确认没漏掉受影响的文件
- **接手新模块**：跑 `codegraph brief <文件>` 快速了解结构
- **怀疑死代码**：跑 `codegraph audit <函数>` 看 caller 覆盖

## 输出规则

- 先用 explain/impact 探路，再动手改代码
- 改完用 impact 验证影响范围
- 不要把 codegraph 输出原样塞给用户——提炼关键发现
