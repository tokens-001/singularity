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
    const dotColor={'pending':'var(--yellow)','routed':'var(--text2)','dispatched':'var(--accent)','running':'var(--accent)','validating':'var(--purple)','done':'var(--green)','failed':'var(--red)','rolled_back':'var(--red)','decomposed':'var(--yellow)','blocked':'var(--purple)','conflict_held':'var(--purple)'};
    const dc=dotColor[t.status]||'var(--text3)';
    return `<tr data-action="toggle-detail" data-task-id="${t.id}" id="row-${t.id}" style="border-left:3px solid ${dc}">
      <td style="font-family:'SF Mono',monospace;font-size:10px;color:var(--text2)">${idShort}${heldMark}</td>
      <td>${esc(desc)}</td>
      <td><span class="badge ${t.status}" style="display:inline-flex;align-items:center;gap:4px"><span style="width:6px;height:6px;border-radius:50%;flex-shrink:0;background:${dc}"></span>${status}</span></td>
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
        ${node.meta&&node.meta.error?`<span class="tl-meta" style="color:var(--st-fail)">${esc(node.meta.error.slice(0,80))}</span>`:''}
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
        ${data.route_locked?'<span style="font-size:9px;color:var(--st-pending);margin-left:4px">🔒锁定</span>':''}
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
        html+=`<div class="kb-hit">▸ <span style="color:var(--accent)">score=${(d.score||0).toFixed(1)}</span> · ${esc((d.title||d.id||'').slice(0,60))}</div>`;
      });
    }
    if(pre.memory&&!pre.memory.error){
      const mem=pre.memory;
      html+=`<div style="margin-top:6px;font-weight:600;font-size:10px">🧠 MAGMA 记忆 · intent=${mem.intent||'?'}</div>`;
      if(mem.narrative&&mem.narrative.length){
        mem.narrative.slice(0,3).forEach(h=>{
          html+=`<div class="magma-hit">
            <span style="color:var(--st-hold)">${(h.score||0).toFixed(4)}</span>
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
        ${t.route_locked?'<span style="font-size:9px;color:var(--st-pending)">🔒</span>':''}
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
        <span class="pulse-dot" style="width:5px;height:5px;background:var(--accent);display:inline-block;border-radius:50%"></span>
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
        ${t.error?`<span style="font-size:10px;color:var(--st-fail)">${esc(t.error.slice(0,60))}</span>`:''}
      </span>
      <button class="btn cyan sm" onclick="retryTask('${t.id}')">重试</button>
    </div>`).join(''):'<span style="color:var(--text2);font-size:11px">无可重试任务</span>';
}

