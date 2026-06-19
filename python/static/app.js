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
  if(activeTab==='config'){ renderAPIStore(); renderModels(); renderLayerSwitch(); renderIntervention(); }
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
  renderStatusCards(); renderAgentRow(); renderTokenStats(); loadPerfStats();

  if(tasksChanged){ renderTasks(); renderConflicts(); }
  renderIntervention(); // dashboard always shows held/failed
  if(activeTab==='tasks'){ renderTasks(); if(flowChanged){ _lastFlowHash=flowHash; renderFlowDiagram(); } }

  ind.className='live on'; ind.textContent='●';
}

// ═══════════════════════════════════════════════════════
// Status Cards
// ═══════════════════════════════════════════════════════
function renderStatusCards(){
  const counts=statusData.counts||{};
  const heldCount=tasks.filter(t=>t.held).length;
  const cards=[
    {k:'pending',label:'待处理',cls:'pending'},
    {k:'running',label:'运行中',cls:'running',n:statusData.running_total||0},
    {k:'done',label:'已完成',cls:'done'},
    {k:'failed',label:'失败',cls:'failed'},
    {k:'conflict_held',label:'冲突',cls:'conflict'},
    {k:'held',label:'扣留',cls:'held',n:heldCount},
  ];
  document.getElementById('status-cards').innerHTML=cards.map(c=>{
    const n=c.n!==undefined?c.n:(counts[c.k]||0);
    return `<div class="card ${c.cls}"><div class="count${c.k==='running'?' pulse':''}">${n}</div><div class="label">${c.label}</div></div>`;
  }).join('');

  // 空状态引导: 无任务 + 调度循环未启动
  const totalTasks = Object.values(counts).reduce((a,b)=>a+b,0);
  const onboarding = document.getElementById('onboarding');
  if (onboarding) {
    onboarding.style.display = (totalTasks === 0 && !_loop_running) ? 'block' : 'none';
  }

  // Populate filter dropdown
  const sel=document.getElementById('filter-status');
  if(sel.options.length<=1){
    [...new Set(tasks.map(t=>t.status))].sort().forEach(s=>{
      if(!sel.querySelector(`[value="${s}"]`)){
        const o=document.createElement('option');o.value=s;o.textContent=STATUS_CN[s]||s;sel.appendChild(o);
      }
    });
  }
}

// ═══════════════════════════════════════════════════════
// Agent Row
// ═══════════════════════════════════════════════════════
function renderAgentRow(){
  const agents=statusData.agents||{};
  const loads=statusData.heartbeat_levels||{};
  document.getElementById('agent-row').innerHTML=['E','D','E+'].map(l=>{
    const n=loads[l]||0;
    const models=(agents[l]||[]).map(a=>a.model).join(', ');
    const c=l==='E'?'e-dot':l==='D'?'d-dot':'ep-dot';
    return `<span><span class="agent-dot ${c}"></span>${l} ${models} <b>${n}</b></span>`;
  }).join('');
  document.getElementById('timing-line').textContent=
    `等待 ${statusData.avg_wait||'--'} · 完成 ${statusData.avg_done||'--'}`+
    (statusData.stalled&&statusData.stalled.length?` · ⚠ ${statusData.stalled.length} 卡住`:'');
}

async function renderTokenStats(){
  const tt=statusData.token_totals;
  const el=document.getElementById('token-section');
  const body=document.getElementById('token-body');
  if(!tt||!Object.keys(tt).length){el.style.display='none';return;}
  el.style.display='';
  const total=Object.values(tt).reduce((a,b)=>a+b,0);
  let html=`<b>累计</b> ${fmtTokens(total)} · `+
    Object.entries(tt).map(([l,t])=>`${l}: ${fmtTokens(t)}`).join(' · ');
  // 拉取今日用量 & 预算
  try{
    const u=await api('/api/token-usage');
    if(!u.error){
      html+=`<br><b>今日</b> ${fmtTokens(u.daily_tokens||0)} · <b>费用</b> $${(u.daily_cost||0).toFixed(4)}`;
      if(u.budget_daily>0) html+=` · <b>预算</b> $${u.daily_cost.toFixed(2)}/$${u.budget_daily.toFixed(2)}`;
      if(u.warning) html+=`<br><span style="color:var(--orange)">⚠ ${esc(u.warning)}</span>`;
    }
  }catch(_){}
  body.innerHTML=html;
}
function fmtTokens(n){if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return Math.round(n/1e3)+'K';return String(n);}

// 设置向导: 检查 API 配置状态
async function checkSetup(){
  try{
    const d=await api('/api/api-store');
    if(d.error)return;
    const entries=Object.values(d);
    const active=entries.filter(e=>e.status==='active'&&e.available);
    const onboarding=document.getElementById('onboarding');
    if(onboarding){
      if(active.length===0){
        onboarding.style.display='block';
        onboarding.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px;color:var(--orange)">⚡ 快速设置</div>
          <div style="font-size:10px;color:var(--text2);line-height:1.8">
          <div>检测到 <b style="color:var(--red)">0</b> 个可用 API。在 <b style="color:var(--cyan);cursor:pointer" onclick="document.querySelector('[data-tab=config]').click()">配置 → API 库</b> 添加或启用。</div>
          <div style="margin-top:4px;color:var(--text3)">API Key 需在 .env 文件中配置环境变量后重启服务。</div>
          </div>`;
      }else if(active.length<2){
        onboarding.style.display='block';
        onboarding.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px;color:var(--cyan)">${active.length} 个 API 就绪</div>
          <div style="font-size:10px;color:var(--text2);line-height:1.8">
          <div>已检测到: ${active.map(e=>esc(e.provider)).join(', ')}</div>
          <div>建议至少配置 2 个不同 provider 以实现容灾。</div>
          </div>`;
      }
    }
  }catch(_){}
}

// 性能面板
async function loadPerfStats(){
  try{
    const d=await api('/api/perf');
    if(d.error||!d.count)return;
    const el=document.getElementById('perf-section');
    if(el){
      el.style.display='';
      let html=`<b>${d.count}</b> 样本 · 平均 <b>${(d.avg_total_ms/1000).toFixed(1)}s</b>`;
      if(d.phase_avg_ms) html+=` · 执行:${(d.phase_avg_ms.execute_ms/1000).toFixed(1)}s`;
      if(d.slowest_5&&d.slowest_5.length){
        html+=`<br>最慢: `+d.slowest_5.map(s=>`<span style="font-family:mono;font-size:9px;color:var(--orange)">${s.task_id} ${s.total_s}s</span>`).join(' ');
      }
      document.getElementById('perf-body').innerHTML=html;
    }
  }catch(_){}
}

// ═══════════════════════════════════════════════════════
// Flow Diagram — SVG 状态机
// ═══════════════════════════════════════════════════════
function renderFlowDiagram(){
  const counts=statusData.counts||{};
  const nodes=[
    {id:'pending',label:'PENDING',x:20,y:55},
    {id:'routed',label:'ROUTED',x:170,y:55},
    {id:'dispatched',label:'DISPATCHED',x:320,y:55},
    {id:'running',label:'RUNNING',x:470,y:25},
    {id:'done',label:'DONE',x:640,y:25},
    {id:'failed',label:'FAILED',x:640,y:85},
    {id:'conflict_held',label:'CONFLICT',x:790,y:85},
    {id:'decomposed',label:'DECOMPOSED',x:790,y:25},
  ];
  const colors={
    pending:'#6b7280',routed:'#4a8cf7',dispatched:'#a371f7',
    running:'#39d2c0',done:'#4ec97a',failed:'#f44747',
    conflict_held:'#d2991d',decomposed:'#d2991d',
  };

  let svg=`<svg class="flow-svg" viewBox="0 0 950 120">`;
  // Arrows
  const arrows=[
    ['pending','routed'],['routed','dispatched'],['dispatched','running'],
    ['running','done'],['running','failed'],['running','decomposed'],['running','conflict_held'],
  ];
  arrows.forEach(([a,b])=>{
    const na=nodes.find(n=>n.id===a), nb=nodes.find(n=>n.id===b);
    const x1=na.x+72,x2=nb.x,y1=na.y+12,y2=nb.y+12;
    svg+=`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#252a35" stroke-width="1.5"/>`;
    // arrowhead
    const ang=Math.atan2(y2-y1,x2-x1);
    svg+=`<polygon points="${x2-4},${y2-2.5} ${x2},${y2} ${x2-4},${y2+2.5}"
     transform="rotate(${ang*180/Math.PI},${x2},${y2})" fill="#252a35"/>`;
  });

  // Nodes
  nodes.forEach(n=>{
    const c=colors[n.id]||'#6b7280';
    const cnt=counts[n.id]||0;
    const highlight=flowClickFilter===n.id?'stroke-width="2" stroke="'+c+'"':'';
    svg+=`<g class="flow-node" onclick="flowClick('${n.id}')" ${highlight}>
      <rect x="${n.x}" y="${n.y}" width="72" height="24" rx="4" fill="rgba(19,23,31,.9)" stroke="${cnt>0?c:'#252a35'}"/>
      <text x="${n.x+36}" y="${n.y+14}" fill="${cnt>0?c:'#6b7280'}" font-size="9">${n.label}</text>
      <text x="${n.x+36}" y="${n.y+44}" class="count" fill="${cnt>0?c:'#252a35'}">${cnt}</text>
    </g>`;
  });
  svg+=`</svg>`;
  document.getElementById('flow-body').innerHTML=svg;
}

