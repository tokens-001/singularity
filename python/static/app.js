// 奇点 — 前端逻辑
// ═══════════════════════════════════════════

let _tasks = [], _projects = [], _loopRunning = false, _activeTab = 'dashboard';

// ── API ──
async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    const d = await r.json();
    if (!r.ok && d.error) toast(d.error, 'error');
    return d;
  } catch (e) { toast(e.message, 'error'); return { error: e.message }; }
}

// ── Toast ──
function toast(msg, type = 'info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  t.onclick = () => t.remove();
  c.appendChild(t);
  setTimeout(() => { if (t.parentNode) t.remove(); }, 4000);
}

// ── Tab ──
document.addEventListener('DOMContentLoaded', () => {
  const nav = document.querySelector('header nav');
  if (nav) nav.addEventListener('click', e => {
  if (!e.target.classList.contains('tab')) return;
  _activeTab = e.target.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  e.target.classList.add('active');
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + _activeTab).classList.add('active');
    if (_activeTab === 'dashboard') refreshDashboard();
    if (_activeTab === 'tasks') renderTasks();
    if (_activeTab === 'project') loadProjects();
    if (_activeTab === 'config') renderConfig();
  });
});

// ═══════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════
async function refreshDashboard() {
  const s = await api('/api/status');
  const counts = s.counts || {};
  const cards = [
    { cls: 'pending', label: '待处理', value: counts.pending || 0 },
    { cls: 'running', label: '运行中', value: counts.running || 0 },
    { cls: 'done', label: '完成', value: counts.done || 0 },
    { cls: 'failed', label: '失败', value: counts.failed || 0 },
  ];
  document.getElementById('status-cards').innerHTML = cards.map(c =>
    `<div class="card ${c.cls}"><div class="value">${c.value}</div><div class="label">${c.label}</div></div>`
  ).join('');

  // Budget
  const tu = await api('/api/token-usage');
  document.getElementById('budget-badge').textContent =
    `💰 $${(tu.daily_cost || 0).toFixed(2)}`;

  // Loop status
  const h = await api('/health');
  _loopRunning = h.loop_running;
  updateLoopUI();
}

function updateLoopUI() {
  const badge = document.getElementById('loop-badge');
  badge.textContent = _loopRunning ? '调度运行中' : '调度已停';
  badge.className = 'badge ' + (_loopRunning ? 'on' : 'off');
  document.getElementById('btn-loop-start').style.display = _loopRunning ? 'none' : '';
  document.getElementById('btn-loop-stop').style.display = _loopRunning ? '' : 'none';
}

async function toggleLoop() {
  if (_loopRunning) {
    await api('/api/loop/stop', { method: 'POST' });
  } else {
    await api('/api/loop/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"concurrent":1}' });
  }
  await refreshDashboard();
}

async function cleanup() {
  const r = await api('/api/cleanup', { method: 'POST' });
  if (r.ok) toast(`清理完成`);
  refreshDashboard();
}

// ═══════════════════════════════════════════
// Tasks
// ═══════════════════════════════════════════
async function renderTasks() {
  const data = await api('/api/tasks');
  _tasks = data.tasks || [];
  const statusFilter = document.getElementById('filter-status').value;
  const search = (document.getElementById('filter-search').value || '').toLowerCase();

  let filtered = _tasks;
  if (statusFilter) filtered = filtered.filter(t => t.status === statusFilter);
  if (search) filtered = filtered.filter(t => (t.description || '').toLowerCase().includes(search) || (t.id || '').includes(search));

  document.getElementById('task-count').textContent = `${filtered.length} 条`;

  const icons = { done: '✅', failed: '❌', running: '🔄', pending: '⏳', blocked: '🔒' };
  document.querySelector('#task-table tbody').innerHTML = filtered.map(t => `
    <tr>
      <td style="font-family:monospace;font-size:10px">${(t.id || '').slice(-12)}</td>
      <td>${esc((t.description || '').slice(0, 80))}</td>
      <td>${icons[t.status] || ''} ${t.status || '?'}</td>
      <td>${t.route_level || t.level || '?'}</td>
      <td>${fmtDur(t.elapsed || 0)}</td>
      <td>
        ${t.status === 'pending' || t.status === 'running' ? `<button class="btn sm danger" onclick="cancelTask('${t.id}')">取消</button>` : ''}
      </td>
    </tr>
  `).join('');
}

