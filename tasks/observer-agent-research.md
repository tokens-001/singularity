# 观察者智能体 — 代码库调研报告

> 2026-06-23，基于 6 维度并行探索，覆盖通信层/数据源/执行器/守护线程/前端/生命周期。

---

## 一、通信层：bridge.py（WebSocket 服务器）

**文件**：`src/singularity/scheduler/bridge.py`（170 行）

### 现状

- **库**：`websockets`（运行时导入，未在 pyproject.toml 声明但环境已安装 v16）
- **架构**：独立 daemon 线程跑 asyncio event loop，绑定 `127.0.0.1:5051`
- **启动**：`start_ws_server()` 在 `app.py:1472` 调用
- **停止**：`stop_ws_server()` 在信号处理器的优雅关闭中调用，关闭所有客户端连接并停 event loop

### 协议

JSON-RPC 2.0，**单向推送**（服务器→客户端）：

```json
{"jsonrpc": "2.0", "method": "event", "params": {"kind": "<type>", "msg": "<text>", "ts": <float>}}
```

认证：第一条消息必须是 `{"method": "auth", "params": {"token": "..."}}`，验证通过后标记 `authenticated=True`。`broadcast_json()` 跳过未认证客户端。

### 关键发现

- **客户端→服务器的消息被全部丢弃**（`bridge.py:125-126`：`async for _ in ws: pass`）
- 设计意图明确写在注释："客户端通过 HTTP API 交互，WS 仅用于推送"
- 没有消息路由/分发机制，没有 handler 注册表
- `_WSClient` 已有 `send_json()` 方法（支持单连接回复），基础设施存在
- 事件推送与 SSE 广播同步进行（`app.py:489-497`，`kind != "ping"` 时双管道推送）

### 双向聊天需要的改动

| 改动 | 规模 | 说明 |
|------|------|------|
| 替换 idle loop 为消息分发器 | **中** | `async for _ in ws: pass` → 按 `method` 字段路由到不同 handler |
| 新增消息类型 | **小** | 加 `chat`、`command` 等 method，每个有独立 handler |
| 请求/响应关联 | **小** | JSON-RPC 已有 `id` 字段，回传即可 |
| 流式响应 | **中** | 一次对话产生多条服务器消息（thinking → partial → done），共享 correlation ID |
| 连接亲和性 | **小** | `_WSClient` 引用在 handler 内可用，直接 `client.send_json()` |

### 风险

- **线程安全**：Flask 线程 ↔ asyncio event loop 跨线程通信，已有 `run_coroutine_threadsafe` 模式可复用
- **认证**：已有 token 验证，Observer 聊天直接复用，不需要新认证机制

### 改动评估：**中**

---

## 二、数据源：观察者可读取的所有数据

### 2.1 witness.py — 心跳与系统状态

**文件**：`src/singularity/scheduler/witness.py`（~210 行）

| 查询方法 | 返回 | 用途 |
|---------|------|------|
| `check_stalled(timeout_seconds=600) -> list[str]` | 停滞任务 ID 列表 | F3 异常检测 |
| `_count_by_status() -> dict[str, int]` | 各状态任务计数 | F1 系统状态 |
| `_heartbeat_task_levels() -> dict[str, int]` | 各级别活跃心跳数 | F1 系统状态 |
| `_timing_stats() -> tuple[list, list]` | 等待时间列表 + 完成耗时列表 | F1 统计 |
| `_token_stats() -> dict[str, int]` | 各级别 token 消耗 | F1 成本 |
| `status(agents=None) -> str` | 格式化多行状态报告 | F1 综合查询 |
| `force_cleanup_heartbeats() -> tuple[int, int]` | 清理计数 | F3 泄漏检测 |

数据来源：心跳文件 `<QIDIAN_DIR>/heartbeats/<task_id>_<level>.json` 和 trace 文件。

### 2.2 tracker.py — 任务状态机

**文件**：`src/singularity/scheduler/tracker.py`（~530 行）

