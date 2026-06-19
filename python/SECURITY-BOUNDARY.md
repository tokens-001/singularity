# 奇点调度平台 — 后端安全边界表

> 版本: v1.0 | 更新: 2026-06-19 | 覆盖范围: 60 个 API 端点

---

## 1. 系统概览

| 项目 | 说明 |
|------|------|
| 运行环境 | Flask (Python 3.14) |
| 网络绑定 | `127.0.0.1:5050`（仅本地） |
| 持久化 | 文件系统 JSON（`.qidian/` 目录），无数据库 |
| 外部依赖 | LLM API（DeepSeek/GLM/Kimi/OpenAI）、git worktree |
| 认证模式 | Token-based Bearer，默认关闭（`QIDIAN_AUTH=1` 启用） |
| CORS | 仅允许 `localhost`/`127.0.0.1` 来源 |

---

## 2. 接口输入全景

### 2.1 调度循环控制（3 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| POST | `/api/loop/start` | `concurrent` | int | 否 | `min(1, max(8, input))` |
| POST | `/api/loop/stop` | — | — | — | — |
| GET | `/api/loop/status` | — | — | — | — |

### 2.2 任务 CRUD（9 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/tasks` | `status`, `level` | query string | 否 | 无 |
| POST | `/api/tasks` | `description` | string | 是 | `len ≤ 8000` |
| | | `priority` | int | 否 | 无上限 |
| | | `depends_on` | list[string] | 否 | 不校验 ID 存在性 |
| | | `route_level` | string | 否 | `in ("E","D","E+")` |
| GET | `/api/tasks/<id>` | `task_id` | path | 是 | 直接拼入文件路径 |
| POST | `/api/tasks/<id>/cancel` | `task_id` | path | 是 | 同上 |
| POST | `/api/tasks/<id>/delete` | `task_id` | path | 是 | 删除 3 个目录下的同名文件 |
| POST | `/api/tasks/<id>/retry` | `task_id` | path | 是 | 状态转换 |
| POST | `/api/tasks/<id>/hold` | `task_id` | path | 是 | + `reason` (string, 否) |
| POST | `/api/tasks/<id>/release` | `task_id` | path | 是 | — |
| POST | `/api/tasks/<id>/override-route` | `task_id` | path | 是 | + `level` (必填, "E"/"D"/"E+"), `locked` (bool, 否) |
| POST | `/api/tasks/<id>/rollback` | `task_id` | path | 是 | 回滚 git 快照 |

### 2.3 任务追踪（2 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/tasks/<id>/trace` | `task_id`, `section`, `format` | path + query | 是 | 读 trace JSON 文件 |
| GET | `/api/tasks/<id>/timeline` | `task_id` | path | 是 | 读 task + trace + heartbeat |

### 2.4 Patch 与 Supervisor（2 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| POST | `/api/tasks/<id>/apply` | `task_id` | path | 是 | 读取 LLM 输出的 patch 文件，解析 `@files` 头，写入目标文件 |
| POST | `/api/tasks/<id>/supervise` | `task_id` | path | 是 | 只读校验 |

### 2.5 合并冲突（2 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/conflicts` | — | — | — | — |
| POST | `/api/conflicts/<id>/resolve` | `task_id`, `action` | path + body | 是 | `action ∈ ("manual","abort")` |

### 2.6 Memory / MAGMA（3 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/memory` | `q`, `files`, `beam`, `hops` | query | 否 | beam/hops 做 int() 转换 |
| GET | `/api/memory/chain/<id>` | `task_id`, `direction` | path + query | 是 | direction ∈ ("up","down","both") |
| POST | `/api/memory/rebuild` | — | — | — | — |