async function cancelTask(id) {
  await api('/api/tasks/' + id + '/cancel', { method: 'POST' });
  renderTasks();
}

// ═══════════════════════════════════════════
// Project
// ═══════════════════════════════════════════
async function loadProjects() {
  const r = await api('/api/projects');
  _projects = r.projects || [];
  const list = document.getElementById('project-list');
  if (!_projects.length) { list.innerHTML = '<span class="muted">暂无项目</span>'; return; }

  const labels = { template: '📋', researching: '🔍', gate1: '①', planning: '🏗', gate2: '②', executing: '⚡', gate3: '③', reviewing: '🔎', fixing: '🔧', gate4: '④', done: '✅' };
  list.innerHTML = _projects.map(p => `
    <div onclick="loadProject('${p.id}')" style="padding:8px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;justify-content:space-between">
      <span><b>${esc(p.name || '?')}</b> <span class="muted">${(p.id||'').slice(-8)}</span></span>
      <span>${labels[p.phase] || ''} ${p.phase || '?'}</span>
    </div>
  `).join('');
}

async function createProject() {
  const name = document.getElementById('proj-name').value.trim();
  const desc = document.getElementById('proj-desc').value.trim();
  if (!name || !desc) return toast('项目名称和需求描述为必填', 'error');
  const r = await api('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name,
      template: document.getElementById('proj-template').value,
      description: desc,
      scope: '',
      constraints: [],
      budget: parseFloat(document.getElementById('proj-budget').value) || 3.0,
    })
  });
  if (r.ok) {
    document.getElementById('proj-name').value = '';
    document.getElementById('proj-desc').value = '';
    loadProjects();
    loadProject(r.project.id);
  }
}

