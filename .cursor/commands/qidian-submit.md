---
name: qidian-submit
description: 提交任务到Singularity Dispatch器。选中文本作为任务描述提交。
---

将选中内容提交到Singularity Agent 调度平台：

```bash
cd /Users/jingzhe/Singularity && python3 -m scheduler add "$SELECTED_TEXT"
```

提交后任务会自动进入调度队列 (PENDING → ROUTED → DISPATCHED → RUNNING → DONE)。
用 `qidian-status` 查看执行进度。
