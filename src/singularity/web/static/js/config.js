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
  const colors={critical:'var(--st-fail)',alert:'var(--st-pending)'};
  body.innerHTML=reports.map(r=>`<div style="padding:6px 0;border-bottom:1px solid var(--border)">
    <span style="color:${colors[r.severity]||'var(--text2)'};font-weight:600">[${r.severity}]</span>
    <b>${esc(r.title)}</b>
    <div style="color:var(--text2);margin-top:2px">${esc(r.what||'')}</div>
    ${r.suggestion?`<div style="color:var(--accent);font-size:9px;margin-top:2px">→ ${esc(r.suggestion)}</div>`:''}
  </div>`).join('');
}
function esc(s){if(!s)return'';const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function fmtDur(s){if(s<60)return Math.round(s)+'s';if(s<3600)return(s/60).toFixed(1)+'m';return(s/3600).toFixed(1)+'h';}
function unix2str(ts){if(!ts)return'--';return new Date(ts*1000).toLocaleString('zh-CN');}

// ═══════════════════════════════════════════════════════
// 推荐阶段 (两档后替代 E/E+/D)
// ═══════════════════════════════════════════════════════
const PHASES = ['定义','架构','实现','审查','验收','交付'];
const PHASE_COLORS = {
  '定义':'var(--accent)', '架构':'var(--st-hold)', '实现':'var(--st-done)',
  '审查':'var(--st-pending)', '验收':'var(--st-done)', '交付':'var(--text2)'
};

// ═══════════════════════════════════════════════════════
// API 库
// ═══════════════════════════════════════════════════════
async function renderAPIStore(){
  const body = document.getElementById('api-store-body');
  try {
    const d = await api('/api/api-store');
    if (d.error) { body.innerHTML = `<span style="color:var(--st-fail)">${esc(d.error)}</span>`; return; }
    const entries = Object.values(d);
    if (!entries.length) { body.innerHTML = '<span style="color:var(--text3);font-family:var(--mono);font-size:9px">NO_API_CONFIGURED</span>'; return; }
    const statusDot = {active:'<span style="color:var(--st-done);font-size:7px">●</span>',quota_exhausted:'<span style="color:var(--st-pending);font-size:7px">◑</span>',rate_limited:'<span style="color:var(--st-pending);font-size:7px">◐</span>',disabled:'<span style="color:var(--st-fail);font-size:7px">○</span>'};
    const statusLabel = {active:'ON',quota_exhausted:'QTA',rate_limited:'RTL',disabled:'OFF'};
    body.innerHTML = entries.map(e => {
      const dot = statusDot[e.status]||statusDot.disabled;
      const lbl = statusLabel[e.status]||'OFF';
      return `<div style="padding:4px 0;border-bottom:1px solid var(--bg3);display:flex;align-items:center;gap:8px;font-size:10px">
        <span style="width:12px;text-align:center">${dot}</span>
        <span style="font-family:var(--mono);font-weight:500;min-width:66px;color:var(--text)">${esc(e.provider)}</span>
        <span style="font-family:var(--mono);font-size:9px;color:var(--text3);min-width:28px">[${lbl}]</span>
        <span style="font-size:9px;color:var(--text3);flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(e.base_url)}</span>
        <button class="btn cyan sm" style="font-size:9px" onclick="scanModels('${esc(e.id)}')">🔍 扫描</button>
        <select onchange="setAPIStatus('${esc(e.id)}',this.value)" style="background:var(--bg);border:none;border-bottom:1px solid var(--grid);color:var(--text2);font-size:9px;padding:2px 4px;font-family:var(--mono);width:auto">
          <option value="">—</option><option value="active">ON</option><option value="quota_exhausted">QTA</option><option value="rate_limited">RTL</option><option value="disabled">OFF</option>
        </select>
        <button class="btn sm" style="color:var(--st-fail);font-size:8px;padding:1px 4px" onclick="removeAPI('${esc(e.id)}')">DEL</button>
      </div>`;
    }).join('');
  } catch(e) { body.innerHTML = `<span style="color:var(--st-fail)">${esc(e.message)}</span>`; }
}

async function addAPI(){
  const id = document.getElementById('api-new-id').value.trim();
  const provider = document.getElementById('api-new-provider').value.trim();
  const url = document.getElementById('api-new-url').value.trim();
  const env = document.getElementById('api-new-env').value.trim();
  if (!id) return alert('需要 id');
  const r = await api('/api/api-store', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,provider,base_url:url,api_key_env:env})});
  if (r.error) { alert(r.error); return; }
  document.getElementById('api-new-id').value='';
  document.getElementById('api-new-provider').value='';
  document.getElementById('api-new-url').value='';
  document.getElementById('api-new-env').value='';
  renderAPIStore();
  toast('API 已添加，正在扫描模型...', 'success');
  scanModels(id);
}

