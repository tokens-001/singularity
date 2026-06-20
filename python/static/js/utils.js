// 奇点 Agent 调度平台 — 前端逻辑

async function startProject(id){
  const r=await api('/api/projects/'+id+'/start',{method:'POST'});
  if(r.ok){ toast(r.message||'后台执行中','success'); loadProject(id); setTimeout(()=>{loadProject(id);loadProjects();},5000); }
  else alert(r.error||'失败');
}
async function runPhase(id){
  const r=await api('/api/projects/'+id+'/run-phase',{method:'POST'});
  if(r.ok){ toast(r.message||'后台执行中','success'); loadProject(id); setTimeout(()=>{loadProject(id);loadProjects();},5000); }
  else alert(r.error||'失败');
}
async function createProject(){
  const name=document.getElementById('new-project-name').value.trim();
  const desc=document.getElementById('new-project-desc').value.trim();
  if(!name) return alert('需要项目名称');
  if(!desc) return alert('需要需求描述（必填，复制上面给的提示词粘贴进来）');
  const r=await api('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    name,template:document.getElementById('new-project-template').value,
    description:desc,
    scope:document.getElementById('new-project-scope').value.trim(),
    constraints:(document.getElementById('new-project-constraints').value.trim()||'').split(';').filter(Boolean),
    budget:parseFloat(document.getElementById('new-project-budget').value)||3.0,
  })});
  if(r.ok){
    ['new-project-name','new-project-desc','new-project-scope','new-project-constraints'].forEach(x=>{const el=document.getElementById(x);if(el)el.value=''});
    loadProjects(); loadProject(r.project.id);
  } else alert(r.error||'创建失败');
}

// ═══════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════
let tasks=[], statusData={}, conflicts=[], expandedTask=null, activeTab='dashboard', flowClickFilter='', _loop_running=false;
let _lastTasksHash='', _lastFlowHash='';  // 避免无脑重绘

const STATUS_CN = {
  pending:'待处理',routed:'已路由',dispatched:'已分发',running:'运行中',
  validating:'验证中',done:'完成',failed:'失败',rolled_back:'已回滚',
  decomposed:'已分解',blocked:'阻塞',conflict_held:'冲突'
};

// ═══════════════════════════════════════════════════════
// Tab Switching
// ═══════════════════════════════════════════════════════
// 任务表格事件委托 (替代内联 onclick)
document.getElementById('task-table').addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if (!btn) {
    // 点击行 → 切换详情
    const row = e.target.closest('tr[data-action="toggle-detail"]');
    if (row) toggleDetail(row.dataset.taskId);
    return;
  }
  const taskId = btn.dataset.taskId;
  if (!taskId) return;
  e.stopPropagation();
  if (btn.dataset.action === 'cancel') cancelTask(taskId);
  else if (btn.dataset.action === 'delete') deleteTask(taskId);
});

// Esc 关闭展开的详情
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && expandedTask) {
    const detail = document.getElementById('detail-' + expandedTask);
    const row = document.getElementById('row-' + expandedTask);
    if (detail) detail.classList.remove('open');
    if (row) row.classList.remove('expanded');
    expandedTask = null;
  }
});

document.getElementById('tab-bar').addEventListener('click',e=>{
  if(!e.target.classList.contains('tab'))return;
  activeTab = e.target.dataset.tab;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  e.target.classList.add('active');
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('tab-'+activeTab).classList.add('active');
  if(activeTab==='tasks'){ renderTasks(); populateDecisionsPicker(); if(_cachedDcTaskId){ document.getElementById('dc-task-picker').value = _cachedDcTaskId; loadDecisions(); } }
  if(activeTab==='project') loadProjects();
  if(activeTab==='config'){ renderAPIStore(); renderModels(); renderLayerSwitch(); renderIntervention(); renderSkills(); renderPermissions(); renderMCPServers(); }
});

// ═══════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════
function toast(msg,type='error',suggestion=''){
  const c=document.getElementById('toast-container');
  const t=document.createElement('div');
  t.className='toast '+type;
  t.innerHTML = `<div style="font-weight:500">${esc(msg)}</div>${suggestion ? `<div style="font-size:9px;color:var(--text3);margin-top:2px">${esc(suggestion)}</div>` : ''}`;
  t.onclick=()=>t.remove();
  c.appendChild(t);
  setTimeout(()=>{if(t.parentNode)t.remove();},4000);
}
async function api(path,opts){
  try{const r=await fetch(path,opts);const d=await r.json();
    if(!r.ok&&d.error){toast(d.error);return{error:d.error};}
    return d;
  }catch(e){toast(e.message);return{error:e.message}};
}

// ═══════════════════════════════════════════════════════
// Interaction states (loading / empty / error)
// ═══════════════════════════════════════════════════════
function showSkeleton(el, rows=5){ el.innerHTML=Array.from({length:rows},()=>`<div class="skeleton w${80-Math.floor(Math.random()*50)}"></div>`).join(''); }
function showEmpty(el, msg, tabHint=''){ el.innerHTML=`<div class="empty-state">${msg}${tabHint?` <a onclick="switchTab('${tabHint}')">${tabHint}</a>`:''}</div>`; }
function showError(el, msg, retryFn){ el.innerHTML=`<div class="error-inline" onclick="(${retryFn.toString()})()">${msg}</div>`; }

// ═══════════════════════════════════════════════════════
// Refresh
// ═══════════════════════════════════════════════════════
async function refreshAll(){
  const ind=document.getElementById('live-indicator');
  ind.className='live'; ind.textContent='◆';

  const [s,t,c,r]=await Promise.all([
    api('/api/status'),api('/api/tasks'),api('/api/conflicts'),api('/api/reports/critical')
  ]);
  if(r&&r.length){renderReports(r);}
  statusData=s; conflicts=c.conflicts||[];
  const newTasks=t.tasks||[];

  // 数据没变就跳过 DOM 重绘
  const tasksHash=JSON.stringify(newTasks);
  const flowHash=JSON.stringify(s.counts||{});
  const tasksChanged=tasksHash!==_lastTasksHash;
  const flowChanged=flowHash!==_lastFlowHash;

  tasks=newTasks; _lastTasksHash=tasksHash;
  renderStatusCards(); renderAgentRow();

  if(tasksChanged){ renderTasks(); }
  // 卡片网格已包含 token/耗时/冲突/干预信息
  if(activeTab==='tasks'){ renderTasks(); if(flowChanged){ _lastFlowHash=flowHash; renderFlowDiagram(); } }

  ind.className='live on'; ind.textContent='●';
}