| 查询方法 | 返回 | 用途 |
|---------|------|------|
| `_read(task_id) -> Optional[Task]` | 完整 Task 对象（22 字段） | F2 任务级查询 |
| `list_pending() -> list[Task]` | 就绪任务，按 priority+starvation 排序 | F1 队列状态 |
| `next_ready() -> Optional[Task]` | 最高优先级就绪任务 | F1 队列状态 |
| `ready_tasks(exclude) -> list[Task]` | DAG 就绪任务（含副作用：提升 ROUTED/阻塞未满足/失败死依赖） | F1 队列状态 |
| `dag_metrics() -> dict` | omega/δ/gamma/node_count/edge_count/components/topology_hint | F1 拓扑分析 |
| `_load_all_tasks() -> list[Task]` | 全部任务 | F1 全量扫描 |

Task 字段（dataclass，22 字段）包括：id, description, status, priority, depends_on, route_level, route_gate, route_type, error, retry_count, max_retries, created_at, updated_at, starvation_score, children, depth, held, held_reason, project_id 等。

读操作受 `RLock` 保护，并发安全。

### 2.3 _api_monitor.py — HTTP 可访问的聚合接口

**文件**：`src/singularity/scheduler/_api_monitor.py`（~110 行）

所有 handler 返回 `(dict, status_code)`：

| Handler | 内容 |
|---------|------|
| `status_overview()` | 任务计数 + 心跳级别 + 运行总数 + 平均等待/耗时 + token 合计 + 停滞列表 |
| `token_usage()` | Token 预算使用统计 |
| `perf_stats()` | 性能统计 |
| `dag_metrics()` | DAG 结构指标 |
| `judge_monitor_status()` | 裁判监控统计 |
| `model_profile_status()` | 模型画像摘要 |
| `model_profile_pattern()` | 模型路由模式 |
| `reports_critical()` | 近期严重 chancellor 报告 |
| `reports_list()` | 近期 chancellor 报告（30 条） |
| `template_list()` | 任务模板目录 |
| `health_check()` | 系统健康：状态、磁盘、loop 运行中、SSE 客户端数、项目数 |
| `auth_status()` | 认证配置 |
| `cleanup()` | 清理计数 |

### 2.4 execution_judge.py — 执行裁判

**文件**：`src/singularity/scheduler/execution_judge.py`（~500 行）

| 方法 | 返回 | 用途 |
|------|------|------|
| `judge(task_desc, output, task_type) -> JudgeVerdict` | pass/score/reason/failure_mode/uncertain | F2 裁判结果 |
| `should_retry(verdict, retry_count, max_retries) -> bool` | 重试决策 | F2 决策链 |
| `build_reflexion_feedback(verdict) -> str` | 反思反馈文本 | F2 决策链 |
| `classify_finding(finding) -> str` | 发现分类 | F2 代码审查结果 |
| `fuse_outputs(task_desc, output_a, output_b, ...) -> str` | Fusion 合成结果 | F2 多模型融合结果 |
| `execute_fusion_tool(question, tier) -> str` | Fusion 工具调用 | F2 融合查询 |

`JudgeVerdict` 字段：`pass_`, `score`, `reason`, `failure_mode`, `uncertain`。

裁判本身是无状态的——每次调用独立判断。持久化由 judge_monitor 处理。

### 2.5 judge_monitor.py — 裁判统计与异常检测

**文件**：`src/singularity/scheduler/judge_monitor.py`（~370 行）

`JudgeMonitorStore.get_stats()` 返回：
- **pass_rates_by_type**：按任务类型的通过率和均分
- **model_correlations**：按模型的总判定数、通过数、均分、失败模式分布、分歧重试数、偏差标记
- **score_distribution**：5 桶直方图（0.0-1.0）
- **anomalies**：检测到的异常列表（kind/detail/threshold/observed/detected_at）
- **disagreement_rate**：分歧率
- **total_judgments**：总判定数

异常检测规则：
1. 某任务类型 pass_rate > 95% → `excessive_pass_rate`
2. 某任务类型 pass_rate < 5% → `low_pass_rate`
3. 某模型 avg_score < 0.3 → `model_bias`

