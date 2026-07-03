# Ruflo 借鉴清单

> 2026-07-03。基于 Ruflo v3.6.12 源码分析 + 奇点当前代码对比。全部落地。

---

## ✅ 已做

### 奇点已优于或等效 Ruflo
- [x] **Observer direct 模式** — 预取全量状态一次注入 prompt，比 Ruflo function calling 多轮更省 token
- [x] **Skill 缓存** — `_SKILL_CACHE` 按 (level, model) 缓存，等效 Ruflo pattern caching
- [x] **Router 分类缓存** — `_CLASSIFY_CACHE` 200 条 LRU + 1 小时过期，Ruflo 无此机制
- [x] **Observer prompt 去冗余** — 256 字符，function calling 的 tools 参数已含完整 schema
- [x] **模型路由简洁** — 两档 + Hedge 权重，vs Ruflo MoE 8 专家+Q-Learning
- [x] **直接调 API** — httpx 直调，vs Ruflo `spawn('claude', ...)`，不依赖 Claude Code

### 从 Ruflo 借鉴并落地
- [x] **执行中途 pause/resume** — `_check_paused()` 每 turn 检测信号文件，切 PAUSED 状态阻塞等恢复。PAUSED 期间可 cancel。API: `/api/tasks/<id>/pause|resume`
- [x] **Skill 语义检索（embedding）** — `_ntilc_filter` 优先用 cosine 相似度匹配，降级关键词。`_get_embed_model` 默认启用（`QIDIAN_SKIP_EMBED=1` 可关）
- [x] **Observer 状态增量推送** — `_build_status_context` 加 hash 缓存，状态无变化时复用上次上下文，省 ~30% token
- [x] **MCP server 模式** — `observer/mcp_server.py`，JSON-RPC 2.0 over stdio，暴露 11 个工具。Claude Code 可通过 `python3 -m singularity.observer.mcp_server` 注册

---

## ❌ 不该借鉴

| 项 | 理由 |
|----|------|
| MoE + Q-Learning 路由 | 5 个模型用两档+Hedge 够用 |
| 4 种蜂群拓扑 | 软件工程流程是线性的 |
| spawn claude 子进程 | 奇点直接调 API 更轻量 |
| Byzantine Fault Tolerance | 代码合并不需要容拜占庭故障 |
| 3 Queen 分层 | Observer+合成器+拆解器已覆盖 |
| SQLite + HNSW 存储 | JSON 文件可 grep、零依赖 |
| WASM Agent Booster | 奇点没有可跳过 LLM 的 transform 场景 |
