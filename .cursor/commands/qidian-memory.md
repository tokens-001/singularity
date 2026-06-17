---
name: qidian-memory
description: 查询 MAGMA 多图记忆 — 语义相似任务、因果链、实体重叠。
---

查询奇点 MAGMA 多图记忆：

```bash
cd /Users/jingzhe/奇点 && python3 -m scheduler memory query "$SELECTED_TEXT"
```

子命令:
- `python3 -m scheduler memory stats` — 记忆统计
- `python3 -m scheduler memory chain <task_id>` — 因果链溯源
- `python3 -m scheduler memory latent` — 慢通道候选
- `python3 -m scheduler memory traverse <query>` — Beam Search 遍历