### 汇总：观察者可用的所有数据

| 类别 | 数据 | 源 |
|------|------|-----|
| 系统状态 | 任务计数、心跳数、停滞列表、token 消费、磁盘空间 | witness + _api_monitor |
| 队列状态 | 就绪任务、优先级、饥饿分数、DAG 拓扑 | tracker |
| 任务详情 | 完整 22 字段 Task 对象、执行路径、错误信息 | tracker |
| 裁判结果 | pass/score/reason/failure_mode、重试决策、Fusion 结果 | execution_judge |
| 裁判统计 | 通过率、模型偏差、分数分布、异常事件 | judge_monitor |
| 模型数据 | 模型画像、成本/速度、路由模式 | model_profile |
| 运维数据 | 报告、模板、认证状态 | _api_monitor |

### 改动评估：**小**（数据源已完备，观察者只需直接 import 调用）

---

## 三、执行器模式：复用 OpenAIAgentExecutor

### 现状

**文件**：
- `src/singularity/scheduler/executors/base.py`（~95 行）— 抽象基类
- `src/singularity/scheduler/executors/openai_agent.py`（~490 行）— OpenAI function calling 实现

### BaseExecutor 接口

```python
class BaseExecutor:
    def __init__(self, agent_cfg, task, task_id,
                 baseline_ref="", cwd="", agent_level="", **kwargs):
        ...

    def run(self) -> ExecutorResult:
        raise NotImplementedError
```

`ExecutorResult`：success, raw_output, changed_files, patch_path, elapsed, token_count, error, error_kind, tool_events。

### OpenAIAgentExecutor 运行循环

1. **工具装配**：`TOOLS`（4 内置） + `_skill_tools` + `_mcp_tools`
2. **系统提示**：`SYSTEM_PROMPT` + `_skill_prompt`
3. **API 调用**：支持 Chat Completions 和 Responses API 两套格式
4. **工具执行**：`_execute_tool(name, args)` 分发到 4 种路径（内置/技能/MCP/未知）
5. **死循环检测**：同工具+同参数连续 2 轮 → 强制结束
6. **最终答案**：模型不再调用工具时输出 content
7. **轮次耗尽**：达到 `max_turns` → 返回失败

### 四个内置工具

| 工具 | 类型 | 观察者需要？ |
|------|------|------------|
| `read_file` | 只读 | ✅ 保留 |
| `write_file` | 写 | ❌ 移除/禁止 |
| `run_command` | 执行 | ❌ 移除/禁止 |
| `search_code` | 只读 | ✅ 保留 |

### 三个工具注入点

1. **skill_tools**：OpenAI function calling 格式 + `skill.expand_body(**args)` 实现
2. **mcp_tools**：OpenAI function calling 格式 + `mcp_executor(name, args)` 回调
3. **permission_checker**：`(tool_name, args, level, model, task_id) -> (allowed, reason)` — 可在权限层阻止写/执行

### 观察者复用方案

**不继承也不修改 OpenAIAgentExecutor**，直接实例化时注入定制参数：

| 定制点 | 默认值 | 观察者配置 |
|--------|--------|-----------|
| `TOOLS` | 读/写/跑/搜 | 替换为只读查询工具（query_task/get_status/list_stalled/...） |
| `SYSTEM_PROMPT` | "你是编程 agent" | "你是系统观察者，用只读工具查询状态，不写文件不跑命令" |
| `permission_checker` | 按等级/模型/工具名检查 | `write_file` 和 `run_command` 直接拒绝 |
| `mcp_tools` | MCP 服务器工具 | 注入观察者专用查询工具 |
| `max_turns` | 10 | 可降低到 5（聊天不需要多轮工具调用） |
| `max_tool_turns` | 3 | 可降低到 2 |

**不需要改动**：整个 run 循环、API 调用、响应解析、死循环检测、错误处理、ExecutorResult 打包——全部复用。

### 风险

- `_track_changed_files()` 会调 git diff，观察者不写文件所以 diff 为空，无害
- 需要创建新的 `agent_cfg` 条目（模型选便宜的 DeepSeek Chat，因为观察者只需要推理不写代码）

