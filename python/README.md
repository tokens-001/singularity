# 奇点 Agent 调度平台

多模型协同调度引擎 — E/E+/D 三层模型分层 → 任务调度 → 执行验证 → 项目工作流。

## 快速启动

```bash
# 本地
bash start.sh           # 前台
bash start.sh -d        # 后台

# Docker
bash start.sh docker    # docker compose up -d --build
```

→ Web 控制台: **http://127.0.0.1:5050**

## 环境配置

复制 `.env` 模板，填入你的 API Key（`start.sh` 首次运行自动生成模板）：

```ini
DEEPSEEK_API_KEY=sk-...
ZHIPU_API_KEY=...
OPENAI_API_KEY=sk-...
KIMI_API_KEY=sk-...
```

> `ANTHROPIC_API_KEY` 仅 Claude Code CLI 模式需要，`openai-agent` 模式用不到。

## 架构

```
E 层 (日常执行) ── DeepSeek Chat / GLM-5 Turbo / Kimi K2.7 / DeepSeek V4 Pro (CLI)
E+ 层 (复杂构建) ── GLM-5.2
D 层 (架构设计) ── Claude Opus 4.8 / GPT-5.5 / GPT-5.5 Pro / GLM-5.2 / DeepSeek V4 Pro
```

前端 `配置 → 架构切换` 可实时开关每层模型，无需改配置文件。

## CLI

```bash
# 调度循环
python3 -m scheduler loop                    # 常驻循环
python3 -m scheduler status                  # 队列状态

# 项目工作流
python3 -m scheduler project create "项目名" --template product_dev
python3 -m scheduler project list
python3 -m scheduler project show <id>
python3 -m scheduler project advance <id> --yes    # 推进 (费用确认)
python3 -m scheduler project delete <id>

# 记忆
python3 -m scheduler memory stats
python3 -m scheduler memory query "关键词"

# 看板
python3 tools/dash.py
```

## 项目工作流

```
📋 模板 → 🔍 调研(E层) → ①门 → 🏗 架构(D层) → ②门 → ⚡ 执行 → ③门 → 🔎 审查(D层) → ④门 → ✅ 完成
```

- 调研/架构/审查：同步 LLM 调用，产出回写项目文件
- 执行：tracker 任务分发，由调度循环消费，按 DAG 依赖顺序执行
- 审查→修复→再审查 循环（≤5 轮）
- 自动模式跳过 Owner Gate

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 + 磁盘 + 循环状态 |
| `/api/status` | GET | 任务队列统计 |
| `/api/tasks` | GET/POST | 任务列表 / 创建 |
| `/api/tasks/<id>` | GET | 任务详情 + 时间线 |
| `/api/tasks/<id>/cancel` | POST | 取消任务 |
| `/api/tasks/<id>/delete` | POST | 删除任务 |
| `/api/agents` | GET | Agent 列表 |
| `/api/models` | GET/POST | 模型库 CRUD |
| `/api/api-store` | GET/POST | API 库 CRUD |
| `/api/events` | GET | SSE 实时事件流 |
| `/api/projects` | GET/POST | 项目列表 / 创建 |
| `/api/projects/<id>/cost` | GET | 费用估算 |
| `/api/projects/<id>/gate-confirm` | POST | Gate 确认 |

## 测试

```bash
QIDIAN_SKIP_EMBED=1 python3 smoke_test.py
```

## 关键文件

| 文件 | 职责 |
|------|------|
| `app.py` | Flask Web 控制台 + SSE 推送 |
| `scheduler/orchestrator.py` | 调度核心：选队 → dispatch → 验证 → 合并 |
| `scheduler/dispatcher.py` | Agent 选取 + 执行分发 |
| `scheduler/workflow.py` | 10 阶段项目工作流引擎 |
| `scheduler/memory.py` | MAGMA 多图记忆 (语义+因果+时间+实体) |
| `scheduler/validator.py` | L1-L4 验证护栏 |
| `scheduler/project.py` | 项目状态机 + 持久化 |
| `scheduler/api_store.py` | API 库自动发现 + 状态管理 |
| `scheduler/model_registry.py` | 模型能力注册 + 查询 |
| `scheduler/agents.toml` | Agent 配置 |
| `scheduler/models.toml` | 模型能力注册表 |
| `scheduler/executors/openai_agent.py` | 通用 Agent Runtime (OpenAI 兼容 API) |
| `templates/index.html` | Web 控制台前端 |
| `tools/dash.py` | 终端彩色看板 |