async function loadProject(id) {
  const p = await api('/api/projects/' + id);
  if (p.error) return;

  const phases = ['template', 'researching', 'gate1', 'planning', 'gate2', 'executing', 'gate3', 'reviewing', 'fixing', 'gate4', 'done'];
  const labels = ['📋模板', '🔍调研', '①', '🏗架构', '②', '⚡执行', '③', '🔎审查', '🔧修复', '④', '✅完成'];
  const idx = phases.indexOf(p.phase);

  const stepper = phases.map((ph, i) => {
    let cls = 'future';
    if (i < idx) cls = 'done';
    else if (i === idx) cls = 'current';
    return `<span class="phase-step ${cls}">${labels[i]}</span>`;
  }).join('');

  // Artifacts
  let arts = '';
  if (p.research_report) {
    const refs = (p.research_report.references || []).length;
    arts += `<div>📊 调研: ${refs} 条引用</div>`;
  }
  if (p.architecture) {
    const tasks = (p.architecture.tasks || []).length;
    arts += `<div>🏗 架构: ${tasks} 任务</div>`;
  }
  if (p.issues && p.issues.length) {
    arts += `<div>🐛 问题: ${p.issues.length} 个</div>`;
  }

  // Actions
  let actions = '';
  if (p.phase === 'template') {
    actions = `<button class="btn primary" onclick="startProject('${p.id}')">▶ 启动工作流</button>`;
  } else if (p.phase.startsWith('gate')) {
    actions = `<button class="btn success" onclick="gateConfirm('${p.id}','approved')">✓ 批准</button>
               <button class="btn danger" onclick="gateConfirm('${p.id}','rejected')">✗ 打回</button>
               <button class="btn primary" onclick="autoAdvance('${p.id}')">⚡ 自动判分</button>`;
  } else if (p.phase !== 'done') {
    actions = `<button class="btn primary" onclick="runPhase('${p.id}')">▶ 执行 ${labels[idx]}</button>`;
  }

  document.getElementById('project-detail').style.display = '';
  document.getElementById('project-detail-content').innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h3 style="margin:0">${esc(p.name)} <span class="muted">${p.phase}</span></h3>
      <span class="muted">💰 $${(p.token_spent||0).toFixed(2)} / $${(p.token_budget_total||5).toFixed(2)}</span>
    </div>
    <div class="phase-bar">${stepper}</div>
    <div style="margin:8px 0;color:var(--text2)">${esc((p.description||'').slice(0, 200))}</div>
    <div style="margin:8px 0;display:flex;gap:16px">${arts}</div>
    <div class="row">${actions}</div>
    <div style="margin-top:8px"><button class="btn sm" onclick="autoAdvance('${p.id}')">⚡ 自动推进</button></div>
  `;
}

async function startProject(id) {
  const r = await api('/api/projects/' + id + '/start', { method: 'POST' });
  if (r.ok) { toast('后台执行中...', 'info'); setTimeout(() => loadProject(id), 5000); }
}

async function runPhase(id) {
  const r = await api('/api/projects/' + id + '/run-phase', { method: 'POST' });
  if (r.ok) { toast('后台执行中...', 'info'); setTimeout(() => loadProject(id), 5000); }
}

async function gateConfirm(id, decision) {
  const r = await api('/api/projects/' + id + '/gate-confirm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision })
  });
  if (r.ok) { toast(`Gate ${decision === 'approved' ? '已批准' : '已打回'} → ${r.next_phase || r.phase}`, 'success'); loadProject(id); }
}

async function autoAdvance(id) {
  const r = await api('/api/projects/' + id + '/auto', { method: 'POST' });
  if (r.ok) { toast(`→ ${r.phase} (${r.action})`, 'info'); loadProject(id); }
  else toast(r.message || '失败', 'error');
}

// ═══════════════════════════════════════════
// Config
// ═══════════════════════════════════════════
async function renderConfig() {
  const data = await api('/api/agents');
  if (data.error) return;
  const order = data._order || {};
  const tiers = { D: 'D · 架构', 'E+': 'E+ · 复杂', E: 'E · 执行' };
  const costColors = { budget: '#16a34a', standard: '#2563eb', premium: '#dc2626' };

  document.getElementById('agent-tiers').innerHTML = Object.entries(tiers).map(([lvl, label]) => {
    const agents = data[lvl] || [];
    const tierOrder = order[lvl] || [];
    const sorted = [...agents].sort((a, b) => {
      const ai = tierOrder.indexOf(a.model), bi = tierOrder.indexOf(b.model);
      return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
    });
    return `<div class="tier-card">
      <h4>${label} <span class="muted">（越靠前越优先，拖拽排序暂不支持，手动编辑 agents_custom.json 的 _order）</span></h4>
      ${sorted.map((a, i) => `
        <div class="agent-row">
          <span class="pos">#${i + 1}</span>
          <span class="model">${esc(a.model)}</span>
          <span class="cost" style="color:${costColors[a.cost] || '#666'}">${a.cost || 'standard'}</span>
          ${a.default ? '<span class="badge on">默认</span>' : ''}
          <span class="muted">${(a.roles || []).join(', ')}</span>
          <span style="flex:1"></span>
          <span class="muted">${a.api_key_env || ''}</span>
        </div>
      `).join('')}
    </div>`;
  }).join('');

  // API Store
  const apis = await api('/api/api-store');
  const entries = Object.values(apis).filter(e => typeof e === 'object');
  document.getElementById('api-store').innerHTML = entries.length
    ? entries.map(e => `<div style="padding:4px 0;font-size:12px">🟢 ${esc(e.provider)} (${e.status || '?'}) — ${esc(e.base_url || '')}</div>`).join('')
    : '<span class="muted">无 API 配置</span>';
}

// ═══════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function fmtDur(s) { if (!s || s < 0) return '--'; if (s < 60) return Math.round(s) + 's'; if (s < 3600) return (s / 60).toFixed(1) + 'm'; return (s / 3600).toFixed(1) + 'h'; }

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  refreshDashboard();
  setInterval(() => { if (_activeTab === 'dashboard') refreshDashboard(); }, 5000);
});