### 改动评估：**小**（实例化配置，不改现有类）

---

## 四、守护线程模式：conductor.py autopilot

### 现状

代码库有 **3 种**守护线程模式：

### 模式 1：Conductor Autopilot（conductor.py:229-303）

```python
_autopilot_threads: dict[str, threading.Thread] = {}
_autopilot_stop: dict[str, bool] = {}

def start_autopilot(project_id: str) -> dict:
    _autopilot_stop[project_id] = False
    def _run():
        for step in range(max_steps):       # 有限循环
            if _autopilot_stop.get(project_id, False):
                break
            do_work()
            if idle:
                exponential_backoff_sleep() # 5→10→20→…→300s
        cleanup()
    t = threading.Thread(target=_run, daemon=True)
    _autopilot_threads[project_id] = t
    t.start()

def stop_autopilot(project_id: str):
    _autopilot_stop[project_id] = True
```

特点：有限循环、指数退避、文件轮询、协作停止。

### 模式 2：Scheduler Loop（app.py:344-519）⭐ 推荐模板

```python
_loop_stop: threading.Event = threading.Event()

def start_loop(concurrent: int = 1):
    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_loop_worker, daemon=True)
    _loop_thread.start()

def _loop_worker():
    while not _loop_stop.is_set():
        try:
            results = orchestrator.run_queue(agents)
            if no_work: sleep(3)
            else: process_results()
        except Exception as e:
            _push_event("error", f"loop error: {e}")
            sleep(5)
    _push_event("system", "loop stopped")

def stop_loop():
    _loop_stop.set()
```

特点：`threading.Event`（天然线程安全）、每轮 `try/except Exception` 兜底、空闲退避、SSE 事件推送。

### 模式 3：WebSocket Server（bridge.py:135-169）

独立 asyncio event loop 在 daemon 线程中运行 `websockets.serve`。用 `await asyncio.Future()` 保持永远运行。

### 推荐：Observer Agent 守护线程模板

基于模式 2（`_loop_worker`），因为：
- `threading.Event` 比 dict[bool] 更安全
- 每轮 try/except 防止单次异常炸线程
- 已有 `_pending_sse_events` 跨组件通信模式
- 已有 `_push_event` / SSE 广播推送结果

```python
_observer_stop = threading.Event()
_observer_thread: threading.Thread | None = None

def start_observer():
    _observer_stop.clear()
    _observer_thread = threading.Thread(target=_observer_worker, daemon=True)
    _observer_thread.start()

def _observer_worker():
    # 初始化：加载观察者 LLM（便宜模型）
    while not _observer_stop.is_set():
        try:
            # 1. 检查异常条件（停滞/偏差/成本）
            anomalies = _check_anomalies()
            for a in anomalies:
                _push_event("observer_alert", a)
            # 2. 处理待处理的聊天消息队列
            _process_chat_queue()
        except Exception as e:
            _log.error(f"observer error: {e}")
        _observer_stop.wait(interval)  # 用 wait 代替 sleep，stop 时立即响应

def stop_observer():
    _observer_stop.set()
```

### 改动评估：**小**（复制 _loop_worker 模式，~50 行新代码）

---

## 五、前端集成

### 文件结构

| 文件 | 行数 | 内容 |
|------|------|------|
| `web/templates/index.html` | 440 | 主模板 |
| `web/static/style.css` | 467 | 全部 CSS（CSS 变量深色主题） |
| `web/static/js/utils.js` | 174 | 标签切换、API 助手、toast、状态 |
| `web/static/js/dashboard.js` | 232 | 仪表盘卡片、agent 行、流程、token |
| `web/static/js/tasks.js` | 274 | 任务表格、详情、决策链 |
| `web/static/js/project.js` | 277 | 项目管理 |
| `web/static/js/config.js` | 1200 | SSE/配置/技能/MCP/权限/审批 |
| `web/static/app.js` | - | 外部文件，不存在于本地 |

### 标签页切换机制（utils.js:73-83）