function flowClick(nodeId){
  flowClickFilter=flowClickFilter===nodeId?'':nodeId;
  document.getElementById('filter-status').value=flowClickFilter;
  renderFlowDiagram();
  renderTasks();
}

// ═══════════════════════════════════════════════════════
// Task Table
// ═══════════════════════════════════════════════════════
function renderTasks(){
  const fs=document.getElementById('filter-status').value;
  const fl=document.getElementById('filter-level').value;
  const fh=document.getElementById('f-held').checked;

  const fq=(document.getElementById('filter-search')?.value||'').toLowerCase();
  let filtered=tasks;
  if(fs) filtered=filtered.filter(t=>t.status===fs);
  if(fl) filtered=filtered.filter(t=>t.route_level===fl);
  if(fh) filtered=filtered.filter(t=>t.held);
  if(fq) filtered=filtered.filter(t=>(t.description||'').toLowerCase().includes(fq)||(t.id||'').includes(fq));

  document.getElementById('task-count').textContent=`${filtered.length} 条`;

  const tbody=document.getElementById('task-table');
  tbody.innerHTML=filtered.map(t=>{
    const idShort=(t.id||'').slice(-8);
    const desc=(t.description||'').slice(0,70);
    const status=STATUS_CN[t.status]||t.status||'?';
    const wait=fmtDur(t.wait_sec||0);
    const lvl=t.route_level||'';
    const lvls=lvl==='E'?'level-e':lvl==='D'?'level-d':lvl==='E+'?'level-ep':'';
    const heldMark=t.held?' ◈':'';
    return `<tr data-action="toggle-detail" data-task-id="${t.id}" id="row-${t.id}">
      <td style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${idShort}${heldMark}</td>
      <td>${esc(desc)}</td>
      <td><span class="badge ${t.status}">${status}</span></td>
      <td>${lvl?`<span class="level-tag ${lvls}">${lvl}</span>`:'--'}</td>
      <td style="font-size:11px;color:var(--text2)">${t.priority||0}</td>
      <td>${['pending','routed'].includes(t.status)?`<button class="btn red sm" style="font-size:8px;padding:1px 4px" data-action="cancel" data-task-id="${t.id}">取消</button>`:""}${["pending","routed","failed","rolled_back","done","decomposed"].includes(t.status)?`<button class="btn orange sm" style="font-size:8px;padding:1px 4px;margin-left:2px" data-action="delete" data-task-id="${t.id}">删除</button>`:''}</td>
      <td style="font-size:10px;color:var(--text2)">${wait}</td>
    </tr><tr class="detail" id="detail-${t.id}"><td colspan="6"></td></tr>`;
  }).join('')||`<tr><td colspan="6" style="color:var(--text2);text-align:center;padding:24px">-- 暂无任务 --</td></tr>`;
}

async function toggleDetail(taskId){
  const detailRow=document.getElementById('detail-'+taskId);
  const row=document.getElementById('row-'+taskId);
  if(detailRow.classList.contains('open')){
    detailRow.classList.remove('open');row.classList.remove('expanded');expandedTask=null;return;
  }
  if(expandedTask){
    const prev=document.getElementById('detail-'+expandedTask);
    if(prev)prev.classList.remove('open');
    const pr=document.getElementById('row-'+expandedTask);
    if(pr)pr.classList.remove('expanded');
  }
  expandedTask=taskId;row.classList.add('expanded');

  const [data,tl]=await Promise.all([
    api('/api/tasks/'+taskId),api('/api/tasks/'+taskId+'/timeline')
  ]);
  if(data.error){detailRow.querySelector('td').innerHTML=`<div class="error-block">${esc(data.error)}</div>`;detailRow.classList.add('open');return;}

  const parents=(data._dag_parents||[]).map(p=>`  ↳ ${(p.id||'').slice(-8)} ${esc((p.description||'').slice(0,30))} [${STATUS_CN[p.status]||p.status}]`);
  const children=(data._dag_children||[]).map(c=>`  ↳ ${(c.id||'').slice(-8)} ${esc((c.description||'').slice(0,30))} [${STATUS_CN[c.status]||c.status}]`);
  const created=unix2str(data.created_at); const updated=unix2str(data.updated_at);

  // Mini timeline
  let tlHtml='';
  if(tl.timeline){
    tl.timeline.forEach(node=>{
      const cls=node.to==='done'?'done':node.to==='failed'||node.to==='rolled_back'?'failed':node.to==='running'?'active':'';
      tlHtml+=`<div class="tl-node ${cls}">
        <span class="tl-from">${node.from||'∅'}</span>→<span class="tl-to">${node.to}</span>
        ${node.meta&&node.meta.route_level?`<span class="tl-meta">level=${node.meta.route_level}</span>`:''}
        ${node.meta&&node.meta.error?`<span class="tl-meta" style="color:var(--red)">${esc(node.meta.error.slice(0,80))}</span>`:''}
      </div>`;
    });
  }

  // Action buttons
  let actions='';
  const st=data.status;
  if(st==='pending'||st==='routed'||st==='blocked'){
    actions+=data.held
      ?`<button class="btn cyan sm" onclick="event.stopPropagation();releaseTask('${taskId}')">释放</button>`
      :`<button class="btn purple sm" onclick="event.stopPropagation();holdTask('${taskId}')">扣留</button>`;
    actions+=`<button class="btn orange sm" onclick="event.stopPropagation();overrideRoute('${taskId}')">覆盖路由</button>`;
  }
  if(st==='running'||st==='dispatched'){
    actions+=`<button class="btn red sm" onclick="event.stopPropagation();cancelTask('${taskId}')">取消</button>`;
  }
  if(st==='failed'||st==='rolled_back'){
    actions+=`<button class="btn cyan sm" onclick="event.stopPropagation();retryTask('${taskId}')">重试</button>`;
  }
  if(data._has_trace){
    actions+=`<button class="btn sm" onclick="event.stopPropagation();viewTrace('${taskId}')">Trace</button>`;
  }
  actions+=`<button class="btn red sm" onclick="event.stopPropagation();rollbackTask('${taskId}')">回滚</button>`;

  detailRow.querySelector('td').innerHTML=`
    <div class="detail-grid">
      <div><dt>ID</dt><dd style="font-family:'SF Mono',monospace;font-size:11px">${data.id||taskId}</dd></div>
      <div><dt>状态</dt><dd><span class="badge ${st}">${STATUS_CN[st]||st}</span>
        ${data.held?'<span class="held-tag">扣留</span>':''}
        ${data.route_locked?'<span style="font-size:9px;color:var(--orange);margin-left:4px">🔒锁定</span>':''}
      </dd></div>
      <div><dt>描述</dt><dd>${esc(data.description||'')}</dd></div>
      <div><dt>路由级别 · 类型</dt><dd>${data.route_level||'--'} · ${data.route_type||'--'}</dd></div>
      <div><dt>优先级 · 深度</dt><dd>${data.priority||0} · ${data.depth||0}</dd></div>
      <div><dt>重试</dt><dd>${data.retry_count||0}/${data.max_retries||3}</dd></div>
      <div><dt>创建</dt><dd>${created}</dd></div>
      <div><dt>更新</dt><dd>${updated}</dd></div>
      <div><dt>等待 · 耗时</dt><dd>${fmtDur(data.wait_sec||0)} · ${data.duration_sec!=null?fmtDur(data.duration_sec):'--'}</dd></div>
      <div><dt>快照</dt><dd style="font-family:'SF Mono',monospace;font-size:10px">${data.snapshot_id||'--'}</dd></div>
    </div>
    ${tlHtml?`<div style="margin-top:8px"><dt>流转</dt><div class="timeline" style="margin-top:4px">${tlHtml}</div></div>`:''}
    ${parents.length?`<div style="margin-top:8px"><dt>依赖</dt><div class="dag-tree">${parents.join('\n')}</div></div>`:''}
    ${children.length?`<div style="margin-top:4px"><dt>子任务</dt><div class="dag-tree">${children.join('\n')}</div></div>`:''}
    ${data.error?`<div class="error-block">${esc(data.error)}</div>`:''}
    <div class="detail-actions">${actions}</div>`;
  detailRow.classList.add('open');
}

// ═══════════════════════════════════════════════════════
// Decision Chain — Tab 3
// ═══════════════════════════════════════════════════════
function populateDecisionsPicker(){
  const sel=document.getElementById('dc-task-picker');
  const done=tasks.filter(t=>t.status==='done'||t.status==='failed');
  sel.innerHTML='<option value="">-- 选择已完成任务 --</option>'+
    done.map(t=>`<option value="${t.id}">${(t.id||'').slice(-8)} ${esc((t.description||'').slice(0,50))}</option>`).join('');
}

