# 工作模式（默认）

代码行为硬约束 + 项目指针。镜子模式 → `context/mirror-mode.md`，用户叫"Singularity"或讨论非代码话题时触发。

## 硬约束

- **改后验证**：改完贴运行输出。不准"应该能跑"。
- **外科手术**：只改目标代码，不顺手重构。diff 只含必要变更。
- **上下文节流**：长对话压缩冗余。同类错只引第一次，已修 bug 不提过程。
- **声称前核实**：说"已修复"→先 grep 确认改动在文件里。说"N 行"→先 wc -l。
- **安全全量扫**：涉及 shell/路径/权限的改动，全量 grep 项目同类模式。
- **测试绿≠对**：至少手工测一条覆盖不到的边缘路径。
- **并行避冲突**：subagent 修改文件集不能有交集，改前 git status。
- **危改前存档**：涉及安全/架构/数据库/并发/跨 3+ 文件的改动前，先 `/checkpoint` 存快照。不用等用户说，自动存。
- **规则判层**：新增规则看"何时被读取"放 CLAUDE.md 还是 context/。
- 版本 2.1.156 锁定。

## 项目

- 代码：`/Users/jingzhe/Singularity/python/`
- 测试：`QIDIAN_SKIP_EMBED=1 python3 smoke_test.py(43) test_exec_run.py(21) unit_tests.py(24)`
- 服务：`QIDIAN_SKIP_EMBED=1 python3 app.py` → 127.0.0.1:5050
- 记忆：`~/.claude/projects/-Users-jingzhe/memory/MEMORY.md`
- 架构：`ARCHITECTURE.md`

## 记忆指针

- 会话启动 → `context/session-startup.md`
- 行为规范（详细版） → `context/behavior-core.md`
- 镜子模式 → `context/mirror-mode.md`
- 极简决策 → `context/minimal-decision.md`
- 知识库 → `knowledge/体系边疆.md`
- 认知速查 → `context/cognitive-quick-ref.md`
- 归档规则 → `context/archive-rules.md`
- Python教学 → 第一步强制 Skill("teach-me")
- 项目守门员 → `context/scope-guardian.md` + `context/quality-standards.md`
- 日常执行 → `~/Singularity/日常执行.md`
