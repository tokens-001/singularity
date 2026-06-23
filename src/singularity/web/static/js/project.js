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
        ${c.error?`<span style="font-size:10px;color:var(--st-pending)">${esc(c.error.slice(0,80))}</span>`:''}
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
  const phases=['template','researching','gate1','planning','gate2','executing','reviewing','fixing','gate3','done'];
  const labels=['模板','调研','①调研','架构','②架构','执行','内审','修复','③交付','完成'];
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
  if (p.error) { document.getElementById('project-detail-body').innerHTML=`<span style="color:var(--st-fail)">${esc(p.error)}</span>`; return; }
  const phases=['template','researching','gate1','planning','gate2','executing','reviewing','fixing','gate3','done'];
  const labels=['📋模板','🔍调研','①调研','🏗架构','②架构','⚡执行','🔎内审','🔧修复','③交付','✅完成'];
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
        ${h.next_agent?`<div style="color:var(--accent)">→ ${esc(h.next_agent)}</div>`:''}
        ${h.human_confirm?'<div style="color:var(--st-pending)">⚠ 需人工确认</div>':''}
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
    activeAgent = `<span style="font-size:9px;color:var(--st-hold);font-family:var(--mono)">当前: ${esc(last.agent_model)} · ${esc(last.phase)}</span>`;
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


