// 奇点 Agent 调度平台 — 入口
// 功能模块已拆分到 js/ 目录：
//   utils.js — 状态/API/UI助手/刷新
//   dashboard.js — 仪表盘卡片/Agent行/Token/性能/状态机
//   tasks.js — 任务表格/详情/决策链/批操作
//   project.js — 项目/干预/提交
//   config.js — 配置/Skill/MCP/SSE/Loop/模板

// ═══════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════
refreshAll();
refreshJudgeMonitor();
refreshPatternProfile();
connectSSE();
checkSetup();