```javascript
document.getElementById('tab-bar').addEventListener('click', e => {
  if (!e.target.classList.contains('tab')) return;
  activeTab = e.target.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  e.target.classList.add('active');
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + activeTab).classList.add('active');
  // 按需初始化
  if (activeTab === 'tasks') { renderTasks(); ... }
  if (activeTab === 'project') loadProjects();
  if (activeTab === 'config') { renderAPIStore(); ... }
});
```

### 关键技术细节

- **无框架**：原生 JS，无 React/Vue/Bootstrap/Tailwind
- **CSS 变量**：`--bg`, `--surface`, `--text`, `--accent`, `--st-done`, `--st-run`, `--st-fail` 等，暗色默认
- **HTTP 助手**：`api(path, opts)` 在 utils.js:97-105，返回 `fetch().then(r=>r.json())`
- **RAF 批处理**：`scheduleDOM(fn)` 在 utils.js:37-53
- **SSE**：`EventSource` 连接 `/api/events`，断线自动重连（指数退避 1s→30s）
- **无 WebSocket 前端连接**：bridge.py 的后端 WS 服务器存在，但 JS 代码中没有任何 `new WebSocket(...)` 调用

### 添加聊天标签页的改动

| 文件 | 改动 | 规模 |
|------|------|------|
| `index.html` | 加 `<div class="tab" data-tab="chat">对话</div>` + `<div class="tab-content" id="tab-chat">` 含消息列表+输入框 | **小** |
| `style.css` | 加聊天气泡/输入区样式 ~50 行，用已有 CSS 变量 | **小** |
| `utils.js` | tab-switch handler 加 `if (activeTab === 'chat') initChat()` | **小** |
| `static/js/chat.js` | **新文件**：WebSocket 连接 + JSON-RPC auth + 消息收发 + UI 渲染 ~150 行 | **小** |
| `index.html` | 加 `<script src="/static/js/chat.js">` | **小** |

### 改动评估：**小**（5 个文件，总计 ~250 行新代码）

---

## 六、启动/停止时机

### 启动序列（app.py `if __name__ == "__main__"` 块）

```
1. 模块导入 + 路由注册（import 时）
2. 信号处理器注册（SIGTERM/SIGINT → _graceful_shutdown）
3. 启动自检（模型注册表、tracker 缓存预热）
4. MCP 初始化（加载配置）
5. WebSocket 服务器启动    ← app.py:1472
6. Flask app.run()          ← app.py:1476
```

**注意**：调度循环（`_loop_worker`）不是自动启动的——通过 `/api/loop/start` HTTP 端点按需启动。autopilot 同理，按项目按需启动。

### 停止序列（`_graceful_shutdown`，app.py:1430-1442）

```python
def _graceful_shutdown(signum, frame):
    orchestrator.stop_loop()  # ⚠️ BUG: 此方法不存在，抛 AttributeError
    ws_bridge.stop_ws_server()
    # MCP 断开
    sys.exit(0)
```

### 发现的 Bug

`orchestrator.stop_loop()` 在 `orchestrator.py` 中**不存在**。正确的停止方法是 app.py 自己的 `stop_loop()`（第 513 行），它设置 `_loop_stop` Event。

### Observer 启动 hook

**位置**：`app.py`，在第 1475 行之后、`app.run()` 之前。

```python
# 在 app.py:1472-1476 之间插入
ws_bridge.start_ws_server(host="127.0.0.1", port=5051)
# ↓ 新增
observer.start_observer()
# ↑ 新增
app.run(debug=False, host="127.0.0.1", port=5050)
```

与 WebSocket 服务器同级，因为这个服务也是进程级后台线程。

### Observer 停止 hook

**位置**：`app.py`，`_graceful_shutdown` 函数内，MCP 断开之后、`sys.exit(0)` 之前。

```python
def _graceful_shutdown(signum, frame):
    stop_loop()                        # ← 修复 bug
    ws_bridge.stop_ws_server()
    # MCP disconnect...
    observer.stop_observer()           # ← 新增
    sys.exit(0)
```

### 改动评估：**小**（2 行插入 + 1 个 bug 修复）