### 2.7 项目 CRUD + 流程（12 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/projects` | — | — | — | — |
| POST | `/api/projects` | `name` | string | 是 | `len ≤ 200` |
| | | `template` | string | 否 | 默认 "product_dev" |
| | | `description`, `scope` | string | 否 | 无长度限制 |
| | | `constraints` | list[string] | 否 | 无校验 |
| | | `budget` | float | 否 | 默认 5.0 |
| GET | `/api/projects/<id>` | `project_id` | path | 是 | 读 JSON 文件 |
| POST | `/api/projects/<id>/gate-confirm` | `project_id`, `gate`, `decision` | path + body | 是 | decision ∈ ("approved","rejected") |
| GET | `/api/templates` | — | — | — | — |
| POST | `/api/projects/<id>/run-phase` | `project_id` | path | 是 | 后台线程执行 |
| POST | `/api/projects/<id>/start` | `project_id` | path | 是 | 后台线程启动工作流 |
| POST | `/api/projects/<id>/auto` | `project_id` | path | 是 | 推进一个阶段 |
| POST | `/api/projects/<id>/autopilot` | `project_id` | path | 是 | 后台自动推进至 DONE |
| DELETE | `/api/projects/<id>/autopilot` | `project_id` | path | 是 | 发送停止信号 |
| GET | `/api/projects/<id>/cost` | `project_id` | path | 是 | — |
| GET | `/api/projects/<id>/lineage` | `project_id`, `limit` | path + query | 是 | limit 默认 50 |
| POST | `/api/projects/<id>/snapshot` | `project_id` | path | 是 | 创建 git stash |
| GET | `/api/projects/<id>/lineup` | `project_id` | path | 是 | — |
| PUT | `/api/projects/<id>/lineup` | `project_id`, `lineup` | path + body | 是 | lineup 为 dict |

### 2.8 Agent 配置（4 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/agents` | — | — | — | 返回含 `api_key_env` 字段 |
| POST | `/api/agents` | `model`, `level` | string | 是 | level 无枚举限制 |
| | | `type`, `entry`, `api_key_env`, `max_turns`, `roles`, `sandbox`, `mode`, `request_template` | mixed | 否 | entry 经 SSRF 白名单校验 |
| PUT | `/api/agents/<level>/<model>` | `level`, `model` | path | 是 | entry 经 SSRF 校验 |
| | | 任意更新字段 | body dict | 否 | — |
| DELETE | `/api/agents/<level>/<model>` | `level`, `model` | path | 是 | — |

### 2.9 API 厂商配置（4 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/api-store` | — | — | — | 返回 `api_key_env` 字段 |
| POST | `/api/api-store` | `id` | string | 是 | 无格式校验 |
| | | `provider`, `base_url`, `api_key_env`, `notes` | string | 否 | base_url 经 SSRF 白名单校验 |
| DELETE | `/api/api-store/<api_id>` | `api_id` | path | 是 | — |
| PUT | `/api/api-store/<id>/status` | `api_id`, `status`, `notes` | path + body | 是 | — |

### 2.10 模型注册（4 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/models` | — | — | — | — |
| GET | `/api/models/tier/<tier>` | `tier` | path | 是 | — |
| POST | `/api/models` | `id` | string | 是 | 无格式校验 |
| | | `provider`, `display`, `tiers`, `speed`, `cost`, `reasoning`, `max_turns`, `notes` | mixed | 否 | 默认值填充 |
| DELETE | `/api/models/<id>` | `model_id` | path | 是 | — |
| PUT | `/api/models/<id>` | `model_id` + 任意更新字段 | path + body | 是 | — |

### 2.11 认证与用户管理（4 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/auth/status` | — | — | — | 返回是否启用 + 用户列表 |
| POST | `/api/auth/bootstrap` | — | — | — | 创建/返回 admin |
| POST | `/api/auth/users` | `id`, `name`, `role` | string | 是 | role ∈ ("admin","operator","viewer") |
| | | `Authorization: Bearer <token>` | header | auth=1时必填 | 需要 `can_manage` |
| DELETE | `/api/auth/users/<id>` | `user_id` | path | 是 | 同上 |

### 2.12 监控 / 分析（3 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/judge-monitor` | — | — | — | 返回 judge 统计 |
| GET | `/api/model-profile` | — | — | — | 返回模型画像 |
| GET | `/api/model-profile/pattern` | `task_type`, `template_id` | query | 否 | 默认 "default" |

### 2.13 其他（6 端点）

