// Singularity Agent 调度平台 — 入口
// 功能模块已拆分到 js/ 目录：
//   utils.js — 状态/API/UI助手/刷新
//   dashboard.js — 仪表盘卡片/Agent行/Token/性能/状态机
//   tasks.js — 任务表格/详情/决策链/批操作
//   project.js — 项目/干预/提交
//   config.js — 配置/Skill/MCP/SSE/Loop/模板

// ═══════════════════════════════════════════════════════
// 主题切换 (localStorage 持久化 + prefers-color-scheme 默认)
// ═══════════════════════════════════════════════════════
(function(){
  var KEY = 'qd-theme';
  var saved = localStorage.getItem(KEY);
  var sysDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
  } else if (!sysDark) {
    // ponytail: dark 是 CSS 默认值, 仅 light 需显式标记
    document.documentElement.setAttribute('data-theme', 'light');
  }

  // 系统主题变化时, 若用户未显式选择则跟随
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e){
    if (localStorage.getItem(KEY)) return;
    document.documentElement.setAttribute('data-theme', e.matches ? 'dark' : 'light');
  });

  window.toggleTheme = function(){
    var cur = document.documentElement.getAttribute('data-theme');
    var next = cur === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem(KEY, next);
  };
})();

// ═══════════════════════════════════════════════════════
// T19: Service Worker 注册 (仅 localhost / HTTPS)
// ═══════════════════════════════════════════════════════
if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
  navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
}

// ═══════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════
showSkeleton(document.getElementById('db-grid'), 3);
refreshAll();
refreshJudgeMonitor();
refreshPatternProfile();
connectSSE();
checkSetup();