---

## 七、综合评估

### 各模块改动汇总

| 模块 | 改动规模 | 新代码估计 | 风险 |
|------|---------|-----------|------|
| 数据源 | **极小** | 0 行（直接 import 现有模块） | 无 |
| 执行器 | **小** | ~100 行（工具定义 + agent_cfg） | 低：实例化配置，不改现有类 |
| 守护线程 | **小** | ~50 行（复制 _loop_worker 模式） | 低：成熟模式 |
| 前端 | **小** | ~250 行（HTML + CSS + JS） | 低：不改现有组件 |
| 通信层 | **中** | ~150 行（消息分发 + handler） | 中：需要改 bridge.py 核心循环 |
| 启动/停止 | **小** | 5 行（hook 插入 + bug 修复） | 极低 |

### 总改动估计

- **新文件**：1 个（`observer.py`）
- **修改文件**：4-5 个（bridge.py, app.py, index.html, utils.js, + chat.js）
- **总新代码**：~550 行
- **零新依赖**：全部复用已有库和模块

### 不侵入执行路径（NF1 满足）

观察者智能体是纯旁路系统：
- 不修改 scheduler/dispatcher/executor 任何执行逻辑
- 挂掉只影响聊天面板，不影响任务执行
- 守护线程有 `try/except Exception` 兜底，单次异常不炸线程
- 读操作失败（如 tracker 文件损坏）只影响回答质量，不传播错误

### 零新依赖（NF4 满足）

| 能力 | 复用组件 |
|------|---------|
| WebSocket | `websockets` 库（bridge.py 已用） |
| LLM 推理 | `OpenAIAgentExecutor`（已有 tool-use 能力） |
| 前端 | 原生 JS（fetch + WebSocket + DOM） |
| 认证 | `AuthStore`（bridge.py 已用） |
| 数据查询 | witness/tracker/judge_monitor/_api_monitor（直接 import） |

### 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| bridge.py 双向改造引入并发 bug | 中 | 中 | 用 `run_coroutine_threadsafe` 已有模式，加锁保护 client 集合 |
| 观察者 LLM 调用消耗 token | 高 | 低 | 用最便宜模型（DeepSeek Chat），限制 max_turns=5 |
| 观察者线程泄漏 | 低 | 低 | daemon=True 自动终止，加上 `_observer_stop` Event |
| 前端 WebSocket 断线 | 高 | 低 | 参考 SSE 的重连机制（指数退避） |
| 与现有 SSE 事件推送冲突 | 极低 | 低 | 不同管道，bridge.py 已与 SSE 共存 |
| `_graceful_shutdown` 中的 `orchestrator.stop_loop()` bug | 高 | 低 | 本次顺修 |

---

## 八、推荐实施路线

### Phase 1：核心框架（先跑通聊天管道）

1. 创建 `observer.py`：守护线程 + 消息队列 + 异常检测 stub
2. 修改 `bridge.py`：接收客户端消息（`chat` method）→ 入队 → 返回响应
3. 创建 `chat.js`：WebSocket 连接 + 消息 UI
4. 修改 `index.html` + `utils.js`：加第 5 个标签页
5. 修改 `app.py`：启动/停止 hook

**验证**：前端能发"你好"，观察者线程收到消息并 echo 回前端。

### Phase 2：接入 LLM 大脑

6. 在 observer.py 中实例化 `OpenAIAgentExecutor`，注入只读查询工具
7. 实现 `_process_chat_queue()`：取出消息 → 调 executor.run() → 回写 WebSocket

**验证**：问"现在有几个任务在跑？"→观察者调 query_task 工具→返回数字。

### Phase 3：异常检测与主动推送

8. 实现 `_check_anomalies()`：停滞检测 + 裁判偏差 + 成本异常
9. `_push_event("observer_alert", ...)` 推送告警

**验证**：制造停滞任务 → 前端收到观察者告警。

### Phase 4：操作建议与渐进替换

10. 实现 F4 操作建议（释放/重试/回滚）
11. 仪表盘面板可选折叠，不强制移除（NF5）