let _cachedDcTaskId = '';
async function loadDecisions(){
  _cachedDcTaskId = document.getElementById('dc-task-picker').value;
  const taskId=document.getElementById('dc-task-picker').value;
  if(!taskId)return;
  const [route,pre,val]=await Promise.all([
    api(`/api/tasks/${taskId}/trace?section=route`),
    api(`/api/tasks/${taskId}/trace?section=pre_search`),
    api(`/api/tasks/${taskId}/trace?section=validation`),
  ]);

  let html='';

  // Card 1: Route
  const lvl=route.level||'?';
  const lvlC=lvl==='E'?'e-dot':lvl==='D'?'d-dot':'ep-dot';
  html+=`<div class="decision-card">
    <div class="dc-header route">路由决策 · level=<b>${lvl}</b> · type=${route.task_type||'?'} · gate=${route.gate_required?'ON':'off'}</div>
    <div class="dc-body">`;
  if(route.matched_signals&&route.matched_signals.length){
    route.matched_signals.forEach(s=>{
      let icon='▸';
      if(s.includes('complexity@1'))icon='◆';
      else if(s.includes('complexity@2'))icon='◇';
      else if(s.includes('complexity@3'))icon='○';
      else if(s.includes('降级'))icon='↓';
      else if(s.includes('gate'))icon='⚷';
      else if(s.includes('MAGMA'))icon='🧠';
      else if(s.includes('leading'))icon='⇱';
      html+=`<div class="signal-row"><span class="sig-icon">${icon}</span><span>${esc(s)}</span></div>`;
    });
  }else{html+=`<span style="color:var(--text2)">无匹配信号（默认 E）</span>`;}
  html+=`</div></div>`;

  // Card 2: Pre-Search
  if(pre&&!pre.error){
    html+=`<div class="decision-card">
      <div class="dc-header presearch">I 层预检 · ${pre.skipped?'已跳过':'已完成'}${pre.reason?` · ${esc(pre.reason)}`:''}</div>
      <div class="dc-body">`;
    if(pre.top_decisions&&pre.top_decisions.length){
      html+=`<div style="margin-bottom:6px;font-weight:600;font-size:10px">📚 知识库命中</div>`;
      pre.top_decisions.forEach(d=>{
        html+=`<div class="kb-hit">▸ <span style="color:var(--cyan)">score=${(d.score||0).toFixed(1)}</span> · ${esc((d.title||d.id||'').slice(0,60))}</div>`;
      });
    }
    if(pre.memory&&!pre.memory.error){
      const mem=pre.memory;
      html+=`<div style="margin-top:6px;font-weight:600;font-size:10px">🧠 MAGMA 记忆 · intent=${mem.intent||'?'}</div>`;
      if(mem.narrative&&mem.narrative.length){
        mem.narrative.slice(0,3).forEach(h=>{
          html+=`<div class="magma-hit">
            <span style="color:var(--purple)">${(h.score||0).toFixed(4)}</span>
            <span style="font-family:'SF Mono',monospace;font-size:10px">${(h.task_id||'').slice(-8)}</span>
            [${(h.graph_sources||[]).join(',')}]
            ${esc((h.description||'').slice(0,50))}
          </div>`;
        });
      }
      if(mem.entity_matches&&Object.keys(mem.entity_matches).length){
        html+=`<div style="margin-top:4px">📁 实体: ${Object.keys(mem.entity_matches).join(', ')}</div>`;
      }
      if(mem.graph_coverage&&Object.keys(mem.graph_coverage).length){
        html+=`<div style="color:var(--text2)">📊 图覆盖: ${Object.entries(mem.graph_coverage).map(([k,v])=>`${k}=${v}`).join(', ')}</div>`;
      }
    }
    html+=`</div></div>`;
  }

  // Card 3: Validation
  if(val&&!val.error){
    const v=val;
    html+=`<div class="decision-card">
      <div class="dc-header validation">验证结论 · ${v.verdict||'?'} · action=${v.action||'?'} · turns=${v.turns_used||0}</div>
      <div class="dc-body">
        <div>原始判定: <b>${v.validate_verdict||'?'}</b> · ${esc(v.validate_reason||'')}</div>
        <div>Gate: ${v.gate_passed===null?'未触发':v.gate_passed?'✅通过':'❌失败'}${v.gate_message?` · ${esc(v.gate_message)}`:''}</div>
        ${v.unverified&&v.unverified.length?`<div style="margin-top:4px">⚠ 未验证: ${v.unverified.map(u=>esc(u)).join(' · ')}</div>`:''}
        ${v.changed_files&&v.changed_files.length?`<div style="margin-top:4px">📄 文件: ${v.changed_files.join(', ')}</div>`:''}
        <div style="margin-top:4px;color:var(--text2)">Token: ${v.token_count||0} · 耗时: ${v.elapsed?v.elapsed.toFixed(1)+'s':'?'}</div>
        ${v.agent_output?`<details style="margin-top:6px"><summary>Agent 原始输出</summary><pre class="agent-out">${esc(v.agent_output)}</pre></details>`:''}
      </div></div>`;
  }

  document.getElementById('decision-cards-content').innerHTML=html||'<span style="color:var(--text2)">选择任务后加载决策链</span>';
}

// ═══════════════════════════════════════════════════════
// Intervention — Tab 4
// ═══════════════════════════════════════════════════════
function renderIntervention(){
  // Queue management: PENDING + ROUTED + BLOCKED
  const queue=tasks.filter(t=>['pending','routed','blocked'].includes(t.status));
  document.getElementById('queue-list').innerHTML=queue.length?queue.map(t=>`
    <div class="intervention-row">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" class="q-check" value="${t.id}" onchange="updateCheckedCount()">
        <span style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${(t.id||'').slice(-8)}</span>
        ${t.held?'<span class="held-tag">扣留</span>':''}
        ${t.route_locked?'<span style="font-size:9px;color:var(--orange)">🔒</span>':''}
        <span>${esc((t.description||'').slice(0,50))}</span>
        <span class="badge ${t.status}">${STATUS_CN[t.status]||t.status}</span>
      </label>
      <div style="display:flex;gap:4px">
        ${t.held
          ?`<button class="btn cyan sm" onclick="releaseTask('${t.id}')">释放</button>`
          :`<button class="btn purple sm" onclick="holdTask('${t.id}')">扣留</button>`}
        <button class="btn orange sm" onclick="overrideRoute('${t.id}')">覆盖</button>
      </div>
    </div>`).join(''):'<span style="color:var(--text2);font-size:11px">无排队任务</span>';

  // Active: RUNNING + DISPATCHED
  const active=tasks.filter(t=>['running','dispatched'].includes(t.status));
  const now = Date.now() / 1000;
  document.getElementById('active-list').innerHTML=active.length?active.map(t=>{
    const elapsed = t.created_at ? (now - t.created_at) : 0;
    const lvl = t.route_level || '?';
    return `<div class="intervention-row">
      <span style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span class="pulse-dot" style="width:5px;height:5px;background:var(--cyan);display:inline-block;border-radius:50%"></span>
        <span style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${(t.id||'').slice(-8)}</span>
        <span class="badge ${t.status}">${STATUS_CN[t.status]||t.status}</span>
        <span style="font-size:9px;color:var(--text3)">[${lvl}]</span>
        <span style="font-size:9px;color:var(--text2)">${fmtDur(elapsed)}</span>
        <span style="font-size:9px;color:var(--text2);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((t.description||'').slice(0,50))}</span>
      </span>
      <button class="btn red sm" onclick="cancelTask('${t.id}')">取消</button>
    </div>`;
  }).join(''):'<span style="color:var(--text2);font-size:11px">无活跃任务</span>';

  // Failed / retryable
  const failed=tasks.filter(t=>['failed','rolled_back'].includes(t.status));
  document.getElementById('failed-list').innerHTML=failed.length?failed.map(t=>`
    <div class="intervention-row">
      <span>
        <span style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${(t.id||'').slice(-8)}</span>
        <span class="badge ${t.status}">${STATUS_CN[t.status]||t.status}</span>
        ${esc((t.description||'').slice(0,50))}
        ${t.error?`<span style="font-size:10px;color:var(--red)">${esc(t.error.slice(0,60))}</span>`:''}
      </span>
      <button class="btn cyan sm" onclick="retryTask('${t.id}')">重试</button>
    </div>`).join(''):'<span style="color:var(--text2);font-size:11px">无可重试任务</span>';
}

// ═══════════════════════════════════════════════════════
// Batch ops
// ═══════════════════════════════════════════════════════
function getChecked(){return [...document.querySelectorAll('.q-check:checked')].map(c=>c.value);}
function updateCheckedCount(){
  const n = document.querySelectorAll('.q-check:checked').length;
  const el = document.getElementById('checked-count');
  if (el) el.textContent = n + ' 选中';
  document.querySelectorAll('.batch-btn').forEach(b => b.disabled = n === 0);
}
async function batchHold(){for(const id of getChecked())await api(`/api/tasks/${id}/hold`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{"reason":"批量扣留"}'});refreshAll();}
async function batchRelease(){for(const id of getChecked())await api(`/api/tasks/${id}/release`,{method:'POST'});refreshAll();}
async function batchOverride(){
  const lvl=document.getElementById('batch-level').value;if(!lvl)return;
  for(const id of getChecked())await api(`/api/tasks/${id}/override-route`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:lvl})});
  refreshAll();
}

