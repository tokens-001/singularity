---
name: qidian-loop
description: 启动奇点调度循环 (常驻后台)。Ctrl+C 停止。
---

启动奇点调度常驻循环：

```bash
cd /Users/jingzhe/奇点 && python3 -m scheduler loop --concurrent 2
```

调度器会自动取队列中的 PENDING 任务 → 路由 → 分发 → 执行 → 验证 → 合并。
