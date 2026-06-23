// ═══════════════════════════════════════════════════════
// Dashboard 卡片网格
// ═══════════════════════════════════════════════════════
function fmt(n){ if(n==null)return'—'; if(typeof n==='number'){ if(n>1e6)return(n/1e6).toFixed(2)+'M'; if(n>1e3)return(n/1e3).toFixed(1)+'K'; return n.toFixed(0); } return n; }
function fmtT(v){ if(!v||v==='—')return'—'; const m=/^([\d.]+)s?$/.exec(String(v)); if(m)return parseFloat(m[1]).toFixed(1)+'s'; return v; }

// card(icon, title, bigVal, bigColor, sub, rows) → 统一卡片骨架
function card(icon,title,bigVal,bigColor,sub,rows){
  return`<div class="db-card"><div class="db-head"><span class="db-icon">${icon}</span><span class="db-title">${title}</span></div><div class="db-body"><div class="db-val" style="color:${bigColor||'var(--text)'}">${bigVal}</div><div class="db-sub">${sub}</div>${rows||''}</div></div>`;
}

async function renderStatusCards(){
  const grid=document.getElementById('db-grid');
  if(!grid) return;
  let h={status:'?',disk_free_mb:0,sse_clients:0,loop_running:false};
  try{ h=await(await fetch('/health')).json(); }catch(e){}
  const s=statusData, c=s.counts||{}, total=Object.values(c).reduce((a,b)=>a+b,0)||0;
  const ok=h.status==='ok', diskGB=Math.round((h.disk_free_mb||0)/1024);

  // 1. 健康
  const health=card('●', '健康', ok?'在线':'异常', ok?'var(--green)':'var(--red)',
    `磁盘 ${diskGB} GB  ·  调度${h.loop_running?'运行中':'已停'}  ·  SSE ${h.sse_clients||0}`);

  // 2. 任务
  const pp=total?Math.round(c.pending/total*100):0, rp=total?Math.round(c.running/total*100):0;
  const dp=total?Math.round(c.done/total*100):0, fp=total?Math.round(c.failed/total*100):0;
  const bar=total?`<div class="db-bar"><span class="b bp" style="width:${Math.max(pp,1)}%"></span><span class="b br" style="width:${Math.max(rp,1)}%"></span><span class="b bd" style="width:${Math.max(dp,1)}%"></span><span class="b bf" style="width:${Math.max(fp,1)}%"></span></div>`:'';
  const taskRows=(bar+'<div class="db-sep"></div>'+
    `<div class="db-row"><span class="db-l"><span class="db-dot db-dot-yellow"></span>待处理</span><span class="db-v">${c.pending||0}</span></div>`+
    `<div class="db-row"><span class="db-l"><span class="db-dot db-dot-green"></span>运行中</span><span class="db-v">${c.running||0}</span></div>`+
    `<div class="db-row"><span class="db-l"><span class="db-dot" style="background:var(--text3)"></span>已完成</span><span class="db-v">${c.done||0}</span></div>`+
    `<div class="db-row"><span class="db-l"><span class="db-dot db-dot-red"></span>失败</span><span class="db-v">${c.failed||0}</span></div>`);
  const tasks=card('☰','任务',total,'var(--text)',`共 ${total} 个任务`,taskRows);

  // 3. Token
  const tt=s.token_totals||{}, tTotal=Object.values(tt).reduce((a,b)=>a+b,0)||0;
  const tokenRows='<div class="db-sep"></div>'+
    `<div class="db-row"><span class="db-l"><span class="db-tag db-tag-e">E</span></span><span class="db-v">${fmt(tt.E||0)}</span></div>`+
    `<div class="db-row"><span class="db-l"><span class="db-tag db-tag-ep">E+</span></span><span class="db-v">${fmt(tt['E+']||0)}</span></div>`+
    `<div class="db-row"><span class="db-l"><span class="db-tag db-tag-d">D</span></span><span class="db-v">${fmt(tt.D||0)}</span></div>`;
  const token=card('◇','Token',fmt(tTotal),'var(--text)','累计消耗',tokenRows);

  // 4. Agent
  const ag=s.agents||{}, ac={}; Object.entries(ag).forEach(([l,v])=>{ac[l]=(v||[]).length;});
  const aTotal=Object.values(ac).reduce((a,b)=>a+b,0);
  const tier=(l,tag,cls)=>{
    const n=ac[l]||0, ns=(ag[l]||[]).map(a=>a.model).join(', ');
    return`<div class="db-row"><span class="db-l"><span class="db-tag ${cls}">${tag}</span></span><span class="db-v">${n}</span></div><div class="db-models">${ns||'—'}</div>`;
  };
  const agentRows='<div class="db-sep"></div>'+tier('E','E','db-tag-e')+tier('E+','E+','db-tag-ep')+tier('D','D','db-tag-d');
  const agent=card('◎','Agent',aTotal,'var(--text)',`${aTotal} 个模型就绪`,agentRows);

  // 5. 耗时
  const stalled=s.stalled||[];
  const perfRows='<div class="db-row"><span class="db-l">平均等待</span><span class="db-v">'+fmtT(s.avg_wait)+'</span></div>'+
    '<div class="db-row"><span class="db-l">平均完成</span><span class="db-v">'+fmtT(s.avg_done)+'</span></div>'+
    '<div class="db-sep"></div>'+
    `<div class="db-row"><span class="db-l">卡住</span><span class="db-v" style="color:${stalled.length?'var(--red)':'var(--text2)'}">${stalled.length}</span></div>`;
  const perf=card('⏱','耗时',fmtT(s.avg_done)||'—','var(--text)','平均完成耗时',perfRows);

  // 6. 项目
  let proj=card('▣','项目','—','var(--text)','加载中…');
  grid.innerHTML=health+tasks+token+agent+perf+proj;
  try{
    const ps=(await(await fetch('/api/projects')).json()).projects||[];
    const active=ps.filter(p=>!['done','archived'].includes(p.phase)).length;
    const done=ps.filter(p=>p.phase==='done').length;
    const projRows='<div class="db-sep"></div>'+
      `<div class="db-row"><span class="db-l">活跃</span><span class="db-v">${active}</span></div>`+
      `<div class="db-row"><span class="db-l">完成</span><span class="db-v">${done}</span></div>`;
    proj=card('▣','项目',ps.length,'var(--text)',`${active} 进行中  ·  ${done} 已完成`,projRows);
    grid.innerHTML=health+tasks+token+agent+perf+proj;
  }catch(e){}

  const totalTasks=Object.values(c).reduce((a,b)=>a+b,0);
  const ob=document.getElementById('onboarding');
  if(ob) ob.style.display=(totalTasks===0&&!_loop_running)?'block':'none';

  const sel=document.getElementById('filter-status');
  if(sel&&sel.options.length<=1){
    [...new Set(tasks.map(t=>t.status))].sort().forEach(s=>{
      const o=document.createElement('option');o.value=s;o.textContent=STATUS_CN[s]||s;sel.appendChild(o);
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
      if(u.warning) html+=`<br><span style="color:var(--st-pending)">⚠ ${esc(u.warning)}</span>`;
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
        onboarding.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px;color:var(--st-pending)">⚡ 快速设置</div>
          <div style="font-size:10px;color:var(--text2);line-height:1.8">
          <div>检测到 <b style="color:var(--st-fail)">0</b> 个可用 API。在 <b style="color:var(--accent);cursor:pointer" onclick="document.querySelector('[data-tab=config]').click()">配置 → API 库</b> 添加或启用。</div>
          <div style="margin-top:4px;color:var(--text3)">API Key 需在 .env 文件中配置环境变量后重启服务。</div>
          </div>`;
      }else if(active.length<2){
        onboarding.style.display='block';
        onboarding.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px;color:var(--accent)">${active.length} 个 API 就绪</div>
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
        html+=`<br>最慢: `+d.slowest_5.map(s=>`<span style="font-family:mono;font-size:9px;color:var(--st-pending)">${s.task_id} ${s.total_s}s</span>`).join(' ');
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
      <title>${n.label}: ${cnt} 个任务</title>
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