// ═══════════════════════════════════════════════════════
// Single actions
// ═══════════════════════════════════════════════════════
async function holdTask(id,reason=''){if(!confirm(`扣留任务 ${id.slice(-8)}?`))return;await api(`/api/tasks/${id}/hold`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});refreshAll();}
async function releaseTask(id){await api(`/api/tasks/${id}/release`,{method:'POST'});refreshAll();}
async function overrideRoute(id){
  const lvl=prompt('覆盖路由级别 (E / D / E+):','D');if(!lvl)return;
  await api(`/api/tasks/${id}/override-route`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:lvl})});
  refreshAll();
}
async function cancelTask(id){if(!confirm(`取消任务 ${id.slice(-8)}?`))return;const r=await api(`/api/tasks/${id}/cancel`,{method:'POST'});if(r.ok){toast(r.message||'已取消','success');}else{toast(r.error||'取消失败','error');}refreshAll();}
async function retryTask(id){await api(`/api/tasks/${id}/retry`,{method:'POST'});refreshAll();}

// ═══════════════════════════════════════════════════════
// Submit / Trace / Rollback / Resolve (existing)
// ═══════════════════════════════════════════════════════
async function submitTask(){
  const desc=document.getElementById('new-task-desc').value.trim();if(!desc)return;
  const r=await api('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:desc})});
  if(r.ok){document.getElementById('new-task-desc').value='';refreshAll();}
  else alert('创建失败: '+(r.error||'?'));
}

async function viewTrace(taskId){
  const data=await api('/api/tasks/'+taskId+'/trace');
  const modal=document.getElementById('trace-modal'),content=document.getElementById('trace-content');
  if(data.error){content.innerHTML=`<h3>Trace 不可用</h3><p>${esc(data.error)}</p><button class="btn" onclick="closeTrace()">关闭</button>`;}
  else{
    content.innerHTML=`
      <h3 style="margin-bottom:8px">Trace · ${(taskId||'').slice(-8)}</h3>
      <div style="font-size:12px;margin-bottom:12px">
        <p><b>最终状态:</b> ${data.final_status||'?'}</p>
        <p><b>路由:</b> ${data.route?.level||'?'} · ${(data.route?.matched_signals||[]).join(', ')}</p>
        <p><b>验证:</b> ${data.validation?.verdict||'?'}</p>
        <p><b>文件:</b> ${(data.changed_files||[]).join(', ')||'无'}</p>
        <p><b>耗时:</b> ${data.elapsed?data.elapsed.toFixed(1)+'s':'?'} · <b>Token:</b> ${data.token_count||0}</p>
      </div>
      <details><summary>完整 JSON</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>
      <button class="btn" onclick="closeTrace()" style="margin-top:8px">关闭</button>`;
  }
  modal.classList.add('open');
}
function closeTrace(){document.getElementById('trace-modal').classList.remove('open');}

async function deleteTask(id){
  if(!confirm(`确认删除任务 ${id.slice(-8)}？此操作不可撤销。`))return;
  const r = await api(`/api/tasks/${id}/delete`, {method:'POST'});
  if(r.ok){ toast(r.message||'已删除', 'success'); } else { toast(r.error||'删除失败', 'error'); }
  refreshAll();
}
async function rollbackTask(taskId){
  if(!confirm(`回滚 ${taskId.slice(-8)}?`))return;
  const r=await api('/api/tasks/'+taskId+'/rollback',{method:'POST'});
  if(r.ok)refreshAll();else alert('回滚失败: '+(r.error||'?'));
}

function renderConflicts(){
  const sec=document.getElementById('conflict-section'),body=document.getElementById('conflict-body');
  if(!conflicts.length){sec.style.display='none';const e=document.getElementById('conflict-empty');if(e)e.style.display='';const c=document.getElementById('conflict-count');if(c)c.textContent='';return;}sec.style.display='';const e=document.getElementById('conflict-empty');if(e)e.style.display='none';const c=document.getElementById('conflict-count');if(c)c.textContent='('+conflicts.length+')';
  body.innerHTML=conflicts.map(c=>`
    <div class="intervention-row">
      <span>
        <span style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${(c.id||'').slice(-8)}</span>
        ${esc((c.description||'').slice(0,50))}
        ${c.error?`<span style="font-size:10px;color:var(--orange)">${esc(c.error.slice(0,80))}</span>`:''}
      </span>
      <div style="display:flex;gap:4px">
        <button class="btn cyan sm" onclick="resolveConflict('${c.id}','manual')">已解决</button>
        <button class="btn red sm" onclick="resolveConflict('${c.id}','abort')">放弃</button>
      </div>
    </div>`).join('');
}
async function resolveConflict(id,action){
  const r=await api('/api/conflicts/'+id+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  if(r.ok)refreshAll();else alert('操作失败: '+(r.error||'?'));
}

// ═══════════════════════════════════════════════════════
// Project
// ═══════════════════════════════════════════════════════
async function loadProjects(){
  const r=await api('/api/projects');
  const list = document.getElementById('project-list');
  const projects = r.projects || [];
  if (!projects.length) { list.innerHTML='<span style="color:var(--text3);font-family:var(--mono);font-size:9px">暂无项目</span>'; return; }
  const phases=['template','researching','gate1','planning','gate2','executing','gate3','reviewing','fixing','gate4','done'];
  const labels=['模板','调研','①','架构','②','执行','③','审查','修复','④','完成'];
  list.innerHTML = projects.map(p => {
    const idx = phases.indexOf(p.phase); const phaseLabel = idx>=0 ? labels[idx] : p.phase;
    return `<div onclick="loadProject('${esc(p.id)}')" style="padding:6px 0;border-bottom:1px solid var(--bg3);cursor:pointer;display:flex;justify-content:space-between;align-items:center">
      <span style="font-family:var(--mono);font-size:10px">${esc(p.name||'?')} <span style="color:var(--text3);font-size:8px">${esc((p.id||'').slice(-8))}</span></span>
      <span style="font-size:9px;color:var(--text3)">${p.template||'?'} · ${phaseLabel} · $${p.token_budget_total||0}</span>
    </div>`;
  }).join('');
}
async function loadProject(id){
  if (!id) return;
  const p = await api('/api/projects/'+id);
  if (p.error) { document.getElementById('project-detail-body').innerHTML=`<span style="color:var(--red)">${esc(p.error)}</span>`; return; }
  const phases=['template','researching','gate1','planning','gate2','executing','gate3','reviewing','fixing','gate4','done'];
  const labels=['📋模板','🔍调研','①门','🏗架构','②门','⚡执行','③门','🔎审查','🔧修复','④门','✅完成'];
  const idx = phases.indexOf(p.phase);
  const bar = phases.map((ph,i)=>{
    let cls='future'; if(i<idx) cls='done'; else if(i===idx) cls='current';
    return `<span class="phase-step ${cls}" title="${ph}">${labels[i]}</span>`;
  }).join('');

  // Artifacts display
  let artifacts = '';
  if (p.research_report) {
    const rr = p.research_report;
    const refs = rr.references||[];
    const reportId = 'report-'+id;
    let reportHtml = `<div class="artifact"><b>📊 调研报告</b>: ${refs.length}条引用`;
    if (rr.recommendation) reportHtml += `, ${esc(rr.recommendation).slice(0,100)}`;
    reportHtml += ` <button class="btn sm" style="font-size:8px;padding:1px 4px" onclick="document.getElementById('${reportId}').style.display=document.getElementById('${reportId}').style.display==='none'?'block':'none'">展开</button></div>`;
    reportHtml += `<div id="${reportId}" style="display:none;margin-top:4px;padding:6px;background:var(--bg2);font-size:9px;max-height:300px;overflow-y:auto;white-space:pre-wrap;font-family:var(--mono)">`;
    if (refs.length) {
      reportHtml += `<b>引用 (${refs.length}):</b>\n`;
      refs.forEach(r => {
        reportHtml += `  • ${esc(r.name||'?')} [${esc(r.applicability||'?')}]\n`;
        reportHtml += `    ${esc(r.core_idea||'').slice(0,120)}\n`;
      });
    }
    if (rr.comparison) reportHtml += `\n<b>对比:</b>\n${esc(rr.comparison).slice(0,500)}\n`;
    if (rr.pitfalls && rr.pitfalls.length) reportHtml += `\n<b>坑点:</b>\n${rr.pitfalls.map(p=>'• '+esc(p)).join('\n')}`;
    if (rr.parse_error) reportHtml += `\n⚠ 解析失败，原始输出:\n${esc((rr.raw_output||'').slice(0,2000))}`;
    reportHtml += `</div>`;
    artifacts += reportHtml;
  }
  }
  if (p.architecture) {
    const arch = p.architecture;
    const tasks = arch.tasks||[];
    const cons = arch.constraints||[];
    const archId = 'arch-'+id;
    let archHtml = `<div class="artifact"><b>🏗 架构方案</b>: ${tasks.length}任务, ${cons.length}约束`;
    if (arch.architecture) archHtml += ` — ${esc(arch.architecture).slice(0,100)}`;
    archHtml += ` <button class="btn sm" style="font-size:8px;padding:1px 4px" onclick="document.getElementById('${archId}').style.display=document.getElementById('${archId}').style.display==='none'?'block':'none'">展开</button></div>`;
    archHtml += `<div id="${archId}" style="display:none;margin-top:4px;padding:6px;background:var(--bg2);font-size:9px;max-height:300px;overflow-y:auto;font-family:var(--mono)">`;
    if (arch.architecture) archHtml += `<b>设计:</b>\n${esc(arch.architecture)}\n\n`;
    if (tasks.length) {
      archHtml += `<b>任务 (${tasks.length}):</b>\n`;
      tasks.forEach(t => {
        archHtml += `  • <b>${esc(t.id||'?')}</b> ${esc(t.title||'')} [${esc(t.complexity||'?')}]\n`;
        archHtml += `    验收: ${esc(t.acceptance||'').slice(0,100)}\n`;
      });
    }
    if (cons.length) {
      archHtml += `<b>约束 (${cons.length}):</b>\n`;
      cons.forEach(c => archHtml += `  • ${esc(c.text||c||'')}\n`);
    }
    if (arch.parse_error) archHtml += `\n⚠ 解析失败，原始输出:\n${esc((arch.raw_output||'').slice(0,1500))}`;
    archHtml += `</div>`;
    artifacts += archHtml;
  }
  if (p.issues && p.issues.length) {
    const bugs = p.issues.filter(i=>i.severity==='bug').length;
    artifacts += `<div class="artifact"><b>🐛 问题清单</b>: ${p.issues.length}个 (bug=${bugs})</div>`;
  }
  if (p.task_ids && p.task_ids.length) {
    artifacts += `<div class="artifact"><b>📋 关联任务</b>: ${p.task_ids.length}个</div>`;
  }
  if (p.handoffs && p.handoffs.length) {
    const last = p.handoffs.slice(-3);
    const hId = 'handoffs-'+id;
    let hHtml = `<div class="artifact"><b>🔄 交接记录</b>: ${p.handoffs.length}条`;
    hHtml += ` <button class="btn sm" style="font-size:8px;padding:1px 4px" onclick="document.getElementById('${hId}').style.display=document.getElementById('${hId}').style.display==='none'?'block':'none'">展开</button></div>`;
    hHtml += `<div id="${hId}" style="display:none;margin-top:4px;padding:6px;background:var(--bg2);font-size:9px;max-height:200px;overflow-y:auto">`;
    last.forEach(h => {
      const v = h.verdict==='pass'?'✅':'❌';
      hHtml += `<div style="margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--bg3)">
        <div>${v} <b>${esc(h.agent_model)}</b> · ${esc(h.phase)} · ${new Date(h.ts*1000).toLocaleString('zh-CN')}</div>
        <div style="color:var(--text2)">📝 ${esc((h.conclusion||'').slice(0,120))}</div>
        ${h.deliverable?`<div style="color:var(--text3)">📦 ${esc((h.deliverable||'').slice(0,100))}</div>`:''}
        ${h.next_agent?`<div style="color:var(--cyan)">→ ${esc(h.next_agent)}</div>`:''}
        ${h.human_confirm?'<div style="color:var(--orange)">⚠ 需人工确认</div>':''}
      </div>`;
    });
    hHtml += `</div>`;
    artifacts += hHtml;
  }

  // Budget
  const budgetInfo = `<span style="font-size:9px;color:var(--text3)">💰 $${(p.token_spent||0).toFixed(2)} / $${(p.token_budget_total||5).toFixed(2)}</span>`;

  // Phase-specific actions
  // Active agent indicator
  let activeAgent = '';
  if (p.handoffs && p.handoffs.length) {
    const last = p.handoffs[p.handoffs.length-1];
    activeAgent = `<span style="font-size:9px;color:var(--purple);font-family:var(--mono)">当前: ${esc(last.agent_model)} · ${esc(last.phase)}</span>`;
  }

  let actions = '';
  const isGate = p.phase.startsWith('gate');
  if (p.phase === 'template') {
    actions = `<button class="btn purple sm" onclick="startProject('${esc(p.id)}')">▶ 启动工作流</button>`;
  } else if (isGate) {
    actions = `<button class="btn green sm" onclick="gateConfirm('${esc(p.id)}','approved')">✓ 批准</button>
               <button class="btn red sm" onclick="gateConfirm('${esc(p.id)}','rejected')">✗ 打回</button>`;
  } else if (p.phase !== 'done') {
    actions = `<button class="btn cyan sm" onclick="advanceWithCost('${esc(p.id)}')">▶ 执行 ${labels[idx]}</button>`;
  }

  document.getElementById('project-detail-title').innerHTML = `${esc(p.name)} <span style="font-weight:normal;font-size:9px;color:var(--text3)">${p.phase}</span> ${budgetInfo} ${activeAgent}`;
  document.getElementById('project-detail-body').innerHTML = `
    <div class="phase-bar">${bar}</div>
    <div style="font-size:9px;color:var(--text2);margin:6px 0">${esc(p.description||'暂无描述').slice(0,200)}</div>
    ${p.scope?`<div style="font-size:9px;color:var(--text3);margin:4px 0">范围: ${esc(p.scope)}</div>`:''}
    ${artifacts?`<div style="margin:6px 0">${artifacts}</div>`:''}
    <div style="margin-top:6px;display:flex;gap:4px;flex-wrap:wrap;align-items:center">
      ${actions}
      <button class="btn sm" onclick="document.getElementById('project-detail').style.display='none'">关闭</button>
    </div>
  `;
  document.getElementById('project-detail').style.display='block';

async function gateConfirm(id, decision) {
  const sure = decision==='approved' ? '批准进入下一阶段?' : `打回? 将回到上一阶段`;
  if (!confirm(sure)) return;
  const r = await api('/api/projects/'+id+'/gate-confirm', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({decision})
  });
  if (r.ok) { loadProject(id); loadProjects(); }
  else alert(r.error||'操作失败');
}

async function advanceWithCost(id) {
  try {
    const costR = await api('/api/projects/'+id+'/cost');
    if (costR.cost > 0) {
      document.getElementById('cost-modal-body').innerHTML = `
        <div>阶段: <b>${costR.phase}</b></div>
        <div>Agent层: <b>${costR.level}</b></div>
        <div>估算费用: <b>$${costR.cost.toFixed(2)}</b></div>
        <div style="margin-top:4px;color:var(--text3)">累计: $${(costR.token_spent||0).toFixed(2)} / 预算: $${(costR.token_budget_total||5).toFixed(2)}</div>
      `;
      document.getElementById('cost-modal-confirm').onclick = async () => {
        closeCostModal();
        const r = await api('/api/projects/'+id+'/run-phase', {method:'POST'});
        if (r.ok) { toast(r.message||'后台执行中','success'); loadProject(id); setTimeout(()=>{loadProject(id);loadProjects();},5000); }
        else toast(r.error||'执行失败','error');
      };
      document.getElementById('cost-modal').style.display = 'flex';
      return;
    }
  } catch(e) {}
  const r = await api('/api/projects/'+id+'/run-phase', {method:'POST'});
  if (r.ok) { toast(r.message||'后台执行中','success'); loadProject(id); setTimeout(()=>{loadProject(id);loadProjects();},5000); }
  else toast(r.error||'执行失败','error');
}
function closeCostModal(){ document.getElementById('cost-modal').style.display='none'; }
// (orphaned code block removed)


// ═══════════════════════════════════════════════════════
// Loop Control
// ═══════════════════════════════════════════════════════
async function startLoop(){
  const r=await api('/api/loop/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"concurrent":1}'});
  if(r.ok || r.running){_loop_running=true;updateLoopUI(true);refreshAll();pollLoopEvents();}
}
async function stopLoop(){
  const r=await api('/api/loop/stop',{method:'POST'});
  if(r.ok || !r.running){_loop_running=false;updateLoopUI(false);refreshAll();}
}
async function cleanupStale(){
  if(!confirm('清理残留心跳文件和任务缓存？'))return;
  const r=await api('/api/cleanup',{method:'POST'});
  if(r.ok){toast(`清理完成: ${r.cleaned?.heartbeats||0} 心跳, ${r.cleaned?.tasks||0} 任务`,'success');refreshAll();}
  else toast(r.error||'清理失败','error');
}
function updateLoopUI(running){
  document.getElementById('loop-dot').className='loop-indicator '+(running?'on':'off');
  document.getElementById('loop-label').className='loop-status '+(running?'on':'off');
  document.getElementById('loop-label').textContent=running?'调度循环 · 运行中':'调度循环 · 已停止';
  document.getElementById('btn-loop-start').style.display=running?'none':'';
  document.getElementById('btn-loop-stop').style.display=running?'':'none';
}
async function pollLoopEvents(){
  try{
    const r=await api('/api/loop/status');
    if(r.error) return;
    _loop_running = !!r.running;
    if(r.running) updateLoopUI(true); else updateLoopUI(false);
    const info = document.getElementById('loop-info');
    if(info) info.textContent = r.running ? `concurrent=${r.concurrent}` : '';
    if(r.events && r.events.length){
      const section = document.getElementById('event-section');
      const feed = document.getElementById('event-feed');
      if(section) section.style.display = '';
      if(feed) {
        feed.innerHTML = r.events.map(e => {
          const cls = 'ev-' + (e.kind || 'idle');
          const ts = new Date(e.ts * 1000).toLocaleTimeString('zh-CN');
          return '<div class="event-row"><span class="ev-ts">' + ts + '</span><span class="' + cls + '">' + esc(e.msg) + '</span></div>';
        }).join('');
      }
    }
  }catch(e){ console.error('pollLoopEvents:', e); }
}
// Check loop status on init
pollLoopEvents();
setInterval(pollLoopEvents,2000);