| 方法 | 路径 | 输入参数 | 类型 | 必填 | 校验 |
|------|------|---------|------|------|------|
| GET | `/api/status` | — | — | — | 读取心跳文件 |
| POST | `/api/cleanup` | — | — | — | 删除心跳文件 |
| GET | `/api/token-usage` | — | — | — | — |
| PUT | `/api/token-budget` | `daily`, `monthly` | float | 否 | 默认 0 |
| GET | `/api/perf` | — | — | — | — |
| GET | `/api/reports` | `min`, `limit` | query | 否 | min 默认 "routine", limit 默认 30 |
| GET | `/api/reports/critical` | — | — | — | — |
| GET | `/api/events` | — | — | — | SSE 流，最多 20 连接 |
| GET | `/health` | — | — | — | 磁盘用量 + 状态 |

---

## 3. 登录状态与系统权限设计

### 3.1 认证机制

| 维度 | 实现 |
|------|------|
| 认证方案 | Bearer Token（无用户名/密码） |
| Token 生成 | `secrets.token_hex(16)` — 32 字符 hex，128-bit 熵 |
| Token 存储 | 明文存于 `.qidian/users.json`（文件权限 644），内存 dict 双索引 |
| Token 比对 | 字典查找（**非** constant-time） |
| Token 有效期 | **无过期机制**，永久有效 |
| Token 刷新 | 不支持 |
| 会话管理 | 无（Flask session 未使用） |
| 启用条件 | `QIDIAN_AUTH=1` 环境变量（**默认关闭**） |
| 文件路径 | `_auth.py:67-100` |

### 3.2 三层 RBAC 角色

| 角色 | `can_write` | `can_manage` | 权限范围 |
|------|-------------|-------------|---------|
| `admin` | ✅ | ✅ | 创建/删除用户，所有操作 |
| `operator` | ✅ | ❌ | 创建任务/项目，不可管理用户 |
| `viewer` | ❌ | ❌ | 只读（**未强制执行**） |

### 3.3 Auth 强制执行范围

| 状态 | 受保护端点 | 未保护端点 |
|------|----------|-----------|
| `QIDIAN_AUTH=1` | `POST /api/auth/users`, `DELETE /api/auth/users/<id>` | **其余 57 个端点全裸** |
| `QIDIAN_AUTH` 未设置 | 无 | **全部 59 个端点** |

### 3.4 实际防护层

```
网络层: 127.0.0.1 绑定 (唯一有效防护)
   ↓
CORS 层: localhost 来源白名单
   ↓
Auth 层: 默认关闭, 仅 2 个用户管理端点可被保护
   ↓
数据层: 无 ownership 字段, 无行级权限
```

---

## 4. 密码/Token 规则

| 规则项 | 当前状态 |
|--------|---------|
| Token 长度 | 32 字符 hex（128-bit） |
| Token 复杂度 | 由 `secrets` 模块生成，密码学安全 |
| 密码策略 | **无**（系统不使用密码） |
| 最小长度 | N/A |
| 锁定策略 | 无（无登录失败计数） |
| 历史限制 | 无 |
| 过期策略 | **无**（Token 永久有效） |
| 存储加密 | **明文存储**（.qidian/users.json） |
| 传输加密 | N/A（localhost 不经过网络） |
| 暴力破解防护 | 无 rate limiting |
| 日志暴露 | 启动时打印 `admin token: {前 8 位}...` |

---

## 5. 数据归属

### 5.1 数据模型

| 实体 | 持久化路径 | Owner 字段 | 归属隔离 |
|------|-----------|-----------|---------|
| Task | `.qidian/tasks/{id}.json` | **无 `created_by`** | ❌ 无隔离 |
| Project | `.qidian/projects/{id}.json` | **无 `created_by`** | ❌ 无隔离 |
| User | `.qidian/users.json` | `id`（身份标识） | N/A（用户管理） |
| Agent Config | `.qidian/agents_custom.json` | 全局共享 | ❌ 无隔离 |
| API Store | `.qidian/api_store.json` | 全局共享 | ❌ 无隔离 |
| Judge Monitor | `.qidian/judge_monitor.json` | 全局共享 | ❌ 无隔离 |
| Model Profile | `.qidian/model_profile.json` | 全局共享 | ❌ 无隔离 |
| Trace | `.qidian/traces/{id}.json` | 关联 task_id | ❌ 无 Task 级别隔离 |

### 5.2 访问控制结论

