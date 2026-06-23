# 前端事件驱动改造方案

## 目标

Web 面板不再"卡住等结果"——Agent 执行过程中能看到它在干嘛。

## 不改的

- 现有 SSE 管道不动，4种事件（task/system/idle/memory）保留
- 不重写前端，只加新事件类型和对应渲染
- 后端调度逻辑不动，只在 executor 执行时多抛几个 `_push_event`

## 新增 SSE 事件

| 事件 | 触发时机 | 数据 | 前端表现 |
|------|---------|------|---------|
| `tool:start` | 模型开始调工具 | `{tool, path/command, task_id}` | 任务详情区追加 "🔧 正在读 app.py..." |
| `tool:done` | 工具执行完 | `{tool, result_preview, task_id}` | 更新为 "✅ 读完了 (1200字符)" |
| `turn` | 每轮推理结束 | `{turn, task_id}` | 任务卡片下方 "第 3/5 轮" 进度条 |
| `approval` | 需要用户确认 | `{action, detail, task_id}` | 弹确认对话框 |
| `subagent` | 子 Agent 生成/完成 | `{action, type, task_id}` | 仪表盘 "子 Agent 并行中..." |

## 涉及改动的文件

```
app.py              ← _push_event 加新事件 + 审批 API
_ exec.py           ← executor 里加 tool:start/tool:done/turn 事件发射
app.js              ← 新 SSE 事件监听 + 渲染
index.html          ← 审批对话框 HTML + 工具状态区
```

## 实施顺序

### Step 1: tool:start + tool:done
- executor 执行 read_file/write_file/run_command 前后抛事件
- 前端任务详情区追加工具调用记录
- 最小改动，效果最明显

### Step 2: turn 进度
- executor 每轮循环结束时抛 turn 事件
- 前端任务卡片显示轮次进度

### Step 3: approval 确认
- 危险操作（rm/curl/sudo）触发 approval 事件
- 前端弹确认框，用户点击后回调 API
- 衔接已有的 blocklist 逻辑

### Step 4: subagent 并行
- D 层委员会/WolfPack 模式下抛 subagent 事件
- 仪表盘展示子 Agent 并行状态

## 不做的

- thinking delta 字符流（太碎，SSE 扛不住。Web 不需要像终端那样打字机效果）
- 实时文件 diff 预览（Scream Code 的 diff 面板，Web 不适合）
- 审批超时自动拒绝（先做简单的，后续补）