// ═══════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════
function renderReports(reports){
  const sec=document.getElementById('reports-section');
  const body=document.getElementById('reports-body');
  if(!reports.length){sec.style.display='none';return;}
  sec.style.display='block';
  const colors={critical:'var(--red)',alert:'var(--orange)'};
  body.innerHTML=reports.map(r=>`<div style="padding:6px 0;border-bottom:1px solid var(--border)">
    <span style="color:${colors[r.severity]||'var(--text2)'};font-weight:600">[${r.severity}]</span>
    <b>${esc(r.title)}</b>
    <div style="color:var(--text2);margin-top:2px">${esc(r.what||'')}</div>
    ${r.suggestion?`<div style="color:var(--cyan);font-size:9px;margin-top:2px">→ ${esc(r.suggestion)}</div>`:''}
  </div>`).join('');
}
function esc(s){if(!s)return'';const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function fmtDur(s){if(s<60)return Math.round(s)+'s';if(s<3600)return(s/60).toFixed(1)+'m';return(s/3600).toFixed(1)+'h';}
function unix2str(ts){if(!ts)return'--';return new Date(ts*1000).toLocaleString('zh-CN');}

// ═══════════════════════════════════════════════════════
// 基础设施 Tab — API库 / 模型库 / Agent编组
// ═══════════════════════════════════════════════════════

async function renderAPIStore(){
  const body = document.getElementById('api-store-body');
  try {
    const d = await api('/api/api-store');
    if (d.error) { body.innerHTML = `<span style="color:var(--red)">${esc(d.error)}</span>`; return; }
    const entries = Object.values(d);
    if (!entries.length) { body.innerHTML = '<span style="color:var(--text3);font-family:var(--mono);font-size:9px">NO_API_CONFIGURED</span>'; return; }
    const statusDot = {active:'<span style="color:var(--green);font-size:7px">●</span>',quota_exhausted:'<span style="color:var(--orange);font-size:7px">◑</span>',rate_limited:'<span style="color:var(--orange);font-size:7px">◐</span>',disabled:'<span style="color:var(--red);font-size:7px">○</span>'};
    const statusLabel = {active:'ON',quota_exhausted:'QTA',rate_limited:'RTL',disabled:'OFF'};
    body.innerHTML = entries.map(e => {
      const dot = statusDot[e.status]||statusDot.disabled;
      const lbl = statusLabel[e.status]||'OFF';
      const models = (e._models||[]).slice(0,3).join(' ');
      return `<div style="padding:4px 0;border-bottom:1px solid var(--bg3);display:flex;align-items:center;gap:8px;font-size:10px">
        <span style="width:12px;text-align:center">${dot}</span>
        <span style="font-family:var(--mono);font-weight:500;min-width:66px;color:var(--text)">${esc(e.provider)}</span>
        <span style="font-family:var(--mono);font-size:9px;color:var(--text3);min-width:28px">[${lbl}]</span>
        <span style="font-size:9px;color:var(--text3);flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(e.base_url)}</span>
        <select onchange="setAPIStatus('${esc(e.id)}',this.value)" style="background:var(--bg);border:none;border-bottom:1px solid var(--grid);color:var(--text2);font-size:9px;padding:2px 4px;font-family:var(--mono);width:auto">
          <option value="">—</option>
          <option value="active">ON</option>
          <option value="quota_exhausted">QTA</option>
          <option value="rate_limited">RTL</option>
          <option value="disabled">OFF</option>
        </select>
        <button class="btn sm" style="color:var(--red);font-size:8px;padding:1px 4px" onclick="removeAPI('${esc(e.id)}')">DEL</button>
      </div>`;
    }).join('');
  } catch(e) { body.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

async function addAPI(){
  const id = document.getElementById('api-new-id').value.trim();
  const provider = document.getElementById('api-new-provider').value.trim();
  const url = document.getElementById('api-new-url').value.trim();
  const env = document.getElementById('api-new-env').value.trim();
  if (!id) return alert('需要 id');
  const r = await api('/api/api-store', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,provider,base_url:url,api_key_env:env})});
  if (r.error) alert(r.error); else {
    document.getElementById('api-new-id').value='';
    document.getElementById('api-new-provider').value='';
    document.getElementById('api-new-url').value='';
    document.getElementById('api-new-env').value='';
    renderAPIStore();
  }
}