- **无多租户概念。** 系统为单用户设计。
- 任何能访问 `localhost:5050` 的进程/用户可读写所有数据。
- `project_id` 字段仅用于工作流关联，**非**访问控制。
- 即使 auth 启用，`viewer` 角色可直接调用 mutation 端点。

---

## 6. 注入风险详细说明

### 6.1 Shell 命令注入

| 风险类型 | 代码位置 | 输入来源 | 当前处理逻辑 | 风险等级 | 验证方式 |
|---------|---------|---------|-------------|---------|---------|
| CLI 进程执行 | `claude_cli.py:58-85` | `self.task`（用户任务描述） | `shlex.split` + `shell=False`，`{prompt}` 逐参数替换 | ✅ 已防护 | `shlex.split` 确保 prompt 为单一 argv 元素，`shell=False` 无 shell 解释 |
| Agent 工具执行 | `openai_agent.py:315-331` | LLM 生成的 `command` 参数 | `shlex.split` + `shell=False` + 环境变量脱敏（过滤 API_KEY/TOKEN/SECRET/PASSWORD） | ✅ 已防护 | LLM 可在沙箱内执行任意命令（30s 超时），但无法通过 shell 元字符逃逸 |
| Git 操作 | `worktree.py:25-31`, `snapshot.py:55-114` | 系统内部路径 | 硬编码 argv，`shell=False` | ✅ 已防护 | 无用户输入流入命令 |
| Git diff 追踪 | `claude_cli.py:134-148`, `openai_agent.py:362-378` | `baseline_ref`（git SHA） | 硬编码 argv | ✅ 已防护 | SHA 是系统生成的 git 对象 hash |

**验证命令:**
```bash
# 验证 shlex 不会让注入通过
python3 -c "import shlex; print(shlex.split('echo hello; rm -rf /'))"
# 输出: ['echo', 'hello;', 'rm', '-rf', '/'] — ';' 被当作字面量，不解释
```

### 6.2 路径穿越

| 风险类型 | 代码位置 | 输入来源 | 当前处理逻辑 | 风险等级 | 验证方式 |
|---------|---------|---------|-------------|---------|---------|
| 文件读写 | `openai_agent.py:291-297` (`_safe_path`) | LLM 生成的 `path` 参数 | `Path.resolve()` + 前缀检查 | ✅ 已防护 | resolve 消除 `..` 后再检查是否在 root 内 |
| Patch 写入 | `zhipu_api.py:174-178` | LLM 输出的 `@files` 声明 | `Path.resolve()` + 前缀检查 | ✅ 已防护 | 同上；拒绝时记录到 `failed` 列表 |
| 任务文件路径 | `tracker.py:113`, `app.py:437` | URL path 中的 `task_id` | 无显式校验，依赖 Flask 路由 | ⚠️ 中风险 | Flask 规范化 URL，但 defense-in-depth 缺失 |
| Worktree 路径 | `worktree.py:49-51` | 系统生成的 `task_id` + `agent_level` | 固定 internal path | ✅ 已防护 | 两个参数均为系统内部值 |

**验证命令:**
```bash
python3 -c "
from pathlib import Path
root = Path('/tmp/test').resolve()
dest = (root / '../../etc/passwd').resolve()
print('escaped:', not str(dest).startswith(str(root)))
# 输出: escaped: True
"
```

### 6.3 SSRF（服务端请求伪造）

| 风险类型 | 代码位置 | 输入来源 | 当前处理逻辑 | 风险等级 | 验证方式 |
|---------|---------|---------|-------------|---------|---------|
| API 调用 URL | `openai_agent.py:383-414` | `agents.toml` / `agents_custom.json` 中的 `entry` 字段 | 管理员配置文件控制 | ⚠️ 中风险 | 若攻击者可写 `.qian/agents_custom.json`，可重定向 API 调用 |
| URL 扫描 | `api_store.py:220-266` | `api_store.json` 中的 `base_url` | 拼接 `/models` 后发起 GET | ⚠️ 中风险 | 同上 |
| Agent 入口配置 | `app.py:1204-1228` (POST/PUT agents) | Web UI 传入的 `entry` URL | `_is_safe_api_url()` 白名单 + 内网 IP 拦截 | ✅ 已防护 | 仅允许 10 个已知 API 厂商域名 |
| API 厂商配置 | `app.py:1268-1296` (POST api-store) | Web UI 传入的 `base_url` | `_is_safe_api_url()` 白名单 | ✅ 已防护 | 同上 |

