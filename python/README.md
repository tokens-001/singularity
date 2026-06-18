# 奇点 Agent 调度平台

多模型协同调度引擎 — 路由→执行→验证→合并，10 阶段项目工作流。

## 启动

```bash
cd 奇点/python
python3 app.py
# → http://127.0.0.1:5050
```

需在 `.env` 配 API key（启动自动加载）：
```
DEEPSEEK_API_KEY=sk-...
ZHIPU_API_KEY=...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
KIMI_API_KEY=sk-...
```

## 架构

```
E 层 (日常) ── DeepSeek Flash / GLM-5 Turbo / Kimi K2.7
E+ 层 (构建) ── GLM-5.2
D 层 (架构) ── Claude Opus / GPT-5.5 / DeepSeek V4 Pro / GLM-5.2
```

前端 `配置 → 架构切换` 可实时开关每层模型。

## CLI

```bash
# 任务
python3 -m scheduler add "修 bug"         # 入队
python3 -m scheduler run "修 bug"         # 入队+立即跑
python3 -m scheduler loop                 # 常驻循环
python3 -m scheduler status               # 队列状态

# 项目工作流
python3 -m scheduler project create "项目名" --template product_dev
python3 -m scheduler project list
python3 -m scheduler project show <id>
python3 -m scheduler project advance <id> --yes    # 有费用确认
python3 -m scheduler project advance <id> --approve  # Gate 批准
python3 -m scheduler project reject <id>            # Gate 打回

# 记忆
python3 -m scheduler memory stats
python3 -m scheduler memory query "关键词"
```

## 项目工作流

```
📋模板 → 🔍调研(E层) → ①门 → 🏗架构(D层) → ②门 → ⚡执行 → ③门 → 🔎审查(D层) → ④门 → ✅完成
```

- 调研/架构/审查为同步 LLM 调用，产出回写项目
- 执行为 tracker 任务分发，由调度循环消费
- 审查→修复→再审查循环（≤5 轮）
- 自动模式跳过 Owner Gate

## 关键文件

| 文件 | 职责 |
|------|------|
| `app.py` | Flask Web 控制台 |
| `scheduler/orchestrator.py` | 调度核心：取队→dispatch→validate→merge |
| `scheduler/dispatcher.py` | Agent 选取 + 执行 |
| `scheduler/workflow.py` | 10 阶段项目工作流引擎 |
| `scheduler/memory.py` | MAGMA 多图记忆（语义+因果+时间+实体） |
| `scheduler/validator.py` | L1-L4 验证护栏 |
| `scheduler/supervisor.py` | 监督层四维质检 |
| `scheduler/project.py` | 项目状态机 + 持久化 |
| `scheduler/agents.toml` | Agent 配置 |
| `scheduler/models.toml` | 模型能力注册 |

## 数据目录

```
.qidian/
  tasks/        # 任务 JSON
  projects/     # 项目 JSON
  heartbeats/   # 心跳文件
  snapshots/    # git 快照
  traces/       # 执行追踪
  logs/         # 调度日志
  agents_custom.json  # 自定义 Agent 配置
```
