---
name: qidian-submit
description: 提交任务到奇点调度器。选中文本作为任务描述提交。
---

将选中内容提交到奇点 Agent 调度平台：

```bash
cd /Users/jingzhe/奇点 && python3 -m scheduler add "$SELECTED_TEXT"
```

提交后任务会自动进入调度队列 (PENDING → ROUTED → DISPATCHED → RUNNING → DONE)。
用 `qidian-status` 查看执行进度。
