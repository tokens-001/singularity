# Singularity — AI Agent 调度平台

多模型协作开发平台。用户提需求 → 6阶段流水线自动拆解/分配/执行/审查/交付。

## 定位

- **是什么**：AI Agent 调度平台，让多个 AI 模型按角色协作完成软件开发任务
- **核心能力**：任务分解 → 角色匹配 → 多模型并行执行 → GATE 审查 → 代码合并交付
- **当前版本**：v4.2，封存中，276 测试绿，功能完整但不继续开发

## 架构概览

```
用户输入 → Router(分类) → Orchestrator(调度) → Dispatcher(分发)
                                                    ↓
          6阶段流水线:  定义→G1→架构→G2→实现→集成→审查→G3→交付→DONE
                                                    ↓
          13角色并行:   架构师/前端/后端/测试/安全/DevOps/数据/产品/UI/QA/代码审查/DDD/研究院
                                                    ↓
          2档模型:      便宜(定义/实现/交付) + 强力(架构/审查/验收)
                                                    ↓
          3模型委员会:   架构/安全/研究员/系统架构 — 4角色多模型碰撞，其余单模型
                                                    ↓
          Executor → Validator → Merge → 产出代码+审查报告
```

## 核心模块

| 模块 | 位置 | 职责 |
|------|------|------|
| Router | `scheduler/router.py` | LLM 分类任务→匹配角色 |
| Orchestrator | `scheduler/orchestrator.py` | 主调度循环，管理阶段流转 |
| Dispatcher | `scheduler/dispatcher.py` | 分发任务到 executor，壳（实现在 `_dispatch_*.py`） |
| Workflow | `scheduler/workflow.py` | 6阶段流水线，壳（实现在 `_workflow_phases.py`） |
| Validator | `scheduler/validator.py` | GATE 审查，决定 pass/retry/abort |
| Memory | `scheduler/memory.py` | 任务记忆索引，壳（实现在 `_memory_*.py`） |
| Observer | `scheduler/_observer_*.py` + `observer/` | WebSocket 实时监控+主动执行 |
| Executors | `scheduler/executors/` | 模型调用适配（Claude/DeepSeek/Kimi/GLM/OpenAI） |
| Web | `web/app.py` (Flask) + `web/frontend/` (React) | 前端界面：Chat/Tasks/Config/Projects |
| Skills | `skills/*/SKILL.md` | 13个角色+observer子角色的系统提示词 |
| Neijinglu | `scheduler/neijinglu.py` | 任务执行日志，记录每个阶段输入/输出/裁决 |

## 关键设计决策

- **文件持久化**而非数据库 — `.qidian/` 下 JSON，可 grep、零依赖
- **Worktree 隔离** — git worktree 做任务沙箱，类 Docker 但更轻
- **GATE 人审断点** — G1/G2/G3 三处可暂停等人工确认
- **两档模型**而非三层 — 废弃了 E/E+/D，场景匹配更好
- **多模型碰撞不滥用** — 仅4个高风险角色用，其余单模型

## 硬约束

- **改后验证**：贴运行输出，不准"应该能跑"
- **外科手术**：只改目标代码，diff 只含必要变更
- **声称前核实**：说"已修复"→ grep 确认；说"N 行"→ wc -l
- **测试绿≠对**：手工测一条边缘路径
- **并行避冲突**：subagent 修改文件不交叠，改前 git status
- **危改前存档**：跨 3+ 文件或安全/架构改动前自动 `/checkpoint`
- **先读蓝图**：改奇点架构/流程前必须先读 `docs/专家团队架构.md` + `docs/ARCHITECTURE.md`
- **对比标注**：做技术对比时，推测和确认要分开标注，不确定就说"不确定"
- **禁止编造**：没读到源码/文档的实现细节不准编，直接说"没看到代码，不确定"

## 项目

- 代码：`src/singularity/`（scheduler / web / skills）
- 安装：`pip install -e .`
- 测试：`pytest tests/test_scheduler/ -q`（75）+ `python3 tests/test_exec_run.py`（21）
- 服务：`python3 -m singularity.web.app` → 127.0.0.1:5050
- 记忆：`~/.claude/projects/-Users-jingzhe/memory/MEMORY.md`
- 架构：`docs/ARCHITECTURE.md`
- 演化史：`docs/演化史.md` — 每次重要更新记录，旧文件不用留

## 恢复

```bash
cd ~/奇点
python3 -m singularity.web.app          # 本地
docker compose up -d --build            # Docker
```

当前状态：v4.2 封存，6阶段流水线串通，276测试绿，13角色+3模型委员会。无进行中任务。

## 指针

- 演化史 → `docs/演化史.md`
- 架构 → `docs/ARCHITECTURE.md` + `docs/专家团队架构.md`
- 个人知识 → `~/知识/`