async function setAPIStatus(id, status){
  if (!status) return;
  const r = await api(`/api/api-store/${id}/status`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  if (!r.error) renderAPIStore();
}

async function removeAPI(id){
  if (!confirm(`删除 API "${id}"？`)) return;
  const r = await api(`/api/api-store/${id}`, {method:'DELETE'});
  if (!r.error) renderAPIStore();
}

async function addModel(){
  const sel = document.getElementById('model-new-provider');
  const provider = sel.options[sel.selectedIndex]?.dataset?.id || sel.value;
  const id=document.getElementById('model-new-id').value.trim();
  const display=document.getElementById('model-new-display').value.trim();
  const tier=document.getElementById('model-new-tier').value;
  const speed=document.getElementById('model-new-speed').value;
  const cost=document.getElementById('model-new-cost').value;
  const reasoning=document.getElementById('model-new-reasoning').checked;
  if(!id||!provider) return alert('需要 id 和 provider');
  const r=await api('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,provider,display,tiers:[tier],speed,cost,reasoning,max_turns:5})});
  if(r.error) alert(r.error); else {
    ['model-new-id','model-new-display'].forEach(x=>document.getElementById(x).value='');
    renderModels();
  }
}

// provider 下拉切换时自动建议
function onProviderChange(){
  const sel = document.getElementById('model-new-provider');
  const provider = sel.options[sel.selectedIndex]?.dataset?.id || sel.value;
  if (!provider) return;
  // 已知 provider → 模型名映射，自动填 id
  const hints = {
    deepseek: { id: 'deepseek-chat', display: 'DeepSeek V3', tier: 'E', speed: 'fast', cost: 'budget' },
    zhipu: { id: 'glm-5-turbo', display: 'GLM-5 Turbo', tier: 'E', speed: 'slow', cost: 'budget' },
    kimi: { id: 'kimi-k2.7-code', display: 'Kimi K2.7', tier: 'E', speed: 'medium', cost: 'standard' },
    anthropic: { id: 'claude-opus-4-8', display: 'Claude Opus 4.8', tier: 'D', speed: 'slow', cost: 'premium' },
    openai: { id: 'gpt-5.5', display: 'GPT-5.5', tier: 'D', speed: 'fast', cost: 'premium' },
  };
  const hint = hints[provider];
  if (hint) {
    document.getElementById('model-new-id').value = hint.id;
    document.getElementById('model-new-display').value = hint.display;
    document.getElementById('model-new-tier').value = hint.tier;
    document.getElementById('model-new-speed').value = hint.speed;
    document.getElementById('model-new-cost').value = hint.cost;
  }
}

// 从 API 库填充 provider 下拉
async function populateProviderDropdown(){
  const sel = document.getElementById('model-new-provider');
  const current = sel.value;
  try {
    const d = await api('/api/api-store');
    if (d.error) return;
    sel.innerHTML = '<option value="">-- 选 API --</option>';
    for (const [key, e] of Object.entries(d)) {
      const dot = e.available ? '●' : '○';
      sel.innerHTML += `<option value="${esc(key)}" data-id="${esc(key)}">${dot} ${esc(e.provider)} [${esc(key)}]</option>`;
    }
    if (current) sel.value = current;
  } catch(_) {}
}

// renderModels 时同步更新 provider 下拉
const _origRenderModels = renderModels;
renderModels = async function() {
  await populateProviderDropdown();
  return _origRenderModels();
};
async function setDefaultModel(id, tier){
  // 先获取当前 agents 配置
  const ag = await api('/api/agents');
  if (!ag[tier]) return;
  // 把该层所有 agent 的 default 置 false，目标置 true
  for (const a of ag[tier]) {
    const body = {default: a.model === id};
    await api('/api/agents/'+tier+'/'+a.model, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }
  renderModels();
}
async function updateModel(id, field, value){
  const body = {};
  if (field === 'tiers') {
    // toggle tier in/out
    const all = await api('/api/models');
    const model = all[id];
    if (!model) return;
    const tiers = [...(model.tiers || [])];
    const idx = tiers.indexOf(value);
    if (idx >= 0) tiers.splice(idx, 1);
    else tiers.push(value);
    if (!tiers.length) return;
    body.tiers = tiers;
  } else {
    body[field] = value;
  }
  const r = await api('/api/models/'+id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (r.error) { /* ignore */ } else renderModels();
}
async function editModel(id){
  event.stopPropagation();
  const old = document.getElementById('model-edit-'+id);
  if (old) { old.remove(); renderModels(); return; }
  const models = await api('/api/models');
  const m = Object.values(models).find(x => x.id === id);
  if (!m) return;
  const tiers = m.tiers||[];
  const html = '<div id="model-edit-'+esc(m.id)+'" style="margin:4px 0;padding:8px;border:1px solid var(--cyan);background:var(--bg2)">层级：'+['E','E+','D'].map(t=>{
    const checked = tiers.includes(t);
    return '<label style="display:inline-block;margin-right:12px;cursor:pointer;font-size:11px"><input type="checkbox" id="em-'+esc(m.id)+'-'+t+'" '+(checked?'checked':'')+'> '+t+'</label>';
  }).join('')+' <button class="btn sm" onclick="saveModelEdit(\''+esc(m.id)+'\')" style="margin-left:8px;margin-right:4px">保存</button><button class="btn sm" style="color:var(--text3)" onclick="editModel(\''+esc(m.id)+'\')">取消</button></div>';
  document.getElementById('models-body').insertAdjacentHTML('afterbegin', html);
}
async function saveModelEdit(id){
  const tiers = ['E','E+','D'].filter(t => document.getElementById('em-'+id+'-'+t)?.checked);
  if (!tiers.length) { alert('至少选一个层级'); return; }
  const r = await api('/api/models/'+id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({tiers})});
  if (r.error) alert(r.error); else renderModels();
}
async function removeModel(id){
  if(!confirm('删除模型 '+id+' ？')) return;
  const r=await api('/api/models/'+id,{method:'DELETE'});
  if(r.error) alert(r.error); else renderModels();
}

async function renderModels(){
  const body = document.getElementById('models-body');
  try {
    const d = await api('/api/models');
    if (d.error) { body.innerHTML = '<span style="color:var(--red)">'+esc(d.error)+'</span>'; return; }
    const models = Object.values(d);
    if (!models.length) { body.innerHTML = '<span style="color:var(--text3);font-family:var(--mono);font-size:9px">NO_MODELS_REGISTERED</span>'; return; }
    let agentData = {};
    try { agentData = await api('/api/agents'); } catch(e) {}
    const defaults = {};
    for (const [lvl, agents] of Object.entries(agentData)) {
      if (!Array.isArray(agents)) continue;
      for (const a of agents) {
        if (a.default) { defaults[a.model] = defaults[a.model] || {}; defaults[a.model][lvl] = true; }
      }
    }
    const costMark = {budget:'_',standard:'=',premium:'≡'};
    // 按 provider 分组
    const providerNames = {deepseek:'DeepSeek',zhipu:'智谱 GLM',kimi:'Moonshot Kimi',openai:'OpenAI',anthropic:'Anthropic'};
    const groups = {};
    for (const m of models) {
      m._isDefault = defaults[m.id] || {};
      const p = m.provider || 'other';
      if (!groups[p]) groups[p] = [];
      groups[p].push(m);
    }
    function renderCard(m){
      const tiers = (m.tiers||[]).map(t => {
        const tc = {E:'var(--cyan)','E+':'var(--orange)',D:'var(--purple)'}[t]||'var(--text3)';
        return '<span style="color:'+tc+';font-family:var(--mono);font-size:8px">'+t+'</span>';
      }).join('/');
      const dot = m.api_available
        ? '<span style="color:var(--green);font-size:7px">●</span>'
        : '<span style="color:var(--text3);font-size:7px">○</span>';
      const cm = costMark[m.cost]||'_';
      let html = '<div style="padding:4px 0 4px 12px;border-bottom:1px solid var(--bg3)">';
      html += '<div style="display:flex;align-items:center;gap:8px;font-size:10px">';
      html += '<span style="width:12px;text-align:center">'+dot+'</span>';
      html += '<span style="font-family:var(--mono);font-weight:500;min-width:80px;color:var(--text)">'+esc(m.display)+'</span>';
      html += '<span style="font-family:var(--mono);font-size:9px;color:var(--text3)">'+esc(m.id)+'</span>';
      html += '<span style="font-size:9px;color:var(--text3);font-family:var(--mono)">'+cm+'</span>';
      html += '<span style="font-size:8px;color:var(--text3)">'+m.speed+'</span>';
      if (m.reasoning) html += '<span style="font-size:8px;color:var(--purple);font-family:var(--mono)">RSN</span>';
      html += '<span style="flex:1"></span>';
      html += '<span style="font-size:8px;font-family:var(--mono)">'+tiers+'</span>';
      html += ' <button class="btn sm" style="color:var(--cyan);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();editModel(\''+esc(m.id)+'\')">✎</button>';
      html += ' <button class="btn sm" style="color:var(--red);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();removeModel(\''+esc(m.id)+'\')">DEL</button>';
      html += '</div>';
      html += '<div style="font-size:8px;color:var(--text3);margin-top:2px;font-family:var(--mono)">'+esc(m.notes||'')+'</div>';
      html += '<div style="margin-top:2px;display:flex;gap:2px">';
      for (const t of ['E','E+','D']) {
        const on = (m.tiers||[]).includes(t);
        const tc = {E:'var(--cyan)','E+':'var(--orange)',D:'var(--purple)'}[t]||'var(--text3)';
        html += '<span onclick="event.stopPropagation();updateModel(\''+esc(m.id)+'\',\'tiers\',\''+t+'\')" style="cursor:pointer;font-size:7px;font-family:var(--mono);padding:1px 4px;color:'+(on?tc:'var(--text3)')+';background:'+(on?'var(--bg3)':'transparent')+'">['+t+']</span>';
      }
      html += '<span style="font-size:7px;color:var(--text3);font-family:var(--mono)"> 层级</span>';
      for (const t of ['E','E+','D']) {
        if (!(m.tiers||[]).includes(t)) continue;
        const isDef = m._isDefault && m._isDefault[t];
        html += '<span onclick="event.stopPropagation();setDefaultModel(\''+esc(m.id)+'\',\''+t+'\')" style="cursor:pointer;font-size:7px;font-family:var(--mono);padding:1px 4px;color:'+(isDef?'var(--cyan)':'var(--text3)')+';background:'+(isDef?'rgba(57,210,192,.12)':'transparent')+'">'+(isDef?'DEF':'def')+'</span>';
      }
      html += '</div></div>';
      return html;
    }
    let html = '';
    const order = ['deepseek','zhipu','kimi','openai','anthropic','other'];
    for (const p of order) {
      if (!groups[p] || !groups[p].length) continue;
      html += '<div style="margin-bottom:8px">';
      html += '<div style="color:var(--cyan);font-size:10px;font-weight:500;padding:4px 0;border-bottom:1px solid var(--cyan)">'+esc(providerNames[p]||p)+' <span style="color:var(--text3);font-size:8px">'+groups[p].length+'个</span></div>';
      html += groups[p].map(renderCard).join('');
      html += '</div>';
    }
    body.innerHTML = html;
  } catch(e) { body.innerHTML = '<span style="color:var(--red)">'+esc(e.message)+'</span>'; }
}


async function loadLineup(){
  const sel = document.getElementById('lineup-project-select');
  const pid = sel.value;
  const editor = document.getElementById('lineup-editor');
  const status = document.getElementById('lineup-status');
  if (!pid) { editor.style.display='none'; status.textContent=''; return; }

  // 加载项目 lineup
  const r = await api(`/api/projects/${pid}/lineup`);
  const lineup = (r.lineup) || {};
  // 加载可用模型列表
  const [eMods, epMods, dMods] = await Promise.all([
    api('/api/models/tier/E'), api('/api/models/tier/E+'), api('/api/models/tier/D')
  ]);

  function renderCheckboxes(tier, models, containerId){
    const c = document.getElementById(containerId);
    const selected = lineup[tier] || [];
    c.innerHTML = models.map(m => {
      const checked = selected.includes(m.id) ? 'checked' : '';
      const dot = m.api_available
        ? '<span style="color:var(--green);font-size:7px">●</span>'
        : '<span style="color:var(--text3);font-size:7px">○</span>';
      return `<label style="display:block;padding:2px 0;cursor:pointer;font-family:var(--mono);font-size:9px">
        <input type="checkbox" value="${esc(m.id)}" ${checked} onchange="lineupChanged()"
          ${m.api_available?'':'disabled'}>
        ${dot} ${esc(m.display)} <span style="color:var(--text3)">${m.cost}</span>
      </label>`;
    }).join('');
  }

  renderCheckboxes('E', eMods, 'lineup-e');
  renderCheckboxes('E+', epMods, 'lineup-ep');
  renderCheckboxes('D', dMods, 'lineup-d');
  editor.style.display = 'block';
  status.textContent = lineup && Object.keys(lineup).length ? '已有自定义编组' : '使用全局默认';
}

function lineupChanged(){
  document.getElementById('lineup-status').textContent = '已修改，待保存';
  document.getElementById('lineup-status').style.color = 'var(--orange)';
}

async function saveLineup(){
  const pid = document.getElementById('lineup-project-select').value;
  if (!pid) return;
  function getChecked(containerId){
    const c = document.getElementById(containerId);
    return [...c.querySelectorAll('input:checked')].map(cb => cb.value);
  }
  const lineup = {E: getChecked('lineup-e'), 'E+': getChecked('lineup-ep'), D: getChecked('lineup-d')};
  const r = await api(`/api/projects/${pid}/lineup`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({lineup})});
  if (r.error) { alert(r.error); return; }
  document.getElementById('lineup-status').textContent = '已保存 ✓';
  document.getElementById('lineup-status').style.color = 'var(--green)';
}

async function renderLayerSwitch(){
  const body = document.getElementById('layer-switch-body');
  const [agents, models] = await Promise.all([api('/api/agents'), api('/api/models')]);
  if (agents.error || models.error) { body.innerHTML = '<span style="color:var(--red)">加载失败</span>'; return; }

  const tiers = [
    {id:'E', color:'var(--cyan)', label:'E 层', desc:'日常执行 · bugfix · 查询'},
    {id:'E+', color:'var(--orange)', label:'E+ 层', desc:'复杂构建 · 多文件 · 新模块'},
    {id:'D', color:'var(--purple)', label:'D 层', desc:'架构设计 · 审查 · 方案'},
  ];

  body.innerHTML = tiers.map(t => {
    const tierAgents = agents[t.id] || [];
    const activeModels = new Set(tierAgents.map(a => a.model));
    const available = Object.values(models).filter(m => (m.tiers||[]).includes(t.id));

    const cards = available.map(m => {
      const active = activeModels.has(m.id);
      const online = m.api_available;
      const bg = active ? 'var(--bg3)' : 'transparent';
      const border = active ? t.color : 'var(--bg3)';
      const statusDot = online
        ? `<span style="color:var(--green);font-size:7px">●</span>`
        : `<span style="color:var(--red);font-size:7px">●</span>`;
      const costLabel = {budget:'$',standard:'$$',premium:'$$$'}[m.cost]||'$$';

      return `<div onclick="toggleLayerAgent('${t.id}','${esc(m.id)}',${!active})"
        style="cursor:pointer;padding:8px 10px;border:1px solid ${border};background:${bg};display:flex;align-items:center;gap:8px;min-width:180px;transition:all .15s">
        <span style="display:flex;align-items:center;gap:4px;min-width:50px">
          <span style="width:10px;text-align:center">${statusDot}</span>
          <span style="font-size:8px;font-family:var(--mono);text-transform:uppercase;color:${active?t.color:'var(--text3)'}">${active?'ON':'OFF'}</span>
        </span>
        <div style="min-width:0;flex:1">
          <div style="font-size:10px;font-weight:500;color:var(--text)">${esc(m.display||m.id)}</div>
          <div style="font-size:8px;color:var(--text3);font-family:var(--mono)">${esc(m.provider)} · ${m.speed} · ${costLabel}</div>
        </div>
      </div>`;
    }).join('');

    return `<div style="margin-bottom:10px">
      <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:6px">
        <span style="font-size:10px;font-weight:600;color:${t.color};font-family:var(--mono)">${t.label}</span>
        <span style="font-size:8px;color:var(--text3)">${t.desc}</span>
        <span style="flex:1"></span>
        <span style="font-size:8px;color:var(--text3);font-family:var(--mono)">${tierAgents.length} active</span>
      </div>
      <div style="display:flex;gap:4px;flex-wrap:wrap">${cards||'<span style="color:var(--text3);font-size:9px">无可用模型</span>'}</div>
    </div>`;
  }).join('');
}

async function toggleLayerAgent(tier, modelId, enable){
  if (enable) {
    // Add to this layer: clone from existing config or create new
    await api('/api/agents', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({level:tier,model:modelId})});
  } else {
    await api('/api/agents/'+tier+'/'+modelId, {method:'DELETE'});
  }
  renderLayerSwitch();
}

async function populateLineupProjects(){
  const sel = document.getElementById('lineup-project-select');
  try {
    const r = await api('/api/projects');
    const projects = r.projects || [];
    sel.innerHTML = '<option value="">— 选择项目 —</option>' +
      projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)} (${p.phase})</option>`).join('');
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════
// SSE 事件流 — 替代轮询
// ═══════════════════════════════════════════════════════
function connectSSE(){
  const es = new EventSource('/api/events');
  es.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if (d.kind === 'init') {
        // 初始状态 → 轻量更新
        statusData = {counts: d.counts||{}, running_total: d.running_total||0, heartbeat_levels:{}, agents:{}, avg_wait:'--', avg_done:'--'};
        renderStatusCards();
        if (d.running) updateLoopUI(true);
        // 渲染历史事件
        if (d.events && d.events.length) {
          const feed = document.getElementById('event-feed');
          if (feed) {
            feed.innerHTML = d.events.map(e => {
              const cls = 'ev-' + (e.kind || 'idle');
              const ts = new Date(e.ts * 1000).toLocaleTimeString('zh-CN');
              return '<div class="event-row"><span class="ev-ts">' + ts + '</span><span class="' + cls + '">' + esc(e.msg) + '</span></div>';
            }).join('');
          }
        }
      } else {
        // 事件推送 → 更新事件流
        const feed = document.getElementById('event-feed');
        if (feed && d.kind && d.msg) {
          const cls = d.kind==='task'?'ev-task':d.kind==='error'?'ev-error':d.kind==='system'?'ev-system':'ev-idle';
          const row = document.createElement('div');
          row.className = 'event-row';
          row.innerHTML = `<span class="ev-ts">${new Date(d.ts*1000).toLocaleTimeString('zh-CN')}</span><span class="${cls}">${esc(d.msg)}</span>`;
          feed.insertBefore(row, feed.firstChild);
          while (feed.children.length > 50) feed.removeChild(feed.lastChild);
        }
        // 数据面板自动刷新（debounce 2s，避免高频抖动）
        if (d.kind === 'task' || d.kind === 'system' || d.kind === 'workflow' || d.kind === 'memory') {
          _scheduleRefresh(2000);
        }
      }
    } catch(_){}
  };
  let _reconnectTimer = null;
  let _fallbackPolling = null;
  es.onerror = () => {
    es.close();
    if (!_fallbackPolling) {
      _fallbackPolling = setInterval(refreshAll, 3000);
      toast('实时推送断开，已切换轮询模式', 'error');
    }
    const ind = document.getElementById('live-indicator');
    if(ind) { ind.style.color = 'var(--red)'; ind.textContent = '◇'; }
    if (_reconnectTimer) clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(connectSSE, 5000);
  };
  es.onopen = () => {
    if (_fallbackPolling) { clearInterval(_fallbackPolling); _fallbackPolling = null; toast('实时推送已恢复', 'success'); }
    const ind = document.getElementById('live-indicator');
    if(ind) { ind.style.color = 'var(--green)'; ind.textContent = '◆'; }
  };
}

// ── Debounce helper for SSE-triggered refresh ──
let _refreshTimer = null;
function _scheduleRefresh(delay) {
  if (_refreshTimer) clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(refreshAll, delay);
}

// ═══════════════════════════════════════════════════════
// Judge Monitor & Pattern Profile
// ═══════════════════════════════════════════════════════
async function refreshJudgeMonitor(){
  try{
    const r=await fetch('/api/judge-monitor');
    const d=await r.json();
    if(d.error) return;
    const body=document.getElementById('judge-monitor-body');
    const flag=document.getElementById('jm-anomaly-flag');
    if(!body) return;
    let html='';

    // 通过率
    if(d.pass_rates_by_type){
      html+='<div style="color:var(--cyan);margin-bottom:4px">通过率</div>';
      for(const [type,stats] of Object.entries(d.pass_rates_by_type)){
        const color=stats.rate>0.9?'var(--orange)':stats.rate<0.1?'var(--red)':'var(--green)';
        html+=`<div>${type}: <span style="color:${color}">${(stats.rate*100).toFixed(0)}%</span> (${stats.passes}/${stats.total})</div>`;
      }
    }

    // 模型偏差
    if(d.model_correlations&&Object.keys(d.model_correlations).length){
      html+='<div style="color:var(--cyan);margin-top:6px">模型偏差</div>';
      for(const [m,c] of Object.entries(d.model_correlations)){
        if(c.bias_flag||c.total_judged>=5){
          const color2=c.bias_flag?'var(--orange)':c.avg_score<0.5?'var(--yellow)':'';
          html+=`<div>${m}: ${(c.avg_score*100).toFixed(0)}分 (${c.total_judged}次)${c.bias_flag?' ⚠':''}</div>`;
        }
      }
    }

    // 异常
    if(d.anomalies&&d.anomalies.length>0){
      if(flag) flag.style.display='inline';
      html+='<div style="color:var(--orange);margin-top:6px">异常</div>';
      for(const a of d.anomalies) html+=`<div style="color:var(--orange)">• ${esc(a.detail)}</div>`;
    }else if(flag) flag.style.display='none';

    body.innerHTML=html||'<span style="color:var(--text3)">暂无数据</span>';
  }catch(e){}
}

async function refreshPatternProfile(){
  try{
    const r=await fetch('/api/model-profile');
    const d=await r.json();
    if(d.error) return;
    const body=document.getElementById('pattern-profile-body');
    if(!body) return;
    let html='';

    const byType={};
    for(const [key,stats] of Object.entries(d.profiles||{})){
      const parts=key.split('/');
      const model=parts.slice(0,-1).join('/');
      const type=parts[parts.length-1];
      if(!byType[type]) byType[type]=[];
      byType[type].push({model,...stats});
    }

    for(const [type,models] of Object.entries(byType)){
      models.sort((a,b)=>b.success_rate-a.success_rate);
      html+=`<div style="color:var(--cyan);margin-top:4px">${type}</div>`;
      for(const m of models.slice(0,3)){
        html+=`<div>${m.model}: ${(m.success_rate*100).toFixed(0)}% (${m.total}次)</div>`;
      }
    }

    body.innerHTML=html||'<span style="color:var(--text3)">暂无数据</span>';
  }catch(e){}
}

// 每 30 秒刷新一次裁判监控和模式画像（低频，避免不必要的负载）
setInterval(refreshJudgeMonitor,30000);
setInterval(refreshPatternProfile,30000);

// ═══════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════
refreshAll();
refreshJudgeMonitor();
refreshPatternProfile();
connectSSE();
checkSetup();