**SSRF 防护白名单:**
```
api.deepseek.com, api.moonshot.cn, open.bigmodel.cn,
api.openai.com, api.anthropic.com, dashscope.aliyuncs.com,
api.minimax.chat, api.baichuan-ai.com, api.stepfun.com,
api.lingyiwanwu.com
```

**验证命令:**
```bash
# 内网 IP 拦截
curl -s -X POST http://127.0.0.1:5050/api/api-store \
  -H 'Content-Type: application/json' \
  -d '{"id":"test","base_url":"http://169.254.169.254/latest/meta-data/"}'
# 预期返回: {"error": "不允许的 base_url（仅支持已知 API 厂商域名）"}

# 未知域名拦截
curl -s -X POST http://127.0.0.1:5050/api/api-store \
  -H 'Content-Type: application/json' \
  -d '{"id":"test","base_url":"http://evil.com"}'
# 预期返回: 同上错误
```

### 6.4 反序列化

| 风险类型 | 代码位置 | 输入来源 | 当前处理逻辑 | 风险等级 | 验证方式 |
|---------|---------|---------|-------------|---------|---------|
| LLM 输出解析 | `openai_agent.py:224-232` | LLM function call `arguments` JSON 字符串 | `json.loads` + 降级修复（单引号替换） | ✅ 安全 | stdlib json 无反序列化攻击面 |
| Task 文件加载 | `tracker.py:113` | 磁盘上的 task JSON 文件 | `json.loads` + `Task.from_dict(**d)` | ✅ 安全 | 文件由系统自身写入 |
| 配置文件加载 | `api_store.py:39-43,283`, `dispatcher.py:320` | `.qidian/*.json` | `json.loads` + 字段白名单 | ✅ 安全 | 仅使用 stdlib json |
| 模板渲染 | `zhipu_api.py:84-86` | `agents_custom.json` 中 `request_template` | `json.loads(json.dumps(tmpl))` 深拷贝 + 字符串替换 | ✅ 安全 | 纯字符串操作，无 eval |

**全局结论:** 整个代码库未使用 `pickle`、`yaml.load`、`eval`、`exec`。所有反序列化使用 `json.loads`。

**验证命令:**
```bash
grep -rn "pickle\|yaml.load\|eval(\|exec(" /Users/jingzhe/奇点/python/scheduler/ | grep -v "#\|\.pyc\|__pycache__"
# 预期输出: 无匹配（除 import 语句中的 yaml 模块名）
```

### 6.5 LLM 输出注入

| 风险类型 | 代码位置 | 输入来源 | 当前处理逻辑 | 风险等级 | 验证方式 |
|---------|---------|---------|-------------|---------|---------|
| 文件写入 | `openai_agent.py:308-313` (`_tool_write`) | LLM 生成的 `path` + `content` | `_safe_path` 防穿越，内容无过滤 | 🔴 高风险 | LLM 可在项目树内写任意文件 |
| 文件读取 | `openai_agent.py:299-306` (`_tool_read`) | LLM 生成的 `path` | `_safe_path` 防穿越，返回内容给 LLM | 🔴 高风险 | LLM 可读 `.env` 等敏感文件，内容进入对话 |
| Patch 声明 | `zhipu_api.py:157-182` (`apply_patch`) | LLM 输出的 `@files` 声明 | 正则解析 + 路径前缀检查 | 🔴 高风险 | `@files: .env` 可覆盖敏感文件 |
| Trace 存储 | `orchestrator.py:325-386`, `neijinglu.py:128` | LLM 原始输出 | JSON 序列化后存磁盘 | 🟡 中风险 | 下游读取 trace 的消费者可能受影响 |
| Planner 内容 | `_exec.py:387-390` | Planner LLM 输出 | 写入 markdown 文件 | 🟡 中风险 | Planner 内容可流入 E+ 子任务描述，传播注入 |