// ═══════════════════════════════════════════════════════
// 模型扫描 & 导入
// ═══════════════════════════════════════════════════════
let _scanResults = {};
let _importedModelIds = {};

async function scanModels(apiId){
  const btns = document.querySelectorAll(`button[onclick*="scanModels('${apiId}')"]`);
  btns.forEach(b => { b.textContent = '扫描中...'; b.disabled = true; });
  try {
    const existing = await api('/api/models');
    _importedModelIds = {};
    if (!existing.error) Object.values(existing).forEach(m => { _importedModelIds[m.id] = true; });
    const r = await api('/api/api-store/'+apiId+'/scan', {method:'POST'});
    if (r.error) { alert(r.error); return; }
    _scanResults[apiId] = r.models||[];
    renderScanResults(apiId);
    const news = (r.models||[]).filter(m => !_importedModelIds[m.id]).length;
    toast(`发现 ${r.total} 个模型${news ? '，'+news+' 个未导入' : '（全部已导入）'}`);
  } catch(e) { alert(e.message); }
  finally { btns.forEach(b => { b.textContent = '🔍 扫描'; b.disabled = false; }); }
}

function renderScanResults(apiId){
  const models = _scanResults[apiId]||[];
  const panel = document.getElementById('scan-results-panel');
  const body = document.getElementById('scan-results-body');
  if (!panel || !body) return;
  const newModels = models.filter(m => !_importedModelIds[m.id]);
  const oldModels = models.filter(m => _importedModelIds[m.id]);
  panel.style.display = 'block';
  const ratingColor = {'SSS+':'#ffd700','SSS':'var(--st-hold)','SS+':'#ff8c00','SS':'var(--accent)','S+':'#9acd32','S':'var(--st-done)','A+':'#5dade2','A':'var(--text3)','?':'var(--text3)'};
  const costLabel = {budget:'$',standard:'$$',premium:'$$$'};

  const renderCard = (m, alreadyImported) => {
    const rc = ratingColor[m.rating]||'var(--text3)';
    const cost = costLabel[m.cost]||'$$';
    const rf = m.recommended_for||m.tiers||[];
    const phaseTags = PHASES.map(p => {
      const on = rf.includes(p);
      return `<label style="cursor:pointer;color:${on?PHASE_COLORS[p]:'var(--text3)'};font-size:7px"><input type="checkbox" ${on?'checked':''}>${p.slice(0,1)}</label>`;
    }).join('');
    const statusTag = alreadyImported
      ? '<span style="color:var(--st-done);font-size:7px;background:rgba(62,207,142,.1);padding:2px 4px;border-radius:2px">已导入</span>'
      : (m.known ? '' : '<span style="color:var(--st-pending);font-size:7px;background:rgba(216,162,54,.1);padding:2px 4px;border-radius:2px">NEW</span>');
    return `<label style="display:flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid var(--bg3);cursor:${alreadyImported?'default':'pointer'};min-width:200px;${alreadyImported?'opacity:.5':''}">
      ${alreadyImported ? '<span style="color:var(--st-done);font-size:7px">✓</span>' :
        `<input type="checkbox" class="scan-check-${apiId}" value="${esc(m.id)}" ${m.known?'checked':''}>`}
      <div style="min-width:0">
        <div style="font-size:10px;color:var(--text)">${esc(m.display||m.id)} ${statusTag}</div>
        <div style="font-size:8px;color:var(--text3);font-family:var(--mono)">
          <span style="color:${rc}">${m.rating}</span> · ${m.speed} · ${cost}
        </div>
        ${m.strengths&&m.strengths.length?`<div style="font-size:7px;color:var(--text3)">${m.strengths.slice(0,3).join(' · ')}</div>`:''}
      </div>
      ${alreadyImported ? '' : `<span style="display:flex;gap:2px;flex-shrink:0" data-model="${esc(m.id)}">${phaseTags}</span>`}
    </label>`;
  };

  body.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-size:11px;font-weight:600;color:var(--accent)">🔍 ${apiId} — ${models.length} 个模型</span>
      <button class="btn purple sm" onclick="importSelected('${apiId}')">导入选中 (${newModels.length})</button>
      <button class="btn sm" onclick="selectAllScan('${apiId}')">全选未导入</button>
      <span style="flex:1"></span>
    </div>
    ${newModels.length ? '<div style="font-size:9px;color:var(--text2);margin-bottom:4px">🆕 未导入 ('+newModels.length+')</div>' : ''}
    <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">${newModels.map(m => renderCard(m, false)).join('')||'<span style="color:var(--text3);font-size:9px">全部已导入</span>'}</div>
    ${oldModels.length ? '<div style="font-size:9px;color:var(--text2);margin-bottom:4px">✅ 已导入 ('+oldModels.length+')</div><div style="display:flex;flex-wrap:wrap;gap:4px">'+oldModels.map(m => renderCard(m, true)).join('')+'</div>' : ''}`;
}

function selectAllScan(apiId){
  document.querySelectorAll('.scan-check-'+apiId).forEach(c => { if (!c.disabled) c.checked = true; });
}

async function importSelected(apiId){
  const checks = document.querySelectorAll('.scan-check-'+apiId+':checked');
  if (!checks.length) return alert('至少选一个模型');
  const models = Array.from(checks).map(cb => {
    const m = _scanResults[apiId].find(x=>x.id===cb.value);
    const phaseRow = cb.closest('label').querySelector('[data-model]');
    const phaseChecks = phaseRow ? phaseRow.querySelectorAll('input[type=checkbox]:checked') : [];
    const rf = Array.from(phaseChecks).map(c => PHASES[Array.from(phaseRow.children).indexOf(c.parentElement)]).filter(Boolean);
    return {id:m.id, provider:m.provider, display:m.display||m.id,
            recommended_for:rf.length?rf:(m.recommended_for||['实现']),
            speed:m.speed||'medium', cost:m.cost||'standard',
            rating:m.rating||'?', strengths:m.strengths||[], notes:m.notes||''};
  });
  const r = await api('/api/models/import', {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({models, auto_assign:true})});
  if (r.error) { alert(r.error); return; }
  toast(`导入 ${r.imported.length} 个模型`);
  if (r.errors.length) toast(`${r.errors.length} 个失败`, 'error');
  _scanResults[apiId] = [];
  document.getElementById('scan-results-panel').style.display = 'none';
  renderModels();
  renderAgentPool();
  populateLineupProjects();
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
  const speed=document.getElementById('model-new-speed').value;
  const cost=document.getElementById('model-new-cost').value;
  const reasoning=document.getElementById('model-new-reasoning').checked;
  if(!id||!provider) return alert('需要 id 和 provider');
  const r=await api('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,provider,display,recommended_for:['实现'],speed,cost,reasoning,max_turns:5})});
  if(r.error) alert(r.error); else {
    ['model-new-id','model-new-display'].forEach(x=>document.getElementById(x).value='');
    renderModels();
  }
}

function onProviderChange(){
  const sel = document.getElementById('model-new-provider');
  const provider = sel.options[sel.selectedIndex]?.dataset?.id || sel.value;
  if (!provider) return;
  const hints = {
    deepseek: { id: 'deepseek-chat', display: 'DeepSeek V3', speed: 'fast', cost: 'budget' },
    zhipu: { id: 'glm-5-turbo', display: 'GLM-5 Turbo', speed: 'slow', cost: 'budget' },
    kimi: { id: 'kimi-k2.7-code', display: 'Kimi K2.7', speed: 'medium', cost: 'standard' },
    anthropic: { id: 'claude-opus-4-8', display: 'Claude Opus 4.8', speed: 'slow', cost: 'premium' },
    openai: { id: 'gpt-5.5', display: 'GPT-5.5', speed: 'fast', cost: 'premium' },
  };
  const hint = hints[provider];
  if (hint) {
    document.getElementById('model-new-id').value = hint.id;
    document.getElementById('model-new-display').value = hint.display;
    document.getElementById('model-new-speed').value = hint.speed;
    document.getElementById('model-new-cost').value = hint.cost;
  }
}

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

const _origRenderModels = renderModels;
renderModels = async function() {
  await populateProviderDropdown();
  return _origRenderModels();
};

async function updateModel(id, field, value){
  const body = {};
  if (field === 'recommended_for') {
    const all = await api('/api/models');
    const model = all[id];
    if (!model) return;
    const rf = [...(model.recommended_for||model.tiers||[])];
    const idx = rf.indexOf(value);
    if (idx >= 0) rf.splice(idx, 1);
    else rf.push(value);
    if (!rf.length) return;
    body.recommended_for = rf;
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
  const rf = m.recommended_for||m.tiers||[];
  const html = '<div id="model-edit-'+esc(m.id)+'" style="margin:4px 0;padding:8px;border:1px solid var(--accent);background:var(--bg2)">推荐阶段：'+PHASES.map(p=>{
    const checked = rf.includes(p);
    return `<label style="display:inline-block;margin-right:8px;cursor:pointer;font-size:10px;color:${checked?PHASE_COLORS[p]:'var(--text3)'}"><input type="checkbox" id="em-${esc(m.id)}-${p}" ${checked?'checked':''}> ${p}</label>`;
  }).join('')+' <button class="btn sm" onclick="saveModelEdit(\''+esc(m.id)+'\')" style="margin-left:8px;margin-right:4px">保存</button><button class="btn sm" style="color:var(--text3)" onclick="editModel(\''+esc(m.id)+'\')">取消</button></div>';
  document.getElementById('models-body').insertAdjacentHTML('afterbegin', html);
}

async function saveModelEdit(id){
  const rf = PHASES.filter(p => document.getElementById('em-'+id+'-'+p)?.checked);
  if (!rf.length) { alert('至少选一个推荐阶段'); return; }
  const r = await api('/api/models/'+id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({recommended_for:rf})});
  if (r.error) alert(r.error); else renderModels();
}

async function removeModel(id){
  if(!confirm('删除模型 '+id+' ？')) return;
  const r=await api('/api/models/'+id,{method:'DELETE'});
  if(r.error) alert(r.error); else { renderModels(); renderAgentPool(); refreshAll(); }
}

async function renderModels(){
  const body = document.getElementById('models-body');
  try {
    const d = await api('/api/models');
    if (d.error) { body.innerHTML = '<span style="color:var(--st-fail)">'+esc(d.error)+'</span>'; return; }
    const models = Object.values(d);
    if (!models.length) { body.innerHTML = '<span style="color:var(--text3);font-family:var(--mono);font-size:9px">NO_MODELS_REGISTERED</span>'; return; }
    const costMark = {budget:'_',standard:'=',premium:'≡'};
    const providerNames = {deepseek:'DeepSeek',zhipu:'智谱 GLM',kimi:'Moonshot Kimi',openai:'OpenAI',anthropic:'Anthropic',qwen:'阿里云 千问'};
    const groups = {};
    for (const m of models) {
      const p = m.provider || 'other';
      if (!groups[p]) groups[p] = [];
      groups[p].push(m);
    }
    function renderCard(m){
      const rf = (m.recommended_for||m.tiers||[]);
      const phaseTags = rf.map(p => `<span style="color:${PHASE_COLORS[p]||'var(--text3)'};font-family:var(--mono);font-size:7px">${p}</span>`).join(' ');
      const dot = m.api_available
        ? '<span style="color:var(--st-done);font-size:7px">●</span>'
        : '<span style="color:var(--text3);font-size:7px">○</span>';
      const cm = costMark[m.cost]||'_';
      let html = '<div style="padding:4px 0 4px 12px;border-bottom:1px solid var(--bg3)">';
      html += '<div style="display:flex;align-items:center;gap:8px;font-size:10px">';
      html += '<span style="width:12px;text-align:center">'+dot+'</span>';
      html += '<span style="font-family:var(--mono);font-weight:500;min-width:80px;color:var(--text)">'+esc(m.display)+'</span>';
      html += '<span style="font-family:var(--mono);font-size:9px;color:var(--text3)">'+esc(m.id)+'</span>';
      const ratingColors = {'SSS+':'#ffd700','SSS':'var(--st-hold)','SS+':'#ff8c00','SS':'var(--accent)','S+':'#9acd32','S':'var(--st-done)','A+':'#5dade2','A':'var(--text3)'};
      if (m.rating) html += '<span style="font-size:8px;font-weight:600;color:'+(ratingColors[m.rating]||'var(--text3)')+';font-family:var(--mono)">'+esc(m.rating)+'</span>';
      html += '<span style="font-size:9px;color:var(--text3);font-family:var(--mono)">'+cm+'</span>';
      html += '<span style="font-size:8px;color:var(--text3)">'+m.speed+'</span>';
      if (m.reasoning) html += '<span style="font-size:8px;color:var(--st-hold);font-family:var(--mono)">RSN</span>';
      html += '<span style="flex:1"></span>';
      html += '<span style="font-size:8px;font-family:var(--mono)">'+phaseTags+'</span>';
      html += ' <button class="btn sm" style="color:var(--accent);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();editModel(\''+esc(m.id)+'\')">✎</button>';
      html += ' <button class="btn sm" style="color:var(--st-fail);font-size:7px;padding:1px 3px;margin-left:4px" onclick="event.stopPropagation();removeModel(\''+esc(m.id)+'\')">DEL</button>';
      html += '</div>';
      html += '<div style="font-size:8px;color:var(--text3);margin-top:2px;font-family:var(--mono)">'+esc(m.notes||'')+'</div>';
      html += '<div style="margin-top:2px;display:flex;gap:2px">';
      for (const p of PHASES) {
        const on = rf.includes(p);
        html += `<span onclick="event.stopPropagation();updateModel('${esc(m.id)}','recommended_for','${p}')" style="cursor:pointer;font-size:7px;font-family:var(--mono);padding:1px 3px;color:${on?PHASE_COLORS[p]:'var(--text3)'};background:${on?'var(--bg3)':'transparent'}">[${p.slice(0,1)}]</span>`;
      }
      html += '<span style="font-size:7px;color:var(--text3);font-family:var(--mono)"> 推荐</span>';
      html += '</div></div>';
      return html;
    }
    let html = '';
    const order = ['deepseek','zhipu','kimi','qwen','openai','anthropic','other'];
    for (const p of order) {
      if (!groups[p] || !groups[p].length) continue;
      html += '<div style="margin-bottom:8px">';
      html += '<div style="color:var(--accent);font-size:10px;font-weight:500;padding:4px 0;border-bottom:1px solid var(--accent)">'+esc(providerNames[p]||p)+' <span style="color:var(--text3);font-size:8px">'+groups[p].length+'个</span></div>';
      html += groups[p].map(renderCard).join('');
      html += '</div>';
    }
    body.innerHTML = html;
  } catch(e) { body.innerHTML = '<span style="color:var(--st-fail)">'+esc(e.message)+'</span>'; }
}

// ═══════════════════════════════════════════════════════
// Agent 池 (两档后: 全池展示, 不分组)
// ═══════════════════════════════════════════════════════
async function renderAgentPool(){
  const body = document.getElementById('layer-switch-body');
  const [agents, models] = await Promise.all([api('/api/agents'), api('/api/models')]);
  if (agents.error || models.error) { body.innerHTML = '<span style="color:var(--st-fail)">加载失败</span>'; return; }

  const allModels = Object.values(models);
  const allAgents = [];
  for (const [lvl, lst] of Object.entries(agents)) {
    if (!Array.isArray(lst)) continue;
    for (const a of lst) allAgents.push({...a, _level: lvl});
  }
  const activeModels = new Set(allAgents.map(a => a.model));
  const disabledModels = new Set();
  for (const [lvl, lst] of Object.entries(agents._disabled||{})) {
    if (Array.isArray(lst)) lst.forEach(m => disabledModels.add(m));
  }

  const cards = allModels.map(m => {
    const active = activeModels.has(m.id);
    const disabled = disabledModels.has(m.id);
    const online = m.api_available;
    const rf = m.recommended_for||m.tiers||[];
    const bg = active ? 'var(--bg3)' : (disabled ? 'rgba(240,97,109,.04)' : 'transparent');
    const border = active ? 'var(--accent)' : (disabled ? 'rgba(240,97,109,.3)' : 'var(--bg3)');
    const costLabel = {budget:'$',standard:'$$',premium:'$$$'}[m.cost]||'$$';
    const stateLabel = active ? 'ON' : (disabled ? '禁用' : 'OFF');
    const stateColor = active ? 'var(--accent)' : (disabled ? 'var(--st-fail)' : 'var(--text3)');
    const clickAction = disabled
      ? `if(confirm('重新启用 ${m.display||m.id}？'))togglePoolAgent('${esc(m.id)}',true)`
      : `togglePoolAgent('${esc(m.id)}',${!active})`;
    const phaseHint = rf.length ? rf.map(p => p.slice(0,1)).join('') : '—';

    return `<div onclick="${clickAction}"
      style="cursor:pointer;padding:8px 10px;border:1px solid ${border};background:${bg};display:flex;align-items:center;gap:8px;min-width:180px;transition:all .15s"
      title="${disabled?'点击重新启用':'点击切换启用/禁用'}">
      <span style="display:flex;align-items:center;gap:4px;min-width:40px">
        <span style="width:10px;text-align:center">${online?'<span style="color:var(--st-done);font-size:7px">●</span>':'<span style="color:var(--st-fail);font-size:7px">●</span>'}</span>
        <span style="font-size:8px;font-family:var(--mono);text-transform:uppercase;color:${stateColor}">${stateLabel}</span>
      </span>
      <div style="min-width:0;flex:1">
        <div style="font-size:10px;font-weight:500;color:${active?'var(--text)':'var(--text3)'}">${esc(m.display||m.id)}</div>
        <div style="font-size:8px;color:var(--text3);font-family:var(--mono)">${esc(m.provider)} · ${m.speed} · ${costLabel} · ${phaseHint}</div>
      </div>
    </div>`;
  }).join('');

  body.innerHTML = `
    <div style="margin-bottom:10px">
      <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:6px">
        <span style="font-size:10px;font-weight:600;color:var(--accent);font-family:var(--mono)">Agent 池</span>
        <span style="font-size:8px;color:var(--text3)">${allAgents.length} active · ${disabledModels.size} 禁用 · 全池自由选择</span>
      </div>
      <div style="display:flex;gap:4px;flex-wrap:wrap">${cards||'<span style="color:var(--text3);font-size:9px">无可用模型</span>'}</div>
    </div>`;
}

async function togglePoolAgent(modelId, enable){
  if (enable) {
    await api('/api/agents', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:modelId})});
  } else {
    await api('/api/agents/any/'+modelId, {method:'DELETE'});
  }
  renderAgentPool();
  const s = await api('/api/status');
  if (s && s.agents) { statusData = s; renderAgentRow(); }
}

// backward compat
async function renderLayerSwitch(){ renderAgentPool(); }

// ═══════════════════════════════════════════════════════
// Lineup (两档后: 全池勾选, 不分层)
// ═══════════════════════════════════════════════════════
async function loadLineup(){
  const sel = document.getElementById('lineup-project-select');
  const pid = sel.value;
  const editor = document.getElementById('lineup-editor');
  const status = document.getElementById('lineup-status');
  if (!pid) { editor.style.display='none'; status.textContent=''; return; }

  const r = await api(`/api/projects/${pid}/lineup`);
  const lineup = (r.lineup) || {};
  const allMods = await api('/api/models');

  function renderCheckboxes(models, containerId){
    const c = document.getElementById(containerId);
    const selected = lineup['any'] || [];
    c.innerHTML = models.map(m => {
      const checked = selected.includes(m.id) ? 'checked' : '';
      const dot = m.api_available
        ? '<span style="color:var(--st-done);font-size:7px">●</span>'
        : '<span style="color:var(--text3);font-size:7px">○</span>';
      return `<label style="display:block;padding:2px 0;cursor:pointer;font-family:var(--mono);font-size:9px">
        <input type="checkbox" value="${esc(m.id)}" ${checked} onchange="lineupChanged()"
          ${m.api_available?'':'disabled'}>
        ${dot} ${esc(m.display)} <span style="color:var(--text3)">${m.cost}</span>
      </label>`;
    }).join('');
  }

  renderCheckboxes(Object.values(allMods), 'lineup-e');
  document.getElementById('lineup-ep').parentElement.style.display = 'none';
  document.getElementById('lineup-d').parentElement.style.display = 'none';
  editor.style.display = 'block';
  status.textContent = lineup && Object.keys(lineup).length ? '已有自定义编组' : '使用全池默认';
}

function lineupChanged(){
  document.getElementById('lineup-status').textContent = '已修改，待保存';
  document.getElementById('lineup-status').style.color = 'var(--st-pending)';
}

async function saveLineup(){
  const pid = document.getElementById('lineup-project-select').value;
  if (!pid) return;
  function getChecked(containerId){
    const c = document.getElementById(containerId);
    return [...c.querySelectorAll('input:checked')].map(cb => cb.value);
  }
  const lineup = {any: getChecked('lineup-e')};
  const r = await api(`/api/projects/${pid}/lineup`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({lineup})});
  if (r.error) { alert(r.error); return; }
  document.getElementById('lineup-status').textContent = '已保存 ✓';
  document.getElementById('lineup-status').style.color = 'var(--st-done)';
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
// SSE 事件流 — 替代轮询，Last-Event-ID + 指数退避重连
// ═══════════════════════════════════════════════════════
let _sseBackoff = 1000;            // 退避延迟，初始 1s，指数增长至 30s
const _SSE_BACKOFF_MAX = 30000;
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
              const kindToCls = {'task':'ev-task','error':'ev-error','system':'ev-system',
                                  'tool:start':'ev-tool-start','tool:done':'ev-tool-done',
                                  'turn':'ev-turn','approval':'ev-approval','subagent':'ev-subagent'};
              const cls = kindToCls[e.kind] || 'ev-' + (e.kind || 'idle');
              const ts = new Date(e.ts * 1000).toLocaleTimeString('zh-CN');
              return '<div class="event-row"><span class="ev-ts">' + ts + '</span><span class="' + cls + '">' + esc(e.msg) + '</span></div>';
            }).join('');
          }
        }
      } else {
        // 事件推送 → 更新事件流
        const feed = document.getElementById('event-feed');
        if (feed && d.kind && d.msg) {
          const kindToCls = {'task':'ev-task','error':'ev-error','system':'ev-system',
                              'tool:start':'ev-tool-start','tool:done':'ev-tool-done',
                              'turn':'ev-turn','approval':'ev-approval','subagent':'ev-subagent'};
          const cls = kindToCls[d.kind] || 'ev-idle';
          const row = document.createElement('div');
          row.className = 'event-row';
          row.innerHTML = `<span class="ev-ts">${new Date(d.ts*1000).toLocaleTimeString('zh-CN')}</span><span class="${cls}">${esc(d.msg)}</span>`;
          feed.insertBefore(row, feed.firstChild);
          while (feed.children.length > 50) feed.removeChild(feed.lastChild);
        }
        // 数据面板自动刷新（debounce 2s，避免高频抖动）
        if (d.kind === 'task' || d.kind === 'system' || d.kind === 'workflow' || d.kind === 'memory' || d.kind === 'turn' || d.kind === 'agent_change') {
          _scheduleRefresh(2000);
        }
      }
    } catch(_){}
  };
  let _sseBackoff = 1000;            // 指数退避初始 1s
  const _SSE_BACKOFF_MAX = 30000;   // 退避上限 30s
  let _reconnectTimer = null;
  let _fallbackPolling = null;
  es.onerror = () => {
    es.close();
    if (!_fallbackPolling) {
      _fallbackPolling = setInterval(refreshAll, 3000);
      toast('实时推送断开，已切换轮询模式', 'error');
    }
    const ind = document.getElementById('live-indicator');
    if(ind) { ind.style.color = 'var(--st-fail)'; ind.textContent = '◇'; }
    if (_reconnectTimer) clearTimeout(_reconnectTimer);
    // 指数退避: 1s → 2s → 4s → ... → max 30s
    _reconnectTimer = setTimeout(() => { connectSSE(); }, _sseBackoff);
    _sseBackoff = Math.min(_sseBackoff * 2, _SSE_BACKOFF_MAX);
  };
  es.onopen = () => {
    _sseBackoff = 1000;  // 连接成功，重置退避
    if (_fallbackPolling) { clearInterval(_fallbackPolling); _fallbackPolling = null; toast('实时推送已恢复', 'success'); }
    const ind = document.getElementById('live-indicator');
    if(ind) { ind.style.color = 'var(--st-done)'; ind.textContent = '◆'; }
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
      html+='<div style="color:var(--accent);margin-bottom:4px">通过率</div>';
      for(const [type,stats] of Object.entries(d.pass_rates_by_type)){
        const color=stats.rate>0.9?'var(--st-pending)':stats.rate<0.1?'var(--st-fail)':'var(--st-done)';
        html+=`<div>${type}: <span style="color:${color}">${(stats.rate*100).toFixed(0)}%</span> (${stats.passes}/${stats.total})</div>`;
      }
    }

    // 模型偏差
    if(d.model_correlations&&Object.keys(d.model_correlations).length){
      html+='<div style="color:var(--accent);margin-top:6px">模型偏差</div>';
      for(const [m,c] of Object.entries(d.model_correlations)){
        if(c.bias_flag||c.total_judged>=5){
          const color2=c.bias_flag?'var(--st-pending)':c.avg_score<0.5?'var(--yellow)':'';
          html+=`<div>${m}: ${(c.avg_score*100).toFixed(0)}分 (${c.total_judged}次)${c.bias_flag?' ⚠':''}</div>`;
        }
      }
    }

    // 异常
    if(d.anomalies&&d.anomalies.length>0){
      if(flag) flag.style.display='inline';
      html+='<div style="color:var(--st-pending);margin-top:6px">异常</div>';
      for(const a of d.anomalies) html+=`<div style="color:var(--st-pending)">• ${esc(a.detail)}</div>`;
    }else if(flag) flag.style.display='none';

    body.innerHTML=html||'<div class="empty-state">暂无裁判数据</div>';
  }catch(e){ body.innerHTML='<div class="error-inline" onclick="refreshJudgeMonitor()">加载失败，点击重试</div>'; }
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
      html+=`<div style="color:var(--accent);margin-top:4px">${type}</div>`;
      for(const m of models.slice(0,3)){
        html+=`<div>${m.model}: ${(m.success_rate*100).toFixed(0)}% (${m.total}次)</div>`;
      }
    }

    body.innerHTML=html||'<div class="empty-state">暂无画像数据</div>';
  }catch(e){ body.innerHTML='<div class="error-inline" onclick="refreshPatternProfile()">加载失败，点击重试</div>'; }
}

// 每 30 秒刷新一次裁判监控和模式画像（低频，避免不必要的负载）
setInterval(refreshJudgeMonitor,30000);
setInterval(refreshPatternProfile,30000);

// ═══════════════════════════════════════════════════════
// MCP 服务器管理
// ═══════════════════════════════════════════════════════

async function renderMCPServers(){
  const body = document.getElementById('mcp-servers-body');
  const count = document.getElementById('mcp-tool-count');
  try {
    const r = await fetch('/api/mcp/servers');
    const data = await r.json();
    if (!data.servers || !data.servers.length) {
      body.innerHTML = '<span style="color:var(--text2)">无 MCP 服务器。点击"+ 添加"配置。</span>';
      if (count) count.textContent = '';
      return;
    }
    let html = '<table style="width:100%;border-collapse:collapse;font-size:9px">';
    html += '<tr style="color:var(--text2)"><th style="text-align:left;padding:2px 4px">名称</th><th style="text-align:left;padding:2px 4px">传输</th><th style="text-align:left;padding:2px 4px">命令/URL</th><th style="text-align:center;padding:2px 4px">工具数</th><th style="text-align:center;padding:2px 4px">操作</th></tr>';
    for (const s of data.servers) {
      const connected = s.connected ? '🟢' : '🔴';
      const addr = s.transport === 'stdio' ? (s.command || '').substring(0, 60) : (s.url || '').substring(0, 60);
      html += `<tr style="border-top:1px solid var(--grid)">
        <td style="padding:2px 4px">${connected} ${escHtml(s.name)}</td>
        <td style="padding:2px 4px;color:var(--text2)">${escHtml(s.transport)}</td>
        <td style="padding:2px 4px;font-size:8px;color:var(--text2)">${escHtml(addr)}</td>
        <td style="padding:2px 4px;text-align:center;color:var(--fg2)">${s.tool_count}</td>
        <td style="padding:2px 4px;text-align:center">
          <button class="btn cyan sm" style="font-size:8px;padding:1px 4px" onclick="reconnectMCPServer('${escHtml(s.name)}')">重连</button>
          <button class="btn sm" style="font-size:8px;padding:1px 4px;color:#f44747" onclick="deleteMCPServer('${escHtml(s.name)}')">删</button>
        </td></tr>`;
    }
    html += '</table>';
    body.innerHTML = html;
    // 工具总数
    try {
      const tr = await fetch('/api/mcp/tools');
      const td = await tr.json();
      if (count) count.textContent = `共 ${(td.tools||[]).length} 个工具`;
    } catch(e) {}
  } catch(e) {
    body.innerHTML = `<span style="color:#f44747">加载失败: ${escHtml(e.message)}</span>`;
  }
}

function toggleMCPForm(){
  const form = document.getElementById('mcp-form');
  form.style.display = form.style.display === 'none' ? 'block' : 'none';
  if (form.style.display !== 'none') onMCPTransportChange();
}

function onMCPTransportChange(){
  const transport = document.getElementById('mcp-new-transport').value;
  document.getElementById('mcp-new-command').style.display = transport === 'stdio' ? '' : 'none';
  document.getElementById('mcp-new-url').style.display = transport === 'http' ? '' : 'none';
}

async function addMCPServer(){
  const name = document.getElementById('mcp-new-name').value.trim();
  if (!name) { alert('请输入名称'); return; }
  const transport = document.getElementById('mcp-new-transport').value;
  const command = document.getElementById('mcp-new-command').value.trim();
  const url = document.getElementById('mcp-new-url').value.trim();
  const timeout = parseFloat(document.getElementById('mcp-new-timeout').value) || 30;
  const enabled = document.getElementById('mcp-new-enabled').checked;
  const body = {name, transport, timeout, enabled};
  if (transport === 'stdio') body.command = command;
  else body.url = url;
  try {
    const r = await fetch('/api/mcp/servers', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    if (r.ok) {
      toggleMCPForm();
      renderMCPServers();
    } else {
      const e = await r.json();
      alert(`保存失败: ${e.error||r.status}`);
    }
  } catch(e) {
    alert(`请求失败: ${e.message}`);
  }
}

async function deleteMCPServer(name){
  if (!confirm(`删除 MCP 服务器 "${name}"?`)) return;
  try {
    await fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {method:'DELETE'});
    renderMCPServers();
  } catch(e) {
    alert(`删除失败: ${e.message}`);
  }
}

async function reconnectMCPServer(name){
  try {
    const r = await fetch(`/api/mcp/servers/${encodeURIComponent(name)}/reconnect`, {method:'POST'});
    const data = await r.json();
    if (data.ok) {
      renderMCPServers();
    } else {
      alert(`重连失败: ${data.error}`);
    }
  } catch(e) {
    alert(`重连失败: ${e.message}`);
  }
}

async function refreshMCPTools(){
  try {
    const r = await fetch('/api/mcp/refresh', {method:'POST'});
    const data = await r.json();
    renderMCPServers();
  } catch(e) {
    alert(`刷新失败: ${e.message}`);
  }
}
