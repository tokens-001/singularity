# API 参考

Base: `http://127.0.0.1:5050`

## 任务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 任务列表，`?status=pending\|running\|done\|failed` |
| GET | `/api/tasks/:id` | 任务详情 |
| GET | `/api/tasks/:id/trace` | 任务 trace |
| GET | `/api/tasks/:id/timeline` | 任务时间线 |
| POST | `/api/tasks` | 创建，body: `{description, route_type?, route_level?}` |
| POST | `/api/tasks/:id/cancel` | 取消 |
| POST | `/api/tasks/:id/retry` | 重试 |
| POST | `/api/tasks/:id/hold` | 暂停 |
| POST | `/api/tasks/:id/release` | 释放 |

## 项目

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/projects` | 项目列表 |
| GET | `/api/projects/:id` | 项目详情 |
| POST | `/api/projects` | 创建，body: `{name, description, template}` |
| POST | `/api/projects/:id/run-phase` | 手动推进阶段 |
| POST | `/api/projects/:id/gate-confirm` | GATE 审批，body: `{gate, decision}` |

Phase: `template` → `researching` → `gate1` → `planning` → `gate2` → `executing` → `integrating` → `reviewing` → `gate3` → `delivering` → `done`

## Observer

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/observer/chat` | 对话，body: `{question}` |

## 智能体

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 智能体列表 |
| POST | `/api/agents` | 添加/启用，body: `{model, level?, type?, ...}` |
| PUT | `/api/agents/any/:model` | 更新配置 |
| DELETE | `/api/agents/any/:model` | 禁用 |
| GET | `/api/agents/any/:model/skills` | 技能绑定 |
| PUT | `/api/agents/any/:model/skills` | 更新技能绑定，body: `{skills: [...]}` |

## 模型

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/models` | 模型目录 |
| POST | `/api/models` | 添加模型 |
| PUT | `/api/models/:id` | 更新模型 |
| DELETE | `/api/models/:id` | 删除模型 |
| POST | `/api/models/import` | 批量导入 |

## API 连接

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/api-store` | API 连接列表 |
| POST | `/api/api-store` | 添加连接 |
| DELETE | `/api/api-store/:id` | 删除连接 |
| POST | `/api/api-store/:id/scan` | 扫描厂商模型 |

## 技能

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 技能列表 |
| POST | `/api/skills` | 创建技能 |
| DELETE | `/api/skills/:name` | 删除技能 |

## 调度 & 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 系统状态 |
| POST | `/api/loop/start` | 启动调度循环 |
| POST | `/api/loop/stop` | 停止调度循环 |
| GET | `/api/loop/status` | 循环状态 |
| GET | `/api/token-usage` | Token 用量 |
| PUT | `/api/token-budget` | 设置预算 |
| GET | `/api/events` | SSE 事件流 |