**验证方式:**
- 当前无敏感文件 blocklist 防护
- 攻击场景：prompt injection → LLM 执行 `write_file("/.env", "恶意内容")` → 覆盖环境变量文件
- 缓解：需加文件 blocklist（`.env`, `*.json` 配置, `.git/` 等）

### 6.6 SQL/NoSQL 注入

| 风险类型 | 代码位置 | 当前处理逻辑 | 风险等级 |
|---------|---------|-------------|---------|
| SQL 注入 | 全局 | **无 SQL 数据库**，纯文件系统 JSON 持久化 | ✅ 无风险 |
| NoSQL 注入 | 全局 | **无 NoSQL 数据库**，ChromaDB 仅用于本地向量搜索 | ✅ 无风险 |

---

## 7. 当前防御层总览

```
┌──────────────────────────────────────────────────┐
│ 网络层                                             │
│ ✅ 127.0.0.1 绑定（2026-06-19 加固）               │
│ ✅ CORS localhost 白名单（2026-06-19 加固）         │
├──────────────────────────────────────────────────┤
│ 输入校验层                                         │
│ ✅ task description ≤ 8000 字符                     │
│ ✅ project name ≤ 200 字符                          │
│ ✅ concurrent ∈ [1, 8]                             │
│ ⚠️ depends_on 不校验 ID 存在性                      │
│ ⚠️ 大量端点输入无长度/格式校验                        │
├──────────────────────────────────────────────────┤
│ Auth 层                                           │
│ ⚠️ 默认关闭（需 QIDIAN_AUTH=1）                    │
│ ⚠️ 仅 2/59 端点受保护                                │
│ ⚠️ viewer 角色无强制只读                             │
├──────────────────────────────────────────────────┤
│ 注入防护层                                         │
│ ✅ Shell: shell=False + shlex.split（全代码库）      │
│ ✅ 路径穿越: Path.resolve() + 前缀检查                │
│ ✅ SSRF: URL 白名单 + 内网 IP 拦截                   │
│ ✅ 反序列化: 仅 json.loads，无 pickle/eval/exec       │
│ ❌ LLM 输出: 无敏感文件 blocklist                     │
├──────────────────────────────────────────────────┤
│ 数据层                                            │
│ ⚠️ 无 created_by 字段，无数据归属                     │
│ ⚠️ Token 明文存储                                   │
│ ✅ API key 本身不存入文件系统                         │
│ ✅ 环境变量脱敏（openai_agent subprocess 过滤）       │
├──────────────────────────────────────────────────┤
│ 资源控制层                                         │
│ ✅ SSE 连接 ≤ 20                                   │
│ ✅ concurrent ≤ 8                                   │
│ ⚠️ 无 API rate limiting（可无限提交任务）             │
│ ⚠️ 无 worktree 数量限制                             │
│ ⚠️ 无 LLM API 消费硬上限                             │
└──────────────────────────────────────────────────┘

图例: ✅ 已加固  ⚠️ 部分/弱防护  ❌ 未防护  🔴 设计风险
```

---

## 8. 待修复 Gap 清单

| 优先级 | 类别 | 问题 | 建议 |
|--------|------|------|------|
| P0 | LLM 注入 | 无敏感文件 blocklist | `_tool_read`/`_tool_write`/`apply_patch` 加文件名黑名单（`.env`, `*.json`, `.git/`） |
| P0 | 数据归属 | 无 `created_by` | 单用户场景可接受，但需在文档声明 |
| P1 | Auth 覆盖 | 仅 3% 端点受保护 | 若启用 auth，应保护所有 POST/PUT/DELETE |
| P1 | 限流 | 无 API rate limit | 加 Flask-Limiter 或简易计数器 |
| P1 | Worktree | 无数量上限 | 加 `len(list_worktrees()) < 50` 检查 |
| P2 | Token 安全 | 明文存储 | hashlib.sha256 哈希存储 |
| P2 | Token 安全 | 无过期 | 加 `created_at` + 30 天过期检查 |
| P2 | 输入校验 | depends_on 无验证 | 校验引用的 task_id 是否真实存在 |
| P3 | 错误信息 | 部分端点泄露内部状态 | 统一用 `"服务器内部错误"` 代替 `str(e)` |
| P3 | 日志安全 | admin token 前 8 位打印 | 完全隐藏或只打印存在性确认 |
